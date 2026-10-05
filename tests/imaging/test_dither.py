"""Dithering: output levels, canvas-coordinate tiling, spatial stability.

Spec §3.  Read the honesty note at the top of
:mod:`pyvisionect.imaging.dither` before treating any of this as
GraphicsMagick-equivalent: only the ``none`` path is verified against real
device bytes (``test_golden_capture.py``).
"""

from __future__ import annotations

import warnings

import numpy as np
import pytest

from pyvisionect.imaging import (BAYER_8, Dithering, Encoding, bayer_matrix,
                                 blue_noise_matrix, quantise, quantise_none,
                                 quantise_ordered)
from pyvisionect.imaging.dither import is_pixel_local, tile_matrix

ALL_DITHERS = [Dithering.NONE, Dithering.BAYER, Dithering.FLOYD_STEINBERG,
               Dithering.BLUE_NOISE]
RAMP_4 = np.array([n * 17 for n in range(16)], dtype=np.uint8)


def _quantise(grey, encoding, dithering, **kw):
    """Call :func:`quantise`, silencing the deliberate bayer/4bpp warning."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        return quantise(grey, encoding, dithering, **kw)


@pytest.fixture
def photo() -> np.ndarray:
    """A smooth, non-degenerate test image (no RNG dependence on output)."""
    y, x = np.mgrid[0:96, 0:128]
    v = (128 + 110 * np.sin(x / 11.0) * np.cos(y / 17.0)
         + 0.4 * x - 0.2 * y)
    return np.clip(v, 0, 255).astype(np.uint8)


# ---------------------------------------------------------------- levels ----

@pytest.mark.parametrize("dithering", ALL_DITHERS)
def test_4bpp_output_lands_on_the_n_times_17_ramp(photo, dithering):
    q = _quantise(photo, Encoding.FOUR_BIT, dithering)
    assert set(np.unique(q)) <= set(RAMP_4.tolist())


@pytest.mark.parametrize("dithering", ALL_DITHERS)
def test_1bpp_output_is_only_0_and_255(photo, dithering):
    q = _quantise(photo, Encoding.ONE_BIT, dithering)
    assert set(np.unique(q)) <= {0, 255}


@pytest.mark.parametrize("dithering", ALL_DITHERS)
@pytest.mark.parametrize("encoding", [Encoding.ONE_BIT, Encoding.FOUR_BIT])
def test_quantising_is_idempotent(photo, dithering, encoding):
    """Re-dithering already-quantised content must not move it."""
    q = _quantise(photo, encoding, dithering)
    assert np.array_equal(_quantise(q, encoding, dithering), q)


def test_none_is_exactly_top_nibble_truncation():
    src = np.array([[0x00, 0x0F, 0x10, 0x1F, 0xF0, 0xFF]], dtype=np.uint8)
    assert np.array_equal(quantise_none(src, Encoding.FOUR_BIT),
                          np.array([[0, 0, 17, 17, 255, 255]], dtype=np.uint8))


def test_none_is_lossless_on_pre_dithered_content():
    """The property the live session depends on.

    The user's renderer already quantises to the ``n*17`` ramp and runs
    ``DefaultDithering: "none"``, so ``none`` must be a bit-for-bit identity on
    that content -- ``(n*17) >> 4 == n`` for every ``n`` in 0..15.
    """
    rng = np.random.default_rng(3)
    pre = RAMP_4[rng.integers(0, 16, (64, 64))].astype(np.uint8)
    assert np.array_equal(quantise_none(pre, Encoding.FOUR_BIT), pre)


def test_none_at_1bpp_is_the_msb():
    src = np.array([[0x7F, 0x80]], dtype=np.uint8)
    assert np.array_equal(quantise_none(src, Encoding.ONE_BIT),
                          np.array([[0, 255]], dtype=np.uint8))


# ----------------------------------------------------------------- bayer ----

def test_bayer_is_bilevel_and_says_so(photo):
    """GM's ``OrderedDitherImage`` takes no level parameter -- spec §3.2."""
    with pytest.warns(UserWarning, match="bi-level"):
        q = quantise(photo, Encoding.FOUR_BIT, Dithering.BAYER)
    assert set(np.unique(q)) == {0, 255}


def test_bayer_at_1bpp_does_not_warn(photo):
    with warnings.catch_warnings():
        warnings.simplefilter("error", UserWarning)
        quantise(photo, Encoding.ONE_BIT, Dithering.BAYER)


def test_bayer_can_be_forced_multilevel(photo):
    q = quantise(photo, Encoding.FOUR_BIT, Dithering.BAYER, levels=16)
    assert len(np.unique(q)) > 2
    assert set(np.unique(q)) <= set(RAMP_4.tolist())


