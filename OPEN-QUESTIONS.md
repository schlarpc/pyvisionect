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

### A2. Can an SSID contain spaces? — **ANSWERED: yes. `wifi_ssid_set` takes the rest of the line verbatim** (2026-10-05)

**Yes, and there is no quoting convention at all.** `wifi_ssid_set` consumes
everything after the single space that separates it from its argument, byte for
byte, and writes that to TCLV 65. Interior spaces, runs of spaces, a leading
space, a trailing space, apostrophes, double quotes, single quotes, backslashes
and percent signs all land **literally**. So a spaced SSID works, and *any*
attempt to quote or escape it corrupts it — the quotes become part of the SSID.

Measured on the live sign over `/dev/ttyUSB0`, firmware 7.4.4407, CLI 1.2.
Every write was RAM-only; `flash_save` was never sent. Starting config was
`SSID: Spaceless / Security: wpa2 / Band: 0`, `Conn: tcp open`, and it was
restored and verified at the end of both runs. 16 forms were tried, each one
followed immediately by `wifi_conf_get`, so the column below is what the
*device* read back, not what the setter claimed.

| sent after `wifi_ssid_set ` | reply | SSID read back |
|---|---|---|
| `Oneword` | `WiFi SSID set` | `Oneword` |
| `Don't` | `WiFi SSID set` | `Don't` |
| `"Quoted"` | `WiFi SSID set` | `"Quoted"` |
| `'Quoted'` | `WiFi SSID set` | `'Quoted'` |
| `Back\slash` | `WiFi SSID set` | `Back\slash` |
| `Two Words` | `WiFi SSID set` | `Two Words` |
| `Two  Words` (two spaces) | `WiFi SSID set` | `Two  Words` (two spaces) |
| `Three Little Words` | `WiFi SSID set` | `Three Little Words` |
| `"Two Words"` | `WiFi SSID set` | `"Two Words"` |
| `'Two Words'` | `WiFi SSID set` | `'Two Words'` |
| `Two\ Words` | `WiFi SSID set` | `Two\ Words` |
| `Two<TAB>Words` | *(see below)* | `TwoWords` |
| `Two%20Words` | `WiFi SSID set` | `Two%20Words` |
| `McDonald's Free WiFi` | `WiFi SSID set` | `McDonald's Free WiFi` |
| `"McDonald's Free WiFi"` | `WiFi SSID set` | `"McDonald's Free WiFi"` |
| `McDonald\'s\ Free\ WiFi` | `WiFi SSID set` | `McDonald\'s\ Free\ WiFi` |

A second run added the whitespace edge cases and a reproduction:

| sent after `wifi_ssid_set ` | SSID read back (raw frame) |
|---|---|
| ` Two Words` (extra leading space) | `SSID:  Two Words` — the extra space is **kept** |
| `Two Words ` (trailing space) | `SSID: Two Words ` — the trailing space is **kept** |
| 31 chars, no space | all 31 |
| `Twelve Chars Plus More Padding X` (exactly 32) | all 32 |
| `McDonald's Free WiFi` again | `McDonald's Free WiFi` — reproduces |

#### Why this is "rest of the line", not "argv[1]"

Three observations rule out tokenise-then-take-the-first-token, and also rule
out tokenise-then-rejoin-with-single-spaces:

* **A run of two spaces survives as two spaces.** An argv rejoin would collapse
  it.
* **A leading space survives.** The dispatcher consumes the command name and
  exactly one separator, then stops looking.
* **A trailing space survives**, visible in the raw frame as
  `SSID: Two Words \r\n`.

That last one is easy to miss, because `SerialConsole` strips each reply line
before putting it in `CommandResult.lines`. The trailing space is only in
`CommandResult.raw`. Anyone re-running this must read the raw frame, or they
will conclude the firmware trims and be wrong.

#### TAB is the line editor's help key, not an argument character

`wifi_ssid_set Two<TAB>Words` is the one form that did something unexpected.
The raw frame:

```
wifi_ssid_set Two\r\r\nwifi_ssid_set <ssid>: Set WiFi SSID\r\r\n> wifi_ssid_set TwoWords\r\nWiFi SSID set\r\n>
```

TAB was swallowed by the console's own line editing, which printed the matched
command's usage line and redisplayed the buffer; the remaining `Words` was
appended to `Two`, and the SSID that landed was `TwoWords`. So **TAB cannot be
put into an SSID over this console**, and as a bonus the firmware has a
usage-lookup key that prints a hidden command's syntax — `wifi_ssid_set <ssid>`,
straight from the device, which is independent corroboration of the arity A1
inferred from the vendor page.

