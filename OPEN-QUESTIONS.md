# Open questions and unfinished work

The honest ledger. Each item says what is unresolved and the experiment or work
that would settle it. Kept next to the code so the method travels with it.

Status key: **OPEN** (not started) · **BLOCKED** (needs something we lack) ·
**PARTIAL** (works, with a caveat worth removing).

---

## A. Experiments needing the physical device

The sign is reachable over USB serial, so these are cheap right now. Nothing
persists without `flash_save`, which makes almost all of them reversible.

### A1. Probe the 95 "unlisted" commands for more hidden ones — OPEN
`ABSENT_FROM_7_4_4407` was built by diffing the device's `help` against the
vendor reference. The method is **unsound**: `wifi_ssid_set` was in that set and
is in fact present, merely unlisted.

Bare invocation is an existence oracle:

| response | meaning |
|---|---|
| `E: Invalid argument(s)` | present, hidden |
| `Command '<x>' not recognised.` | genuinely absent |

> **Only safe for commands with a required argument.** A bare call to a
> *nullary* command (`fs_format`, `cc3100_format`, `scpu_reset`, `scpu_upgrade`)
> **executes** it. Filter by arity from the vendor reference first; skip unknown
> arity. This is the same class of mistake as allow-listing `sf_rdid` (A6).

Payoff: whole families (`touch_*`, `frontlight_*`, `scpu_*`) are presumed absent
on hardware that may support them. Each hidden command is a capability the
library currently refuses to use.

### A2. Can an SSID contain spaces? — OPEN (unblocked by A1's finding)
`wifi_ssid_set` takes the **SSID alone**, no PSK, so this needs no credentials.
Without `flash_save`: read `wifi_conf_get`, try bare / `"quoted"` / `\ ` escaped
forms of a name with a space and an apostrophe, read back, restore. Answers
whether the console tokenises on whitespace or takes the rest of the line, and
decides whether `provisioning.py` can honestly claim arbitrary SSIDs.

Until then `plan_wifi` and `Sign.set_wifi` still refuse a spaced SSID, but the
refusal now says the route exists and is untested rather than claiming it is
impossible. Settling this is a one-line change to `plan_wifi`.

### A3. `vlog_unify_levels` — which end of the scale is quiet? — OPEN
`0` could mean "emit nothing" or "emit everything"; nothing observed
distinguishes them, and the help line ("Reset ... to default") contradicts its
own `<usb_level>` signature. Run it with a capture going and see whether the
heartbeat burst stops or multiplies, then `vlog_set_default_levels`. Until then
`unify_usb_log_levels()` takes a mandatory argument and promises nothing.

### A4. TLS on port 11113 — OPEN
`ProtocolHeader` sniffing shares the port with plaintext; the vendor peeks 6
bytes for a ClientHello. TCLV **145** (`TLS mode: 0=disabled, 1=TLS 1.3`) is
network-writable, so it can be set through our own listener.
Needs: an SSL context + the same 6-byte peek in `io/tcp.py`, a self-signed cert,
then flip 145. **Unknowns:** does the device validate the server certificate,
and against what trust store? No TCLV parameter for a server CA was found, which
*suggests* no validation — unverified. Also unverified whether 145 survives
without `flash_save`.
> One-way door: enabling 145 against a server with no cert strands the device
> (recoverable only over USB). Cert first, then flip.

### A5. Push an image to the sign from `pyvisionect` — **DONE**
Driven end to end on 2026-10-04 against the live 31.2" sign, with no vendor
software in the path. A 1440x2560 canvas encoded at 4 bpp with blue-noise
dithering in 0.05 s into 2 rectangles of 2880x640 (921,600 bytes each), pushed
as packet `4164952196`, acked by the device, and then — on the next heartbeat —
reported back as `DisplayStateCRC = 2631394564`, **exactly** the
`state_checksum` our encoder computed, with `DisplayUpdateCount` 0 -> 1.

That closes the loop: the device independently agrees, pixel for pixel, with
what we believed we sent. Every layer is now proven against real hardware --
framing, direction-dependent CRC, block chain, LZ4, the 4 bpp low-nibble pack,
the 4-band fold into 2 screens with the screen-1 lane swap, the `eink-flip`
mirror, and XXHash32 over the 8-bit state image.

