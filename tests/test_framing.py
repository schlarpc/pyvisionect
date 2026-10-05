"""ProtocolHeader / DataHeader framing and the incremental decoder."""

from __future__ import annotations

import random
import struct

import pytest

from pyvisionect.wire import (
    DATA_HEADER_SIZE,
    MAX_FRAME_LENGTH,
    PROTOCOL_HEADER_SIZE,
    PROTOCOL_VERSION,
    ChecksumMismatch,
    Compression,
    DataHeader,
    Direction,
    FrameDecoder,
    FrameTooLarge,
    ProtocolHeader,
    ShortBuffer,
    UnsupportedCompression,
    UnsupportedSecurity,
    UnsupportedVersion,
    encode_frame,
)

UUID = bytes.fromhex("00112233445566778899aabb00000000")


def test_sizes() -> None:
    assert PROTOCOL_HEADER_SIZE == 20
    assert DATA_HEADER_SIZE == 36
    assert PROTOCOL_VERSION == 3
    assert MAX_FRAME_LENGTH == 0x3200000 == 52_428_800


def test_protocol_header_round_trip() -> None:
    header = ProtocolHeader(3, 0, 1, 68, 0xD92C258D)
    assert len(header.pack()) == 20
    assert ProtocolHeader.unpack_from(header.pack()) == header
    assert header.pack_prefix() == header.pack()[:16]


def test_data_header_round_trip() -> None:
    header = DataHeader(device_id=UUID, type=3, id=1307, length=496)
    assert len(header.pack()) == 36
    assert DataHeader.unpack_from(header.pack()) == header


def test_data_header_rejects_wrong_uuid_length() -> None:
    with pytest.raises(ValueError, match="16 bytes"):
        DataHeader(device_id=b"short", type=3, id=1, length=0).pack()


def test_short_headers_rejected() -> None:
    with pytest.raises(ShortBuffer):
        ProtocolHeader.unpack_from(b"\0" * 19)
    with pytest.raises(ShortBuffer):
        DataHeader.unpack_from(b"\0" * 35)


def _frame(payload: bytes, *, compression: int, direction: Direction) -> bytes:
    return encode_frame(
        DataHeader(device_id=UUID, type=3, id=1, length=len(payload)),
        payload,
        direction=direction,
        compression=compression,
    )


@pytest.mark.parametrize("compression", [Compression.NONE, Compression.LZ4])
@pytest.mark.parametrize("direction", list(Direction))
def test_encode_decode_round_trip(compression: int, direction: Direction) -> None:
    payload = bytes(range(256)) * 3
    raw = _frame(payload, compression=compression, direction=direction)
    frames = FrameDecoder(direction, strict=True).feed(raw)
    assert len(frames) == 1
    assert frames[0].payload == payload
    assert frames[0].header.compression == compression


def test_decoder_is_incremental_byte_by_byte() -> None:
    payload = b"status-ish" * 40
    raw = _frame(payload, compression=Compression.NONE,
                 direction=Direction.DEVICE_TO_SERVER)
    decoder = FrameDecoder(Direction.DEVICE_TO_SERVER, strict=True)
    got = []
    for i in range(len(raw)):
        got.extend(decoder.feed(raw[i : i + 1]))
    assert len(got) == 1
    assert got[0].payload == payload
    assert decoder.buffered == 0


def test_decoder_handles_several_frames_in_one_read() -> None:
    raw = b"".join(
        _frame(bytes([i]) * 16, compression=Compression.LZ4,
               direction=Direction.SERVER_TO_DEVICE)
        for i in range(5)
    )
    frames = FrameDecoder(Direction.SERVER_TO_DEVICE, strict=True).feed(raw)
    assert len(frames) == 5
    assert [f.payload[0] for f in frames] == [0, 1, 2, 3, 4]


def test_decoder_holds_a_partial_frame() -> None:
    raw = _frame(b"x" * 100, compression=Compression.NONE,
                 direction=Direction.DEVICE_TO_SERVER)
    decoder = FrameDecoder(Direction.DEVICE_TO_SERVER)
    assert decoder.feed(raw[:-1]) == []
    assert decoder.buffered == len(raw) - 1
    assert len(decoder.feed(raw[-1:])) == 1


def test_bad_version_rejected() -> None:
    raw = bytearray(_frame(b"", compression=Compression.NONE,
                           direction=Direction.DEVICE_TO_SERVER))
    struct.pack_into("<I", raw, 0, 4)
    with pytest.raises(UnsupportedVersion, match="unsupported protocol version 4"):
        FrameDecoder().feed(bytes(raw))


def test_oversized_length_rejected() -> None:
    raw = struct.pack("<5I", 3, 0, 0, MAX_FRAME_LENGTH + 1, 0)
    with pytest.raises(FrameTooLarge, match="packet too large"):
        FrameDecoder().feed(raw)


def test_unsupported_compression_rejected() -> None:
    body = b"\0" * DATA_HEADER_SIZE
    raw = struct.pack("<5I", 3, 0, 2, len(body), 0) + body
    with pytest.raises(UnsupportedCompression, match="unsupported compression 2"):
        FrameDecoder().feed(raw)


def test_security_rejected_with_an_explanation() -> None:
    body = b"\0" * DATA_HEADER_SIZE
    raw = struct.pack("<5I", 3, 2, 0, len(body), 0) + body
    with pytest.raises(UnsupportedSecurity, match="not implemented"):
        FrameDecoder().feed(raw)


def test_strict_mode_catches_a_wrong_checksum() -> None:
    raw = bytearray(_frame(b"abc", compression=Compression.NONE,
                           direction=Direction.DEVICE_TO_SERVER))
    struct.pack_into("<I", raw, 16, 0xDEADBEEF)
    # Lenient by default: the real gateway never validates this field.
    assert len(FrameDecoder(Direction.DEVICE_TO_SERVER).feed(bytes(raw))) == 1
    with pytest.raises(ChecksumMismatch):
        FrameDecoder(Direction.DEVICE_TO_SERVER, strict=True).feed(bytes(raw))


def test_wrong_direction_is_caught_in_strict_mode() -> None:
    """Encode as a server, decode as if from a device -> checksum mismatch."""
    raw = _frame(b"payload", compression=Compression.LZ4,
                 direction=Direction.SERVER_TO_DEVICE)
    with pytest.raises(ChecksumMismatch):
        FrameDecoder(Direction.DEVICE_TO_SERVER, strict=True).feed(raw)


def test_raw_body_passthrough_is_byte_exact() -> None:
    original = _frame(b"hello" * 9, compression=Compression.LZ4,
                      direction=Direction.SERVER_TO_DEVICE)
    frame = FrameDecoder(Direction.SERVER_TO_DEVICE).feed(original)[0]
    again = encode_frame(
        frame.data,
        frame.payload,
        direction=Direction.SERVER_TO_DEVICE,
        compression=frame.header.compression,
        raw_body=frame.raw_body,
    )
    assert again == original


def test_fuzz_round_trip() -> None:
    rng = random.Random(0xC0FFEE)
    for _ in range(100):
        payload = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 600)))
        compression = rng.choice([Compression.NONE, Compression.LZ4])
        direction = rng.choice(list(Direction))
        header = DataHeader(
            device_id=bytes(rng.randrange(256) for _ in range(16)),
            type=rng.choice([1, 3, 5, 8, 10, 11]),
            id=rng.getrandbits(32),
            length=len(payload),
        )
        raw = encode_frame(header, payload, direction=direction,
                           compression=compression)
        frame = FrameDecoder(direction, strict=True).feed(raw)[0]
        assert frame.payload == payload
        assert frame.data == header