#### What changed in the library

`plan_wifi` and `Sign.set_wifi` no longer refuse a spaced SSID. When the SSID
contains whitespace they emit the three single-argument setters instead of
`wifi_conf_set`:

```
wifi_psk_set <psk>
wifi_security_set <security>
wifi_ssid_set <ssid>
```

SSID last, so the field that decides association is the final write. Two
consequences worth knowing:

* **Band (TCLV 68) cannot be written on this route.** There is no
  `wifi_band_set` — not in `help`, not in the vendor's 162 documented commands.
  `wifi_conf_set` is the only writer, and it is the one command that cannot
  carry the space. So a spaced SSID with a non-default `band` is still refused,
  and with `band=0` the plan carries a note that TCLV 68 is left as it is.
* **`help` still does not list `wifi_ssid_set`**, so `Sign._run`'s post-`refresh_commands`
  gate would refuse it. The spaced-SSID path passes `check=False`, and
  `Plan.execute` exempts the names in `HIDDEN_IN_7_4_4407` from its
  missing-command check. The gate is unchanged for everything else: `help` is
  still the only evidence on firmware this library has not seen.

#### What this does *not* settle: the PSK — **OPEN**

The old refusal said "the CLI is whitespace-delimited and cannot carry a
space". That reason is now known to be **wrong** for single-argument commands,
and `wifi_psk_set` is a single-argument command of exactly the same shape, so it
very probably carries a space too. It is still refused, for a different and
better reason: **there is no read path for TCLV 67.** `wifi_conf_get` returns
SSID, security and band and never the passphrase, so there is no way to check
what landed. The SSID experiment above was safe precisely because every write
was read straight back; the PSK has no such oracle, and a silently truncated
passphrase does not fail loudly — it just stops the sign associating, which on
this firmware means `E: Max conn errs. Reboot` with `ErrorCode` still `0x0`.

Settling it needs an AP whose passphrase you control, set to something with a
space, and a willingness to force a re-association (`cs 1`, `cs 3`) and watch
whether it comes back. That is an afternoon with a spare AP, not a console
session on a sign someone is using.

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

### A6. `sf_rdid` / `sf_rdst` crash the CLI task — CLOSED, not worth testing
Both assert (`spi_flash_cli.c:139`/`:188`) and kill `usb_cli_task`; the watchdog
notices and does nothing, then forces a reset ~25 min later. They read a
*selected* device without checking `sf_select` ran, so plausibly `sf_select`
first makes them safe.

**Deliberately not tested, and the hypothesis is left unproven.** The cost of
being wrong is a crashed console and a device reboot ~25 minutes later; the
prize is the JEDEC id and status register of the SPI flash holding the
filesystem. We have no use for either -- that flash carries the factory demo
images (see D2/B7), not firmware, and `fs_stats` already reports the capacity
figures that matter. Testing a known-crashing pair of commands to learn a chip
id nobody needs is a bad trade on a sign that lives in a kitchen.

Both stay gated behind `i_really_mean_it`, with the assert sites named in the
docstring so the next person can make their own call. If someone does try it,
`sf_list` first is safe and tells you what there is to select.

### A7. `play_music` — ANSWERED, and it kills the USB console

It plays **the Indiana Jones theme** (confirmed audibly by the owner).

**It also appears to kill `usb_cli_task`**, exactly like `sf_rdid`/`sf_rdst` (A6).
Observed: the command produced **no echo and no reply at all**, and every
subsequent command -- `cli_version_get`, `conn_state_get`, `server_tcp_get`, even
a bare newline -- returned empty. Meanwhile the **device itself stayed perfectly
healthy**: still connected to Home Assistant, heartbeating on the 60 s cadence,
pinging normally. That asymmetry (console dead, device alive) is the A6
signature, and it implies the same consequence: the watchdog logs the task
timeout, does nothing, then forces a full reset roughly 25 minutes later.

[INFERENCE] The crash is attributed to `play_music` because the console was
responsive immediately before and dead immediately after, with nothing else
issued in between. Not re-tested -- deliberately, since reproducing it costs
another device reboot to learn nothing new.

So `play_music` belongs with `sf_rdid`/`sf_rdst` in the
**"present, documented, and will crash your console"** set, not in the harmless
curiosities. It should be gated behind `i_really_mean_it` alongside them.

It does **not** explain the beeping-with-no-server report: that is better
accounted for by `E: Max conn errs. Reboot` (see E2), where a sign that
power-cycles also re-runs whatever it plays at boot.

