# Firmware and bootloader transfer

Short version, up front: **you cannot build or modify a firmware image.** The images the
vendor serves are encrypted under a key that exists only inside the vendor's own
toolchain and the device's bootloader. What you *can* do is relay a vendor image verbatim,
because the container is fully understood. Whether you should is another matter — see
[the warning](#should-a-reimplementation-do-this-at-all).

## Three transports, not one

This is the most important structural fact, and it is easy to miss:

| what is updated | who drives it | transport |
|---|---|---|
| **app firmware** | the **bootloader**, after the device reboots | a **dedicated connection** on the same port, with a different `ProtocolHeader.Version` |
| **bootloader** | the **running app firmware** | the normal device connection, carrying pre-framed legacy packets replayed verbatim |
| **WiFi module** | the **running app firmware** | the normal device connection: the blob is written to the device filesystem as a file, then a parameter write starts the flash |

[D] throughout this section unless marked otherwise.

### The app-firmware flow

1. The server decides an update is wanted.
2. It sets a per-device option `RequestedFirmware = "<crc as 8 lowercase hex>"`, persists
   it, logs `"rebooting device to update firmware"`, and sends a **reboot command**. It
   does **not** transfer any firmware at this point. (If the device has no bootloader it
   logs `"device doesn't have bootloader when updating firmware, skipping reboot"`.)
3. The device reboots into its bootloader and opens a **separate** TCP connection, which
   the mux routes by `ProtocolHeader.Version` to the firmware protocol handler.
4. That handler reads and verifies a header, parses the device's Start packet, mirrors its
   fields into the device record, looks up `RequestedFirmware` (falling back to the
   currently recorded `Firmware`), and starts flashing:
   1. ACK the Start packet;
   2. resolve the CRC to an image, else `"requested firmware not found"`;
   3. check it fits — `"no enough space on device: required %d, available %d"` (sic),
      against the Start packet's `Space` field;
   4. stream the blocks.
5. On success the server deletes `RequestedFirmware` so the device does not loop.

## Framing on the firmware connection

```go
// 8 bytes, little-endian
type Header struct {
    Version uint32   // off 0
    Type    uint32   // off 4
}
```

The header verifier accepts exactly three versions and requires `Type == 3`:

| `Header.Version` | protocol generation | Start packet |
|---|---|---|
| `0xF0000000` | v1 | `StartV1` |
| `0xF0000001` | v2 | `StartV2` |
| `0xF0000002` | v3 | `StartV3` |

Note that the **mux** registers only `0xF0000001` and `0xF0000002`
([framing.md](framing.md#the-listener-and-the-version-mux)), while the header verifier
inside the protocol accepts `0xF0000000` too.

`Header.Type` is the sub-packet kind:

| `Type` | kind | direction |
|---|---|---|
| 1 | Control (ACK/NACK) | server → device |
| 2 | Data (one firmware block) | server → device |
| 3 | Start (the first packet) | device → server |

Every packet on this connection is the 8-byte `Header` followed by a fixed-size body, read
with little-endian binary decoding. It is **not** the normal `ProtocolHeader` /
`DataHeader` nesting.

## The Start packet (device → server)

```go
// 28 bytes
type StartV1 struct {
    FW    uint32    // off 0   running app firmware CRC32 (0 == none / erased)
    HW    uint32    // off 4   hardware id word (0 => unconfigured)  [I] meaning
    Space uint32    // off 8   available space for the image, bytes
    ID    [16]byte  // off 12  device UUID
}

// 76 bytes
type StartV2 struct {
    StartV1                      // off 0
    FirmwareMajor      uint32    // off 28
    FirmwareMinor      uint32    // off 32
    FirmwareRevision   uint32    // off 36
    BootloaderMajor    uint32    // off 40
    BootloaderMinor    uint32    // off 44
    BootloaderRevision uint32    // off 48
    HardwareNameID     uint32    // off 52
    HardwareMajor      uint32    // off 56
    HardwareMinor      uint32    // off 60
    HardwareRevision   uint32    // off 64
    HWFWInterface      uint32    // off 68   = status "HardwareFirmwareIface"
    HeartbeatInterval  uint32    // off 72
}

// 80 bytes
type StartV3 struct {
    StartV2                      // off 0
    DisplayID uint32             // off 76   = status DisplayType / DisplayIds
}
```

The server mirrors these into the device's record, and sets
`ApplicationVersion = fmt.Sprintf("%x", FirmwareCRC())`.

**The firmware handle is the CRC-32 of the image, printed as 8 lowercase hex digits.**
Verified: a live sign reports `ApplicationVersion = "652be4ad"`, and the vendor's catalogue
entry for it is `1_652be4ad_7.4.4407_8_1.1.0_3255107880_universal.firmware`. [W]

If the reported firmware revision is `0xFFFF`, the server logs
`"legacy bootloader detected w/o next-gen firmware, will flash"`.

## The Data packet

```go
// 12 bytes — firmware protocol v1
type DataV1 struct {
    PacketNumber  uint16  // off 0   1-based block index
    Flags         uint16  // off 2
    PayloadLength uint16  // off 4   bytes of payload that follow
    Uncompressed  uint16  // off 6   size after decompression
    Checksum      uint32  // off 8
}

// 16 bytes — firmware protocol v2/v3
type DataV2 struct {
    PacketNumber  uint16  // off 0
    Flags         uint16  // off 2
    PayloadLength uint16  // off 4
    Uncompressed  uint16  // off 6
    Payload       uint16  // off 8    <- the only new semantic field
    Reserved      uint16  // off 10
    Checksum      uint32  // off 12
}
```

**The difference between V1 and V2 is exactly the extra `Payload` + `Reserved` uint16
pair.**

### `Flags`

The server's own packer stores `1` in `Flags` when LZ4 shrank the chunk and `0` when it
did not — so **bit 0 = "payload is LZ4-compressed"**, and note this is the *opposite*
polarity from the transport block chain's `Stored` field. [D]

From the real vendor images, additionally: [W]

| bit | mask | meaning |
|---:|---|---|
| 0 | `0x1` | payload is LZ4-compressed |
| 2 | `0x4` | **last block** |
| 3 | `0x8` | **encrypted** — set on every block of every vendor image |

**[I]** for bits 2 and 3 — derived from the images, not from code. Observed `Flags` values
in vendor images are `0x09` for ordinary blocks and `0x0d` for the final one. [W]

### `Payload` is a padding length

**`Payload` is the number of padding bytes added to 16-align the block for the cipher**, so
the real compressed length is `PayloadLength - Payload`. Confirmed arithmetically against
reconstructed known plaintext. [W] The server's own packer writes `Payload = 0` — it
stores a zero 32-bit word over the `Payload` + `Reserved` pair — because it does not
encrypt. [D]

### `Checksum` is over the plaintext

**Standard zlib/IEEE CRC-32 over the fully decompressed plaintext block** — not the LZ4
stream, and not the ciphertext. **Verified**, not inferred: a run of 13 identical records
in two different images turned out to be a constant-fill run, and
`crc32(b"\x00" * 2020)` equals their `Checksum` exactly; the same check closes for a
512-byte run in an older image. [W]

## Control packets and the NACK codes

```go
// 8 bytes
type Control struct {
    PacketNumber uint16  // off 0  the block being acknowledged
    Ack          uint16  // off 2  0xFFFF == NACK, anything else == ACK
    Info         uint32  // off 4  NACK reason code
}
```

`Info` codes, from the server's error map (base value `0x10000`): [D]

| `Info` | message |
|---|---|
| `0x10000` | `version not supported` |
| `0x10001` | `packet number mismatch` |
| `0x10002` | `payload size mismatch` |
| `0x10003` | `payload size too big` |
| `0x10004` | `uncompression failed` |
| `0x10005` | `CRC failed` |
| `0x10006` | `unexpected packet type` |
| `0x10007` | `ack/nack field value not known` |

Server-internal errors in the same package, never on the wire: `timed out`,
`sent incomplete data`, `resend limit reached`, `invalid version`,
`already in progress`. [D]

## The transfer state machine

```
Start():
  if already in progress -> error
  for i = 1 .. totalBlocks:               # 1-BASED
      blockInAir = i
      report progress (percent = 100*i/total, capped at 100)
      sendBlock(i)                        # Header{ver, Type=2} + DataV* + payload
      ctrl = waitReply()                  # select over reply, error, timer
      if timeout                 -> "timed out"
      if ctrl.PacketNumber != blockInAir  -> ignore it, keep waiting
      if ctrl.Ack == 0xFFFF:              # it was a NACK
            log the reason from the Info table
            resend++
            if resend >= 3:  -> "resend limit reached", abort
            i--                           # retry the SAME block
      else:
            resend = 0
```

[D] Constants, read as package variables that are never written at runtime:

| constant | value |
|---|---|
| resend limit | **3** per block |
| per-block reply timeout | **15 s** |
| read buffer | 4096 bytes |
| reply channel depth | 8 |

**An interrupted update resumes.** The server persists the last acknowledged block, keyed
by UUID, and seeds the loop from it on a retry. [D]

### Where the blocks come from

The server picks the block source by protocol version: [D]

- **`0xF0000000`**: the server packs the image itself — chunk the raw image into
  **512-byte** pieces, LZ4-compress each, CRC-32 each, and emit headers.
- **`0xF0000001` / `0xF0000002`**: the server parses an **already-formatted** file —
  repeatedly read a `Header`, then a `DataV2`, then `PayloadLength` bytes, until EOF. A
  short read mid-record is an unexpected-EOF error. This is exactly the on-disk container
  format.

So for the modern protocol versions **the server is a relay, not a packer.** It does not
construct or validate firmware blocks; it replays a file.

### There is no erase, activate or rollback packet

**The bootloader owns the flash.** The server only streams numbered, CRC'd, LZ4'd blocks;
the bootloader decides where they go and when to jump to the new image. [D]

What exists instead:

- **Failure counting on the server side.** A per-device counter, with
  `"firmware update blocked, too many failed retries"` and an `until` timestamp after
  which it stops trying.
- **"Rollback" is just asking for an older CRC.** Because the handle is the image CRC,
  setting the requested firmware to an older image's CRC is the whole mechanism — the
  update check honours an explicitly requested firmware over the newest one.
- The only erase primitive in the protocol belongs to the **file** API, not here. See
  [packets.md](packets.md#the-file-protocol-type-10).

## The bootloader path is different again

Gates, in order: a status packet must be present; the device must be known; it must not be
update-blocked; an explicit request or an auto-flash option must be set; a newer
bootloader must exist; a staged-rollout check must pass; and **battery must be `>= 20`**,
else `"battery level is too low to update bootloader"`. [D]

If the device reports no bootloader at all and a global option is set, the server picks the
bootloader matching the *running app firmware* rather than the newest one.

**The transfer is not the state machine above.** The server walks the `.bootloader` blob as
a sequence of **pre-framed legacy 20-byte-header packets** and replays each one verbatim
over the ordinary device connection: read a 20-byte `ProtocolHeader`, take `Length`, send
header + payload as a request, advance the cursor by `Length + 20`, retry up to 3 times per
block. [D]

Errors on this path include the typo `"seek boodloader data: %w"`, which is a useful
fingerprint if you are matching log output.

## WiFi-module firmware

A third path, file-based. [D]

Requirements, in order: a status packet; the device's feature bitmask present; the device
advertising WiFi-update support (`"WiFi update is not supported by device"`); and
`ConnectivityVersion`, `HardwareNameID`, hardware version, display type and both firmware
versions all present. Then a minimum-host-revision gate.

The server additionally requires host revision `>= 4249`, that no firmware or bootloader
update is already pending (`"device needs to update firmware or bootloader before wifi"`),
and that an auto-flash option or an explicit request is set.

The transfer: battery `>= 20`; show an upgrade screen via a parameter write; stop the JS
engine; fetch the blob; free-space check
(`"not enough space to write wifi update file"`); **write the blob to the device filesystem
as `wifi_update.bin`** through the type-10 file protocol; then send a parameter to
`"start the actual wifi firmware update"`.

The vendor's WiFi catalogue has exactly **two** entries, both **Redpine/Silicon Labs
RS9116W** images: [W]

```json
{"ID":"1_9.0.1278270476.wifi", "Checksum":2450780620,
 "Version":{"Major":9,"Revision":1278270476}, "Type":"wifi",
 "Description":"RS9116W.2.5.2.0.4",
 "Meta":{"compression":"gzip","originalFilename":"RS9116W.2.5.2.0.4.rps"},
 "MinHostRevision":4249, "WiFiFileHash":2797804332}
{"ID":"1_9.1.1282428435.wifi", "Description":"RS9116W.2.10.5.0.1",
 "Meta":{"compression":"gzip","originalFilename":"RS9116W.2.10.5.0.1.rps"},
 "MinHostRevision":4249, "WiFiFileHash":3173907269}
```

So a device with a **TI CC3100** radio — like the reference sign, which nevertheless
advertises the WiFi-OTA feature bit — has **nothing applicable on the service**. [W] This
is also why the WiFi blob carries a `Meta.compression` field: it is the only image that is
container-compressed by the service rather than block-compressed by the protocol. (The
vendor's `"gzip"` label does not match what is actually served; treat the `Meta` values as
unreliable.)

## Staged rollout

The vendor server supports a percentage rollout per version. [D]

```go
type RolloutInfo struct {
    Version    Version  // cbor 0
    Percentage int32    // cbor 1
}
```

- A version **absent** from the rollout map defaults to **100%** — so an image with no
  rollout entry is fully released.
- `>= 100` allows immediately.
- Otherwise the server counts the auto-update-eligible population from its database and
  uses two Redis keys — a *set* and a *counter* — to admit devices. A device already in the
  set stays in the rollout (sticky); otherwise it is admitted while the counter is below
  the configured percentage of the population. **[I]** the exact command sequence and the
  admission arithmetic; the key construction and the set/counter pair are certain.
- Failing admission yields `"skipping automatic update, not part of rollout"`.

**No rollout array was present in any of the vendor's three live lists**, so in practice
every image is at 100%. [W]

## The vendor firmware service

The service is live, unauthenticated, plain HTTP, and the exact image a reference sign runs
was pulled from it. These are properties of the product, so they are documented here.

The shipped configuration template names:

```json
"FirmwareRepository": [ "http://firmware.visionect.com:8089/" ]
```

The client appends `/api/v1/` to the configured base. [D] Paths: [D]

| call | type | path |
|---|---|---|
| list | firmware | `device?type=firmware` |
| list | bootloader | `device?type=bootloader` |
| list | wifi | `wifi` |
| blob | firmware or bootloader | `device/<FirmwareID>` |
| blob | wifi | `wifi/<FirmwareID>` |

The list parser accepts **either** `application/json` **or** CBOR, dispatching on the
response `Content-Type`, and stores the `ETag` header as a cache id for conditional
refetch. **No authentication, signing, API key or client certificate appears anywhere in
the client.** [D]

Exercised live (read-only GETs only; nothing uploaded, no auth probed): [W]

| endpoint | result |
|---|---|
| `/api/v1/device?type=firmware` | 200, JSON, 4.47 MB, **18 417** entries |
| `/api/v1/device?type=bootloader` | 200, JSON, 219 KB, **943** entries |
| `/api/v1/wifi` | 200, JSON, 573 B, **2** entries |
| `/api/v1/device/<id>` | 200, `application/octet-stream`, the image |

### The ID is a structured filename

```
<fmt>_<checksum:%08x>_<Major>.<Minor>.<Revision>_<HwNameID>_<HwMaj>.<HwMin>.<HwRev>_<DisplayID>_<Subtype>.<type>

1_652be4ad_7.4.4407_8_1.1.0_3255107880_universal.firmware
1_b6de05a3_7.4.4407_8_1.1.0_0_universal.bootloader
1_9.0.1278270476.wifi                                   # wifi ids are just 1_<Maj>.<Min>.<Rev>.wifi
```

`<fmt>` is `1` for every entry in all three lists; `DisplayID` is `0` for bootloaders. [W]

**The ID encodes the whole lookup key**, and every component of it is a field the device
reports in its status or Start packet: [D]

```go
type fwListKey struct {          // app firmware
    HwNameID  uint32
    Hardware  Version
    DisplayID DisplayType        // the opaque panel id
    Subtype   Subtype
}
type blListKey struct {          // bootloader — note: no DisplayID, no Subtype
    HwNameID uint32
    Hardware Version
}
// wifi: keyed by HwNameID, indexed by version
```

### Types and subtypes — the complete lists

| `Type` | name | what it is |
|---:|---|---|
| 0 | `none` | sentinel |
| 1 | `firmware` | host MCU **application** firmware |
| 2 | `bootloader` | host MCU **bootloader** |
| 3 | `wifi` | connectivity-module firmware |

| `Subtype` | name |
|---:|---|
| 0 | `none` |
| 1 | `universal` |
| 2 | `ac` |

That is the whole list. [D] **There is no separate type for touch-controller firmware or
for display waveforms.** The touch controller's version is reported (status tags 30–31)
and never updated by this service.

`Subtype` is per-model power topology — `ac` for mains-powered models, `universal` for the
rest — and it is part of the firmware lookup key but **not** of the bootloader key. [D]

### The catalogue metadata schema

```go
type FirmwareInfo struct {
    ID              FirmwareID       `cbor:"0,keyasint,omitempty"`   // the structured filename
    Checksum        uint32           `cbor:"1,keyasint,omitempty"`
    Version         Version          `cbor:"2,keyasint,omitempty"`
    Hardware        *Version         `cbor:"3,keyasint,omitempty"`
    HwNameID        *uint32          `cbor:"4,keyasint,omitempty"`
    DisplayID       *uint32          `cbor:"5,keyasint,omitempty"`
    Type            Type             `cbor:"6,keyasint,omitempty"`
    Subtype         Subtype          `cbor:"7,keyasint,omitempty"`
    Description     string           `cbor:"8,keyasint,omitempty"`
    Meta            map[string]any   `cbor:"255,keyasint,omitempty"`
    MinHostRevision *uint32          `cbor:"3000,keyasint,omitempty"`
    WiFiFileHash    *uint32          `cbor:"3001,keyasint,omitempty"`
}
type FirmwareList struct {
    Version   int            `cbor:"0,keyasint,omitempty"`
    Firmwares []FirmwareInfo `cbor:"1,keyasint,omitempty"`
    Rollout   []RolloutInfo  `cbor:"2,keyasint,omitempty"`
}
```

### Catalogue population

Useful as a map of what hardware exists: [W]

| `HwNameID` | app-firmware images | | `Subtype` | images |
|---:|---:|---|---|---:|
| 1 | 1 | | `universal` | 14 782 |
| 3 | 1 454 | | `ac` | 3 635 |
| 4 | 2 772 | | | |
| 5 | 5 034 | | | |
| 6 | 4 | | | |
| 7 | 896 | | | |
| **8** | **1 067** | | | |
| 11 | 883 | | | |
| 13 | 3 958 | | | |
| 24 | 466 | | | |
| 42 | 1 882 | | | |

`HwNameID 8` spans `DisplayID ∈ {36, 37, 1611727188, 3255107862, 3255107880, 3255107882,
3255107884, 3255107885, 3255107886, 3271885052, 3859087658, 3859087660, 3942973737}` and
hardware revisions 1.1.0, 2.0.0, 2.0.1, 2.1.0, 2.1.1, 2.1.2, with app versions from
3.39.2365 to 8.3.4510. [W]

For one exact key — `HwNameID 8`, hardware 1.1.0, `DisplayID 3255107880`, subtype
`universal` — there are 100 images, the newest being 7.4.4407, which is what the reference
sign runs. Versions 8.1 through 8.3 exist only for hardware 2.1.1 and 2.1.2. [W] So a
given sign may already be at the ceiling for its hardware revision.

The service floor for both firmware and bootloader lists is version **3.39.2365**; 33
distinct versions exist below the current one. Nothing older is served. [W]

## The containers

### `.firmware` — the transfer stream, serialised

```
repeat until EOF:
    Header   { uint32 Version = 0xF0000002 ; uint32 Type = 2 }           //  8 B
    DataV2   { PacketNumber, Flags, PayloadLength, Uncompressed,
               Payload, Reserved, uint32 Checksum }                      // 16 B
    uint8    payload[PayloadLength]
```

The server's own parser consumes this directly, and an independent parser reproduces it
byte-exactly: [W]

| image | records | `Uncompressed` | `PayloadLength` | last-record `Flags` |
|---|---|---|---|---|
| 7.4.4407 | 203, ending exactly at EOF | 2020 (1520 on the last) | 384–1920 | `0x0d` (others `0x09`) |
| 7.3.4358 | 202, ending exactly at EOF | 2020 (1980 on the last) | 592–1920 | `0x0d` |
| 3.39.2365 | 883, ending exactly at EOF | 512 (20 on the last) | 16–496 | `0x0d` |

So the vendor packs modern images with a **2020-byte** uncompressed chunk and old 3.x
images with **512** — the latter matching the server's own packer. [W]

### `.bootloader` — pre-framed legacy packets

Three records of a 20-byte `ProtocolHeader` plus `Length` bytes, chaining exactly to EOF:
[W]

| offset | Version | Security | Compression | Length | Checksum |
|---|---|---|---|---|---|
| `0x00000` | 3 | 1 | 1 | `0x50` (80) | `0x5971bcf4` |
| `0x00064` | 3 | 1 | 1 | `0x16730` (91 952) | `0x36d4a713` |
| `0x167a8` | 3 | 1 | 1 | `0x50` (80) | `0x5971bcf4` |

Records 1 and 3 are **identical** — same length, same CRC — and bracket the payload.
**[I]** a descriptor written before and re-written after the image to commit it.
`Compression = 1` means the payloads are LZ4; `Security = 1` is what marks them encrypted
in this container.

Note that this reuses the **transport** `ProtocolHeader` as a file container format, with
`Security = 1` carrying a meaning it never has on the socket. See
[framing.md](framing.md#security-values-d). The container's checksums follow the device
direction's rule — CRC over the header, not the body — which independently corroborates
that asymmetry. [W]

## The payloads are encrypted — AES-CBC, fixed fleet-wide IV

The server's own packer only LZ4s and CRCs. The **vendor's** images are additionally
**encrypted with a 128-bit block cipher**.

> **This was initially recovered as ECB and that was wrong.** The corrected answer is
> **CBC with a fixed IV**. It is called out because the difference changes what a key
> would buy you, and because the first key search was invalidated by the wrong mode.

### Evidence that it is encrypted at all [W]

1. **Every `PayloadLength` is a multiple of 16** — 203/203, 202/202, 883/883 records, and
   41/41 bootloader sub-blocks. LZ4 output lengths are arbitrary; this is block alignment.
2. The bootloader container makes the padding explicit: its sub-blocks are *uncompressed*
   (`PayloadLength == Uncompressed` for 38 of 41) and yet
   `PayloadLength == ceil(Uncompressed/16)*16` — `0x37 → 0x40`, `0x3b → 0x40`,
   `0x75 → 0x80`. Padding on data that was never compressed can only be cipher padding.
3. Shannon entropy **7.9993 bits/byte** over 273 584 payload bytes, and 7.997–7.999
   across every other image tested.

### Evidence that the mode is chained, not ECB [W]

Three independent tests over three images (17 099 + 17 010 + 15 462 cipher blocks):

1. **Do repeated ciphertext blocks ever appear at different intra-record offsets?** 2, 2
   and 105 duplicate block groups respectively; **zero** of them span more than one
   distinct intra-record offset. Under ECB, with 105 repeat groups in one image, some
   offset-mismatched repeats would be expected.
2. **Is every repeat's preceding in-record prefix identical?** 211 duplicate groups across
   the three images: **identical prefix 211/211, zero counterexamples**. The offsets of
   duplicate groups form a clean decaying staircase from offset 0, i.e. matches are always
   a contiguous run starting at block 0.
3. **Decisive: can a match reappear after a mismatch?** 200 record pairs share their first
   ciphertext block. In **zero** of them does any later block match after the first
   mismatch. Once the chain diverges it never re-converges — the defining behaviour of a
   chained mode, impossible under ECB.

The hard 16-byte padding rules out the unpadded stream modes (CTR, CFB, OFB), so the mode
is **CBC** or an equivalent chained block mode.

### The key and IV are fleet-wide [W]

- 54 of the 64 offset-0 duplicate groups are shared by records with **different**
  `PacketNumber`, so the IV is not derived from the packet number.
- One ciphertext block appears as block 0 of records in **three different images eight
  years apart**, with different packet numbers, lengths, pad values and CRCs.
- The 512-byte all-zero block's ciphertext occurs in images for `HwNameID` **1, 4, 6 and
  8**.

**One key and one IV for the entire product line, 2016 through 2024.**

### Everything is encrypted, including the oldest image [W]

12 images across 6 hardware families, both types, down to the 3.39.2365 service floor:
**all encrypted, all 16-aligned, all at maximum entropy.** There is no plaintext image
anywhere on the service. A plaintext *bootloader* would have handed over the decryptor and
the key; none exists.

### Where the key is not

| route | status |
|---|---|
| the device filesystem | **closed.** Enumerated in full: six framebuffers, 0.6% metadata, no partition selector, MCU flash not addressable through the file API ([packets.md](packets.md#there-is-no-list-opcode)) |
| the vendor server | **closed.** No cipher or key symbol in the firmware package; the packer only LZ4s and CRC-32s; `crypto/aes` is linked for TLS only. The one key-distribution path fetches *session* keys over HTTPS and holds nothing static |
| the vendor firmware service | **closed.** 12 images, 6 families, down to the floor — all encrypted |
| guessing | **closed enough.** A sound oracle, 969 candidates × AES / Camellia / SEED at every legal key length, **0 hits** |
| differential analysis | **exhausted.** Of a 408 876-byte plaintext, only 6.4% is provably identical between adjacent releases, essentially all of it one 26 KB zero region. The image is monolithic with no stable data section |
| the USB serial console | **closed on the known command set.** No memory, flash, peek or dump command exists. `flash_print` dumps stored *settings*, not flash contents |
| **SWD/JTAG or a chip-off flash dump** | **the only remaining route** |

**[I]** but strongly argued: the plaintext almost certainly *is* on the device, in MCU
internal flash. A Cortex-M-class MCU executes code in place over its flash bus and cannot
run CBC-encrypted code without on-the-fly decryption hardware, which parts of this class
and era do not have — so the bootloader must decrypt each block and write **plaintext**.
Supporting but not conclusive: the device reports an `ApplicationVersion` equal to the
vendor's catalogue `Checksum`, and no ciphertext-side CRC candidate matches it, so **[I]**
the catalogue checksum is the CRC-32 of the reassembled plaintext — which means the device
has seen the plaintext. Whether it can still *recompute* it from flash cannot be
distinguished without a dump. [GAP]

There is a verified, IV-free key oracle with a filter strength of about 2⁻¹²⁰ and a cost of
two block decryptions, built on exactly reconstructed known plaintext from a constant-fill
run. It confirms or rejects a candidate in microseconds and immediately yields both the IV
and the padding scheme on a hit. **If a key ever surfaces, the entire catalogue decrypts at
once.** Do not re-run key searches without new information.

## Should a reimplementation do this at all?

**Probably not.** The honest position:

- You cannot build an image, so there is nothing to gain beyond relaying a vendor blob.
- Relaying a blob means handing a device an opaque payload it will write to its own flash,
  with a container you reconstructed, over a path you cannot test without risking the only
  hardware you have. The update is not transactional and there is no rollback packet.
- The reference sign is already at the newest version its hardware revision can take, which
  is a common situation given the catalogue shape.

If you implement anything here, implement the **read** side — parse the containers, read
the catalogue, report to the user which version they are on and whether a newer one exists
for their key. That is useful and risk-free. Leave the write path off by default.
