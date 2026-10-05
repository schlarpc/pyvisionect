# pyvisionect

A clean, **sans-io** Python reimplementation of the Visionect e-ink sign wire
protocol ("protocol v3"), so you can drive a sign directly and drop the vendor
server software entirely.

Built by reverse-engineering `visionect-server-v3:8.5.5` and validated against
1.9 MB of real captured traffic from a 32" sign: a full connect, 20+ heartbeats,
a TCLV parameter round trip, a file open/NACK, and a 1.84 MB image push.

```
io/        asyncio TCP listener; pyserial USB provisioning   <- the ONLY I/O
session/   DeviceConnection: bytes in -> Events out
packets/   per-type payload codecs
wire/      framing, LZ4 block chain, direction-dependent CRC

imaging/   PIL/numpy -> dithered, bit-packed rectangles + XXHash32 state
devices/   capability tables: TCLV, status enums, panel geometry (data, not code)
```

Nothing under `wire/`, `packets/`, `session/` or `devices/` imports `socket` or
`asyncio`, or reads a clock. There is a test that AST-scans the package to keep
it that way.

## Install

Python 3.11+.

```sh
pip install pyvisionect                 # wire/packets/session/devices + io.tcp
pip install 'pyvisionect[imaging]'      # + numpy and Pillow, for the imaging pipeline
pip install 'pyvisionect[usb]'          # + pyserial, for USB provisioning
pip install 'pyvisionect[test]'         # + pytest and dpkt, to run the test suite
```

Runtime dependencies are deliberately tiny: `lz4` (the block codec the wire
uses) and `xxhash` (the display-state checksum). Keep `xxhash` installed even
though the imaging module has a pure-Python fallback — the native one is roughly
nine times faster per frame.

## You are the server

This is the thing to internalise before anything else.

**A Visionect sign is a TCP client.** It dials out to port 11113 and speaks
first. There is no handshake, no authentication, no version negotiation and no
nonce exchange. The first thing on the wire is a status packet, and the 16-byte
UUID in its header is the entire identity claim. Registration is auto-create on
first sight.

So a reimplementation does not talk *to* a server — it **is** the server. It
listens, accepts whatever connects, and drives the sign. No VSS, no licence
server, no cloud, no account.

Two consequences:

1. The library's primary object is a **listener owning N device connections**,
   not a client. Home Assistant (or whatever you like) hosts that listener.
2. Because there is no auth, **put the listener somewhere only your signs can
   reach.** Anything that can open TCP 11113 can claim to be any UUID.

### Hello, sign

```python
import asyncio

from pyvisionect.io import VisionectServer
from pyvisionect.session import DeviceConnected, Nacked, StatusReceived


async def on_events(conn, events):
    for event in events:
        if isinstance(event, DeviceConnected):
            print(f"{event.uuid} online: {conn.state.panel.describe()}")
            conn.apply_pending()                   # drain work queued while it slept
        elif isinstance(event, StatusReceived):
            fields = event.status.fields()
            print(f"  battery {fields['BatteryLevel']}%  "
                  f"rssi {fields['SignalStrength']} dBm  "
                  f"temp {fields['AverageTemperature']} C")
        elif isinstance(event, Nacked):
            print(f"  device refused request {event.packet_id}: {event.control}")


asyncio.run(VisionectServer(on_events=on_events).serve_forever())
```

Queueing from inside the callback needs no explicit flush; the server drains
after every callback. To queue from elsewhere, use `server.connection_for(uuid)`
and then `await server.flush(uuid)`.

### The sign is asleep. Queue, do not wait.

`server.connection_for(uuid)` returning `None` is **the normal case** — a
battery sign is awake for about a minute a day, and even a mains one reconnects
unpredictably. So the library owns the deferral, because the coalescing rules
are correctness rather than style:

```python
work = store.queue(uuid)          # durable, coalescing, per-UUID
work.set_image(frame)             # content: last-write-wins, depth exactly one
work.write_param(29, 30)          # TCLV writes: last-write-wins per id
work.read_params([18, 19])        # TCLV reads: set union
work.sleep(60)                    # replaces any prior sleep
```

Ten pushes to a sleeping sign show the tenth, not all ten. Setting the heartbeat
to 5 and then 30 means 30. Pressing reboot twice is one reboot. Then, on the
`DeviceConnected` event:

```python
sent = conn.apply_pending()       # {slot: packet_id}, in PENDING_ORDER
```

**The order is not arbitrary**, and `apply_pending` enforces it:

| # | slot | why here |
|--:|---|---|
| 1 | param reads | correct our view before anything acts on it, and before a write races them |
| 2 | param writes | cheap, small, may change heartbeat or power behaviour |
| 3 | `flash_save` (TCLV 53) | persist step 2 before anything can reboot |
| 4 | framebuffer read | expensive; do it while the link definitely exists |
| 5 | **image push** | the big one; it must land before the device leaves |
| 6 | ghost clear | a second full-screen push, only after 5 |
| 7 | **sleep** | the device leaves — everything above must already be on the wire |
| 8 | **reboot** | last, unconditionally. Nothing survives it |

Steps 5→7 are the one that bites: the vendor waits for its renderer before
publishing its sleep packet, and getting it wrong leaves the user staring at
yesterday's dashboard for a day.

Each slot clears **only on an ack**, so a connection that drops mid-reconcile
leaves the remainder queued:

