# Overview: the device is a client, and you are the server

## The shape of the thing

A Visionect sign is a **TCP client**. It dials out to one address and port, speaks first,
and multiplexes everything over that one connection for as long as it stays up.

```
sign  ──────────── TCP ───────────▶  server
      status (it speaks first)
      ◀── control{ack}
      ◀── param read / write
      ──▶ param reply
      ◀── image push
      ──▶ control{ack}
      ──▶ status every heartbeat interval
```

Consequences, each of which has been checked:

- **The sign listens on nothing.** A full 65535-port SYN scan of a live sign found zero
  open TCP ports: 65253 answered with RST (a live, correct TCP stack) and 282 gave no
  answer, consistent with the WiFi module dropping probes while the radio is in power
  save. A UDP scan with service probes on the top 200 ports plus mDNS, SSDP, NetBIOS,
  SNMP, CoAP and TFTP got silence on all of them. It answers ICMP echo and nothing
  else. [W]
- **There is no server-initiated connection anywhere in the protocol.** Parameter reads,
  file operations and image pushes all travel down the connection the device opened. [W][D]
- **A reimplementation is therefore a listener**, not a client. It accepts whatever
  connects and drives it.

## There is no discovery, in either direction

This is worth stating flatly because people reasonably assume otherwise.

- The sign advertises nothing. A 15-minute passive LAN capture, enumerated exhaustively
  rather than sampled, found the device's *complete* non-TCP output to be: ARP replies,
  two ARP requests each for the default gateway and the server, two 802.2 LLC XID link
  probes, DHCP Discover and Request, and ICMP echo replies. No mDNS, no SSDP, no LLMNR,
  no NetBIOS, no WS-Discovery. [W]
- It does not even make a DNS query when its configured server address is a hostname —
  it goes from DHCP straight to an ARP for the server's *address*. The name is resolved
  and cached somewhere persistent. [W] [I] that resolution happens on cold boot, or that
  the resolved address is stored in flash alongside the name.
- The vendor server contains no discovery code at all. `go tool nm` over `gateway`,
  `admin` and `networkmanager` returns no symbol from any mDNS, zeroconf, Bonjour,
  DNS-SD, SSDP or UPnP package, and the admin UI assets contain none either. [D]
- A targeted `avahi-browse` for `_vss._tcp` and `_visionect._tcp` returns empty. [W]

So the **only** binding between a sign and a server is the address and port written into
the device's flash, plus the UUID the device presents in every packet. Adding
discoverability on the server side is pointless, because the firmware has no client for
it.

### Reconnects are a full IP-stack re-initialisation

A reconnect is not a socket retry. Observed sequence, twice in a 22-minute window: [W]

```
802.2 XID link probe
DHCP Discover   (from 0.0.0.0, broadcast — a full DISCOVER, not a unicast renew)
DHCP Request
ARP who-has <default gateway>
ARP who-has <server>
TCP SYN → 11113
status packet, with the device's packet-ID counter reset to 1
```

So the source port, the DHCP lease and the packet counter all change together. **Never
key session state on the source port or the packet ID.** Only the UUID is stable. One of
those observed connections opened and closed without carrying a single protocol byte, so
a silent connection must not evict existing state either. [W]

## The UUID is the only credential

There is no login, no authentication exchange, no nonce, no version negotiation and no
capability handshake. The device opens TCP and immediately sends a status packet; the
16-byte UUID in that packet's `DataHeader` is the entire identity claim. [W][D]

The vendor gateway's only pre-protocol step is to peek 6 bytes to see whether the
connection is a TLS ClientHello. [D]

What this means if you are implementing a server:

- **Anything that can reach your port and knows a UUID is that device.** Enrollment in
  the vendor server is auto-create on first connect. [D]
- **The UUID is not secret and not random.** It is factory-programmed, readable over the
  serial console with `uuid_get`, printed by the vendor server's own UI, and structured:
  on the reference device, bytes 6..11 are the ASCII digits of the unit's serial number.
  [W] Do not treat it as a bearer token.
