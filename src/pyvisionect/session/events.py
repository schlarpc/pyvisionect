"""Events a :class:`~pyvisionect.session.connection.DeviceConnection` emits.

All events are frozen dataclasses with no behaviour.  ``feed()`` and
``advance()`` return lists of them; the caller decides what to do.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..packets import (
    ButtonPacket,
    CborPacket,
    ControlPacket,
    FilePacket,
    GpsPacket,
    ParamPacket,
    StatusPacket,
    TouchPacket,
)

__all__ = [
    "Event",
    "DeviceConnected",
    "StatusReceived",
    "Acked",
    "Nacked",
    "ParamsReceived",
    "FileEventReceived",
    "TouchReceived",
    "ButtonReceived",
    "GpsReceived",
    "CborReceived",
    "UnknownPacketType",
    "ProtocolViolation",
    "WatchdogFired",
    "ReadTimeout",
    "HeartbeatOverdue",
]


@dataclass(frozen=True, slots=True)
class Event:
    """Base class. Carries the device UUID when one is known."""

    device_id: bytes = field(default=b"", kw_only=True)

    @property
    def uuid(self) -> str:
        """The UUID in canonical 8-4-4-4-12 form, or ``""``."""
        if len(self.device_id) != 16:
            return ""
        h = self.device_id.hex()
        return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


@dataclass(frozen=True, slots=True)
class DeviceConnected(Event):
    """The first status packet of a TCP connection arrived.

    There is no handshake: the UUID in this packet's ``DataHeader`` is the entire
    identity claim, and registration is auto-create on first connect.  The
    gateway requires the first packet to be a status packet
    (``main.ErrNotFirstStatusPacket``).
    """

    status: StatusPacket = field(default_factory=StatusPacket)
    packet_id: int = 0
    connect_reason: int | None = None


@dataclass(frozen=True, slots=True)
class StatusReceived(Event):
    """A status packet (type 3). Also resets the server's inactivity watchdog."""

    status: StatusPacket = field(default_factory=StatusPacket)
    packet_id: int = 0


@dataclass(frozen=True, slots=True)
class Acked(Event):
    """The device acked one of our requests (``Control{Flags & 1}``)."""

    packet_id: int = 0
    control: ControlPacket | None = None


@dataclass(frozen=True, slots=True)
class Nacked(Event):
    """The device refused one of our requests.

    ``charging`` is control flag bit 25 (``vio.ErrNackCharging``).
    ``error_code`` is the uint32 a device NACK carries in its payload, e.g.
    ``0x00008a00`` for the ``@screen_N`` open the 32" sign always refuses.
    """

    packet_id: int = 0
    control: ControlPacket | None = None
    charging: bool = False
    error_code: int | None = None


@dataclass(frozen=True, slots=True)
class ParamsReceived(Event):
    """A param packet (type 8) -- the reply to a read, or a write result."""

    params: ParamPacket = field(default_factory=ParamPacket)
    packet_id: int = 0


@dataclass(frozen=True, slots=True)
class FileEventReceived(Event):
    """A file packet (type 10) from the device."""

    file: FilePacket | None = None
    packet_id: int = 0


@dataclass(frozen=True, slots=True)
class TouchReceived(Event):
    touch: TouchPacket | None = None
    packet_id: int = 0


@dataclass(frozen=True, slots=True)
class ButtonReceived(Event):
    button: ButtonPacket | None = None
    packet_id: int = 0


@dataclass(frozen=True, slots=True)
class GpsReceived(Event):
    gps: GpsPacket | None = None
    packet_id: int = 0


@dataclass(frozen=True, slots=True)
class CborReceived(Event):
    """The "AC"/JS-app channel. Firmware 7.4.4407 never sends this."""

    cbor: CborPacket | None = None
    packet_id: int = 0


@dataclass(frozen=True, slots=True)
class UnknownPacketType(Event):
    """A packet type with no handler. We NACK it, as the gateway does."""

    packet_type: int = 0
    packet_id: int = 0
    payload: bytes = b""


@dataclass(frozen=True, slots=True)
class ProtocolViolation(Event):
    """Something the peer did that the protocol does not allow.

    The connection should normally be closed. Carries the exception when one was
    raised.
    """

    reason: str = ""
    error: Exception | None = None


@dataclass(frozen=True, slots=True)
class WatchdogFired(Event):
    """The inactivity watchdog expired and a status request has been queued.

    The gateway's behaviour exactly: on expiry it sends
    ``Command{Type: 11}`` ("status request") and closes the connection if that
    request fails.
    """

    quiet_for: float = 0.0


@dataclass(frozen=True, slots=True)
class ReadTimeout(Event):
    """No frame arrived within ``PacketTimeout`` seconds (config default 300)."""

    quiet_for: float = 0.0


@dataclass(frozen=True, slots=True)
class HeartbeatOverdue(Event):
    """The device missed the deadline it announced in status tag 27 ``NextStatus``.

    Advisory only: ``NextStatus`` is in **minutes** and the device's own
    heartbeat interval (TCLV 29) is what really governs. Nothing in the gateway
    acts on this; it is exposed because it is a cheap health signal.
    """

    expected_within: float = 0.0
    quiet_for: float = 0.0
