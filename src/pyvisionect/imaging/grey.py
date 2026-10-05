"""Input conversion to 8-bit grey, and the optional ``Beautify`` stage.

Spec §3.3 (``imgproc.CvToGrayImage``, ``eink-generic.go:111``) and §4.3
(``pretty.BeautifyCV``).
"""

from __future__ import annotations

from typing import Any

import numpy as np

__all__ = ["to_grey8", "beautify"]

#: Go's ``color.GrayModel`` / OpenCV's ``CV_RGB2GRAY`` luma weights, in the
#: fixed-point form Go uses: ``(19595*r + 38470*g + 7471*b + 1<<15) >> 16``,
#: i.e. 0.299 / 0.587 / 0.114.
_LUMA = (19595, 38470, 7471)


def to_grey8(img: Any) -> np.ndarray:
    """Coerce an image to a contiguous ``(height, width)`` uint8 array.

    Accepts:

    * a PIL ``Image`` in any mode (converted with ``.convert("L")``, which uses
      the same ITU-R 601 luma weights);
    * a 2-D numpy array (uint8 passed through; float assumed to be ``0..1`` if
      its max is ``<= 1.0``, else ``0..255``);
    * a 3-D numpy array with 1, 3 or 4 channels (RGB / RGBA; alpha is ignored,
      matching ``CvToGrayImage``, which only looks at the colour channels).
    """
    if hasattr(img, "convert") and hasattr(img, "size"):  # PIL.Image.Image
        pil = img if img.mode == "L" else img.convert("L")
        return np.ascontiguousarray(np.asarray(pil, dtype=np.uint8))

    arr = np.asarray(img)
    if arr.ndim == 3:
        if arr.shape[2] == 1:
            arr = arr[:, :, 0]
        elif arr.shape[2] in (3, 4):
            rgb = arr[:, :, :3].astype(np.uint32)
            acc = (rgb[:, :, 0] * _LUMA[0] + rgb[:, :, 1] * _LUMA[1]
                   + rgb[:, :, 2] * _LUMA[2] + (1 << 15)) >> 16
            arr = np.clip(acc, 0, 255).astype(np.uint8)
        else:
            raise ValueError(f"cannot convert a {arr.shape[2]}-channel image to grey")
    if arr.ndim != 2:
        raise ValueError(f"expected a 2-D image, got shape {arr.shape}")
    if arr.dtype != np.uint8:
        f = arr.astype(np.float64)
        scale = 255.0 if (f.size and f.max() <= 1.0) else 1.0
        arr = np.clip(np.rint(f * scale), 0, 255).astype(np.uint8)
    return np.ascontiguousarray(arr)


def beautify(grey: np.ndarray, gamma: float = 1.1, do2step: bool = False,
             fallback: tuple[float, float] = (0.0, 255.0)) -> np.ndarray:
    """``pretty.BeautifyCV.Beautify`` on a grey image -- spec §4.3.

    A per-channel linear contrast stretch (auto-levels) followed by a gamma
    LUT.  Reconstructed from the compiled ``beautify.cpp`` helpers::

        minMax(img, &lo, &hi, fallbackLo, fallbackHi)
            cvMinMaxLoc(...); if (lo == hi) { lo = fallbackLo; hi = fallbackHi; }
        rangeMap(img, inLo, inHi, outLo, outHi)
            range = (inHi == inLo) ? inHi : (inHi - inLo);
            scale = (outHi - outLo) / range;  shift = outLo;
            cvConvertScale(img, img, scale, shift);
        gammaCorrect(src, dst, gamma)
            cvLUT(src, dst, generateGammaLUT(gamma));

    .. note:: **[INFERRED]**
       The spec flags the exact ``shift`` term as unverified -- the compiled
       code zeroes ``inLo`` before computing it, which is only consistent with
       callers that always pass ``inLo = 0``.  We implement the textbook
       auto-levels form ``(v - lo) * 255 / (hi - lo)``, which is what the
       helper computes when ``outLo = 0`` and ``inLo`` is subtracted first.
       Do not treat this function as byte-exact to the vendor.

    This stage is **off by default** in :func:`pyvisionect.imaging.encode_frame`.
    The vendor enables it (``Beautify{Enable: true, Gamma: 1.1}``) for HTML
    sessions, but it destroys content that the renderer has already quantised
    to the output ramp -- which is exactly how the live session is driven.

    :param do2step: run the contrast stretch twice (the vendor's ``do2Step``).
    """
    g = np.asarray(grey, dtype=np.uint8)
    if g.size == 0:
        return g.copy()
    out = g.astype(np.float64)
    for _ in range(2 if do2step else 1):
        lo = float(out.min())
        hi = float(out.max())
        if lo == hi:
            lo, hi = fallback
        rng = hi - lo if hi != lo else (hi if hi else 1.0)
        out = (out - lo) * (255.0 / rng)
        out = np.clip(out, 0.0, 255.0)
    if gamma and gamma != 1.0:
        lut = np.clip(np.rint(((np.arange(256) / 255.0) ** (1.0 / gamma)) * 255.0),
                      0, 255).astype(np.uint8)
        return lut[np.clip(np.rint(out), 0, 255).astype(np.uint8)]
    return np.clip(np.rint(out), 0, 255).astype(np.uint8)