### A10. Do rectangle (partial) updates actually work on this panel? — **ANSWERED: yes, and now SHIPPED** (2026-10-05)

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

#### What shipped

`pyvisionect.imaging.partial`, plus the primitives underneath it. Opt-in, off by
default, and `encode_frame` with no `partial=` argument is byte-for-byte what it
was.

```python
from pyvisionect.imaging import DirtyTracker, PartialPolicy, Dithering

tracker = DirtyTracker(panel=panel, encoding=4, dithering=Dithering.BLUE_NOISE,
                       policy=PartialPolicy(max_consecutive_partials=10))
frame = tracker.update(img)       # full screen first, then partials
if frame.rectangles:
    conn.send_image(frame)
```

* **`encode_partial_frame(...)`** — stateless: canvas dirty rects (detected or
  given) → per band → lane columns → snapped to the 4-px interleave group →
  screen rects, cut from the *new* state image for **both** lanes, mirrored,
  packed, interleaved. `encode_frame(..., partial=True)` delegates to it.
* **`PartialPolicy`** — the ghosting budget (default 10, the vendor's
  `noFullUpdateMax`) and a cost gate at half a full push.
* **`DirtyTracker`** — holds the `FrameState` and the consecutive counter, with
  `rollback()` for a push that did not go out and `reset()` to drop the state.
* **`interlace.lane_span` / `screen_span` / `lane_of_display` /
  `displays_of_screen`** — the fold restricted to a sub-range. It restricts
  cleanly because it is a pure column permutation.
* **`Panel.screen_width` / `screen_height` / `screens` /
  `supports_screen_rectangles`**, and bounds checking that **refuses** an
  out-of-bounds rectangle rather than clamping it.
* **`DeviceState.accepts_screen_rectangles`**, backed by
  `SCREEN_RECTANGLE_VERIFIED_HARDWARE`.

`supports_rectangles` was left `False` **on purpose**, and the reasoning is
worth keeping: it is a correct statement about `getRectangleSupport`, half the
library's prose cites it as such, and flipping it would make code that merely
asked for a frame start emitting partials. The contrary evidence is a different
proposition — "we measured one device taking one" — so it got its own name. The
new set is a record of an experiment, not a capability a device advertises;
there is no way to ask a device this, so the only honest way to add an id is to
point one at a `DirtyTracker` and watch the glass.

The hardware run also settled what A10 had left open:

| was unexercised | now |
|---|---|
| only `ScreenID 0` | **both.** `l: (1 1168 80 512 128)` acked and drawn; band 3 and band 2 both reached |
| one rectangle per packet | **three in one packet**, mixed geometry, all drawn |
| a rect straddling a band seam | **two rects, one packet**, band-local `y=580` and `y=4` |
| partner lane held (one offline check) | **verified on glass**: a partial on band 2 whose partner rows land inside an existing black bar on band 3 left the bar perfectly intact |
| out-of-bounds rectangles | **still unprobed, now unreachable** — the library refuses them |

Ten pushes, ten acks, zero NACKs on the shipped encoder, `ErrorCode 0`
throughout, and the device's echoed `DisplayStateCRC` equal to our computed
state checksum on every push that got as far as the next heartbeat.

#### The correction: a partial may not also ask for the clearing waveform

**This cost a NACK to find and it contradicts the first run.** With
`RectangleHeader.Options` bit `0x0002` *clear* — the shipped default, which B1
established means "inverse update" — the firmware parses the rectangle, echoes
the header back, and then refuses it:

```
l: (0 1168 160 512 128), enc: 0x4 pde: 0x0, te: 0x0
u: (0 1168 160 512 128), wfn: 2, dum: 1, inv: 1
Image download completed (0 0)
Skip image update: partial image not allowed
                      -> NACK, ErrorCode 0x06000000, EpdUpd=0, ImgId=0x00000000
```

The identical rectangle with bit `0x0002` **set** logs `inv: 0`, acks, and draws
in 1723 ms. It is a sensible rule: an inverse clearing refresh drives the whole
panel, so "clear only this rectangle" is not expressible.

The first A10 run recorded partials being accepted with the bit clear, and its
own quoted console output shows `inv: 1` on an accepted strip. That is **not
reproducible** — the same wire shape now gets refused every time. Something
about the device's state differed; the sign spent the hours in between pointed
back at VSS for B1, so a parameter or a mode may have changed. Unresolved, and
not worth another probe: the rule as it stands is unambiguous and the library
obeys it.