def test_bayer_matrix_recursion():
    assert np.array_equal(bayer_matrix(2),
                          np.floor((np.array([[0, 2], [3, 1]]) + 0.5) / 4 * 256))
    assert BAYER_8.shape == (8, 8)
    assert sorted(BAYER_8.ravel().tolist()) == sorted(
        np.floor((np.arange(64) + 0.5) / 64 * 256).astype(int).tolist())


# ------------------------------------------------------------- blue noise ----

def test_blue_noise_matrix_is_a_committed_permutation():
    m = blue_noise_matrix()
    assert m.shape == (64, 64) and m.dtype == np.uint8
    assert m.mean() == pytest.approx(127.5, abs=0.01)
    # 4096 ranks squeezed into 256 threshold buckets -> 16 pixels per bucket
    counts = np.bincount(m.ravel(), minlength=256)
    assert counts.min() == counts.max() == 16


def test_blue_noise_matrix_is_actually_blue():
    """Low-frequency energy must be strongly suppressed versus white noise."""
    m = blue_noise_matrix()
    b = (m >= 128).astype(np.float64)
    b -= b.mean()
    power = np.abs(np.fft.fftshift(np.fft.fft2(b))) ** 2
    n = m.shape[0]
    yy, xx = np.mgrid[0:n, 0:n]
    r = np.hypot(yy - n // 2, xx - n // 2)
    low = power[r < 5].mean()
    high = power[(r >= 20) & (r < 32)].mean()
    assert high > 20 * low, f"low={low:.1f} high={high:.1f} is not blue noise"


def test_blue_noise_is_deterministic_across_calls():
    assert np.array_equal(blue_noise_matrix(), blue_noise_matrix())


def test_caller_supplied_matrix_is_used(photo):
    """So the user can drop in the exact matrix their renderer uses."""
    custom = bayer_matrix(4)
    a = quantise(photo, Encoding.FOUR_BIT, Dithering.BLUE_NOISE,
                 blue_noise=custom)
    assert not np.array_equal(
        a, quantise(photo, Encoding.FOUR_BIT, Dithering.BLUE_NOISE))
    # the mode is only a matrix selector, so the same matrix gives the same
    # pixels whichever ordered mode asks for it
    assert np.array_equal(
        a, quantise(photo, Encoding.FOUR_BIT, Dithering.BAYER,
                    bayer=custom, levels=16))
    assert np.array_equal(
        a, quantise(photo, Encoding.FOUR_BIT, Dithering.BLUE_NOISE,
                    blue_noise=custom))


# ------------------------------------------------------- canvas tiling ----

@pytest.mark.parametrize("dithering", [Dithering.BAYER, Dithering.BLUE_NOISE])
def test_ordered_dither_is_phased_on_canvas_coordinates(photo, dithering):
    """A sub-rectangle dithered with its canvas origin == the whole-canvas crop.

    This is what stops an ordered pattern from seaming at rectangle
    boundaries.  Encoding the same region as part of a big rectangle or as a
    small one must give identical pixels.
    """
    whole = _quantise(photo, Encoding.FOUR_BIT, dithering, origin=(0, 0))
    y0, x0, h, w = 13, 29, 40, 51
    piece = _quantise(photo[y0:y0 + h, x0:x0 + w], Encoding.FOUR_BIT,
                      dithering, origin=(y0, x0))
    assert np.array_equal(piece, whole[y0:y0 + h, x0:x0 + w])


@pytest.mark.parametrize("dithering", [Dithering.BAYER, Dithering.BLUE_NOISE])
def test_two_adjacent_rects_match_one_combined_rect(photo, dithering):
    """The single easiest thing to get wrong -- asserted directly."""
    split = 37
    left = _quantise(photo[:, :split], Encoding.FOUR_BIT, dithering,
                     origin=(0, 0))
    right = _quantise(photo[:, split:], Encoding.FOUR_BIT, dithering,
                      origin=(0, split))
    combined = _quantise(photo, Encoding.FOUR_BIT, dithering, origin=(0, 0))
    assert np.array_equal(np.hstack([left, right]), combined)


def test_tile_matrix_wraps():
    m = np.arange(4, dtype=np.uint8).reshape(2, 2)
    t = tile_matrix(m, (3, 3), origin=(1, 1))
    assert np.array_equal(t, np.array([[3, 2, 3], [1, 0, 1], [3, 2, 3]]))


def test_pixel_locality_classification():
    assert is_pixel_local(Dithering.NONE)
    assert is_pixel_local(Dithering.BAYER)
    assert is_pixel_local(Dithering.BLUE_NOISE)
    assert not is_pixel_local(Dithering.FLOYD_STEINBERG)


# ---------------------------------------------------- spatial stability ----

def _dirty_bbox_area(a: np.ndarray, b: np.ndarray) -> int:
    mask = a != b
    if not mask.any():
        return 0
    ys, xs = np.nonzero(mask)
    return int((ys.max() - ys.min() + 1) * (xs.max() - xs.min() + 1))


def test_spatial_stability_ranking():
    """Quantify why blue noise is the right default for an e-ink sign.

    Make one small local change, re-dither, and measure the **bounding box of
    the dirty pixels** -- which is what change detection turns into
    rectangles.  An error-diffusing dither propagates the edit to the end of
    the image, which inflates the rectangle list, defeats the <=15-region
    merge (spec §4.1) and makes the panel flash more.  Ordered dithers confine
    it to the edit.

    Measured on the 256x256 synthetic image below with a 16x16 edit
    (256 px, bbox 256):

    ====================  =========  ========
    mode                  dirty px   bbox
    ====================  =========  ========
    none                  256        256
    bayer                 126        256
    blue-noise            256        256
    **floyd-steinberg**   **1077**   **20592**
    ====================  =========  ========
    """
    y, x = np.mgrid[0:256, 0:256]
    base = np.clip(128 + 110 * np.sin(x / 11.0) * np.cos(y / 17.0)
                   + 0.4 * x - 0.2 * y, 0, 255).astype(np.uint8)
    changed = base.copy()
    changed[100:116, 120:136] = 200
    edit_area = 16 * 16

    bbox: dict[int, int] = {}
    pixels: dict[int, int] = {}
    for d in ALL_DITHERS:
        a = _quantise(base, Encoding.FOUR_BIT, d)
        b = _quantise(changed, Encoding.FOUR_BIT, d)
        bbox[int(d)] = _dirty_bbox_area(a, b)
        pixels[int(d)] = int(np.count_nonzero(a != b))

    for d in (Dithering.NONE, Dithering.BAYER, Dithering.BLUE_NOISE):
        assert bbox[int(d)] == edit_area, (
            f"{Dithering(d).name} dirtied a {bbox[int(d)]}-px bounding box for "
            f"a {edit_area}-px edit; an ordered dither must be local"
        )
        assert pixels[int(d)] <= edit_area

    fs = int(Dithering.FLOYD_STEINBERG)
    assert bbox[fs] > 20 * edit_area, (
        f"expected error diffusion to smear the edit; bbox was {bbox[fs]}"
    )
    assert pixels[fs] > 2 * edit_area


def test_floyd_steinberg_smear_inflates_the_rectangle_list():
    """The downstream cost of the smear, measured through change detection."""
    from pyvisionect.imaging import detect_changes

    y, x = np.mgrid[0:256, 0:256]
    base = np.clip(128 + 110 * np.sin(x / 11.0) * np.cos(y / 17.0), 0,
                   255).astype(np.uint8)
    changed = base.copy()
    changed[100:116, 120:136] = 200

    areas = {}
    for d in (Dithering.BLUE_NOISE, Dithering.FLOYD_STEINBERG):
        a = _quantise(base, Encoding.FOUR_BIT, d)
        b = _quantise(changed, Encoding.FOUR_BIT, d)
        areas[int(d)] = sum(r.area for r in detect_changes(b, a))

    assert areas[int(Dithering.BLUE_NOISE)] * 10 < \
        areas[int(Dithering.FLOYD_STEINBERG)], areas


def test_blue_noise_has_no_bayer_crosshatch(photo):
    """Bayer's regular grid shows up as spectral spikes; blue noise must not.

    Dither a flat mid-grey and compare the peak off-DC spectral magnitude.
    """
    flat = np.full((64, 64), 128, dtype=np.uint8)
    peaks = {}
    for d in (Dithering.BAYER, Dithering.BLUE_NOISE):
        q = _quantise(flat, Encoding.ONE_BIT, d).astype(np.float64)
        q -= q.mean()
        p = np.abs(np.fft.fft2(q))
        p[0, 0] = 0.0
        peaks[int(d)] = p.max()
    assert peaks[int(Dithering.BLUE_NOISE)] < peaks[int(Dithering.BAYER)], (
        f"blue-noise peak {peaks[int(Dithering.BLUE_NOISE)]:.0f} should be "
        f"below bayer's {peaks[int(Dithering.BAYER)]:.0f}"
    )


# -------------------------------------------------------------- errors ----

def test_dithering_zero_is_rejected(photo):
    with pytest.raises(ValueError, match="DitheringType 0"):
        quantise(photo, Encoding.FOUR_BIT, Dithering.DEFAULT)


def test_unknown_dithering_is_rejected(photo):
    with pytest.raises(ValueError, match="unknown DitheringType"):
        quantise(photo, Encoding.FOUR_BIT, 9)


def test_too_many_levels_for_the_encoding(photo):
    with pytest.raises(ValueError, match="at most 2 levels"):
        quantise_ordered(photo, Encoding.ONE_BIT, BAYER_8, levels=16)


def test_non_uniform_level_count_is_rejected(photo):
    with pytest.raises(ValueError, match="do not divide"):
        quantise_ordered(photo, Encoding.FOUR_BIT, BAYER_8, levels=5)
