"""XXHash32 -- the image *state* tag.  Spec §6.1.

``render.(*DisplaysEncoder).calculateStateImageChecksum`` (``image-state.go:1577``)
is::

    dg.checksum = xxhash.Checksum32S(stateImg.Pix, 0)   // :1594
    if dg.checksum == 0 { dg.checksum = 1 }             // :1596

i.e. **XXHash32, seed 0, over the raw 8-bit ``Pix`` bytes of the server's
state image**, with the result ``0`` remapped to ``1`` so that ``0`` can mean
"unknown".  This is the value the device echoes back as ``DisplayStateCRC`` /
``ImageChecksum``.

Two things it is **not**:

* it is not a CRC (transport-level checksums are CRC-32/IEEE -- spec §6.4);
* it is not computed over the *encoded* data, but over the un-encoded 8-bit
  model of what the panel shows.

Getting it wrong makes the device and server disagree forever: the real server
resends a full screen whenever the echoed value differs from its own
(``checkAndPrepareIfFullScreenNeededUnlocked``, ``image-state.go:1395-1405``).

A pure-Python XXH32 is included so that ``imaging/`` needs nothing but numpy
and Pillow.  If the ``xxhash`` C extension is importable it is used instead,
which is ~200x faster on a full 3.7 MB canvas; both paths are checked against
each other in the test suite.
"""

from __future__ import annotations

import struct
from typing import Callable

__all__ = ["xxh32", "state_checksum", "HAVE_NATIVE_XXHASH"]

_MASK = 0xFFFFFFFF
_P1 = 2654435761
_P2 = 2246822519
_P3 = 3266489917
_P4 = 668265263
_P5 = 374761393


def _rotl(x: int, r: int) -> int:
    return ((x << r) | (x >> (32 - r))) & _MASK


def _xxh32_python(data: bytes, seed: int = 0) -> int:
    """Reference XXH32 (``github.com/OneOfOne/xxhash.Checksum32S`` equivalent)."""
    view = memoryview(data)
    n = len(view)
    pos = 0
    if n >= 16:
        v1 = (seed + _P1 + _P2) & _MASK
        v2 = (seed + _P2) & _MASK
        v3 = seed & _MASK
        v4 = (seed - _P1) & _MASK
        unpack = struct.unpack_from
        limit = n - 16
        while pos <= limit:
            a, b, c, d = unpack("<4I", view, pos)
            v1 = (_rotl((v1 + a * _P2) & _MASK, 13) * _P1) & _MASK
            v2 = (_rotl((v2 + b * _P2) & _MASK, 13) * _P1) & _MASK
            v3 = (_rotl((v3 + c * _P2) & _MASK, 13) * _P1) & _MASK
            v4 = (_rotl((v4 + d * _P2) & _MASK, 13) * _P1) & _MASK
            pos += 16
        h = (_rotl(v1, 1) + _rotl(v2, 7) + _rotl(v3, 12) + _rotl(v4, 18)) & _MASK
    else:
        h = (seed + _P5) & _MASK
    h = (h + n) & _MASK
    while n - pos >= 4:
        h = (h + struct.unpack_from("<I", view, pos)[0] * _P3) & _MASK
        h = (_rotl(h, 17) * _P4) & _MASK
        pos += 4
    while pos < n:
        h = (h + view[pos] * _P5) & _MASK
        h = (_rotl(h, 11) * _P1) & _MASK
        pos += 1
    h ^= h >> 15
    h = (h * _P2) & _MASK
    h ^= h >> 13
    h = (h * _P3) & _MASK
    h ^= h >> 16
    return h


try:  # pragma: no cover - depends on the environment
    import xxhash as _xxhash_mod

    def _xxh32_native(data: bytes, seed: int = 0) -> int:
        return _xxhash_mod.xxh32(data, seed=seed).intdigest()

    HAVE_NATIVE_XXHASH = True
    xxh32: Callable[..., int] = _xxh32_native
except ImportError:  # pragma: no cover
    HAVE_NATIVE_XXHASH = False
    xxh32 = _xxh32_python

#: Always available, for differential testing against :data:`xxh32`.
xxh32_python = _xxh32_python


def state_checksum(pix: bytes) -> int:
    """``ImageHeader.Checksum`` for a state image's raw ``Pix`` bytes.

    ``pix`` must be the contiguous, row-major, ``stride == width`` 8-bit
    buffer of the whole session canvas -- exactly what Go's
    ``image.NewGray(image.Rect(0, 0, w, h)).Pix`` is.

    Spec §6.1: XXHash32 seed 0, with ``0`` remapped to ``1``.
    """
    h = xxh32(pix, 0)
    return h if h != 0 else 1