```python
elif isinstance(event, Acked):
    work.on_acked(event.packet_id)
elif isinstance(event, Nacked):
    work.on_nacked(event.packet_id, charging=event.charging)
```

A charging NACK (control flag bit 25) is a **retry-later, not a failure** —
treating it as fatal would make an integration look broken whenever someone
plugs the sign in.

`work.to_dict()` / `PendingWork.from_dict()` persist the register, so a
consumer restart does not silently drop a push the user queued ten minutes ago.
The `EncodedFrame` itself is deliberately not persisted — it is megabytes of
pixels and cheaper to re-encode; the revision counter survives so the intent is
not lost.

### Pushing a frame

```python
import functools

from PIL import Image

from pyvisionect.imaging import encode_frame

panel = conn.state.panel                      # from the device's own DisplayType

# encode_frame is CPU-bound and blocking -- see below. Never on the event loop.
frame = await loop.run_in_executor(None, functools.partial(
    encode_frame,
    Image.open("dashboard.png"),
    panel=panel,
    encoding=4,                               # 4 bpp, 16 greys
    dithering=3,                              # floyd-steinberg
    prev_state=conn.state.imaging_state,      # survives reconnects
))
store.queue(uuid).set_image(frame)            # or conn.send_image(frame) if awake
```

**`encode_frame` is CPU-bound, blocking, and releases no locks.** A 1440×2560
canvas goes through grey conversion, change detection, dithering
(Floyd–Steinberg is inherently serial), bit packing, 4-pixel interlacing and
LZ4: hundreds of milliseconds on a desktop, plausibly seconds on a Raspberry
Pi. Run it in an executor (`hass.async_add_executor_job` in Home Assistant).
On the event loop it stalls everything for the duration, which presents to a
user as "the whole instance freezes every hour". The resulting ~1.84 MB socket
write is fine on the loop — it is just a `write` plus a `drain` — but budget
roughly 6–8 MB of transient peak memory per device being pushed to.

`frame.state_checksum` becomes `ImageHeader.Checksum`, which is exactly what the
device echoes back as `DisplayStateCRC` (status tag 9).

### Sync state is tri-state — and `False` is not yet a problem

```python
conn.state.in_sync                 # True / False / None
conn.state.sync_status(now)        # SyncStatus.{IN_SYNC, CONVERGING, DIVERGED, UNKNOWN}
```

`None` means "we have never pushed, or the device has not said yet" — a fresh
install, not a problem. Present it differently from `False`.

`False` is not a problem *yet* either, which is the part that bites. A push
legitimately leaves the device disagreeing with us until it has drawn the frame
and reported the new `DisplayStateCRC`: **10–48 s on the live 31.2" sign**,
depending on where the push lands relative to the heartbeat. Anything that shows
`in_sync is False` as a fault therefore raises one after every normal update.
That is exactly what happened to the Home Assistant integration's
`binary_sensor.display_out_of_sync`.

`sync_status()` splits that `False` in two. It reports `DIVERGED` only when the
device has genuinely failed to converge — either it has been in touch
`CONVERGENCE_CONTACTS` times since the push **and** `settle_time()` has passed,
or `convergence_grace()` has passed with no word at all. Both windows are
derived from `NextStatus` (status tag 27), the device's own announcement of when
it will next be in touch, so a sign on an hourly heartbeat is not called broken
for the 59 minutes it is away.

The `settle_time()` gate on the contact counter is not belt-and-braces. The sign
**bursts** status packets around a draw — measured at nine in the first 55 s
after a push, then one a minute — so the bare counter can be satisfied in fifteen
seconds, before a 1.84 MB frame has finished transferring.

Pass the clock in, as everywhere else in this library:

```python
conn.send_image(frame, now=time.monotonic())
```

Without a `now` the verdict falls back to the contact counter alone, which is
the right behaviour for a state restored from disk: the push it remembers is
from before the restart and has long since had its chance.

E-ink keeps its image across a restart, so if you persist the encoder state and
the checksum, the correct action on first contact is often **nothing at all**:

```python
blob = frame.state.to_bytes()                 # versioned, self-describing, tiny
...
state.imaging_state = FrameState.from_bytes(blob)
state.pushed_checksum = saved_checksum
# on the next DeviceConnected: if state.in_sync is True, there is nothing to do
```

The format is owned by the library, so adding a field to `FrameState` is not a
breaking change for everyone who persisted one. Both grey planes are deflated,
and real e-ink content is mostly flat white: a 1440×2560 state usually lands in
tens of kilobytes rather than 3.7 MB. `DeviceState.to_dict()` /
`DeviceStateStore.to_dict()` do the same for the rest of the store.

### Forcing a full-screen redraw

Two levers, and they are not equivalent:

* `encode_frame(..., force_full_screen=True)` — the **verified** one. Skips
  change detection and emits rectangles covering every display, so the device
  repaints everything because it was sent everything.
* `conn.send_image(frame, force_full=True)` — the *checksum* lever. Perturbs
  `ImageHeader.Checksum` so it cannot match the tag the device holds. Verified
  end to end that the device never refuses a mismatched tag and that a mismatch
  costs exactly one extra full-screen redraw. It does not change which pixels
  are sent.

On `HardwareNameID == 8` — the 32" sign — every push is full-screen anyway, so
`force_full` is a no-op there. `conn.state.supports_rectangles` tells you which
case you are in.

