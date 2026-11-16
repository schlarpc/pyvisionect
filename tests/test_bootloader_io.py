"""Bootloader transfers over real local TCP streams; firmware is synthetic."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import replace

import pytest

from pyvisionect import bootloader as bl
from pyvisionect.io import VisionectServer
from pyvisionect.io.bootloader import run_bootloader
from pyvisionect.io.firmware import Build, RepositoryError
from test_bootloader import REAL_HELLO, REAL_OFFER, make_firmware
from test_tcp_server import status_frame

DEVICE_ID = bytes.fromhex("36002000124738303933303600000000")
BUILD = Build(0xA529A106, "a529a106", (7, 3, 4358), (1, 1, 0), 8, 3255107884)
DATA = make_firmware([b"a" * 16, b"b" * 32])
CONFIG = bl.SessionConfig(hello_timeout_s=0.2, settle_s=0.01, ack_timeout_s=0.2)


@pytest.fixture(autouse=True)
def synthetic_firmware(monkeypatch):
    monkeypatch.setattr("pyvisionect.io.firmware.KNOWN_SHA256", {})


async def close_writer(writer):
    writer.close()
    with contextlib.suppress(ConnectionError):
        await writer.wait_closed()


async def start_transfer(*, build=BUILD, device_id=DEVICE_ID, config=CONFIG,
                         on_events=None, firmware=DATA, tasks=None):
    loop = asyncio.get_running_loop()
    result = loop.create_future()

    async def handle(reader, writer, head):
        if tasks is not None:
            tasks.append(asyncio.current_task())
        try:
            session = await run_bootloader(
                reader, writer, head, firmware=firmware, build=build,
                device_id=device_id, config=config, on_events=on_events,
            )
            result.set_result(session)
        except BaseException as exc:
            result.set_exception(exc)

    server = VisionectServer(
        on_events=lambda *_: None, on_bootloader=handle,
        host="127.0.0.1", port=0,
    )
    await server.start()
    port = server._server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    return server, reader, writer, result


def test_transfer_sends_the_original_records_after_each_ack():
    async def scenario():
        events = []
        server, reader, writer, result = await start_transfer(on_events=events.extend)
        try:
            # Deliberately split the sniff and the rest of the hello.
            for part in (REAL_HELLO[:2], REAL_HELLO[2:6], REAL_HELLO[6:]):
                writer.write(part)
                await writer.drain()
                await asyncio.sleep(0)
            assert await reader.readexactly(16) == REAL_OFFER
            assert await reader.readexactly(40) == DATA[:40]
            writer.write(bl.encode_ack(1))
            await writer.drain()
            assert await reader.readexactly(56) == DATA[40:]
            writer.write(bl.encode_ack(2))
            await writer.drain()
            session = await asyncio.wait_for(result, 2)
            assert session.state is bl.State.DONE
            assert session.acked == 2
            assert [e.block for e in events if isinstance(e, bl.BlockAcked)] == [1, 2]
            assert any(isinstance(e, bl.Finished) for e in events)
        finally:
            await close_writer(writer)
            await server.close(abort=True)
    asyncio.run(asyncio.wait_for(scenario(), 5))


@pytest.mark.parametrize("failure", ["pin", "file", "callback", "cancel"])
def test_errors_and_cancellation_close_the_connection(failure, monkeypatch):
    async def scenario():
        tasks = []

        def callback(events):
            if failure == "callback":
                raise RuntimeError("callback failed")

        if failure == "pin":
            monkeypatch.setattr("pyvisionect.io.firmware.KNOWN_SHA256", {BUILD.checksum: "0" * 64})
        server, reader, writer, result = await start_transfer(
            firmware=b"bad" if failure == "file" else DATA,
            on_events=callback, tasks=tasks,
        )
        try:
            writer.write(REAL_HELLO)
            await writer.drain()
            if failure == "cancel":
                assert await reader.readexactly(16) == REAL_OFFER
                tasks[0].cancel()
            expected = {
                "pin": RepositoryError, "file": bl.BootloaderError,
                "callback": RuntimeError, "cancel": asyncio.CancelledError,
            }[failure]
            with pytest.raises(expected):
                await asyncio.wait_for(result, 2)
            assert await asyncio.wait_for(reader.read(), 2) == b""
        finally:
            await close_writer(writer)
            await server.close(abort=True)
    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_default_server_does_not_offer_firmware():
    async def scenario():
        server = VisionectServer(on_events=lambda *_: None, host="127.0.0.1", port=0)
        await server.start()
        port = server._server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(REAL_HELLO)
            await writer.drain()
            assert await asyncio.wait_for(reader.read(), 2) == b""
            assert not server.connections
        finally:
            await close_writer(writer)
            await server.close(abort=True)
    asyncio.run(asyncio.wait_for(scenario(), 5))


@pytest.mark.parametrize("changes", [
    {"device_id": bytes(16)},
    {"build": replace(BUILD, hardware=(2, 0, 0))},
    {"build": replace(BUILD, hardware_name_id=9)},
    {"build": replace(BUILD, display_id=7)},
])
def test_wrong_device_or_hardware_gets_no_offer(changes):
    async def scenario():
        server, reader, writer, result = await start_transfer(**changes)
        try:
            writer.write(REAL_HELLO)
            await writer.drain()
            session = await asyncio.wait_for(result, 2)
            assert session.state is bl.State.FAILED
            assert not session.sent_any_record
            assert await reader.read() == b""
        finally:
            await close_writer(writer)
            await server.close(abort=True)
    asyncio.run(asyncio.wait_for(scenario(), 5))


@pytest.mark.parametrize("kind", ["bootloader", "short_hello", "timeout", "disconnect", "wrong_ack"])
def test_transfer_stops_without_retrying(kind):
    async def scenario():
        events = []
        server, reader, writer, result = await start_transfer(on_events=events.extend)
        try:
            hello = REAL_HELLO
            if kind == "bootloader":
                hello = bl.encode_hello(replace(bl.parse_hello(hello), bootloader=(7, 0, 1)))
            writer.write(hello[:6] if kind == "short_hello" else hello)
            await writer.drain()
            if kind not in ("bootloader", "short_hello"):
                assert await reader.readexactly(16) == REAL_OFFER
                assert await reader.readexactly(40) == DATA[:40]
                if kind == "disconnect":
                    await close_writer(writer)
                elif kind == "wrong_ack":
                    writer.write(bl.encode_ack(2))
                    await writer.drain()
            session = await asyncio.wait_for(result, 2)
            assert session.state is bl.State.FAILED
            assert session.acked == 0
            assert session.sent_any_record == (kind not in ("bootloader", "short_hello"))
            assert len([e for e in events if isinstance(e, bl.Failed)]) == 1
        finally:
            await close_writer(writer)
            await server.close(abort=True)
    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_bootloader_routing_preserves_normal_connections():
    async def scenario():
        seen = asyncio.Event()
        routed = []

        def normal(conn, events):
            seen.set()

        async def bootloader(reader, writer, head):
            routed.append(head + await reader.readexactly(88 - len(head)))

        server = VisionectServer(
            on_events=normal, on_bootloader=bootloader, host="127.0.0.1", port=0,
        )
        await server.start()
        port = server._server.sockets[0].getsockname()[1]
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(REAL_HELLO)
            await writer.drain()
            assert await asyncio.wait_for(reader.read(), 2) == b""
            await close_writer(writer)
            assert routed == [REAL_HELLO]
            assert not seen.is_set()
            _reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(status_frame())
            await writer.drain()
            await asyncio.wait_for(seen.wait(), 2)
            assert server.stats.identified == 1
            await close_writer(writer)
        finally:
            await server.close(abort=True)
    asyncio.run(asyncio.wait_for(scenario(), 5))
