# Open questions and unfinished work

The honest ledger. Each item says what is unresolved and the experiment or work
that would settle it. Kept next to the code so the method travels with it.

Status key: **OPEN** (not started) · **BLOCKED** (needs something we lack) ·
**PARTIAL** (works, with a caveat worth removing).

---

## A. Experiments needing the physical device

The sign is reachable over USB serial, so these are cheap right now. Nothing
persists without `flash_save`, which makes almost all of them reversible.

### A1. Probe the "unlisted" commands for more hidden ones — **ANSWERED: there are none reachable** (2026-10-05)

**49 probed, 49 genuinely absent, 0 new hidden commands.** `wifi_ssid_set` is a
one-off, not the tip of an iceberg, and the library is **not** refusing
capabilities this device actually has.

`ABSENT_FROM_7_4_4407` was built by diffing the device's `help` against the
vendor reference, and the method is **unsound**: `wifi_ssid_set` was in that set
and is in fact present, merely unlisted. Bare invocation is an existence oracle:

| response | meaning |
|---|---|
| `E: Invalid argument(s)` | present, hidden |
| `Incorrect command parameter(s).` | present, hidden (the `vlog_*` spelling) |
| `Command '<x>' not recognised.` | genuinely absent |

Note the oracle has **two** positive spellings, which was not known before. And
matching on the `E:` prefix alone is not sound: the firmware emits unsolicited
`E: ...` log lines too (`E: TCP connection Error: -111`), so a probe has to
match the full string. This probe did.

> **Only safe for commands with a required argument.** A bare call to a
> *nullary* command (`fs_format`, `cc3100_format`, `scpu_reset`, `scpu_upgrade`)
> **executes** it. Filter by arity from the vendor reference first; skip unknown
> arity. This is the same class of mistake as allow-listing `sf_rdid` (A6).

#### What was probed, and how the candidates were chosen

Arity came from the vendor page's own per-command **Syntax** row, parsed out of
the HTML rather than read off a summary — 162 sections, one per command. Two
cross-checks mattered:

* `touch_hw_pwr`'s Syntax row is **corrupt** on the vendor page (it repeats the
  description). Its **Parameters** table names a required `enable`, which is the
  independent second source that let it be probed.
* Every other command's Syntax row and Parameters table agree.

| disposition | n | reason |
|---|---:|---|
| **probed** | **49** | ≥1 required argument in the vendor reference |
| skipped | 39 | nullary — a bare call would *execute* it |
| skipped | 4 | arity disputed: `vcom_test`, `touch_test`, `scpu_reset`, `scpu_psu_reset` (reference says 1 arg, field notes say nullary-and-destructive) |
| skipped | 3 | flashes firmware: `scpu_upgrade`, `scpu_psu_upgrade`, `touch_fw_update` |
| skipped | 1 | `t2s_speak` — would speak aloud if the documented arity is wrong |

All 49 answered `Command '<x>' not recognised.` The 49 are recorded as
`PROBED_ABSENT_7_4_4407`, kept **separate** from `ABSENT_FROM_7_4_4407` so that
"proven absent" and "merely unlisted" never merge back into one idea.

#### Why the negative is trustworthy

A negative result from an oracle is only as good as the oracle. It was validated
three times in the same session, in the same code path:

* **before** the batch — `wifi_ssid_set` → `E: Invalid argument(s)`, a nonsense
  name → `not recognised`;
* **after** the batch — both again, plus a resample of 6 of the 49, all
  reproducing;
* and the captured `help` was re-parsed and matched `LISTED_BY_HELP` exactly
  (111 names, with `wifi_ssid_set` confirmed absent from it).

So a false "absent" would have had to survive a working positive control on
either side of it.

#### The 48 that remain, and why they stay that way

They are not unprobed through haste — the oracle cannot safely be pointed at
them. The loss that matters is **`flash_print`**, the single highest-value
command for dumping stored settings, and it is nullary: a bare call runs it.
Settling it needs a different method (a firmware-side look, or accepting that
running it is harmless — it only prints). `fw_checksum_get` and
`accelerometer_conf_get` are in the same position.

