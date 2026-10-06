"""The asyncio listener: shutdown, bind errors, and the stats counters.

The shutdown test is a **regression test for a hang**, which is why it is here
rather than left to integration testing.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import socket
import ssl
import struct
from pathlib import Path

import pytest

from pyvisionect.devices.enums import PacketType
from pyvisionect.io import DEFAULT_PORT, VisionectServer
from pyvisionect.io.tcp import looks_like_client_hello, server_ssl_context
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


# --------------------------------------------------------------- TLS


CERT = Path(__file__).parent / "data" / "test-tls-cert.pem"
KEY = Path(__file__).parent / "data" / "test-tls-key.pem"


def client_hello_prefix() -> bytes:
    """The six bytes the sniffer looks at, for a TLS 1.3 ClientHello.

    ``16`` handshake record, ``03 01`` the legacy record version TLS 1.3 still
    puts on the wire, two length bytes, ``01`` ClientHello.
    """
    return bytes([0x16, 0x03, 0x01, 0x02, 0x00, 0x01])


def test_the_sniffer_matches_the_vendors_test() -> None:
    assert looks_like_client_hello(client_hello_prefix())
    # Every legacy record version the gateway accepts.
    for minor in (1, 2, 3, 4):
        assert looks_like_client_hello(bytes([0x16, 0x03, minor, 0, 0, 0x01]))
    # And the ones it does not.
    assert not looks_like_client_hello(bytes([0x16, 0x03, 0x00, 0, 0, 0x01]))
    assert not looks_like_client_hello(bytes([0x16, 0x03, 0x05, 0, 0, 0x01]))
    assert not looks_like_client_hello(bytes([0x17, 0x03, 0x03, 0, 0, 0x01]))
    assert not looks_like_client_hello(bytes([0x16, 0x03, 0x03, 0, 0, 0x02]))
    # Short is not TLS, it is a connection that closed.
    assert not looks_like_client_hello(b"")
    assert not looks_like_client_hello(client_hello_prefix()[:5])


def test_a_real_protocol_header_is_not_mistaken_for_a_client_hello() -> None:
    """``Version`` is a LE uint32, so version 3 is ``03 00 00 00`` on the wire.

    That is the collision worth being explicit about: the sniffer's first test
    is on byte 0, and byte 0 of every frame this library speaks is ``0x03``.
    """
    frame = status_frame()
    assert frame[:4] == b"\x03\x00\x00\x00"
    assert not looks_like_client_hello(frame[:6])
    # Belt and braces: no prefix of a real frame passes the test.
    for n in range(0, 64):
        assert not looks_like_client_hello(frame[n : n + 6])


def test_tls_and_plaintext_clients_share_one_listener() -> None:
    """One port, both protocols, decided per connection by the first 6 bytes."""

    async def scenario() -> None:
        server, port, seen = await _serve(certfile=str(CERT), keyfile=str(KEY))
        try:
            # --- plaintext, exactly as before -------------------------------
            plain_reader, plain_writer = await asyncio.open_connection(
                "127.0.0.1", port
            )
            plain_writer.write(status_frame())
            await plain_writer.drain()
            ack = await asyncio.wait_for(plain_reader.read(4096), timeout=2.0)
            version, _sec, comp, _len, _ck = struct.unpack_from("<5I", ack, 0)
            assert version == 3 and comp == Compression.LZ4

            # --- TLS, same port ---------------------------------------------
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            tls_reader, tls_writer = await asyncio.open_connection(
                "127.0.0.1", port, ssl=context
            )
            tls_writer.write(status_frame(packet_id=2))
            await tls_writer.drain()
            ack = await asyncio.wait_for(tls_reader.read(4096), timeout=5.0)
            version, _sec, comp, _len, _ck = struct.unpack_from("<5I", ack, 0)
            assert version == 3 and comp == Compression.LZ4

            assert server.stats.accepted == 2
            assert server.stats.identified == 2
            assert server.stats.tls_accepted == 1
            assert server.stats.tls_failed == 0
            assert server.stats.tls_unsupported == 0
            assert sum(isinstance(e, DeviceConnected) for e in seen) == 2

            plain_writer.close()
            tls_writer.close()
        finally:
            await server.close()

    asyncio.run(asyncio.wait_for(scenario(), timeout=20.0))


def test_a_push_survives_the_tunnel() -> None:
    """Server-to-device traffic has to come back out of the TLS stream intact.

    A read-params request is the smallest frame the server originates, and it
    exercises the ``write`` / ``drain`` path that a 1.84 MB image push uses.
    """

    async def scenario() -> None:
        server, port, _ = await _serve(certfile=str(CERT), keyfile=str(KEY))
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", port, ssl=context
            )
            writer.write(status_frame())
            await writer.drain()
            await asyncio.wait_for(reader.read(4096), timeout=5.0)  # the ack

            conn = server.connection_for(UUID)
            assert conn is not None
            conn.read_params([29])
            await server.flush(UUID)
            data = await asyncio.wait_for(reader.read(4096), timeout=5.0)
            version, _sec, _comp, _len, _ck = struct.unpack_from("<5I", data, 0)
            assert version == 3
            writer.close()
        finally:
            await server.close()

    asyncio.run(asyncio.wait_for(scenario(), timeout=20.0))


@pytest.mark.parametrize("abort", [False, True])
def test_closing_with_a_tls_client_still_connected_does_not_hang(abort: bool) -> None:
    """The ``close_clients()`` regression, again, with TLS in the way.

    Worth its own case: a TLS connection holds an ``SSLObject`` and a second
    layer of transport, and the sign holds it open just as permanently.
    """

    async def scenario() -> None:
        server, port, _ = await _serve(certfile=str(CERT), keyfile=str(KEY))
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=context)
        try:
            writer.write(status_frame())
            await writer.drain()
            await asyncio.wait_for(reader.read(4096), timeout=5.0)
            assert server.stats.tls_accepted == 1
            # Deliberately left open, exactly as a mains-powered sign does.
            await asyncio.wait_for(server.close(abort=abort), timeout=5.0)
        finally:
            writer.close()
            with _ignore():
                await writer.wait_closed()

    asyncio.run(asyncio.wait_for(scenario(), timeout=20.0))


def test_a_client_hello_without_a_certificate_is_counted_and_said_out_loud(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The one way to strand a sign: TCLV 145 on, no certificate here.

    Without the counter this is a silent ``rejected``, indistinguishable from a
    port scanner, and the sign is unreachable until someone finds a USB cable.
    """

    async def scenario() -> None:
        server, port, seen = await _serve()
        try:
            assert server.ssl_context is None
            _reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(client_hello_prefix() + b"\x00" * 64)
            await writer.drain()
            await asyncio.sleep(0.1)
            assert server.stats.accepted == 1
            assert server.stats.tls_unsupported == 1
            assert server.stats.identified == 0
            assert seen == []
            writer.close()
        finally:
            await server.close()

    with caplog.at_level(logging.ERROR, logger="pyvisionect.io.tcp"):
        asyncio.run(asyncio.wait_for(scenario(), timeout=10.0))
    assert "TCLV 145" in caplog.text


