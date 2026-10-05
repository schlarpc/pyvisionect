"""Bit/nibble packing round trips.  Spec §2.3."""

from __future__ import annotations

import numpy as np
import pytest

from pyvisionect.imaging import (Encoding, pack, pack_1bpp, pack_4bpp,
                                 payload_size, unpack, unpack_1bpp,
                                 unpack_4bpp)

RAMP_4 = np.array([n * 17 for n in range(16)], dtype=np.uint8)


def test_4bpp_nibble_layout_matches_encode_eight_to_four():
    """``dst[o] = (src[2o+1] & 0xF0) | (src[2o] >> 4)`` -- even pixel LOW."""
    src = np.array([0x00, 0x10, 0x20, 0x30], dtype=np.uint8)
    out = pack_4bpp(src)
    assert out == bytes([(0x10 & 0xF0) | (0x00 >> 4), (0x30 & 0xF0) | (0x20 >> 4)])
    assert out == bytes([0x10, 0x32])


def test_4bpp_nibble_is_truncation_not_rounding():
    """``src >> 4``: 0x1F and 0x10 both become nibble 1."""
    assert pack_4bpp(np.array([0x10, 0x1F], dtype=np.uint8)) == bytes([0x11])


def test_decode_four_to_eight_expands_to_n_times_17():
    assert np.array_equal(unpack_4bpp(bytes([0x10, 0xF0])),
                          np.array([0, 17, 0, 255], dtype=np.uint8))


def test_1bpp_bit_layout_is_msb_first():
    """``dst`` bit 7 is pixel 0 (``encode_eight_to_one``)."""
    src = np.array([255, 0, 0, 0, 0, 0, 0, 255], dtype=np.uint8)
    assert pack_1bpp(src) == bytes([0b10000001])


def test_1bpp_threshold_is_hard_at_128():
    src = np.array([0x7F, 0x80] + [0] * 6, dtype=np.uint8)
    assert pack_1bpp(src) == bytes([0b01000000])


def test_decode_one_to_eight():
    assert np.array_equal(unpack_1bpp(bytes([0b10100000]))[:3],
                          np.array([255, 0, 255], dtype=np.uint8))


@pytest.mark.parametrize("encoding", [Encoding.ONE_BIT, Encoding.FOUR_BIT])
@pytest.mark.parametrize("w,h", [(16, 1), (4, 4), (1440, 640), (48, 3), (2880, 2)])
def test_round_trip_on_the_output_ramp(encoding, w, h):
    """pack -> unpack is lossless for pixels already on the encoding's ramp."""
    rng = np.random.default_rng(1234)
    if encoding == Encoding.FOUR_BIT:
        px = RAMP_4[rng.integers(0, 16, (h, w))].astype(np.uint8)
    else:
        px = (rng.integers(0, 2, (h, w)) * 255).astype(np.uint8)
    data = pack(px, encoding)
    assert len(data) == payload_size(w, h, encoding)
    back = unpack(data, encoding, w * h).reshape(h, w)
    assert np.array_equal(back, px)


@pytest.mark.parametrize("encoding,w,h,expected", [
    (Encoding.FOUR_BIT, 2880, 640, 921600),
    (Encoding.FOUR_BIT, 1440, 640, 460800),
    (Encoding.ONE_BIT, 2880, 640, 230400),
    (Encoding.ONE_BIT, 1440, 640, 115200),
])
def test_payload_size_matches_the_wire(encoding, w, h, expected):
    """``W*H/2`` (4 bpp) / ``W*H/8`` (1 bpp) -- spec §2.3."""
    assert payload_size(w, h, encoding) == expected


def test_packing_is_linear_across_rows_with_no_padding():
    """stride == width; an odd width must not pad a row.

    A 3x2 4 bpp rectangle is 6 pixels = 3 bytes, with row 1's first pixel
    sharing byte 1 with row 0's last pixel.
    """
    px = np.array([[0x00, 0x10, 0x20], [0x30, 0x40, 0x50]], dtype=np.uint8)
    out = pack_4bpp(px)
    assert len(out) == 3
    assert out == bytes([0x10, 0x32, 0x54])


def test_odd_pixel_count_pads_with_black():
    """``CEightToFourBit`` appends one 0x00 byte (``encode.go:37-38``)."""
    assert pack_4bpp(np.array([0xF0], dtype=np.uint8)) == bytes([0x0F])


def test_1bpp_uses_ceil_not_the_vendor_underallocation():
    """The vendor's ``(len + (len & 7)) / 8`` under-allocates; we use ceil."""
    for n in range(1, 33):
        px = np.full(n, 255, dtype=np.uint8)
        assert len(pack_1bpp(px)) == -(-n // 8), n


@pytest.mark.parametrize("encoding", [0, 2, 3, 5, 8, 16])
def test_bad_encodings_are_rejected(encoding):
    with pytest.raises(ValueError, match="only 1 and 4"):
        pack(np.zeros(16, dtype=np.uint8), encoding)
    with pytest.raises(ValueError, match="only 1 and 4"):
        unpack(b"\x00" * 2, encoding)
    with pytest.raises(ValueError, match="only 1 and 4"):
        payload_size(16, 16, encoding)
