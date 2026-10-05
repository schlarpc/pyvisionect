# Status reporting (packet type 3)

The status packet is the device's heartbeat, its identity claim and its entire telemetry
surface. It is the **first** packet of every connection, and the gateway rejects a
connection whose first packet is anything else. [D]

## The encoding is a flat tag/value stream, not CBOR

This is the single most important correction to the "everything in this protocol is CBOR"
assumption. The Go `Status` struct carries `cbor:"N,keyasint"` tags, and those integer
keys *do* equal the on-wire tags — but the encoder is hand-written, with
`write` / `writeInt` / `writeValue` / `writeMultiValue` methods and no CBOR library
involved anywhere in the path. [D]

```
+---------------------------------------------------------------+
|                      Tag   (uint32 LE)                        |
+---------------------------------------------------------------+
|                      Value (uint32 LE)                        |
+---------------------------------------------------------------+
    ... repeated, one 8-byte record per present field ...
+---------------------------------------------------------------+
|                      0xFFFFFFFF  (sentinel tag)               |
+---------------------------------------------------------------+
|                      <value>                                  |
+---------------------------------------------------------------+
```

The encoder is literally: [D]

```go
func (e *Encoder) write(tag uint32, v *uint32) error {
    if v == nil { return nil }                    // absent fields are OMITTED
    binary.LittleEndian.PutUint32(buf[0:], tag)
    binary.LittleEndian.PutUint32(buf[4:], *v)
    return e.flush(8)
}
```

Four rules follow, and all four are easy to get wrong:

1. **Every value on the wire is a `uint32`.** All typing, units, signedness and scaling
   are applied *above* the codec. There is no type information in the stream.
2. **Absent fields are omitted entirely.** A status is a *sparse* list — 62 records out
   of ~104 possible tags in the real capture. [W] **An absent key does not mean zero**;
   it means the device does not have that sensor or has nothing to report.
3. **Multi-word values occupy consecutive tags.** A 6-byte BSSID takes two tags; a
   version triple takes three; a string takes as many as it needs. This is exactly why
   the tag numbering has gaps.
4. **The stream ends with a sentinel record whose tag is `0xFFFFFFFF`.** A parser should
   stop there. The vendor's v1 name table calls that tag `LastStatus`, but the server's
   `LastStatus` field is its own receive timestamp and never travels on the wire, so the
   name is misleading. [D] In the capture the sentinel's value was `0x779050df`, which is
   not a plausible Unix time and changes with the payload contents. **[I]** a nonce or a
   checksum over the records. The server carries it verbatim and never interprets it. [D]

Unknown tags are preserved by the server into a `map[uint32]uint32`, so a device may send
tags this table does not list. [D]

## Scalar value codecs

From the per-type `UnmarshalUint32` / `MarshalUint32` methods. [D]

