#!/usr/bin/env python3
"""Generate the blue-noise threshold matrix shipped in
``pyvisionect/imaging/data/blue_noise_64.npy``.

Implements Ulichney's *void-and-cluster* method ("The void-and-cluster method
for dither array generation", SPIE 1913, 1993):

1. Build an initial binary pattern (IBP) by repeatedly moving the pixel of the
   tightest cluster into the largest void until the process reaches a fixed
   point.  The result is the *prototype binary pattern*.
2. Phase I   -- remove the tightest cluster repeatedly, numbering downwards
               from ``ones-1`` to 0.
3. Phase II  -- insert into the largest void repeatedly, numbering upwards from
               ``ones`` until half the pixels are set.
4. Phase III -- continue on the inverted pattern (tightest cluster of the
               *minority* colour) for the second half.

The "cluster/void" measure is a cyclic Gaussian-filtered copy of the binary
pattern (sigma = 1.5, as recommended by Ulichney).  The filter field is
maintained incrementally, so the whole run is O(N^2) rather than O(N^2 log N).

The output is the *rank* matrix rescaled to uint8 thresholds in [0, 255].

This script is deterministic (fixed RNG seed) and only needs to be re-run if
the matrix size changes.  Its output is committed; the library never generates
a matrix at import time.
"""

from __future__ import annotations

import argparse
import pathlib

import numpy as np


def _gaussian_kernel(size: int, sigma: float) -> np.ndarray:
    """Cyclic Gaussian kernel of shape (size, size), centred at (0, 0)."""
    d = np.arange(size)
    d = np.minimum(d, size - d)  # wrap-around distance
    dy, dx = np.meshgrid(d, d, indexing="ij")
    k = np.exp(-(dx.astype(np.float64) ** 2 + dy.astype(np.float64) ** 2) / (2.0 * sigma**2))
    return k


class _Field:
    """Cyclic Gaussian-filtered view of a binary pattern, updated in place."""

    def __init__(self, size: int, sigma: float) -> None:
        self.size = size
        self.kernel = _gaussian_kernel(size, sigma)
        self.value = np.zeros((size, size), dtype=np.float64)
        # Pre-roll the kernel for every origin lazily via np.roll (cheap at 64x64).

    def add(self, y: int, x: int, sign: float = 1.0) -> None:
        self.value += sign * np.roll(np.roll(self.kernel, y, axis=0), x, axis=1)


def _tightest_cluster(field: _Field, pattern: np.ndarray) -> tuple[int, int]:
    """Set pixel with the largest filter value (densest neighbourhood)."""
    masked = np.where(pattern, field.value, -np.inf)
    idx = int(np.argmax(masked))
    return divmod(idx, field.size)


def _largest_void(field: _Field, pattern: np.ndarray) -> tuple[int, int]:
    """Clear pixel with the smallest filter value (emptiest neighbourhood)."""
    masked = np.where(pattern, np.inf, field.value)
    idx = int(np.argmin(masked))
    return divmod(idx, field.size)


def void_and_cluster(size: int = 64, sigma: float = 1.5, seed: int = 20251004) -> np.ndarray:
    n = size * size
    rng = np.random.default_rng(seed)

    # --- initial binary pattern: ~1/10 of the pixels, uniformly at random ----
    ones = max(1, n // 10)
    flat = rng.permutation(n)[:ones]
    pattern = np.zeros((size, size), dtype=bool)
    pattern.flat[flat] = True

    field = _Field(size, sigma)
    for y, x in zip(*np.nonzero(pattern)):
        field.add(int(y), int(x), 1.0)

    # --- step 1: relax to the prototype binary pattern -----------------------
    while True:
        cy, cx = _tightest_cluster(field, pattern)
        pattern[cy, cx] = False
        field.add(cy, cx, -1.0)
        vy, vx = _largest_void(field, pattern)
        if (vy, vx) == (cy, cx):
            pattern[cy, cx] = True
            field.add(cy, cx, 1.0)
            break
        pattern[vy, vx] = True
        field.add(vy, vx, 1.0)

    prototype = pattern.copy()
    rank = np.full((size, size), -1, dtype=np.int64)

    # --- phase I: remove tightest clusters, numbering downwards --------------
    work = prototype.copy()
    wfield = _Field(size, sigma)
    for y, x in zip(*np.nonzero(work)):
        wfield.add(int(y), int(x), 1.0)
    for r in range(ones - 1, -1, -1):
        cy, cx = _tightest_cluster(wfield, work)
        work[cy, cx] = False
        wfield.add(cy, cx, -1.0)
        rank[cy, cx] = r

    # --- phase II: fill largest voids, numbering upwards --------------------
    work = prototype.copy()
    wfield = _Field(size, sigma)
    for y, x in zip(*np.nonzero(work)):
        wfield.add(int(y), int(x), 1.0)
    for r in range(ones, n // 2):
        vy, vx = _largest_void(wfield, work)
        work[vy, vx] = True
        wfield.add(vy, vx, 1.0)
        rank[vy, vx] = r

    # --- phase III: invert; tightest cluster of the minority colour ---------
    # `work` now has n//2 ones.  Looking for the tightest cluster of ZEROS is
    # the same as the largest void of ONES in the inverted field.
    inv = ~work
    ifield = _Field(size, sigma)
    for y, x in zip(*np.nonzero(inv)):
        ifield.add(int(y), int(x), 1.0)
    for r in range(n // 2, n):
        cy, cx = _tightest_cluster(ifield, inv)
        inv[cy, cx] = False
        ifield.add(cy, cx, -1.0)
        rank[cy, cx] = r

    assert (rank >= 0).all(), "void-and-cluster left pixels unranked"
    assert len(np.unique(rank)) == n, "ranks are not a permutation"
    return rank


def ranks_to_thresholds(rank: np.ndarray) -> np.ndarray:
    """Rescale a rank permutation of 0..N-1 to uint8 thresholds 0..255.

    ``(rank + 0.5) / N * 256`` puts the thresholds at bin centres, so the
    matrix is symmetric about 128 and never produces a 0 or 255 bias.
    """
    n = rank.size
    thr = np.floor((rank.astype(np.float64) + 0.5) / n * 256.0)
    return np.clip(thr, 0, 255).astype(np.uint8)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--size", type=int, default=64)
    ap.add_argument("--sigma", type=float, default=1.5)
    ap.add_argument("--seed", type=int, default=20251004)
    ap.add_argument("--out", type=pathlib.Path, required=True)
    args = ap.parse_args()

    rank = void_and_cluster(args.size, args.sigma, args.seed)
    thr = ranks_to_thresholds(rank)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    np.save(args.out, thr)
    print(f"wrote {args.out} shape={thr.shape} dtype={thr.dtype} "
          f"min={thr.min()} max={thr.max()} mean={thr.mean():.2f}")


if __name__ == "__main__":
    main()
