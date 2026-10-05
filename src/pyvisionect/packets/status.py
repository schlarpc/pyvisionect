"""Status packets (type 3) -- the device's heartbeat and the only packet it must send.

The payload is **not CBOR**.  It is a flat sequence of 8-byte records::

    +-------------------------------+
    |        Tag   (uint32 LE)      |
    +-------------------------------+
    |        Value (uint32 LE)      |
    +-------------------------------+
     ... one record per *present* field ...
    |        Tag = 0xFFFFFFFF       |   <- sentinel, always last
    |        Value = <changes with the payload>
    +-------------------------------+

``status.(*Encoder).write`` (``status/encoder.go:348-356``) returns early on a nil
pointer, so **absent fields are omitted entirely** and a status is a sparse list.
In the captured heartbeat 62 of the ~104 possible tags are present.

Multi-word values occupy **consecutive** tags
(``status.(*Encoder).writeMultiValue``, ``encoder.go:371-382``), which is exactly
why the tag numbering has gaps.

Tag table provenance
--------------------

Two sources agree and are reconciled here:

* the **v2** ``status.Status`` struct's ``cbor:"N,keyasint"`` numbers, read out of
  the Go runtime type metadata.  The flat wire payload reuses those numbers as
  its uint32 keys, so the CBOR tags are directly authoritative even though this
  firmware never sends CBOR.
* the **v1** ``proto.statusStringMap`` (103 entries + sentinel), which is what the
  REST API surfaces.

Where they disagree, **v2 wins** and the v1 name is kept as an alias:

===  ==========================  ==========================
tag  v2 (authoritative)          v1 (REST name)
===  ==========================  ==========================
2    ``HardwareID``              ``BootloaderVersion``
3    ``FirmwareID``              ``ApplicationVersion``
8    ``DisplayType``             ``DisplayIds``
46   ``FLButtonCount``           ``ButtonCount``
===  ==========================  ==========================

Every tag present in v1 but absent from the v2 struct turned out to be a
*continuation* tag of a multi-word v2 field (``GTIN2..4``, ``FirmwareMinor``,
``BSSID2``, ...), so the two tables are consistent once multi-word fields are
expanded.  Tags 91/92 (``BSSID``) exist only in v2.

Scalar encodings that are easy to get wrong
-------------------------------------------

* ``SignalStrength`` (13) is a **dBm magnitude with the sign dropped** -- negate it.
  The captured 44 means -44 dBm.
* ``DeviceUptime`` (15) and ``NextStatus`` (27) are in **minutes**, not seconds.
  The captured 35793 is ~24.9 days.
* ``Temperature`` is a signed **int8** in the low byte, no scaling. ``0xFFFFFFFF``
  (and ``0xFF``) means "no reading".
* ``HardwareID`` packs ``ID<<24 | Family<<16 | PCB``.
* ``String`` is 4 ASCII chars per word, **little-endian**, NUL-terminated.
* ``BSSID`` is 6 bytes over 2 tags, LE.
* ``Features`` (43) is a **10**-bit mask. We do not reproduce the vendor's
  bit-9-dropping legacy parser.
* ``ErrorCode`` (1) is the *reason the packet was sent*, not a fault.
* ``0xFFFFFFFF`` as a value means "field absent" for the fields whose
  ``UnmarshalUint32`` treats it as a no-op (``HardwareID``, ``MobileModuleID``,
  ``MobileLinkType``, ``ProximityThreshold``, ...).
"""

from __future__ import annotations

import enum
import struct
from dataclasses import dataclass, field
from typing import Any, Iterator

from ..devices import enums as devenums
from ..wire.errors import PayloadError

__all__ = [
    "SENTINEL_TAG",
    "ABSENT",
    "StatusCodec",
    "StatusTag",
    "STATUS_TAGS",
    "STATUS_TAG_NAMES",
    "StatusPacket",
    "decode_status_fields",
]