### A6. `sf_rdid` / `sf_rdst` crash the CLI task — PARTIAL
Both assert (`spi_flash_cli.c:139`/`:188`) and kill `usb_cli_task`; the watchdog
notices and does nothing, then forces a reset ~25 min later. They read a
*selected* device without checking `sf_select` ran. Untested hypothesis:
`sf_select` first makes them safe. Both are gated behind an explicit flag.

### A7. `play_music` — OPEN, trivial
"Play built-in song." Unknown what it does. Probably explains the reported
beeping when no server is reachable.

### A10. Do rectangle (partial) updates actually work on this panel? — **ANSWERED: yes** (2026-10-05)

**They work.** The device accepts a rectangle smaller than the screen, acks it,
draws exactly that rectangle and nothing else, and reports back our state
checksum. The claim we had been making -- "partial updates don't work on this
hardware" -- was never about the hardware. `getRectangleSupport` returns false
unconditionally for `HardwareNameID == 8` (`client.go:41`), so the vendor's
server never sends this sign a partial rectangle. We are the server now, and the
sign takes them without complaint.

Driven live against the 31.2" Place&Play 32 (firmware 7.4.4407, hardware 1.1.0)
with `hass-visionect` stopped and our own listener on 11113. **17 pushes, 17
acks, zero NACKs**, `ErrorCode 0` throughout.

#### What was sent and what came back

| # | push | rect | raw bytes | ack | device-echoed `DisplayStateCRC` |
|---|---|---|---:|---|---|
| 1 | baseline grid, full screen | 2 x `2880x640` @ screen 0,1 | 1 843 200 | yes, 7 s | `1713733503` = ours |
| 2 | full-width strip | `2880x128` @ (0,256) screen 0 | 184 320 | yes, 3 s | `2291401510` = ours |
| 3 | off-axis block | `512x160` @ (1024,448) screen 0 | 40 960 | yes, 3 s | — |
| 4 | one-band block (lane B held) | `512x192` @ (256,64) screen 0 | 49 152 | yes, 4 s | `4191466816` = ours |
| 5-16 | same `256x128` block, 12 in a row | `256x128` @ (1600,96) screen 0 | 16 384 each | all, ~3 s each | — |
| 17 | final card, full screen | 2 x `2880x640` | 1 843 200 | yes, 4 s | `3498374405` = ours |

`DisplayUpdateCount` went **6 -> 23**: every partial counts as one update, same
as a full frame.

#### The firmware narrates it

The USB console echoes the rectangle header back verbatim, so there is no doubt
about what the device parsed:

```
full screen     l: (0   0   0 2880 640), enc: 0x4 pde: 0x0, te: 0x0
                u: (0   0   0 2880 640), wfn: 2, dum: 1, inv: 1
strip           l: (0   0 256 2880 128), enc: 0x4 ...
                u: (0   0 256 2880 128), wfn: 2, dum: 1, inv: 1
off-axis block  l: (0 1024 448  512 160), enc: 0x4 ...
```

`(ScreenID X Y W H)`, exactly the `RectangleHeader` we built. Same waveform
number (`wfn: 2`) for partial and full.

#### The glass agrees, including the fold

Photographed through the PTZ webcam after each push (`tmp/visionect/agent-a10/shots/`).

A **screen-space** rectangle on `ScreenID 0` lands on **two** canvas bands,
because screen 0 is the 4-pixel interleave of displays 0 and 1. The strip at
screen `y=256` appeared as two full-width black bars, at canvas `y 256..383` and
canvas `y 896..1023`, and nothing else on the panel moved -- the whole bottom
half (`ScreenID 1`, canvas `y 1280..2559`) was untouched. The `512x160` block at
screen `(1024,448)` landed at canvas `x 672..927`, `y 448..607` and
`y 1088..1247`, so **X offsets are honoured** too.

Both are exactly where the verified interlace map says they should be. The
screen-space geometry is:

