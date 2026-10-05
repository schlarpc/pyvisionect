# Framing

Three nested fixed-width little-endian headers. **It is not CBOR**, despite the
`cbor:"N,keyasint"` struct tags the vendor binaries carry — those describe the server's
*internal* representation, and the integer keys happen to coincide with the on-wire tags
of the status payload. There is a real CBOR packet type (12), used by a different
generation of device; see [packets.md](packets.md).

```
ProtocolHeader   20 B   Version, Security, Compression, Length, Checksum
 ├─ if Compression != 0: a chain of 24-byte block records
 │     BlockIndex, BlocksMinusOne, PayloadLength, UncompressedLength, Stored, Reserved
 │     ... concatenating every block's plaintext yields the bytes below
 └─ DataHeader   36 B   Priority, DeviceID[16], Type, ID, Length, Reserved
       └─ payload, Length bytes, format per Type
```

Every layout in this file has been **validated byte-for-byte against a live capture**. [W]

## The listener and the version mux

The gateway listens on one TCP port, default **11113**, and multiplexes protocol versions
on it. The port is a command-line flag, not a config-file key. [D]

On accept it sets `TCP_NODELAY`, then peeks (does not consume) the first 6 bytes through a
4096-byte buffered reader. If those bytes look like a TLS ClientHello it wraps the
connection in TLS; otherwise it reads 20 bytes and dispatches on
`ProtocolHeader.Version`: [D]

| `Version` | handler |
|---|---|
| `0x00000003` | the normal device protocol — this document |
| `0xF0000001` | bootloader / firmware-update protocol v2 ([firmware.md](firmware.md)) |
| `0xF0000002` | bootloader / firmware-update protocol v3 ([firmware.md](firmware.md)) |

Anything else: connection dropped with `"unsupported protocol version %d"`.

There is **no negotiation.** The device sends `Version = 3` in every frame and the server
either has a handler for that value or hangs up.

## `ProtocolHeader` — 20 bytes

```
 offset size type        field
 ------ ---- ----------- --------------------------------------------------
   0     4   uint32 LE   Version       always 3 for the device protocol
   4     4   uint32 LE   Security      0 = none, 2 = session key, 3 = security packet
   8     4   uint32 LE   Compression   0 = none, 1 = LZ4 block chain
  12     4   uint32 LE   Length        bytes of body that follow
  16     4   uint32 LE   Checksum      CRC-32/IEEE — SEE THE NEXT SECTION
 ------ ----
  20         total
```

```
 0                   1                   2                   3
 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
+---------------------------------------------------------------+
|                       Version  (= 3)                          |
+---------------------------------------------------------------+
|                       Security (0 | 2 | 3)                    |
+---------------------------------------------------------------+
|                       Compression (0 | 1)                     |
+---------------------------------------------------------------+
|                       Length  (<= 0x0320_0000)                |
+---------------------------------------------------------------+
|                       Checksum (direction-dependent!)         |
+---------------------------------------------------------------+
|                       Body: Length bytes                      |
```

### `Security` values [D]

| value | meaning |
|---|---|
| 0 | no encryption. The body is the packet bytes verbatim. |
| 2 | AES-128-CBC under the per-session key |
| 3 | AES-128-CBC "security packet" — a distinct IV, used for the key-activation exchange |
| 1 | **not used on the socket.** No gateway code path produces or specially handles it on TCP. The same 20-byte header is reused as a *file container* for firmware and bootloader images, where `Security = 1` appears and means "encrypted with the firmware key". [I] A `Security == 1` frame arriving over TCP would be fed to the session-key decryptor and fail. |

### `Compression` values [D]

| value | meaning |
|---|---|
| 0 | none — the body is `DataHeader` + payload directly |
| 1 | the body is a chain of 24-byte block records (below) |
| other | rejected with `"unsupported compression %d"` |

### Decoder limits [D]

- Read exactly 20 bytes; a short read is `"read protocol header: %w"`.
- `Version != 3` → `"unsupported protocol version %d"`.
- `Length > 0x3200000` (**52,428,800 = 50 MiB**) → `"packet too large: %d"`. This is the
  maximum frame size.
