"""The LZ4 block chain."""

from __future__ import annotations

import random
import struct

import pytest

from pyvisionect.wire import (
    BLOCK_HEADER_SIZE,
    BLOCK_PLAINTEXT_SIZE,
    BlockError,
    BlockHeader,
    decode_blocks,
    encode_blocks,
    iter_block_headers,
)


def test_constants() -> None:
    assert BLOCK_HEADER_SIZE == 24
    assert BLOCK_PLAINTEXT_SIZE == 0x12C0 == 4800


def test_header_round_trip() -> None:
    header = BlockHeader(7, 384, 4607, 4800, 0, 0)
    assert len(header.pack()) == 24
    assert BlockHeader.unpack_from(header.pack()) == header
    assert header.block_count == 385
    assert not header.is_stored


def test_captured_stored_ack_block() -> None:
    """The captured 44-byte ack: PayloadLength == UncompressedLength == 44, Stored=1."""
    header = BlockHeader.unpack_from(struct.pack("<6I", 0, 0, 44, 44, 1, 0))
    assert header.is_stored
    assert header.payload_length == header.uncompressed_length == 44
    assert header.block_count == 1


def test_stored_polarity_one_means_verbatim() -> None:
    """Stored == 1 is NOT compressed. Getting this backwards is a classic."""
    rng = random.Random(0)
    plain = bytes(rng.randrange(256) for _ in range(4800))
    body = encode_blocks(plain)
    header = BlockHeader.unpack_from(body)
    # Random bytes cannot be shrunk, so the encoder must store them.
    assert header.stored == 1
    assert body[24:] == plain


def test_compressible_block_uses_lz4() -> None:
    plain = b"\x00" * 4800
    body = encode_blocks(plain)
    header = BlockHeader.unpack_from(body)
    assert header.stored == 0
    assert header.payload_length < header.uncompressed_length
    assert decode_blocks(body) == plain


def test_block_only_compressed_when_strictly_smaller() -> None:
    """encoder.go:374 JBE -> stored when lz4_size <= UncompressedLength."""
    def noop(_: bytes) -> bytes:
        return b"\x00" * 4800  # same size as the chunk -> must be stored

    body = encode_blocks(b"\x01" * 4800, compressor=noop)
    assert BlockHeader.unpack_from(body).stored == 1


def test_multi_block_chain_structure() -> None:
    rng = random.Random(7)
    plain = bytes(rng.randrange(256) for _ in range(4800 * 3 + 101))
    body = encode_blocks(plain)
    headers = [h for _o, h in iter_block_headers(body)]
    assert len(headers) == 4
    assert [h.index for h in headers] == [0, 1, 2, 3]
    assert {h.blocks_minus_one for h in headers} == {3}
    assert [h.uncompressed_length for h in headers] == [4800, 4800, 4800, 101]
    assert all(h.reserved == 0 for h in headers)
    assert decode_blocks(body, strict=True) == plain


def test_round_trip_property() -> None:
    rng = random.Random(31337)
    for _ in range(60):
        size = rng.randrange(0, 4800 * 3)
        if rng.random() < 0.5:
            plain = bytes(rng.randrange(256) for _ in range(size))
        else:  # compressible
            plain = (b"visionect" * (size // 9 + 1))[:size]
        body = encode_blocks(plain)
        assert decode_blocks(body) == plain


def test_raw_lz4_block_not_frame_format() -> None:
    lz4_block = pytest.importorskip(
        "lz4.block", reason="cross-checks our block against the C binding"
    )
    """The payload must be a raw block: no frame magic, no size prefix."""
    plain = b"abcd" * 1200
    body = encode_blocks(plain)
    header = BlockHeader.unpack_from(body)
    payload = body[24 : 24 + header.payload_length]
    assert not payload.startswith(b"\x04\x22\x4d\x18")  # LZ4 frame magic
    assert lz4_block.decompress(payload, uncompressed_size=4800) == plain


def test_short_header_rejected() -> None:
    with pytest.raises(BlockError, match="block header"):
        BlockHeader.unpack_from(b"\0" * 23)


def test_payload_running_past_the_body_rejected() -> None:
    body = struct.pack("<6I", 0, 0, 9999, 9999, 1, 0) + b"short"
    with pytest.raises(BlockError, match="past the body"):
        decode_blocks(body)


def test_strict_rejects_out_of_order_index() -> None:
    body = struct.pack("<6I", 5, 1, 1, 1, 1, 0) + b"a"
    body += struct.pack("<6I", 6, 1, 1, 1, 1, 0) + b"b"
    assert decode_blocks(body) == b"ab"  # lenient, like the gateway
    with pytest.raises(BlockError, match="out of order"):
        decode_blocks(body, strict=True)


def test_strict_rejects_nonzero_reserved() -> None:
    body = struct.pack("<6I", 0, 0, 1, 1, 1, 0xDEAD) + b"a"
    with pytest.raises(BlockError, match="Reserved"):
        decode_blocks(body, strict=True)


def test_blocks_too_large_rejected() -> None:
    body = struct.pack("<6I", 0, 100_000, 1, 1, 1, 0) + b"a"
    with pytest.raises(BlockError, match="too large"):
        decode_blocks(body)


def test_bad_lz4_payload_reported_clearly() -> None:
    body = struct.pack("<6I", 0, 0, 4, 4800, 0, 0) + b"\xff\xff\xff\xff"
    with pytest.raises(BlockError):
        decode_blocks(body)


# --- LZ4 backend ----------------------------------------------------------
# cramjam and python-lz4 produce the same wire bytes but differ in Python-side
# convention: cramjam prepends a 4-byte LE uncompressed size that is NOT part
# of a raw block. Mixing that up ships malformed blocks that look fine locally,
# so it is pinned here in both directions.


def test_the_codec_backend_resolves_and_names_itself() -> None:
    from pyvisionect.wire.blocks import codec_name, have_lz4

    assert have_lz4() is True
    assert codec_name() in {"cramjam", "lz4"}


@pytest.mark.parametrize("size", [1, 15, 16, 4800, 65536])
def test_a_block_round_trips_through_whichever_backend_is_installed(size: int) -> None:
    from pyvisionect.wire.blocks import lz4_compress_block, lz4_decompress_block

    plain = (b"visionect " * ((size // 10) + 1))[:size]
    assert lz4_decompress_block(lz4_compress_block(plain), size) == plain


def test_cramjam_and_python_lz4_agree_on_the_wire_bytes() -> None:
    """Either backend must read the other's output, or we cannot change default."""
    cramjam = pytest.importorskip("cramjam")
    lz4_block = pytest.importorskip("lz4.block")

    plain = (b"visionect frame payload " * 200) + bytes(range(256))
    prefixed = bytes(cramjam.lz4.compress_block(plain))
    # the prefix really is the uncompressed length, little-endian
    assert prefixed[:4] == len(plain).to_bytes(4, "little")
    raw_from_cramjam = prefixed[4:]
    raw_from_lz4 = lz4_block.compress(plain, store_size=False)

    assert lz4_block.decompress(raw_from_cramjam, uncompressed_size=len(plain)) == plain
    assert bytes(cramjam.lz4.decompress_block(raw_from_lz4, output_len=len(plain))) == plain


def test_a_truncated_block_is_a_BlockError_not_a_backend_error() -> None:
    from pyvisionect.wire.blocks import BlockError, lz4_compress_block, lz4_decompress_block

    payload = lz4_compress_block(b"x" * 4800)
    with pytest.raises(BlockError):
        lz4_decompress_block(payload[: len(payload) // 2], 4800)
