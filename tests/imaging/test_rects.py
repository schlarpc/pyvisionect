"""Rectangle alignment, merging and change detection.  Spec §2.4, §4.1, §4.2."""

from __future__ import annotations

import numpy as np
import pytest

from pyvisionect.imaging import (ALIGN_QUANTUM, MAX_REGIONS_PER_DISPLAY,
                                 Encoding, Rect, align_to_quantum,
                                 apply_region_count_limit, detect_changes,
                                 is_aligned, join_contours, limit_regions,
                                 unite_overlapping_regions)


# ----------------------------------------------------------- 16-bit quanta ----

def test_quantum_values():
    """``16 // bpp``: 16 sub-pixels for 1 bpp, 4 for 4 bpp."""
    assert ALIGN_QUANTUM[Encoding.ONE_BIT] == 16
    assert ALIGN_QUANTUM[Encoding.FOUR_BIT] == 4


def test_alignment_is_on_width_times_height_not_width():
    """The rule the design doc got wrong -- spec §2.4.

    A 3x4 rectangle has 12 sub-pixels, which divides the 4 bpp quantum, even
    though its width does not.
    """
    assert is_aligned(Rect(0, 0, 3, 4), Encoding.FOUR_BIT)
    assert not is_aligned(Rect(0, 0, 3, 3), Encoding.FOUR_BIT)
    assert is_aligned(Rect(0, 0, 4, 4), Encoding.ONE_BIT)
    assert not is_aligned(Rect(0, 0, 5, 3), Encoding.ONE_BIT)


def test_total_below_the_quantum_is_never_aligned():
    """``if q > total { return false }`` -- ``sixteen-bit-limitation.go:38``."""
    assert not is_aligned(Rect(0, 0, 1, 2), Encoding.FOUR_BIT)
    assert not is_aligned(Rect(0, 0, 2, 2), Encoding.ONE_BIT)


def test_full_1440x640_display_is_already_aligned():
    """921600 % 4 == 0 and % 16 == 0 -- no growth needed, spec §2.4."""
    r = Rect(0, 0, 1440, 640)
    for enc in (Encoding.ONE_BIT, Encoding.FOUR_BIT):
        assert is_aligned(r, enc)
        assert align_to_quantum(r, enc, Rect(0, 0, 1440, 2560)) == r


@pytest.mark.parametrize("encoding", [Encoding.ONE_BIT, Encoding.FOUR_BIT])
@pytest.mark.parametrize("rect", [
    Rect(0, 0, 1, 1), Rect(3, 7, 5, 3), Rect(100, 100, 17, 9),
    Rect(1430, 2550, 10, 10), Rect(0, 0, 1439, 1),
])
def test_alignment_always_produces_an_aligned_rect_inside_the_border(encoding, rect):
    border = Rect(0, 0, 1440, 2560)
    out = align_to_quantum(rect, encoding, border)
    assert is_aligned(out, encoding)
    assert out.x >= border.x and out.far_x <= border.far_x
    assert out.y >= border.y and out.far_y <= border.far_y
    # growth only, never shrinkage
    assert out.x <= rect.x and out.far_x >= rect.far_x
    assert out.y <= rect.y and out.far_y >= rect.far_y


def test_alignment_outside_the_border_raises():
    with pytest.raises(ValueError, match="outside boundaries"):
        align_to_quantum(Rect(2000, 0, 10, 10), 4, Rect(0, 0, 100, 100))


def test_alignment_in_an_unalignable_border_raises():
    """A 3x1 border cannot host any 4-sub-pixel rectangle."""
    with pytest.raises(ValueError, match="cannot be aligned"):
        align_to_quantum(Rect(0, 0, 3, 1), 4, Rect(0, 0, 3, 1))


# ------------------------------------------------------------- union logic ----

def test_overlap_is_strict():
    """Touching edges do not count -- ``join.go:31-48``."""
    assert not Rect(0, 0, 10, 10).overlaps(Rect(10, 0, 10, 10))
    assert Rect(0, 0, 10, 10).overlaps(Rect(9, 0, 10, 10))
    assert not Rect(0, 0, 0, 10).overlaps(Rect(0, 0, 10, 10))


def test_union_requires_overlap_bounding_box_does_not():
    a, b = Rect(0, 0, 4, 4), Rect(10, 10, 4, 4)
    assert a.union(b) is None
    assert a.bounding_box(b) == Rect(0, 0, 14, 14)