```
screen x -> lane column  x/2            (keep x and w multiples of 8)
lane column c -> canvas column 1439-c   (the eink-flip mirror)
screen y -> band-local y, unchanged
screen 0 lanes = displays 0, 1          screen 1 lanes = displays 3, 2
```

Checked offline before anything went on the wire: a sub-rectangle cut this way
is **byte-identical** to the same window of the verified full-screen `2880x640`
payload (`tmp/visionect/agent-a10/verify_geometry.py`, 5/5 match).

#### A canvas-space partial is a screen-space one with the partner lane held

To repaint one band only, send a screen-space rectangle whose **other lane
carries the unchanged pixels from the server's own state image**. Push #4 did
that: canvas `y 64..255` went black, and its partner region at canvas
`y 704..895` -- which was redrawn with identical pixels -- shows no visible
change at all. Cost is 2x the bytes you strictly need, which is still nothing.

#### No device-side cap on consecutive partials

Twelve partials back to back, ~6 s apart, all acked, none promoted to a full
redraw, no accumulated ghosting visible at the test site. The vendor's
`noFullUpdateMax = 10` is a *server-side* policy; the firmware does not enforce
one. Keeping a periodic full refresh is still sensible for ghosting, but it is
our choice, not the device's.

#### What it actually buys -- bandwidth, not latency

Measured from the firmware's own `Profiling:` line:

| push | wire bytes (`Pv2Len`) | raw bytes | `EpdUpd` | total |
|---|---:|---:|---:|---:|
| full screen, first of session | 144 976 | 1 843 200 | 5 299 ms | 6 298 ms |
| full screen, steady state | 77 613 | 1 843 200 | 2 916 ms | 3 637 ms |
| `2880x128` strip | 2 143 | 184 320 | 2 908 ms | 3 039 ms |
| `512x160` block | 557 | 40 960 | 2 905 ms | 2 992 ms |
| `256x128` block | **292** | 16 384 | 2 904 ms | **2 983 ms** |

**The honest reading: the wire cost collapses by ~250x, the panel time barely
moves.** `EpdUpd` is ~2.9 s whatever the rectangle's area -- the waveform has a
floor on this panel and a partial does not escape it. The first push after a
long idle took 7 waveform passes and `UPD_FULL`; everything afterwards, partial
*and* full, took 2-4 passes of `UPD_FULL_AREA`. So `UPD_FULL` vs `UPD_FULL_AREA`
is **not** "partial vs full rectangle"; it is the device's own periodic clearing
refresh.

So a clock that changes one digit costs ~300 bytes and ~3.0 s instead of ~78 KB
and ~3.6 s, plus it saves encoding and LZ4-ing 1.84 MB on the server every tick.
That is a real win for a battery device on wifi and for a Raspberry Pi doing the
encoding -- it is **not** the "continuous updates" win we hoped for, because the
~3 s panel floor is unchanged.

#### What it would take to use this in the library

1. **`DeviceState.supports_rectangles`** (`session/device.py:106`) returns False
   for `HardwareNameID 8`. That is a correct statement about the *vendor
   server's* behaviour and a false one about the device. It needs to become two
   ideas, not one: "the vendor stack would never send one" and "this device
   accepts one".
2. **`Panel.forces_full_screen`** (`imaging/panel.py:248`) promotes every frame
   to full screen. Verified: `encode_frame(..., rects=[Rect(200,300,400,100)],
   prev_state=...)` returns `full_screen=True` with the usual 2 x `2880x640`.
   Patch that property out and the next gate fires --
   `ValueError: interlacing needs exactly one full-size rectangle per display
   (got [1, 0, 0, 0])`. Both gates are *about the fold*, and both are correct
   for a canvas-space rectangle.
3. **The missing piece is a screen-space encoder**, which never enters the fold:
   cut the rectangle from the state canvas for both lanes of the target screen
   (`INTERLACE_PAIRS[2]`), mirror, pack 4 bpp, interleave, emit one `Rectangle`
   with screen-space `x/y/w/h`. Constraints: `x` and `w` multiples of 8 so each
   lane row is a whole number of 2-byte interleave groups, and the existing
   `w*h % 4 == 0` quantum.