SENTINEL_TAG = 0xFFFFFFFF
"""The record that terminates the list. v1 calls the tag ``LastStatus``.

Its *value* is **the CRC-32/ISO-HDLC of every record byte that precedes it** --
the ordinary zlib/PNG CRC-32, i.e. ``zlib.crc32(payload[:-8])``. Confirmed on
16 of 16 live status frames (2026-10-05), across three TCP connections and three
connect reasons.

We still carry it verbatim and never enforce it, for one concrete reason: the
committed golden fixture **fails** the check, by a constant ``0xe63fe9a0``,
because the fixture is scrubbed and ``BSSID`` lives inside the CRC'd region.
Substituting the same bytes into every frame of a fixed-length region shifts
every CRC by the same amount, which is exactly what is observed. So anything
that validated this would reject our own test data, and any future scrub has to
recompute it.
"""

ABSENT = 0xFFFFFFFF
"""The "field not present" sentinel *value*."""

_REC = struct.Struct("<II")


class StatusCodec(enum.Enum):
    """How to turn a tag's raw uint32 word(s) into a Python value."""

    U32 = "u32"
    BOOL = "bool"
    I32 = "i32"
    TEMPERATURE = "temperature"
    HARDWARE_ID = "hardware_id"
    VERSION = "version"
    TOUCH_VERSION = "touch_version"
    FEATURES = "features"
    SCPU_SENSORS = "scpu_sensors"
    BSSID = "bssid"
    STRING = "string"
    RSSI = "rssi"
    MINUTES = "minutes"
    ENUM = "enum"


@dataclass(frozen=True, slots=True)
class StatusTag:
    """One row of the status tag table."""

    tag: int
    name: str
    codec: StatusCodec = StatusCodec.U32
    words: int = 1
    """How many consecutive tags the value occupies."""
    v1_name: str | None = None
    """The v1/REST name, when it differs from *name*."""
    enum_table: dict[int, str] | None = None
    unit: str | None = None

    @property
    def tags(self) -> range:
        return range(self.tag, self.tag + self.words)


def _t(tag, name, codec=StatusCodec.U32, words=1, v1=None, table=None, unit=None):
    return StatusTag(tag, name, codec, words, v1, table, unit)


