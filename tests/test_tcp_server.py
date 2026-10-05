"""The asyncio listener: shutdown, bind errors, and the stats counters.

The shutdown test is a **regression test for a hang**, which is why it is here
rather than left to integration testing.
"""

from __future__ import annotations

import asyncio
import errno
import socket
import struct

import pytest

from pyvisionect.devices.enums import PacketType
from pyvisionect.io import DEFAULT_PORT, VisionectServer
from pyvisionect.packets import StatusPacket
from pyvisionect.session import DeviceConnected, DeviceStateStore
from pyvisionect.wire import Compression, DataHeader, Direction, encode_frame
from pyvisionect.wire.errors import ListenError

UUID = bytes.fromhex("00112233445566778899aabb00000000")

STATUS_TAGS = [
    (0, 3), (1, 0), (8, 0xC2050128), (9, 0xDB963A1A), (10, 100),
    (16, 7), (17, 4), (18, 4407), (22, 8), (27, 1), (40, 3), (43, 585), (54, 1),
]


def status_frame(packet_id: int = 1) -> bytes:
    payload = StatusPacket(records=STATUS_TAGS, sentinel=0xDEADBEEF).encode()
    return encode_frame(
        DataHeader(device_id=UUID, type=PacketType.STATUS, id=packet_id,
                   length=len(payload)),
        payload,
        direction=Direction.DEVICE_TO_SERVER,
        compression=Compression.NONE,
    )


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _serve(**kwargs) -> tuple[VisionectServer, int, list]:
    seen: list = []

    async def on_events(conn, events):
        seen.extend(events)

    port = free_port()
    server = VisionectServer(
        on_events=on_events, host="127.0.0.1", port=port, **kwargs
    )
    await server.start()
    return server, port, seen


def test_default_port_is_the_vendors() -> None:
    assert DEFAULT_PORT == 11113


@pytest.mark.parametrize("abort", [False, True])
def test_close_does_not_hang_with_a_client_still_connected(abort: bool) -> None:
    """Regression: since Python 3.12 ``wait_closed()`` waits for live clients too.

    A mains-powered sign holds its connection open permanently, so
    ``close()`` + ``wait_closed()`` without ``close_clients()`` never returns.
    In Home Assistant that surfaces as a config entry that cannot be reloaded
    and a port that stays bound.
    """

    async def scenario() -> None:
        server, port, _seen = await _serve()
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(status_frame())
            await writer.drain()
            await asyncio.sleep(0.05)
            assert server.stats.identified == 1
            # The client is deliberately left open, exactly as a sign does.
            await asyncio.wait_for(server.close(abort=abort), timeout=2.0)
        finally:
            writer.close()
            with _ignore():
                await writer.wait_closed()

    asyncio.run(asyncio.wait_for(scenario(), timeout=10.0))


class _ignore:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return True


def test_the_port_is_released_so_a_reload_can_rebind() -> None:
    """The symptom of the old bug downstream: EADDRINUSE on a config reload."""

    async def scenario() -> None:
        server, port, _ = await _serve()
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(status_frame())
        await writer.drain()
        await asyncio.sleep(0.05)
        await asyncio.wait_for(server.close(), timeout=2.0)
        writer.close()

        # Rebinding the same port must now succeed -- this is the reload path.
        again = VisionectServer(
            on_events=lambda c, e: None, host="127.0.0.1", port=port
        )
        await again.start()
        await asyncio.wait_for(again.close(), timeout=2.0)

    asyncio.run(asyncio.wait_for(scenario(), timeout=10.0))


def test_close_is_idempotent() -> None:
    async def scenario() -> None:
        server, _port, _ = await _serve()
        await server.close()
        await server.close()

    asyncio.run(asyncio.wait_for(scenario(), timeout=10.0))


def test_bind_failure_is_a_listen_error_with_an_errno() -> None:
    async def scenario() -> None:
        first, port, _ = await _serve()
        try:
            second = VisionectServer(
                on_events=lambda c, e: None, host="127.0.0.1", port=port
            )
            with pytest.raises(ListenError) as excinfo:
                await second.start()
            error = excinfo.value
            assert error.errno == errno.EADDRINUSE
            assert error.port == port
            assert error.host == "127.0.0.1"
            assert "Visionect server" in str(error)
            assert isinstance(error.__cause__, OSError)
        finally:
            await first.close()

    asyncio.run(asyncio.wait_for(scenario(), timeout=10.0))