4. **Checksum bookkeeping has to follow the partial.** Apply the rectangle to
   the state image and re-hash, or the device's echoed `DisplayStateCRC` will
   not match and the next push costs a redundant full redraw. Every partial
   above did this and every echo came back equal.

Working code for all of it: `tmp/visionect/agent-a10/a10d_lib.py`
(`lane_regions`, `screen_rect`).

#### Still unknown

* **Out-of-bounds rectangles were deliberately not probed** (`y+h > 640`,
  `x+w > 2880`). Whether the firmware clamps, refuses, or writes past its
  framebuffer is unmeasured, and poking a memory-safety edge on a kitchen sign
  was not worth it. Clamp server-side until someone tests it.
* Only `ScreenID 0` was exercised on hardware. Screen 1 is the same code path
  with the lane swap (displays 3, 2) and was checked offline, not on glass.
* Only one rectangle per packet was sent; `NrPrimitives > 1` with mixed
  geometry is untested.
* Ghosting over hundreds of partials is not characterised -- 12 was clean.

### A11. `display_out_of_sync` false-alarms on every push — OPEN (HA integration)

Confirmed live. A push legitimately leaves the device out of sync until it draws
the frame and reports the new `DisplayStateCRC` on its next heartbeat --
measured at **~45-60 s** on this sign. During that window:

```
t+0s    pushed=3557037784  device=3256188928  in_sync=false
t+45s   pushed=3557037784  device=3557037784  in_sync=true   updates 3 -> 6
```

The entity carries `BinarySensorDeviceClass.PROBLEM`, so Home Assistant raises a
**problem indicator after every normal update**. The underlying tri-state is
correct and the library is right to report `in_sync=false` in the gap; it is the
presentation that is wrong.

Fix: debounce against the expected confirmation window -- only assert the problem
once the device has had a contact *after* the push and still disagrees (i.e.
`last_contact > last_push` and still out of sync), or allow ~2x the heartbeat
interval. Note `pending_changes` already reports its own `in_sync: true` from
integration bookkeeping, so the two entities visibly contradicted each other
during the window, which is how this was found.


### A8. The panel-boundary dark band — OPEN, partially characterised

A visible artefact at the ScreenID 0 / ScreenID 1 boundary on the 31.2" panel.
Present under the **vendor stack too**, so it is the device, not this library.
Only really obvious with blue-noise dithering, because blue noise is isotropic
and spatially stable: in a smooth gradient its texture is the only structure, so
a small systematic offset reads as a clean edge. Floyd-Steinberg's worm
artefacts and `none`'s hard banding both mask it.

**What it is.** A *localised dark band*, **not** a step between halves. Measured
from an uncorrected mid-grey column (base level 8.5, blue noise), deviation from
a fitted baseline, in 8-bit photo levels:

```
 y=1256   -1.50
 y=1264   -4.94
 y=1277   -6.33     <- trough
 y=1290   -6.34
 y=1298   -3.40
 y=1307   -2.66
```

Roughly **45 source px wide (~12 mm)**, about **one full grey step** deep,
near y=1280.

**Dead ends, recorded so they are not repeated.**
1. *Per-half offset.* The first model was "ScreenID 1 renders darker", from
   measuring a step with +-80 px windows and a +-10 px guard around the
   boundary. That window structure averages a narrow band into *both* sides and
   reports a small asymmetry instead of the band. A sweep of constant offsets
   (0.0 .. 1.4 steps added to the bottom half) made things **worse**: the band
   survived and the correction added a visible brightness step of its own.
   A half-plane correction cannot cancel a localised band.
2. *Symmetric raised-cosine on the boundary.* Halfwidths 24/40/64, amplitudes
   0.6/1.0/1.4. Best was the **widest** (64/1.0), suggesting both too narrow and
   too weak -- but none was clean.
3. *VCOM mismatch.* Ruled out: `display_conf_get` reports Vcom 0..3 all
   identical at 2400 mV (4..7 unused).

**Current hypothesis.** The bump needs to be wider, stronger, **and not centred
on y=1280** -- the owner's read is that the centre belongs further *up* (smaller
y, canvas coordinates, where the rendered text is upright). That would mean the
band is offset from the screen boundary, which argues against a simple
two-driver-join explanation and explains why every symmetric bump looked wrong
regardless of width.