#### Incidental finding: the documented count was wrong

The vendor page documents **162** commands, not 160. `rs9110_scan` and
`rs9113_scan` were missing from `DOCUMENTED_COUNT` *and* from
`ABSENT_FROM_7_4_4407`, so the books were out by two at both ends and still
looked self-consistent. `commands.py` now asserts the arithmetic
(`present-and-documented + unlisted == documented`) so that cannot recur.

### A2. Can an SSID contain spaces? — OPEN (unblocked by A1's finding)
`wifi_ssid_set` takes the **SSID alone**, no PSK, so this needs no credentials.
Without `flash_save`: read `wifi_conf_get`, try bare / `"quoted"` / `\ ` escaped
forms of a name with a space and an apostrophe, read back, restore. Answers
whether the console tokenises on whitespace or takes the rest of the line, and
decides whether `provisioning.py` can honestly claim arbitrary SSIDs.

Until then `plan_wifi` and `Sign.set_wifi` still refuse a spaced SSID, but the
refusal now says the route exists and is untested rather than claiming it is
impossible. Settling this is a one-line change to `plan_wifi`.

### A3. `vlog_unify_levels` — which end of the scale is quiet? — **ANSWERED: 1 is quiet, and 0 is not a level at all** (2026-10-05)

The question was *"does `0` mean emit nothing or emit everything?"* It means
**neither**. The firmware answers `E: Invalid argument(s)` to `0` — and to `-1`,
`6`, `99` and anything non-numeric. **The valid range is 1–5**, on all three of
`vlog_unify_levels`, `vlog_set_source_level` and `vlog_set_destination_level`.
Five levels, matching the five `[DIWEF]` severities. So the premise was wrong,
and the honest headline is that the old API invited callers to pass the one
value the device refuses.

