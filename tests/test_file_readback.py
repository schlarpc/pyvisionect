"""End to end: ``VisionectServer.read_device_file`` against a fake sign.

The fake answers the way the real one does -- replies as ``Op 6`` (``event``),
never more than 1024 bytes per reply, no seek -- so these exercise the
restart-from-zero recovery that the missing seek opcode forces on us.
"""

from __future__ import annotations

import asyncio
import socket
import struct

import pytest

from pyvisionect.devices.enums import FileOperation, PacketType
from pyvisionect.io import VisionectServer
from pyvisionect.io.tcp import FileReadError
from pyvisionect.packets import (
    DEVICE_FILE_READ_CHUNK,
    ControlPacket,
    FilePacket,
    StatusPacket,
)
from pyvisionect.wire import (Compression, DataHeader, Direction, FrameDecoder,
                              encode_frame)

UUID = bytes.fromhex("00112233445566778899aabb00000000")

STATUS_TAGS = [
    (0, 3), (1, 0), (8, 0xC2050128), (9, 0xDB963A1A), (10, 100),
    (16, 7), (17, 4), (18, 4407), (22, 8), (27, 1), (40, 3), (43, 585), (54, 1),
]

LISTING = (
    b"/image0.pv2 0 134409\n/image1.pv2 0 199047\n/image2.pv2 0 1213841\n"
    b"/image3.pv2 0 781996\n/image4.pv2 0 453496\n/image5.pv2 0 1007217\n\x00"
)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def device_frame(packet_type: int, payload: bytes, packet_id: int) -> bytes:
    return encode_frame(
        DataHeader(device_id=UUID, type=packet_type, id=packet_id,
                   length=len(payload)),
        payload,
        direction=Direction.DEVICE_TO_SERVER,
        compression=Compression.NONE,
    )


class FakeSign:
    """Answers open/read/close the way firmware 7.4.4407 does.

    Args:
        files: ``{name: contents}``.
        drop_reply_at: swallow the read whose reply would start at this
            offset, once, to force the caller to restart.
    """

    def __init__(self, files: dict[str, bytes], *, drop_reply_at: int | None = None):
        self.files = files
        self.drop_reply_at = drop_reply_at
        self.decoder = FrameDecoder(Direction.SERVER_TO_DEVICE)
        self.open_name: str | None = None
        self.cursor = 0
        self.next_id = 1000
        self.opens = 0

    def _id(self) -> int:
        self.next_id += 1
        return self.next_id

    def handle(self, data: bytes) -> bytes:
        out = bytearray()
        for frame in self.decoder.feed(data):
            if frame.type != PacketType.FILE:
                continue
            packet = FilePacket.decode(frame.payload)
            out += self._handle_file(packet, frame.id)
        return bytes(out)

    def _ack(self, packet_id: int) -> bytes:
        return device_frame(
            PacketType.CONTROL, ControlPacket.ack().encode(), packet_id
        )

    def _nack(self, packet_id: int, code: int) -> bytes:
        return device_frame(
            PacketType.CONTROL, ControlPacket.nack(code).encode(), packet_id
        )

    def _handle_file(self, packet: FilePacket, packet_id: int) -> bytes:
        if packet.op == FileOperation.OPEN:
            name = packet.parsed().filename
            if name not in self.files:
                return self._nack(packet_id, 0x00008A00)
            self.opens += 1
            self.open_name = name
            self.cursor = 0
            return self._ack(packet_id)
        if packet.op == FileOperation.CLOSE:
            self.open_name = None
            return self._ack(packet_id)
        if packet.op == FileOperation.READ:
            (asked,) = struct.unpack_from("<I", packet.raw, 0)
            # The device caps every reply at 1024 bytes, whatever was asked.
            want = min(asked, DEVICE_FILE_READ_CHUNK)
            body = self.files[self.open_name][self.cursor : self.cursor + want]
            if self.drop_reply_at is not None and self.cursor == self.drop_reply_at:
                self.drop_reply_at = None
                return self._ack(packet_id)  # acked, but no data ever arrives
            self.cursor += len(body)
            return self._ack(packet_id) + device_frame(
                PacketType.FILE,
                FilePacket(op=FileOperation.EVENT, raw=body).encode(),
                self._id(),
            )
        return self._nack(packet_id, 0)


