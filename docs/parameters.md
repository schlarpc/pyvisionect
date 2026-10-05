# Parameters: the TCLV configuration namespace (packet type 8)

TCLV is the device's own parameter namespace, stored in its flash. It is reachable two
ways — over the network as packet type 8, and over the USB serial console as ASCII
commands — and **the key space is shared between them while the encoding is not**. See
[usb-cli.md](usb-cli.md).

"TCLV" appears to stand for Type / Control / Length / Value, which is exactly the record
layout.

## Wire format

```
ParamHeader (8 bytes)
   0   4  Reserved        uint32 LE
   4   4  PayloadLength   uint32 LE   = sum over records of (4 + len(Value))

then, repeated:
ParamPayload (4 + N bytes)
   +0  2  Type     uint16 LE   the parameter id
   +2  1  Control  uint8       0 read, 1 write, 2 read error, 3 write error
   +3  1  Length   uint8       = len(Value), so a value is at most 255 bytes
   +4  N  Value    N bytes, untyped
```

Total packet size is `8 + sum(4 + len(Value_i))`, and `PayloadLength = total - 8`. [D]
Confirmed byte-for-byte in the capture. [W]

**The `Control` byte is the whole protocol.** [D]

| value | meaning | direction |
|---:|---|---|
| 0 | **read** | request, and also the reply to a successful read |
| 1 | **write** | request, and also the reply to a successful write |
| 2 | **read error** | reply only |
| 3 | **write error** | reply only |

A read *request* carries `Control = 0` and `Length = 0` — no value bytes. A read *reply*
carries `Control = 0` with the value filled in. [W]

**Values are untyped bytes.** The wire carries no type information: a numeric parameter
is a little-endian integer of whatever width the parameter happens to use, a port is a
`uint16`, and a string is bare ASCII **with no NUL terminator**. [W] The length is the
only framing you get.

> This is the most awkward part of the protocol to implement, because the type of each
> parameter is **not** on the wire and **not** in the vendor's descriptor tables either —
> those carry only id, name and writability. [D] The vendor server has per-id
> parse/format functions (below), which is the only machine-readable source for the
> value types, and it only covers a fraction of the ids.

### The server's per-id value codecs

Two maps in the vendor binary say how to convert between the textual value its API uses
and the raw bytes on the wire. A parameter absent from these maps is handled as raw
bytes. [D]

The **parse** map (text → bytes) has 53 entries:

| parameter ids | parser |
|---|---|
| 19–27, 36, 37, 113, 119, 120 | `u16` |
| 112, 114, 115 | `u8` |
| 39–46, 1002–1009 | front-light map (a PWM+LDR pair) |
| 108, 164, 166, 174, 175, 177, 178 | string |
| 134–137 | string, 128 bytes max |
| 1010–1013, 1016, 1017, 1019, 1020 | `u16` |
| 1022 | `u16` **[I]** — the 53rd entry's register was not resolved, but every neighbouring co-processor `u16` setting uses this parser and 1022 is a percentage |

The **format** map (bytes → text) has 30 entries: ids 39–46 and 1002–1009 as front-light
maps (1009 is **[I]**, same reason), and ids 60–66, 70, 71, 74–76, 170, 175 as strings.

Everything else is raw. In practice: assume little-endian unsigned integer of the length
the device returns, except for the ids listed as strings.

## The id table

235 entries, recovered from the legacy package's parameter-type map — a 169-entry static
array pair plus 66 individually assigned entries. Names are **verbatim vendor strings**
from the binary. [D]

### Bank 1: the main MCU (0–178)