STATUS_TAGS: tuple[StatusTag, ...] = (
    _t(0, "ConnectReason", StatusCodec.ENUM, table=devenums.CONNECT_REASON),
    _t(1, "ErrorCode", StatusCodec.ENUM, table=devenums.ERROR_CODE),
    _t(2, "HardwareID", StatusCodec.HARDWARE_ID, v1="BootloaderVersion"),
    _t(3, "FirmwareID", StatusCodec.U32, v1="ApplicationVersion"),
    _t(4, "WiFiModuleID", StatusCodec.U32, v1="WiFiModuleId"),
    _t(5, "MobileModuleID", StatusCodec.U32, v1="MobileModuleId"),
    _t(6, "ConnectivityUsed", StatusCodec.ENUM, table=devenums.CONNECTIVITY_USED),
    _t(7, "NumberOfSupportedDisplays", StatusCodec.U32, v1="NumSupportedDisplays"),
    _t(8, "DisplayType", StatusCodec.U32, v1="DisplayIds"),
    _t(9, "DisplayStateCRC", StatusCodec.U32),
    _t(10, "BatteryLevel", StatusCodec.U32, v1="Battery", unit="%"),
    _t(11, "ChargingStatus", StatusCodec.ENUM, v1="Charger", table=devenums.CHARGING_STATUS),
    _t(12, "AverageTemperature", StatusCodec.TEMPERATURE, v1="Temperature", unit="degC"),
    _t(13, "SignalStrength", StatusCodec.RSSI, v1="RSSI", unit="dBm"),
    _t(14, "ExternalBattery", StatusCodec.U32),
    _t(15, "DeviceUptime", StatusCodec.MINUTES, v1="Uptime", unit="min"),
    _t(16, "Firmware", StatusCodec.VERSION, words=3),
    _t(19, "Bootloader", StatusCodec.VERSION, words=3),
    _t(22, "HardwareNameID", StatusCodec.U32),
    _t(23, "Hardware", StatusCodec.VERSION, words=3),
    _t(26, "HardwareFirmwareInterface", StatusCodec.U32, v1="HardwareFirmwareIface"),
    _t(27, "NextStatus", StatusCodec.MINUTES, unit="min"),
    _t(28, "ExternalTemperatureSensor", StatusCodec.TEMPERATURE,
       v1="ExternalTempSensor", unit="degC"),
    _t(29, "TouchType", StatusCodec.ENUM, table=devenums.TOUCH_TYPE),
    _t(30, "TouchFirmware", StatusCodec.TOUCH_VERSION, words=2),
    _t(32, "TouchEventCount", StatusCodec.U32),
    _t(33, "AccelerometerEventCount", StatusCodec.U32, v1="AcceleromenterEventCount"),
    _t(34, "BatteryVoltage", StatusCodec.U32, unit="mV"),
    _t(35, "BatteryCurrent", StatusCodec.U32, unit="mA"),
    _t(36, "EventQueueOverflow", StatusCodec.U32, v1="EventQueueOverFlow"),
    _t(37, "GPSState", StatusCodec.U32, v1="PV2GPSState"),
    _t(38, "DisplayWidth", StatusCodec.U32, unit="px"),
    _t(39, "DisplayHeight", StatusCodec.U32, unit="px"),
    _t(40, "ProtocolVersion", StatusCodec.U32),
    _t(41, "HumiditySensor", StatusCodec.U32),
    _t(42, "PressureSensor", StatusCodec.U32),
    _t(43, "Features", StatusCodec.FEATURES, v1="PV2Features"),
    _t(44, "SCPUSensorStatus", StatusCodec.SCPU_SENSORS),
    _t(45, "EPDTemperatureSensor", StatusCodec.TEMPERATURE, v1="EPDTempSensor", unit="degC"),
    _t(46, "FLButtonCount", StatusCodec.U32, v1="ButtonCount"),
    _t(47, "MobileLinkType", StatusCodec.U32),
    _t(48, "FlSensorValue", StatusCodec.U32, v1="PV2FLSensorValue"),
    _t(49, "VMSensorValue", StatusCodec.U32, v1="SCPUVMSensorValue"),
    _t(50, "GTIN", StatusCodec.STRING, words=4),
    _t(54, "ImagePushAllowed", StatusCodec.BOOL),
    _t(55, "RTCGMTEpoch", StatusCodec.U32, v1="RtcGmtEpoch"),
    _t(56, "MobileSecurity", StatusCodec.U32, v1="MobSecurity"),
    _t(57, "JSAPIVersion", StatusCodec.VERSION, words=3),
    _t(60, "FSTotalSize", StatusCodec.U32, v1="FsTotalSize", unit="bytes"),
    _t(61, "FSFreeSize", StatusCodec.U32, v1="FsFreeSize", unit="bytes"),
    _t(62, "JSEStatus", StatusCodec.U32, v1="JseStatus"),
    _t(63, "ProximityCalibration", StatusCodec.U32, v1="ProximityCal"),
    _t(64, "HitDetector", StatusCodec.U32),
    _t(65, "Button1Count", StatusCodec.U32),
    _t(66, "Button2Count", StatusCodec.U32),
    _t(67, "Button3Count", StatusCodec.U32),
    _t(68, "Button4Count", StatusCodec.U32),
    _t(69, "BLEState", StatusCodec.U32),
    _t(70, "ProximityCount", StatusCodec.U32),
    _t(71, "NetworkErrCount", StatusCodec.U32, v1="NetworkErrorCount"),
    _t(72, "JSAppVersion", StatusCodec.VERSION, words=3),
    _t(75, "WallMountCount", StatusCodec.U32),
    _t(76, "JSAppID", StatusCodec.STRING, words=10),
    _t(86, "JSAppType", StatusCodec.ENUM, table=devenums.JS_APP_TYPE),
    _t(87, "MCUAwakeCount", StatusCodec.U32, v1="McuAwakeCount"),
    _t(88, "DCMState", StatusCodec.ENUM, v1="DcmState", table=devenums.DCM_STATE),
    _t(89, "WiFiDTIM", StatusCodec.U32, v1="WiFiDtim"),
    _t(90, "ProximityThreshold", StatusCodec.U32),
    _t(91, "BSSID", StatusCodec.BSSID, words=2),
    _t(93, "BatterySOC1Voltage", StatusCodec.U32, unit="mV"),
    _t(94, "BatterySOC1Current", StatusCodec.I32, unit="mA"),
    _t(95, "BatterySOC1Percent", StatusCodec.U32, unit="%"),
    _t(96, "BatterySOC2Voltage", StatusCodec.U32, unit="mV"),
    _t(97, "BatterySOC2Current", StatusCodec.I32, unit="mA"),
    _t(98, "BatterySOC2Percent", StatusCodec.U32, unit="%"),
    _t(99, "DisplayUpdateCount", StatusCodec.U32),
    _t(100, "ConnectivityVersion", StatusCodec.VERSION, words=3),
    _t(103, "QSPIFlashID", StatusCodec.ENUM, table=devenums.QSPI_FLASH_ID),
)

