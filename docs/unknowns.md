# What is still unknown

An honest ledger. Each entry says what is unresolved and, where there is one, the
experiment that would settle it. Nothing here is a known-unknown dressed up as a solved
problem; where the answer is "we guessed and it worked", that is said.

Status key: **OPEN** (nobody has tried) · **BLOCKED** (needs hardware or data we lack) ·
**PARTIAL** (works, with a caveat worth removing).

---

## Protocol gaps

### The inverse-update signal: the firmware's reaction — INFERRED

Clearing `RectangleHeader.Options` bit `0x0002` on a full-screen packet is verified
**server-side**. That the firmware reads it as "use the clearing waveform" is inference.
With the shipped configuration the bit is already clear, so a default deployment never
signals it; proving it needs a configuration in which bit 1 is *normally set*, then a
comparison of the panel's behaviour across one packet with the bit cleared.

See [imaging.md](imaging.md#imageheaderoptions-and-rectangleheaderoptions).

### `ImageHeader.Options` per-bit meanings — OPEN

No code in any of the four vendor binaries tests any individual bit; the server only
copies the value. The bits are presumably firmware flags. Nothing short of firmware
analysis or systematic experimentation on hardware will recover them.

### Rectangle alignment growth order — INFERRED, and unreachable here

`Width * Height` must divide into 16-bit quanta, and the vendor grows the rectangle
outward — but **not in which order**: right then left? symmetric? height first? It is
never sent on `HardwareNameID 8`, where the *server* disables rectangle support unconditionally and
every push is full-screen.

**To settle it:** capture one partial push from a device that does support rectangles.

### Interlacing mode 1 (hardware revision 1.0.0) — BLOCKED

The mode selector returns 1 for `HardwareNameID 8` at hardware revision exactly 1.0.0, and
2 otherwise. Mode 2's band pairing is verified at 100% pixel accuracy; **mode 1 has never
been observed**, and its pairing and plane order would have to be guessed. Guessing wrong
puts the image on the panel in the wrong order.

**To settle it:** a 1.0.0 device. Until then, refuse rather than guess.

### Secure mode: multi-chunk CBC IV chaining — BLOCKED

For a single-chunk body (≤ 2400 bytes, the overwhelming majority) the IV is unambiguously
a hardcoded literal. For multi-chunk bodies both sides update the IV between chunks and
the rule was not pinned: the encoder's register trace points at the previous chunk's
*ciphertext* tail (standard CBC chaining), the decoder's at what looks like the previous
chunk's *plaintext* tail. Standard chaining is likely but unproven.

Academic as long as a self-hosted server runs `Security = 0`, which it must — see
[overview.md](overview.md#1-protocolheadersecurity--aes-128-cbc-inside-the-stream).

### TLS: does the device validate the server certificate? — OPEN

TCLV 145 is network-writable and the gateway's TLS is opportunistic on the same port, so a
reimplementation can turn TLS on without a cable. Unknown: **whether the device validates
the certificate, and against what trust store.** No TCLV parameter for a server CA exists
anywhere in the 235-entry table, which *suggests* no validation — unverified. Also
unverified whether parameter 145 survives a reboot without an explicit `flash_save`.

> **One-way door.** Enabling 145 against a server with no certificate strands the device,
> recoverable only over the serial console. Certificate first, then flip.

### Packet type 2 (command) is entirely unverified — OPEN

Never captured, in either direction, in any capture. The 12-byte header is read out of the
Go struct and nothing more — and there is **direct precedent in this protocol for the
struct and the wire format disagreeing** (the param packet's struct is 12 bytes, its wire
header is 8).

Worse, **the v1 and v2 versions of the command enum disagree on values 0 and 1**, and one
of those two is `reboot`. See
[packets.md](packets.md#packet-type-2-command-is-unverified).

Two of the command ids — `refresh` and `clear screen` — have **no emission site anywhere
in the vendor server**; they are enum entries and nothing else.

**To settle it:** nothing on the wire will do it. Either find a capture from a deployment
that uses these, or probe cautiously against hardware you can physically recover, starting
with `echo`/`reboot` once you have determined which is which.

### Remaining small gaps — OPEN

| gap | what is missing |
|---|---|
| `DataHeader.Priority` | the value space. No stringer, and the gateway never writes it; 0 in every observed frame. |
| the status sentinel's **value** | what `0xFFFFFFFF`'s value means. It changes with the payload contents; **[I]** a nonce or a checksum over the records. The server carries it verbatim and never interprets it. |
| the GPS payload | the coordinate **string framing** inside packet type 7. |
| device-side error codes | the NACK payload code space. `0x00008a00` is observed, nothing documents it. |
| `DisplayIds` derivation | the function the device firmware uses to produce the panel-set identifier. The server only reads it and compares against a stored copy. |
| `MobSecurity` values | what `2` means. No value table in any binary, in the vendor's API schema, or in its web assets. **[I]** it may be a TI SimpleLink security-type ordinal. |
| TCLV value **types** | the vendor's descriptor tables carry only id, name and writability. The server's per-id parse/format maps cover 53 and 30 ids respectively; the remainder are raw bytes of whatever width the device returns. |
| TCLV 52's semantics | the parameter table calls it "Sleep mode **disable**" (reads as a boolean); the vendor's CLI documentation gives it three values with different meanings. The two descriptions disagree. |
| TCLV 131's key format | 16 printable ASCII characters is the best-supported reading **[I]**; base64 is second. Untested, and moot given the escrow problem. |

---

## Experiments needing the physical device

These are cheap — the serial console is reachable whenever a cable is, and nothing persists
without `flash_save`, which makes almost all of them reversible.

### Probe the unlisted commands for more hidden ones — ANSWERED for 49 of 97, 48 unreachable

The present/absent split was built by diffing `help` against the vendor reference, and the
method is **unsound**: one documented command (`wifi_ssid_set`) turned out to be present but
merely unlisted. So 97 commands were *presumed* absent on the strength of a method already
known to be wrong once.

The [existence oracle](usb-cli.md#the-existence-oracle) settles each one in one round trip.

> **Only safe for commands with a required argument.** A bare call to a **nullary** command
> **executes** it. Filter by arity from the vendor reference first; skip unknown arity.

**Result (7.4.4407, 2026-10-05): 49 probed, 49 genuinely absent, no new hidden commands.**
Those 49 are every unlisted command the vendor reference gives a required argument, less the
ones that flash firmware and the four whose arity the sources disagree about. All answered
`Command '<x>' not recognised.` The oracle was validated before, between and after the batch
with a known-present control and a nonsense name, so a false "absent" would have had to
survive a working positive control.

So the uncompiled hardware families (`touch_*`, `frontlight_*`, `scpu_*`, `mobile_*`,
`rs911x_*`, `sim5320_*`, `t2s_*`) really are uncompiled, and a reimplementation is **not**
refusing capabilities the device has. `wifi_ssid_set` is a one-off.

**The 48 still unprobed are unprobed on purpose**, and the oracle cannot reach them: 39 are
nullary in the vendor reference, 3 flash firmware, 4 have a disputed arity (`vcom_test`,
`touch_test`, `scpu_reset`, `scpu_psu_reset`), and `t2s_speak` would speak if its documented
arity is wrong. That set includes `flash_print` — the single highest-value command and
exactly the kind a bare call would simply *run*. The one question nothing else can answer is
whether an **undocumented memory or flash read** exists — see
[firmware.md](firmware.md#where-the-key-is-not).

Incidental finding: the vendor's page documents **162** commands, not the 160 previously
counted. `rs9110_scan` and `rs9113_scan` were missing from both the documented total and the
unlisted set, so the books were out by two at each end and still looked self-consistent.

### Can an SSID contain a space? — ANSWERED: yes, and there is no quoting convention

The setter that takes the SSID alone needs no credentials, so this was answerable without
`flash_save`: 16 forms were written and each one read straight back. It **takes the rest
of the line verbatim.** Interior spaces, runs of spaces, a leading space, a trailing
space, apostrophes, double quotes, single quotes, backslashes and percent signs all land
in the field byte for byte. So a spaced SSID works, and any attempt to quote or escape it
corrupts it — `"My AP"` reads back with the quotes in it.

Three observations rule out tokenise-then-take-the-first-token *and*
tokenise-then-rejoin: a run of two spaces survives as two, a leading space survives, and
a trailing space survives. The last is only visible in the raw frame, because the
library's line reader strips each reply line — anyone re-running this must read the raw
bytes or they will conclude the firmware trims.

The one whitespace character that cannot be carried is TAB, and it fails *silently*: the
console's line editor treats it as a usage-lookup key, prints the matched command's
syntax, and redisplays the buffer with the tab gone, so `Two<TAB>Words` sets `TwoWords`.

**The passphrase is a separate, still-open question.** Its setter is the same shape, so it
very probably carries a space too, but there is no read path for the passphrase field —
nothing can be checked, and a truncated one fails as a sign that will not associate.

### `vlog_unify_levels`: which end of the scale is quiet? — ANSWERED: 1 is quiet, and 0 is not a level

The question assumed `0` meant either "emit nothing" or "emit everything". It means
**neither**: the firmware answers `E: Invalid argument(s)` to `0`, and to `-1`, `6`, `99`
and anything non-numeric. **The valid range is 1–5**, on all three of `vlog_unify_levels`,
`vlog_set_source_level` and `vlog_set_destination_level` — five levels, matching the five
`[DIWEF]` severities.

**Level 1 is silent, level 5 is everything.** Measured four ways on 2026-10-05, including
one run with the level order reversed: 0 lines in 90 s at level 1, against 18–61 per 90 s at
level 5 on a healthy link and ~1 400 lines/min at level 5 while the radio was
reconnect-flapping. By line kind — 1 emits nothing; 2–3 only `E:` error lines; 4 adds state
narration (`From state N going to state N`, DHCP/IP/DNS); 5 adds debug detail (`Frame send N
bytes`, image transfer, EPD temperature). The command's own help line ("Reset logger levels
to default for USB") is wrong: it uses its argument, and `vlog_set_default_levels` is the
one that resets.

**It does remove the need for the demultiplexer's heuristic — but only inside a window you
control.** Nothing persists without `flash_save`, and the sign reboots itself when it cannot
reach its server (`E: Max conn errs. Reboot`), restoring default verbosity mid-session. It
also hides `E: TCP connection Error` along with the chatter, so level 2 or 3 is the better
choice for a quiet port that still reports faults.

**The two-axis model is only half characterised.** There are exactly **nine destinations**,
ids 0–8: the firmware range-checks that argument and refuses `-1` and `9` upward. It does
**not** range-check the *source* id — `-1`, `32` and `9999` are all accepted silently — so
the source namespace cannot be enumerated by probing, and an out-of-range id is an unchecked
index into firmware state. **Which destination id is the USB UART is still unknown.**
Identifying it needs a reliable stimulus that makes the firmware narrate on demand, and the
attempt was defeated by the sign having almost nothing to say once its link went stable:
the verbose output at levels 4–5 is mostly network state-machine churn, which only happens
while the link is flapping.

### `sf_rdid` / `sf_rdst` — PARTIAL

Both assert and kill the console task; the watchdog notices and does nothing; the device
recovers on its own reboot ~25 minutes later. **[I, untested]** running `sf_select` first
may make them safe, since the assertion is a missing-selection check.

### `play_music` — OPEN, trivial

"Play built-in song." Unknown what it actually does. Plausibly explains the beeping a sign
produces when no server is reachable.

### Panel draw latency — OPEN

Measured: encode 0.05 s; transfer and ack ~7 s for 1.84 MB; `DisplayStateCRC` confirmation
only on the next heartbeat, ~53 s later. So the perceived latency is mostly *confirmation*,
not draw time.

**Still unmeasured: the panel draw itself**, bounded between the ack and the next heartbeat.
The serial console narrates display activity, so capturing it across a push would pin it.
Worth doing, because "appears ~10 s after the call" is a very different promise from
"up to a minute".

### Which `.pv2` file is the live framebuffer? — OPEN

Six cached buffers for two panel channels implies current/previous/working, but **the
semantics are unknown**. Identification is cheap and does not need the semantics: every
frame carries an `ImageHeader.Checksum` you generated, stored in the file's first block, so
six × (open + 256-byte read + close) tells you which is which. The *meaning* of the six
slots remains open.

---

## A panel artefact, partially characterised

On the 31.2" four-panel sign there is a **visible dark band near the `ScreenID 0` /
`ScreenID 1` boundary**. It is present under the vendor's own software too, so it is the
device, not any reimplementation. It is only really obvious with blue-noise dithering,
because blue noise is isotropic and spatially stable: in a smooth gradient its texture is
the only structure, so a small systematic offset reads as a clean edge. Floyd-Steinberg's
worm artefacts and undithered banding both mask it.

**What it is.** A *localised dark band*, **not** a step between halves. Measured from an
uncorrected mid-grey column, as deviation from a fitted baseline in 8-bit levels:

```
 y=1256   -1.50
 y=1264   -4.94
 y=1277   -6.33    <- trough
 y=1290   -6.34
 y=1298   -3.40
 y=1307   -2.66
```

Roughly **45 source pixels wide (~12 mm)** and about **one full grey step** deep, near
y=1280 in canvas coordinates.

**Dead ends, recorded so they are not repeated.**

1. *Per-half offset.* The first model was "the lower half renders darker", derived from
   measuring a step with ±80 px windows and a ±10 px guard around the boundary. That window
   structure averages a narrow band into *both* sides and reports a small asymmetry instead
   of the band. A sweep of constant offsets added to the lower half made things **worse**:
   the band survived and the correction added a visible brightness step of its own. **A
   half-plane correction cannot cancel a localised band.**
2. *Symmetric raised-cosine bump on the boundary.* Halfwidths 24/40/64 at amplitudes
   0.6/1.0/1.4. The best was the **widest and strongest**, suggesting all of them were both
   too narrow and too weak — but none was clean.
3. *VCOM mismatch.* Ruled out: all four configured VCOM values read identical.

**Current hypothesis.** The compensation needs to be wider, stronger, **and not centred on
the screen boundary** — which would mean the band is offset from the join, arguing against
a simple two-driver-join explanation and explaining why every symmetric bump looked wrong
regardless of width. The first measurement also recovered faster on the high-y side, so the
true shape may be **skewed**.

**Next experiment.** Fix a wide halfwidth and a strong amplitude and sweep the **centre
offset** over roughly ±160 px against an uncorrected reference. Pin the centre first, then
refine width and amplitude, then consider an asymmetric profile.

**Method notes, learned expensively.**

- **Closed-loop beats photometry.** Reflective e-ink photographs badly: glare, lighting
  gradients and perspective repeatedly broke automated measurement (one photograph's glare
  merged eight test bars into two). Put candidate corrections on the glass side by side and
  let a human pick.
- **Make every candidate straddle the boundary, as columns.** A flat field puts the two
  sides far apart with no shared reference and the artefact becomes nearly invisible.
- **Use half-step base levels** (e.g. 8.5 on the 16-level ramp). At an exact ramp level the
  dither is a no-op and the test degenerates to a flat field.
- **Any correction must go in before dithering**, so it is absorbed into the noise rather
  than drawing an edge of its own.

**It may be uncorrectable.** If those pixels cannot reach the same states as the rest of
the panel, pre-compensation gets closer but never clean. If it *is* correctable, it is
per-unit silicon calibration data, not a property of the model — and worth noting that the
vendor's software has no compensation mechanism of any kind, so compensating would make a
reimplementation render *better* than the software it replaces.

---

## Fidelity caveats for anyone reimplementing

### Vendor LZ4 output is not reproducible — PARTIAL, probably permanent

Of 328 captured gateway blocks, **0** reproduce under a standard LZ4 block compressor in
default or fast mode at any acceleration; 3 match under high-compression mode, and those
are 44-byte ack bodies. Harmless for interoperability — the device decodes any valid LZ4
block — but **byte-exact re-encoding of compressed server frames is unavailable**, so you
cannot round-trip a captured server frame bit-for-bit.

### Dithering is not bit-exact to GraphicsMagick — PARTIAL, by choice

No GraphicsMagick output was available to diff against. A from-scratch implementation's
Bayer matrix and Floyd-Steinberg weights are its own, and `QuantizeImage`'s
histogram-derived palette is deliberately replaced with the uniform `n*17` wire ramp, which
is what the panel actually wants. Bayer is bi-level by nature, so warning at 4 bpp is
better than inventing a 16-level Bayer the vendor never produced.

This is a difference in output pixels, not in protocol conformance. The device cannot tell.

### Firmware decryption — BLOCKED

AES-**CBC** with a fixed fleet-wide IV, 2016 → 2024, key absent from the server, from the
device filesystem and from the console's command set (no memory or flash read exists).
Reachable only via SWD/JTAG or a chip-off flash dump.

**Do not re-run key searches without new information.** The first attempt was unsound (it
assumed ECB), and the sound re-run — 969 candidates across three ciphers at every legal key
length, against a verified ~2⁻¹²⁰ oracle — found nothing. A sound oracle exists, so any
candidate that ever surfaces is confirmed or rejected in microseconds.

See [firmware.md](firmware.md#the-payloads-are-encrypted--aes-cbc-fixed-fleet-wide-iv).

### Device framebuffer readback — OPEN as a feature

Proven possible: the six cached `.pv2` buffers are plaintext LZ4 and readable over packet
type 10. Not yet turned into anything useful. Combined with the state-checksum echo it
would let a server verify, rather than assume, what is on the glass.

---

## Things that were never checked at all

Stated so nobody assumes coverage:

- **Touch, buttons, GPS and accelerometer packets** (types 6, 7, 11) have layouts read from
  the vendor structs and have **never been seen on the wire** — the reference sign has no
  touch panel and no buttons. The gesture-rotation table in
  [imaging.md](imaging.md#the-display-model) is from the server's translation code, not
  from observation.
- **Packet type 12 (CBOR)** framing is understood; the **body is not parsed at all**. That
  channel belongs to a different device generation and the reference firmware never emits
  it.
- **The colour panels.** Two of the five drivers are colour (`*-color-mask`), with a real
  RGBW→RGB model, and none of it was examined beyond noting that it exists.
- **Multi-device display groups.** The synchronised-flip command exists and a server option
  governs whether to hold the flip until every member is online. Never exercised.
- **Cellular devices.** Every mobile-related field on the reference device reads as absent.
  The whole cellular path — APN configuration, link type, the `MobSecurity` value space —
  is unexercised.
- **Ethernet devices**, likewise.
- **Server-scheduled sleep.** The vendor server has a sleep scheduler with timezone and
  working-hours logic, which emits command type 4. It was read out of the binary and
  **never exercised**, because the deployment it was read from had the feature gated off.
  Note its one surprising detail: sleeps longer than 60 minutes are **quantised to whole
  hours**, and nothing can wake a sleeping device early.
