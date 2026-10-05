"""Control packets (type 1) -- the ack/nack channel.

Wire format (``packet/control.go:35-43``), requires ``8 + len(payload)`` bytes::

    offset size field
      0     4   Flags  uint32 LE  (packet.ControlFlags)
      4     4   Length uint32 LE  (= len(Payload))
      8   ...   Payload

Semantics
---------

* ``Flags & 1`` -> **ACK**. ``packet.(*Control).Ack`` is literally ``return flags & 1``.
* ``Flags == 0``  -> **NACK**. A captured device NACK carries 4 extra bytes of
  error code, e.g. ``0x00008a00`` for "no such file".
* **Bit 25** (``0x02000000``) -> NACK because the device is charging
  (``vio.ErrNackCharging``, ``pv3.go:498``).

The responder echoes the originator's ``DataHeader.ID``.  ``ID == 0xFFFFFFFF`` is
the "do not ack" sentinel in ``WriteControl`` (``pv3.go:512``).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from ..devices.enums import ControlFlags, PacketType
from ..wire.errors import PayloadError

__all__ = ["NO_ACK_ID", "ControlPacket"]

NO_ACK_ID = 0xFFFFFFFF
"""``WriteControl`` is a no-op for this id (``pv3.go:512`` ``CMPL BX, $-0x1``)."""

_HDR = struct.Struct("<II")


@dataclass(frozen=True, slots=True)
class ControlPacket:
    """A control payload."""

    flags: int
    payload: bytes = b""

    TYPE = PacketType.CONTROL

    @property
    def is_ack(self) -> bool:
        return bool(self.flags & ControlFlags.ACK)

    @property
    def is_nack(self) -> bool:
        return not self.is_ack

    @property
    def nack_charging(self) -> bool:
        """Bit 25: the device refused because it is charging."""
        return bool(self.flags & ControlFlags.NACK_CHARGING)

    @property
    def error_code(self) -> int | None:
        """The uint32 a device NACK carries in its payload, if any."""
        if len(self.payload) >= 4:
            return int.from_bytes(self.payload[:4], "little")
        return None

    @classmethod
    def ack(cls) -> "ControlPacket":
        """The ack every handler sends: ``Control{Flags: 1}``, empty payload."""
        return cls(flags=ControlFlags.ACK)

    @classmethod
    def nack(cls, error_code: int | None = None) -> "ControlPacket":
        """``Flags == 0``; the dispatcher's unknown-packet-type reply."""
        payload = b"" if error_code is None else error_code.to_bytes(4, "little")
        return cls(flags=0, payload=payload)

    @classmethod
    def decode(cls, payload: bytes) -> "ControlPacket":
        if len(payload) < 8:
            raise PayloadError(f"control payload is {len(payload)} bytes, need >= 8")
        flags, length = _HDR.unpack_from(payload, 0)
        return cls(flags=flags, payload=payload[8 : 8 + length])

    def encode(self) -> bytes:
        return _HDR.pack(self.flags, len(self.payload)) + self.payload

    def __str__(self) -> str:
        kind = "ack" if self.is_ack else "nack"
        if self.nack_charging:
            kind = "nack(charging)"
        code = self.error_code
        suffix = f" code=0x{code:08x}" if code is not None else ""
        return f"Control{{{kind} flags=0x{self.flags:08x}{suffix}}}"