So `encode_partial_frame` sets the bit on every partial rectangle regardless of
`rect_options`, and leaves `rect_options` untouched on the full-screen fallback
— which is exactly where the clearing refresh belongs, and is what makes the
ghosting budget worth spending.

#### The payoff, re-measured against the shipped encoder

| push | screen rect | `Pv2Len` | raw | `EpdUpd` | total |
|---|---|---:|---:|---:|---:|
| full screen, dense grid | 2 x `2880x640` | 144 976 | 1 843 200 | 5 298 ms | 6 287 ms |
| full screen, typical content | 2 x `2880x640` | 83 729 | 1 843 200 | 2 916 ms | 3 618 ms |
| `1232x200` canvas block | `2464x200` | 21 856 | 246 400 | 1 693 ms | 1 860 ms |
| `256x128` canvas block | `512x128` | 1 590 | 32 768 | 1 723 ms | 1 807 ms |
| 3 rects, two bands | 3 rects | 3 355 | 36 800 | 1 819 ms | 1 911 ms |
| `200x100` canvas block | `400x100` | **342** | 20 000 | 1 690 ms | **1 770 ms** |

**~245x on the wire** for a realistic small change, which is the number to design
to. Encode cost falls as well, measured offline on the real captured dashboard
canvas with a `300x120` change: a full blue-noise encode is 34.3 ms, the
change-detected partial 9.3 ms, and the partial with `rects=` supplied
**1.6 ms**. Ordered dithers are phased on the canvas coordinate, so the partial
quantises its dirty slice with that slice's origin and gets byte-identical
pixels while touching 1/100th of the canvas; what remains is change detection,
which runs over the whole canvas whatever the change. The panel time moves as well — ~1.7 s partial against 2.9–5.3 s full — but
A10's original "flat ~2.9 s regardless of area" reading needs a correction too:
most of that gap is the **clearing waveform**, not the area. A partial is
forbidden from requesting one, a full push with `RectangleFlags = 0` always
does. `UPD_FULL` (7 passes, 5.3 s) versus `UPD_FULL_AREA` (2–4 passes, 2.9 s) on
a full push is the device's own periodic decision and was seen going both ways in
one session with identical `inv: 1` headers. Still: do not sell this as faster
refreshes. The refresh a partial skips is the clearing refresh you have to take
periodically anyway.

#### The ghosting policy, fired on hardware

`PartialPolicy(max_consecutive_partials=6)` for the test. Pushes 1–6 after the
baseline went out as partials, the counter reaching 6; push 7 came back
`FULL (ghosting-refresh-due)`, `n=2`, `2880x640` each, the console logged
`inv: 1` on both rectangles, and the counter reset to 0. The default is the
vendor's 10. Note the firmware enforces no cap of its own — twelve back-to-back
partials drew fine in the first run — so this is entirely our policy, and a
tracker configured never to refresh will slowly turn the panel to mush.

The cost gate fired on hardware too: pushing a whole new image through
`tracker.update()` came back `FULL (not-worth-it)` rather than as a pile of
rectangles covering the screen twice over.

#### What the waveform console said, push by push — including a clean negative

The serial console was watched for `wfn:` / `inv:` / `UPD_*` on every push of the
validation run, specifically to see whether the firmware ever clears ghosting on
its own initiative. Correlated per push:

| time | push | `wfn` | `inv` | `Force Inverse` line | resulting updates |
|---|---|---|---|---|---|
| 10:37 | full, 2 rects | 2 | 1 | **yes** | 8x `UPD_FULL` |
| 10:42 | full, 2 rects | 2 | 1 | **yes** | 8x `UPD_FULL` |
| 10:43 | partial, 1 rect | 2 | 0 | no | 1x `UPD_FULL_AREA` |
| 10:46 | full, 2 rects | 2 | 1 | **yes** | 8x `UPD_FULL` |
| 10:47 | partial, 1 | 2 | 0 | no | 1x `UPD_FULL_AREA` |
| 10:49 | partial, 1 (screen 1) | 2 | 0 | no | 1x `UPD_FULL_AREA` |
| 10:51 | partial, **3 rects** | 2 | 0 | no | 3x `UPD_FULL_AREA` |
| 10:52 | partial, 2 (seam) | 2 | 0 | no | 2x `UPD_FULL_AREA` |
| 10:54 | partial, 1 | 2 | 0 | no | 1x `UPD_FULL_AREA` |
| 10:54 | partial, 1 | 2 | 0 | no | 1x `UPD_FULL_AREA` |
| 10:56 | full (forced by policy) | 2 | 1 | **no** | 4x `UPD_FULL_AREA` |
| 10:58 | partial, 1 | 2 | 0 | no | 1x `UPD_FULL_AREA` |
| 11:00 | partial, 1 | 2 | 0 | no | 1x `UPD_FULL_AREA` |
| 11:02 | full | 2 | 1 | **no** | 4x `UPD_FULL_AREA` |

