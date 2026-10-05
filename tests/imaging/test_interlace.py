"""Interlacing: the 4-pixel granularity and the screen-1 lane swap.  Spec §1.6."""

from __future__ import annotations

import numpy as np
import pytest

from pyvisionect.imaging import (INTERLACE_PAIRS, Encoding, deinterlace,
                                 deinterlace_1bpp, deinterlace_4bpp,
                                 interlace, interlace_1bpp, interlace_4bpp,
                                 unpack_1bpp, unpack_4bpp)
from pyvisionect.imaging.interlace import interlace_pairs


@pytest.mark.parametrize("encoding", [Encoding.ONE_BIT, Encoding.FOUR_BIT])
def test_round_trip(encoding):
    rng = np.random.default_rng(7)
    a = rng.integers(0, 256, 720, dtype=np.uint8).tobytes()
    b = rng.integers(0, 256, 720, dtype=np.uint8).tobytes()
    out = interlace(a, b, encoding)
    assert len(out) == len(a) + len(b)
    assert deinterlace(out, encoding) == (a, b)


@pytest.mark.parametrize("encoding,unpacker", [
    (Encoding.ONE_BIT, unpack_1bpp),
    (Encoding.FOUR_BIT, unpack_4bpp),
])
def test_both_encodings_alternate_every_four_pixels(encoding, unpacker):
    """1 bpp (nibbles) and 4 bpp (byte pairs) produce the same pixel order.

    That 4-pixel granularity is exactly the "16-bit quantum" the rectangle
    sizer enforces (spec §2.4).
    """
    rng = np.random.default_rng(11)
    a = rng.integers(0, 256, 32, dtype=np.uint8).tobytes()
    b = rng.integers(0, 256, 32, dtype=np.uint8).tobytes()
    pa, pb = unpacker(a), unpacker(b)
    got = unpacker(interlace(a, b, encoding))
    want = np.concatenate([np.concatenate([pa[i:i + 4], pb[i:i + 4]])
                           for i in range(0, pa.size, 4)])
    assert np.array_equal(got, want)


def test_4bpp_interleaver_matches_the_go_loop():
    a = bytes([1, 2, 3, 4])
    b = bytes([0x11, 0x12, 0x13, 0x14])
    assert interlace_4bpp(a, b) == bytes([1, 2, 0x11, 0x12, 3, 4, 0x13, 0x14])


def test_1bpp_interleaver_matches_the_go_loop():
    a, b = bytes([0xAB]), bytes([0xCD])
    # out[0] = (0xAB & 0xF0) | (0xCD >> 4) = 0xAC
    # out[1] = ((0xAB << 4) & 0xFF) | (0xCD & 0x0F) = 0xBD
    assert interlace_1bpp(a, b) == bytes([0xAC, 0xBD])
    assert deinterlace_1bpp(bytes([0xAC, 0xBD])) == (a, b)


def test_mode2_pairing_table():
    """Screen 0 takes bands 0,1; screen 1 takes bands **3,2** (swapped)."""
    assert interlace_pairs(2) == ((0, (0, 1)), (1, (3, 2)))
    assert INTERLACE_PAIRS[2] == ((0, (0, 1)), (1, (3, 2)))


def test_mode1_refuses_to_guess():
    with pytest.raises(NotImplementedError, match="mode 1"):
        interlace_pairs(1)


def test_unknown_mode_raises():
    with pytest.raises(ValueError, match="unknown interlacing mode"):
        interlace_pairs(5)


def test_mismatched_halves_raise():
    with pytest.raises(ValueError, match="differ in size"):
        interlace_4bpp(b"\x00" * 4, b"\x00" * 6)


def test_bad_lengths_raise():
    with pytest.raises(ValueError, match="divisible by 4"):
        deinterlace_4bpp(b"\x00" * 6)
    with pytest.raises(ValueError, match="even byte count"):
        deinterlace_1bpp(b"\x00" * 3)
