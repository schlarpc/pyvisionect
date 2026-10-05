"""Quantisation and dithering.  Spec §3.

**Nothing about grey-level reduction happens on the device.**  The panel
receives already-quantised 1 bpp or 4 bpp data and only drives the waveform
(spec §3.3).  So every mode here is a purely library-side choice with zero
protocol impact -- the ``DitheringType`` number never leaves the process.

Output levels
-------------
A quantised buffer always lands on the encoding's uniform ramp:

* 4 bpp -> 16 levels, ``n * 17`` for ``n`` in 0..15
* 1 bpp -> 2 levels, ``0`` and ``255``

so that :func:`pyvisionect.imaging.pack.pack` is afterwards exact (its nibble
is ``pix >> 4`` and its bit is ``pix >= 0x80``).

Where each mode is applied
--------------------------
``none``, ``bayer`` and ``blue-noise`` are **pixel-local**: the output for a
pixel depends only on that pixel and on its *canvas* coordinate.  The encoder
therefore runs them **once over the whole session canvas, in canvas
coordinates**, before cropping to displays.  Two consequences, both deliberate:

* an ordered pattern never seams at a rectangle boundary -- two partial
  updates that happen to abut produce exactly the pixels one combined update
  would have;
* the result is invariant under the subsequent crop / rotate / mirror, so the
  state image and the wire data agree trivially.

``floyd-steinberg`` diffuses error and is **not** pixel-local, so it is run
per encoded rectangle in display-local (post-rotate, post-mirror) space, which
is where the vendor runs it (``eink-generic.go:135-137``, called from
``cache.CheckCache`` on the already-cut, already-rotated sub-image).  Error
diffusion is inherently seam-prone; that is a property of the algorithm, not a
bug here.

Divergence from GraphicsMagick -- read this before claiming bit-exactness
-------------------------------------------------------------------------
The vendor calls into GraphicsMagick (``bill.vnct.xyz/vss/imgproc``):

* ``bayer`` -> ``GmOrderedDither`` -> GM's ``OrderedDitherImage()``, which
  takes **no level parameter** and is a fixed ordered dither **to bi-level**.
  With ``Encoding: 4`` the vendor therefore still produces a 2-level image
  which then gets nibble-packed as ``0x0``/``0xF`` (spec §3.2, marked
  **[INFERRED]** there).  **We deliberately differ**: our ``bayer`` is a
  multi-level ordered dither to the encoding's level count, because that is
  what the 4 bpp wire format can actually carry.  Pass
  ``bayer_levels_override=2`` if you want the vendor's behaviour.
  GM's internal matrix is also not published in the report, so even the
  bi-level output would not be pixel-identical.  **This makes blue-noise
  (mode 4) the only good ordered dither at 4 bpp.**
* ``floyd-steinberg`` -> ``GmFloydSteinbergDither`` ->
  ``QuantizeImage(number_colors=encodingLevels, dither=1)``.  GM *builds its
  palette from the image histogram*, so at 16 colours it produces 16
  **image-derived** greys which the packer then truncates to nibbles -- lossy
  and content-dependent.  We quantise to the uniform ``n*17`` ramp instead,
  which is what the wire format encodes, and we use Pillow's
  Floyd-Steinberg error diffusion.
  **This is not bit-exact to GraphicsMagick and has not been claimed to be.**
  No GM output was available to diff against.

What *is* verified against real device bytes is the ``none`` path: see
``tests/imaging/test_golden_capture.py``.
"""

from __future__ import annotations

import functools
import importlib.resources
import warnings
from typing import Optional

import numpy as np
from PIL import Image

from .constants import ENCODING_LEVELS, LEVEL_STEP, Dithering, Encoding

__all__ = [
    "bayer_matrix",
    "BAYER_8",
    "blue_noise_matrix",
    "quantise",
    "quantise_none",
    "quantise_ordered",
    "quantise_floyd_steinberg",
    "tile_matrix",
    "is_pixel_local",
]


# --------------------------------------------------------------------------
# threshold matrices
# --------------------------------------------------------------------------