def test_a_failed_handshake_is_counted_not_raised() -> None:
    """A device that dislikes the certificate must not take the listener down."""

    async def scenario() -> None:
        server, port, _ = await _serve(certfile=str(CERT), keyfile=str(KEY))
        try:
            # A well-formed ClientHello prefix followed by rubbish: OpenSSL
            # rejects the record and the connection dies, alone.
            _reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(client_hello_prefix() + b"\xff" * 512)
            await writer.drain()
            await asyncio.sleep(0.2)
            assert server.stats.tls_failed == 1
            assert server.stats.tls_accepted == 0
            writer.close()

            # And the listener still works, in both protocols.
            reader2, writer2 = await asyncio.open_connection("127.0.0.1", port)
            writer2.write(status_frame())
            await writer2.drain()
            ack = await asyncio.wait_for(reader2.read(4096), timeout=2.0)
            assert len(ack) > 20
            writer2.close()
        finally:
            await server.close()

    asyncio.run(asyncio.wait_for(scenario(), timeout=20.0))


def test_a_silent_connection_is_still_silent_with_tls_configured() -> None:
    """The peek must not turn the normal zero-byte connection into an error."""

    async def scenario() -> None:
        server, port, seen = await _serve(certfile=str(CERT), keyfile=str(KEY))
        try:
            _reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            await asyncio.sleep(0.1)
            assert server.stats.accepted == 1
            assert server.stats.silent_connections == 1
            assert server.stats.rejected == 0
            assert server.stats.tls_failed == 0
            assert seen == []
        finally:
            await server.close()

    asyncio.run(asyncio.wait_for(scenario(), timeout=10.0))