STATUS_TAG_BY_BASE: dict[int, StatusTag] = {t.tag: t for t in STATUS_TAGS}

STATUS_TAG_NAMES: dict[int, str] = {}
for _tag in STATUS_TAGS:
    for _i, _n in enumerate(_tag.tags):
        STATUS_TAG_NAMES[_n] = _tag.name if _i == 0 else f"{_tag.name}+{_i}"
STATUS_TAG_NAMES[SENTINEL_TAG] = "LastStatus"
del _tag, _i, _n


# ------------------------------------------------------------------ codecs

def _decode_string(words: list[int]) -> str:
    """4 ASCII chars per word, little-endian, NUL-terminated.

    GTIN spans tags 50..53 and the captured frame's last word is ``0xffff0032``,
    i.e. ``"2\\0"`` followed by 0xff padding -- so the NUL ends the string and
    whatever follows in that word is ignored.
    """
    out = bytearray()
    for w in words:
        chunk = w.to_bytes(4, "little")
        if b"\x00" in chunk:
            out += chunk[: chunk.index(b"\x00")]
            break
        out += chunk
    return out.decode("ascii", errors="replace")


def _encode_string(value: str, words: int) -> list[int]:
    """Inverse of :func:`_decode_string`, padding the final word with NULs."""
    raw = value.encode("ascii")
    if len(raw) >= words * 4:
        raise ValueError(f"{value!r} does not fit in {words} words with a NUL")
    raw += b"\x00"
    raw += b"\x00" * (-len(raw) % 4)
    return [int.from_bytes(raw[i : i + 4], "little") for i in range(0, len(raw), 4)]


def _decode_bssid(words: list[int]) -> str | None:
    """6 bytes over 2 tags, LE: tag+0 = bytes 0..3, tag+1 = bytes 4..5."""
    if not words or words[0] == ABSENT:
        return None
    raw = words[0].to_bytes(4, "little") + (
        words[1].to_bytes(4, "little")[:2] if len(words) > 1 else b""
    )
    return ":".join(f"{b:02X}" for b in raw[:6])


def _decode_temperature(word: int) -> int | None:
    """Signed int8 in the low byte, degrees C, no scaling."""
    if word == ABSENT:
        return None
    low = word & 0xFF
    if low == 0xFF:
        return None
    return low - 256 if low >= 0x80 else low