def bayer_matrix(size: int = 8) -> np.ndarray:
    """Recursive Bayer (ordered) threshold matrix, uint8 in ``[0, 255]``.

    ``size`` must be a power of two.  The classic recursion is

    .. code-block:: text

        M_1   = [0]
        M_2n  = [[4M+0, 4M+2],
                 [4M+3, 4M+1]]

    which yields ``[[0,2],[3,1]]`` at 2x2.  The integer ranks ``0..size^2-1``
    are rescaled to bin-centre thresholds ``floor((r + 0.5) / size^2 * 256)``,
    so the matrix mean is 127.5 and the dither is unbiased.
    """
    if size < 1 or size & (size - 1):
        raise ValueError(f"bayer matrix size must be a power of two, got {size}")
    m = np.zeros((1, 1), dtype=np.int64)
    while m.shape[0] < size:
        m = np.block([[4 * m, 4 * m + 2], [4 * m + 3, 4 * m + 1]])
    n = size * size
    return np.floor((m + 0.5) / n * 256.0).clip(0, 255).astype(np.uint8)


#: The default ordered-dither matrix.
BAYER_8 = bayer_matrix(8)


@functools.lru_cache(maxsize=1)
def blue_noise_matrix() -> np.ndarray:
    """The shipped 64x64 blue-noise threshold matrix, uint8 in ``[0, 255]``.

    Generated once by ``tools/gen_blue_noise.py`` with Ulichney's
    void-and-cluster algorithm (sigma 1.5, fixed seed) and **committed** as
    ``data/blue_noise_64.npy``.  Nothing is generated at import time, so the
    output of :func:`quantise` is reproducible across runs and machines.

    Why blue noise is the right default for an e-ink sign: it is *spatially
    stable*.  A small content change perturbs only the pixels underneath it,
    whereas Floyd-Steinberg propagates error across the rest of the image, so a
    one-word text change can dirty a huge area.  That directly inflates the
    change-detect rectangle list, defeats the <=15-region merge (spec §4.1) and
    causes more panel flashing.  Ordered dithers keep dirty regions tight, and
    blue noise gets that stability without Bayer's visible cross-hatch.
    ``tests/imaging/test_dither.py::test_spatial_stability_ranking`` measures
    exactly this.
    """
    with importlib.resources.as_file(
        importlib.resources.files(__package__) / "data" / "blue_noise_64.npy"
    ) as path:
        m = np.load(path)
    if m.dtype != np.uint8 or m.ndim != 2:
        raise ValueError("blue-noise matrix must be a 2-D uint8 array")
    return m


def tile_matrix(matrix: np.ndarray, shape: tuple[int, int],
                origin: tuple[int, int] = (0, 0)) -> np.ndarray:
    """Tile a threshold matrix over ``shape``, phased by an absolute ``origin``.

    ``origin`` is ``(y, x)`` in **canvas** coordinates.  Phasing on the
    absolute position is what makes an ordered dither seam-free across
    independently-encoded rectangles.
    """
    h, w = shape
    oy, ox = origin
    mh, mw = matrix.shape
    ys = (np.arange(oy, oy + h) % mh)
    xs = (np.arange(ox, ox + w) % mw)
    return matrix[np.ix_(ys, xs)]


# --------------------------------------------------------------------------
# quantisers
# --------------------------------------------------------------------------

def _levels_and_step(encoding: int, levels: int | None = None) -> tuple[int, int]:
    """Resolve ``(levels, step)`` so that output values are ``n * step``.

    ``step`` is derived from the *level count actually used*, not from the
    encoding, so that a reduced level count (e.g. bi-level Bayer at 4 bpp)
    still lands on values the encoding can carry: 2 levels -> ``0``/``255``,
    which nibble-pack to ``0x0``/``0xF``.
    """
    try:
        default_levels = ENCODING_LEVELS[int(encoding)]
        default_step = LEVEL_STEP[int(encoding)]
    except KeyError:
        raise ValueError(
            f"bad encoding {encoding!r}; only 1 and 4 exist (spec §2.1)"
        ) from None
    if levels is None:
        return default_levels, default_step
    if levels < 2:
        raise ValueError("levels must be >= 2")
    if levels > default_levels:
        raise ValueError(
            f"encoding {int(encoding)} carries at most {default_levels} levels, "
            f"got {levels}"
        )
    if 255 % (levels - 1):
        raise ValueError(
            f"{levels} levels do not divide the 0..255 range evenly; the wire "
            "format only has uniform ramps (spec §2.3)"
        )
    return levels, 255 // (levels - 1)


