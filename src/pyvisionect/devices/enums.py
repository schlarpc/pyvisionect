"""Status-field enumerations, as data.

Every table here was read out of ``bin/gateway`` -- jump tables and compare
chains in the corresponding ``(*T).String`` methods.  The Go *identifier* names
are not in the binary (constants carry no metadata), only the display strings,
so the strings below are verbatim and the Python names are ours.
"""

from __future__ import annotations

__all__ = [
    "CONNECT_REASON",
    "ERROR_CODE",
    "CONNECTIVITY_USED",
    "CHARGING_STATUS",
    "TOUCH_TYPE",
    "DCM_STATE",
    "JS_APP_TYPE",
    "HARDWARE_FAMILY",
    "QSPI_FLASH_ID",
    "RENDERER",
    "PV2_FEATURE_BITS",
    "SCPU_SENSOR_BITS",
    "CommandType",
    "FileOperation",
    "FileMode",
    "EncodingType",
    "ImageType",
    "ParamControl",
    "ControlFlags",
    "PacketType",
    "describe",
]


def describe(table: dict[int, str], value: int, fallback: str = "n/a") -> str:
    """Look *value* up in *table*, mirroring the vendor's ``String()`` fallback."""
    return table.get(value, fallback)


# ---------------------------------------------------------------- packet types

class PacketType:
    """``packet.Type``.

    Recovered two independent ways that agree: ``packet.Type.String``'s jump
    table (12 entries, indexed by ``value - 1``) and ``packet.packetTypesFunc``
    (a 13-entry array of constructor closures).

    ``INPUT`` (4) and ``BOOTLOADER`` (0xFFFFFFFF) are genuine historical types
    that survive in the v1 enum, but **this server build can neither construct
    nor dispatch them**: ``packet.New`` rejects ``Type >= 13``,
    ``packetTypesFunc[4]`` is nil, and the bootloader protocol moved out to its
    own mux versions 0xF0000001 / 0xF0000002.
    """

    CONTROL = 1
    COMMAND = 2
    STATUS = 3
    INPUT = 4  # hole in both v2 tables
    IMAGE = 5
    TOUCH = 6
    GPS = 7
    PARAM = 8
    FILE = 10
    BUTTON = 11
    CBOR = 12
    BOOTLOADER = 0xFFFFFFFF

    NAMES: dict[int, str] = {
        1: "control",
        2: "command",
        3: "status",
        4: "input",
        5: "image",
        6: "touch",
        7: "GPS",
        8: "param",
        10: "file",
        11: "button",
        12: "CBOR",
        0xFFFFFFFF: "bootloader",
    }

    DEVICE_TO_SERVER = frozenset({1, 3, 6, 7, 8, 10, 11, 12})
    """Types the gateway has a handler for (plus 1, handled inline)."""

    SERVER_TO_DEVICE = frozenset({1, 2, 5, 8, 10})
    """Types the server originates. 2 and 5 are server-only."""

    @classmethod
    def name(cls, value: int) -> str:
        return cls.NAMES.get(value, f"unknown({value})")


class ControlFlags:
    """``packet.ControlFlags`` -- the bits the gateway actually tests."""

    ACK = 0x00000001
    """``packet.(*Control).Ack`` is literally ``return flags & 1``."""

    NACK_CHARGING = 0x02000000
    """Bit 25. ``pv3.go:498`` ``BTL $0x19, flags`` -> ``vio.ErrNackCharging``."""

    NACK = 0x00000000
    """``flags == 0`` -> ``vio.ErrNack`` (``pv3.go:500``)."""


class ParamControl:
    """``packet.ParamControl`` (uint8), ``param.go:114-124``."""

    READ = 0
    WRITE = 1
    READ_ERROR = 2
    WRITE_ERROR = 3

    NAMES: dict[int, str] = {0: "read", 1: "write", 2: "read error", 3: "write error"}


