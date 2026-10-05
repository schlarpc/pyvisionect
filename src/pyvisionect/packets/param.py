"""Parameter packets (type 8) -- the TCLV configuration channel.

Wire format (``packet.(*Param).MarshalBinaryTo``, ``packet/param.go:21-47``)::

    ParamHeader (8 bytes)
       0   4  Reserved      uint32 LE
       4   4  PayloadLength uint32 LE   = sum over items of (4 + len(Value))

    then, repeated:
    ParamPayload
       +0  2  Type    uint16 LE   (the TCLV parameter id)
       +2  1  Control uint8       (0 read, 1 write, 2 read error, 3 write error)
       +3  1  Length  uint8       (= len(Value); a value is at most 255 bytes)
       +4  N  Value   Length bytes

Note the header is **8 bytes**, not the 12 of ``proto.Command`` -- ``Reserved``
is the first word and ``PayloadLength`` the second, and nothing else is emitted.
Counted off the capture: a 32-byte read request carried 24 bytes of items.

A **read request** carries no TLVs at all: it is 8 header bytes followed by a
bare ``uint32`` per requested id.  That is not a separate struct -- the gateway's
captured request was::

    00 00 00 00   Reserved      = 0
    18 00 00 00   PayloadLength = 24
    1d 00 00 00   -> id 29, Control 0 (read), Length 0
    12 00 00 00   -> id 18, Control 0, Length 0
    ...

i.e. six zero-value ``ParamPayload`` records, which is exactly what this codec
produces for ``ParamItem(29, READ, b"")``.

Values are **untyped bytes**: a uint32 for numeric keys, a uint16 for the port,
bare ASCII with no NUL for strings.  The library does not guess; use
:func:`encode_value` / :func:`decode_value` when you want a typed view.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from ..devices.enums import PacketType, ParamControl
from ..devices.tclv import (
    DEFAULT_VALUE_WIDTH,
    check_writable,
    name_of,
    width_of,
)
from ..wire.errors import PayloadError

__all__ = [
    "ParamItem",
    "ParamPacket",
    "encode_value",
    "decode_value",
    "PARAM_ERROR_NO_SUCH_PARAMETER",
    "PARAM_ERROR_BAD_VALUE",
    "PARAM_ERROR_NAMES",
]

PARAM_ERROR_NO_SUCH_PARAMETER = 0x00580000
"""Error value for a parameter this firmware does not implement.

Measured: reading 145 (``TLS mode``), 146 (``EPD count``), 147..151, 158..160
and the deliberately invented id 250 all came back ``control=2``, ``Length=4``,
``value=00 00 58 00`` -- byte for byte identical.

What makes this a *reading* rather than a guess is that other refusals in the
same sweep came back **different**. Sweeping 130..160 on firmware 7.4.4407
produced four distinct error values, and the ones that are not ``0x58`` land on
parameters the device demonstrably has:

===========  ===============================================================
error value  ids
===========  ===============================================================
``0x58``     133-137, 141, 142, 145-151, 158-160, and the invented 250
``0x5d``     138 (BLE MAC), 143 (WiFi module upgrade), 157 (format filesystem)
``0x8d``     139 (performs WiFi scan)
``0x60``     140 (BLE advertising data)
===========  ===============================================================

BLE is on (155 reads 1), the filesystem exists, the WiFi module exists -- and
those ids answer with something *other* than ``0x58``. So ``0x58`` is the
firmware saying it has no such parameter, not a blanket "no". The other three
values have no interpretation here; three of their four ids are command-shaped,
which is suggestive and nothing more.
"""

PARAM_ERROR_BAD_VALUE = 0x005A0000
"""Error value for a parameter that exists but whose value was unacceptable.

Measured: a **one-byte** write to 29 (``Heart beat interval``, a uint32) came
back ``control=3``, ``value=00 00 5a 00``. The same id read back cleanly in the
same session, so this is not "no such parameter" -- it is the width.

