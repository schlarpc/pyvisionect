"""asyncio TCP listener -- the only networking code in the library.

The sign is a **client**: it dials out to TCP 11113 and speaks first.  So the
server's job is to listen, accept whatever connects, and let the sans-io
:class:`~pyvisionect.session.connection.DeviceConnection` do the rest.

There is no auth and no handshake, so this listener accepts every connection.
Put it somewhere only your signs can reach.

The vendor's gateway also sniffs the first 6 bytes for a TLS ClientHello
(``b[0] == 0x16``, ``b[1] == 0x03``, ``b[2] in 1..4``, ``b[5] == 0x01``) and
opportunistically upgrades.  We do not: the 8.5.5 image ships no certificate, so
real deployments are plaintext, and TCLV 145 (``TLS mode``) is read-only over
the network anyway.

Memory note: a full-screen push to the 32" panel is a single ~1.84 MB write.
Between the encoded rectangles, the block chain and the socket buffer, budget
roughly 6-8 MB of transient peak per device being pushed to.  The write itself
is a ``write`` + ``drain`` and is fine on the event loop; the *encoding* is not
(see :mod:`pyvisionect.imaging`).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from ..session import DeviceConnection, DeviceStateStore
from ..session.connection import ConnectionConfig
from ..session.events import Event
from ..wire.errors import ListenError

__all__ = ["DEFAULT_PORT", "ServerStats", "VisionectServer", "EventHandler"]

DEFAULT_PORT = 11113
"""``main.init`` registers ``flag.String("port", "11113", ...)``; it is not in
``config.json``."""

READ_CHUNK = 65536

log = logging.getLogger(__name__)

EventHandler = Callable[[DeviceConnection, list[Event]], Awaitable[None] | None]


@dataclass
class ServerStats:
    """Counters for telling apart the two ways this silently does nothing.

    "Nothing ever reaches the port" and "something connects but is not a sign"
    need opposite troubleshooting advice, and neither produces an exception.
    These counters are the cheapest way to tell them apart:

    * ``accepted == 0``  -> nothing is reaching the port at all. Check that the
      sign is pointed here (``server_tcp_set``, or the DNS name it holds),
      that the host firewall allows 11113, and that no other process holds it.
    * ``accepted > 0`` but ``identified == 0`` -> something is connecting but
      never sends a valid first status packet. Could be a port scanner, a
      health check, or a sign speaking a protocol version we do not handle.
    * ``accepted > silent_connections`` and ``identified > 0`` -> it works.

    One caveat worth knowing before you treat a counter as an error: the capture
    shows a connection that **opened and closed carrying zero protocol bytes**,
    from the real sign, in normal operation.  ``silent_connections`` counts
    those and they are not a fault.
    """

    accepted: int = 0
    """Connections accepted, including ones that sent nothing."""

    identified: int = 0
    """Connections that produced a valid first status packet (and so a UUID)."""

    silent_connections: int = 0
    """Accepted connections that closed without sending a single byte.

    Normal: observed from the real sign in the reference capture.
    """

    rejected: int = 0
    """Connections closed on a protocol violation."""

    bytes_in: int = 0
    bytes_out: int = 0

    last_accept_at: float | None = None
    last_seen: dict[bytes, float] = field(default_factory=dict)
    """UUID -> clock value of the last frame received from it."""

    def last_seen_uuid(self) -> dict[str, float]:
        """:attr:`last_seen` keyed on the canonical UUID string."""
        out = {}
        for device_id, when in self.last_seen.items():
            h = device_id.hex()
            out[f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"] = when
        return out


class VisionectServer:
    """Accepts device connections and drives a ``DeviceConnection`` per socket.

    Args:
        on_events: called with ``(connection, events)`` after every read and
            every timer tick. May be sync or async.
        store: the shared per-UUID state map. One is created if omitted; share
            your own if you want state to outlive the server object.
        config: passed to each :class:`DeviceConnection`.
        host / port: where to listen.
        clock: seconds-valued monotonic clock. Injectable for tests; this is the
            **only** place the library reads a clock.

    Example:
        >>> async def main():
        ...     async def handle(conn, events):
        ...         for e in events:
        ...             print(e)
        ...     server = VisionectServer(on_events=handle)
        ...     await server.serve_forever()
    """

    def __init__(
        self,
        *,
        on_events: EventHandler,
        store: DeviceStateStore | None = None,
        config: ConnectionConfig | None = None,
        host: str = "0.0.0.0",
        port: int = DEFAULT_PORT,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.on_events = on_events
        self.store = store if store is not None else DeviceStateStore()
        self.config = config or ConnectionConfig()
        self.host = host
        self.port = port
        self.clock = clock
        self.stats = ServerStats()
        self._server: asyncio.AbstractServer | None = None
        self._connections: dict[bytes, DeviceConnection] = {}
        self._writers: dict[bytes, asyncio.StreamWriter] = {}

    @property
    def connections(self) -> dict[bytes, DeviceConnection]:
        """Live connections keyed on the device UUID.

        Keyed on the UUID and nothing else: a reconnect comes from a different
        source port with its packet counter reset to 1.
        """
        return dict(self._connections)

    def connection_for(self, device_id: bytes) -> DeviceConnection | None:
        """The live connection for *device_id*, or None if it is not connected.

        **None is the common case.** A battery sign is absent most of the time.
        Queue work on ``store.queue(device_id)`` instead of waiting for this to
        be non-None; see :class:`~pyvisionect.session.pending.PendingWork`.
        """
        return self._connections.get(device_id)

    async def start(self) -> None:
        """Bind and start accepting.

        Raises:
            ListenError: instead of a bare :class:`OSError`, so a caller can
                branch on ``errno`` to tell "port busy" from "cannot bind that
                address".
        """
        try:
            self._server = await asyncio.start_server(
                self._handle_client, self.host, self.port
            )
        except OSError as exc:
            raise ListenError(self.host, self.port, exc) from exc
        log.info("listening for Visionect devices on %s:%d", self.host, self.port)

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def close(self, *, abort: bool = False) -> None:
        """Stop accepting, close live device connections, and wait.

        **The ``close_clients()`` call is load-bearing, and leaving it out
        hangs forever on this hardware.** Since Python 3.12,
        ``Server.wait_closed()`` waits until the server is closed *and all
        active connections have finished* -- and a mains-powered Visionect sign
        holds its TCP connection open permanently. So ``close()`` followed by
        ``wait_closed()`` never returns, and wrapping it in
        ``contextlib.suppress`` does nothing for a hang.

        The ordering is the one Python's own documentation mandates:
        ``close()`` *before* ``close_clients()``, "to avoid races with new
        clients connecting".

        There is no precedent to copy here: ``close_clients`` appears zero times
        in all of Home Assistant core, because no core integration hosts a
        listener for devices that hold connections open indefinitely.

        Args:
            abort: use ``abort_clients()`` instead of ``close_clients()``, which
                drops connections without draining pending writes. Worth it on a
                shutdown path, where finishing a half-written 1.84 MB frame is
                not worth waiting for.
        """
        if self._server is None:
            return
        server, self._server = self._server, None
        server.close()
        # Python 3.13+ has both; guard anyway so the failure mode is a logged
        # warning rather than an AttributeError during shutdown.
        closer = getattr(server, "abort_clients" if abort else "close_clients", None)
        if closer is not None:
            closer()
        else:  # pragma: no cover - only on an unexpectedly old runtime
            log.warning(
                "asyncio.Server has no close_clients(); wait_closed() may hang "
                "while a device holds its connection open"
            )
        with contextlib.suppress(Exception):
            await server.wait_closed()

    # --------------------------------------------------------------------

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        conn = DeviceConnection(store=self.store, config=self.config)
        self.stats.accepted += 1
        self.stats.last_accept_at = self.clock()
        log.info("device connected from %s", peer)
        registered: bytes | None = None
        received_any = False
        timer_task = asyncio.create_task(self._run_timers(conn, writer))
        try:
            while not conn.closed:
                data = await reader.read(READ_CHUNK)
                if not data:
                    break
                received_any = True
                self.stats.bytes_in += len(data)
                now = self.clock()
                events = conn.feed_at(data, now)
                if registered is None and conn.device_id is not None:
                    registered = conn.device_id
                    self.stats.identified += 1
                    self._connections[registered] = conn
                    self._writers[registered] = writer
                if registered is not None:
                    self.stats.last_seen[registered] = now
                await self._flush(conn, writer)
                if events:
                    await self._dispatch(conn, events)
        except (ConnectionResetError, asyncio.IncompleteReadError):
            log.info("device %s dropped the connection", peer)
        finally:
            if not received_any:
                # Normal, not an error: the capture shows the real sign opening
                # and closing a connection carrying no protocol bytes at all.
                self.stats.silent_connections += 1
            if conn.closed and received_any:
                self.stats.rejected += 1
            timer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await timer_task
            if registered is not None and self._connections.get(registered) is conn:
                del self._connections[registered]
                self._writers.pop(registered, None)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            log.info("device %s disconnected", peer)

    async def _run_timers(
        self, conn: DeviceConnection, writer: asyncio.StreamWriter
    ) -> None:
        """Poll ``advance()`` once a second.

        A second of granularity is plenty: the shortest timer here is the
        read timeout at 300 s and the watchdog at 15 min.
        """
        while not conn.closed:
            await asyncio.sleep(1.0)
            events = conn.advance(self.clock())
            await self._flush(conn, writer)
            if events:
                await self._dispatch(conn, events)

    async def _flush(
        self, conn: DeviceConnection, writer: asyncio.StreamWriter
    ) -> None:
        data = conn.drain()
        if data:
            self.stats.bytes_out += len(data)
            writer.write(data)
            await writer.drain()

    async def _dispatch(self, conn: DeviceConnection, events: list[Event]) -> None:
        result = self.on_events(conn, events)
        if asyncio.iscoroutine(result):
            await result

    async def flush(self, device_id: bytes) -> None:
        """Push whatever a caller queued on a device's connection.

        Call this after queueing from *outside* the read loop, e.g.::

            conn = server.connection_for(uuid)
            conn.send_image(frame)
            await server.flush(uuid)

        Queueing from inside an ``on_events`` callback needs no explicit flush;
        the server drains after every callback.

        Raises:
            KeyError: if that UUID has no live connection.
        """
        conn = self._connections.get(device_id)
        writer = self._writers.get(device_id)
        if conn is None or writer is None:
            raise KeyError(f"no live connection for {device_id.hex()}")
        await self._flush(conn, writer)