class CommandType:
    """``packet.CommandType`` (int32 -- the negative values are real).

    ``packet.CommandType.String``, ``packet/command.go:47-95``.
    """

    REBOOT = 0
    ECHO = 1
    CONFIGURATION = 2
    AT_COMMAND = 3
    SLEEP = 4
    LOGS = 5
    SET_VCOM = 6
    SET_HEARTBEAT = 7
    SET_NETWORK_RETRIES = 8
    LED_WINDOW = 9
    LDRLED = 10
    STATUS_REQUEST = 11
    SYSTEM = 12
    BATTERY = 13
    TOUCH_UPDATE = 14
    SET_GPS = 16
    FRONT_LIGHT = 17
    SYNC_DISPLAY = 18
    FRONT_LIGHT_EXTENSION = 22
    REFRESH = -1
    CLEAR_SCREEN = -2
    KEYBOARD = -3
    BEEP = -4

    NAMES: dict[int, str] = {
        0: "reboot",
        1: "echo",
        2: "configuration",
        3: "AT command",
        4: "sleep",
        5: "logs",
        6: "set Vcom",
        7: "set heartbeat",
        8: "set network retries",
        9: "LED window",
        10: "LDRLED",
        11: "status request",
        12: "system",
        13: "battery",
        14: "touch update",
        16: "set gps",
        17: "front light",
        18: "sync display",
        22: "front light extension",
        -1: "refresh",
        -2: "clear screen",
        -3: "keyboard",
        -4: "beep",
    }


class FileOperation:
    """``packet.FileOperation`` (uint32), ``file.go:98-114``."""

    OPEN = 0
    CLOSE = 1
    READ = 2
    WRITE = 3
    ERASE = 4
    WRITE_DESCRIPTOR = 5
    EVENT = 6

    NAMES: dict[int, str] = {
        0: "open",
        1: "close",
        2: "read",
        3: "write",
        4: "erase",
        5: "write descriptor",
        6: "event",
    }


class FileMode:
    """``packet.FileMode`` (uint32), ``file.go:127-134``."""

    NONE = 0
    WRITE = 1
    READ = 2

    NAMES: dict[int, str] = {0: "none", 1: "write", 2: "read"}


class EncodingType:
    """``packet.EncodingType`` (uint16), ``image.go:197-204``.

    Only 1 and 4 exist. 16 greys are the uniform ramp ``n * 17``.
    """

    UNDEFINED = 0
    ONE_BIT = 1
    FOUR_BIT = 4

    NAMES: dict[int, str] = {0: "undefined", 1: "1 bit", 4: "4 bit"}

    ALIGNMENT: dict[int, int] = {1: 16, 4: 4}
    """Horizontal alignment quantum in pixels, per encoding."""


class ImageType:
    """``packet.ImageType`` (uint16), ``image.go:217-224``."""

    UNKNOWN = 0
    GRAY = 1

    NAMES: dict[int, str] = {0: "unknown", 1: "gray"}


# ------------------------------------------------------------- status enums

CONNECT_REASON: dict[int, str] = {
    0: "unknown",
    1: "reboot",
    2: "wakeup",
    3: "heartbeat",
    4: "logs full",
    5: "connection error",
    6: "protocol error",
    7: "command line",
    8: "by server request",
    9: "display driver failed",
    10: "connection driver error",
    11: "server changes",
}
"""Status tag 0. ``status.(*ConnectReason).String.jump5`` @ ``0x1adb940``.

Note: the captured first-packet-after-connect carries 5, steady-state heartbeats
carry 3.
"""

ERROR_CODE: dict[int, str] = {
    0: "no error",
    1: "none",
    2: "command pending",
    3: "image received",
    4: "wakeup",
    5: "connectivity error",
    6: "pv2 error",
    7: "heartbeat",
    8: "connectivity changed",
    9: "image transfer pending",
    10: "touch pending",
    11: "gps pending",
    12: "status send",
    13: "deep sleep request",
    14: "connectivity start",
    15: "internal image update",
    16: "gps control",
    17: "connectivity driver error",
    18: "button pending",
    19: "reboot",
    20: "local image",
    21: "proximity",
    22: "status acked",
    23: "file transfer pending",
    24: "file transfer done",
    25: "server change",
}
"""Status tag 1. ``(*ErrorCode).String.jump5`` @ ``0x1aea4c0``, 26 entries.

**This is the reason the packet was sent, not a fault.**  13 ("deep sleep
request") is the device asking to sleep, not an error condition.
"""

