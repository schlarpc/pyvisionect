"""The remaining payload types: command, touch, button, GPS, CBOR.

All little-endian, all small.  ``command`` (2) is server-to-device only; the
others are device-to-server.

.. note::
   **Packet type 2 (``command``) is captured for 2 of its 24 command ids.**

   ``sleep`` (4) and ``status request`` (11) were provoked out of the vendor
   server on 2026-10-05 and decoded off the wire by :class:`CommandPacket`
   unmodified::

       sleep           04000000 04000000 00000000 8e030000
       status request  0b000000 00000000 00000000

   So the 12-byte ``CommandHeader`` (``Type`` / ``PayloadLength`` /
   ``Reserved``) is **real**, and ``Reserved`` is emitted.  The precedent that
   made it suspect no longer applies: ``proto.Command``'s struct is 12 bytes
   and the param packet's on-wire header is **8**, because ``Reserved`` is
   simply dropped there -- but type 2 emits it.  The zero-payload status
   request settles that on its own: 12 bytes of header, nothing after.  The
   device acks both frames with ``Control{Flags: 1}``, and the status that
   answers a status request carries ``ConnectReason == 8`` ("by server
   request").

   **Every other command id has never been observed on the wire**, in either
   direction, so the payload and effect of the remaining 22 are code-read
   only.  Five of them have **no emission site anywhere in the vendor
   server**: ``9`` (LED window), ``10`` (LDRLED), ``-1`` (refresh), ``-2``
   (clear screen) and ``-3`` (keyboard).  ``-4`` ("beep") does have one, built
   inline in ``webkit.generateCommand``; and ``15`` is emitted by
   ``stdcmd.DisplayID.Done`` even though both ``String()`` chains print
   ``unknown`` for it.

   Consequences for a consumer:

   * prefer a verified route wherever one exists -- an image push instead of
     ``refresh``/``clear_screen``, a TCLV write instead of ``set_heartbeat``;
   * ``ConnectionConfig(allow_command_packets=False)`` refuses to emit type 2
     at all, so an integration can ship a "safe mode" and a test suite can
     assert that nothing slipped through.  Two ids out of 24 is not enough to
     take the other 22 on trust;
   * a reboot *is* observable after the fact: the device reconnects with
     ``ConnectReason == 1``, which
     :attr:`~pyvisionect.packets.StatusPacket.connect_reason_name` reports.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from ..devices.enums import CommandType, PacketType
from ..wire.errors import PayloadError

__all__ = [
    "CommandPacket",
    "TouchPacket",
    "ButtonPacket",
    "GpsPacket",
    "CborPacket",
]

_CMD = struct.Struct("<iII")
_TOUCH = struct.Struct("<5I")
_BUTTON = struct.Struct("<2I")
_CBOR = struct.Struct("<3I")


@dataclass(slots=True)
class CommandPacket:
    """``packet.CommandHeader`` + payload (12 + N bytes).

    The 12-byte header is confirmed on the wire, ``Reserved`` included: this
    codec decoded both captured type-2 frames with no change (see the module
    note).

    ``Type`` is a signed int32: ``-1`` refresh, ``-2`` clear screen,
    ``-3`` keyboard, ``-4`` beep are all real values.  ``0`` is **echo** and
    ``1`` is **reboot**, in both the v1 and the v2 enum.
    """

    type: int
    payload: bytes = b""
    reserved: int = 0

    TYPE = PacketType.COMMAND

    @property
    def type_name(self) -> str:
        return CommandType.NAMES.get(self.type, "unknown")

    @classmethod
    def decode(cls, payload: bytes) -> "CommandPacket":
        if len(payload) < 12:
            raise PayloadError(f"command payload is {len(payload)} bytes, need >= 12")
        type_, length, reserved = _CMD.unpack_from(payload, 0)
        return cls(type=type_, payload=payload[12 : 12 + length], reserved=reserved)

    def encode(self) -> bytes:
        return _CMD.pack(self.type, len(self.payload), self.reserved) + self.payload


@dataclass(frozen=True, slots=True)
class TouchPacket:
    """``packet.Touch`` -- five uint32s, 20 bytes."""

    display_crc: int
    display_id: int
    finger_num: int
    x: int
    y: int

    TYPE = PacketType.TOUCH

    @classmethod
    def decode(cls, payload: bytes) -> "TouchPacket":
        if len(payload) < 20:
            raise PayloadError(f"touch payload is {len(payload)} bytes, need 20")
        return cls(*_TOUCH.unpack_from(payload, 0))

    def encode(self) -> bytes:
        return _TOUCH.pack(
            self.display_crc, self.display_id, self.finger_num, self.x, self.y
        )


@dataclass(frozen=True, slots=True)
class ButtonPacket:
    """``packet.Button`` -- ``Active``, ``State``; 8 bytes."""

    active: int
    state: int

    TYPE = PacketType.BUTTON

    @classmethod
    def decode(cls, payload: bytes) -> "ButtonPacket":
        if len(payload) < 8:
            raise PayloadError(f"button payload is {len(payload)} bytes, need 8")
        return cls(*_BUTTON.unpack_from(payload, 0))

    def encode(self) -> bytes:
        return _BUTTON.pack(self.active, self.state)


@dataclass(frozen=True, slots=True)
class GpsPacket:
    """``packet.GPS`` -- ``PowerState uint32`` then a coordinate string.

    The Go struct holds a ``string``; the on-wire framing of that string was not
    recovered (no GPS device was available), so the remainder is kept as bytes
    and exposed as NUL-trimmed ASCII.  [GAP]
    """

    power_state: int
    raw: bytes = b""

    TYPE = PacketType.GPS

    @property
    def coordinates(self) -> str:
        return self.raw.split(b"\x00")[0].decode("ascii", errors="replace")

    @classmethod
    def decode(cls, payload: bytes) -> "GpsPacket":
        if len(payload) < 4:
            raise PayloadError(f"gps payload is {len(payload)} bytes, need >= 4")
        (power,) = struct.unpack_from("<I", payload, 0)
        return cls(power_state=power, raw=payload[4:])

    def encode(self) -> bytes:
        return struct.pack("<I", self.power_state) + self.raw


@dataclass(slots=True)
class CborPacket:
    """``packet.CBORHeader`` + CBOR bytes (``packet/cbor.go:18-46``).

    The "AC" (JS-app) device generation's channel.  Firmware 7.4.4407 never
    emits it, so this codec is framing-only: the CBOR body is not parsed.
    """

    user_data: int = 0
    data: bytes = b""
    reserved: int = 0

    TYPE = PacketType.CBOR

    @classmethod
    def decode(cls, payload: bytes) -> "CborPacket":
        if len(payload) < 12:
            raise PayloadError(f"cbor payload is {len(payload)} bytes, need >= 12")
        user_data, reserved, length = _CBOR.unpack_from(payload, 0)
        if len(payload) < 12 + length:
            raise PayloadError(
                f"cbor declares {length} bytes, only {len(payload) - 12} remain"
            )
        return cls(user_data=user_data, data=payload[12 : 12 + length], reserved=reserved)

    def encode(self) -> bytes:
        return _CBOR.pack(self.user_data, self.reserved, len(self.data)) + self.data