async def _run(sign: FakeSign, body):
    async def on_events(conn, events):
        pass

    port = free_port()
    server = VisionectServer(on_events=on_events, host="127.0.0.1", port=port)
    await server.start()
    reader, writer = await asyncio.open_connection("127.0.0.1", port)

    async def pump() -> None:
        while True:
            data = await reader.read(65536)
            if not data:
                return
            reply = sign.handle(data)
            if reply:
                writer.write(reply)
                await writer.drain()

    pump_task = asyncio.create_task(pump())
    try:
        payload = StatusPacket(records=STATUS_TAGS).encode()
        writer.write(device_frame(PacketType.STATUS, payload, 1))
        await writer.drain()
        for _ in range(200):
            if server.connection_for(UUID) is not None:
                break
            await asyncio.sleep(0.01)
        assert server.connection_for(UUID) is not None
        return await body(server)
    finally:
        pump_task.cancel()
        writer.close()
        await server.close(abort=True)


def test_reading_a_file_whole() -> None:
    content = bytes(range(256)) * 12  # 3072 bytes, three replies
    sign = FakeSign({"/image0.pv2": content})
    seen: list[tuple[int, int]] = []

    async def body(server):
        return await server.read_device_file(
            UUID,
            "/image0.pv2",
            len(content),
            progress=lambda done, total: seen.append((done, total)),
        )

    got = asyncio.run(asyncio.wait_for(_run(sign, body), timeout=20.0))
    assert got == content
    assert seen[-1] == (len(content), len(content))
    assert sign.opens == 1


def test_a_listing_comes_back_parsed() -> None:
    sign = FakeSign({".": LISTING})

    async def body(server):
        return await server.list_device_files(UUID)

    listing = asyncio.run(asyncio.wait_for(_run(sign, body), timeout=20.0))
    assert set(listing) == {f"/image{i}.pv2" for i in range(6)}
    assert listing["/image0.pv2"].size == 134409
    assert listing["/image0.pv2"].checksum == "0"


def test_a_refused_open_raises_rather_than_returning_nothing() -> None:
    sign = FakeSign({"/image0.pv2": b"x" * 10})

    async def body(server):
        with pytest.raises(FileReadError, match="refused the open"):
            await server.read_device_file(UUID, "/@screen_2", 10)

    asyncio.run(asyncio.wait_for(_run(sign, body), timeout=20.0))


def test_a_lost_reply_restarts_the_whole_transfer() -> None:
    """There is no seek, so the only recovery is offset 0 again."""
    content = bytes(range(256)) * 8  # 2048 bytes
    sign = FakeSign({"/image0.pv2": content}, drop_reply_at=1024)

    async def body(server):
        return await server.read_device_file(
            UUID, "/image0.pv2", len(content), timeout=0.6, attempts=3
        )

    got = asyncio.run(asyncio.wait_for(_run(sign, body), timeout=30.0))
    assert got == content
    assert sign.opens == 2, "the second attempt re-opened from the start"


def test_giving_up_says_why_resuming_is_not_an_option() -> None:
    content = b"x" * 4096
    sign = FakeSign({"/image0.pv2": content})

    async def body(server):
        # Re-arm the drop on every attempt so no attempt can finish.
        original = sign._handle_file

        def always_drop(packet, packet_id):
            if packet.op == FileOperation.READ:
                sign.drop_reply_at = sign.cursor
            return original(packet, packet_id)

        sign._handle_file = always_drop
        with pytest.raises(FileReadError, match="no seek opcode"):
            await server.read_device_file(
                UUID, "/image0.pv2", len(content), timeout=0.4, attempts=2
            )

    asyncio.run(asyncio.wait_for(_run(sign, body), timeout=30.0))


def test_an_absent_device_is_a_key_error_not_a_hang() -> None:
    sign = FakeSign({})

    async def body(server):
        with pytest.raises(KeyError):
            await server.read_device_file(b"\x00" * 16, "/image0.pv2", 10)

    asyncio.run(asyncio.wait_for(_run(sign, body), timeout=20.0))


def test_a_listener_sees_events_and_can_unsubscribe() -> None:
    sign = FakeSign({".": LISTING})

    async def body(server):
        seen: list = []
        remove = server.add_listener(lambda conn, events: seen.extend(events))
        await server.list_device_files(UUID)
        assert seen, "the listener saw the file replies"
        remove()
        before = len(seen)
        await server.list_device_files(UUID)
        assert len(seen) == before

    asyncio.run(asyncio.wait_for(_run(sign, body), timeout=20.0))