def test_bad_address_is_also_a_listen_error() -> None:
    """An unbindable host still gives a ListenError naming host and port.

    Note the errno may legitimately be ``None``: when several candidate
    addresses all fail, asyncio raises a summary ``OSError`` and discards the
    per-address errno. The error says so rather than pretending.
    """
    port = free_port()

    async def scenario() -> None:
        server = VisionectServer(
            on_events=lambda c, e: None, host="203.0.113.1", port=port
        )
        with pytest.raises(ListenError) as excinfo:
            await server.start()
        error = excinfo.value
        assert error.host == "203.0.113.1"
        assert error.port == port
        assert error.errno in (None, errno.EADDRNOTAVAIL, errno.EACCES, errno.EINVAL)
        assert "203.0.113.1" in str(error) and str(port) in str(error)

    asyncio.run(asyncio.wait_for(scenario(), timeout=10.0))


def test_a_real_device_connection_is_identified_and_acked() -> None:
    async def scenario() -> None:
        server, port, seen = await _serve()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(status_frame())
            await writer.drain()
            ack = await asyncio.wait_for(reader.read(4096), timeout=2.0)
            assert len(ack) > 20
            version, _sec, comp, length, _ck = struct.unpack_from("<5I", ack, 0)
            assert version == 3 and comp == Compression.LZ4
            assert any(isinstance(e, DeviceConnected) for e in seen)
            assert server.connection_for(UUID) is not None
            assert UUID in server.stats.last_seen
            assert server.stats.accepted == 1
            assert server.stats.identified == 1
            assert server.stats.bytes_in == len(status_frame())
            assert server.stats.bytes_out == len(ack)
            writer.close()
        finally:
            await server.close()

    asyncio.run(asyncio.wait_for(scenario(), timeout=10.0))


def test_a_connection_carrying_no_bytes_is_counted_but_not_an_error() -> None:
    """The capture shows the real sign doing exactly this. It is normal."""

    async def scenario() -> None:
        server, port, seen = await _serve()
        try:
            _reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            await asyncio.sleep(0.1)
            assert server.stats.accepted == 1
            assert server.stats.silent_connections == 1
            assert server.stats.identified == 0
            assert server.stats.rejected == 0
            assert seen == []
        finally:
            await server.close()

    asyncio.run(asyncio.wait_for(scenario(), timeout=10.0))


def test_stats_tell_nothing_reaching_the_port_from_not_a_sign() -> None:
    async def scenario() -> None:
        server, port, _ = await _serve()
        try:
            assert server.stats.accepted == 0  # "nothing is reaching the port"
            _reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(struct.pack("<5I", 99, 0, 0, 0, 0))  # wrong version
            await writer.drain()
            await asyncio.sleep(0.1)
            # "something connects but is not a sign": accepted, never identified
            assert server.stats.accepted == 1
            assert server.stats.identified == 0
            assert server.stats.rejected == 1
            writer.close()
        finally:
            await server.close()

    asyncio.run(asyncio.wait_for(scenario(), timeout=10.0))


def test_last_seen_uuid_is_human_readable() -> None:
    async def scenario() -> None:
        server, port, _ = await _serve()
        try:
            _reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(status_frame())
            await writer.drain()
            await asyncio.sleep(0.1)
            assert "00112233-4455-6677-8899-aabb00000000" in server.stats.last_seen_uuid()
            writer.close()
        finally:
            await server.close()

    asyncio.run(asyncio.wait_for(scenario(), timeout=10.0))


def test_flush_pushes_work_queued_outside_the_read_loop() -> None:
    async def scenario() -> None:
        store = DeviceStateStore()
        server, port, _ = await _serve(store=store)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(status_frame())
            await writer.drain()
            await asyncio.wait_for(reader.read(4096), timeout=2.0)  # the ack

            conn = server.connection_for(UUID)
            assert conn is not None
            conn.read_params([29])
            await server.flush(UUID)
            data = await asyncio.wait_for(reader.read(4096), timeout=2.0)
            assert len(data) > 20
            with pytest.raises(KeyError):
                await server.flush(b"\x00" * 16)
            writer.close()
        finally:
            await server.close()

    asyncio.run(asyncio.wait_for(scenario(), timeout=10.0))
