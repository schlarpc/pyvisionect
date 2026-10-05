"""Session-canvas <-> display-local coordinate translation.  Spec §1.5.

``Display.RectangleTranslateToReal`` (``display-translations.go:246``) takes a
rectangle in *session/canvas* coordinates and produces a *display-local,
rotation-applied* rectangle stamped with that display's ``ScreenID``::

    Wd, Hd  = display.DimensionsRotated()
    rctX    = clamp(in.X,              display.X, display.X+Wd)
    rctY    = clamp(in.Y,              display.Y, display.Y+Hd)
    rctFarX = clamp(in.X+in.Width,     rctX,      display.X+Wd)
    rctFarY = clamp(in.Y+in.Height,    rctY,      display.Y+Hd)
    if rctFarX <= rctX || rctFarY <= rctY: error "rectangle is not for this display"
    Xd, Yd  = rctX-display.X, rctY-display.Y
    switch rotation {
    case 0: out.X, out.Y = Xd, Yd
    case 1: out.X, out.Y = Hd-Yd-h, Xd ; out.W, out.H = h, w
    case 2: out.X, out.Y = Wd-Xd-w, Hd-Yd-h
    case 3: out.X, out.Y = Yd, Wd-Xd-w ; out.W, out.H = h, w
    }

.. note::
   Only ``rotation == 0`` is verified against real captured traffic (our sign
   runs it).  1/2/3 implement the formulas above and are covered by
   round-trip tests against :func:`translate_to_session`, but no rotated
   hardware was available.
"""

from __future__ import annotations

import numpy as np

from .panel import DisplayGeometry
from .rects import Rect

__all__ = ["translate_to_real", "translate_to_session", "rotate_image",
           "unrotate_image", "mirror_image"]


def _clamp(v: int, lo: int, hi: int) -> int:
    return lo if v < lo else (hi if v > hi else v)


def translate_to_real(rect: Rect, display: DisplayGeometry) -> Rect:
    """Canvas rectangle -> display-local, rotation-applied rectangle.

    :raises ValueError: if the rectangle does not intersect this display
        (the vendor logs ``"rectangle is outside of this display"`` and
        returns ``errors.New("rectangle is not for this display")``).
    """
    wd, hd = display.rotated_width, display.rotated_height
    x0 = _clamp(rect.x, display.x, display.x + wd)
    y0 = _clamp(rect.y, display.y, display.y + hd)
    x1 = _clamp(rect.far_x, x0, display.x + wd)
    y1 = _clamp(rect.far_y, y0, display.y + hd)
    if x1 <= x0 or y1 <= y0:
        raise ValueError(f"rectangle {rect!r} is not for display {display.id}")
    w, h = x1 - x0, y1 - y0
    xd, yd = x0 - display.x, y0 - display.y
    rot = display.rotation
    if rot == 0:
        return Rect(xd, yd, w, h)
    if rot == 1:
        return Rect(hd - yd - h, xd, h, w)
    if rot == 2:
        return Rect(wd - xd - w, hd - yd - h, w, h)
    if rot == 3:
        return Rect(yd, wd - xd - w, h, w)
    raise ValueError(f"bad display rotation {rot!r}")


def translate_to_session(rect: Rect, display: DisplayGeometry) -> Rect:
    """Inverse of :func:`translate_to_real` (``display-translations.go:287``)."""
    wd, hd = display.rotated_width, display.rotated_height
    rot = display.rotation
    if rot == 0:
        xd, yd, w, h = rect.x, rect.y, rect.width, rect.height
    elif rot == 1:
        w, h = rect.height, rect.width
        xd, yd = rect.y, hd - rect.x - h
    elif rot == 2:
        w, h = rect.width, rect.height
        xd, yd = wd - rect.x - w, hd - rect.y - h
    elif rot == 3:
        w, h = rect.height, rect.width
        xd, yd = wd - rect.y - w, rect.x
    else:
        raise ValueError(f"bad display rotation {rot!r}")
    return Rect(display.x + xd, display.y + yd, w, h)


def rotate_image(tile: np.ndarray, rotation: int) -> np.ndarray:
    """``imgproc.CvRotateOrthogonal`` -- rotate a display-local crop.

    The rotation sense is pinned to :func:`translate_to_real`: after rotating,
    the pixel at the rectangle's translated ``(x, y)`` must be the pixel that
    was at its canvas ``(x, y)``.  ``rotation == 1`` (90 degrees) therefore
    maps canvas ``(xd, yd)`` to ``(Hd-yd-h, xd)``, which is ``np.rot90`` with
    ``k = -1`` (clockwise).
    """
    if rotation == 0:
        return tile
    if rotation == 1:
        return np.rot90(tile, k=-1)
    if rotation == 2:
        return np.rot90(tile, k=2)
    if rotation == 3:
        return np.rot90(tile, k=1)
    raise ValueError(f"bad display rotation {rotation!r}")


def unrotate_image(tile: np.ndarray, rotation: int) -> np.ndarray:
    """Inverse of :func:`rotate_image`."""
    if rotation == 0:
        return tile
    if rotation == 1:
        return np.rot90(tile, k=1)
    if rotation == 2:
        return np.rot90(tile, k=2)
    if rotation == 3:
        return np.rot90(tile, k=-1)
    raise ValueError(f"bad display rotation {rotation!r}")


def mirror_image(tile: np.ndarray) -> np.ndarray:
    """Horizontal flip -- ``imgproc.CvFlip``, the whole of ``eink-flip``.

    Spec §1.6.  Self-inverse, so the same function un-mirrors.
    """
    return tile[:, ::-1]