**The clean negative, which is the point: no partial ever produced an `inv: 1`,
a `Force Inverse` line or a `UPD_FULL` that we did not ask for.** Eight accepted
partial pushes, twelve rectangles, both `ScreenID`s: every one `inv: 0` →
exactly one `UPD_FULL_AREA` per rectangle. The firmware initiated **no**
clearing refresh of its own across the run. So the forced-full policy is
**necessary, not precautionary** — nothing else is going to clear the panel. It
does not prove the firmware never self-manages (the run is ~25 minutes), but it
removes "the firmware probably handles it" as a reason to skip the policy.

**And a new one, unexplained: `inv: 1` is a request the firmware sometimes
declines.** Three full-screen pushes carried byte-identical headers with the bit
clear, and the console parsed `inv: 1` on all of them — but only the first three
emitted `Force Inverse and full area update` and ran `UPD_FULL` (8 passes,
5298 ms). The last two logged no such line and ran `UPD_FULL_AREA` (4 passes,
2916 ms). `ImgOpt` tracks the split (`0x00003024` when granted, `0x00001124`
when not) but the bits beyond `0x0002` are not decoded and nothing is inferred
from them here.

Interval does not explain it: 10:46 was granted 4 minutes after 10:42, and 10:56
was declined 10 minutes after 10:46. A10's first run saw the same shape and read
it as "the first push after a long idle" — that does not fit either, since 10:42
and 10:46 were both minutes after the previous push. **Recorded as unexplained.**
What it means for us is worth stating plainly: a forced full-screen push
*requests* the clearing refresh and does not guarantee one, so the conservative
default (10) earns its keep, and anyone who raises it is betting on a lever that
is only sometimes pulled.

`wfn: 2` on all 23 lines this session — partial and full, both screens, 4 bpp.
It remains the **only** waveform number ever observed on this panel, nothing is
known about what selects it, and **no waveform-selection API is exposed**; the
panel reports a waveform file `WF=31.2_C296`, so a table exists that we have
never touched.

#### Still unknown

* **Out-of-bounds rectangles were deliberately not probed** (`y+h > 640`,
  `x+w > 2880`). Whether the firmware clamps, refuses, or writes past its
  framebuffer is unmeasured. The library now validates against the panel bounds
  and raises rather than sending, so this stays closed by construction.
* **Why the first run's partials were accepted with `inv: 1`.** See above.
* **Ghosting over hundreds of partials is not characterised.** Twelve were clean
  in the first run, eight in the second; the policy exists because nobody has
  run the long experiment.
* **Why the firmware grants `Force Inverse` on some full pushes and not others.**
  See the table above. Until that is understood, "force a full screen" should be
  read as "ask for a clearing refresh", not "get one".
* **Waveform selection.** `wfn: 2` is the only value ever seen, across every
  capture. Whether the server can select another, and what the panel's
  `WF=31.2_C296` table holds, is completely unexplored. No API for it.
* **Interlacing mode 1** (hardware revision 1.0.0) has no lane map, so
  `supports_screen_rectangles` is False for it and both paths refuse. B3.
* **Floyd-Steinberg partials seam.** The dither is not pixel-local, so a partial
  re-dithers only its own band slice and its pixels differ from a full push's.
  The partial stays self-consistent, which is all the device checks. Ordered and
  blue-noise dithers are phased on the canvas and do not seam.

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

### A12. Waveform selection and ghost management are barely explored — OPEN

We have driven this panel hundreds of times and seen **exactly one waveform**.
Everything observed, across every capture:

```
UPD_FULL        50x
UPD_FULL_AREA   14x
wfn: 2          26x     <- the only waveform number ever seen
inv: 1 / inv: 0 14x each
ImgOpt          0x00003024 (inverse) / 0x00000124 (non-inverse)
```

E-ink controllers normally expose several waveforms -- a slow clearing refresh
plus faster non-flashing modes for text -- and the device reports `WF=31.2_C296`
as its waveform file, so a table exists. We do not know what selects `wfn`,
whether the server can influence it at all, or what the other values are.

**What is actually verified:** clearing is driven by `RectangleHeader.Options`
bit `0x0002`; with the shipped `RectangleFlags = 0` the device performs an
inverse clearing refresh on **every** full-screen push (B1, measured). That is
the only ghost-clearing lever we have evidence for, and it is why the partial
tracker forces a periodic full push.