**Level 1 is silent. Level 5 is everything.** The help line ("Reset logger
levels to default for USB") is simply wrong: the command uses its argument, and
`vlog_set_default_levels` is the one that resets.

| level | what reaches the USB UART |
|---|---|
| 1 | nothing |
| 2, 3 | `E:` error lines only |
| 4 | + state narration (`From state N going to state N`, `New connectivity state`, DHCP/IP/DNS) |
| 5 | + debug detail (`Frame send N bytes`, `Image transfer pending`, `EPD temperature`) |

#### The measurements, and the two confounds that had to be beaten

| run | method | level 1 | level 5 |
|---|---|---:|---:|
| 1 | passive, interleaved 90 s × 2 each | **1 line / 180 s** | 4 099 lines / 180 s |
| 2 | passive sweep 1–5, 45 s each | *(polluted by a reboot)* | 213/min |
| 3 | `pss`-triggered, 3 rounds, order reversed on round 2 | **0, 0, 1** | 57, 31, 52 |
| 4 | passive 90 s, 5→1→5 | **0 lines / 90 s** | 18 and 61 lines / 90 s |

Two things made this harder than it looks, and both are worth recording because
they would catch the next person:

1. **The passive rate is not a stable baseline.** The sign deep-sleeps between
   heartbeats, so a short window either catches a burst or does not — and an
   *asleep* sign narrates nothing at any level, which zeroed a whole round. The
   fix was a deterministic stimulus: `pss` (ACTION tier) makes the sign emit a
   status packet on demand, and counting the lines *it* produces is immune to
   both. `app_wakeup` first makes the awake state deterministic too.
2. **A concurrent agent was pushing images**, and the sign's own reboots
   injected boot banners mid-window. Interleaving the levels (and reversing the
   order on one round) is what stops a drifting background rate masquerading as
   a result.

The level 2-vs-3 ordering looks non-monotonic (12.0 vs 3.3 lines mean) and is
not: level 2's count is dominated by an `E: TCP connection Error: -111` retry
storm, which is an artifact of the sign's server being unreachable at the time,
not a property of the scale.

#### Does it suppress the interleaving `console.py` works around? Yes — conditionally

Yes: at level 1 the port goes to ~0 lines, and the lines it silences are
*exactly* the heartbeat narration `LOG_PATTERNS` exists to filter
(`sys evt vplatform_heartbeat.c:19, Heartbeat (7)`, `Received event: Heartbeat
(7)`, `Heart-beat event`). But it is **not** a replacement for the four
mechanisms, for two reasons:

* **It does not persist, and the sign un-does it by itself.** Nothing survives
  without `flash_save`, and the sign reboots when it cannot reach its server —
  observed directly, with the firmware stating the reason: `E: Max conn errs.
  Reboot`. A long-lived console session will silently get default verbosity back
  partway through. Re-apply after any reconnect; never assume it is in force.
* **It hides the diagnostics too.** Level 1 also suppresses
  `E: TCP connection Error`, often the only indication the far end is dead.
  **Level 2 or 3 is the better choice** for a quiet port that still reports
  faults — that is the recommendation for anyone driving the console
  programmatically.

#### The two-axis model — half characterised, and one real hazard

* **Nine destinations, ids 0–8.** The firmware range-checks this argument:
  `-1`, `9`, `16`, `100`, `255`, `9999` are all refused.
* **The source id is not range-checked at all.** `-1`, `14`, `32`, `100`, `255`
  and `9999` are *accepted silently*. So the source namespace cannot be
  enumerated by probing, and an out-of-range id is an unchecked index into
  firmware state. `set_log_source_level` now refuses a negative source on its
  own authority, because the device will not. **This is the one place the
  library is deliberately stricter than the firmware.**
* **Which destination id is the USB UART is still unknown — INCONCLUSIVE.** Two
  attempts failed, and the reason is informative: the verbose output at levels
  4–5 is mostly *network state-machine churn*, so once the sign's link went
  stable there was nothing left to narrate and the control condition collapsed
  to 1–2 lines. Identifying it needs a stimulus that provokes narration
  independent of link state. Recorded as unfinished rather than guessed at.

API now: `unify_usb_log_levels(level)` validates 1–5 and keeps its mandatory
argument (a console driver wants 1, someone debugging a dead link wants 3);
`silence_usb_logs()` is the shorthand for the quiet end.

### A4. TLS on port 11113 — **ANSWERED: the server side works, the device side does not exist** (2026-10-05)

**TLS is implemented and proven against the live sign's own listener. The sign
will never use it: firmware 7.4.4407 does not implement TCLV parameter 145 at
all,** and answers both a read and a write of it with the same error code it
gives for a parameter id that was invented for the experiment.

So the one-way door turns out not to be a door. It cannot be opened from the
network, which also means it cannot be used to strand the sign — the hazard this
item was written around is not reachable on this firmware.

#### The server half: opportunistic TLS on the one port

`io/tcp.py` now does what the gateway does. Pass `ssl_context=` (or
`certfile=` / `keyfile=`, or use `server_ssl_context()`), and the first six
bytes of each connection decide, per connection, whether it is TLS or
plaintext. One listener, one port, both protocols. With nothing configured the
behaviour is as before.

Two details worth keeping:

- **`loop.start_tls()` cannot be used.** Recognising a ClientHello means
  consuming the six bytes that begin it, and OpenSSL reads from the socket, so
  it would never see them and the handshake would die on a malformed record.
  The listener drives an `ssl.SSLObject` over a pair of `ssl.MemoryBIO`
  instead, which puts us in charge of what OpenSSL reads — and the first thing
  it reads is those six bytes. The adapter quacks like the
  `StreamReader`/`StreamWriter` pair the read loop already used, so nothing
  above it changed.
- **The sniff runs even with TLS off.** Not for the upgrade, which is
  impossible without a certificate, but so that a ClientHello arriving at a
  plaintext-only listener is logged and counted (`ServerStats.tls_unsupported`)
  instead of being rejected as a bad frame header. That was the one failure
  mode capable of stranding a sign, and it needed to be legible.

There is no collision with the plaintext path and the tests say so over every
six-byte window of a real frame: `ProtocolHeader.Version` is a little-endian
`uint32`, so version 3 puts `03 00 00 00` on the wire and fails the sniffer's
first test. A header would have to carry `Version == 0x01030316` to pass.

Verified against the **live Home Assistant listener** the sign was connected
to at the time, with a self-signed certificate for `10.42.0.50`:

```
version: TLSv1.3
cipher : ('TLS_AES_256_GCM_SHA384', 'TLSv1.3', 256)
peer cert bytes: 800
```

and, in the same process, the sign itself reconnected and kept working in
plaintext. That is the whole point of the design: `tls_accepted 1`,
`identified 1`, bytes flowing both ways, one port.

#### The device half: parameter 145 is not in this build

Everything below is from the wire, decoded out of a `tshark` capture of the
sign's own connection. Three attempts, one answer:

| what the server sent | what the device answered |
|---|---|
| `id=145 control=0 (read) len=0` | `control=2 (read error) value=00 00 58 00` |
| `id=145 control=1 (write) len=1 value=01` | `control=3 (write error) value=00 00 58 00` |
| `id=145 control=1 (write) len=4 value=01000000` | `control=3 (write error) value=00 00 58 00` |

`00 00 58 00` is not a generic "no", and that is the whole argument. Sweeping
130..160 one id at a time produced **four different** error values, and the
three that are not `0x58` land on parameters this device demonstrably has:

| error value | ids |
|---|---|
| `0x58` | 133–137, 141, 142, **145**, 146–151, 158–160, and **250, invented for this test** |
| `0x5d` | 138 (`BLE MAC`), 143 (`Executes WiFi module upgrade`), 157 (`Format the file system`) |
| `0x8d` | 139 (`Performs WiFi scan`) |
| `0x60` | 140 (`BLE advertising data`) |
| `0x5a` | **29** (`Heart beat interval`) — on a *one-byte* write only |

BLE is enabled on this sign (155 reads 1), it has a filesystem, it has a WiFi
module — and those ids answer with something other than `0x58`. The firmware is
not refusing everything it does not want to discuss with one code; it is
distinguishing cases, and 145 falls in the same bucket as an id that exists in
no table anywhere.

`0x5a` is the other end of the control. Parameter 29 read back cleanly as
`01000000` in the same session, so it exists; a one-byte write of 2 was refused
with `0x5a`, and a four-byte write of the same value was then **accepted** and
read back as `02000000`. So the device also distinguishes "your value is wrong"
from "no such parameter" — and 145 gets the second, from the read path (which
carries no value at all and so cannot be a value complaint) *and* from the
write path at the correct width.

The parameter table in `devices/tclv.py` is the **server's** table: it covers
every device generation the gateway supports. This firmware implements a sparse
subset of it — in 130..160 it answers only 130, 131, 132, 144, 152, 153, 154,
155 and 156. 145 is simply not in the 7.4.4407 build.

(The remaining three error values have no interpretation. Three of their four
ids are command-shaped, which is suggestive and nothing more.)

And the sign never behaved as though anything had changed. Four separate
connections were captured across the experiment window, including one the sign
dialled from scratch after `cs 1` tore the session down **immediately after an
attempted write of 145**. Every one of them opened `03 00 00 00 00 00`. No
ClientHello ever left the device.

#### What this settles, and what it does not

**Settled.** TLS cannot be enabled on this sign from the network. The hazard
this item was written around — enabling 145 against a certificate-less server
and stranding the device — is unreachable here, because the write never lands.

**Settled, incidentally.** That the device has a refusal *vocabulary* rather
than one error, which is what made any of this diagnosable — and that TCLV
value width is a property of the parameter rather than of the value. That
second one was a live bug: `write_params({29: 1})` had been sending one byte to
a `uint32` and being refused silently, because nothing looked at `control=3`.
Fixed, with the measured widths recorded in
`devices.tclv.VALUE_WIDTHS`; `flash_save` (53) is genuinely one byte, which is
why it had always worked and why nobody noticed.

**Still open — and no longer answerable from this sign.**

- *Does the device validate the server certificate, and against what?* No
  ClientHello was ever sent, so nothing validated anything. The inference
  stands and stays an inference: no TCLV parameter for a server CA exists
  anywhere in the table, and the device's only certificate store is the WiFi
  EAP one, which `certs_config_get` reports as `No EAP cert found!`. That is
  suggestive of no validation, or of a baked-in store, and it is not evidence.
- *Does 145 survive without `flash_save`?* Unanswerable: there is nothing to
  persist. It stays open for a firmware that implements the parameter.
- *Does the device's TLS stack exist at all?* The parameter's description string
  (`"TLS mode: 0=disabled, 1=TLS 1.3"`) is in the vendor's **server** binary,
  not in this firmware. Whether 7.4.4407 contains a TLS implementation that is
  merely unreachable, or no implementation, would need the firmware image —
  which is encrypted (D3).

The only route left to the device half is a firmware with 145 in it. The
server half is done and will work the moment such a device appears: it is the
same six-byte test the vendor's own gateway applies.

> The "one-way door" warning is retired for this firmware. Keep it for any
> device that *does* answer a read of 145 — against such a device the order is
> still certificate first, parameter second.

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

### A11. `display_out_of_sync` false-alarms on every push — **FIXED** (2026-10-05)

A push legitimately leaves the device out of sync until it draws the frame and
reports the new `DisplayStateCRC`. Measured on this sign at **10–48 s**,
depending on where the push lands relative to the heartbeat. The entity carried
`BinarySensorDeviceClass.PROBLEM`, so Home Assistant raised a problem indicator
after every normal update. The library's tri-state was right; the presentation
was not.

**The fix is a fourth state, not a wider timeout.** `DeviceState.sync_status()`
returns `UNKNOWN` / `IN_SYNC` / `CONVERGING` / `DIVERGED`, and only `DIVERGED`
turns the sensor on. A mismatch is `DIVERGED` when **either**:

* the device has been in touch `CONVERGENCE_CONTACTS` (2) times since the push
  **and** `settle_time()` has passed — one full announced interval plus a
  60 s draw allowance, so those contacts could actually have carried the news;
* or `convergence_grace()` has passed — `contacts × interval + 60 s` — which
  catches a sign that stopped calling altogether.

Both windows come from **`NextStatus`** (status tag 27), the device's own
announcement of when it will next be in touch, rather than a flat timeout. A
sign on an hourly heartbeat is therefore not called broken for the 59 minutes it
is legitimately away.

#### The thing that only hardware told us

The first cut used the contact counter alone, on the reasoning that "the device
has had a chance to report" needs no clock. Live, that is wrong: **the sign
bursts status packets around a draw.** Measured on 2026-10-05 with a one-minute
announced heartbeat, contacts after a push went

```
0 -> 9 in the first 55 s,   then ~1 per minute
```

so two contacts can elapse in fifteen seconds — before a 1.84 MB frame has even
finished transferring. The counter is now gated on `settle_time()` for exactly
this reason, and `tests/test_sync_status.py` pins the case.

#### Verified on the live sign

Push, then sample every 5 s for 260 s
(`tmp/visionect/agent-fixes/watch.py`):

```
03:36:00  out_of_sync=off  sync=converging  match=False  pushed=2023074219 device=2881787238
03:36:10  out_of_sync=off  sync=in_sync     match=True   pushed=2023074219 device=2023074219
... 250 s, never on
```

`binary_sensor.display_out_of_sync` **never left `off`**, including the ~10 s
where the checksums genuinely differed. Recorder history over an earlier push
agrees: one state row for the whole period, no transitions.

For the genuine-desync half, the sign was left holding a frame whose checksum
the integration no longer believed (a bogus `pushed_checksum` injected into
`.storage/visionect.runtime`, with the content source pointed at an unreachable
URL so the automatic re-assert could not repair it):

```
03:43:42  out_of_sync=on   sync=diverged    match=False  pushed=123456789 device=2023074219
... held on for 4 minutes
03:47:50  out_of_sync=off  sync=in_sync     match=True   pushed=2023074219 device=2023074219   (after a repair push)
```

Note `checksum_override` is **not** a way to force a desync, contrary to the
obvious reading: `send_image_packet` records the override as `pushed_checksum`
and the device echoes the same value back, so the two agree. It is the
full-redraw lever, not a desync lever.

#### `pending_changes` and `display_out_of_sync` now agree

They visibly contradicted each other during the window, which is how this was
found. `_pending_attrs` now carries `sync_status` alongside the raw `in_sync`,
and both read the same verdict. The live trace above shows them in step
throughout, including `pend.sync=converging` while `out_of_sync=off`.

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

### B7. Which `.pv2` index is the live framebuffer? — **ANSWERED: none of them** (2026-10-05)

The question was malformed. `/image0.pv2` … `/image5.pv2` are **not
framebuffers**; they are Visionect's shipped demo screens, written at the
factory and never touched again.

Evidence, all from the live sign:

* **A push changes none of them.** Listed before a distinctive full-screen push
  and again after the device had echoed the new `DisplayStateCRC`: all six sizes
  identical to the byte, and `/image0.pv2`'s first 16 bytes unchanged.
* **They carry `ImageHeader.Checksum == 0`** in every file, so the
  identification shortcut this entry proposed — match the stored checksum
  against `DeviceState.pushed_checksum` — cannot work at all. Nothing we push
  ever has a zero checksum; the vendor remaps a computed 0 to 1.
* **`DataHeader.DeviceID` is sixteen zero bytes**, where any frame we send
  carries the sign's real UUID.
* **Two were pulled whole and rendered**: `/image0.pv2` is a wayfinding board
  ("Welcome to Nanotech inc.", a room directory) and `/image4.pv2` is a museum
  label ("Room 35 — Michelangelo and the Florentines"). Both are full-canvas
  4 bpp frames, 2 × 2880×640, `ProtocolHeader.Version` **2**.

So there is no device-side readback of the live frame on firmware 7.4.4407, and
`DisplayStateCRC` remains the only way to ask the sign what it is showing — which
is enough, and free. `/image1`, `/image2`, `/image3` and `/image5` were left
unread; at ~2.3 KiB/s their 199 KB–1.2 MB would have been another 20 minutes of
chat for no new information.

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

### C4. Full-pcap replay tests skip in CI — CLOSED, working as intended

Not a gap. These two tests exist to re-verify the *extraction* against the
**original, unmodified** capture, including the whole 1.84 MB image push. That
purpose requires the original bytes, and the original bytes contain the owner's
device UUID, WiFi SSID, BSSID and internal hostname -- which is exactly why the
committed fixture is scrubbed.

Bundling a scrubbed pcap would make the test verify nothing: it would be
checking the extractor against bytes the extractor's own substitution rules
produced. The ~90 KB fixture already covers the same decode/re-encode ground in
CI; this pair is a local-only check that the fixture was derived faithfully.

Correct behaviour is therefore: run when `PYVISIONECT_PCAP` (or the default
path) points at a real capture, skip otherwise. Leave as is.

### D1. Home Assistant integration — BUILT, running
Implemented and running against the live sign. Lives outside this repo (the
config dir of the test rig, archived under `artifacts/visionect/ha-integration/`).

Fixed 2026-10-05 alongside A11 and D2: **the actions took `device_id` only**, so
the obvious first call — naming the image entity you can actually see — came
back as a bare `400`. They now accept `entity_id`, `area_id`, `floor_id`,
`label_id` and a nested `target:` mapping (which is what the REST API passes
through verbatim, and was the specific shape that produced the unexplained 400).
`services.yaml` offers both a device and an entity target on every action so the
UI picker does too, and "you did not say which sign" is now a schema failure —
a 400 **with the message** — rather than a `ServiceValidationError`, which the
REST API renders as a bare 500.

### D2. Device file readback as a feature — **DONE** (2026-10-05)

Built, and pointed at what the device actually holds rather than at the
framebuffer it turns out not to keep (see B7).

Library:

* `packets/stored.py` — `parse_stored_frame_header()` reads a `.pv2`'s whole
  shape out of its first 44 bytes; `parse_stored_frame()` decodes the file into
  an `ImagePacket`. `StoredFrame.looks_like_our_push` is the zero-checksum /
  zero-UUID test that tells a factory file from one of ours.
* `imaging/decode.py` — `decode_image_packet()`, the inverse of `encode_frame`,
  through the interlaced fold and the `eink-flip` mirror. Gated on the golden
  capture: encode the canvas, decode the result, get the canvas back bit for
  bit, and the same `ImageHeader.Checksum` (3741864387).
* `session/filetransfer.py` — `FileRead`, the sans-io `open → read* → close`
  sequencer.
* `io/tcp.py` — `VisionectServer.read_device_file()` / `list_device_files()`,
  plus `add_listener()` so a transfer can watch events without the application
  routing for it.

Integration: `visionect.read_device_file` (returns the headers and the measured
rate; populates a diagnostic `image.*_device_file` entity, disabled by default)
and `visionect.list_device_files`, which now actually works.

#### Three device facts the API exposes rather than hides

| | measured |
|---|---|
| reply size cap | **1024 bytes**, whatever you ask for — 32768 returns 1024 |
| seek | **none**; `open` rewinds to 0 and nothing else moves the cursor |
| throughput | **~2.3 KiB/s**, ~420 ms per round trip |

So one of this sign's stored frames is **1–9 minutes**, and a lost reply costs
the whole transfer — `read_device_file(attempts=…)` restarts from zero because
that is the only recovery that exists. Live: `/image0.pv2`, 134409 bytes,
**56.4 s at 2.33 KiB/s**, through the Home Assistant service, decoded, and the
PNG the entity serves is byte-identical to an offline decode of the same bytes.

#### Two bugs this found in the existing file code

Both made the file protocol silently return nothing, and both had been
code-read rather than measured:

1. **Every reply the device sends is `FileOperation.EVENT` (6), not `READ` (2).**
   `_DECODERS` had no entry for `event`, so `FilePacket.parsed()` returned
   `None` for every reply the device has ever sent and the integration's
   `list_device_files` always reported an empty filesystem.
2. **The listing columns are `name checksum size`, not `name size checksum`.**
   Read the old way, every file is zero bytes long. The order is pinned by the
   files themselves: `/image0.pv2` lists as 134409 and its own
   `ProtocolHeader.Length` is 134389 = 134409 − 20.

A third, smaller: `PendingWork`'s `framebuffer_read` slot went through
`apply_pending`, which can only emit **one packet per slot** — so it sent the
opening `open` and nothing ever read. The integration now drives file work as a
conversation instead.

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

### E2. The firewall rule is temporary — OPEN, and the failure mode is now confirmed
Port 11113 was opened with `nixos-firewall-tool`, which does not survive a
reboot, while E1 *does*. If the host reboots before E1 is restored, the sign has
nowhere to report — likely the reported beeping. Either restore E1 or make the
rule declarative.

**Observed live on 2026-10-05**, incidentally, while running A3: with nothing
listening on `10.42.0.50:11113` the sign logs
`E: Opening TCP socket` / `E: TCP connection Error: -111` in a retry loop,
`NETWORK_ERROR_COUNT` climbs, and **the firmware reboots itself** — stating the
reason outright:

```
E: Max conn errs. Reboot
```

So the consequence of E1+E2 drifting apart is not just "no updates": it is a
sign that power-cycles itself every few hours. Two reboots were seen in about
40 minutes. `ERROR_CODE` stayed `0x0` throughout, so **the status packet does
not report this** — the only indication is the serial log at level 2 or above,
which is an argument for not running the console at `vlog_unify_levels 1` (A3).

### E3. The listener is a user unit, not declarative — OPEN
`~/.config/systemd/user/pyvisionect-listener.service` with lingering enabled, run
from a venv outside the repo. Survives reboot, but is not in the NixOS config.
