"""1 bpp / 4 bpp bit packing.  Spec §2.3.

All four vendor helpers (``encode_eight_to_four``, ``encode_eight_to_one``,
``decode_four_to_eight``, ``decode_one_to_eight``, compiled C in
``vss/pkg/driver``) operate on a **flat, row-major, ``stride == width`` buffer
for the whole rectangle**.  The pack runs straight across row boundaries;
there is **no per-row padding**.  That is why the alignment constraint in
:mod:`pyvisionect.imaging.rects` is on ``width * height`` and not on width.

4 bpp (``encode_eight_to_four``)::

    dst[o] = (src[c+1] & 0xF0) | (src[c] >> 4);

    src:  [ P0 ][ P1 ][ P2 ][ P3 ] ...        8-bit grey
    dst:  bit  7 6 5 4   3 2 1 0
                 P1          P0               EVEN pixel in the LOW nibble

Each nibble is the *top 4 bits* of the 8-bit grey value -- truncation, not
rounding.  The inverse ``decode_four_to_eight`` expands nibble ``n`` to
``n * 17`` (``(b << 4) + (b & 0x0F)``), which is where the uniform 16-level
ramp ``0, 17, 34, ... 255`` comes from.

1 bpp (``encode_eight_to_one``)::

    dst byte:  bit7 bit6 bit5 bit4 bit3 bit2 bit1 bit0
                P0   P1   P2   P3   P4   P5   P6   P7     MSB-FIRST

Each pixel contributes only its MSB, i.e. a hard threshold at ``>= 0x80``.
This is *not* a dither; dithering has to have happened earlier.

.. warning::
   The vendor's ``cEightToOneBit`` computes ``encodedLen = (len+extra)/8``
   where ``extra = len & 7`` (``encode.go:50-52``), which **under-allocates**
   for any length that is not a multiple of 8 and makes the cgo call write past
   the slice.  In practice the rectangle sizer guarantees ``w*h % 16 == 0`` for
   1 bpp, so it never fires.  We use ``ceil(len / 8)``, as the spec recommends.
"""

from __future__ import annotations

import numpy as np

from .constants import BITS_PER_PIXEL, Encoding

__all__ = [
    "pack_4bpp",
    "unpack_4bpp",
    "pack_1bpp",
    "unpack_1bpp",
    "pack",
    "unpack",
    "payload_size",
]


def _flat(pix: np.ndarray) -> np.ndarray:
    arr = np.ascontiguousarray(pix)
    if arr.dtype != np.uint8:
        raise TypeError(f"pixel buffer must be uint8, got {arr.dtype}")
    return arr.reshape(-1)


def pack_4bpp(pix: np.ndarray) -> bytes:
    """8 bpp grey -> 4 bpp, even pixel in the low nibble.

    An odd pixel count is padded with one ``0x00`` (black) byte, matching
    ``CEightToFourBit`` (``encode.go:33-38``).
    """
    flat = _flat(pix)
    if flat.size % 2:
        flat = np.concatenate([flat, np.zeros(1, dtype=np.uint8)])
    lo = flat[0::2] >> 4
    hi = flat[1::2] >> 4
    return ((hi << 4) | lo).astype(np.uint8).tobytes()


def unpack_4bpp(data: bytes, count: int | None = None) -> np.ndarray:
    """4 bpp -> 8 bpp grey on the ``n * 17`` ramp (``decode_four_to_eight``).

    :param count: number of pixels to return; defaults to ``2 * len(data)``.
        Use it to drop the padding pixel added by :func:`pack_4bpp`.
    """
    b = np.frombuffer(data, dtype=np.uint8)
    out = np.empty(b.size * 2, dtype=np.uint8)
    out[0::2] = (b & 0x0F) * 17
    out[1::2] = (b >> 4) * 17
    return out if count is None else out[:count]


def pack_1bpp(pix: np.ndarray) -> bytes:
    """8 bpp grey -> 1 bpp, MSB-first, hard threshold at ``>= 0x80``.

    A pixel count that is not a multiple of 8 is padded with zero bytes
    (black -> bit 0), matching ``cEightToOneBit`` (``encode.go:54-55``), but
    using ``ceil(len / 8)`` output bytes rather than the vendor's buggy
    ``(len + (len & 7)) / 8``.
    """
    flat = _flat(pix)
    bits = flat >= 0x80
    rem = -flat.size % 8
    if rem:
        bits = np.concatenate([bits, np.zeros(rem, dtype=bool)])
    return np.packbits(bits, bitorder="big").tobytes()


def unpack_1bpp(data: bytes, count: int | None = None) -> np.ndarray:
    """1 bpp -> 8 bpp grey, ``1 -> 0xFF`` / ``0 -> 0x00`` (``decode_one_to_eight``)."""
    b = np.frombuffer(data, dtype=np.uint8)
    out = np.unpackbits(b, bitorder="big") * np.uint8(255)
    out = out.astype(np.uint8)
    return out if count is None else out[:count]


def pack(pix: np.ndarray, encoding: int) -> bytes:
    """Dispatch on ``proto.EncodingType`` -- ``eink-generic.go:146-151``."""
    if encoding == Encoding.ONE_BIT:
        return pack_1bpp(pix)
    if encoding == Encoding.FOUR_BIT:
        return pack_4bpp(pix)
    raise ValueError(f"bad encoding {encoding!r}; only 1 and 4 exist (spec §2.1)")


def unpack(data: bytes, encoding: int, count: int | None = None) -> np.ndarray:
    """Inverse of :func:`pack` -- ``eink-generic.go:154-186`` (``image-decode``)."""
    if encoding == Encoding.ONE_BIT:
        return unpack_1bpp(data, count)
    if encoding == Encoding.FOUR_BIT:
        return unpack_4bpp(data, count)
    raise ValueError(f"bad encoding {encoding!r}; only 1 and 4 exist (spec §2.1)")


def payload_size(width: int, height: int, encoding: int) -> int:
    """``RectangleHeader.PayloadLength`` -- spec §2.3.

    ``width * height // (8 // bpp)``, i.e. ``w*h/2`` for 4 bpp and ``w*h/8``
    for 1 bpp.  Rounded up, as :func:`pack` pads.
    """
    bpp = BITS_PER_PIXEL.get(int(encoding))
    if bpp is None:
        raise ValueError(f"bad encoding {encoding!r}; only 1 and 4 exist (spec §2.1)")
    per_byte = 8 // bpp
    return (width * height + per_byte - 1) // per_byte