| id | name |
|---:|---|
| 0 | TCLV Magic number |
| 1 | TCLV table version |
| 2 | Connectivity type |
| 3 | Wifi ACK or NACK timeout and Waiting for next block timeout |
| 4 | Wifi power saving timeout |
| 5 | WiFi DTIM skip interval in msec |
| 6 | Mobile ACK or NACK timeout and Waiting for next block timeout |
| 7 | Mobile power saving timeout |
| 8 | Ethernet MAC address |
| 9 | Ethernet TCP retry count |
| 10 | Ethernet TCP retry timeout [100ms] |
| 11 | Ethernet ACK or NACK timeout and Waiting for next block timeout |
| 12 | Ethernet power saving timeout |
| 13–16 | IPv4 static IP / Netmask / Gateway / DNS server |
| 17 | IPv4 mode: 0=Static IP, 1=DHCP |
| 18 | **Server IP** |
| 19 | **Server port** |
| 20–27 | VCOM of Display 0..7 |
| 28 | Display Type |
| 29 | **Heart beat interval** |
| 30 | Network error retry interval |
| 31–33 | Proximity: offset calibration / threshold in % / diode current in 10mA |
| 34, 35 | Accelerometer threshold count / debounce count |
| 36, 37 | Battery OFF threshold / Battery ON threshold |
| 38 | Battery threshold count |
| 39–46 | Front light map 0..7: PWM+LDR |
| 47 | Front light threshold |
| 48 | Front light server control |
| 49 | System screens: 1=Battery, 2=Not connected, 3=Battery+Not connected |
| 50 | Touch mode: 0=OFF, 1=ON, 3=ON+Beep |
| 51 | Shipping mode: 0=OFF, 1=ON |
| 52 | Sleep mode disable: 0=Sleep mode enabled, 1=Sleep mode disabled |
| 53 | **Command to save parameters to flash** |
| 54–56 | Roaming mode / threshold in dBm / hysteresis in dBm |
| 57–59 | Writes EAP certificate chunk / descriptor; Erases EAP certificate |
| 60–64 | Certificate entry 0..4 |
| 65 | **WiFi SSID** |
| 66 | **WiFi security mode** |
| 67 | **WiFi password** |
| 68 | **WiFi band: 0=dual, 1=2.4GHz, 2=5GHz** |
| 69 | Mobile APN string |
| 70 | **Mobile security mode** |
| 71 | Mobile username |
| 72 | Mobile password |
| 73 | Mobile: not used. Set to 0 |
| 74–76 | WiFi EAP method / password / username |
| 77, 78 | Enable feature / Disable feature |
| 79 | Write: Play RTTTL song [text]. Read: play status: 0=Idle, 1=Playing |
| 80 | Force connection establish. Read requested connection status |
| 81 | Read connection status |
| 82 | Application name |
| 83 | Connectivity support: bit0=Wifi, bit1=Ethernet, bit2=Mobile |
| 84 | Connectivity drivers: see CLI command `conn_type_list` |
| 85 | Disable connectivity for N minutes |
| 86 | HW version: ID, Major, Minor, BOM |
| 87 | Application version: Major, Minor, Revision |
| 88 | Bootloader version: Major, Minor, Revision |
| 89 | Device UUID |
| 90 | Application ready status: 0=Not ready, 1=Ready |
| 91 | Reboot device, optional argument: 1 = upgrade, 0 = boot |
| 92 | Jump to application |
| 93 | Connectivity FW version |
| 95 | Touch FW version |
| 96 | If 1, device successfully sent status packet to server, 0 otherwise |
| 97, 98 | Frontlight LDR / PWM filter coefficient |
| 99 | Front light sensor timeout |
| 100–102 | Heater low / high temperature limit, Heater operation mode |
| 103–106 | Extension battery threshold / min temp / max temp / operation mode |
| 107 | Used (compiled) display type |
| 108 | GTIN |
| 109 | EPD border mode: 0=server, 1=white, 2=black |
| 110 | WiFi MAC |
| 111 | T2S enable |
| 112–118 | T2S speech voice / rate / volume / timeout, T2S speak, External battery used for battery screens, EPD temperature limit mode |
| 119, 120 | Temp high / Temp low |
| 121 | Displays preloaded local image |
| 122, 123 | SNTP server address / port |
| 124, 125 | HTP server URL / port |
| 126 | Sets CLI password |
| 127 | Resets CLI password and all security related settings |
| 128 | CLI login |
| 129 | CLI security state: 1=unlocked, 0=locked |
| **130** | **Outbound encryption: 0=Disabled, 1=Enabled** |
| **131** | **Encryption key** |
| 132 | Mobile connection IMEI |
| 133 | Executes application upgrade |
| 134–137 | T2S button 1..4 text |
| 138 | BLE MAC |
| 139 | Performs WiFi scan |
| 140 | BLE advertising data |
| 141, 142 | MPPT: VFB / MPPT voltage [mV] |
| 143 | Executes WiFi module upgrade |
| 144 | Set WiFi region: 0=Default, 1=US, 2=EUROPE, 3=JAPAN |
| **145** | **TLS mode: 0=disabled, 1=TLS 1.3** |
| 146 | EPD count |
| 147 | Panel orientation: 0=Portrait, 1=Landscape |
| 148 | Panel alignment: 0=Vertical, 1=Horizontal |
| 149 | Panel rotation: 0=No rotation, 1=180deg rotation |
| 150 | Panel update order: 0=Ascending, 1=Descending |
| 151 | Restart JS engine |
| 152, 153 | Display width / height in pixels |
| 154 | Mobile preferred mode: 0=Disabled, 2=Auto, 38=LTE only, … |
| 155 | BLE mode: 0=Disabled, 1=Enabled |
| 156 | EPD temperature |
| 157 | Format the file system |
| 158 | Reads the 16-byte wall-mount ID |
| 159 | Sets the PoE wall-mount LEDs |
| 160 | JS engine status |
| 161 | Flushes the syslog to the file system |
| 162 | Halt JS app execution |
| 163 | Sends device to sleep mode |
| 164 | JSE eval |
| 165 | Reads Wall-mount HW version: ID, Major, Minor, BOM |
| 166 | Changes the server URL and/or port: `[Server URL]:[Server port]` |
| 167 | Connectivity link type: 0=None, 1=WiFi, 2=Mobile, 3=Ethernet |
| 168 | Executes JS application patch upgrade |
| 169 | Cleans the file system of any previous JS upgrades |
| 170 | Read BSSID of the currently connected AP |
| 171 | Set M2M mode for N minutes (0 to disable) or read remaining M2M time |
| 172 | Sets battery charging mode: 0 = Normal (default), 1 = Charging disabled |
| 173 | Set WiFi mode: 0=Default, 1=Disable broadcast filtering, 2=enable Enhanced max PSP, 3=both |
| 174 | Opens a file for reading `[filename]:[r]` or writing `[filename]:[w]` |
| 175 | Reads/writes file data |
| 176 | Closes an opened file |
| 177 | Returns an S-box hash over a file's content and the file size, for `[filename]` |
| 178 | Removes a file from the file system, specified with `[filename]` |

