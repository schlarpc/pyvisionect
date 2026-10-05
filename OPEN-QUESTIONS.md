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

### A5. Push an image to the sign from `pyvisionect` — OPEN
**The biggest untested path.** Receive, decode and ack are proven against the
real device; the encoder reproduces captured payloads byte-exactly offline. But
we have never driven the panel from our own stack. Until that lands, "replaces
the vendor server" is only two-thirds demonstrated.

### A6. `sf_rdid` / `sf_rdst` crash the CLI task — PARTIAL
Both assert (`spi_flash_cli.c:139`/`:188`) and kill `usb_cli_task`; the watchdog
notices and does nothing, then forces a reset ~25 min later. They read a
*selected* device without checking `sf_select` ran. Untested hypothesis:
`sf_select` first makes them safe. Both are gated behind an explicit flag.

### A7. `play_music` — OPEN, trivial
"Play built-in song." Unknown what it does. Probably explains the reported
beeping when no server is reachable.

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
