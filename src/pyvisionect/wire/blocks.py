"""The LZ4 block chain used when ``ProtocolHeader.Compression == 1``.

Layout of one block record (24-byte header, 6 x uint32 LE, then the payload).
Recovered from ``proto/v2`` ``encoder.go:51-56`` (marshal) and
``encoder.go:62-69`` (unmarshal)::

    offset size field               notes
      0     4   BlockIndex          0-based index of this block
      4     4   BlocksMinusOne      total blocks - 1; the SAME value in every header
      8     4   PayloadLength       bytes of block payload that follow
     12     4   UncompressedLength  4800 (0x12C0) except the last block
     16     4   Stored              1 = payload verbatim, 0 = payload is a raw LZ4 block
     20     4   Reserved            always 0
     24   ...   payload             PayloadLength bytes

Two things bite here:

1. **The polarity of ``Stored`` is inverted from what the name suggests on a
   first read.** ``1`` means *not* compressed.  ``encoder.go:374-380``::

       CMPL 0x34(SP) /*UncompressedLength*/, BX /*lz4 output size*/
       JBE  -> stored branch:  Stored = 1 ; PayloadLength = UncompressedLength
       fallthrough -> lz4:     Stored = 0 ; PayloadLength = lz4 size

   so LZ4 is used only when it *strictly* shrinks the block.

2. **``BlocksMinusOne`` is an index, not a count or a flag.** On the captured
   1.84 MB image push it is ``384`` in all 385 blocks, so a receiver knows the
   total block count from the first header alone.

The compressed payload is a **raw LZ4 block** -- no frame magic, no content
checksum, no size prefix.  That is ``lz4.block`` in python-lz4, with
``store_size=False``, and emphatically *not* ``lz4.frame``.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Callable, Iterable

from .errors import BlockError, MissingLz4

__all__ = [
    "BLOCK_HEADER_SIZE",
    "BLOCK_PLAINTEXT_SIZE",
    "STORED_ONLY",
    "BlockHeader",
    "decode_blocks",
    "encode_blocks",
    "codec_name",
    "have_lz4",
    "iter_block_headers",
    "lz4_compress_block",
    "lz4_decompress_block",
    "stored_only",
]

BLOCK_HEADER_SIZE = 24
BLOCK_PLAINTEXT_SIZE = 0x12C0
"""4800 bytes. ``encoder.go:353`` ``MOVL $0x12c0, 0x34(SP)``."""

MAX_TOTAL = 0x3200000
"""50 MiB. ``decoder.go:234``: ``"blocks too large: %d"``."""

_BLOCK = struct.Struct("<6I")


_BACKEND: tuple[str, object, object] | None = None


def _load_backend():
    """Resolve an LZ4 block codec, preferring ``cramjam``.

    Two implementations are accepted and they produce the *same wire bytes*;
    they differ only in Python-side convention, which this function hides:

    * ``cramjam`` -- Rust, and the **only** one that publishes musllinux
      wheels, so it is the one that installs in Home Assistant's Alpine
      container. Its ``compress_block`` prepends a 4-byte little-endian
      uncompressed size, which is *not* part of a raw block and is stripped
      here. Its ``decompress_block`` wants that length as ``output_len``.
    * ``lz4`` (python-lz4) -- the C binding, kept as a fast path when it is
      already installed. ``store_size=False`` is the raw form.

    Imported lazily so a deployment that neither compresses outbound frames
    (``ConnectionConfig(compressor=STORED_ONLY)``) nor receives a compressed
    one can run with neither installed. That is a real configuration: every
    device->server frame in the capture carries ``Compression = 0``, so a pure
    server needs a codec only for its own outbound traffic.
    """
    global _BACKEND
    if _BACKEND is not None:
        return _BACKEND
    try:
        import cramjam

        def _c(plain: bytes) -> bytes:
            # strip the 4-byte LE size cramjam prepends; the wire wants a bare block
            return bytes(cramjam.lz4.compress_block(plain))[4:]

        def _d(payload: bytes, n: int) -> bytes:
            return bytes(cramjam.lz4.decompress_block(payload, output_len=n))

        _BACKEND = ("cramjam", _c, _d)
        return _BACKEND
    except ImportError:
        pass
    try:
        import lz4.block as _m

        def _c(plain: bytes) -> bytes:
            return _m.compress(plain, store_size=False)

        def _d(payload: bytes, n: int) -> bytes:
            return _m.decompress(payload, uncompressed_size=n)

        _BACKEND = ("lz4", _c, _d)
        return _BACKEND
    except ImportError:
        pass
    raise MissingLz4(
        "this frame needs an LZ4 block codec and neither 'cramjam' nor 'lz4' "
        "is installed. Install one (pip install cramjam -- it is the one with "
        "musllinux wheels, so it works in the official Home Assistant "
        "container) or, if you only act as a server, pass "
        "ConnectionConfig(compressor=STORED_ONLY) to emit every block "
        "uncompressed -- the device never compresses, so inbound frames need "
        "no codec at all."
    )


def codec_name() -> str | None:
    """Which LZ4 backend is in use, or ``None`` if neither is installed."""
    try:
        return _load_backend()[0]
    except MissingLz4:
        return None


def have_lz4() -> bool:
    """Whether an LZ4 block codec is importable (either backend)."""
    return codec_name() is not None


@dataclass(frozen=True, slots=True)
class BlockHeader:
    """One 24-byte block record header."""

    index: int
    blocks_minus_one: int
    payload_length: int
    uncompressed_length: int
    stored: int
    reserved: int = 0

    @property
    def block_count(self) -> int:
        """Total number of blocks in the chain (``BlocksMinusOne + 1``)."""
        return self.blocks_minus_one + 1

    @property
    def is_stored(self) -> bool:
        """True when the payload is verbatim rather than an LZ4 block."""
        return self.stored == 1

    def pack(self) -> bytes:
        return _BLOCK.pack(
            self.index,
            self.blocks_minus_one,
            self.payload_length,
            self.uncompressed_length,
            self.stored,
            self.reserved,
        )

    @classmethod
    def unpack_from(cls, buf: bytes, offset: int = 0) -> "BlockHeader":
        if len(buf) - offset < BLOCK_HEADER_SIZE:
            raise BlockError(
                f"need {BLOCK_HEADER_SIZE} bytes for a block header, have {len(buf) - offset}"
            )
        return cls(*_BLOCK.unpack_from(buf, offset))


def lz4_decompress_block(payload: bytes, uncompressed_length: int) -> bytes:
    """Inflate a raw LZ4 block to exactly *uncompressed_length* bytes."""
    try:
        out = _load_backend()[2](payload, uncompressed_length)
    except MissingLz4:
        raise
    except Exception as exc:  # both backends raise their own bare errors
        raise BlockError(f"raw LZ4 block did not inflate: {exc}") from exc
    if len(out) != uncompressed_length:
        raise BlockError(
            f"LZ4 block inflated to {len(out)} bytes, header said {uncompressed_length}"
        )
    return out


def lz4_compress_block(plain: bytes) -> bytes:
    """Deflate *plain* to a raw LZ4 block (no size prefix, no frame header).

    Note: this does **not** reproduce the vendor's ``vss/lz4.Lz4Compress`` output
    byte-for-byte -- see ``README.md``.  Any valid LZ4 block is decodable by the
    device, so interoperability is unaffected; only byte-identical replay of a
    captured server frame is.
    """
    return _load_backend()[1](plain)


def stored_only(plain: bytes) -> bytes:
    """A "compressor" that never shrinks anything, so every block is ``Stored=1``.

    Returns one byte more than it was given, which makes
    :func:`encode_blocks` take the stored branch for every chunk -- the same
    branch the vendor takes whenever LZ4 would not help.  The result is a
    perfectly ordinary, spec-conformant block chain that needs no LZ4 codec on
    either side.

    Worth considering because the captured 1.84 MB push shows LZ4 buying
    essentially nothing on dithered halftone data: 60 of its 385 blocks were
    stored anyway, and the 325 compressed ones averaged a 0.956 ratio.  The
    trade is ~4% more bytes on the wire for zero C extensions.

    Note this keeps ``Compression = 1`` framing, which is what the device has
    been observed receiving.  Sending ``Compression = 0`` *to* a device is a
    different thing and is **unverified** -- the gateway never does it.
    """
    return plain + b"\x00"


STORED_ONLY = stored_only
"""Alias, for use as ``ConnectionConfig(compressor=STORED_ONLY)``."""


def decode_blocks(body: bytes, *, strict: bool = False) -> bytes:
    """Reassemble the plaintext from an LZ4 block chain.

    Args:
        body: the frame body, starting at the first block header.
        strict: also validate ``BlockIndex`` ordering, the constancy of
            ``BlocksMinusOne``, ``Reserved == 0`` and that the chain consumes the
            body exactly.  The real gateway checks none of these.

    Raises:
        BlockError: on a malformed chain.
    """
    out = bytearray()
    offset = 0
    index = 0
    expected_count: int | None = None

    while offset < len(body):
        header = BlockHeader.unpack_from(body, offset)
        if expected_count is None:
            expected_count = header.block_count
            # decoder.go:228-234
            total = expected_count * BLOCK_PLAINTEXT_SIZE
            if expected_count == 1:
                total = header.uncompressed_length
            if total > MAX_TOTAL:
                raise BlockError(f"blocks too large: {total}")
        elif strict and header.block_count != expected_count:
            raise BlockError(
                f"block {header.index}: BlocksMinusOne changed "
                f"{expected_count - 1} -> {header.blocks_minus_one}"
            )
        if strict:
            if header.index != index:
                raise BlockError(f"block index out of order: want {index}, got {header.index}")
            if header.reserved != 0:
                raise BlockError(f"block {header.index}: Reserved = {header.reserved}, want 0")
            if header.stored not in (0, 1):
                raise BlockError(f"block {header.index}: Stored = {header.stored}, want 0 or 1")

        start = offset + BLOCK_HEADER_SIZE
        end = start + header.payload_length
        if end > len(body):
            raise BlockError(
                f"block {header.index} payload runs past the body "
                f"({end} > {len(body)})"
            )
        payload = body[start:end]
        if header.is_stored:
            if strict and header.payload_length != header.uncompressed_length:
                raise BlockError(
                    f"block {header.index} is stored but PayloadLength "
                    f"{header.payload_length} != UncompressedLength {header.uncompressed_length}"
                )
            out += payload
        else:
            out += lz4_decompress_block(payload, header.uncompressed_length)

        offset = end
        index += 1

    if expected_count is not None and strict and index != expected_count:
        raise BlockError(f"chain has {index} blocks, first header said {expected_count}")
    return bytes(out)


def iter_block_headers(body: bytes) -> Iterable[tuple[int, BlockHeader]]:
    """Yield ``(offset, header)`` for each block in *body* without inflating it."""
    offset = 0
    while offset < len(body):
        header = BlockHeader.unpack_from(body, offset)
        yield offset, header
        offset += BLOCK_HEADER_SIZE + header.payload_length


def encode_blocks(
    plain: bytes,
    *,
    compressor: Callable[[bytes], bytes] = lz4_compress_block,
    block_size: int = BLOCK_PLAINTEXT_SIZE,
) -> bytes:
    """Split *plain* into ``block_size`` chunks and emit the block chain.

    Follows the vendor's per-block decision exactly: compress the chunk, and use
    the LZ4 payload only if it is **strictly** smaller than the chunk, otherwise
    mark the block ``Stored = 1`` and emit the chunk verbatim.

    Args:
        plain: the marshalled packet (``DataHeader`` + payload, or raw body).
        compressor: raw-LZ4-block compressor.  Swappable so that a caller who has
            reproduced ``vss/lz4.Lz4Compress`` can get byte-identical frames.
        block_size: plaintext chunk size. Do not change; 4800 is what the device
            expects.

    Returns:
        The frame body.
    """
    if not plain:
        # The vendor never emits a zero-length body; one empty stored block keeps
        # the chain well-formed if a caller ever asks for it.
        return BlockHeader(0, 0, 0, 0, 1).pack()

    chunks = [plain[i : i + block_size] for i in range(0, len(plain), block_size)]
    last = len(chunks) - 1
    out = bytearray()
    for i, chunk in enumerate(chunks):
        squeezed = compressor(chunk)
        if len(squeezed) < len(chunk):
            payload, stored = squeezed, 0
        else:
            payload, stored = chunk, 1
        out += BlockHeader(i, last, len(payload), len(chunk), stored).pack()
        out += payload
    return bytes(out)