### Bank 2: the SCPU (1000–1303)

The sensor / front-light co-processor.

| id | name |
|---:|---|
| 1000 | TCLV table version (SCPU bank) |
| 1001 | SCPU Frontlight Mode |
| 1002–1009 | SCPU Front light map 0..7: PWM+LDR |
| 1010–1013 | SCPU Frontlight PWM frequency in Hz / samplerate in sec / prefilter / postfilter |
| 1014 | SCPU Frontlight sensor timeout in sec |
| 1015 | SCPU Heater Mode |
| 1016, 1017 | SCPU Heater OFF / ON temperature in C |
| 1018 | SCPU Device Mode |
| 1019, 1020 | SCPU Minimal / Maximal OFF temperature in C |
| 1021 | SCPU Battery threshold in mV |
| 1022 | SCPU Frontlight PWM limit in % |
| 1300–1303 | Command to push / pull / save parameters to SCPU; Reboots the SCPU |

### Bank 3: the DPU (2000–2303)

The display power unit — the e-ink rail generator.

| id | name |
|---:|---|
| 2000 | TCLV table version (DPU bank) |
| 2001 | DPU mode |
| 2002–2009 | DPU VSPOS / VSNEG / VGPOS / VGNEG / VCOM1..VCOM4 rail voltage in mV |
| 2010–2017 | DPU sequence 0..7 ON: delay in ms + rail ID |
| 2018–2025 | DPU sequence 0..7 OFF: delay in ms + rail ID |
| 2300–2303 | Command to push / pull / save parameters; Reboots the unit |

