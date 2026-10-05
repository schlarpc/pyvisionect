"""The sans-io ``open -> read* -> close`` sequencer.

The behaviours encoded here were measured against firmware 7.4.4407, not
guessed: replies arrive as ``Op 6`` (``event``), one reply is at most 1024
bytes however much you ask for, and there is no seek.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from pyvisionect.devices.enums import FileMode, FileOperation
from pyvisionect.packets import DEVICE_FILE_READ_CHUNK, FilePacket
from pyvisionect.session import FileRead, FileReadState
from pyvisionect.session.events import Acked, FileEventReceived, Nacked


@dataclass
class FakeConn:
    """Records what a :class:`FileRead` asked for, hands back packet ids."""

    calls: list[tuple] = field(default_factory=list)
    next_id: int = 100

    def _id(self) -> int:
        self.next_id += 1
        return self.next_id

    def file_open(self, filename: str, mode: int = FileMode.READ) -> int:
        pid = self._id()
        self.calls.append(("open", filename, mode, pid))
        return pid

    def file_read(self, length: int) -> int:
        pid = self._id()
        self.calls.append(("read", length, pid))
        return pid

    def file_close(self) -> int:
        pid = self._id()
        self.calls.append(("close", pid))
        return pid

    def last_id(self) -> int:
        return self.calls[-1][-1]


def reply(data: bytes) -> FileEventReceived:
    """What the device actually sends: op 6, bytes in the payload."""
    return FileEventReceived(file=FilePacket(op=FileOperation.EVENT, raw=data))


def drive(seq: FileRead, conn: FakeConn, chunks: list[bytes]) -> None:
    seq.start(conn)
    seq.on_event(Acked(packet_id=conn.last_id()), conn)
    for chunk in chunks:
        if not seq.awaiting_reply:
            break
        seq.on_event(reply(chunk), conn)
    if seq.state == FileReadState.CLOSING:
        seq.on_event(Acked(packet_id=conn.last_id()), conn)


def test_a_whole_small_file() -> None:
    conn = FakeConn()
    seq = FileRead("/image0.pv2", 2048)
    drive(seq, conn, [b"a" * 1024, b"b" * 1024])
    assert seq.done
    assert seq.data == b"a" * 1024 + b"b" * 1024
    assert seq.reads == 2
    assert [c[0] for c in conn.calls] == ["open", "read", "read", "close"]
    assert conn.calls[0][1:3] == ("/image0.pv2", FileMode.READ)


def test_the_last_request_asks_only_for_what_is_left() -> None:
    conn = FakeConn()
    seq = FileRead("/image0.pv2", 1500)
    drive(seq, conn, [b"a" * 1024, b"b" * 476])
    assert seq.done and len(seq.data) == 1500
    reads = [c[1] for c in conn.calls if c[0] == "read"]
    assert reads == [1024, 476]


def test_a_chunk_larger_than_the_device_allows_is_clamped() -> None:
    """Asking for 32768 returns 1024; believing otherwise mis-counts progress."""
    seq = FileRead("/x", 10_000, chunk=32768)
    assert seq.chunk == DEVICE_FILE_READ_CHUNK


def test_an_overlong_reply_is_trimmed_to_the_declared_length() -> None:
    conn = FakeConn()
    seq = FileRead("/x", 1000)
    drive(seq, conn, [b"z" * 1024])
    assert seq.done and len(seq.data) == 1000


def test_a_refused_open_fails_with_the_device_error_code() -> None:
    conn = FakeConn()
    seq = FileRead("/@screen_2", 100)
    seq.start(conn)
    seq.on_event(Nacked(packet_id=conn.last_id(), error_code=0x00008A00), conn)
    assert seq.failed
    assert "refused the open" in (seq.error or "")
    assert "0x00008a00" in (seq.error or "")


def test_an_empty_reply_mid_file_is_a_failure_not_a_silent_short_read() -> None:
    conn = FakeConn()
    seq = FileRead("/image0.pv2", 4096)
    drive(seq, conn, [b"a" * 1024, b""])
    assert seq.failed
    assert "offset 1024" in (seq.error or "")


def test_a_listing_stops_at_the_first_short_reply() -> None:
    """``Read(".", 10240)`` answers with 129 bytes and nothing more exists."""
    conn = FakeConn()
    seq = FileRead(".", 10240, stop_on_short=True)
    drive(seq, conn, [b"/image0.pv2 0 134409\n"])
    assert seq.done
    assert seq.data == b"/image0.pv2 0 134409\n"
    assert [c[0] for c in conn.calls] == ["open", "read", "close"]


def test_a_zero_length_read_closes_straight_away() -> None:
    conn = FakeConn()
    seq = FileRead("/empty", 0)
    seq.start(conn)
    seq.on_event(Acked(packet_id=conn.last_id()), conn)
    assert seq.state == FileReadState.CLOSING
    seq.on_event(Acked(packet_id=conn.last_id()), conn)
    assert seq.done and seq.data == b"" and seq.progress == 1.0


def test_restart_goes_back_to_offset_zero_because_there_is_no_seek() -> None:
    conn = FakeConn()
    seq = FileRead("/image0.pv2", 4096)
    seq.start(conn)
    seq.on_event(Acked(packet_id=conn.last_id()), conn)
    seq.on_event(reply(b"a" * 1024), conn)
    assert seq.bytes_read == 1024
    seq.restart(conn)
    assert seq.bytes_read == 0
    assert seq.restarts == 1
    assert conn.calls[-1][0] == "open"


def test_progress_tracks_the_declared_length() -> None:
    conn = FakeConn()
    seq = FileRead("/x", 4096)
    seq.start(conn)
    seq.on_event(Acked(packet_id=conn.last_id()), conn)
    seq.on_event(reply(b"a" * 1024), conn)
    assert seq.progress == pytest.approx(0.25)


def test_events_for_other_traffic_are_ignored() -> None:
    conn = FakeConn()
    seq = FileRead("/x", 1024)
    seq.start(conn)
    assert seq.on_event(Acked(packet_id=999_999), conn) is False
    assert seq.on_event(Nacked(packet_id=999_999), conn) is False
    assert seq.state == FileReadState.OPENING


def test_a_file_reply_before_the_open_is_acked_is_not_ours() -> None:
    conn = FakeConn()
    seq = FileRead("/x", 1024)
    seq.start(conn)
    assert seq.on_event(reply(b"stray"), conn) is False
    assert seq.bytes_read == 0


def test_a_negative_length_is_rejected() -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        FileRead("/x", -1)


def test_the_read_reply_opcode_is_event_not_read() -> None:
    """The regression this module exists for.

    The device answers with ``Op 6``; an earlier version of
    :mod:`pyvisionect.packets.file` decoded nothing for it, so every reply
    parsed to ``None`` and consumers saw an empty filesystem.
    """
    from pyvisionect.packets import FileBytes

    packet = FilePacket(op=FileOperation.EVENT, raw=b"/image0.pv2 0 134409\n")
    parsed = packet.parsed()
    assert isinstance(parsed, FileBytes)
    assert parsed.data == b"/image0.pv2 0 134409\n"
