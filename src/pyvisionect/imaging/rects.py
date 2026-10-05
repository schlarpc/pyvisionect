"""Rectangle bookkeeping: change detection, region merging, alignment.

Spec §2.4 (the 16-bit quantum), §4.1 (``MergeRegions``) and §4.2
(``ChangesAutodetect``).  All functions are pure.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Iterable, Sequence

import numpy as np

from .constants import (ALIGN_QUANTUM, MAX_REGIONS_PER_DISPLAY,
                        MERGE_THRESHOLD_PERCENT, MERGE_THRESHOLD_STEP_PERCENT)

__all__ = [
    "Rect",
    "detect_changes",
    "unite_overlapping_regions",
    "join_contours",
    "apply_region_count_limit",
    "limit_regions",
    "align_to_quantum",
    "is_aligned",
]


@dataclass(frozen=True)
class Rect:
    """An axis-aligned rectangle, ``(x, y, width, height)``, integer pixels."""

    x: int
    y: int
    width: int
    height: int

    def __post_init__(self) -> None:
        if self.width < 0 or self.height < 0:
            raise ValueError(f"negative rectangle extent: {self!r}")

    # -- geometry ----------------------------------------------------------

    @property
    def far_x(self) -> int:
        return self.x + self.width

    @property
    def far_y(self) -> int:
        return self.y + self.height

    @property
    def area(self) -> int:
        return self.width * self.height

    @property
    def center(self) -> tuple[float, float]:
        """``join.go:221-228`` uses the centre for pairwise distances."""
        return (self.x + 0.5 * self.width, self.y + 0.5 * self.height)

    @property
    def is_empty(self) -> bool:
        return self.width == 0 or self.height == 0

    def overlaps(self, other: "Rect") -> bool:
        """Strict AABB intersection -- ``RenderRect.rectsOverlap``, ``join.go:31-48``.

        Touching edges do **not** count, and an empty rectangle never overlaps.
        """
        if self.is_empty or other.is_empty:
            return False
        return (self.x < other.far_x and other.x < self.far_x
                and self.y < other.far_y and other.y < self.far_y)

    def bounding_box(self, other: "Rect") -> "Rect":
        """``commonRect`` -- ``join.go:185-192``.  No overlap test."""
        x = min(self.x, other.x)
        y = min(self.y, other.y)
        return Rect(x, y, max(self.far_x, other.far_x) - x,
                    max(self.far_y, other.far_y) - y)

    def union(self, other: "Rect") -> "Rect | None":
        """``RenderRect.Union`` -- ``join.go:77-99``.  **Requires** overlap."""
        if not self.overlaps(other):
            return None
        return self.bounding_box(other)

    def intersection(self, other: "Rect") -> "Rect | None":
        x = max(self.x, other.x)
        y = max(self.y, other.y)
        fx = min(self.far_x, other.far_x)
        fy = min(self.far_y, other.far_y)
        if fx <= x or fy <= y:
            return None
        return Rect(x, y, fx - x, fy - y)

    def translated(self, dx: int, dy: int) -> "Rect":
        return replace(self, x=self.x + dx, y=self.y + dy)

    def as_slice(self) -> tuple[slice, slice]:
        """``(rows, cols)`` numpy slices for a canvas-space crop."""
        return (slice(self.y, self.far_y), slice(self.x, self.far_x))


# --------------------------------------------------------------------------
# change detection -- spec §4.2
# --------------------------------------------------------------------------

def _connected_components(mask: np.ndarray) -> list[Rect]:
    """Bounding boxes of 8-connected ``True`` components, largest area first.

    The vendor runs ``cvAbsDiff`` -> ``cvThreshold`` -> ``cvFindContours``
    (``CV_RETR_EXTERNAL``) -> ``cvBoundingRect``, sorted by contour area
    descending (``changes.cpp``, spec §4.2).  The bounding boxes of 8-connected
    components are the same set of rectangles as the bounding boxes of external
    contours.

    .. note::
       We sort by **bounding-box** area, not by contour area as OpenCV does.
       The ordering only affects which pair ``joinContours`` happens to merge
       first when the region count is already over the limit, never
       correctness.  Implemented with run-length rows + union-find so that
       ``imaging/`` needs no scipy or OpenCV.
    """
    h, w = mask.shape
    parent: list[int] = []

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    runs_by_row: list[list[tuple[int, int, int]]] = []  # (x0, x1_exclusive, label)
    for y in range(h):
        row = mask[y]
        if not row.any():
            runs_by_row.append([])
            continue
        # run starts/ends via edge differences
        d = np.flatnonzero(np.diff(np.concatenate(([0], row.view(np.uint8), [0]))))
        starts, ends = d[0::2], d[1::2]
        runs: list[tuple[int, int, int]] = []
        prev = runs_by_row[y - 1] if y else []
        for x0, x1 in zip(starts.tolist(), ends.tolist()):
            label = len(parent)
            parent.append(label)
            # 8-connectivity: the previous row's runs touching [x0-1, x1]
            for px0, px1, plabel in prev:
                if px0 <= x1 and x0 <= px1:
                    union(label, plabel)
            runs.append((x0, x1, label))
        runs_by_row.append(runs)

    boxes: dict[int, list[int]] = {}
    for y, runs in enumerate(runs_by_row):
        for x0, x1, label in runs:
            root = find(label)
            b = boxes.get(root)
            if b is None:
                boxes[root] = [x0, y, x1, y + 1]
            else:
                b[0] = min(b[0], x0)
                b[2] = max(b[2], x1)
                b[3] = y + 1
    rects = [Rect(x0, y0, x1 - x0, y1 - y0) for x0, y0, x1, y1 in boxes.values()]
    rects.sort(key=lambda r: (-r.area, r.y, r.x))
    return rects


def detect_changes(new_grey: np.ndarray, old_grey: np.ndarray,
                   threshold: float = 0.0) -> list[Rect]:
    """``changes.DetectorCV.DetectChanges`` -- spec §4.2.

    ``threshold`` is a **per-pixel absolute grey difference**.  The vendor's
    default is ``0``, and ``cvThreshold(diff, 0, 255, CV_THRESH_BINARY)`` marks
    a pixel changed when ``diff > 0`` -- i.e. *any* non-zero difference.  So
    the comparison is strictly greater-than, not greater-or-equal.

    The two-pass structure of ``get_changes`` is reproduced:
    components -> ``uniteOverlappingRegions(areaCriteria=false)`` -> (if more
    than 100 regions) ``joinContours`` at 5 %/10 % of the canvas diagonal ->
    ``uniteOverlappingRegions(areaCriteria=true)``.

    .. note::
       The vendor chunks the >100 case into 100-rect groups before the final
       join (``changes.cpp``, marked **[INFERRED]** in the spec).  We run the
       single global ``joinContours`` pass only; the chunking is a performance
       detail whose bookkeeping the report could not pin down.
    """
    a = np.asarray(new_grey, dtype=np.uint8)
    b = np.asarray(old_grey, dtype=np.uint8)
    if a.shape != b.shape:
        raise ValueError(f"change detection needs equal shapes, got {a.shape} vs {b.shape}")
    diff = np.abs(a.astype(np.int16) - b.astype(np.int16))
    mask = diff > threshold
    if not mask.any():
        return []
    rects = _connected_components(mask)
    rects = unite_overlapping_regions(rects, area_criteria=False)
    if len(rects) > 100:
        h, w = a.shape
        diag = math.hypot(w, h)
        rects = join_contours(100,
                              diag * MERGE_THRESHOLD_PERCENT / 100.0,
                              diag * MERGE_THRESHOLD_STEP_PERCENT / 100.0,
                              rects)
    return unite_overlapping_regions(rects, area_criteria=True)


# --------------------------------------------------------------------------
# region merging -- spec §4.1
# --------------------------------------------------------------------------

def unite_overlapping_regions(rects: Sequence[Rect],
                              area_criteria: bool = True) -> list[Rect]:
    """``uniteOverlappingRegions`` -- ``join.go:127-182``.  Iterates to a fixpoint.

    :param area_criteria: when true, a union is rejected if its area exceeds
        the sum of the two input areas (``join.go:150-155``), i.e. merging must
        not make the repaint bigger.
    """
    items: list[Rect] = list(rects)
    changed = True
    while changed:
        changed = False
        i = 0
        while i < len(items):
            j = i + 1
            while j < len(items):
                r1, r2 = items[i], items[j]
                if not r1.overlaps(r2):
                    j += 1
                    continue
                u = r1.union(r2)
                if u is None or u.is_empty:
                    j += 1
                    continue
                if area_criteria and u.area > r1.area + r2.area:
                    j += 1
                    continue
                items[i] = u
                del items[j]
                changed = True
            i += 1
    return items


def join_contours(max_regions: int, dist_threshold: float, dist_threshold_step: float,
                  rects: Sequence[Rect]) -> list[Rect]:
    """``joinContures`` -- ``join.go:195-350``.

    Sorts all pairwise centre distances ascending, then repeatedly groups
    rectangles whose centres are closer than ``threshold`` into bounding boxes,
    raising ``threshold`` by ``dist_threshold_step`` until the region count
    drops to ``max_regions`` (or the step is zero, which makes it a single
    pass).
    """
    inp = list(rects)
    if max_regions == 0:
        raise ValueError("region count upper limit 'maxRegions' should greater than 0")
    if dist_threshold_step < 0:
        raise ValueError("join threshold step 'distThresholdStep' should not be a negative number")
    if dist_threshold <= 0:
        raise ValueError("join threshold 'distThreshold' should be greater than 0")
    if len(inp) <= max_regions:
        return inp

    distances: list[tuple[float, int, int]] = []
    for i in range(len(inp) - 1):
        c1 = inp[i].center
        for j in range(i + 1, len(inp)):
            c2 = inp[j].center
            distances.append((math.hypot(c1[0] - c2[0], c1[1] - c2[1]), i, j))
    distances.sort(key=lambda t: (t[0], t[1], t[2]))

    threshold = dist_threshold
    out: list[Rect] = inp
    while True:
        used: list[int] = [-1] * len(inp)
        regions: list[Rect] = []
        for d, i, j in distances:
            if threshold <= d:
                continue
            a, b = used[i], used[j]
            if a == -1 and b == -1:
                regions.append(inp[i].bounding_box(inp[j]))
                used[i] = used[j] = len(regions) - 1
            elif a == -1:
                regions[b] = regions[b].bounding_box(inp[i])
                used[i] = b
            elif b == -1:
                regions[a] = regions[a].bounding_box(inp[j])
                used[j] = a
            elif a != b:
                regions[a] = regions[a].bounding_box(regions[b])
                for k, v in enumerate(used):
                    if v == b:
                        used[k] = a
        regions.extend(inp[k] for k, v in enumerate(used) if v == -1)
        out = unite_overlapping_regions(regions, area_criteria=True)
        if dist_threshold_step <= 0:
            break
        threshold += dist_threshold_step
        if len(out) <= max_regions:
            break
    return out


def apply_region_count_limit(max_regions: int, dist_threshold: float,
                             dist_threshold_step: float,
                             rects: Sequence[Rect]) -> list[Rect]:
    """``applyRegionCountLimit`` -- ``join.go:351-356``."""
    if len(rects) <= max_regions:
        return list(rects)
    return join_contours(max_regions, dist_threshold, dist_threshold_step, rects)


def limit_regions(rects: Sequence[Rect], display_width: int, display_height: int,
                  merge: bool = True,
                  threshold_percent: float = MERGE_THRESHOLD_PERCENT,
                  threshold_step_percent: float = MERGE_THRESHOLD_STEP_PERCENT,
                  max_regions: int = MAX_REGIONS_PER_DISPLAY) -> list[Rect] | None:
    """``displayRects.update``'s region-count branch -- ``image-state.go:221-247``.

    Returns the merged rectangle list, or ``None`` to mean "the caller must
    force a full-screen update" (which is what ``forceFullScreenUnlocked``
    does at ``image-state.go:241`` when merging is disabled and the count is
    over the limit).

    ``threshold_percent`` / ``threshold_step_percent`` are **percentages of the
    display diagonal** (``image-state.go:234-235``); for a 1440x640 display the
    diagonal is 1575.81 px, so the vendor defaults of 5 % / 10 % become
    78.8 px / 157.6 px.  ``NewMergeParameters`` clamps both to ``[0, 100]`` and
    substitutes 5/10 when the step is non-positive (``image-state.go:116-126``).
    """
    out = list(rects)
    if len(out) > max_regions:
        if not merge:
            return None
        step = threshold_step_percent
        start = threshold_percent
        if step <= 0:
            if start <= 0:
                start = MERGE_THRESHOLD_PERCENT
            step = MERGE_THRESHOLD_STEP_PERCENT
        start = min(100.0, max(0.0, start))
        step = min(100.0, max(0.0, step))
        diag = math.hypot(display_width, display_height)
        out = apply_region_count_limit(max_regions, start * diag / 100.0,
                                       step * diag / 100.0, out)
    return unite_overlapping_regions(out, area_criteria=True)


# --------------------------------------------------------------------------
# 16-bit quantum alignment -- spec §2.4
# --------------------------------------------------------------------------

def is_aligned(rect: Rect, encoding: int) -> bool:
    """``isDividableTo16BitQuants`` -- ``sixteen-bit-limitation.go:30-44``.

    The constraint is on ``width * height`` (total sub-pixels), because the
    packer runs linearly over the whole rectangle with no row padding.  A total
    smaller than the quantum is never acceptable (``:38-39``).
    """
    try:
        q = ALIGN_QUANTUM[int(encoding)]
    except KeyError:
        raise ValueError(f"bad encoding {encoding!r}; only 1 and 4 exist (spec §2.1)") from None
    total = rect.width * rect.height
    if total < q:
        return False
    return total % q == 0


def align_to_quantum(rect: Rect, encoding: int, border: Rect) -> Rect:
    """Grow ``rect`` outward until ``width * height`` divides the 16-bit quantum.

    Vendor equivalent: ``sizeRectFor16BitQuantEncoding`` /
    ``driverSizeRectFor16BitQuantEncodingBothDirections``
    (``sixteen-bit-limitation.go:74/81``), reached through the driver op
    ``"rectangle-encode"`` from ``render.SizeRectsForEncoding``
    (``image-state.go:1760``).

    .. note:: **[INFERRED]** growth order.
       The report records *that* the vendor grows the rectangle outward using
       ``nextCommonMultiple``, clamped to the display/canvas border, but not the
       exact order in which it tries width/height or right/left.  We grow the
       **width** to the next multiple of the quantum -- which makes
       ``width * height`` divisible for any height, so it is always sufficient
       -- preferring to grow right (towards ``border.far_x``) and falling back
       to growing left.  If the width cannot reach a multiple of the quantum
       inside the border we grow the height the same way, and if that also
       fails we raise, as the vendor does with ``"rectangle is outside
       boundaries"``.

    A rectangle that is already aligned is returned unchanged, matching the
    vendor's ``isDividableTo16BitQuants`` early exit.
    """
    try:
        q = ALIGN_QUANTUM[int(encoding)]
    except KeyError:
        raise ValueError(f"bad encoding {encoding!r}; only 1 and 4 exist (spec §2.1)") from None
    if border.width <= 0 or border.height <= 0:
        raise ValueError("bad display/image borders")
    r = rect.intersection(border)
    if r is None:
        raise ValueError(f"rectangle {rect!r} is outside boundaries {border!r}")
    if is_aligned(r, encoding):
        return r

    def grow(lo: int, extent: int, b_lo: int, b_hi: int, target: int) -> tuple[int, int] | None:
        """Grow ``[lo, lo+extent)`` to ``target`` inside ``[b_lo, b_hi)``."""
        need = target - extent
        if need <= 0 or target > b_hi - b_lo:
            return None
        right = min(need, b_hi - (lo + extent))
        lo -= need - right
        if lo < b_lo:
            return None
        return lo, target

    want_w = ((r.width + q - 1) // q) * q
    if want_w == r.width:
        want_w = r.width + q
    grown = grow(r.x, r.width, border.x, border.far_x, want_w)
    if grown is not None:
        return Rect(grown[0], r.y, grown[1], r.height)

    want_h = ((r.height + q - 1) // q) * q
    if want_h == r.height:
        want_h = r.height + q
    grown = grow(r.y, r.height, border.y, border.far_y, want_h)
    if grown is not None:
        return Rect(r.x, grown[0], r.width, grown[1])

    raise ValueError(
        f"rectangle {rect!r} cannot be aligned to the {q}-sub-pixel quantum "
        f"inside borders {border!r}"
    )


def align_all(rects: Iterable[Rect], encoding: int, border: Rect) -> list[Rect]:
    """``SizeRectsForEncoding`` -- ``image-state.go:1760``.

    Aligns every rectangle, then re-unites overlaps that the growth created.
    """
    out = [align_to_quantum(r, encoding, border) for r in rects]
    return unite_overlapping_regions(out, area_criteria=False)