(Ids 2300–2303 carry the same descriptive strings as 1300–1303, naming the SCPU. **[I]**
a vendor copy-paste; in context they must act on the DPU.)

## The read-only set, and why USB provisioning is unavoidable

**This is the single most important fact for bootstrapping a sign onto your own server.**

Eight parameters are marked `canWrite: false` in the vendor's own descriptor table. [C]
The vendor server can **read** them with `Control = 0` but will never write them with a
type-8 packet.

| id | name | the USB command that *can* write it |
|---:|---|---|
| 2 | Connectivity type | `conn_type_set <type>` |
| 18 | **Server IP** | `server_tcp_set <ip\|dns> <port>` |
| 19 | **Server port** | `server_tcp_set <ip\|dns> <port>` |
| 65 | **WiFi SSID** | `wifi_ssid_set <ssid>` |
| 66 | **WiFi security mode** | `wifi_security_set <none\|wpa2\|wpa2e>` |
| 67 | **WiFi password** | `wifi_psk_set <psk>` |
| 68 | WiFi band | `wifi_conf_set` argument 4 |
| 70 | Mobile security mode | `mobile_conf_set` argument 2 |

That read-only set is **exactly the network and bootstrap configuration**. There is no
chicken-and-egg escape: a device cannot be told which WiFi network or which server to use
over the network it does not yet have. You cannot move a sign to a new WiFi or a new
server over the air.

The read side genuinely works. A live read of ids 18, 19, 65, 66 and 2 returned the
server hostname, the port, the SSID, `"wpa2"` and `6` straight off the device. [W]

**There is one way around it**, and it needs no cables: if the device already holds a
**hostname** rather than a literal IP in parameter 18, re-point that name at your own
listener. Read what it currently holds with a parameter read — only *writes* are blocked.

Other parameters marked read-only in the vendor's per-firmware tables, beyond the eight
above: 4, 7, 12 (`*_PS_TIMEOUT`, the power-save entry timeouts), 8–10 (Ethernet), 13–17
(IPv4 static config), 30 (network retry interval), 60–64 (certificate entries), 69, 71–76
(the remaining mobile and EAP credentials), 0 and 1 (table identity). [C]

Note the asymmetry: the protocol **ACK timeout** (ids 3, 6, 11) is writable, while the
**power-save entry timeout** (ids 4, 7, 12) is not. [C]

## What a device actually supports is narrower than this table

The vendor server does not offer every id to every device. It picks a descriptor table by
a key built from the device's own reported identity: [D]

```
fw  = "<FirmwareMajor>.<FirmwareMinor>.<FirmwareRevision>"
hw  = "<HardwareMajor>.<HardwareMinor>.<HardwareRevision>"
key = "<hw>-<HardwareNameID>-<fw>"
table = tclvTables[key]          // falling back to tclvTables["generic"]
```

and then filters the chosen table's entries by `HardwareNameID`. The reference device's
key has no entry, so it gets the generic table minus the front-light entries, 44 ids. [C]

Separately, the device itself advertises a support list, which the vendor server exposes.
On the reference firmware that list covers **ids 0–53**. [W]

> **That advertised list is not exhaustive.** Reads of ids 65 and 66 succeeded even
> though both are absent from it. [W] Do not use it as an allow-list.

