"""Reading a ``.pv2`` back off the device's flash.

``tests/data/image0_pv2_head.bin`` is the **real first 4844 bytes** of
``/image0.pv2`` as the 31.2" sign served them on 2026-10-05: the 44-byte
``ProtocolHeader`` + block-0 record, the whole of block 0's LZ4 payload, and a
little slack.  Enough to pin the container layout against device bytes without
carrying 134 KB of demo artwork in the repo.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pyvisionect.packets import (
    DEVICE_IMAGE_FILES,
    STORED_FRAME_HEADER_SIZE,
    ImagePacket,
    Rectangle,
    parse_stored_frame,
    parse_stored_frame_header,
)
from pyvisionect.wire.blocks import encode_blocks
from pyvisionect.wire.errors import PayloadError
from pyvisionect.wire.framing import DataHeader

HEAD = (Path(__file__).parent / "data" / "image0_pv2_head.bin").read_bytes()


def test_the_real_header_reads_as_the_device_wrote_it() -> None:
    header = parse_stored_frame_header(HEAD)
    assert header.version == 2, "the device writes version 2, the wire uses 3"
    assert header.compression == 1
    assert header.length == 134389
    assert header.total_size == 134409, "matches the directory listing exactly"
    assert header.nblocks == 385
    assert header.firstblocklen == 103
    assert header.blocksize == 4800
    assert header.stored == 0


def test_the_header_is_44_bytes_and_nothing_more_is_needed_for_the_shape() -> None:
    assert STORED_FRAME_HEADER_SIZE == 44
    assert parse_stored_frame_header(HEAD[:44]) == parse_stored_frame_header(HEAD)


def test_block_zero_fits_in_the_prefix_we_read() -> None:
    header = parse_stored_frame_header(HEAD)
    assert header.first_block_end <= len(HEAD)


def test_a_truncated_header_says_so() -> None:
    with pytest.raises(PayloadError, match="44 bytes"):
        parse_stored_frame_header(HEAD[:40])


def test_a_foreign_version_is_refused_rather_than_misread() -> None:
    bad = bytearray(HEAD[:44])
    bad[0] = 7
    with pytest.raises(PayloadError, match="Version is 7"):
        parse_stored_frame_header(bytes(bad))


def test_a_short_file_names_the_real_remedy() -> None:
    """There is no seek, so "read more from where you were" is not an option."""
    with pytest.raises(PayloadError, match="no seek opcode"):
        parse_stored_frame(HEAD)


def _synthetic_pv2(checksum: int, device_id: bytes, version: int = 2) -> bytes:
    """Build a stored frame the way the device's own files are shaped."""
    rects = [
        Rectangle(x=0, y=0, width=8, height=2, screen_id=sid, data=bytes(8))
        for sid in (0, 1)
    ]
    payload = ImagePacket(checksum=checksum, rectangles=rects).encode()
    plain = DataHeader(
        device_id=device_id, type=5, id=1, length=len(payload)
    ).pack() + payload
    body = encode_blocks(plain)
    import struct

    return struct.pack("<5I", version, 0, 1, len(body), 0) + body


def test_a_whole_stored_frame_round_trips() -> None:
    raw = _synthetic_pv2(checksum=0, device_id=bytes(16))
    frame = parse_stored_frame(raw)
    assert frame.packet_type == 5
    assert frame.checksum == 0
    assert len(frame.image.rectangles) == 2
    assert [r.screen_id for r in frame.image.rectangles] == [0, 1]


def test_the_shipped_demo_shape_is_recognised_as_not_ours() -> None:
    """Zero checksum plus a zero UUID is the factory's signature.

    Every frame we push carries a non-zero ``ImageHeader.Checksum`` (the
    vendor remaps a computed 0 to 1) and the device's real UUID.
    """
    factory = parse_stored_frame(_synthetic_pv2(checksum=0, device_id=bytes(16)))
    assert factory.looks_like_our_push is False

    ours = parse_stored_frame(
        _synthetic_pv2(checksum=3741864387, device_id=bytes(range(16)))
    )
    assert ours.looks_like_our_push is True


def test_a_version_3_wire_frame_is_also_accepted() -> None:
    assert parse_stored_frame(_synthetic_pv2(0, bytes(16), version=3)).packet_type == 5


def test_the_six_device_files_are_named_as_the_listing_spells_them() -> None:
    assert DEVICE_IMAGE_FILES == tuple(f"/image{i}.pv2" for i in range(6))