def test_unite_overlapping_regions_reaches_a_fixpoint():
    rects = [Rect(0, 0, 10, 10), Rect(5, 5, 10, 10), Rect(12, 12, 10, 10)]
    out = unite_overlapping_regions(rects, area_criteria=False)
    assert out == [Rect(0, 0, 22, 22)]


def test_area_criteria_rejects_a_bigger_union():
    """``if areaUnion > area { continue }`` -- ``join.go:155``."""
    a = Rect(0, 0, 100, 1)
    b = Rect(99, 0, 1, 100)
    assert a.overlaps(b)
    assert unite_overlapping_regions([a, b], area_criteria=True) == [a, b]
    assert unite_overlapping_regions([a, b], area_criteria=False) == \
        [Rect(0, 0, 100, 100)]


# -------------------------------------------------------- region counting ----

def test_apply_region_count_limit_is_a_noop_under_the_limit():
    rects = [Rect(i * 10, 0, 2, 2) for i in range(5)]
    assert apply_region_count_limit(15, 50.0, 10.0, rects) == rects


def test_join_contours_reaches_the_limit():
    rects = [Rect(i * 4, i * 4, 2, 2) for i in range(40)]
    out = join_contours(15, 10.0, 20.0, rects)
    assert len(out) <= 15


def test_join_contours_validates_its_parameters():
    rects = [Rect(i * 4, 0, 2, 2) for i in range(20)]
    with pytest.raises(ValueError, match="maxRegions"):
        join_contours(0, 10.0, 1.0, rects)
    with pytest.raises(ValueError, match="distThresholdStep"):
        join_contours(5, 10.0, -1.0, rects)
    with pytest.raises(ValueError, match="distThreshold"):
        join_contours(5, 0.0, 1.0, rects)


def test_limit_regions_returns_none_when_merging_is_disabled():
    rects = [Rect(i * 4, 0, 2, 2) for i in range(MAX_REGIONS_PER_DISPLAY + 1)]
    assert limit_regions(rects, 1440, 640, merge=False) is None


def test_limit_regions_merges_when_enabled():
    rects = [Rect(i * 4, 0, 2, 2) for i in range(40)]
    out = limit_regions(rects, 1440, 640, merge=True)
    assert out is not None and len(out) <= MAX_REGIONS_PER_DISPLAY


def test_merge_thresholds_are_percentages_of_the_diagonal():
    """1440x640 -> diag 1575.81 -> 78.8 px / 157.6 px -- spec §4.1."""
    import math
    diag = math.hypot(1440, 640)
    assert diag == pytest.approx(1575.81, abs=0.01)
    assert 5.0 * diag / 100.0 == pytest.approx(78.79, abs=0.01)
    assert 10.0 * diag / 100.0 == pytest.approx(157.58, abs=0.01)


# ------------------------------------------------------ change detection ----

def test_detect_changes_finds_separate_boxes():
    a = np.zeros((40, 40), dtype=np.uint8)
    b = a.copy()
    b[2:6, 3:9] = 200
    b[25:30, 30:36] = 50
    out = detect_changes(b, a)
    assert sorted((r.x, r.y, r.width, r.height) for r in out) == \
        [(3, 2, 6, 4), (30, 25, 6, 5)]


def test_detect_changes_merges_8_connected_pixels():
    a = np.zeros((10, 10), dtype=np.uint8)
    b = a.copy()
    b[2, 2] = 255
    b[3, 3] = 255  # diagonal neighbour -> same component
    assert detect_changes(b, a) == [Rect(2, 2, 2, 2)]


def test_detect_changes_threshold_is_strictly_greater_than():
    """Default 0 means *any* non-zero difference counts -- spec §4.2."""
    a = np.zeros((8, 8), dtype=np.uint8)
    b = a.copy()
    b[1, 1] = 1
    assert detect_changes(b, a, threshold=0.0) == [Rect(1, 1, 1, 1)]
    assert detect_changes(b, a, threshold=1.0) == []


def test_detect_changes_on_identical_images_is_empty():
    a = np.full((16, 16), 42, dtype=np.uint8)
    assert detect_changes(a, a.copy()) == []


def test_detect_changes_shape_mismatch_raises():
    with pytest.raises(ValueError, match="equal shapes"):
        detect_changes(np.zeros((4, 4), np.uint8), np.zeros((4, 5), np.uint8))


def test_detect_changes_handles_many_regions():
    """The >100-region path (``changes.cpp`` step 10)."""
    a = np.zeros((200, 200), dtype=np.uint8)
    b = a.copy()
    for i in range(12):
        for j in range(12):
            b[i * 16 + 1, j * 16 + 1] = 255
    out = detect_changes(b, a)
    assert 0 < len(out) <= 144