## Parameters that matter for power and timing

Semantics from the vendor's own descriptor text. [C]

| id | name | semantics |
|---:|---|---|
| 29 | `HEARTBEAT` | *"how long before device sends a status packet after last communication with the server (**minutes**)"*. Network-**writable**. |
| 52 | `SLEEP_MODE` | power-saving mode. Writable. The published values are `0` = CLI wakes on accelerometer, `1` = CLI and CPU always on, `2` = CLI disabled when asleep. Note the parameter table's own name for it is "Sleep mode **disable**", which reads as a boolean — the two descriptions disagree. **[GAP]** |
| 51 | `SYS_SHIP_MODE` | shipping mode: the deepest off state. `0` = disabled, `1` = enabled. **Do not set this remotely on a sign you cannot physically reach.** |
| 5 | `WIFI_DTIM` | DTIM skip interval in **ms** — *"Time until module wakes up and checks for a DTIM"*. The reference device reports 100, which is why its ping RTT is ~100 ms. |
| 4, 7, 12 | `*_PS_TIMEOUT` | *"Timeout before transitioning to power save mode (msec)"*. **Read-only.** |
| 30 | `NET_RETRY_INT` | retry interval in **minutes**. **The device reboots if it still cannot reconnect.** Read-only. |
| 36, 37, 38 | `BATT_THR_OFF` / `ON` / `CNT` | **mV** thresholds for showing the charge screen and for resuming operation, plus the measurement count before a bootloader reboot. Writable. |
| 3, 6, 11 | `*_PV2_TIMEOUT` | the protocol **ACK/NACK timeout in ms**. Writable. |
| 55 | `ROAMING_THR` | *"Roaming threshold in dBm … if AP RSSI below this value"*. Writable. Corroborates the [RSSI sign convention](status.md#rssi-tag-13-is-a-magnitude). |
| 53 | `CMD_FLASH_SAVE` | **persist the parameter set to flash.** Write-only command. |

## Nothing persists without an explicit save

Verbatim from the vendor documentation: [C]

> All changes to device configuration are by default retained only in the working RAM. If
> you want your settings to persist across reboots, you will need to run `flash_save`
> before rebooting.

Parameter **53** is that command over the network; `flash_save` is the same thing over
the serial console. This cuts both ways, and the second direction is the useful one:
**until you save, a power cycle undoes everything**, which makes most parameter
experiments safely reversible.

## Sensible client-side defaults

The vendor's own client uses these, which are a reasonable starting point: [C]

| knob | value |
|---|---|
| max parameters per request | 5 |
| read timeout | 5 s |
| write timeout | 10 s |
| read retries | 1 |
| write retries | 2 |

## Worked example: a parameter read round trip

Captured, with the server hostname and SSID replaced by placeholders of different length,
so the lengths below are recomputed to stay self-consistent. The structure is verbatim.

**Request** (server → device, carried in an LZ4 block; shown inflated):

```
 DataHeader: Type=8 (param) ID=0x3FA3A4F1 Length=32
 payload:
   00 00 00 00   Control       = 0  (read)
   18 00 00 00   PayloadLength = 24 (6 x 4)
   1d 00 00 00   id 29  Heart beat interval       Control=0 Length=0
   12 00 00 00   id 18  Server IP
   13 00 00 00   id 19  Server port
   41 00 00 00   id 65  WiFi SSID
   42 00 00 00   id 66  WiFi security mode
   02 00 00 00   id  2  Connectivity type
```

Note the on-wire header here is **8 bytes**, not the 12 bytes of the vendor's Go struct —
`Reserved` is simply not emitted. Counted exactly: 32 − 24 = 8. [W] This was the precedent
that made the command packet (type 2) suspect — **and type 2 turned out not to follow it**:
captured on 2026-10-05, its header really is 12 bytes with `Reserved` emitted. See
[packets.md](packets.md#packet-type-2-command-is-unverified). The truncation is a property
of the param packet, not of the protocol.

The device **first acks the request** with a control packet carrying the same ID, then
~200 ms later sends its reply as a packet of its own, using its own ID counter: [W]

```
 DataHeader: Type=8 (param) ID=1311 Length=76
 payload:
   00 00 00 00   Control       = 0 (read)
   44 00 00 00   PayloadLength = 68
   1d 00 00 04   {id=29, Control=0, Length=4}
     01 00 00 00                        -> 1  (minutes)
   12 00 00 12   {id=18, Control=0, Length=18}
     "server.example.com"               -> no NUL terminator
   13 00 00 02   {id=19, Control=0, Length=2}
     69 2b                              -> 0x2b69 = 11113   (a uint16, not a uint32)
   41 00 00 0c   {id=65, Control=0, Length=12}
     "example-wifi"
   42 00 00 04   {id=66, Control=0, Length=4}
     "wpa2"                             -> an ASCII string, NOT an integer
   02 00 00 04   {id=2,  Control=0, Length=4}
     06 00 00 00                        -> 6  (WiFi, the TI CC3100 driver)
```

Three things to take from this: the port is a **uint16** while the heartbeat is a
**uint32**; the WiFi security mode is an **ASCII string**, not an enum ordinal; and the
device answers a request as a new packet rather than inline, so a server has to correlate
by id.

## Enum values worth having

From the vendor's admin UI field map. [C]

| parameter | values |
|---|---|
| 2 `CONN_TYPE` | `0` = no connectivity, `1`/`4`/`5`/`6` = WiFi (different radio drivers), `2` = 3G, `3` = Ethernet, `7` = end marker. The reference device is `6`. The vendor's CLI documentation shows `8` for a 4G example, which is past the UI's table — **[I]** firmware-dependent; enumerate with the console's `conn_type_list`. |
| 66 `WIFI_SECURITY` | `none` = open, `wpa2` = WPA2, `wpa2e` = WPA2 Enterprise. **ASCII strings, not integers.** |
| 17 `IPV4_MODE` | `0` = static IP, `1` = DHCP |
| 50 `SYS_TOUCH_EN` | `0` = off, `1` = on, `3` = on + beep |
| 51 `SYS_SHIP_MODE` | `0` = disabled, `1` = enabled |

## The device UUID is not in this table

There is no setter. Parameter 89 reads the UUID; the console's `uuid_get` likewise reads
it. **[I]** The UUID is factory-programmed — on the reference device its leading bytes
have the shape of a TI device identifier and the middle bytes are the ASCII digits of the
unit's serial number. The GTIN (parameter 108) is likewise read-only in practice. [C]

## Parameters 130, 131 and 145: the security trio

Grouped here because they are easy to confuse with each other and with
`ProtocolHeader.Security`.

- **130, "Outbound encryption: 0=Disabled, 1=Enabled"** — a boolean. The serial console's
  setter takes 0 or 1. [W] **This is not the wire protocol's `SecurityType`**, which is
  0/2/3. Turning the vendor's link encryption *off* is exactly what the gateway does: it
  writes `{Type=0x0082, Control=1, Length=4}` with four zero value bytes. [D]
- **131, "Encryption key"** — the device's **long-term** secret, not the session key. The
  gateway mints a fresh random session key per activation and never transmits it; it
  sends an escrow service's opaque response and expects the device to resolve it using
  what it already holds. So **setting this does not buy you self-hosted link
  encryption.** [D] The console takes one whitespace-delimited token and echoes it back
  in single quotes, so it is stored as a printable string; **[I]** 16 printable ASCII
  characters used verbatim is the best-supported reading, because every other key and IV
  in this system is exactly that. Untested. [GAP]
- **145, "TLS mode: 0=disabled, 1=TLS 1.3"** — **network-writable**, and the one that
  actually works. See [overview.md](overview.md#2-tls-on-the-same-port--opportunistic-and-usable).
