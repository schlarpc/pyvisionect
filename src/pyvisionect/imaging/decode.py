"""Wire rectangles -> canvas.  The inverse of :func:`encode_frame`.

This is the vendor's own preview path, ``liveview.DeviceView.Update``
(``device-view.go:89-158``): de-interlace, nibble-decode, un-mirror, blit per
display.  It is exercised byte-exactly by
``tests/imaging/test_golden_capture.py``, which decodes the captured 1.84 MB
push and gets the original canvas back.

What it can and cannot give you back
------------------------------------

The device holds the **post-dither, post-pack** form of a frame, so a decode
gives you exactly the 16-level image the panel was driven with -- never the
source photograph.  Against the rectangles we encoded, though, the round trip
is exact: same bytes in, same canvas out.

``full_canvas`` wants a frame that covers the canvas
----------------------------------------------------

A full-screen push on the 32" sign is two interlaced 2880x640 rectangles that
unfold into four 1440x640 bands.  A *partial* rectangle addressed in screen
coordinates (``OPEN-QUESTIONS.md`` A10) decodes to its own tile but says
nothing about the rest of the canvas, so :func:`decode_image_packet` leaves
the uncovered area at *background* and reports what it covered rather than
pretending to a full picture.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .interlace import deinterlace, interlace_pairs
from .pack import unpack
from .panel import Panel
from .geometry import mirror_image

__all__ = ["DecodedFrame", "decode_image_packet"]


@dataclass(frozen=True)
class DecodedFrame:
    """The pixels a device was sent, plus what was actually covered."""

    canvas: np.ndarray
    """``(canvas_height, canvas_width)`` uint8, on the ``n * 17`` ramp."""

    covered: tuple[tuple[int, int, int, int], ...] = ()
    """``(x, y, w, h)`` canvas-space boxes this frame actually wrote."""

    full: bool = False
    """Whether the covered boxes account for the whole canvas."""

    screens: tuple[int, ...] = field(default=())
    """The ``ScreenID``s the packet addressed."""

    @property
    def shape(self) -> tuple[int, int]:
        return self.canvas.shape  # type: ignore[return-value]

    def to_image(self):
        """A PIL ``L`` image. Needs Pillow."""
        from PIL import Image

        return Image.fromarray(self.canvas, "L")


def _bands(panel: Panel) -> int:
    return panel.displays


def decode_image_packet(
    packet, panel: Panel, *, background: int = 0xFF
) -> DecodedFrame:
    """Rebuild the canvas from an :class:`~pyvisionect.packets.image.ImagePacket`.

    Accepts anything with ``.rectangles``, each exposing ``screen_id``, ``x``,
    ``y``, ``width``, ``height``, ``encoding`` and ``data`` -- so it takes a
    packet decoded off the wire, one read back out of a ``.pv2`` file, or one
    you built yourself.

    Args:
        packet: the image packet.
        panel: the geometry to rebuild into. Pass the same panel the frame was
            encoded for; :func:`pyvisionect.devices.panel_for` gives you the
            right one from the device's ``DisplayType``.
        background: fill for anything the frame did not cover. ``0xFF`` is
            white, the paper colour.

    Raises:
        ValueError: if a rectangle's payload is not the size its geometry
            implies, or if an interlaced frame is missing a screen's half.
    """
    panel = Panel.adapt(panel)
    canvas = np.full(panel.canvas_shape, background, dtype=np.uint8)
    mirror = bool(getattr(panel.driver, "mirror", False))
    mode = panel.interlace_mode
    band_h = panel.rotated_height
    covered: list[tuple[int, int, int, int]] = []
    screens: list[int] = []

    by_screen = {}
    for rect in packet.rectangles:
        by_screen.setdefault(int(rect.screen_id), []).append(rect)
        screens.append(int(rect.screen_id))

    if mode:
        pairs = dict(interlace_pairs(mode))
        for screen_id, rects in sorted(by_screen.items()):
            lanes = pairs.get(screen_id)
            if lanes is None:
                raise ValueError(
                    f"interlacing mode {mode} has no lane pair for ScreenID "
                    f"{screen_id}; known screens are {sorted(pairs)}"
                )
            for rect in rects:
                # A screen-space rectangle is 2x as wide as each lane's tile.
                lane_w, lane_h = rect.width // 2, rect.height
                a, b = deinterlace(rect.data, int(rect.encoding))
                for lane_data, display in zip((a, b), lanes):
                    tile = unpack(
                        lane_data, int(rect.encoding), lane_w * lane_h
                    )
                    if tile.size != lane_w * lane_h:
                        raise ValueError(
                            f"ScreenID {screen_id} lane for display {display} "
                            f"has {tile.size} pixels, expected "
                            f"{lane_w * lane_h} for {lane_w}x{lane_h}"
                        )
                    tile = tile.reshape(lane_h, lane_w)
                    # The eink-flip mirror is applied to the whole display
                    # row, so a sub-rectangle lands mirrored about the
                    # display's own width, not its own.
                    x0 = (
                        panel.rotated_width - (rect.x // 2) - lane_w
                        if mirror
                        else rect.x // 2
                    )
                    y0 = display * band_h + rect.y
                    canvas[y0 : y0 + lane_h, x0 : x0 + lane_w] = (
                        mirror_image(tile) if mirror else tile
                    )
                    covered.append((x0, y0, lane_w, lane_h))
    else:
        for screen_id, rects in sorted(by_screen.items()):
            for rect in rects:
                pixels = unpack(
                    rect.data, int(rect.encoding), rect.width * rect.height
                )
                if pixels.size != rect.width * rect.height:
                    raise ValueError(
                        f"ScreenID {screen_id} rectangle has {pixels.size} "
                        f"pixels, expected {rect.width * rect.height}"
                    )
                tile = pixels.reshape(rect.height, rect.width)
                x0 = (
                    panel.rotated_width - rect.x - rect.width if mirror else rect.x
                )
                y0 = screen_id * band_h + rect.y
                canvas[y0 : y0 + rect.height, x0 : x0 + rect.width] = (
                    mirror_image(tile) if mirror else tile
                )
                covered.append((x0, y0, rect.width, rect.height))

    area = sum(w * h for _x, _y, w, h in covered)
    return DecodedFrame(
        canvas=canvas,
        covered=tuple(covered),
        full=area >= panel.canvas_width * panel.canvas_height,
        screens=tuple(sorted(set(screens))),
    )