def quantise_none(grey: np.ndarray, encoding: int) -> np.ndarray:
    """``DitheringType 1`` -- no dithering.

    4 bpp: plain truncation of the top nibble, ``(pix >> 4) * 17``.
    1 bpp: MSB threshold, ``(pix >= 0x80) * 255``.

    Exactly what :func:`pyvisionect.imaging.pack.pack` would do on its own, so
    a buffer that is already on the ``n * 17`` ramp survives **unchanged**
    (``(n*17) >> 4 == n`` for all ``n`` in 0..15).  That losslessness is the
    property the user's pre-dithered Home Assistant content depends on.
    """
    g = np.asarray(grey, dtype=np.uint8)
    if encoding == Encoding.FOUR_BIT:
        return ((g >> 4) * np.uint8(17)).astype(np.uint8)
    if encoding == Encoding.ONE_BIT:
        return np.where(g >= 0x80, np.uint8(255), np.uint8(0)).astype(np.uint8)
    raise ValueError(f"bad encoding {encoding!r}; only 1 and 4 exist (spec §2.1)")


def quantise_ordered(grey: np.ndarray, encoding: int, matrix: np.ndarray,
                     origin: tuple[int, int] = (0, 0),
                     levels: int | None = None) -> np.ndarray:
    """Ordered (point) dither against a tiled uint8 threshold matrix.

    The quantiser is integer-exact and unbiased::

        n = (pix * (L-1) * 256 + threshold * 255) // (255 * 256)
        out = clip(n, 0, L-1) * step

    Scaling the pixel by 256 and the threshold by 255 (rather than mixing the
    two scales) makes the result **idempotent on the output ramp**: a pixel
    that is already exactly ``n * step`` quantises back to ``n`` for every
    threshold value, so re-dithering never shifts already-quantised content.

    :param origin: ``(y, x)`` canvas coordinate of ``grey[0, 0]``; the matrix
        phase follows it, which is what keeps independently-encoded rectangles
        seam-free.
    """
    g = np.asarray(grey, dtype=np.uint8)
    nlevels, step = _levels_and_step(encoding, levels)
    thr = tile_matrix(matrix, g.shape, origin).astype(np.int32)
    num = g.astype(np.int32) * (nlevels - 1) * 256 + thr * 255
    n = np.clip(num // (255 * 256), 0, nlevels - 1)
    return (n * step).astype(np.uint8)


def quantise_floyd_steinberg(grey: np.ndarray, encoding: int,
                             levels: int | None = None) -> np.ndarray:
    """Floyd-Steinberg error diffusion onto the encoding's uniform ramp.

    Implemented with Pillow's palette quantiser
    (``Image.quantize(palette=..., dither=FLOYDSTEINBERG)``), with the palette
    pinned to the ``step * n`` ramp so the result is exactly representable on
    the wire.  See the module docstring for the divergence from
    GraphicsMagick's ``QuantizeImage``.
    """
    g = np.ascontiguousarray(np.asarray(grey, dtype=np.uint8))
    nlevels, step = _levels_and_step(encoding, levels)
    if g.size == 0:
        return g.copy()
    pal = Image.new("P", (1, 1))
    entries: list[int] = []
    for n in range(nlevels):
        v = min(255, n * step)
        entries += [v, v, v]
    pal.putpalette(entries)
    # Pillow's palette quantiser ignores `palette=` for mode-"L" input (it
    # returns an identity mapping), so the source must be RGB.
    src = Image.fromarray(g, "L").convert("RGB")
    idx = np.array(src.quantize(palette=pal, dither=Image.Dither.FLOYDSTEINBERG))
    return np.minimum(idx.astype(np.int32) * step, 255).astype(np.uint8)


#: Modes whose output for a pixel depends only on that pixel and its canvas
#: coordinate.  The encoder runs these once over the whole canvas; everything
#: else is run per encoded rectangle.
_PIXEL_LOCAL = frozenset({Dithering.NONE, Dithering.BAYER, Dithering.BLUE_NOISE})


def is_pixel_local(dithering: int) -> bool:
    """Whether ``dithering`` can be applied canvas-wide (see module docstring)."""
    return int(dithering) in _PIXEL_LOCAL


def _bayer_levels(encoding: int, levels: int | None) -> int:
    """Resolve the level count for ``DitheringType 2`` -- bi-level, loudly.

    The vendor's ``bayer`` is ``imgproc.GmOrderedDither`` ->
    GraphicsMagick ``OrderedDitherImage()``, which **takes no level
    parameter and is a fixed ordered dither to bi-level** (spec §3.2).  With
    ``Encoding: 4`` the vendor therefore still emits a 2-level image, which
    then gets nibble-packed as ``0x0``/``0xF``.

    We reproduce that -- silently inventing a 16-level "bayer" would not match
    any real server -- but warn, because at 4 bpp it throws away 14 of the 16
    levels the panel can show.  **Blue-noise (mode 4) is the only good ordered
    dither at 4 bpp**; prefer it.  Pass ``levels=16`` explicitly to opt into a
    multi-level ordered dither with the Bayer matrix.
    """
    if levels is not None:
        return levels
    if int(encoding) == Encoding.FOUR_BIT:
        warnings.warn(
            "DitheringType 2 (bayer) is bi-level only: GraphicsMagick's "
            "OrderedDitherImage() takes no level parameter, so a 4 bpp bayer "
            "rectangle degenerates to 2 of the 16 available grey levels "
            "(packed as 0x0/0xF). Use DitheringType 4 (blue-noise) for a "
            "16-level ordered dither, or pass levels=16 to force one.",
            stacklevel=3,
        )
    return 2


def quantise(grey: np.ndarray, encoding: int, dithering: int,
             origin: tuple[int, int] = (0, 0),
             bayer: Optional[np.ndarray] = None,
             blue_noise: Optional[np.ndarray] = None,
             levels: int | None = None) -> np.ndarray:
    """Quantise an 8-bit grey buffer for ``encoding`` using ``dithering``.

    :param dithering: a :class:`~pyvisionect.imaging.constants.Dithering`
        value.  ``0`` (``DEFAULT``) is rejected, matching the vendor's
        ``updateDefaultRectUnlocked`` (``image-state.go:754``).
    :param origin: canvas coordinate of ``grey[0, 0]``; only used by the
        ordered modes.
    :param bayer: threshold matrix overriding :data:`BAYER_8`.
    :param blue_noise: threshold matrix overriding
        :func:`blue_noise_matrix` -- drop in the exact matrix your renderer
        uses to get identical output.
    """
    d = int(dithering)
    if d == Dithering.DEFAULT:
        raise ValueError(
            "DitheringType 0 ('default') is invalid at the encoder; the vendor "
            "rejects it with render.ErrDithering (image-state.go:754). "
            "Pass 1 (none), 2 (bayer), 3 (floyd-steinberg) or 4 (blue-noise)."
        )
    if d == Dithering.NONE:
        return quantise_none(grey, encoding)
    if d == Dithering.BAYER:
        return quantise_ordered(grey, encoding,
                                BAYER_8 if bayer is None else bayer,
                                origin, _bayer_levels(encoding, levels))
    if d == Dithering.FLOYD_STEINBERG:
        return quantise_floyd_steinberg(grey, encoding, levels)
    if d == Dithering.BLUE_NOISE:
        return quantise_ordered(grey, encoding,
                                blue_noise_matrix() if blue_noise is None else blue_noise,
                                origin, levels)
    raise ValueError(
        f"unknown DitheringType {dithering!r}; known values are "
        "0 default (invalid), 1 none, 2 bayer, 3 floyd-steinberg, "
        "4 blue-noise (library extension)"
    )