def _decode_hardware_id(word: int) -> dict[str, Any] | None:
    """``bits 31..24 = ID``, ``23..16 = Family``, ``15..0 = PCB``."""
    if word == ABSENT:
        return None
    hw_id = (word >> 24) & 0xFF
    family = (word >> 16) & 0xFF
    return {
        "id": hw_id,
        "family": family,
        "family_name": devenums.HARDWARE_FAMILY.get(family, "n/a"),
        "pcb": word & 0xFFFF,
    }


def _decode_bits(word: int, names: tuple[str, ...]) -> dict[str, bool]:
    return {name: bool(word >> i & 1) for i, name in enumerate(names)}


def _decode_value(tag: StatusTag, words: list[int]) -> Any:
    codec = tag.codec
    first = words[0]
    if codec is StatusCodec.U32:
        return None if first == ABSENT else first
    if codec is StatusCodec.BOOL:
        return bool(first)
    if codec is StatusCodec.I32:
        return first - (1 << 32) if first >= (1 << 31) else first
    if codec is StatusCodec.ENUM:
        table = tag.enum_table or {}
        return {"value": first, "name": devenums.describe(table, first)}
    if codec is StatusCodec.TEMPERATURE:
        return _decode_temperature(first)
    if codec is StatusCodec.HARDWARE_ID:
        return _decode_hardware_id(first)
    if codec is StatusCodec.VERSION:
        return {
            "major": words[0] if len(words) > 0 else None,
            "minor": words[1] if len(words) > 1 else None,
            "revision": words[2] if len(words) > 2 else None,
        }
    if codec is StatusCodec.TOUCH_VERSION:
        return {
            "major": words[0] if len(words) > 0 else None,
            "minor": words[1] if len(words) > 1 else None,
        }
    if codec is StatusCodec.FEATURES:
        out = _decode_bits(first, devenums.PV2_FEATURE_BITS)
        out["_mask"] = first
        return out
    if codec is StatusCodec.SCPU_SENSORS:
        return _decode_bits(first, devenums.SCPU_SENSOR_BITS)
    if codec is StatusCodec.BSSID:
        return _decode_bssid(words)
    if codec is StatusCodec.STRING:
        return _decode_string(words)
    if codec is StatusCodec.RSSI:
        # dBm magnitude with the sign dropped: 44 on the wire means -44 dBm.
        return None if first == ABSENT else -first
    if codec is StatusCodec.MINUTES:
        return None if first == ABSENT else first
    raise AssertionError(f"unhandled codec {codec}")


def decode_status_fields(raw: dict[int, int]) -> dict[str, Any]:
    """Turn a ``{tag: word}`` map into a ``{name: value}`` map.

    Tags not in :data:`STATUS_TAGS` are collected under ``"Unknown"`` as a
    ``{tag: word}`` dict, mirroring ``status.Status.Unknown``.
    """
    out: dict[str, Any] = {}
    consumed: set[int] = set()
    for tag in STATUS_TAGS:
        if tag.tag not in raw:
            continue
        words = [raw[n] for n in tag.tags if n in raw]
        consumed.update(n for n in tag.tags if n in raw)
        out[tag.name] = _decode_value(tag, words)
    unknown = {t: v for t, v in raw.items() if t not in consumed and t != SENTINEL_TAG}
    if unknown:
        out["Unknown"] = unknown
    return out


# ------------------------------------------------------------------ packet