- **Put the listener somewhere only the sign can reach it**, or add TLS (below). The
  protocol gives you nothing else.

### The derived "device ID hash"

The vendor code derives a 16-byte value from the UUID that appears in the encrypted-chunk
header (see [framing.md](framing.md#encrypted-bodies-security--0)) and as a key-lookup
handle in the server's session-key table. Despite the name, it is a **reversible AES-128
transform, not a digest**: [D]

```
key  = "N3ls0#!Dba0f8*B>"                        # a fixed literal in the shipped binary
iv   = 16 zero bytes
hash = AES-128-CBC-Encrypt(key, iv, uuid_bytes)  # exactly one block
```

`HashHex` is the lowercase hex of those 16 bytes, and the inverse (hex-decode, AES-CBC
decrypt, parse as a UUID) is implemented in the same source file. Worked example with the
placeholder UUID used throughout this document:

```
UUID     00112233-4455-6677-8899-aabb00000000
bytes    00112233445566778899aabb00000000
HashHex  ca1177e2fa6833204b74a6fcff88acdf
```

The key is the same in every installation, so this value is trivially forgeable and
reversible by anyone with the binary. It is an obfuscation, not a secret.

## Transport security: what exists and what you can actually use

Three separate mechanisms get confused with each other. They are unrelated.

### 1. `ProtocolHeader.Security` — AES-128-CBC inside the stream

A per-session 16-byte key encrypts the frame body. **You cannot use this on a self-hosted
server.** The chain of facts: [D]

- The gateway mints the key with `crypto/rand` and **never sends it to the device**. It
  sends the verbatim response body from a Visionect HTTPS "key escrow" service, which is
  opaque to the gateway, and the device is expected to resolve that blob itself.
- The escrow URL comes solely from an environment variable with **no compiled-in
  default**, and the shipped 8.5.5 image sets no such variable. With it unset the manager
  is built with a no-op poster whose `Post` returns `errors.New("key manager not
  initialized")`, which propagates all the way out and makes every connection's security
  activation fail.
- The feature is off by default anyway: the config key that enables it is absent from the
  shipped `config.json` template, so device enrollment writes the per-device `Secure`
  option as `"false"`, and devices enrolled before the key existed have no such option at
  all, which takes an earlier early-return branch.
- Every frame in the live capture carries `Security == 0`, in both directions. [W]

So: **a self-hosted server speaks plaintext by construction.** The decoder half is worth
implementing if you want to accept a device that was already keyed; the encoder half is
unreachable. Details in [framing.md](framing.md#encrypted-bodies-security--0).

Also note the three keys and IVs involved are **hardcoded ASCII literals in the shipped
binary**, identical in every installation. [D] The "security packet" variant
(`Security = 3`) and the device-ID hash are therefore forgeable by anyone with a copy of
the server.

### 2. TLS on the same port — opportunistic, and usable

The gateway peeks the first 6 bytes of each connection and tests for a TLS ClientHello
(`b[0]==0x16 && b[1]==0x03 && b[2] in {1,2,3,4} && b[5]==0x01`). If it looks like TLS it
wraps the connection; otherwise it reads a plain `ProtocolHeader`. So **plaintext and TLS
devices share one port.** [D]

- The gateway's config writes TLS 1.3 as the minimum version, which agrees with the
  device-side parameter 145, documented in the binary as
  `"TLS mode: 0=disabled, 1=TLS 1.3"`. [D]
- Certificate and key come from relative paths `certs/server.crt` / `certs/server.key`,
  overridable by environment variables, polled for mtime changes and hot-reloaded with a
  2 s debounce. **The 8.5.5 image ships no `certs/` directory**, so in a default vendor
  deployment the TLS branch is never taken. [D] The reference deployment logged
  `"couldn't get TLS certificate modification time: ... no such file or directory"` every
  60 s. [W]
- Parameter 145 is **network-writable**, so a reimplementation can turn TLS on without
  touching USB. [C]

**[GAP]** Whether the device validates the server certificate, and against what trust
store. No TCLV parameter for a server CA was found anywhere in the parameter table, which
*suggests* no validation [I], but this is unverified. It is also unverified whether
parameter 145 survives a reboot without an explicit `flash_save`.

> **One-way door.** Enabling parameter 145 against a server that has no certificate
> strands the device, recoverable only over the serial console. Install the certificate
> first, then flip the parameter.

### 3. `MobSecurity` — not transport security at all

Status tag 56, surfaced by the vendor API as `MobSecurity`, is the **cellular APN
authentication mode**. It is TCLV parameter 70, titled *"Mobile security mode"* in the
vendor's own descriptor table, and sits between the APN and username parameters. The
server never interprets the value: it goes through the generic plain-uint32 path and is
decimal-formatted, with no enum type, no value table and no `.String()` method anywhere
in any of the four binaries. [D]

On a WiFi device the field is inert. The reference device reports `MobSecurity: 2` while
reporting both `MobileModuleId` and `MobileLinkType` as the absent sentinel
`0xFFFFFFFF`. **[GAP]** what the value `2` means; no table exists in any binary, in the
vendor's API schema or in the admin UI assets. **[I]** the firmware may reuse the one
field for whichever link is active, in which case `2` matches TI SimpleLink's
`SL_SEC_TYPE_WPA_WPA2`; unconfirmed.

## The sign spends most of its life asleep

A battery sign is **normally offline**. It wakes, reports, and sleeps again. On the
reference device the gap between status packets was around 6.8 minutes when sleeping
(`LastStatus` deltas of ~405 s) and 60 s while awake and connected. [W]

The practical consequence for a server: **queue work, do not wait for it.** A parameter
read or an image push issued while the device is asleep has nowhere to go. The vendor's
own REST endpoints for device file operations carry `webhook_url` / `webhook_key`
parameters for exactly this reason — the call cannot complete synchronously. [D]

`ErrorCode` (status tag 1) is the *reason the status packet was sent*, not a fault
indicator — value 13 is `deep sleep request`. See [status.md](status.md).

## Minimum viable server

In rough dependency order, what you actually have to implement:

1. A TCP listener. [framing.md](framing.md)
2. `ProtocolHeader` encode/decode, with the **direction-dependent checksum**, and the
   24-byte block chain with raw-LZ4 blocks (or all-stored blocks, which the device
   demonstrably accepts).
3. `DataHeader` encode/decode, and the ID-echo rule. [packets.md](packets.md)
4. The status TLV decoder, enough of the tag table to read battery and firmware version,
   and a `Control{Flags: 1}` ack for every status. [status.md](status.md)
5. The image path: dither, bit-pack, the per-model geometry fold, and XXHash32 over your
   own 8-bit state buffer. [imaging.md](imaging.md)

You do not need: the crypto, packet type 2 (command), the CBOR packet type, the firmware
transfer, or region/delta updates. Each is explained where it appears.

---

## An unreachable server makes the sign reboot itself

If nothing answers on the configured server address, the device does not simply
retry forever. After a number of consecutive connection failures the firmware
gives up and **power-cycles itself**, announcing it on the console as:

```
E: Max conn errs. Reboot
```

Observed on firmware 7.4.4407: roughly **two reboots in 40 minutes** of server
downtime. [VERIFIED -- seen on the serial console, with the device's `last_boot`
moving correspondingly.]

Two consequences for anyone implementing a server:

1. **Server downtime is not free.** It costs the device reboots, and each reboot
   costs a full-screen redraw and a fresh DHCP/ARP/TCP cycle. Keep the listener
   up, and prefer a brief restart over a long outage.
2. **The status packet does not report it.** `ErrorCode` stays `0x0` throughout,
   so a server that only reads device status cannot see that it is causing
   reboots. The only in-band signal is `Uptime` resetting and `ConnectReason`
   changing; the explicit message exists solely on the USB console.

This is also why a device whose server has moved can appear to "re-dial on its
own" an hour later: it is not a retry timer, it is the device rebooting into its
newly saved configuration.