### Shutting down

```python
await server.close()              # closes live device connections too
await server.close(abort=True)    # drops them without draining pending writes
```

**This matters more than it looks.** Since Python 3.12, `Server.wait_closed()`
waits until the server is closed *and all active connections have finished* —
and a mains-powered sign holds its TCP connection open permanently. A naive
`close()` + `wait_closed()` therefore **never returns**. Downstream that shows
up as a config entry that cannot be reloaded and a port that stays bound, then
`EADDRINUSE` on the next attempt. `close()` here does
`close()` → `close_clients()` → `wait_closed()`, in that order (Python's own
docs mandate stopping the accept first, to avoid racing new clients). There is a
regression test that hangs without it.

`abort=True` is the shutdown path: finishing a half-written 1.84 MB frame is not
worth waiting for.

If binding fails you get a `ListenError` carrying `errno`, `host` and `port`,
rather than a bare `OSError` to string-match — `EADDRINUSE` (something else
holds 11113, very likely the vendor's own server) needs completely different
advice from `EADDRNOTAVAIL`. Note `errno` can legitimately be `None`: when
several candidate addresses all fail, asyncio raises a summary error and
discards the per-address errno.

### "Nothing happens" — telling the two causes apart

```python
server.stats.accepted            # 0 -> nothing is reaching the port at all
server.stats.identified          # 0 with accepted > 0 -> connects, but not a sign
server.stats.silent_connections  # accepted, sent nothing. NORMAL, not an error
server.stats.last_seen_uuid()    # {uuid: clock} of the last frame from each
```

Those first two need opposite troubleshooting advice and neither raises an
exception, which is why they are counted. `silent_connections` is not a fault:
the reference capture shows the real sign opening and closing a connection
carrying zero protocol bytes, in normal operation.

### Sans-io, if you want to bring your own transport

```python
conn = DeviceConnection(store=DeviceStateStore())
events = conn.feed(data)        # bytes in -> list[Event]
out    = conn.drain()           # -> bytes you must send
events = conn.advance(now)      # timers, with YOUR clock
conn.send_image(frame)          # queues; no I/O
```

`feed_at(data, now)` is `feed` plus the timer bookkeeping, and is what the
asyncio adapter calls.

## Pointing a sign at your server (USB or DNS)

There is **no discovery in either direction.** The sign has no open ports, answers
no mDNS/SSDP/NetBIOS/SNMP/CoAP probe, advertises nothing, and does not even do a
DNS lookup when its server address is already resolved in flash. The server
binaries contain no mDNS/SSDP/UPnP code at all. The only binding is a server
address typed into flash.

And you cannot change that over the network. Eight TCLV parameters are marked
`canWrite: false` in the vendor's own descriptor table, and they are exactly the
ones you would need:

| id | name | USB command |
|---:|---|---|
| 2 | Connectivity type | `conn_type_set <type>` |
| 18 | Server IP | `server_tcp_set <ip\|dns> <port>` |
| 19 | Server port | `server_tcp_set <ip\|dns> <port>` |
| 65 | WiFi SSID | `wifi_ssid_set <ssid>` (hidden from `help`, but present) |
| 66 | WiFi security mode | `wifi_security_set <none\|wpa2\|wpa2e>` |
| 67 | WiFi password | `wifi_psk_set <psk>` |
| 68 | WiFi band | `wifi_conf_set` arg 4 |
| 70 | Mobile security mode | `mobile_conf_set` arg 2 |

The library refuses a network write to any of them and the error names the USB
command that can do the job:

```python
>>> conn.write_params({18: "homeassistant.local"})
ReadOnlyParameter: TCLV parameter 18 ('Server IP') is read-only over the network
(canWrite:false in the vendor's own descriptor table). Set it over the USB serial
CLI instead: server_tcp_set <ip|dns> <port>, then `flash_save`, then `reboot`.
```

So you have two routes.

### Route 1 — DNS re-point (no cables)

If the sign already holds a **hostname** rather than a literal IP, re-point that
name at your listener and change nothing on the device. Read what it currently
holds with a param read of ids 18 and 19 — that works fine over the network,
since only *writes* are blocked.

### Route 2 — USB

An **FTDI FT232** (`0403:6001`, confirmed with `lsusb`) on the Micro-USB
connector, **115200 8N1**, carrying a line-oriented ASCII shell. Everything in
this section was measured on a live 32" sign running firmware **7.4.4407**.

```sh
pyvisionect-usb ports                 # find candidate ports
pyvisionect-usb identify /dev/ttyUSB0 # confirm it is a sign (2 read-only commands)
pyvisionect-usb dump /dev/ttyUSB0     # read every readable setting
pyvisionect-usb commands              # the 112 commands this firmware answers

# Provisioning prints the plan and exits. --execute to actually do it.
pyvisionect-usb provision /dev/ttyUSB0 \
    --ssid MyNetwork --psk 's3cret' --server homeassistant.local
```

From Python, read-only (the default — it cannot change anything):

```python
from pyvisionect.io.usb import SerialConsole, Sign

with SerialConsole("/dev/ttyUSB0") as console:
    console.sync()
    sign = Sign(console)
    info = sign.dump()
    print(info.describe())
    print(info.wifi.ssid, info.server.host, info.status.rssi_dbm)
```

Every getter returns a dataclass, not a string. Writing needs a flag, and the
flag named in the refusal is the gentlest one that would work:

```python
sign = Sign(console)                            # reads only
sign = Sign(console, allow_writes=True)         # + ordinary setters
sign = Sign(console, allow_destructive=True)    # + network/panel/lifecycle
sign = Sign(console, i_really_mean_it=True)     # + formats, firmware, passwords
```

Provisioning is a plan you read before you run:

```python
from pyvisionect.io.usb import plan_repoint

plan = plan_repoint("homeassistant.local")
print(plan.describe())      # every command, what it does, and what it costs
plan.execute(Sign(console, allow_destructive=True))
```

`plan_repoint` is `server_tcp_set` -> **`flash_save`** -> `reboot`. The
`flash_save` is not optional — verbatim from the vendor: *"All changes to device
configuration are by default retained only in the working RAM."* Which cuts both
ways: until it runs, a power-cycle undoes everything.

#### `help` lists 111 of the 162 documented commands — and the firmware has 112

Commands are compiled in behind switches, so the device in front of you is the
only authority — but **`help` is not a complete index of it.** `wifi_ssid_set`
is documented by the vendor, is not printed by `help`, and still answers: a bare
call returns `E: Invalid argument(s)`, where a command the firmware really lacks
returns `Command '<x>' not recognised.` So there is a third category, *hidden*,
and the delta is three sets in `pyvisionect.io.usb.commands` rather than two:
`LISTED_BY_HELP` (111), `HIDDEN_IN_7_4_4407` (1) and `ABSENT_FROM_7_4_4407`
(97, meaning **unlisted**). 49 of those 97 — every one the oracle can safely be
pointed at — have since been probed and are genuinely absent
(`PROBED_ABSENT_7_4_4407`), so `wifi_ssid_set` is a one-off rather than the tip
of an iceberg, and the uncompiled hardware families really are uncompiled.

Two findings change documented advice:

- **`wifi_ssid_set` is hidden, not missing — and it carries the space.** It is
  the vendor's documented workaround for an SSID containing a space, it takes
  the SSID alone with no PSK, and this firmware has it. Measured on the device:
  it takes **everything after the command name and one separating space,
  verbatim**, with no quoting or escaping convention, so `plan_wifi` and
  `Sign.set_wifi` now emit it unquoted for a spaced SSID instead of refusing.
  The passphrase is still refused, for a different reason: its field has no read
  path, so a truncated one cannot be detected.
- **`flash_print` is not listed**, so there is no *known* one-shot settings
  dump. `Sign.dump()` walks the per-area getters instead. It is nullary, and a
  bare call to a nullary command executes it, so the existence probe that
  settled `wifi_ssid_set` is not safe to run here.

Going the other way, 47 present commands are undocumented, including all three
`encryption_*` commands, the whole `fs_*` family and the four `vlog_*` log-level
controls.

#### `sf_rdid` and `sf_rdst` kill the console

Measured, the hard way. Both assert immediately — `assert: spi_flash_cli.c:139`
and `:188` — because neither checks that `sf_select` has chosen a device first.
The assertion kills `usb_cli_task`: the prompt never returns, and the software
watchdog notices (`W WD task timeout: cli_USB` appears on the UART) without
acting on it. The sign itself is fine throughout — it stays on the network,
keeps its heartbeat and keeps its picture — and the console comes back only when
the device reboots, which it does on its own roughly 25 minutes later.

Both are gated behind `i_really_mean_it` for that reason alone; they read
nothing and write nothing. `sf_list` is fine.

#### Asynchronous log lines, and why this is the hard part

The firmware logs to the same UART, unprompted. A heartbeat fires every minute
and dumps eight lines; an image push adds a `Profiling:` line. Any of them can
land in the middle of a reply, so "every line between my command and the prompt
is my answer" corrupts *intermittently* — which is the worst failure mode,
because it passes every test you write by hand.

`SerialConsole` separates the streams with four mechanisms, and only the last
one is a heuristic:

1. **Drain before send** — bytes buffered before the write cannot be the reply.
2. **Echo anchor** — the device echoes the command line; the reply starts there.
3. **Prompt terminator** — the reply ends at a `"> "` starting a line.
4. **Pattern match** — lines inside the frame matching `LOG_PATTERNS` (every
   entry taken from a real capture) move to the log stream.

`CommandResult` carries `lines` (yours), `logs` (the firmware's) and `raw` (both,
lossless). There is also an `on_log` callback and a `logs` history.

The sink can also be turned off at source. The firmware has a `vlog_*` family
that sets log levels per *source* and per *destination* independently — logging
is a matrix — and `vlog_unify_levels <usb_level>` applies one level to every
source on the USB destination. The scale runs **1 (silent) to 5 (everything)**,
and **`0` is not a valid level at all** — the firmware rejects it. So
`vlog_unify_levels 1` silences the port, measured at 0 lines in 90 s against
~1 400 lines/min at level 5 while the radio was flapping, and the command's own
help line ("Reset logger levels to default for USB") is simply wrong.
`Sign.silence_usb_logs()` is the shorthand; `Sign.unify_usb_log_levels(level)`
keeps the mandatory argument, because a console driver wants 1 and someone
debugging a dead link wants 3.

It does **not** replace the four mechanisms above. Nothing persists without
`flash_save`, and the sign reboots itself when it cannot reach its server
(`E: Max conn errs. Reboot`), so the default verbosity comes back underneath a
long-running session. It also hides the error lines along with the chatter.

#### Line discipline, measured

- **CR (`\r`) submits. LF does not.** `cli_version_get\n` is echoed and then
  *held* in the edit buffer, so the next command is appended to it and the pair
  is rejected as one unrecognised word. `\r\n` works but leaves a stray LF that
  pollutes the next read, so the terminator is a bare `\r`.
- The prompt is `"> "`, with no trailing newline. The device echoes. Commands
  are case-insensitive.
- **Only some commands emit `rv:`, and not always last** — `certs_config_get`
  prints `rv: 0` *before* its body. It comes in two spellings in one firmware,
  `rv: 0` and `rv: 0x0`.
- Two error strings, and they quote `help` differently:
  `Command 'x' not recognised.  Enter 'help' ...` and
  `Incorrect command parameter(s).  Enter "help" ...`

#### Soft sleep did not happen

The vendor documents a soft sleep that cuts the console after 15 seconds of
accelerometer inactivity, and recommends setting `SLEEP_MODE` (52) to 1 for a
working session. **It did not bite on 7.4.4407.** A console held its prompt
across an undisturbed 45-second idle and answered immediately after, on a sign
whose uptime had been 25 days — so the CLI had been reachable that whole time
untouched. This library therefore sends no keepalive and does not touch TCLV 52,
both of which would be writes on a read-only session.

#### Encryption: the USB key is not the way in

`encryption_mode_set` takes **0 or 1** — that is solid, it is TCLV 130, whose
vendor description reads *"Outbound encryption: 0=Disabled, 1=Enabled"*. Note
that is a boolean and **not** the wire protocol's `SecurityType` (0/2/3).

The key format is **not** solid. The primitive is AES-128, so 16 bytes; the CLI
takes one whitespace-delimited token and `encryption_config_get` echoes it back
in single quotes, so it is stored as a printable string. The best-supported
reading is **16 printable ASCII characters used verbatim**, because every other
key and IV in this system is exactly that (`F@%gtb7;xLmXV$9a`,
`thisbeemulatorke`, `N3ls0#!Dba0f8*B>`). Base64 (24 chars) is second — it is how
the gateway serialises such a key for escrow. None of this was tested.

And the conclusion that matters: **setting the key over USB does not buy you
self-hosted link encryption.** The gateway encrypts with 16 `crypto/rand` bytes
minted fresh per activation and never transmits them; it sends the escrow
service's opaque response verbatim and the device is expected to resolve it
using what it already holds. TCLV 131 is that long-term secret, not the session
key, and reproducing the resolution means reading firmware that ships encrypted.
**TLS is the better answer in principle and is implemented here** --
`VisionectServer(certfile=..., keyfile=...)` sniffs the first six bytes of each
connection for a ClientHello and upgrades only that connection, so plaintext and
TLS signs share the one port exactly as the vendor's gateway arranges it. But
the device half depends on TCLV 145 (*"TLS mode: 0=disabled, 1=TLS 1.3"*), and
on the firmware this was developed against (7.4.4407) **the device does not
implement 145**: it refuses both a read and a write of it with the same code it
gives for a parameter id that does not exist. So on that firmware there is no
transport security available at all, and the control is network isolation. See
`OPEN-QUESTIONS.md` A4 for the wire evidence.

`pyvisionect.io.usb.encryption` holds all of this with its provenance, and
`pyvisionect-usb encryption` prints it. No setter in that module has ever been
executed.

## Protocol notes worth knowing

### The checksum is direction-dependent

The single most dangerous detail in this protocol. A symmetric CRC
implementation appears to work and then fails silently.

| direction | what is CRC'd |
|---|---|
| server -> device | `crc32_ieee(body)` — the whole body after compression |
| device -> server | `crc32_ieee(header[0:16])` — **it does not cover the payload at all** |

Proof of the device rule: two status packets ten minutes apart, with different
packet IDs and different battery and uptime values, carry the *identical*
checksum `0x198ceb01`, because their `(Version, Security, Compression, Length)`
prefix is identical.

`pyvisionect.wire.frame_checksum` takes a mandatory `Direction` and refuses
anything else. The decoder only *verifies* the inbound field in strict mode
(`ConnectionConfig(strict=True)`), off by default, because the real gateway never
validates it and a CRC that covers no payload bytes cannot detect corruption
anyway.

### Framing

```
ProtocolHeader  20 B LE   Version=3 | Security | Compression | Length | Checksum
  if Compression != 0: a chain of 24-B block records
     BlockIndex(0-based) | BlocksMinusOne | PayloadLength | UncompressedLength
     | Stored | Reserved
     block plaintext = 4800 B (0x12C0)
     Stored==1 -> payload verbatim; Stored==0 -> payload is a RAW LZ4 BLOCK
DataHeader      36 B LE   Priority | DeviceID[16] | Type | ID | Length | Reserved
```

- `Stored == 1` means **not** compressed. LZ4 is used only when it *strictly*
  shrinks the block.
- `BlocksMinusOne` is an **index**, repeated identically in every block — 384 in
  all 385 blocks of the captured image push — so a receiver knows the total from
  the first header.
- The compressed payload is a **raw LZ4 block**: no frame magic, no content
  checksum, no size prefix. `lz4.block`, not `lz4.frame`.
- The device never compresses; the gateway always does, even for an 8-byte ack.
- Maximum frame: 50 MiB (`0x3200000`).

### IDs and acks

- Device packet IDs are a counter that **resets to 1 on every TCP connection**.
- Server packet IDs are a random uint32.
- The responder echoes the originator's ID. `ID == 0xFFFFFFFF` means "do not ack".
- Ack is `Control{Flags: 1}`; `Flags == 0` is a NACK; bit 25 is NACK-because-charging.
  A device NACK carries a 4-byte error code — `0x00008a00` for the `@screen_N`
  open that this hardware always refuses.

### Packet type 2 (command) has never been seen on the wire

Not in either direction, in any capture. The captured traffic covers types 1
(control), 3 (status), 5 (image), 8 (param) and 10 (file) and nothing else. So
`CommandPacket`'s 12-byte header is read out of the Go struct and nothing more
— and there is **direct precedent in this protocol for the struct and the wire
format disagreeing**: `proto.Command`'s struct is 12 bytes and the param
packet's on-wire header is **8**, because `Reserved` is simply not emitted.

Two of the command ids have no emission site anywhere in the vendor server:
`-1` ("refresh") and `-2` ("clear screen") are enum entries and nothing else.
`11` ("status request") is the best attested, emitted by the ping watchdog.
`4` ("sleep") has a traced payload — one little-endian uint32 of minutes,
non-zero, rejected as `errSleepTime` at 0, and `sleep()` validates that.

So: prefer a verified route where one exists — an image push instead of
`refresh`/`clear_screen`, a TCLV write instead of `set_heartbeat` — and if you
want a guarantee:

```python
ConnectionConfig(allow_command_packets=False)   # refuses to emit type 2 at all
```

A reboot is at least observable after the fact: the device reconnects with
`ConnectReason == 1`, which `status.connect_reason_name` reports.

### There is no `list` opcode, and `@screen_N` is not about updates

A directory listing is a plain **read of `"."`** — a path of `"/"` or `""` is
rewritten to the one-byte string `"."`, and `Read(".", 10240)` returns an ASCII
blob:

```python
conn.file_list()                  # open "." for reading
conn.read_file_range(FILE_LIST_LENGTH)
...
parse_file_listing(reply)         # {name: FileListEntry(size, checksum)}
```

The columns are **`name checksum size`** — the useless one is in the middle, and
it reads `"0"` for every file on this firmware. The order is not a guess:
`/image0.pv2` lists as 134409 bytes and that file's own `ProtocolHeader.Length`
reads 134389, which is 134409 minus the 20-byte header.

Every reply the device sends — the listing, and every chunk of a file read —
arrives as `FileOperation.EVENT` (6), **not** `READ` (2). `read` is the request
direction only.

### Reading a file back off the device

```python
listing = await server.list_device_files(uuid)       # one round trip, <1 s
raw     = await server.read_device_file(             # minutes; see below
    uuid, "/image0.pv2", listing["/image0.pv2"].size,
    progress=lambda done, total: print(done, total),
)
frame   = parse_stored_frame(raw)                    # -> ImagePacket
canvas  = decode_image_packet(frame.image, panel).canvas
```

A `.pv2` is a stored protocol frame: `ProtocolHeader` (version **2**, not the
wire's 3) + LZ4 block chain + `DataHeader` + an ordinary type-5 image packet.
`parse_stored_frame_header()` reads the shape out of the first 44 bytes without
inflating anything. `decode_image_packet()` is the inverse of `encode_frame` and
is gated on the golden capture round-tripping bit for bit.

Three device facts shape the API rather than being hidden by it:

* **one reply carries at most 1024 bytes**, whatever you ask for (asking for
  32768 gets you 1024);
* **there is no seek** — `open` rewinds to zero and nothing else moves the
  cursor, so a lost reply means restarting the whole transfer;
* **throughput is ~2.3 KiB/s**, about 420 ms per round trip, so this sign'''s
  stored frames take **1–9 minutes each**.

`FileRead` in `pyvisionect.session.filetransfer` is the sans-io sequencer if you
are driving your own io.

**These files are not framebuffers.** On firmware 7.4.4407 `/image0.pv2` …
`/image5.pv2` are Visionect'''s shipped demo screens — a wayfinding board
("Welcome to Nanotech inc."), a museum label ("Room 35 — Michelangelo and the
Florentines") and four more. Pushing a new frame changes none of them: not their
size, not their first bytes. They also carry `DataHeader.DeviceID == 0` and
`ImageHeader.Checksum == 0`, which no frame we send ever does
(`StoredFrame.looks_like_our_push` is that test). So there is **no device-side
readback of what the panel is currently showing**, and matching a stored
`ImageHeader.Checksum` against `DeviceState.pushed_checksum` cannot work — the
field is zero in all six. `DisplayStateCRC` remains the only way to ask the
device what it is displaying, and it is enough.

An earlier version of these docs said the gateway opens `@screen_<N>` "before
deciding how to update the screen", making the NACK "the mechanism behind
incremental updates". **That was wrong**, and it invented a constraint that does
not exist. The only reference to `"@screen_2"` in the whole gateway binary is a
hardcoded literal inside `main.(*grpcHandlers).handleLiveView` — not even a
`@screen_%d` format. The NACKs in the capture are replies to the `ac-device`
**live-view** request; the admin UI only offers that for `HardwareNameID` in
{11, 13, 24, 34, 42} and this sign is 8. The file protocol is not entangled with
image pushes at all.

(Partial updates are still never sent to this sign by the vendor server, for an unrelated reason:
`getRectangleSupport` returns false unconditionally for `HardwareNameID == 8`.)

### Running without the LZ4 codec

```python
ConnectionConfig(compressor=STORED_ONLY)   # every block Stored=1
```

`lz4` is imported lazily and is only needed to compress outbound frames (the
default) or to inflate an inbound compressed one — which a device never sends:
every device→server frame in the capture carries `Compression = 0`.
`STORED_ONLY` keeps the ordinary block-chain framing and marks every block
`Stored=1`, which is a path the device demonstrably handles (60 of the captured
push's 385 blocks were stored anyway). The trade is roughly 4% more bytes for
zero C extensions beyond numpy/Pillow, and the captured push shows LZ4 buying
almost nothing on dithered halftone data (mean ratio 0.956).

Setting `Compression.NONE` on the **outbound** path is a different thing and is
**unverified** — the gateway was never observed sending an uncompressed frame to
a device, so whether the firmware accepts one is unknown. Do not reach for it.

### Reconnects

A reconnect is a **full IP-stack re-initialisation**, not a socket retry:

```
802.2 XID -> DHCP DISCOVER (from 0.0.0.0) -> DHCP REQUEST
  -> ARP who-has gateway -> ARP who-has server -> TCP SYN :11113
  -> status with the ID counter reset to 1
```

So **never key session state on the source port or the packet counter.** Only the
UUID is stable. `DeviceStateStore` does exactly that, and one of those
connections was observed opening and closing without carrying a single protocol
byte, so a silent connection must not evict anything.

### Status packets are not CBOR

The payload is a flat list of 8-byte `(uint32 tag, uint32 value)` records,
terminated by tag `0xFFFFFFFF`. Absent fields are omitted entirely, so a status
is sparse — 62 records out of ~104 possible tags in the real capture.
Multi-word values occupy *consecutive* tags, which is why the numbering has gaps.

`StatusPacket` keeps the raw record list in wire order, so re-encoding is exactly
the inverse of decoding; `.fields()` gives you the named, typed view.

Codecs that are easy to get wrong:

| field | encoding |
|---|---|
| `SignalStrength` (13) | dBm **magnitude with the sign dropped** — negate it |
| `DeviceUptime` (15), `NextStatus` (27) | **minutes**, not seconds |
| `AverageTemperature` and friends | signed **int8** in the low byte; `0xFF`/`0xFFFFFFFF` = no reading |
| `HardwareID` (2) | `ID<<24 \| Family<<16 \| PCB` |
| `GTIN` (50–53) | 4 ASCII chars per word, little-endian, NUL-terminated |
| `BSSID` (91–92) | 6 bytes over 2 tags, little-endian |
| `Features` (43) | a **10**-bit mask |
| `ErrorCode` (1) | the *reason the packet was sent*, not a fault (13 = deep-sleep request) |
| `0xFFFFFFFF` as a value | "field absent" |

`Features` deserves a note. The vendor's *legacy* parser loops
`for mask := 1; mask <= 0x100; mask <<= 1` and silently drops bit 9 (`WiFiOTA`,
512), so the vendor's own admin UI and engine see the live sign's `585` as if it
were `73`. **We do not replicate that bug.**

### Devices are data, not subclasses

`DisplayType` stays an **opaque uint32**. The live sign reports `0xC2050128`,
which is not in the vendor's enum at all (it falls through to
`DISPLAY_UNKNOWN`) and is nevertheless a perfectly valid key. So: look the raw
value up in a table with a default row, and take *capabilities* from the device's
own `PV2Features` advertisement, never from the model.

For the 32" sign the server models `4 x 1440x640` and then the `eink-flip`
driver's interlacing step emits **two** rectangles of `2880x640` with
`ScreenID` 0 and 1, horizontally mirrored. `imaging/` reproduces both.

`panel_for()` says whether it recognised the panel at all, so diagnostics can be
honest about it rather than silently using a guess:

```python
>>> panel_for(0xC2050128).describe()
'unrecognised panel, assuming 1440x2560 (4 x 1440x640, driver eink-flip)'
>>> panel_for(0xC2050128).is_default
True
```

Delta updates are implemented in the vendor server but it **refuses to send them to
this hardware**: `getRectangleSupport` returns false unconditionally for
`HardwareNameID == 8`, before it even consults the device options. Hence
`DeviceState.supports_rectangles`, which defaults to `False` until the device has
told us what it is.

That is server policy, not a panel limit. The 31.2" sign **does** accept partial
rectangles — measured 2026-10-05, 15 of them acked and drawn only where addressed. What
it cannot take is a rectangle in *canvas* coordinates, which would have to survive the
interlaced fold; one addressed in *screen* coordinates bypasses the fold. This library
has no screen-space encoder yet, so for now: do not build logic that depends on partial
updates landing. See `OPEN-QUESTIONS.md` A10.

## What is deliberately out of scope

- **Link encryption.** `Security != 0` needs a 16-byte session key that the
  vendor's gateway escrows with a Visionect HTTPS service; that key material is
  not in the server image, so a self-hosted deployment is plaintext by
  construction. The decoder raises a clear error and the seam is there.
- **Firmware and bootloader push.** The payloads are AES-CBC (fixed, fleet-wide IV) under a key that
  exists only on Visionect's servers. Relaying an opaque blob to a device that
  will flash it is not something to ship untested.
- **Discovery.** There is none. Give Home Assistant an explicit address.
- **`packet.Type 12` (CBOR) payloads.** The framing is implemented; the CBOR body
  is not parsed. That channel belongs to the newer "AC"/JS-app device generation
  and firmware 7.4.4407 never uses it.

## Tests

```sh
pip install -e '.[test,imaging,usb]'
pytest
```

The primary correctness gate is a **golden replay** of real traffic from the
user's sign (`tests/fixtures/golden.json.gz`, ~90 KB, extracted from a 1.9 MB
pcap with `tools/extract_pcap_fixture.py`). It asserts that every one of the 62
captured frames decodes in strict mode, and that **61 of 62 re-encode
byte-for-byte** — payload re-encoded through its own codec, re-framed, re-CRC'd,
and compared against the captured bytes.

Two honest caveats about that number:

1. **The vendor's LZ4 compressor is not reproducible.** `vss/lz4.Lz4Compress`
   produces different (equally valid) output from python-lz4 for the same input.
   Measured over all 328 LZ4 blocks the gateway sent in the capture: **0 of 328**
   match `lz4.block.compress` in its default or `fast` modes at any
   acceleration, and only **3 of 328** match under `high_compression` (levels
   1–9) — and those three are the tiny 44-byte ack bodies, not the image data.
   So for the gateway direction the replay test feeds the
   encoder a compressor that hands back the capture's own per-block payloads.
   Everything else — the block headers, the stored/LZ4 decision, the CRC, the
   outer header — is reproduced exactly, and the chain is separately rebuilt
   byte-for-byte from the decoded plaintext. Interoperability is unaffected (the
   device decodes any valid LZ4 block); `encode_blocks(compressor=...)` is the
   seam if you ever do reproduce it.
2. **`tcpdump` dropped five 1460-byte segments inside the 1.84 MB image push.**
   4 of its 385 blocks are unrecoverable and `crc32(body)` for that one frame
   cannot be recomputed, so it is the one frame excluded from the byte-exact
   count. Everything else about it *is* checked: 383 of 385 block headers, the
   60-stored/325-LZ4 split, the `DataHeader`, the `ImageHeader` and both
   `RectangleHeader`s, all byte-exact.

If the original pcap happens to be present, `tests/test_pcap_full_replay.py`
repeats the exercise against the raw capture instead of the fixture; it skips
cleanly when it is not.

Also covered: round-trip property tests on every codec, the direction-dependent
checksum against its captured constants, the 235-entry TCLV table, the session
state machine (acks, NACKs, reconnect-by-UUID, all three timers), the
pending-work queue (every coalescing rule, the connect ordering, ack/NACK
bookkeeping, persistence), `FrameState` and `DeviceState` serialisation, the listener over real loopback sockets
(bind errors, stats, flush), and an AST scan that fails the build if anything in
`wire/`, `packets/`, `session/` or `devices/` grows an I/O import or a clock
call.

The USB package has its own 274 tests in `tests/usb/`, run against a fake
transport that replays **real captured frames** from a live sign
(`pyvisionect.io.usb.fake`, scrubbed of identifiers and otherwise untouched).
That matters more than it sounds: a fixture written by hand from a parser's point
of view tests the parser against its own assumptions. Several parsers are shaped
the way they are only because of something visible in those captures and nowhere
in the vendor's documentation — the blank line before `uuid_get`'s payload, the
`rv: 0` that precedes `certs_config_get`'s body, the tab-separated `task_list`
columns, the one `status_get` key with a space in it.

The interleaving tests are the ones worth reading. The fake can splice the real
eight-line heartbeat burst into the middle of a reply, before the echo, or into
the buffer ahead of the write, and the tests assert that a parsed value comes out
identical either way. There is also a test that runs the async-log classifier
over **every line of every captured reply** and fails if it would swallow one,
because a false positive there silently eats an answer.

A further set of cross-checks compares two commands that report the same
underlying value — `wifi_bssid_get` against `status_get`'s two BSSID words,
`cc3100_rssi` against `SIGNAL_STRENGTH`, `fs_stats`' block count against
`FS_TOTAL_SIZE`'s bytes. Those are what would catch a parser that is
self-consistently wrong, and all of them were re-run against the live sign.

One of those is a regression test for a **hang** rather than a wrong answer:
`test_close_does_not_hang_with_a_client_still_connected` opens a client, leaves
it open as a real sign does, and asserts `close()` returns. Remove the
`close_clients()` call and it times out, along with the test that the port is
released for a reload. Worth knowing because `close_clients` appears zero times
in all of Home Assistant core — no core integration hosts a listener for devices
that hold connections open indefinitely, so there was no precedent to copy.

## Provenance

Every non-obvious constant in this package carries a docstring citing where it
came from: a Go symbol and source line from the unstripped `bin/gateway`
(`proto/v2` v2.1.5, `proto` v1.2.23), a byte offset in the captured traffic, or
the vendor's published CLI reference. A handful of things are marked `[INFERRED]`
or `[GAP]` because they were not provable from the material available — notably
`packet.Priority`'s value space, the meaning of the status sentinel's value, the
`GPS` coordinate string framing, the `sleep` command's payload layout, and the
inverse-update option bit. Those are flagged in place rather than smoothed over.

## Licence

MIT.
