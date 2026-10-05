"""pyvisionect -- a sans-io reimplementation of the Visionect sign wire protocol.

**You are the server.**  A Visionect sign is a TCP *client*: it dials out to port
11113 and speaks first.  There is no handshake, no auth and no negotiation -- the
16-byte UUID in the packet header is the entire identity claim, and registration
is auto-create on first connect.  So this library replaces the vendor's server:
it listens, accepts whatever connects, and drives the sign directly.  No VSS, no
licence server, no cloud.

Layering (each layer pure except the last)::

    io/        asyncio TCP listener; pyserial USB provisioning   <- only I/O
    session/   DeviceConnection: bytes in -> Events out
    packets/   per-type payload codecs
    wire/      framing, LZ4 block chain, direction-dependent CRC

    imaging/   PIL/numpy -> dithered, bit-packed rectangles (separate module)
    devices/   capability tables: TCLV, status enums, panel geometry (data)

Quick start::

    import asyncio
    from pyvisionect.io import VisionectServer
    from pyvisionect.session import DeviceConnected, StatusReceived

    async def on_events(conn, events):
        for e in events:
            if isinstance(e, DeviceConnected):
                print("sign online:", e.uuid, conn.state.panel.describe())
                conn.apply_pending()          # drain whatever was queued while it slept
            elif isinstance(e, StatusReceived):
                print("battery", e.status.fields().get("BatteryLevel"))

    asyncio.run(VisionectServer(on_events=on_events).serve_forever())

The sign must be *pointed* at your listener first, either over USB
(:mod:`pyvisionect.io.usb`) or by re-pointing the DNS name it
already holds in flash.  The server address is read-only over the network; see
:mod:`pyvisionect.devices.tclv`.

A sign is **absent most of the time**, so ``server.connection_for(uuid)``
returning ``None`` is the normal case.  Queue work on
``store.queue(uuid)`` -- a durable, coalescing
:class:`~pyvisionect.session.pending.PendingWork` register -- and let
:meth:`~pyvisionect.session.DeviceConnection.apply_pending` drain it, in the
right order, the next time the sign appears.
"""

from __future__ import annotations

__version__ = "0.1.0"

from .devices import TCLV, Panel, panel_for
from .packets import (
    ControlPacket,
    ImagePacket,
    ParamItem,
    ParamPacket,
    Rectangle,
    StatusPacket,
)
from .session import (
    PENDING_ORDER,
    FileRead,
    SyncStatus,
    ConnectionConfig,
    DeviceConnected,
    DeviceConnection,
    DeviceState,
    DeviceStateStore,
    Event,
    PendingWork,
    StatusReceived,
)
from .wire import (
    STORED_ONLY,
    DataHeader,
    Direction,
    Frame,
    FrameDecoder,
    ListenError,
    ProtocolHeader,
    ReadOnlyParameter,
    VisionectError,
    encode_frame,
)

__all__ = [
    "ConnectionConfig",
    "ControlPacket",
    "DataHeader",
    "DeviceConnected",
    "DeviceConnection",
    "DeviceState",
    "DeviceStateStore",
    "Direction",
    "Event",
    "FileRead",
    "Frame",
    "FrameDecoder",
    "ImagePacket",
    "ListenError",
    "PENDING_ORDER",
    "Panel",
    "ParamItem",
    "ParamPacket",
    "PendingWork",
    "ProtocolHeader",
    "ReadOnlyParameter",
    "STORED_ONLY",
    "Rectangle",
    "StatusPacket",
    "StatusReceived",
    "SyncStatus",
    "TCLV",
    "VisionectError",
    "__version__",
    "encode_frame",
    "panel_for",
]