**What is assumed and unverified:** that newer firmware self-manages ghosting.
The server's 120 s idle re-render ticker is armed *only for old firmware*, which
implies the vendor believed so -- but that is the vendor's assumption, not a
measurement. A10 saw 12 consecutive partials with no self-initiated refresh and
no visible ghosting: suggestive, small sample.

**Unexplored levers.** `ImgOpt`'s other bits (`0x3000` vs `0x0100` differ beyond
the inverse bit). And two serial commands never run, both on the do-not-run list
and both display-driver control: **`dcmh <mode>`** ("Runs EPD pre/post-update
hook") and **`dcmc <color>`** ("Clears the display to a specified color"). The
`dcm*` family also includes power-down/sleep/wake for the driver. These are the
most direct route to the panel's own refresh machinery and the most likely to
leave the display in a bad state, so they want a deliberate session with the
owner present, not an autonomous probe.

**Worth doing when someone is watching:** a long partial run (hundreds, not 12)
with the serial console logging `UPD_*`/`wfn:`/`inv:` per push, to find out
whether the firmware ever clears on its own and how much ghosting actually
accumulates. That would turn the forced-full threshold from a precaution into a
measured number.

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

### B1. Inverse update: the firmware's reaction — **ANSWERED: the firmware inverts, and a default deployment inverts on *every* push** (2026-10-05)

`RectangleHeader.Options` bit `0x0002` is read by the firmware, and **clear means
"use the inverse / clearing waveform"**. Settled by an A/B on the live sign with
the sign temporarily pointed back at VSS, driving VSS through its REST API and
watching the wire and the serial console (`vlog_unify_levels 5`) at the same time:

| session `Options["RectangleFlags"]` | on the wire | firmware narration |
|---|---|---|
| absent — the shipped default | `Rectangle(..., options=0, update_options=258)` | `u: (0 0 0 2880 640), wfn: 2, dum: 1, inv: 1` → `Force Inverse and full area update` → `border (0 1)` → `UPD_FULL` |
| `"2"` | `options=2` | `u: (0 0 0 2880 640), wfn: 2, dum: 1, inv: 0` — no `Force Inverse` line — → `border (0 1)` → `UPD_FULL_AREA` |

The firmware's own profiling line agrees: `ImgOpt=0x00003024` with the bit clear
against `ImgOpt=0x00000124` with it set.

**And the premise of this entry was backwards.** The admin UI's per-session
**"Inverse updates"** selector writes exactly this key (`data/admin/all.js`,
`<select name="sessionInverseUpdates">`): `1` = "Server default" (deletes the
key), **`0` = "Enable"**, **`2` = "Disable"**. So `RectangleFlags = 0` does not
mean "this deployment never signals an inverse update" — it means inverse updates
are *on*: the bit is clear on **every** full-screen push, and this sign has been
running a clearing waveform on every redraw all along. The engine's one-shot
`nextFullScreenIsInverseUpdate` (`image-state.go:1519-1522`) only clears a bit
that is already clear here, which is exactly why it was invisible.

Two notes for anyone repeating it:

* A forced re-render needs a *content* change, not just
  `POST /api/session/restart`. With identical pixels and a matching
  `DisplayStateCRC` the engine sends nothing at all. Flipping
  `Options["DefaultDithering"]` between `none` and `floyd-steinberg` guarantees a
  full-screen push and is trivially reversible.
* `RectangleFlags` is a **session** option (`render-settings.go:84`), not a device
  one, and it takes effect on session restart.

Not reached: the one-shot path itself. `reRenderTimeoutStart` is only armed for
firmware *older* than the engine's constant, and never for 7.4.4407. Moot — the
bit's meaning was the question, and the bit is the whole signal.

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

### B5. Remaining `[GAP]`s — **ONE ANSWERED, TWO UNREACHED** (2026-10-05)

**The status sentinel's value: ANSWERED. It is a CRC-32.** The value carried by
tag `0xFFFFFFFF` is the CRC-32/ISO-HDLC (the zlib/PNG one: reflected, init
`0xFFFFFFFF`, final XOR `0xFFFFFFFF`, i.e. plain `zlib.crc32`) of **every record
byte that precedes it**:

```python
zlib.crc32(status_payload[:-8]) == struct.unpack_from("<I", status_payload, -4)[0]
```

16 of 16 live status frames, across three separate TCP connections and three
connect reasons (5 heartbeat, 2 wakeup, 9 by-server-request — the last two also confirmed
live for the first time here). So it is a checksum over the records, not a nonce,
and a receiver can validate a status packet rather than trusting it.

**Why the golden fixture cannot confirm this — and why the library documents it
instead of enforcing it.** All 26 status frames in `tests/fixtures/golden.json.gz`
fail the check, and they fail it by a *constant* `0xe63fe9a0`. That is the
signature of a scrub, not of a different algorithm: the fixture is scrubbed
(`DataHeader.DeviceID` is the `00112233-4455-…` placeholder and the `BSSID`
records read `00:00:5E:00:53:00`), `BSSID` lives *inside* the CRC'd region, and
substituting the same bytes in every frame of a fixed-length region shifts every
CRC by the same amount. Solving for a seed confirms it: `zlib.crc32(body,
0x613fa514)` matches all 26. So anything that validated this checksum would reject
our own test fixtures, and any future scrub has to recompute it.

**`packet.Priority`: UNREACHED, and probably unreachable from the server side.**
`priority` and `reserved` were `0` in all **69** frames of this capture as well
— now across packet types 1, 2, 3, 5 and 8 in *both* directions, and specifically
including the eleven frames generated by the gateway's inactivity watchdog and by
the sleep manager, which are the newest emitters we had never previously seen run. No
emitter writes the field and nothing in the REST or RPC surface sets it; a
non-zero value would have to come from hardware or a server build we do not have.

**The GPS coordinate string framing: UNREACHED, and not reachable on this
hardware.** `packet.GPS` is device→server and this sign has no GPS module, so it
will never send one. The server-side counterpart is a *command*, not a GPS packet:
`networkmanager vss/pkg/command/stdcmd.(*GPS).Done` (`0xffada5`) emits
`CommandID 16` ("set gps") with a 4-byte bool, reachable only through
`PublicAPI.GPSControl` over `net/rpc` — there is no REST route — and it would not
produce a type-7 frame. Settling this needs a GPS-equipped device.

### B6. All of packet type 2 (command) is unverified — **ANSWERED: the header is real, two ids are confirmed, and the enum "disagreement" never existed** (2026-10-05)

**Captured.** Eleven type-2 frames, server→device, on port 11113 — nine `status
request` and two `sleep` — all decoded by `pyvisionect` itself with no
modification whatsoever:

```
sleep            04000000 04000000 00000000 8e030000      (16 B)
                 Type=4   PayloadLength=4  Reserved=0  payload = uint32 LE 910
status request   0b000000 00000000 00000000               (12 B)
                 Type=11  PayloadLength=0  Reserved=0  no payload
```

(The two sleeps were 910 and 907 minutes, `8e030000` and `8b030000`; the nine
status requests are byte-identical to one another.)

**So the 12-byte `CommandHeader` is real, and `Reserved` *is* emitted.** The param
packet's struct-vs-wire mismatch does not generalise to type 2. The status-request
frame proves it on its own: twelve bytes of header and nothing after them. That
was the single biggest unquantified risk in this library's API, and it is closed.

**The firmware side**, serial console at level 5:

```
sys evt pv2_command_packet_handler.c:145, Cmd Pend (2)     # sleep
System command received
Command sleep received
Wake after 910 minutes!

sys evt pv2_command_packet_handler.c:90, Cmd Pend (2)      # status request
System command received
Command status get received
sys evt vplatform_system_commands.c:171, Status Send (12)
```

Both are acked with a type-1 `Control{Flags: 1}` echoing the server's frame id,
and the status that answers id 11 carries **`ConnectReason == 8`** — confirming
live the name this library already gave it.

How they were triggered, since neither is reachable out of the box: id 11 by
adding `"StatusRequester"` to the server's `Config.Features` and setting
`Global.DeviceStatePolling` to 1 minute while raising the device's own heartbeat
above it, so the gateway's inactivity watchdog fires; id 4 by adding
`"SleepManager"` to `Features` and PUTting `Options["SleepSchedule"]` on the
device. Both config changes were reverted afterwards.

#### `reboot` vs `echo`: **0 is `echo`, 1 is `reboot`**, and the two protocol versions never disagreed

The disagreement was a transcription error in our own notes. Read byte by byte,
`proto.CommandID.String` (`bin/gateway` `0x8040a0`) and
`proto/v2/packet.CommandType.String` (`0x8912e0`) load *the same two rodata
strings in the same order* — `0x182dfdd` `"echo"` for 0, `0x18302ec` `"reboot"`
for 1 — and so does the whole remainder of both chains. Independently, the
vendor's only reboot emitter, `networkmanager main.(*Panda).Reboot`
(`0x1026420`), writes `movabs $0x400000001` into the command struct at
`0x10265e5`: `Command = 1`, `PayloadLength = 4`, payload = its bool argument.

**This was a live bug, not just a documentation defect.** `CommandType.REBOOT` was
`0` and `ECHO` was `1`, so `DeviceConnection.reboot()` would have sent `echo`, and
anyone "just testing with an echo" would have rebooted the sign.
`allow_command_packets=False` is the only reason it never fired. Fixed, with the
disassembly cited in the docstring.

#### `allow_command_packets`: leave the default alone, and for a better reason than before

Worth correcting the premise here too: the flag has always defaulted to **`True`**
(`connection.py:157`) — the library ships the *ability* to refuse type 2, not a
refusal. `test_command_packets_are_allowed_by_default` pins that on purpose.

So the question is whether to *tighten* it to `False` now, and the answer is no —
and this capture is the argument. The reason for caution was never the ids; it was
the **framing**: if type 2 were truncated like the param packet, every command the
library emitted would have been malformed. That is now measured and correct. What
remains is per-id semantics, which is a per-call concern and is where the warnings
belong (`refresh` and `clear_screen` have no emitter at all; `reboot` is a reboot).
Tightening the default would break every existing caller to express a risk that
the individual docstrings already express better.

The only change made was the error text: it no longer claims type 2 "has never been
observed on the wire", because it has.

#### Which values were *not* reached, and why

| ids | why not |
|---|---|
| 0 `echo`, 1 `reboot` | no emitter reachable without rebooting the sign, which was out of bounds for this run |
| 9 `LED window`, 10 `LDRLED`, -1 `refresh`, -2 `clear screen`, -3 `keyboard` | **no emitter anywhere in the suite.** `PublicAPI.SetLEDWindow` sends no packet at all — it only writes `Options["LEDWindow"]` |
| 2, 3, 5, 8, 16, 18 | no reachable emitter found |
| 4, 6, 7, 12, 13, 14, 15, 17, 22, -4 | reachable, but only through the one surface below |

Note 15: both `String()` chains print `unknown` for it, but the engine really does
emit it — `stdcmd.DisplayID.Done` writes `15` (immediate at `0xcfc5c5`) with a
uint32 panel-type id in 1..10, rejecting anything else as `"invalid display id"`.

#### The one surface that reaches the engine's commands

All of
`stdcmd.{Sleep,VCOM,SetHeartbeat,System,Battery,DisplayID,Frontlight,FrontlightExt}.Done`,
plus `beep` (-4) and `touchupdate` (14) built inline, are called from exactly one
function — `vss/pkg/backend/webkit.generateCommand` — which is served over a
**unix socket** (`/tmp/visionect-vss/wk-backend.ipc`). There is no TCP, REST or
RPC route to it. The only external trigger is the **`okular` JavaScript object**
injected into the page the session renders
(`vss/extension/visionect-web-extension.so`), and there is no feature gate on the
path.

So the cheapest next step for more type-2 coverage, if anyone wants it, is
`okular.TouchUpdate()` (id 14, no arguments, nothing persisted) or
`okular.Beep(1)` (id **-4** — the only way to see a *negative* command id on the
wire), run either from the rendered page or from the WebKit inspector
(`Options["EnableInspector"] = "true"`, then `/inspector/{session-uuid}/`).
`okular.SetVcom` and `okular.SetDisplayID` write panel parameters; do not.

#### Two operational facts, learned the hard way

* **`SleepSchedule = 0` does not mean "no sleep".** With `"SleepManager"` in
  `Features` and no `Options["WorkHours"]` on the device, the sleep manager read
  the `0`, computed "sleep until local midnight" and sent `sleep` with **910
  minutes**. The sign obeyed in under a second and dropped off the network.
  `Options["ScheduledWakeup"]` then read `2026-10-06 00:00:00 +0000 UTC`, which is
  itself wrong: the time is local midnight, mislabelled as UTC.
* **`app_wakeup` on the USB console wakes it anyway.** Twice, immediately, with a
  reconnect at `ConnectReason` "wakeup". "Nothing can wake a sleeping device
  early" is true of the *network* only; the serial console is the escape hatch,
  and it is the recovery procedure if anyone trips the point above.

Settled in passing: `POST /api/cmd/Status/{uuid}` and
`POST /api/cmd/StatusInit/{uuid}` both answer
`rpc error: code = Unknown desc = not implemented` on 8.5.5. `Param` is the only
working `cmdName`, so there is no direct REST route to a status request — it has
to come from the watchdog.

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