CONNECTIVITY_USED: dict[int, str] = {
    0: "not selected",
    1: "redpine rs9110",
    2: "simcom sim5320",
    3: "wiznet w5500",
    4: "redpine rs9113",
    5: "atmel winc1500",
    6: "ti CC3100",
    7: "stm 32F7ETH",
    8: "simcom SIM7500",
    9: "redpine RS9116",
    10: "ti CC3135",
}
"""Status tag 6. The live 32" sign reports 6."""

CHARGING_STATUS: dict[int, str] = {
    0: "pre charge",
    1: "fast charge",
    2: "done",
    3: "disabled",
}
"""Status tag 11."""

TOUCH_TYPE: dict[int, str] = {0: "none", 1: "CB060E", 2: "EKTF2227"}
"""Status tag 29."""

DCM_STATE: dict[int, str] = {0: "off", 1: "on"}
"""Status tag 88."""

JS_APP_TYPE: dict[int, str] = {0: "none", 1: "TC", 2: "AC"}
"""Status tag 86."""

HARDWARE_FAMILY: dict[int, str] = {
    1: "V-Tablet",
    3: "DS",
    4: "Quad",
    5: "V-Tablet 2",
}
"""The ``Family`` byte inside status tag 2 (``HardwareID``).

0, 2 and >= 6 render ``n/a``; value 2 genuinely has no case in the binary.
"""

QSPI_FLASH_ID: dict[int, str] = {
    0x9D7020: "IS25WP512MG",
    0x9D701A: "IS25WP512M",
    0x9D7019: "IS25WP256D",
}
"""Status tag 103: raw JEDEC RDID words (0x9D = ISSI)."""

RENDERER: dict[int, str] = {
    0: "unknown",
    1: "HTTP",
    2: "HTML",
    3: "ACRender2",
    4: "PNG",
}
"""Server-side only (cbor key 5000000004); never on the wire."""

PV2_FEATURE_BITS: tuple[str, ...] = (
    "RemoteBootloaderUpgrade",  # bit 0  0x001
    "TouchDisabled",            # bit 1  0x002
    "WPA2EnterpriseDisabled",   # bit 2  0x004
    "FileSystem",               # bit 3  0x008
    "DisplayScan42",            # bit 4  0x010
    "NoFading",                 # bit 5  0x020
    "FastBoot",                 # bit 6  0x040
    "JSPatchUpdate",            # bit 7  0x080
    "DoubleBuffer",             # bit 8  0x100
    "WiFiOTA",                  # bit 9  0x200
)
"""Status tag 43, a **10**-bit mask.

``status.(*Features).UnmarshalUint32`` is literally ten ``BTL $i, BX`` pairs, and
the struct field order equals the cbor keyasint order, so bit *i* is field *i*.

The vendor's *legacy* parser ``proto.ParsePV2Features1`` loops
``for mask := 1; mask <= 0x100; mask <<= 1`` and therefore silently drops bit 9
(``WiFiOTA``, 512).  ``admin`` and ``engine`` use that parser and see the live
sign's ``585`` as if it were ``73``.  **We do not replicate that bug.**
"""

SCPU_SENSOR_BITS: tuple[str, ...] = (
    "SCPUTemperature",
    "ExternalTemperature",
    "Humidity",
    "Pressure",
    "SCPUVoltage",
    "ExternalVoltage",
    "ExternalLDRVoltage",
    "ExternalUserSensorVoltage",
)
"""Status tag 44, same construction with 8 bits."""
