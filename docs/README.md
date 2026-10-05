# The Visionect device wire protocol

Interoperability documentation for the TCP protocol that Visionect e-ink signs speak
to their server, plus the USB serial console used to provision them.

This is a reference, not a tutorial and not a writeup. It exists so that someone who
owns one of these signs can write a server for it.

## What this is, and where it came from

The findings here were recovered from two sources:

1. **The vendor server's own binaries.** Visionect Software Suite v8.5.5 ships four Go
   binaries (`gateway`, `engine`, `admin`, `networkmanager`) that are **unstripped and
   carry full DWARF plus Go runtime type metadata**, including struct field names, byte
   offsets and struct tags. Field layouts, enum values and name tables in this document
   were read out of that metadata and out of `go tool objdump` disassembly annotated
   with original `file.go:line` positions. Parameter and status name tables are verbatim
   strings from the binaries.
2. **Packet captures and live hardware.** A full plaintext capture of one sign's session
   with its server — connect, heartbeats, a parameter round trip, a file operation and a
   1.84 MB image push — plus later end-to-end validation against the same sign driven by
   a from-scratch reimplementation with no vendor software in the path.

No firmware was decompiled: the device images the vendor serves are encrypted
(see [firmware.md](firmware.md)). Everything about the device side is therefore inferred
from the server's behaviour, from what the device puts on the wire, or from the device's
own serial console.

The reference device throughout is a 31.2" four-panel sign, `HardwareNameID 8`, hardware
revision 1.1.0, firmware 7.4.4407, panel ID `0xC2050128`. Where a claim is specific to
that hardware it says so.

## How claims are marked

Every non-obvious claim carries a confidence marker. Please preserve these if you copy
material out — the distinction between "we watched this happen" and "the disassembly
implies this" is the most useful thing in this document.

| marker | meaning |
|---|---|
| **[W]** | **Verified on the wire or on hardware.** Observed in a capture, or exercised end to end against a real sign. |
| **[D]** | **Read out of the vendor binaries.** Disassembly, DWARF or Go runtime type metadata. Correct as a description of the server; the device's behaviour is a separate question. |
| **[C]** | **From shipped vendor code or documentation.** Admin UI data tables, the published CLI reference, the shipped config template. |
| **[I]** | **Inferred.** A reading of structure or behaviour that was not directly confirmed. Treat as a hypothesis. |
| **[GAP]** | **Unresolved.** Named here so nobody assumes it was checked. |

A claim with no marker is structural bookkeeping (section cross-references, arithmetic
that follows from marked claims).

## Traps

Three findings in this document were initially recovered *wrong* and later corrected.
The wrong answer is plausible in each case, so each is called out where it appears:

- **The frame checksum is not symmetric.** The server and the device compute it over
  different bytes. A symmetric implementation works against the real gateway, which
  never validates the field, and then fails against something that does.
  See [framing.md](framing.md#the-checksum-is-direction-dependent).
- **4 bpp puts the even pixel in the low nibble.** The opposite reading produces an image
  that looks almost right, and "which order looks more like a natural photo" picks the
  wrong one, because the data is dithered.
  See [imaging.md](imaging.md#bit-and-nibble-packing).
- **The block-chain `Stored` flag has the polarity you would not guess**: `1` means *not*
  compressed. See [framing.md](framing.md#the-block-chain-compression--1).

A fourth, outside the protocol: the device's serial console's `help` output is **not** a
complete index of the commands the firmware answers.
See [usb-cli.md](usb-cli.md#help-is-not-a-complete-index).

## Contents

| file | covers |
|---|---|
| [overview.md](overview.md) | What the device is, what a server has to be, and the security model |
| [framing.md](framing.md) | The three nested headers, the block chain, LZ4, and the direction-dependent checksum |
| [packets.md](packets.md) | Packet types, handler/ack semantics, ID conventions, session lifecycle |
| [status.md](status.md) | The status packet: the flat tag/value encoding and an annotated tag table |
| [parameters.md](parameters.md) | TCLV configuration parameters: encoding, the three banks, the read-only set |
| [imaging.md](imaging.md) | Encodings, bit packing, geometry, interlacing, dithering, the state checksum |
| [firmware.md](firmware.md) | Firmware and bootloader transfer, the vendor firmware service, the encryption |
| [usb-cli.md](usb-cli.md) | The USB serial console and the provisioning bootstrap |
| [unknowns.md](unknowns.md) | What is still unresolved, and what would settle it |

## Conventions

- **Every multi-byte integer on this wire is little-endian.** Without exception, in both
  directions, at every nesting level. [W][D]
- Byte offsets in field tables are from the start of the structure being described.
- Sizes are in bytes.
- "Gateway" means the vendor server's device-facing component; it is the thing a
  reimplementation replaces.
- "PV3" is the vendor's internal name for this protocol (wire version 3). The Go package
  that implements it is called `proto/v2`, which is a Go module major version and
  unrelated. The device also reports a status field `ProtocolVersion`, which is `3`.
  See [packets.md](packets.md#version-numbers-three-unrelated-threes).

## A note on what is deliberately absent

This document covers the device protocol. It does not document the vendor server's own
REST API, its internal gRPC services or its web UI, because a reimplementation replaces
all of them. Facts mined from those surfaces appear here only where they describe the
*device* — status field semantics, parameter writability, power-saving behaviour.