- The body is read into a pooled buffer of exactly `Length` bytes.
- The vendor gateway does **not** validate the inbound `Checksum` at all. There is no
  `crc32` call anywhere in its decode path.

### Observed directional habits [W]

| | device → server | server → device |
|---|---|---|
| `Security` | 0 | 0 |
| `Compression` | **always 0** | **always 1**, even for an 8-byte ack |

The device never compresses. If you are writing a server, you may choose either; see
[Running without an LZ4 codec](#running-without-an-lz4-codec).

## The checksum is direction-dependent

**This is the single most dangerous detail in the protocol.** The two ends compute the
`Checksum` field over *different bytes*. A symmetric CRC implementation appears to work —
because the real gateway never validates the inbound field — and then fails silently
against anything that does.

| direction | what is CRC'd |
|---|---|
| **server → device** | `crc32_ieee(body)` — the whole body *after* compression, *before* encryption |
| **device → server** | `crc32_ieee(header[0:16])` — the first 16 bytes of the `ProtocolHeader` itself. **It does not cover the payload at all.** |

Evidence for the server rule: the encoder computes `crc32.ChecksumIEEE` over exactly the
slice it then hands to the encryptor, and stores the result. [D] All server frames in the
capture match `crc32(body)` exactly — 26/26 across two sessions. [W]

Evidence for the device rule: **two status packets ten minutes apart, with different
packet IDs and different battery and uptime values, carry the identical checksum
`0x198ceb01`**, because their `(Version, Security, Compression, Length)` prefix is
identical. 26/26 device frames match `crc32(header[0:16])`. [W] The same rule is used
independently in the firmware container format. [W]

Rules for an implementation:

- **As a server**: emit `crc32_ieee(body)`. You may ignore the inbound field entirely — a
  CRC that covers no payload bytes cannot detect corruption anyway, so verifying it buys
  nothing but a compatibility hazard. If you verify it at all, make it opt-in.
- **As a device emulator**: emit `crc32_ieee(header[0:16])` to match real firmware. The
  vendor gateway would also accept `crc32(body)` since it never looks.
- **[I]** Real firmware presumably does not validate the server's checksum either,
  otherwise the two rules could not coexist. Not verifiable from the server binary.
- **[I]** This is almost certainly a firmware bug the gateway tolerates.

Note also that the computation order matters: the CRC covers the **compressed but not yet
encrypted** body, and `Length` is the **final** (compressed and encrypted) body length.
See [Encoder order of operations](#encoder-order-of-operations).

## The block chain (`Compression = 1`)

When `Compression != 0`, the body is a sequence of records, each a 24-byte header followed
by `PayloadLength` bytes.

```
 offset size field                notes
   0     4   BlockIndex           0-based index of this block
   4     4   BlocksMinusOne       total blocks - 1; identical in EVERY header
   8     4   PayloadLength        bytes of block payload that follow
  12     4   UncompressedLength   4800 (0x12C0), except the final short block
  16     4   Stored               *** 1 = payload verbatim, 0 = raw LZ4 block ***
  20     4   Reserved             always 0
  24   ...   payload              PayloadLength bytes
```

- **Plaintext block size is 4800 bytes** (`0x12C0`). [D] With `Security != 0` it drops to
  2400; see below.
- **`Stored` has inverted polarity: `1` means NOT compressed.** The encoder compresses
  the block, compares, and takes the stored branch when LZ4 did not strictly shrink it,
  setting `Stored = 1` and `PayloadLength = UncompressedLength`. [D] Confirmed in the
  capture: a 44-byte ack body that LZ4 could not shrink is sent as
  `PayloadLength=44, UncompressedLength=44, Stored=1`. [W] In the 1.84 MB image push,
  385 blocks split **60 stored / 325 LZ4**. [W]
- **`BlocksMinusOne` is an index, not a count or a boolean.** It is `total - 1`, written
  identically into every block header — 384 in all 385 blocks of the captured push. So a
  receiver knows the total block count from the first header. [W][D]
- `BlockIndex` is 0-based. [W][D]
- Blocks are concatenated in order; block *i*'s plaintext lands at `out[i*4800:]`. [D]
- Decoder sizing: `total = (BlocksMinusOne + 1) * 4800`, except that a single-block chain
  uses `UncompressedLength` instead; `total > 0x3200000` → `"blocks too large: %d"`. [D]
- `Reserved` is never written (the buffer is zeroed). [D]

A decoder is therefore:

```python
out = b""
while more:
    idx, last, plen, ulen, stored, _ = struct.unpack("<6I", body[off:off+24])
    blk = body[off+24 : off+24+plen]
    out += blk if stored == 1 else lz4_decompress_block(blk, ulen)
    off += 24 + plen
```

### The LZ4 flavour

**Raw LZ4 block format, not the frame format.** The vendor calls the legacy
`LZ4_compress` alias of `LZ4_compress_default` at default acceleration. That means: no
magic number, no frame header, no content checksum, no block-size prefix. The
uncompressed length has to come out of band, which is exactly what `UncompressedLength`
is for. [D] Every block in the capture round-trips to exactly `UncompressedLength` bytes
with a plain raw-block decoder. [W]

In Python: `lz4.block`, not `lz4.frame`.

**Byte-exact re-encoding of vendor-compressed frames is not available.** Of 328 captured
gateway blocks, 0 reproduce under `lz4.block.compress` in default or fast mode at any
acceleration; 3 match under high-compression mode, and those are 44-byte ack bodies. [W]
This is harmless for interoperability — the device decodes any valid LZ4 block — but it
means you cannot round-trip a captured server frame bit-for-bit.

LZ4 also buys almost nothing on this payload: mean compression ratio **0.956** over the
image push, because 4 bpp dithered halftone data is near-incompressible. [W]

### Running without an LZ4 codec

You can set `Stored = 1` on every block and keep the ordinary block-chain framing. This
is a path the device demonstrably handles — 60 of the captured push's 385 blocks were
stored anyway. [W] The cost is roughly 4% more bytes.

Sending `Compression = 0` on the **outbound** path is a different thing and is
**unverified**: the gateway was never observed sending an uncompressed frame to a device,
so whether the firmware accepts one is unknown. [GAP] Prefer all-stored blocks.

## `DataHeader` — 36 bytes

The inner frame. Present unless the frame is a raw body (below).

```
 offset size type        field
   0     4   uint32 LE   Priority    (always 0 in observed traffic, both directions)
   4    16   [16]byte    DeviceID    the device UUID, raw bytes, in display order
  20     4   uint32 LE   Type        packet type — see packets.md
  24     4   uint32 LE   ID          request/response correlation id
  28     4   uint32 LE   Length      payload length
  32     4   uint32 LE   Reserved    written as-is; the decoder ignores it
  36   ...   payload
```

`DeviceID` is literally the 16 UUID bytes. [D][W] `Priority` has no `.String()` method
and the gateway never writes it; its value space is a **[GAP]**.

For the `ID` conventions and the echo rule, see
[packets.md](packets.md#ids-and-correlation).

## Raw bodies

The encoder accepts exactly two root packet shapes: [D]

- the normal case, which emits `DataHeader` + payload;
- a **raw** body, which emits the given bytes with **no `DataHeader`** — a bare opaque
  body inside the 20-byte `ProtocolHeader`.

Anything else → `"only Raw and Data packets allowed"`.

The only producer of a raw body in the gateway is the security key-activation packet, so
a plaintext server never emits one. A decoder still has to cope: a frame whose `Security`
is 0 but whose body is not a well-formed `DataHeader` is possible in principle.

## Encoder order of operations

From the gateway's encode path, in order: [D]

1. Reserve 20 bytes at the head of the output buffer; the body starts at `buf[20:]`.
2. Marshal the packet into the body.
3. If compression is on: build the block chain in place.
4. **`Checksum = crc32_ieee(body)`** — i.e. over the compressed, not-yet-encrypted body.
5. If security is on: encrypt the body in place.
6. Fill the header: `Version = 3`, `Security`, `Compression`,
   `Length = len(body after step 5)`, `Checksum` from step 4.
7. Write the 20-byte header and flush.

So: **marshal → compress → CRC32 → encrypt → header.**

## Encrypted bodies (`Security != 0`)

Included for completeness and for the decode side. You cannot *originate* this from a
self-hosted server — see [overview.md](overview.md#1-protocolheadersecurity--aes-128-cbc-inside-the-stream).

The body is split into chunks of at most **2400** plaintext bytes, each emitted with a
32-byte **cleartext** header, giving an output stride of 2432 bytes: [D]

```
per chunk:
  offset size field
    0     4   PaddedLength    uint32 LE   plaintext length after padding, multiple of 16, <= 2400
    4     4   OriginalLength  uint32 LE   plaintext length before padding
    8     4   Checksum        uint32 LE   CRC-32/IEEE over the PADDED plaintext
   12     4   Reserved        uint32 LE   0
   16    16   DeviceIDHash    [16]byte    see overview.md
   32   PaddedLength          AES-128-CBC ciphertext
```

- Padding: `pad = 16 - (len % 16)`; **if `len % 16 == 0` no padding is added at all** (so
  this is *not* PKCS#7, which always pads). The pad bytes are `pad` repeated `pad` times.
  [D]
- The decrypt side requires at least 32 bytes, decrypts `PaddedLength` bytes, verifies
  `crc32_ieee(padded_plaintext) == Checksum`, then truncates to `OriginalLength`. [D]
- `DeviceIDHash` is a **key lookup handle, not an IV**. On receive, a connection with no
  key yet uses those 16 bytes to look up a key previously issued for that device. That is
  how a keyed device survives a gateway restart. [D]
- The IV for `Security = 0/2` traffic and the IV for `Security = 3` packets are two
  different hardcoded 16-byte ASCII literals, identical in every installation. [D]

**[GAP] Inter-chunk CBC chaining.** For a single-chunk body — payload ≤ 2400 bytes, which
is the overwhelming majority — the IV is unambiguously the hardcoded constant. For
multi-chunk bodies the two sides update the IV between chunks and the two register traces
could not be reconciled: the encoder's tail points at the previous chunk's *ciphertext*
tail (standard CBC chaining), while the decoder reads what looks like the previous
chunk's *plaintext* tail. Do not trust either reading without a capture.

## Worked example 1 — status heartbeat, device → server

A real captured frame, with the device UUID replaced by the placeholder
`00112233-4455-6677-8899-aabb00000000`. The `ProtocolHeader.Checksum` is unaffected by the
substitution, because the device rule covers only `header[0:16]`.

```
  0000  03 00 00 00 00 00 00 00 00 00 00 00 14 02 00 00
  0010  01 eb 8c 19 00 00 00 00 00 11 22 33 44 55 66 77
  0020  88 99 aa bb 00 00 00 00 03 00 00 00 1b 05 00 00
  0030  f0 01 00 00 00 00 00 00 | 00 00 00 00 03 00 00 00
  0040  01 00 00 00 00 00 00 00 02 00 00 00 ff ff ff ff
  ...
  0220  66 00 00 00 cc 0a 55 c5 ff ff ff ff df 50 90 77
```

```
 0000  03000000   ProtocolHeader.Version      = 3
 0004  00000000   ProtocolHeader.Security     = 0   (plaintext)
 0008  00000000   ProtocolHeader.Compression  = 0   (raw body, no block chain)
 000c  00000214   ProtocolHeader.Length       = 532  (= 36 + 496)
 0010  198ceb01   ProtocolHeader.Checksum     = crc32(bytes 0x00..0x0f)  <-- header-only
 0014  00000000   DataHeader.Priority         = 0
 0018  ..16 B..   DataHeader.DeviceID         = the UUID, raw
 0028  00000003   DataHeader.Type             = 3 (status)
 002c  0000051b   DataHeader.ID               = 1307
 0030  000001f0   DataHeader.Length           = 496
 0034  00000000   DataHeader.Reserved
 0038  payload: 62 x (uint32 tag, uint32 value), ending with tag 0xFFFFFFFF
```

Decoded in full in [status.md](status.md#a-real-status-payload-decoded).

## Worked example 2 — control ack, server → device

The reply to the frame above, showing the block-chain layer. Checksum recomputed for the
placeholder UUID.

```
  0000  03 00 00 00 00 00 00 00 01 00 00 00 44 00 00 00
  0010  ef 78 c5 28 | 00 00 00 00 00 00 00 00 2c 00 00 00
  0020  2c 00 00 00 01 00 00 00 00 00 00 00 | 00 00 00 00
  0030  00 11 22 33 44 55 66 77 88 99 aa bb 00 00 00 00
  0040  01 00 00 00 1b 05 00 00 08 00 00 00 00 00 00 00
  0050  01 00 00 00 00 00 00 00
```

```
 0000  Version=3  Security=0  Compression=1  Length=68  Checksum=0x28c578ef = crc32(body)
 0014  block: BlockIndex=0 BlocksMinusOne=0 PayloadLength=44
               UncompressedLength=44 Stored=1 Reserved=0
               Stored=1 -> the 44 bytes follow verbatim
 002c  DataHeader: Priority=0 DeviceID=<uuid> Type=1 (control) ID=1307 Length=8 Reserved=0
 0050  Control: Flags=0x00000001 (ack)  Length=0
```

Note the shape: a 20-byte frame header plus a 24-byte block header to carry an 8-byte
payload. The gateway compresses everything, including this.

## Worked example 3 — the 1.84 MB image push

Structure only; the payload is covered in [imaging.md](imaging.md). [W]

```
ProtocolHeader  Version=3  Security=0  Compression=1  Length=1783895
                Checksum = crc32(body)
block chain     385 blocks. BlockIndex 0..384; BlocksMinusOne = 384 in ALL of them.
                UncompressedLength = 4800 each, with a 104-byte final remainder.
                325 LZ4 (Stored=0) + 60 stored (Stored=1).
                sum(UncompressedLength) = 1 843 304 = 36 (DataHeader) + 1 843 268 (payload)
DataHeader      Type=5 (image)  ID=3544696833  Length=1843268
payload         ImageHeader + 2 x (RectangleHeader + 921 600 bytes)
```

## Quick reference: encode a frame from scratch

```python
body = marshal(packet)                     # DataHeader + payload, or a raw body

if compression:                            # the gateway always does; the device never does
    out = b""
    blocks = chunks_of(body, 4800)
    for i, blk in enumerate(blocks):
        lz = lz4_compress_block(blk)
        if len(lz) < len(blk):  pl, stored = lz,  0
        else:                   pl, stored = blk, 1
        out += struct.pack("<6I", i, len(blocks) - 1, len(pl), len(blk), stored, 0) + pl
    body = out

# ---- CHECKSUM IS DIRECTION-DEPENDENT ----
#   as a SERVER: crc = crc32_ieee(body)
#   as a DEVICE: crc = crc32_ieee(frame_header[0:16])  (computable once Length is known)
crc = crc32_ieee(body)

if security:                                # not reachable on a self-hosted server
    out = b""
    for chunk in chunks_of(body, 2400):
        pad = 16 - (len(chunk) % 16)
        padded = chunk + (bytes([pad]) * pad if pad != 16 else b"")
        out += struct.pack("<4I", len(padded), len(chunk), crc32_ieee(padded), 0)
        out += device_id_hash                # 16 bytes; see overview.md
        out += aes128_cbc_encrypt(session_key, FIXED_IV, padded)
    body = out

frame = struct.pack("<5I", 3, security, compression, len(body), crc) + body
```

Decoding is the same in reverse, with the outbound `Checksum` left unverified.
