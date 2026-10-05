# Packet types, acks, and the session lifecycle

The `Type` field of the `DataHeader` ([framing.md](framing.md#dataheader--36-bytes))
selects the payload format.

## The type enum

Recovered two independent ways that agree exactly: from the `Type.String()` jump table,
and from a 13-entry array of payload constructors indexed directly by the type value.
`Type >= 13` is rejected. [D]

| value | name | payload | who sends it |
|---:|---|---|---|
| 0 | *(invalid)* | — | — |
| 1 | `control` | `Control` | **both** — the ack/nack |
| 2 | `command` | `CommandHeader` + payload | server → device |
| 3 | `status` | TLV stream | device → server |
| 4 | *(hole)* | — | — |
| 5 | `image` | `ImageHeader` + rectangles | server → device |
| 6 | `touch` | `Touch` | device → server |
| 7 | `GPS` | `GPS` | device → server |
| 8 | `param` | `ParamHeader` + TCLVs | **both** |
| 9 | *(hole)* | — | — |
| 10 | `file` | `FileHeader` + one of four shapes | **both** |
| 11 | `button` | `Button` | device → server |
| 12 | `CBOR` | `CBORHeader` + CBOR | the newer "AC"/JS-app device generation |

Unknown values format as `unknown(%d)`.

Types 4 and 9 are holes in both of the v2 tables. The legacy v1 enum in the same binary
names **4 = `input`** and **`0xFFFFFFFF` = `bootloader`**, so both were genuine protocol
types historically. [D] Neither is reachable in this server build: the constructor table
rejects `Type >= 13`, slot 4 is nil, and the bootloader protocol was moved out to its own
mux versions (see [firmware.md](firmware.md)).

### What was actually seen on the wire

The captured traffic covers types **1, 3, 5, 8 and 10** and nothing else. [W] In
particular:

- **Type 12 (CBOR) was never emitted.** Firmware 7.4.4407 does not use it. That channel
  belongs to the JS-app device generation. Implement the framing if you like; you will
  not need the body.
- **Type 2 (command) has never been seen in either direction, in any capture.** See
  [the warning below](#packet-type-2-command-is-unverified).

## Handler dispatch and who sends the ack

The gateway holds a `map[Type]Handler` with exactly seven entries: types 3, 6, 7, 8, 10,
11 and 12. [D] Types 1, 2 and 5 are deliberately absent — 1 is handled inline, and 2 and
5 are server-to-device only.

Dispatch, in order: [D]

```
t = packet.Type
if t == 3:  reset the inactivity watchdog timer
if t == 1:  return handlePacketControl(packet)
handler, ok = handlers[t]
if not ok:
    WriteControl(packet.ID, flags=0)        # <-- NACK for an unknown packet type
    log(...)
    return error
return handler.Handle(...)
```

**The ack for a *handled* packet is sent by the handler itself**, not by the dispatcher.
For a status packet the handler loads the `DataHeader.ID` and calls
`conn.WriteControl(id, 1)` — flags = 1 = ack. The dispatcher's `WriteControl(id, 0)` is
on the unknown-type branch only, and is the NACK. [D] Confirmed in the capture: the
gateway's reply to each status is `DataHeader{Type=1, ID=<echo>, Length=8}` carrying
`Control{Flags=1, Length=0}`. [W]

## Control packets (type 1)

```
 offset size field
   0     4   Flags   uint32 LE
   4     4   Length  uint32 LE   = len(Payload)
   8   ...   Payload
```

Flag bits actually tested in the gateway: [D]

| bit | mask | meaning |
|---:|---|---|
| 0 | `0x00000001` | **ACK**. `Ack()` is literally `flags & 1`; `String()` renders `"ack"` / `"nack"` on bit 0. |
| 25 | `0x02000000` | **NACK, reason = the device is charging** |

The inbound control handler: [D]

1. The control packet's `DataHeader.ID` must equal the gateway's outstanding request ID,
   else it is logged and ignored.
2. Bit 25 set → "nack, charging".
3. `Flags == 0` → plain nack.
4. Otherwise success.

A device NACK can carry a payload. Observed: [W]

```
device → server, Type=1, Length=12:
  00 00 00 00   Flags  = 0 (nack)
  04 00 00 00   Length = 4
  00 00 8a 00   error code 0x00008a00
```

`0x00008a00` is what this hardware returns for a file-open it refuses. **[GAP]** The
device-side error code space is not documented anywhere in the server.

### `ID = 0xFFFFFFFF` means "do not ack"

The gateway's `WriteControl` is a no-op when the ID is `0xFFFFFFFF`. [D] Treat it as a
sentinel in both directions.

## IDs and correlation

Two independent ID spaces share one connection. [W][D]

- **The originator picks the ID; the responder echoes it verbatim.** Proven in the
  capture: status `ID=1307` → control `ID=1307`. This is the whole of request/response
  correlation. Status heartbeats are acked by ID too.
- **Device-originated IDs are a plain +1 counter that resets to 1 on every new TCP
  connection.** Observed: 1302…1314 on one session, then 1…8 after a reconnect. [W]
- **Server-originated IDs are a random uint32.** The gateway takes the high 32 bits of a
  `math/rand/v2` draw. Observed: `0x3FA3A4F1`, `0x691DED39`, `0xD35B7CC1`. [W][D]

The counter reset is one of three things that change together on a reconnect (source
port, DHCP lease, packet counter). See
[overview.md](overview.md#reconnects-are-a-full-ip-stack-re-initialisation). **Do not key
session state on the ID.**

## Session lifecycle

The gateway's connect path, after the mux hands over the (optionally TLS-wrapped)
connection: [D]

| step | action |
|---|---|
| 1 | Build the connection with a read timeout of `PacketTimeout` seconds (config default **300**) |
| 2 | Read the **first packet** |
| 3 | The first packet **must be a status packet** — else `"first packet is not a status packet"` |
| 4 | Mark the device online |
| 5 | Store the status, update the renderer |
| 6 | Push configuration parameters; arm the keepalive |
| 7 | Process that same first status packet normally through handler 3 |
| 8 | Attempt security activation (a no-op unless the per-device `Secure` option is `"true"`) |
| 9 | Record connect events |
| 10 | Check for firmware, bootloader and WiFi-module updates |

There is **no separate login or auth exchange.** The device identifies itself purely by
the `DeviceID` field of the first status packet's `DataHeader`, and the gateway
auto-creates an entry for an unknown UUID.

Captured reconnect, showing exactly how little happens: [W]

```
ARP   who-has <default gateway>
ARP   who-has <server>              (by address — no DNS query is made)
TCP   SYN  → :11113
TCP   SYN,ACK
TCP   ACK
PV3   device → server  status, id=1, 552 B      <-- 0.42 s after the ACK
PV3   server → device  control{ack}, id=1
PV3   device → server  status, id=2
PV3   server → device  control{ack}, id=2
```

The first status after a connect differs from steady-state heartbeats in exactly one
field: `ConnectReason` (tag 0) is `5` ("connection error") on the first packet and `3`
("heartbeat") on all later ones. [W] [I] a first-packet / reconnect-cause marker; the
vendor API surfaces `"heartbeat"` either way.

## The two timers

### Device-driven heartbeat

The device reports on its own schedule with `ConnectReason = 3` (`heartbeat`). The
interval is a device-side setting: TCLV parameter **29**, `"Heart beat interval"`, also
settable with command type 7. The device announces in each status how long until it will
next report, in tag **27** `NextStatus`, **in minutes**. [D][W]

Measured on the reference device, awake and connected: a status every **60.0 s ± 0.1 s**,
acked by the server within ~1 ms. [W] `NextStatus` read `1` on that device, and parameter
29 read `1` — consistent with minutes.

### Server-side inactivity watchdog

Independent of the above. The gateway arms a timer that is reset on every received
**status** packet. [D]

- **Not armed for old firmware**: if the device's reported firmware *revision* is
  `<= 269`, no watchdog is created at all.
- Timeout = `DeviceStatePolling` minutes + a uniform random 0–10 s of jitter, falling
  back to **900 s** (15 minutes) when the config value is 0. The config default is 15.
- On expiry the gateway sends **command type 11, "status request"**. If that request
  fails, it closes the connection.

Other hardcoded timeouts in the same code: a 300 s encode timeout for the
security-handshake packets, and a 10 s wait for the device's security ack. [D]

## Other payload layouts

All little-endian. These are the vendor's Go struct layouts; see the caveat about struct
versus wire disagreement [below](#packet-type-2-command-is-unverified).

| type | layout |
|---|---|
| 2 `command` | `Type uint32 @0`, `PayloadLength uint32 @4`, `Reserved uint32 @8`, then payload. Size 12 + payload. [D] |
| 3 `status` | a stream of 8-byte TLV records — [status.md](status.md) |
| 5 `image` | `ImageHeader` (20 B) + `NrPrimitives` × (`RectangleHeader` (24 B) + payload) — [imaging.md](imaging.md) |
| 6 `touch` | five uint32: `DisplayCRC`, `DisplayID`, `FingerNum`, `X`, `Y` (20 B) [D] |
| 7 `GPS` | `PowerState uint32 @0` then a string `Coordinates`. **[GAP]** the string framing. [D] |
| 8 `param` | `Reserved uint32 @0`, `PayloadLength uint32 @4`, then TCLVs — [parameters.md](parameters.md) |
| 10 `file` | `Op uint32 @0`, `Length uint32 @4`, then a payload selected by `Op` — [below](#the-file-protocol-type-10) |
| 11 `button` | two uint32: `Active`, `State` (8 B) [D] |
| 12 `CBOR` | `UserData uint32 @0`, `Reserved uint32 @4`, `Length uint32 @8`, then `Length` bytes of CBOR. [D] |

## Packet type 2 (command) is unverified

**Type 2 has never been observed on the wire, in either direction, in any capture.** [W]
Everything below is read out of the Go struct and the enum's `String()` method. [D]

That matters because **there is direct precedent in this protocol for the struct and the
wire format disagreeing**: the Go command struct is 12 bytes, and the param packet's
on-wire header is **8**, because `Reserved` is simply not emitted. Counted exactly in the
capture: a param payload of 32 bytes with 24 bytes of TLVs leaves 8. [W]

The command type values, from the enum's compare chain. It is an **int32** — the negative
values are real: [D]

| value | string | | value | string |
|---:|---|---|---:|---|
| 0 | `reboot` | | 13 | `battery` |
| 1 | `echo` | | 14 | `touch update` |
| 2 | `configuration` | | 16 | `set gps` |
| 3 | `AT command` | | 17 | `front light` |
| 4 | `sleep` | | 18 | `sync display` |
| 5 | `logs` | | 22 | `front light extension` |
| 6 | `set Vcom` | | -1 | `refresh` |
| 7 | `set heartbeat` | | -2 | `clear screen` |
| 8 | `set network retries` | | -3 | `keyboard` |
| 9 | `LED window` | | -4 | `beep` |
| 10 | `LDRLED` | | 15 | *(no case: `unknown`)* |
| 11 | `status request` | | | |
| 12 | `system` | | | |

> **The two versions of this enum disagree on values 0 and 1.** The table above is the
> **v2** `packet.CommandType`, which is what this protocol version uses. The legacy **v1**
> `proto.CommandID` enum — also linked into the same binary — has them the other way
> round: `0 = echo`, `1 = reboot`. [D] Both were read independently out of their
> respective `String()` methods. Nothing on the wire disambiguates them, and neither value
> has been observed. **Given that one of the two is `reboot`, do not guess.** This is a
> good reason to prefer the verified routes below.

How well attested each one is: [D]

- **11 `status request`** is the best attested — it is what the inactivity watchdog
  emits.
- **4 `sleep`** has a traced payload: one little-endian uint32 of **minutes**, which must
  be non-zero (0 is rejected server-side as `errSleepTime`). The server-side helper that
  builds it writes the command value as an immediate `$0x4`, so this one id is
  unambiguous despite the enum conflict above. Note that **nothing can wake a sleeping
  device early** — there is no server-to-device push while it is asleep.
- **7 `set heartbeat`** likewise takes a uint32 of minutes, rejecting bad values with
  `"invalid heartbeat interval"`. Prefer TCLV parameter 29, which is network-writable.
- **9 `LED window`** corresponds to a server-side option formatted as `"<a>:<b>"`; it is
  for front-light-equipped signs.
- **18 `sync display`** is the synchronised flip for a multi-device display group.
- **`-1 refresh` and `-2 clear screen` have no emission site anywhere in the vendor
  server.** They are enum entries and nothing else.
- The rest were not traced to a call site.

**Recommendation.** Prefer a verified route wherever one exists: an image push instead of
`refresh` or `clear screen`, a TCLV parameter write instead of `set heartbeat`. A reboot
is at least observable after the fact — the device reconnects with `ConnectReason = 1`
(`reboot`).

## The file protocol (type 10)

A general-purpose device filesystem API. It carries WiFi-module firmware blobs, syslog
dumps and JS-app assets, and can read the device's cached framebuffers.

```
FileHeader:  Op uint32 @0, Length uint32 @4
```

`Op` values: [D]

| value | name |
|---|---|
| 0 | `open` |
| 1 | `close` |
| 2 | `read` |
| 3 | `write` |
| 4 | `erase` |
| 5 | `write descriptor` |
| 6 | `event` |

Payload shapes, selected by `Op`: [D]

| shape | layout |
|---|---|
| open | `Mode uint32` then a **NUL-terminated** filename |
| bytes | raw bytes (read/write data) |
| erase | a filename |
| write done | `Length uint32`, `Checksum uint32` |

`Mode`: `0 = none`, `1 = write`, `2 = read`. [D]

`erase` is a **filesystem** erase by filename, not a flash-sector erase. There is no
erase step in the firmware-update path.

### There is no `list` opcode

A directory listing is a plain **read of `"."`**. A path of `"/"` or `""` is rewritten to
the one-byte string `"."`, and reading it returns an ASCII blob of `name size checksum`
rows. [W]

On the reference device the entire filesystem is six cached framebuffers: [W]

```
/image0.pv2  134409      /image3.pv2   781996
/image1.pv2  199047      /image4.pv2   453496
/image2.pv2 1213841      /image5.pv2  1007217
```

The accounting closes exactly: the file sizes sum to 3 790 006, against
`FsTotalSize - FsFreeSize = 3 813 376`, a 0.6% difference that is filesystem metadata.
**There is no room for anything hidden** — no bootloader, no key material, no
configuration file, no certificates. [W]

The listing's `checksum` column reads `"0"` for every file on this firmware, so it
identifies nothing. To tell which cached frame is which, read the first ~256 bytes of
each and match the stored `ImageHeader.Checksum` against a value you pushed yourself.
**[GAP]** Six buffers for two panel channels implies current/previous/working, but the
semantics are unknown.

Path probes, all read-only: [W]

| path | result |
|---|---|
| `/` or `""` | the six-file listing |
| `/sys` | `read file: open file: not acknowledged` — **the device itself NACKs the open** |
| `/flash`, `/..` | the device was asleep; not retried |

So the path is genuinely passed to the device and the device's own `open()` rejects
anything outside that flat namespace. MCU internal flash is **not** addressable through
this API.

### Correction: `@screen_N` is not about incremental updates

An earlier reading of the capture concluded that the gateway opens a device-side file
`@screen_<N>` "before deciding how to update the screen", making the NACK the mechanism
behind incremental updates. **That was wrong**, and it invented a constraint that does
not exist.

The only reference to `"@screen_2"` in the whole gateway binary is a hardcoded literal —
not even a `@screen_%d` format string — inside the handler for a **live-view** request.
[D] The admin UI only offers live view for a specific set of `HardwareNameID` values, not
including the reference device's. The file protocol is not entangled with image pushes at
all.

Partial updates genuinely never land on this hardware, but for an unrelated reason; see
[imaging.md](imaging.md#region-and-delta-updates-exist-but-are-unreachable-here).

## Version numbers: three unrelated threes

| number | what it is |
|---|---|
| `ProtocolHeader.Version = 3` | the **wire** protocol version. Hardcoded by the encoder, required by the decoder, and the mux key. |
| the Go package `proto/v2` | the **Go module major version** of the library that implements wire version 3. Go module semantics, nothing more. |
| status tag 40 `ProtocolVersion` | what the **device reports it speaks**. The reference device reports `3`, matching the header. |

The gateway's own naming is consistent with "wire v3" throughout: `pv3Network`,
`pv3Connection`, `PV3StatusHandler`, source file `pv3.go`. [D]
