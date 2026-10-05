# The USB serial console

Every sign has a Micro-USB connector carrying a USB-to-UART bridge and a line-oriented
ASCII command shell. **This is the only way to write the network and bootstrap
configuration** — see [parameters.md](parameters.md#the-read-only-set-and-why-usb-provisioning-is-unavoidable).

The vendor server contains **no USB provisioning code whatsoever**: no usb, serial, tty,
cdc or hidraw symbol outside Go standard-library false positives, no udev rule, no
VID/PID, no serial tool, and zero occurrences of "usb" in its web assets. [D] This channel
is entirely device-side.

There is also **no "drop a config file on a USB drive" format**. No HID interface, no mass
storage, no DFU appears in the vendor documentation or in the shipped server image.
Bootstrapping is interactive.

## Transport

| | |
|---|---|
| bridge | **FTDI FT232**, USB ID `0403:6001`, confirmed with `lsusb` on a live sign [W] |
| line settings | **115200 baud, 8 data bits, no parity, 1 stop bit** [C, verbatim from the vendor documentation] |
| Linux device node | `/dev/ttyUSB*` (the `ftdi_sio` driver) |
| macOS device node | `/dev/cu.usbserial-XXXXXXXX` |
| protocol | a line-oriented ASCII shell. Type a command, press enter, read text back. **Not** the binary TCLV encoding. |

The device **holds its network session while on USB** — it does not need to be taken
offline. [W]

## Line discipline, measured

All of this was measured on a live sign running firmware 7.4.4407, and **none of it is
documented by the vendor**. [W]

- **CR (`\r`) submits. LF does not.** Sending `<command>\n` gets the command echoed and
  then *held* in the edit buffer, so the next command is appended to it and the pair is
  rejected as one unrecognised word. `\r\n` works but leaves a stray LF that pollutes the
  next read. **Use a bare `\r`.**
- The prompt is `"> "`, with **no trailing newline**.
- The device **echoes** the command line.
- **Commands are case-insensitive.**
- **Only some commands emit a return-value line, and not always last** — one command
  prints `rv: 0` *before* its body. It comes in two spellings within one firmware:
  `rv: 0` and `rv: 0x0`.
- There are two error strings, and they quote `help` differently:
  - `Command 'x' not recognised.  Enter 'help' ...`
  - `Incorrect command parameter(s).  Enter "help" ...`

## The firmware logs to the same UART, and this is the hard part

The firmware emits log lines **unprompted** on the same port. A heartbeat fires every
minute and dumps eight lines; an image push adds a profiling line. Any of them can land
**in the middle of a reply**. [W]

So "every line between my command and the prompt is my answer" corrupts *intermittently* —
which is the worst possible failure mode, because it passes every test you write by hand.

Four mechanisms separate the streams reliably, and only the last is a heuristic:

1. **Drain before send.** Bytes buffered before your write cannot be part of your reply.
2. **Echo anchor.** The device echoes the command line; the reply starts after that echo.
3. **Prompt terminator.** The reply ends at a `"> "` that starts a line.
4. **Pattern match.** Lines inside the frame that match known log formats are moved to a
   separate stream.

Keep the raw byte stream as well as the split views; the split is lossy by construction.

**The sink can be turned off at source.** The firmware has a `vlog_*` family that sets log
levels per *source* and per *destination* independently — logging is a matrix — and
`vlog_unify_levels <usb_level>` applies one level to every source on the USB destination.
The scale runs **1 (silent) to 5 (everything)** and **`0` is not a valid level**: the
firmware rejects it, along with `-1`, `6` and anything non-numeric. So
`vlog_unify_levels 1` silences the port — measured at 0 lines in 90 s, against 18–61 at
level 5 on a healthy link and ~1 400 lines/min at level 5 while the radio was
reconnect-flapping. The command's own help line ("Reset logger levels to default for USB")
is simply wrong; `vlog_set_default_levels` is the one that resets. [W]

By line kind: level 1 emits nothing, 2–3 emit only `E:` error lines, 4 adds state
narration (`From state N going to state N`, DHCP/IP/DNS), 5 adds debug detail (`Frame send
N bytes`, image transfer, EPD temperature).

**It is not a replacement for the four mechanisms above**, because it does not persist:
nothing survives without `flash_save`, and the sign reboots itself when it cannot reach its
server (`E: Max conn errs. Reboot`), which quietly restores the default verbosity
mid-session. Treat it as an optimisation for a known-quiet window. It also hides
`E: TCP connection Error` and friends, so level 2 or 3 is the better choice if you still
want to see faults.

## `help` is not a complete index

This is the fourth trap in these documents, and the one most likely to make a
reimplementation refuse a capability it actually has.

Commands are compiled in behind firmware build switches, so **the device in front of you is
the only authority**. But `help` does not tell you what that is. Measured on firmware
7.4.4407: [W]

| set | count |
|---:|---|
| documented by the vendor | **162** |
| listed by `help` | **111** |
| **present and answering** | **112** |
| hidden — present but not listed by `help` | **1**, and now on evidence |
| documented but not listed | 97 — of which **49 probed and genuinely absent** |
| present but **undocumented** | **47** |

So there are **three** categories, not two: listed, hidden, and absent.

### The existence oracle

A **bare invocation** distinguishes present-but-hidden from genuinely absent: [W]

| response | meaning |
|---|---|
| `E: Invalid argument(s)` | the command is **present**, just hidden |
| `Command '<x>' not recognised.` | the command is genuinely **absent** |

> **Only safe for commands that take a required argument.** A bare call to a **nullary**
> command **executes it.** Filter by arity against the vendor reference first, and skip
> anything whose arity you do not know. Several nullary commands are destructive.

### Two findings this changes

- **The documented workaround for an SSID containing a space is hidden, not missing.**
  `wifi_ssid_set` is documented by the vendor, is **not** printed by `help`, and
  nevertheless answers. It takes the SSID alone with no password. [W] (Whether the
  console's parser carries a space *through* it is still untested — **[GAP]**. So the
  route exists and is unverified, rather than being impossible.)
- **`flash_print` is not listed**, so there is no *known* one-shot settings dump on this
  firmware. Walk the per-area getters instead. It is nullary, so the existence probe above
  is **not** safe to run on it.

Going the other way, the 47 undocumented-but-present commands include all three
`encryption_*` commands, the whole `fs_*` family and the four `vlog_*` log-level controls.
[W]

## The bootstrap sequence

Verbatim from the vendor reference: [C]

```
# 1. WiFi
wifi_conf_set <SSID> <PSK> wpa2 0
#    (or, if the SSID contains spaces, the single-value setters:)
wifi_ssid_set <SSID>
wifi_psk_set  <PSK>

# 2. Server
server_tcp_set <SERVER IP or DNS NAME> 11113

# 3. Persist  -- MANDATORY
flash_save

# 4. Apply
reboot
```

For a cellular device instead of WiFi:

```
conn_type_set 8
flash_save
reboot
```

> **All changes to device configuration are by default retained only in the working RAM.
> If you want your settings to persist across reboots, you will need to run `flash_save`
> before rebooting.** — vendor documentation, verbatim [C]

That cuts both ways, and the second direction is the useful one: **until `flash_save`
runs, a power cycle undoes everything**, which makes almost every console experiment
reversible.

### Re-pointing an already-provisioned sign

If you only need to move a sign to a different server, the minimal plan is:

```
server_tcp_set <your host or IP> 11113
flash_save
reboot
```

`cs 3` ("Connect to server") **forces an immediate reconnect without a reboot** [C], which
is strictly better when you want to test a change: it removes the wake-cycle wait, and if
you have not run `flash_save`, nothing persists. Prefer this while experimenting, and save
only once you are satisfied.

And if the sign already holds a **hostname** in parameter 18 rather than a literal IP, you
may not need the cable at all — re-point that name. Read what it holds with a network
parameter read of ids 18 and 19; only *writes* are blocked.

## The key space is shared with the network protocol; the encoding is not

**Every console setting is a TCLV parameter** — the same numeric id space carried by packet
type 8. [C] The correspondence, with the network writability from the vendor's own
descriptor table:

| console command | TCLV ids | writable over the network? |
|---|---:|---|
| `wifi_ssid_set <ssid>` / `wifi_conf_set` arg 1 | 65 | **no** |
| `wifi_psk_set <psk>` / arg 2 | 67 | **no** |
| `wifi_security_set <sec>` / arg 3 | 66 | **no** |
| `wifi_conf_set` arg 4 (band) | 68 | **no** |
| `wifi_eap_conf_em_set` / `_usr_set` / `_pwd_set` | 74 / 76 / 75 | no |
| (EAP certificate upload) | 57, 58, 59, 60–64 | write-only / read-only |
| **`server_tcp_set <ip\|dns> <port>`** | **18, 19** | **no** |
| `ipv4_conf_set <ip> <nm> <gw> <dns> <mode>` | 13–17 | no |
| `conn_type_set <type>` | 2 | **no** |
| `server_hb_set <time>` | 29 | **yes** |
| `mobile_conf_set <apn> <sec> <usr> <psk> <band>` | 69–73 | no |
| `conn_retry_set <time>` | 30 | no |
| `eth_conf_set <mac>` / `eth_conf_ext_set` | 8, 9, 10 | no |
| `battery_conf_set <off> <on> <cnt>` | 36, 37, 38 | yes |
| `accelerometer_conf_set <thr> <deb>` | 34, 35 | yes |
| `display_conf_set <vcom...> <type>` | 20–27, 28 | yes |
| `system_conf_set <batt_ind> <touch> <ship>` | 49, 50, 51 | yes |
| (soft sleep) | 52 | yes |
| `roaming_conf_set <mode> <thr> <hyst>` | 54, 55, 56 | yes |
| `frontlight_conf_*` | 39–46, 47, 48 | yes |
| `feat_enable` / `feat_disable <key>` | 77 / 78 | write-only |
| `ext_conf_set`, `heater_conf_set`, `scpu_*` | 103, 117, 1000–1021, 1300–1302 | mixed |
| **`flash_save`** | **53** | write-only |
| (table identity) | 0, 1 | read-only |

**So: one key space, two encodings.** The console speaks ASCII commands; the network speaks
TCLV records. The parameter id table in [parameters.md](parameters.md) is directly reusable
for the console — you just translate id → command name rather than id → uint16.

## Command inventory by area

162 documented commands. [C] Highlights beyond configuration:

- **Identity and introspection**: `help`, `uuid_get`, `gtin_get`, `fw_version_get`,
  `fw_checksum_get`, `cli_version_get`, `uptime`, `flash_print` (dumps all stored
  settings), `flash_load`, `status_get`, `pss`.
- **Radio diagnostics (CC3100 generation)**: `cc3100_scan`, `cc3100_rssi`,
  `cc3100_mac_address`, `cc3100_fw_version`, `cc3100_dns <url>`, `cc3100_pwr`,
  `cc3100_fw_upgrade`, `cc3100_format` *(destructive — formats the radio's SPI flash)*.
- **Power and lifecycle**: `app_sleep <min>`, `app_wakeup`, `autoconn <timeout>`,
  `reboot`, `sleep_conf_get`, `bsim` / `bsimv` (battery simulation).
- **Connectivity state**: `conn_type_list`, `conn_type_get`, `conn_state_get`,
  `cs <state>`, `conn_retry_get` / `_set`.
- **Hardware extensions**: `scpu_*` (the co-processor: front light, heater, power rails),
  `touch_*`, `frontlight_*`, `t2s_*` (text to speech), `play_music`.

### `pss` and `status_get` are the useful ones for protocol work

**The console can make the device emit a status packet over the network link on demand.**
[C] That decouples protocol work from the heartbeat interval entirely, which is worth
knowing before you spend an afternoon waiting 60 s between experiments.

### `conn_type_list` is the authority on connectivity types

The vendor's UI field map and the vendor's CLI documentation disagree about the value for
4G. Enumerate it on the device instead. [C]

## Commands that bite

### `sf_rdid` and `sf_rdst` kill the console

Measured, the hard way. [W] Both assert immediately — `assert: spi_flash_cli.c:139` and
`:188` — because neither checks that `sf_select` has chosen a device first. The assertion
kills the console task: the prompt never returns, and the software watchdog notices
(`W WD task timeout: cli_USB` appears on the UART) **without acting on it**.

The sign itself is fine throughout — it stays on the network, keeps its heartbeat and keeps
its picture — and the console comes back only when the device reboots on its own, roughly
25 minutes later.

Neither command reads or writes anything useful. **[I, untested]** `sf_select` first may
make them safe. `sf_list` is fine.

### The vendor's soft-sleep warning did not reproduce

The vendor documents a soft sleep that cuts the console after 15 seconds of accelerometer
inactivity, and recommends setting TCLV 52 to `1` ("CLI and CPU always on") for a working
session. [C]

**It did not bite on firmware 7.4.4407.** A console held its prompt across an undisturbed
45-second idle and answered immediately after, on a sign whose uptime had been 25 days — so
the console had been reachable that whole time, untouched. [W] A read-only tool therefore
needs no keepalive and should not touch TCLV 52, since doing so would be a write on an
otherwise read-only session.

### Do not run these

On hardware you care about: `cc3100_format`, `cc3100_fw_upgrade`, `touch_fw_update`,
`scpu_upgrade`, `fs_format`, `feat_disable`, or `system_conf_set` with shipping mode
enabled.

Remember that a bare call to a **nullary** command **executes** it, so the existence probe
above must never be pointed at one of these.

## Encryption over the console: not a way in

Worth stating because the commands exist and look promising.

- **`encryption_mode_set` takes 0 or 1.** That is solid: it is TCLV 130, whose vendor
  description reads *"Outbound encryption: 0=Disabled, 1=Enabled"*. [W][C] Note that is a
  **boolean** and **not** the wire protocol's `SecurityType` (0/2/3).
- **The key format is not solid.** The primitive is AES-128, so 16 bytes; the console takes
  one whitespace-delimited token, and the getter echoes it back in single quotes, so it is
  stored as a printable string. **[I]** 16 printable ASCII characters used verbatim is the
  best-supported reading, because every other key and IV in this system is exactly that.
  Base64 (24 characters) is second, because that is how the vendor server serialises such
  a key for escrow. **None of this has been tested.** [GAP]
- **And the conclusion that matters: setting a key over the console does not buy you
  self-hosted link encryption.** The vendor gateway encrypts with 16 `crypto/rand` bytes
  minted fresh per activation and **never transmits them**; it sends an escrow service's
  opaque response verbatim and expects the device to resolve it with what it already holds.
  TCLV 131 is that long-term secret, not the session key, and reproducing the resolution
  means reading firmware that ships encrypted. [D] See
  [overview.md](overview.md#1-protocolheadersecurity--aes-128-cbc-inside-the-stream).

**Use TLS instead.** TCLV 145 is *"TLS mode: 0=disabled, 1=TLS 1.3"*, it is
**network-writable** so no cable is needed, the gateway's TLS is opportunistic on the same
port, and there is no certificate pinning on the device link. Install a certificate first;
flipping 145 against a server with no certificate strands the device.

## What the console cannot give you

For the record, since this comes up: **there is no `mem_read`, `flash_read`, `peek` or dump
command** in the 162-command documented set. `flash_print` dumps stored *settings* (the
TCLV configuration), not flash contents, and `fw_checksum_get` returns a CRC, not data.
[C] The console yields configuration, not firmware. See
[firmware.md](firmware.md#where-the-key-is-not).

The one genuinely open question this channel could answer is whether an **undocumented**
memory or flash read exists in the shipped firmware — which is exactly why the
`help`-versus-reality gap above matters, and why 47 present-but-undocumented commands is
an interesting number.