def test_a_frame_split_across_the_peek_boundary_still_decodes() -> None:
    """The peek eats the first 6 bytes; they have to come back in order.

    Writing a frame three bytes at a time is the cheap way to prove it, since
    the sniff then necessarily straddles two socket reads.
    """

    async def scenario() -> None:
        server, port, seen = await _serve(certfile=str(CERT), keyfile=str(KEY))
        try:
            frame = status_frame()
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            for i in range(0, len(frame), 3):
                writer.write(frame[i : i + 3])
                await writer.drain()
            ack = await asyncio.wait_for(reader.read(4096), timeout=2.0)
            assert len(ack) > 20
            assert server.stats.identified == 1
            assert any(isinstance(e, DeviceConnected) for e in seen)
            writer.close()
        finally:
            await server.close()

    asyncio.run(asyncio.wait_for(scenario(), timeout=10.0))


def test_ssl_context_and_certfile_are_mutually_exclusive() -> None:
    with pytest.raises(ValueError, match="not both"):
        VisionectServer(
            on_events=lambda c, e: None,
            ssl_context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER),
            certfile=str(CERT),
        )


def test_server_ssl_context_is_tls_13_only_and_asks_for_no_client_cert() -> None:
    """Matches the gateway's ``MinVersion: 0x0304`` and parameter 145's name."""
    context = server_ssl_context(str(CERT), str(KEY))
    assert context.minimum_version is ssl.TLSVersion.TLSv1_3
    assert context.verify_mode is ssl.CERT_NONE


def test_a_connection_that_never_identifies_is_closed() -> None:
    """A device that completes the handshake and says nothing is a zombie.

    Seen twice on real hardware after a Home Assistant restart: ``segs_in: 2``,
    no data for minutes, the sign believing it was connected while the server
    reported it absent. The read loop had nothing to wake it, so the socket sat
    there forever. Closing our end makes the device re-dial.
    """

    async def scenario() -> None:
        store = DeviceStateStore()
        server, port, _ = await _serve(store=store, identify_timeout=0.3)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            # connect, then say nothing at all; the server should hang up
            assert await asyncio.wait_for(reader.read(1), timeout=3.0) == b""
            assert server.stats.unidentified_timeouts == 1
            assert server.stats.identified == 0
            writer.close()
        finally:
            await server.close()

    asyncio.run(asyncio.wait_for(scenario(), timeout=10.0))


def test_an_identified_device_may_go_quiet_without_being_closed() -> None:
    """The watchdog must not fire once a sign has introduced itself.

    A mains-powered sign holds its socket open and heartbeats once a minute, so
    a short silence after identification is normal, not a fault.
    """

    async def scenario() -> None:
        store = DeviceStateStore()
        server, port, _ = await _serve(store=store, identify_timeout=0.3)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(status_frame())
            await writer.drain()
            await asyncio.wait_for(reader.read(4096), timeout=2.0)  # the ack
            await asyncio.sleep(0.6)  # twice the identify timeout
            assert server.stats.unidentified_timeouts == 0
            assert server.connection_for(UUID) is not None
            writer.close()
        finally:
            await server.close()

    asyncio.run(asyncio.wait_for(scenario(), timeout=10.0))