That these two differ is what makes the parameter channel diagnosable at all:
without it, "this firmware has no such setting" and "this library encoded the
value wrongly" are the same symptom.
"""

PARAM_ERROR_NAMES: dict[int, str] = {
    PARAM_ERROR_NO_SUCH_PARAMETER: "no such parameter on this firmware",
    PARAM_ERROR_BAD_VALUE: "parameter exists, value rejected (wrong width?)",
}

_HDR = struct.Struct("<II")
_ITEM = struct.Struct("<HBB")


@dataclass(frozen=True, slots=True)
class ParamItem:
    """One ``packet.ParamPayload`` record."""

    id: int
    control: int = ParamControl.READ
    value: bytes = b""

    def __post_init__(self) -> None:
        if len(self.value) > 255:
            raise ValueError(
                f"TCLV value for id {self.id} is {len(self.value)} bytes; "
                "the Length field is a uint8"
            )

    @property
    def name(self) -> str:
        return name_of(self.id)

    @property
    def is_error(self) -> bool:
        return self.control in (ParamControl.READ_ERROR, ParamControl.WRITE_ERROR)

    @property
    def error_code(self) -> int | None:
        """The device's reason code on an error reply, else None.

        Compare against :data:`PARAM_ERROR_NO_SUCH_PARAMETER` and
        :data:`PARAM_ERROR_BAD_VALUE`. An unrecognised value is returned as-is
        rather than mapped to anything: only two have been observed and there
        is no table for the rest.
        """
        if not self.is_error:
            return None
        return self.as_int()

    @property
    def error_name(self) -> str | None:
        """:attr:`error_code` as prose, or its hex if it is one we have not seen."""
        code = self.error_code
        if code is None:
            return None
        return PARAM_ERROR_NAMES.get(code, f"unknown reason 0x{code:08x}")

    def encode(self) -> bytes:
        return _ITEM.pack(self.id, self.control, len(self.value)) + self.value

    def as_int(self) -> int:
        """Little-endian unsigned integer view of the value."""
        return int.from_bytes(self.value, "little")

    def as_str(self) -> str:
        """ASCII view, stripping a trailing NUL if the device sent one."""
        return self.value.split(b"\x00")[0].decode("ascii", errors="replace")

    def __str__(self) -> str:
        ctl = ParamControl.NAMES.get(self.control, str(self.control))
        return f"{self.id}({self.name!r}) {ctl} = {self.value!r}"


def encode_value(value: int | str | bytes, *, width: int | None = None) -> bytes:
    """Value encoder.

    Args:
        value: ``bytes`` are passed through; ``str`` becomes bare ASCII with no
            terminator (that is what the capture shows); ``int`` becomes a
            little-endian unsigned integer of *width* bytes.
        width: bytes for an integer. Defaults to
            :data:`~pyvisionect.devices.tclv.DEFAULT_VALUE_WIDTH`; callers that
            know the parameter should pass
            :func:`~pyvisionect.devices.tclv.width_of`, which
            :meth:`ParamPacket.write` does for them.

    The default used to be "the narrowest of 1/2/4 that fits the value", which
    is wrong and was wrong silently. Width is a property of the **parameter**,
    not of the value: the heartbeat (29) is a uint32 whose normal value is 1,
    and a one-byte write to it comes back as a write error. See
    :data:`~pyvisionect.devices.tclv.VALUE_WIDTHS`.
    """
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("ascii")
    if isinstance(value, bool):
        return bytes([int(value)])
    if isinstance(value, int):
        if width is None:
            width = DEFAULT_VALUE_WIDTH
        return int(value).to_bytes(width, "little")
    raise TypeError(f"cannot encode {type(value).__name__} as a TCLV value")


def decode_value(raw: bytes) -> int | str:
    """Decode a value as an int if it looks numeric, else as ASCII.

    A heuristic, offered for display only. The protocol carries no type tag.
    """
    if len(raw) in (1, 2, 4) and not all(0x20 <= b < 0x7F for b in raw):
        return int.from_bytes(raw, "little")
    if raw and all(0x20 <= b < 0x7F or b == 0 for b in raw):
        return raw.split(b"\x00")[0].decode("ascii")
    return int.from_bytes(raw, "little")


@dataclass(slots=True)
class ParamPacket:
    """A ``packet.Param`` payload."""

    items: list[ParamItem] = field(default_factory=list)
    reserved: int = 0

    TYPE = PacketType.PARAM

    @classmethod
    def read(cls, ids: list[int] | tuple[int, ...]) -> "ParamPacket":
        """Build a read request for *ids*."""
        return cls(items=[ParamItem(i, ParamControl.READ, b"") for i in ids])

    @classmethod
    def write(
        cls, values: dict[int, int | str | bytes], *, allow_read_only: bool = False
    ) -> "ParamPacket":
        """Build a write request.

        Raises:
            ReadOnlyParameter: for any id in the eight-strong network-read-only
                set, unless *allow_read_only* is set. The error names the USB CLI
                command that can set it.
        """
        items = []
        for pid, value in values.items():
            if not allow_read_only:
                check_writable(pid)
            items.append(
                ParamItem(
                    pid,
                    ParamControl.WRITE,
                    encode_value(value, width=width_of(pid)),
                )
            )
        return cls(items=items)

    @classmethod
    def decode(cls, payload: bytes) -> "ParamPacket":
        if len(payload) < 8:
            raise PayloadError(f"param payload is {len(payload)} bytes, need >= 8")
        reserved, declared = _HDR.unpack_from(payload, 0)
        body = payload[8 : 8 + declared] if declared else payload[8:]
        items: list[ParamItem] = []
        offset = 0
        while offset + 4 <= len(body):
            pid, control, length = _ITEM.unpack_from(body, offset)
            offset += 4
            if offset + length > len(body):
                raise PayloadError(
                    f"param id {pid} declares {length} value bytes, only "
                    f"{len(body) - offset} remain"
                )
            items.append(ParamItem(pid, control, body[offset : offset + length]))
            offset += length
        return cls(items=items, reserved=reserved)

    def encode(self) -> bytes:
        body = b"".join(item.encode() for item in self.items)
        return _HDR.pack(self.reserved, len(body)) + body

    def by_id(self) -> dict[int, ParamItem]:
        return {item.id: item for item in self.items}

    def __iter__(self):
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)