| type | encoding |
|---|---|
| plain `uint32` | the value verbatim |
| temperature | **low byte of the u32 reinterpreted as a signed int8** = degrees C, no scaling. `0xFF` and `0xFFFFFFFF` mean "no reading". |
| `HardwareID` (tag 2) | `bits 31..24 = ID`, `23..16 = Family`, `15..0 = PCB`. `0xFFFFFFFF` = absent. |
| version triple | three consecutive tags: +0 Major, +1 Minor, +2 Revision |
| touch version | two consecutive tags: +0 Major, +1 Minor |
| feature bitmask (tag 43) | one u32, bit *i* → field *i*. See [the feature bits](#the-feature-bitmask-tag-43). |
| sensor-status bitmask (tag 44) | one u32, bits 0..7 = SCPUTemperature, ExternalTemperature, Humidity, Pressure, SCPUVoltage, ExternalVoltage, ExternalLDRVoltage, ExternalUserSensorVoltage |
| BSSID (tags 91–92) | 6 bytes over 2 tags, little-endian: tag+0 = bytes 0..3, tag+1 = bytes 4..5 (high 16 bits unused). Empty → `0xFFFFFFFF`, count 1. |
| string (GTIN, JS app id) | NUL-terminated ASCII, **4 chars per tag, little-endian**, consecutive tags until a NUL byte appears in a word. Trailing bytes in the last word are `0xFF` padding. GTIN gets tags 50–53 (16 chars max); JS app id gets 76–85 (40 chars max). |

### Sentinel values

| value | meaning |
|---|---|
| `0xFFFFFFFF` (4294967295) | **field absent / not available.** Appears as a *value*, on fields whose codec treats it as a no-op. |
| `999` | the same meaning, but only in the vendor server's *weekly aggregate* view, which is a different, numeric, smaller key set. Not a wire value. |

Guard against both if you are consuming the vendor server's API; on the wire only the
first matters.

## The tag table

103 named tags plus the sentinel, recovered from the static tag→name array pair in the
legacy package and cross-checked against the v2 struct's `cbor` keyasint numbers, which
match 1:1 for tags 0..103. [D] Names are verbatim vendor strings.

Confidence: every tag below is read directly from the static array, **except 91/92**,
whose BSSID meaning is an **[I]** inference from the gap in the name table plus BSSID
being a known two-word field. (The inference is well supported: decoding tags 91/92 with
that rule reproduces the MAC the vendor API reports, exactly.) [W]

| tag | name | type / unit | notes |
|---:|---|---|---|
| 0 | `ConnectReason` | enum | why the device dialled in — [table](#connectreason-tag-0) |
| 1 | `ErrorCode` | enum | **the reason this packet was sent, not a fault** — [table](#errorcode-tag-1-is-a-reason-not-a-fault) |
| 2 | `HardwareID` / `BootloaderVersion` | packed | `ID<<24 \| Family<<16 \| PCB`. The vendor API publishes this under the name `BootloaderVersion`, **which is wrong** — see [the mislabel](#the-bootloaderversion-mislabel) |
| 3 | `FirmwareID` / `ApplicationVersion` | uint32 | the firmware image's CRC-32. The vendor API prints it **base 16**. |
| 4 | `WiFiModuleId` | enum | same enum as tag 6 |
| 5 | `MobileModuleId` | enum | `0xFFFFFFFF` = no cellular module |
| 6 | `ConnectivityUsed` | enum | [table](#connectivityused-tags-4-and-6) |
| 7 | `NumSupportedDisplays` | count | **a firmware hint, not a hard limit** — the reference device reports 2 while driving 4 configured displays |
| 8 | `DisplayIds` / `DisplayType` | **opaque uint32** | the panel identity. **Not an enum** — see [imaging.md](imaging.md#the-panel-id-is-an-opaque-uint32) |
| 9 | `DisplayStateCRC` | uint32 | the tag of the last image the device successfully applied — see [imaging.md](imaging.md#the-state-checksum) |
| 10 | `Battery` | **percent 0–100** | but see the [ADC caveat](#battery-is-usually-a-percentage-and-sometimes-not) |
| 11 | `Charger` | enum | `0` pre charge, `1` fast charge, `2` done, `3` disabled |
| 12 | `Temperature` | **signed int8 °C** in the low byte | |
| 13 | `RSSI` | **dBm magnitude, sign dropped** | see [the RSSI trap](#rssi-tag-13-is-a-magnitude) |
| 14 | `ExternalBattery` | mV | |
| 15 | `Uptime` | **minutes** | see [the minutes trap](#uptime-and-nextstatus-are-minutes) |
| 16–18 | `Firmware` Major/Minor/Revision | version triple | |
| 19–21 | `Bootloader` Major/Minor/Revision | version triple | the real bootloader version, unlike tag 2 |
| 22 | `HardwareNameID` | uint32 | the hardware model id |
| 23–25 | `Hardware` Major/Minor/Revision | version triple | |
| 26 | `HardwareFirmwareIface` | uint32 | a hardware/firmware ABI compatibility level |
| 27 | `NextStatus` | **minutes** | how long until the device will next report. Device-owned. |
| 28 | `ExternalTempSensor` | °C | |
| 29 | `TouchType` | enum | `0` none, `1` CB060E, `2` EKTF2227 |
| 30–31 | `TouchFirmware` Major/Minor | touch version | reported only; no firmware type exists to update it |
| 32 | `TouchEventCount` | counter | |
| 33 | `AcceleromenterEventCount` | counter | **the vendor's typo, preserved on the wire.** Spell it wrong. |
| 34 | `BatteryVoltage` | **mV** | |
| 35 | `BatteryCurrent` | **unsigned mA** | not the same signedness as tags 94/97 |
| 36 | `EventQueueOverFlow` | counter | |
| 37 | `PV2GPSState` | uint32 | |
| 38 | `DisplayWidth` | px | the **composite** surface as the device's controller sees it |
| 39 | `DisplayHeight` | px | ditto — need not match a server-side display list |
| 40 | `ProtocolVersion` | uint32 | what the device says it speaks. `3` here. |
| 41 | `HumiditySensor` | %RH | |
| 42 | `PressureSensor` | hPa | |
| 43 | `PV2Features` | **10-bit mask** | [table](#the-feature-bitmask-tag-43) |
| 44 | `SCPUSensorStatus` | 8-bit mask | see the codec table above |
| 45 | `EPDTempSensor` | °C | **panel** temperature — this is what selects the e-ink waveform |
| 46 | `ButtonCount` | counter | specifically the **front-light** button, distinct from tags 65–68 |
| 47 | `MobileLinkType` | uint32 | `0xFFFFFFFF` = n/a |
| 48 | `PV2FLSensorValue` | raw LDR | front-light ambient sensor |
| 49 | `SCPUVMSensorValue` | mV | |
| 50–53 | `GTIN1`..`GTIN4` | **ASCII string** | 13-digit GS1 barcode, 4 chars per tag |
| 54 | `ImagePushAllowed` | bool as uint32 | |
| 55 | `RtcGmtEpoch` | unix seconds | the device's RTC |
| 56 | `MobSecurity` | uint32 | **cellular APN auth mode, not transport security.** See [overview.md](overview.md#3-mobsecurity--not-transport-security-at-all) |
| 57–59 | `JsApiVersion` Major/Minor/Revision | version triple | on-device JS engine |
| 60 | `FsTotalSize` | **bytes** | |
| 61 | `FsFreeSize` | **bytes** | |
| 62 | `JseStatus` | uint32 | JS engine status |
| 63 | `ProximityCal` | uint32 | proximity calibration |
| 64 | `HitDetector` | uint32 | |
| 65–68 | `Button1Count`..`Button4Count` | counters | |
| 69 | `BLEState` | uint32 | |
| 70 | `ProximityCount` | counter | |
| 71 | `NetworkErrorCount` | counter | |
| 72–74 | `JsAppVersion` Major/Minor/Revision | version triple | |
| 75 | `WallMountCount` | counter | wall-mount detach events |
| 76–85 | `JsAppID1`..`JsAppID10` | **ASCII string** | 40 chars max |
| 86 | `JsAppType` | enum | `0` none, `1` TC, `2` AC |
| 87 | `McuAwakeCount` | counter | times the MCU left low power. Rose ~4/minute on the reference device. [W] |
| 88 | `DcmState` | enum | `0` off, `1` on. DC/DC converter mode power-rail state. |
| 89 | `WiFiDtim` | **ms** | DTIM skip interval — how long the radio sleeps between beacon checks |
| 90 | `ProximityThreshold` | uint32 | |
| 91–92 | `BSSID` | **6 bytes over 2 tags** | **[I]** the only inferred name in this table |
| 93–95 | `BatterySOC1` Voltage / Current / Percent | mV / **signed mA** / % | smart-battery gauge 1 |
| 96–98 | `BatterySOC2` Voltage / Current / Percent | mV / **signed mA** / % | smart-battery gauge 2 |
| 99 | `DisplayUpdateCount` | counter | lifetime panel refreshes. Nothing in the server reads it; it is a wear metric. |
| 100–102 | `ConnectivityVersion` Major/Minor/Revision | version triple | radio firmware. The Revision word is an opaque 32-bit build id, not a decimal revision. |
| 103 | `QSPIFlashID` | JEDEC RDID | `0x9D7020` IS25WP512MG, `0x9D701A` IS25WP512M, `0x9D7019` IS25WP256D (`0x9D` = ISSI), else unknown |
| `0xFFFFFFFF` | *(sentinel)* | — | **end of stream.** Value is uninterpreted; **[I]** a nonce or checksum. |

Tags 2 and 3 are named differently by the two versions of the vendor library: the legacy
table calls them `BootloaderVersion` and `ApplicationVersion`, the v2 struct calls them
`HardwareID` and `FirmwareID`. The v2 names describe the contents correctly; the legacy
names are what the vendor API surfaces. [D]

## Gotchas, in the order they will bite you

### `Uptime` and `NextStatus` are minutes

Not seconds. **Proven live**: sampled across status packets exactly 60 s apart, `Uptime`
advanced by exactly 1 each time. [W] The captured value 35793 is ~24.9 days, consistent
with the device's history.

`NextStatus` likewise: the reference device reported `1`, and status packets arrived 60 s
apart. [W] It is a **device-side** timer — the device, not the server, owns the schedule.

### `RSSI` (tag 13) is a magnitude

The value is a **dBm magnitude with the sign dropped**. There is no conversion anywhere
in the server's Go code — the field is a plain `*uint32` passed straight through. The sign
is re-applied client-side, in the admin UI's JavaScript: [D]

```js
var strength = parseInt(rssi);
// some connectivity modules can return positive value for rssi
if (strength > 0) { strength = -1 * strength; }
return strength;
```

So the reference device's reported `44` means **−44 dBm**. [W] Corroborated two ways: the
roaming-threshold parameter's own description says *"Roaming threshold in dBm … if AP
RSSI below this value"*, and the vendor's weekly aggregates show values of 56–75 in worse
weeks, i.e. −56…−75 dBm. [D]

Note the vendor's own comment says *some* modules return a positive value. The only safe
rule is **take the magnitude and apply a minus**.

### `ErrorCode` (tag 1) is a reason, not a fault

26 values. **This field says why the device sent this status packet.** Both `0` and `1`
mean "nothing wrong". [D]

```
 0 no error                 13 deep sleep request
 1 none                     14 connectivity start
 2 command pending          15 internal image update
 3 image received           16 gps control
 4 wakeup                   17 connectivity driver error
 5 connectivity error       18 button pending
 6 pv2 error                19 reboot
 7 heartbeat                20 local image
 8 connectivity changed     21 proximity
 9 image transfer pending   22 status acked
10 touch pending            23 file transfer pending
11 gps pending              24 file transfer done
12 status send              25 server change
```

Anything else renders `n/a`. **`13 = deep sleep request` is how a device announces that
it is about to sleep** — probably the single most useful value in the table for a server
that wants to get work in before the device goes away.

The vendor API prints this field **base 16**, which is a trap if you are parsing that
rather than the wire. [D]

### `Battery` is usually a percentage, and sometimes not

The server's status decoder contains: [D]

```go
if status.BatteryLevel != nil && *status.BatteryLevel > 102 {
    // treat it as a raw 12-bit ADC count and convert
    v := float64(*status.BatteryLevel) / 4096.0 * 3.3
    *status.BatteryLevel = round(interpolate(v, CapacityTableLiIon))
}
```

So **a value ≤ 102 is already a percentage and passes through untouched; anything larger
is a raw 12-bit ADC count** against a 3.3 V reference, converted through an 11-point
Li-ion discharge curve by linear interpolation (clamped at both ends, no default row).
The table's domain is exactly the 12-bit/3.3 V window: ADC 2170 → 1.748 V → 0%, ADC
2603 → 2.098 V → 100%. [D]

The curve, if you want to reproduce it: [D]

| % | V | | % | V |
|---:|---|---|---:|---|
| 0 | 1.74825 | | 60 | 1.90800 |
| 10 | 1.82310 | | 70 | 1.92780 |
| 20 | 1.83810 | | 80 | 1.97295 |
| 30 | 1.84815 | | 90 | 2.02290 |
| 40 | 1.86315 | | 100 | 2.09790 |
| 50 | 1.88805 | | | |

The reference device reports `Battery = 100` directly, so this path was never exercised
live. [W]

### Signedness is not uniform

- `Temperature` (12), `ExternalTempSensor` (28), `EPDTempSensor` (45): **signed int8** in
  the low byte.
- `BatteryCurrent` (35): **unsigned** mA.
- `BatterySOC1Current` (94), `BatterySOC2Current` (97): **signed int32** — negative while
  discharging.

Do not share a parser between 35 and 94/97. [D]

### The `BootloaderVersion` mislabel

The vendor API publishes a key called `BootloaderVersion` whose value is **not a version**.
It is built like this: [D]

```go
key := fmt.Sprintf("%d.%d.%d-%d", Hardware.Major, Hardware.Minor,
                   Hardware.Revision, HardwareNameID)
if friendly, ok := hardware.PandaFlavor[key]; ok { out = friendly } else { out = key }
```

That is to say: it reads the **hardware** version triple and the hardware name id, looks
the pair up in a marketing-name table, and falls back to the raw lookup key when there is
no match. The reference device's `"1.1.0-8"` is an unmatched lookup key. The real
bootloader version is tags 19–21. [D]

`PandaFlavor` covers `HardwareNameID` 1, 3, 4, 5, 6 and 7 only — 13 rows, and the
reference device's id 8 has no row anywhere in the vendor suite. Another server component
compares this same field against the literal string `"Visionect System Board V1.00"`.
**Treat the server's labels as unreliable and use the numeric fields.** [D]

### The feature bitmask (tag 43)

One uint32, ten bits, unpacked bit-for-bit by ten `BTL`/`SETB` pairs. The struct field
order equals the `cbor` keyasint order, so **bit *i* is field *i***: [D]

| bit | mask | field |
|---:|---|---|
| 0 | `0x001` | `RemoteBootloaderUpgrade` |
| 1 | `0x002` | `TouchDisabled` |
| 2 | `0x004` | `WPA2EnterpriseDisabled` |
| 3 | `0x008` | `FileSystem` |
| 4 | `0x010` | `DisplayScan42` |
| 5 | `0x020` | `NoFading` |
| 6 | `0x040` | `FastBoot` |
| 7 | `0x080` | `JSPatchUpdate` |
| 8 | `0x100` | `DoubleBuffer` |
| 9 | `0x200` | `WiFiOTA` |

The reference device reports `585 = 0x249` = bits 0, 3, 6, 9 ⇒ remote bootloader upgrade,
a filesystem, fast boot, WiFi OTA; touch enabled, WPA2-Enterprise available, display
scan 42 off, fading enabled, single-buffered. [W] That is self-consistent with everything
else it reports: the filesystem bit matches non-zero `FsTotalSize`/`FsFreeSize`, and the
remote-bootloader bit matches the server having enabled automatic bootloader flashing for
it.

> **A server-side bug worth knowing about.** The vendor's **legacy** parser loops
> `for mask := 1; mask <= 0x100; mask <<= 1` — it **stops at bit 8 and silently drops
> bit 9** (`WiFiOTA`, value 512), into a struct that has only nine booleans. Two of the
> vendor's own components use that parser, so they see `585` as if it were `73`. [D]
> Only the v2 struct carries `WiFiOTA`. Do not replicate this unless you are
> deliberately matching vendor behaviour.

### `ConnectReason` (tag 0)

Recovered three independent ways that all agree: two jump tables in the v2 package and a
map in the legacy one. [D]

| value | string |
|---:|---|
| 0 | `unknown` |
| 1 | `reboot` |
| 2 | `wakeup` |
| 3 | `heartbeat` |
| 4 | `logs full` |
| 5 | `connection error` |
| 6 | `protocol error` |
| 7 | `command line` |
| 8 | `by server request` |
| 9 | `display driver failed` |
| 10 | `connection driver error` |
| 11 | `server changes` |

Anything else renders `n/a`.

Independently corroborated by the vendor server's weekly aggregate, which keeps a
per-reason counter whose bucket names map 1:1 onto these values. A real week on the
reference device: 5796 heartbeat connects, 6 protocol-error connects, zero
wakeup/reboot. [W]

A reconnect after a reboot shows up as `1`; the first status of a fresh connection in the
capture carried `5`. [W]

### `ConnectivityUsed` (tags 4 and 6)

Cross-checked against the legacy name map. [D]

| value | module |
|---:|---|
| 0 | not selected |
| 1 | redpine rs9110 |
| 2 | simcom sim5320 |
| 3 | wiznet w5500 |
| 4 | redpine rs9113 |
| 5 | atmel winc1500 |
| 6 | **ti CC3100** |
| 7 | stm 32F7ETH |
| 8 | simcom SIM7500 |
| 9 | redpine RS9116 |
| 10 | ti CC3135 |

The reference device reports `6`. [W] Relevant because the WiFi-module firmware service
only carries RS9116 images — see [firmware.md](firmware.md#wifi-module-firmware).

### Other enums

- `HardwareFamily` (inside tag 2): `1` V-Tablet, `3` DS, `4` Quad, `5` V-Tablet 2. Values
  `0`, `2` and `>= 6` render `n/a` — value 2 genuinely has no case. [D]
  `{ID:3, Family:1, PCB:0}` is special-cased to `"Visionect System Board V1.00"`.
- `JsAppType` (86): `0` none, `1` TC, `2` AC. [D]
- `DcmState` (88): `0` off, `1` on. [D]
- `TouchType` (29): `0` none, `1` CB060E, `2` EKTF2227. [D]
- `Charger` (11): `0` pre charge, `1` fast charge, `2` done, `3` disabled — note the
  vendor API emits this one **numerically** despite having a stringer. [D]

## A real status payload, decoded

496 bytes = 62 records, from the captured heartbeat in
[framing.md](framing.md#worked-example-1--status-heartbeat-device--server). Decoded with
nothing but the rules above. Device-identifying values are replaced with placeholders;
everything else is verbatim. [W]

```
[  0] ConnectReason          = 3           -> "heartbeat"
[  1] ErrorCode              = 0           -> "no error"
[  2] HardwareID             = 0xffffffff  -> absent sentinel
[  3] FirmwareID             = 0x652be4ad  -> the firmware image CRC
[  4] WiFiModuleId           = 6
[  5] MobileModuleId         = 0xffffffff  -> absent
[  6] ConnectivityUsed       = 6           -> "ti CC3100"
[  7] NumSupportedDisplays   = 2
[  8] DisplayIds             = 0xc2050128  -> panel id, not in the vendor enum
[  9] DisplayStateCRC        = 0xdb963a1a
[ 10] Battery                = 100         (<= 102, so already a percentage)
[ 11] Charger                = 1           -> "fast charge"
[ 12] Temperature            = 22          (signed int8 degC)
[ 13] RSSI                   = 44          -> -44 dBm
[ 15] Uptime                 = 35793       (minutes = ~24.9 days)
[ 16] FirmwareMajor          = 7
[ 17] FirmwareMinor          = 4
[ 18] FirmwareRevision       = 4407        (> 269, so the server's watchdog is armed)
[ 19] BootloaderMajor        = 7
[ 20] BootloaderMinor        = 4
[ 21] BootloaderRevision     = 4407
[ 22] HardwareNameID         = 8
[ 23] HardwareMajor          = 1
[ 24] HardwareMinor          = 1
[ 25] HardwareRevision       = 0
[ 26] HardwareFirmwareIface  = 0
[ 27] NextStatus             = 1           (minutes)
[ 29] TouchType              = 0           -> "none"
[ 30] TouchFirmwareMajor     = 0
[ 31] TouchFirmwareMinor     = 0
[ 32] TouchEventCount        = 0
[ 34] BatteryVoltage         = 4212        (mV)
[ 35] BatteryCurrent         = 44          (mA, unsigned)
[ 36] EventQueueOverFlow     = 0
[ 38] DisplayWidth           = 2880
[ 39] DisplayHeight          = 640
[ 40] ProtocolVersion        = 3           <-- matches the ProtocolHeader
[ 43] PV2Features            = 585         -> bits 0,3,6,9
[ 45] EPDTempSensor          = 22
[ 46] ButtonCount            = 0
[ 47] MobileLinkType         = 0xffffffff  -> absent
[ 50] GTIN1                  = 0x30333833  -> "3830"
[ 51] GTIN2                  = 0x34353630  -> "0654"
[ 52] GTIN3                  = 0x39373136  -> "6179"
[ 53] GTIN4                  = 0xffff0032  -> "2\0" + 0xffff padding
                                           => "3830065461792"
[ 54] ImagePushAllowed       = 1
[ 56] MobSecurity            = 2           (cellular APN auth mode; inert on WiFi)
[ 60] FsTotalSize            = 8388608     (8 MiB)
[ 61] FsFreeSize             = 4575232
[ 70] ProximityCount         = 0
[ 71] NetworkErrorCount      = 0
[ 87] McuAwakeCount          = 143175
[ 88] DcmState               = 0           -> "off"
[ 89] WiFiDtim               = 100         (ms)
[ 90] ProximityThreshold     = 0xffffffff
[ 91] BSSID1                 = 0x005e0000  -> 00 00 5E 00     (placeholder MAC)
[ 92] BSSID2                 = 0x00000153  -> 53 01
                                           => "00:00:5E:00:53:01"
[ 99] DisplayUpdateCount     = 688
[100] ConnVerMajor           = 6
[101] ConnVerMinor           = 0xffffffff
[102] ConnVerRev             = 0xc5550acc  (an opaque build word)
[0xffffffff] <sentinel>      = 0x779050df
```

Three independent decodings fall out correct from the recovered rules with no fudging,
which is the strongest evidence that the whole status model is right: [W]

1. The 4-chars-per-word, little-endian, NUL-terminated string packing reassembles the
   GTIN exactly as the vendor API reports it.
2. The two-word little-endian BSSID packing reassembles the MAC exactly as the vendor API
   reports it.
3. `0xFFFFFFFF`-as-absent appears on exactly the three fields (`HardwareID`,
   `MobileModuleId`, `MobileLinkType`) whose codecs treat that value as a no-op.

## The device state machine

The vendor server derives a single state string from **five independent atomic booleans**
by a first-match-wins priority chain: [D]

```
sending → receiving → sleeping → charging → online → disconnected
```

plus `offline`, which is the database-side value used when no gateway currently holds the
device. Seven observable strings in total.

None of this is on the wire — it is the server's own bookkeeping — but it is worth knowing
because **`offline` is the normal condition of a battery sign**, not an error. One
reference device cycled `offline → charging → online → sending` on its own heartbeat
within a few minutes. [W]

## How often a status arrives

| situation | interval |
|---|---|
| awake and connected | **60.0 s ± 0.1 s**, acked by the server within ~1 ms [W] |
| battery sign in its sleep cadence | ~6.8 minutes (~405 s between reports) [W] |
| on demand | the server can ask, via command type 11 (`status request`), or the serial console can force one with `pss` |

The device also controls this itself through TCLV parameter 29 (`HEARTBEAT`), described in
the vendor's own table as *"how long before device sends a status packet after last
communication with the server (minutes)"*. [C] See [parameters.md](parameters.md).