@dataclass(slots=True)
class StatusPacket:
    """A status payload, held as the **ordered raw record list**.

    Keeping the records in wire order (rather than only the decoded field map) is
    what makes byte-exact re-encoding provable: :meth:`encode` is the exact
    inverse of :meth:`decode` for every captured frame.

    Attributes:
        records: ``(tag, value)`` in wire order, excluding the sentinel.
        sentinel: the sentinel record's *value*. Carried verbatim; its meaning is
            unconfirmed.
        trailing: any bytes after the sentinel (always empty in practice).
    """

    records: list[tuple[int, int]] = field(default_factory=list)
    sentinel: int | None = None
    trailing: bytes = b""

    TYPE = devenums.PacketType.STATUS

    @classmethod
    def decode(cls, payload: bytes) -> "StatusPacket":
        if len(payload) % 8:
            raise PayloadError(
                f"status payload is {len(payload)} bytes, not a multiple of 8"
            )
        records: list[tuple[int, int]] = []
        sentinel: int | None = None
        offset = 0
        while offset + 8 <= len(payload):
            tag, value = _REC.unpack_from(payload, offset)
            offset += 8
            if tag == SENTINEL_TAG:
                sentinel = value
                break
            records.append((tag, value))
        return cls(records=records, sentinel=sentinel, trailing=payload[offset:])

    def encode(self) -> bytes:
        out = bytearray()
        for tag, value in self.records:
            out += _REC.pack(tag, value)
        if self.sentinel is not None:
            out += _REC.pack(SENTINEL_TAG, self.sentinel)
        out += self.trailing
        return bytes(out)

    # -- views -----------------------------------------------------------

    @property
    def raw(self) -> dict[int, int]:
        """``{tag: value}``. Later duplicates win, as in the Go decoder."""
        return dict(self.records)

    def fields(self) -> dict[str, Any]:
        """The decoded, named field map."""
        return decode_status_fields(self.raw)

    def get(self, name: str, default: Any = None) -> Any:
        return self.fields().get(name, default)

    def __iter__(self) -> Iterator[tuple[int, int]]:
        return iter(self.records)

    def __len__(self) -> int:
        return len(self.records)

    # -- convenience accessors used by the session layer -----------------

    @property
    def connect_reason(self) -> int | None:
        """Status tag 0, raw."""
        return self.raw.get(0)

    @property
    def connect_reason_name(self) -> str:
        """Status tag 0, named: ``"reboot"``, ``"wakeup"``, ``"heartbeat"``, ...

        The cheapest confirmation available that a type-2 command landed: a
        reboot command that worked shows up as the device reconnecting with
        reason ``1`` (``"reboot"``), and a server-requested status as ``8``
        (``"by server request"``) -- ``8`` **confirmed live** 2026-10-05, as the
        answer to a captured ``status request`` command, and ``2`` (``"wakeup"``)
        as the answer to an ``app_wakeup`` out of deep sleep. Unknown values
        render ``"n/a"``, as the vendor's own ``String()`` does.
        """
        reason = self.connect_reason
        if reason is None:
            return "n/a"
        return devenums.describe(devenums.CONNECT_REASON, reason)

    @property
    def error_code(self) -> int | None:
        return self.raw.get(1)

    @property
    def display_type(self) -> int | None:
        """Raw uint32 ``DisplayType`` (tag 8). Deliberately not an enum."""
        return self.raw.get(8)

    @property
    def display_state_crc(self) -> int | None:
        """Tag 9: the XXHash32 the device computed over its own 8-bit state image.

        Compare it against ``EncodedFrame.state_checksum`` to decide whether a
        push is needed. Note this is **not** a CRC despite the vendor's name.
        """
        return self.raw.get(9)

    @property
    def hardware_name_id(self) -> int | None:
        return self.raw.get(22)

    @property
    def next_status_minutes(self) -> int | None:
        return self.raw.get(27)

    @property
    def firmware_version(self) -> tuple[int, int, int] | None:
        r = self.raw
        if 16 not in r:
            return None
        return (r.get(16, 0), r.get(17, 0), r.get(18, 0))

    @property
    def features_mask(self) -> int | None:
        return self.raw.get(43)

    @property
    def image_push_allowed(self) -> bool:
        return bool(self.raw.get(54, 0))

    @property
    def protocol_version(self) -> int | None:
        return self.raw.get(40)