**Next experiment** (`tmp/visionect/push/sweep4.py`, written but never
evaluated): fix halfwidth 128 / amplitude 1.4 and sweep the **centre offset**
over -160, -120, -80, -40, 0, +40, +80 px against an uncorrected reference.
Pin the centre first, then refine width and amplitude, then consider an
**asymmetric** profile -- the first measurement recovered faster on the high-y
side (-3.40 at y=1299 vs -4.94 at y=1264), so the true shape may be skewed.

**Method notes.**
- Closed-loop beats photometry here. Reflective e-ink photographs badly: glare,
  lighting gradients and perspective repeatedly broke automated measurement (one
  photo's glare merged all 8 bars into 2). Put candidate corrections on the
  glass side by side and let a human pick.
- Make every candidate **straddle** the boundary, as columns. A flat field puts
  the two sides far apart with no reference and the artefact becomes nearly
  invisible.
- Use **half-step** base levels (e.g. 8.5). At an exact ramp level the dither is
  a no-op and the test degenerates to a flat field.
- Any correction must go in **before** dithering, so it is absorbed into the
  noise rather than drawing an edge of its own.

**If it is correctable**, it ships as optional per-panel calibration data in
`imaging/` -- values specific to one unit's silicon, not to the model. The
vendor stack has no compensation mechanism of any kind, so this would make the
reimplementation render *better* than the software it replaces. It may also
prove uncorrectable: if those pixels cannot reach the same states as the rest of
the panel, pre-compensation gets closer but never clean.

### A9. Push and refresh latency — measured, mostly explained
From the first live push: encode **0.05 s**; image transferred and **acked after
~7 s** (~1.8 MB to a TI CC3100 at 1-3 Mbps -- LZ4 buys almost nothing on
dithered data); `DisplayStateCRC` confirmation only arrives on the next
heartbeat, **~53 s later**. So the "about a minute" is mostly *confirmation*
latency, not draw time. **`cs 3`** ("Connect to server", documented) forces an
immediate reconnect and removes the wake-cycle wait.
Still unmeasured: the panel draw itself, bounded between the ack and the next
heartbeat. The serial console narrates display activity, so capturing it across
a push would pin it -- worth doing, since "appears ~10 s after the service call"
is a very different UX promise from "up to a minute".

---

## B. Protocol gaps — understood but unverified

### B1. Inverse update: the firmware's reaction — INFERRED
Clearing `RectangleHeader.Options` bit `0x0002` on a full-screen packet is
verified *server-side*. That the firmware reads it as "use the clearing
waveform" is inference. With `RectangleFlags = 0` the bit is already clear, so
this deployment never signals it; proving it needs `RectangleFlags` to normally
set bit 1.

### B2. Rectangle alignment growth order — INFERRED
`W*H` must divide into 16-bit quanta, and the vendor grows the rect outward —
but not in which order (right-then-left? symmetric? height first?). Unreachable
on this hardware, where `getRectangleSupport` is false for `HardwareNameID == 8`
and every push is full-screen. Settle it by capturing one partial push from a
device that supports rectangles.

### B3. Interlacing mode 1 (hw rev 1.0.0) — BLOCKED
Raises `NotImplementedError` rather than guessing a band pairing we have never
observed. Needs a 1.0.0 device.

### B4. Secure mode: multi-chunk CBC IV chaining — BLOCKED
For a single chunk the IV is a hardcoded literal. For multi-chunk bodies both
sides update the IV between chunks and the rule was not pinned; standard CBC
chaining off the previous chunk's ciphertext tail is likely but unproven.
Academic while we ship `Security = 0`.

### B5. Remaining `[GAP]`s — OPEN
`packet.Priority`'s value space; the meaning of the status sentinel's *value*
(carried verbatim, never interpreted); the GPS coordinate string framing.

### B6. All of packet type 2 (command) is unverified — OPEN
Never captured. The param packet is precedent for the Go struct (12 B) and the
wire (8 B) disagreeing, so type 2 is flagged in the API, `refresh`/`clear_screen`
route through image pushes instead, and `reboot` ships disabled by default.

### B7. Which `.pv2` index is the live framebuffer? — OPEN
Six buffers for two channels implies current/previous/working. Sidestepped:
every frame carries an `ImageHeader.Checksum` we computed, stored in block 0, so
identification is 6 × (open + 256-byte read + close). The *semantics* remain
unknown.

---

## C. Library fidelity

### C1. Vendor LZ4 is not python-lz4 — PARTIAL, probably permanent
0 of 328 captured gateway blocks reproduce under `lz4.block.compress` in default
or fast mode at any acceleration; 3 match under high_compression, and those are
44-byte ack bodies. Harmless for interop (the device decodes any valid block),
but it means byte-exact re-encoding of *compressed server frames* is
unavailable. `encode_blocks(compressor=...)` is the seam.

### C2. Golden fixture: server→device frames are our re-encoding — PARTIAL
Scrubbing network identifiers required editing plaintext inside compressed
frames, which C1 makes unreproducible, so those 30 frames were re-encoded.
`test_server_frames_rebuild_from_scratch_byte_exactly` replays the fixture's own
body and is therefore partly self-referential for that direction.
**Unaffected:** the 31 device→server frames are verbatim apart from the
substituted bytes, and the image-push payload blocks are untouched vendor bytes,
so the byte-exact imaging result stands.

### C3. Dithering is not bit-exact to GraphicsMagick — PARTIAL, by choice
No GM output was available to diff. Our bayer matrix and FS weights are our own,
and `QuantizeImage`'s histogram-derived palette is deliberately replaced with the
uniform `n*17` wire ramp. Bayer is bi-level by default and warns at 4 bpp rather
than inventing a 16-level bayer GM never produced.

### C4. Full-pcap replay tests skip — PARTIAL
2 tests skip unless the 1.9 MB pcap is present; the 93 KB extracted fixture
carries the rest. Fine, but means the full-stream path is unexercised in CI.

---

## D. Not yet built

### D1. Home Assistant integration — OPEN
Designed in detail (entity model, deferred-command queue, content-source
abstraction, config flow, Web Serial provisioning) but not implemented.

### D2. Device framebuffer readback as a feature — OPEN
Proven possible (`/image0..5.pv2`, plaintext LZ4, read over packet type 10).
Not yet exposed as a library API or an HA service.

### D3. Firmware decryption — BLOCKED
AES-**CBC** with a fixed fleet-wide IV, 2016→2024, key absent from the server,
the device filesystem and the 112-command CLI (no memory or flash read exists).
Reachable only via SWD/JTAG or chip-off. Do not re-run key searches without new
information; the earlier 242-key attempt was unsound (it assumed ECB), and the
sound 969-candidate re-run found nothing.

### D4. Public protocol documentation — OPEN
The reverse-engineering reports (~8.4k lines) are the most useful artefact for
anyone else with one of these signs, but contain deployment credentials and
cannot be published as-is. Needs a scrubbed edition.

### D5. LICENSE — OPEN
No licence file. The repo is public and therefore implicitly all-rights-reserved,
which is probably not the intent.

---

## E. State to restore

### E1. The sign is persistently pointed at a workstation — OPEN
`server_tcp_set 10.42.0.50 11113` was `flash_save`d to survive the reboot that
forced the reconnect. **Original: `visionect.internal.schlarp.xyz:11113`,
heartbeat interval 1.** Restoring is an explicit step.
Note `cs 3` ("Connect to server") forces a reconnect without a reboot, so the
restore needs no `flash_save` at all — that is the better path, and would have
avoided persisting anything in the first place.

### E2. The firewall rule is temporary — OPEN
Port 11113 was opened with `nixos-firewall-tool`, which does not survive a
reboot, while E1 *does*. If the host reboots before E1 is restored, the sign has
nowhere to report — likely the reported beeping. Either restore E1 or make the
rule declarative.

### E3. The listener is a user unit, not declarative — OPEN
`~/.config/systemd/user/pyvisionect-listener.service` with lingering enabled, run
from a venv outside the repo. Survives reboot, but is not in the NixOS config.
