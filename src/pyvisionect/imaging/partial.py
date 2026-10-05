"""Screen-space partial updates: dirty-region tracking for the interlaced sign.

**Opt-in, per-device, and not vendor-sanctioned.**  The vendor's server never
sends ``HardwareNameID 8`` a partial rectangle -- ``getRectangleSupport``
returns false for it unconditionally (``vss/cmd/engine/client.go:41``) -- so
there is no vendor behaviour here to match and none to fall back on.  What
there is instead is a hand-run experiment on one physical sign: 17 pushes, 17
acks, zero NACKs, the firmware echoing our rectangle headers back verbatim, and
the device's ``DisplayStateCRC`` equal to our computed state checksum every
time.  See ``OPEN-QUESTIONS.md`` A10.

What it buys
------------
**Bytes and encode time.  Not latency.**  Measured on the sign from the
firmware's own ``Profiling:`` line:

====================================  ===============  ===========  ==========
push                                  wire (``Pv2Len``)  raw bytes   ``EpdUpd``
====================================  ===============  ===========  ==========
full screen, steady state                      77 613   1 843 200     2 916 ms
``2880 x 128`` strip                            2 143     184 320     2 908 ms
``256 x 128`` block                               292      16 384     2 904 ms
====================================  ===============  ===========  ==========

The wire cost collapses by ~250x; the panel time does not move.  ``EpdUpd`` has
a ~2.9 s floor on this panel whatever the rectangle's area.  So this module is
worth having on a battery device over wifi, and for a Raspberry Pi that would
otherwise dither, pack, interlace and LZ4 1.84 MB every tick.  It will **not**
make the glass refresh faster, and nothing here should be read as promising
that.

Why a canvas rectangle cannot be sent on its own
------------------------------------------------
The sign has **2 physical screens of 2880 x 640**; the session canvas is
**1440 x 2560** in 4 bands.  ``ScreenID 0`` is the 4-pixel interleave of bands
0 and 1, ``ScreenID 1`` of bands **3 and 2** -- note the swap
(:data:`~pyvisionect.imaging.interlace.INTERLACE_PAIRS`).  A band's pixels are
therefore interleaved with its partner's, and *any* screen-space rectangle
covering some of one band necessarily covers the matching rows of the other.

There is no way to say "only this lane" on the wire.  So a partial update
carries the partner lane's **current, unchanged** pixels alongside the changed
ones, read back out of the state image.  That is a flat 2x on payload, it is
structural, and it is not a bug to optimise away -- A10 verified it works and
leaves the partner band visually untouched.

The mapping, end to end::

    canvas rect -> per band: band-local rows, canvas column range
                -> lane column range   (canvas col c <-> lane col 1439-c, the
                                        eink-flip mirror)
                -> snap lane columns out to the 4-px interleave group
                -> screen rect: x = 2*c0, w = 2*(c1-c0), y and h unchanged
                -> cut BOTH lanes of that screen from the new state image,
                   mirror, pack, interleave
                -> one EncodedRect, screen-addressed, with that ScreenID

Ghosting
--------
E-ink accumulates ghosting under partial updates, and the vendor caps
consecutive partials at ``noFullUpdateMax = 10`` precisely for that
(``client.go:217``).  The firmware enforces no such cap of its own -- twelve
back-to-back partials all drew without a forced refresh -- so the policy is
ours to keep.  :class:`PartialPolicy` keeps it, defaulting to the vendor's 10,
and :class:`DirtyTracker` counts for you.  B1 adds the reason the full-screen
push is the right place to spend it: with the shipped ``RectangleFlags = 0``
the ``Options`` "normal update" bit is clear, so the firmware runs its
**inverse clearing waveform** on every full-screen push (``inv: 1`` ->
``UPD_FULL``).  A tracker that never forces a full refresh never clears, and
the panel slowly turns to mush.

A partial may not also ask for the clearing waveform
---------------------------------------------------
``RectangleHeader.Options`` bit ``0x0002`` **clear** means "inverse update"
(``OPEN-QUESTIONS.md`` B1), and the vendor ships ``RectangleFlags = 0``, so the
bit is clear on an ordinary push and the firmware runs its clearing waveform.
On a rectangle smaller than the screen the firmware refuses outright::

    l: (0 1168 160 512 128), enc: 0x4 pde: 0x0, te: 0x0
    u: (0 1168 160 512 128), wfn: 2, dum: 1, inv: 1
    Image download completed (0 0)
    Skip image update: partial image not allowed
    -> NACK, ErrorCode 0x06000000

Which is reasonable: an inverse clearing refresh drives the whole panel, so
"clear only this rectangle" is not a thing the waveform can do.  Measured on
the sign on 2026-10-05; with bit ``0x0002`` **set** the same rectangle acks and
draws.  So :func:`encode_partial_frame` **always sets the bit on a partial
rectangle**, whatever ``rect_options`` says, and leaves ``rect_options``
untouched on the full-screen fallback -- which is where you *want* the clearing
waveform, and is what makes the ghosting budget work.

Checksums
---------
``ImageHeader.Checksum`` is XXHash32 over the **whole 8-bit canvas state**, not
over the rectangle.  A partial that does not fold its own pixels into the state
image before hashing will disagree with the device's echoed
``DisplayStateCRC`` forever, and the vendor's own recovery for that is a
redundant full-screen redraw.  :func:`encode_partial_frame` updates the state
and hashes the whole canvas, exactly as a full push does.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Optional, Sequence

import numpy as np

from .checksum import state_checksum
from .constants import (MAX_NO_FULL_UPDATE, MAX_REGIONS_PER_DISPLAY,
                        OPTION_NORMAL_UPDATE,
                        RECTANGLE_UPDATE_OPTIONS_DEFAULT, Dithering,
                        ImageType)
from .encoder import EncodedFrame, EncodedRect, FrameState, _validate
from .encoder import encode_frame as _encode_frame
from .geometry import mirror_image
from .grey import beautify as _beautify
from .grey import to_grey8
from .interlace import (INTERLACE_GROUP, SCREEN_X_QUANTUM, displays_of_screen,
                        interlace, lane_of_display, lane_span)
from .dither import is_pixel_local as _is_pixel_local
from .dither import quantise as _quantise
from .pack import pack as _pack
from .pack import payload_size as _payload_size
from .panel import Panel
from .rects import (Rect, detect_changes, is_aligned, limit_regions,
                    unite_overlapping_regions)

__all__ = [
    "PartialPolicy",
    "DirtyTracker",
    "encode_partial_frame",
    "encode_screen_rect",
    "screen_bounds",
    "screen_rects_for_canvas_rect",
    "canvas_regions_for_screen_rect",
    "align_screen_rect",
    "align_and_unite_screen_rects",
    "validate_screen_rect",
    "partial_cost",
    "full_screen_cost",
]

#: Bytes of ``RectangleHeader`` in front of every rectangle's payload
#: (spec §1.1).  Counted into the cost comparison so that "many small
#: rectangles" is not free.
RECTANGLE_HEADER_BYTES = 24


# --------------------------------------------------------------------------
# policy
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class PartialPolicy:
    """When to give up on a partial and push the whole screen instead.

    :param max_consecutive_partials: force a full-screen push once this many
        partials have gone out since the last full one.  The default is the
        vendor's ``noFullUpdateMax`` (``client.go:217``).  This is the
        **ghosting budget** and the full-screen push is what spends it: with
        the shipped ``RectangleFlags = 0`` the firmware runs its inverse
        clearing waveform on every full-screen update (``OPEN-QUESTIONS.md``
        B1).  Set it to ``0`` to disable partials entirely; set it to a
        negative number to never force a refresh, which is a decision about
        your panel's lifetime and not a thing to do casually.
    :param max_cost_fraction: fall back when the partial's wire cost (payloads
        plus rectangle headers, *including* the 2x partner-lane cost) reaches
        this fraction of a full-screen push's.  Many scattered rectangles cost
        more than one full push; past roughly half there is nothing left to
        win and a full push also clears ghosting for free.
    """

    max_consecutive_partials: int = MAX_NO_FULL_UPDATE
    max_cost_fraction: float = 0.5

    def __post_init__(self) -> None:
        if not 0.0 < self.max_cost_fraction <= 1.0:
            raise ValueError(
                f"max_cost_fraction must be in (0, 1], got "
                f"{self.max_cost_fraction!r}"
            )

    def refresh_due(self, consecutive_partials: int) -> bool:
        """Whether the ghosting counter has tripped."""
        if self.max_consecutive_partials < 0:
            return False
        return consecutive_partials >= self.max_consecutive_partials


# --------------------------------------------------------------------------
# screen-space geometry
# --------------------------------------------------------------------------

def screen_bounds(panel: Panel) -> Rect:
    """The in-bounds region for a screen-space rectangle on this panel.

    ``2880 x 640`` on the 32" sign.  Rectangles are validated against this and
    **refused** rather than clamped: whether the firmware clamps, refuses, or
    writes past its framebuffer on an out-of-bounds rectangle is deliberately
    unmeasured (``OPEN-QUESTIONS.md`` A10, "Still unknown"), and probing a
    memory-safety edge on hardware in daily use was not worth it.
    """
    panel = Panel.adapt(panel)
    return Rect(0, 0, panel.screen_width, panel.screen_height)


def validate_screen_rect(screen_id: int, rect: Rect, panel: Panel,
                         encoding: int) -> None:
    """Refuse a screen rectangle the device could not safely be sent.

    :raises ValueError: if the panel has no such ``ScreenID``, if the
        rectangle leaves :func:`screen_bounds`, if its ``x``/``width`` are not
        multiples of :data:`~pyvisionect.imaging.interlace.SCREEN_X_QUANTUM`,
        or if ``width * height`` does not divide the 16-bit quantum.
    """
    panel = Panel.adapt(panel)
    if not panel.supports_screen_rectangles:
        raise ValueError(
            f"panel {panel.width}x{panel.height}x{panel.displays} "
            f"(interlace mode {panel.interlace_mode}, rotation "
            f"{panel.rotation}) cannot address screen-space rectangles"
        )
    displays_of_screen(panel.interlace_mode, int(screen_id))
    bounds = screen_bounds(panel)
    if rect.is_empty:
        raise ValueError(f"empty screen rectangle {rect!r}")
    if (rect.x < 0 or rect.y < 0 or rect.far_x > bounds.far_x
            or rect.far_y > bounds.far_y):
        raise ValueError(
            f"screen rectangle {rect!r} leaves the panel bounds {bounds!r}; "
            "refusing to send it -- out-of-bounds rectangles were never "
            "probed on hardware and the firmware's reaction is unknown"
        )
    if rect.x % SCREEN_X_QUANTUM or rect.width % SCREEN_X_QUANTUM:
        raise ValueError(
            f"screen rectangle {rect!r} needs x and width to be multiples of "
            f"{SCREEN_X_QUANTUM} so the interleave lanes stay paired"
        )
    if not is_aligned(rect, encoding):
        raise ValueError(
            f"screen rectangle {rect!r} has width*height = "
            f"{rect.width * rect.height}, which does not divide the 16-bit "
            f"quantum for encoding {int(encoding)} (spec §2.4)"
        )


def align_screen_rect(rect: Rect, encoding: int, bounds: Rect) -> Rect:
    """Grow a screen rectangle until it is legal, inside ``bounds``.

    Two constraints, in order:

    1. ``x`` and ``width`` snap out to multiples of
       :data:`~pyvisionect.imaging.interlace.SCREEN_X_QUANTUM` (8), so the
       rectangle starts on an even interleave group and ends on a whole pair.
    2. ``width * height`` must then divide the 16-bit quantum
       (:func:`~pyvisionect.imaging.rects.is_aligned`).  At 4 bpp step 1
       already guarantees it.  At 1 bpp the quantum is 16 sub-pixels, so an
       **odd height** additionally needs ``width`` to be a multiple of 16; one
       more 8-pixel growth always supplies that.

    Growth prefers to extend right, falling back to left -- the same
    [INFERRED] order :func:`~pyvisionect.imaging.rects.align_to_quantum` uses
    for canvas rectangles, kept the same so the two cannot drift.

    :raises ValueError: if no legal rectangle containing ``rect`` fits in
        ``bounds``.
    """
    clipped = rect.intersection(bounds)
    if clipped is None:
        raise ValueError(f"screen rectangle {rect!r} is outside {bounds!r}")
    x0 = clipped.x - (clipped.x - bounds.x) % SCREEN_X_QUANTUM
    far = clipped.far_x + -(clipped.far_x - bounds.x) % SCREEN_X_QUANTUM
    far = min(far, bounds.far_x)
    out = Rect(x0, clipped.y, far - x0, clipped.height)
    while not is_aligned(out, encoding):
        if out.far_x + SCREEN_X_QUANTUM <= bounds.far_x:
            out = replace(out, width=out.width + SCREEN_X_QUANTUM)
        elif out.x - SCREEN_X_QUANTUM >= bounds.x:
            out = Rect(out.x - SCREEN_X_QUANTUM, out.y,
                       out.width + SCREEN_X_QUANTUM, out.height)
        else:
            raise ValueError(
                f"screen rectangle {rect!r} cannot be aligned to the 16-bit "
                f"quantum for encoding {int(encoding)} inside {bounds!r}"
            )
    return out


def align_and_unite_screen_rects(rects: Sequence[Rect], encoding: int,
                                 bounds: Rect) -> list[Rect]:
    """Align every screen rectangle, then re-unite the overlaps that created.

    Growing two neighbours to the quantum can make them overlap, and sending
    the same pixels twice in one packet is at best wasteful.  Uniting can in
    turn un-align the result -- a union's height is neither input's -- so this
    iterates to a fixpoint.  It converges in one or two passes in practice,
    and the bound is here so a pathological input cannot spin.

    :raises ValueError: if a rectangle cannot be aligned inside ``bounds``, or
        if align/unite does not settle.
    """
    out = list(rects)
    for _ in range(8):
        aligned = [align_screen_rect(r, encoding, bounds) for r in out]
        united = unite_overlapping_regions(aligned, area_criteria=False)
        if united == aligned:
            return aligned
        out = united
    raise ValueError(
        f"screen rectangles {list(rects)!r} did not settle into an aligned, "
        f"non-overlapping set inside {bounds!r}"
    )


def screen_rects_for_canvas_rect(
    rect: Rect, panel: Panel
) -> list[tuple[int, Rect]]:
    """Map one canvas rectangle to the screen rectangles that cover it.

    One per band the input touches, so a rectangle straddling a band seam
    yields two (and one spanning the whole canvas yields four, two per
    ``ScreenID``).  The returned rectangles are **not** merged or aligned;
    :func:`encode_partial_frame` does both.

    Each result covers more than it was asked to: the partner lane's matching
    rows, always, and up to 3 extra lane columns on each side from the
    interleave-group snap.  Those pixels are re-sent unchanged, which is why
    the caller must cut them from the state image and not from thin air.

    :returns: ``[(screen_id, screen_rect), ...]``, in band order.
    """
    panel = Panel.adapt(panel)
    if not panel.supports_screen_rectangles:
        raise ValueError(
            f"panel with interlacing mode {panel.interlace_mode} and rotation "
            f"{panel.rotation} has no screen-space addressing"
        )
    mirror = bool(panel.driver.mirror)  # type: ignore[union-attr]
    band_h = panel.rotated_height
    lane_w = panel.rotated_width
    out: list[tuple[int, Rect]] = []
    for display in range(panel.displays):
        band = Rect(0, display * band_h, lane_w, band_h)
        part = rect.intersection(band)
        if part is None:
            continue
        screen_id, _lane = lane_of_display(panel.interlace_mode, display)
        # canvas columns -> lane columns (the eink-flip mirror is applied to
        # the whole display row, so it reverses the column order)
        if mirror:
            c0, c1 = lane_w - part.far_x, lane_w - part.x
        else:
            c0, c1 = part.x, part.far_x
        # snap out to whole interleave groups so lane_span() accepts it
        c0 -= c0 % INTERLACE_GROUP
        c1 += -c1 % INTERLACE_GROUP
        c1 = min(c1, lane_w)
        sx, sw = c0 * 2, (c1 - c0) * 2
        out.append((screen_id, Rect(sx, part.y - display * band_h, sw,
                                    part.height)))
    return out


def canvas_regions_for_screen_rect(
    screen_id: int, rect: Rect, panel: Panel
) -> tuple[tuple[int, Rect], ...]:
    """Inverse of :func:`screen_rects_for_canvas_rect`, for one screen rect.

    :returns: ``((display, canvas_rect), (display, canvas_rect))`` -- always
        exactly two, lane A first, because a screen rectangle always lands on
        both of its screen's bands.
    """
    panel = Panel.adapt(panel)
    lanes = displays_of_screen(panel.interlace_mode, int(screen_id))
    c0, c1 = lane_span(rect.x, rect.width)
    lane_w = panel.rotated_width
    band_h = panel.rotated_height
    if bool(panel.driver.mirror):  # type: ignore[union-attr]
        x0, x1 = lane_w - c1, lane_w - c0
    else:
        x0, x1 = c0, c1
    return tuple(
        (display, Rect(x0, display * band_h + rect.y, x1 - x0, rect.height))
        for display in lanes
    )


# --------------------------------------------------------------------------
# cost accounting
# --------------------------------------------------------------------------

def full_screen_cost(panel: Panel, encoding: int) -> int:
    """Wire bytes of a full-screen push: payloads plus rectangle headers.

    ``2 x (24 + 921600) = 1 843 248`` for the 32" sign at 4 bpp.  This is the
    *uncompressed* figure; LZ4 shrinks both sides of the comparison and does
    not shrink them equally, but a partial that is already a fraction of the
    full raw size is a fraction of the compressed size too.
    """
    panel = Panel.adapt(panel)
    per_screen = _payload_size(panel.screen_width, panel.screen_height,
                                    encoding)
    return panel.screens * (RECTANGLE_HEADER_BYTES + per_screen)


def partial_cost(rects: Sequence[EncodedRect]) -> int:
    """Wire bytes of a rectangle list: payloads plus rectangle headers."""
    return sum(RECTANGLE_HEADER_BYTES + len(r.data) for r in rects)


# --------------------------------------------------------------------------
# payload construction
# --------------------------------------------------------------------------

def encode_screen_rect(
    state_grey: np.ndarray,
    *,
    screen_id: int,
    rect: Rect,
    panel: Panel,
    encoding: int,
    options: int = 0,
    image_type: int = int(ImageType.GRAY),
    rectangle_update_options: Optional[int] = None,
    dithering: int = int(Dithering.NONE),
) -> EncodedRect:
    """Build one screen-addressed :class:`~.encoder.EncodedRect` from a state image.

    Both lanes of ``screen_id`` are cut from ``state_grey`` at the same
    band-local rows and lane columns, mirrored if the driver mirrors, packed,
    and interleaved.  Pixels that are not changing are therefore re-sent as
    they already are -- which is exactly what the partner lane needs.

    ``state_grey`` must already hold the pixels you want on the glass, on the
    output ramp (``n * 17`` at 4 bpp, ``0``/``255`` at 1 bpp).  Packing is
    idempotent on that ramp, so a region cut from the state re-encodes to the
    same bytes a full push would have carried for it.

    :param state_grey: the full ``(canvas_height, canvas_width)`` 8-bit state.
    :raises ValueError: via :func:`validate_screen_rect`, or if the resulting
        payload is not the length the geometry implies (a library bug, not a
        caller error).
    """
    panel = Panel.adapt(panel)
    enc = int(encoding)
    validate_screen_rect(screen_id, rect, panel, enc)
    state = np.asarray(state_grey, dtype=np.uint8)
    if state.shape != panel.canvas_shape:
        raise ValueError(
            f"state image is {state.shape[1]}x{state.shape[0]} but panel "
            f"canvas is {panel.canvas_width}x{panel.canvas_height}"
        )
    mirror = bool(panel.driver.mirror)  # type: ignore[union-attr]
    c0, c1 = lane_span(rect.x, rect.width)
    band_h = panel.rotated_height
    halves: list[bytes] = []
    for display in displays_of_screen(panel.interlace_mode, int(screen_id)):
        y0 = display * band_h + rect.y
        rows = state[y0:y0 + rect.height]
        lane = mirror_image(rows) if mirror else rows
        halves.append(_pack(np.ascontiguousarray(lane[:, c0:c1]), enc))
    data = interlace(halves[0], halves[1], enc)
    want = _payload_size(rect.width, rect.height, enc)
    if len(data) != want:
        raise ValueError(
            f"interlaced payload for {rect!r} is {len(data)} bytes, geometry "
            f"implies {want}; this is a bug in pyvisionect, not in your call"
        )
    ruo = (RECTANGLE_UPDATE_OPTIONS_DEFAULT[enc]
           if rectangle_update_options is None else int(rectangle_update_options))
    return EncodedRect(
        x=rect.x, y=rect.y, w=rect.width, h=rect.height,
        screen_id=int(screen_id), options=int(options), data=data,
        encoding=enc, image_type=int(image_type),
        rectangle_update_options=ruo, dithering=int(dithering),
    )


def _apply_to_state(state: np.ndarray, grey: np.ndarray,
                    dirty: Sequence[Rect], panel: Panel, enc: int, dith: int,
                    *, quant_canvas: Optional[np.ndarray],
                    dither_levels: Optional[int]) -> None:
    """Fold the dirty regions' new pixels into the state image, in place.

    Mirrors :func:`~.encoder.encode_frame`'s own state bookkeeping: for a
    pixel-local dither the quantisation is phased on the canvas, so a region's
    pixels are identical whether it is encoded alone or inside a bigger
    rectangle -- that is what keeps partials from seaming.  Floyd-Steinberg is
    not pixel-local, so it is run per band-slice on the mirrored tile, the way
    a full push runs it per display, and a partial *will* seam against its
    neighbours under it.
    """
    mirror = bool(panel.driver.mirror)  # type: ignore[union-attr]
    band_h = panel.rotated_height
    lane_w = panel.rotated_width
    for rect in dirty:
        for display in range(panel.displays):
            band = Rect(0, display * band_h, lane_w, band_h)
            part = rect.intersection(band)
            if part is None:
                continue
            rows, cols = part.as_slice()
            if quant_canvas is not None:
                state[rows, cols] = quant_canvas[rows, cols]
                continue
            tile = grey[rows, cols]
            if mirror:
                tile = mirror_image(tile)
            tile = _quantise(tile, enc, dith, levels=dither_levels)
            state[rows, cols] = mirror_image(tile) if mirror else tile


# --------------------------------------------------------------------------
# the encoder
# --------------------------------------------------------------------------

def encode_partial_frame(
    img: Any,
    *,
    panel: Panel,
    encoding: int,
    dithering: int,
    prev_state: Optional[FrameState],
    rects: Optional[Sequence[Rect]] = None,
    policy: Optional[PartialPolicy] = None,
    consecutive_partials: int = 0,
    inverse: bool = False,
    rect_options: int = 0,
    image_type: int = int(ImageType.GRAY),
    rectangle_update_options: Optional[int] = None,
    change_threshold: float = 0.0,
    merge_regions: bool = True,
    max_regions: int = MAX_REGIONS_PER_DISPLAY,
    force_full_screen: bool = False,
    beautify: bool = False,
    beautify_gamma: float = 1.1,
    bayer_matrix: Optional[np.ndarray] = None,
    blue_noise_matrix: Optional[np.ndarray] = None,
    dither_levels: Optional[int] = None,
) -> EncodedFrame:
    """Encode only what changed, as screen-space rectangles.

    Same contract as :func:`~.encoder.encode_frame` -- image in, wire-ready
    rectangles and a state checksum out, no I/O, no clock, pure in its
    arguments -- except that the rectangles are addressed in **screen**
    coordinates and cover only the dirty region plus its partner lane.

    Falls back to a full-screen push, with
    :attr:`~.encoder.EncodedFrame.fallback_reason` set, whenever a partial is
    not possible or not worth it:

    ================================  ==========================================
    ``fallback_reason``               cause
    ================================  ==========================================
    ``"no-previous-state"``           nothing to diff against, and nothing known
                                      about what the glass currently shows
    ``"forced"``                      ``force_full_screen=True``
    ``"inverse"``                     ``inverse=True``; the clearing waveform is
                                      only legal full-screen (spec §1.4)
    ``"re-encode"``                   encoding or dithering changed, so every
                                      pixel on the panel is stale
    ``"panel-unsupported"``           the panel has no screen-space addressing
                                      (:attr:`~.panel.Panel.supports_screen_rectangles`)
    ``"ghosting-refresh-due"``        the consecutive-partial budget tripped
    ``"too-many-regions"``            more dirty regions than merging allows
    ``"not-worth-it"``                the partial would cost more than
                                      ``policy.max_cost_fraction`` of a full push
    ================================  ==========================================

    On a panel with no interlacing (``interlace_mode == 0``) there is no fold
    to work around, a canvas rectangle *is* a wire rectangle, and this
    function delegates straight to :func:`~.encoder.encode_frame` with the
    detected dirty rectangles -- so the ghosting policy and the dirty tracking
    are usable there too.  ``screen_space`` comes back False in that case.

    .. warning:: **Verified on one device, by hand, and not vendor-sanctioned.**
       Partial rectangles were measured working on ``HardwareNameID 8``-class
       hardware only -- the 31.2" Place&Play 32, firmware 7.4.4407, hardware
       revision 1.1.0 (``OPEN-QUESTIONS.md`` A10).  The vendor's server never
       sends this hardware a partial, so this path has no vendor
       implementation behind it and no vendor support if it misbehaves.  It
       saves **bytes and encode time, not refresh latency**: the panel's
       waveform floor is ~2.9 s regardless of area.

    .. warning:: **CPU-bound and blocking**, for the same reasons
       :func:`~.encoder.encode_frame` is.  Never call it on an asyncio event
       loop.  It is cheaper than a full push -- it dithers and packs a
       fraction of the canvas -- but change detection still runs over the
       whole thing.

    :param prev_state: the :class:`~.encoder.FrameState` from the previous
        call.  Required: without it there is no partner-lane content to hold
        and no model of what the glass shows.  ``None`` falls back to full
        screen rather than raising.
    :param rects: dirty rectangles in **canvas** coordinates, if you already
        know them.  ``None`` means change-detect against ``prev_state``.
    :param rect_options: ``RectangleHeader.Options``.  On a partial rectangle
        the "normal update" bit :data:`~.constants.OPTION_NORMAL_UPDATE` is
        **always added**, because the firmware refuses a partial that also
        asks for the inverse clearing waveform (see the module docstring).  On
        the full-screen fallback it is passed through untouched.
    :param policy: a :class:`PartialPolicy`; ``None`` uses the defaults.
    :param consecutive_partials: partials pushed since the last full-screen
        frame.  :class:`DirtyTracker` keeps this for you.

    :returns: an :class:`~.encoder.EncodedFrame`.  ``rectangles`` is empty when
        nothing changed; ``screen_space`` is True on a real partial.
    """
    panel = Panel.adapt(panel)
    policy = policy or PartialPolicy()
    enc, dith = _validate(encoding, dithering, panel)

    def full(reason: str) -> EncodedFrame:
        frame = _encode_frame(
            img, panel=panel, encoding=enc, dithering=dith, rects=None,
            prev_state=prev_state, inverse=inverse, rect_options=rect_options,
            image_type=image_type,
            rectangle_update_options=rectangle_update_options,
            change_threshold=change_threshold, merge_regions=merge_regions,
            max_regions=max_regions, force_full_screen=True,
            beautify=beautify, beautify_gamma=beautify_gamma,
            bayer_matrix=bayer_matrix, blue_noise_matrix=blue_noise_matrix,
            dither_levels=dither_levels,
        )
        frame.fallback_reason = reason
        return frame

    # ---- structural gates, cheapest first ------------------------------
    if force_full_screen:
        return full("forced")
    if inverse:
        return full("inverse")
    if prev_state is None:
        return full("no-previous-state")
    if prev_state.encoding != enc or prev_state.dithering != dith:
        return full("re-encode")
    if policy.refresh_due(consecutive_partials):
        return full("ghosting-refresh-due")
    if panel.interlace_mode and not panel.supports_screen_rectangles:
        return full("panel-unsupported")

    grey = to_grey8(img)
    if grey.shape != panel.canvas_shape:
        raise ValueError(
            f"image is {grey.shape[1]}x{grey.shape[0]} but panel canvas is "
            f"{panel.canvas_width}x{panel.canvas_height} "
            f"({panel.displays} displays of {panel.width}x{panel.height})"
        )
    if beautify:
        grey = _beautify(grey, gamma=beautify_gamma)

    canvas = Rect(0, 0, panel.canvas_width, panel.canvas_height)
    if rects is not None:
        dirty = [r for r in (c.intersection(canvas) for c in rects)
                 if r is not None]
    else:
        dirty = detect_changes(grey, prev_state.source_grey, change_threshold)
    dirty = unite_overlapping_regions(dirty, area_criteria=True)

    if not dirty:
        return EncodedFrame(
            rectangles=[],
            state_checksum=prev_state.checksum,
            state=FrameState(prev_state.state_grey, grey, prev_state.checksum,
                             enc, dith),
            full_screen=False,
            screen_space=bool(panel.interlace_mode),
            dirty_rects=(),
        )

    # ---- no fold: a canvas rectangle is already a wire rectangle --------
    if not panel.interlace_mode:
        frame = _encode_frame(
            img, panel=panel, encoding=enc, dithering=dith, rects=dirty,
            prev_state=prev_state, inverse=False,
            rect_options=int(rect_options) | OPTION_NORMAL_UPDATE,
            image_type=image_type,
            rectangle_update_options=rectangle_update_options,
            change_threshold=change_threshold, merge_regions=merge_regions,
            max_regions=max_regions, beautify=beautify,
            beautify_gamma=beautify_gamma, bayer_matrix=bayer_matrix,
            blue_noise_matrix=blue_noise_matrix, dither_levels=dither_levels,
        )
        frame.dirty_rects = tuple(dirty)
        if frame.full_screen and frame.fallback_reason is None:
            frame.fallback_reason = "too-many-regions"
        if partial_cost(frame.rectangles) >= (
            policy.max_cost_fraction * full_screen_cost(panel, enc)
        ) and not frame.full_screen:
            return full("not-worth-it")
        return frame

    # ---- canvas rects -> screen rects ----------------------------------
    bounds = screen_bounds(panel)
    per_screen: dict[int, list[Rect]] = {}
    for rect in dirty:
        for screen_id, screen_rect in screen_rects_for_canvas_rect(rect, panel):
            per_screen.setdefault(screen_id, []).append(screen_rect)

    merged: dict[int, list[Rect]] = {}
    for screen_id, screen_rects in sorted(per_screen.items()):
        limited = limit_regions(screen_rects, bounds.width, bounds.height,
                                merge=merge_regions, max_regions=max_regions)
        if limited is None:
            return full("too-many-regions")
        # alignment can make two rectangles overlap, and uniting can un-align
        # the result, so this iterates (see align_and_unite_screen_rects)
        merged[screen_id] = align_and_unite_screen_rects(limited, enc, bounds)
        if len(merged[screen_id]) > max_regions:
            return full("too-many-regions")

    # ---- new state image, then cut the rectangles out of it -------------
    pixel_local = _is_pixel_local(dith)
    quant_canvas: Optional[np.ndarray] = None
    if pixel_local:
        quant_canvas = _quantise(
            grey, enc, dith, origin=(0, 0), bayer=bayer_matrix,
            blue_noise=blue_noise_matrix, levels=dither_levels,
        )
    if prev_state.state_grey.shape != panel.canvas_shape:
        raise ValueError(
            f"prev_state's state image is {prev_state.state_grey.shape[1]}x"
            f"{prev_state.state_grey.shape[0]} but panel canvas is "
            f"{panel.canvas_width}x{panel.canvas_height}"
        )
    state = prev_state.state_grey.copy()
    _apply_to_state(state, grey, dirty, panel, enc, dith,
                    quant_canvas=quant_canvas, dither_levels=dither_levels)

    # A partial that also requests the inverse clearing waveform is refused by
    # the firmware ("Skip image update: partial image not allowed"), so the
    # "normal update" bit is set here regardless of rect_options.  The
    # full-screen fallback above keeps rect_options as given, which is what
    # spends the ghosting budget on a clearing refresh.
    options = int(rect_options) | OPTION_NORMAL_UPDATE
    out: list[EncodedRect] = []
    for screen_id, screen_rects in sorted(merged.items()):
        for rect in screen_rects:
            out.append(encode_screen_rect(
                state, screen_id=screen_id, rect=rect, panel=panel,
                encoding=enc, options=options, image_type=int(image_type),
                rectangle_update_options=rectangle_update_options,
                dithering=dith,
            ))

    if partial_cost(out) >= policy.max_cost_fraction * full_screen_cost(panel, enc):
        return full("not-worth-it")

    checksum = state_checksum(np.ascontiguousarray(state).tobytes())
    return EncodedFrame(
        rectangles=out,
        state_checksum=checksum,
        state=FrameState(state, grey, checksum, enc, dith),
        full_screen=False,
        screen_space=True,
        dirty_rects=tuple(dirty),
    )


# --------------------------------------------------------------------------
# the tracker
# --------------------------------------------------------------------------

class DirtyTracker:
    """Successive frames in, partial updates out, with the ghosting budget kept.

    The one piece of :mod:`pyvisionect.imaging` that is deliberately
    **stateful**: it holds the :class:`~.encoder.FrameState` and the
    consecutive-partial counter so the caller does not have to thread them
    through every call.  Everything it does is still a pure function of that
    state plus the new image -- there is no clock and no I/O.

    ::

        tracker = DirtyTracker(panel=PANEL_32INCH, encoding=4, dithering=1)
        frame = tracker.update(img)            # first frame: full screen
        frame = tracker.update(img2)           # then partials
        if frame.rectangles:
            conn.push_image(frame)             # your transport

    Push what comes back, in order, and nothing else: a frame with no
    rectangles means the panel already shows this image and the right action is
    none.  If a push **fails**, call :meth:`rollback` -- the tracker's state
    image claims the device drew something it did not, and the next partial
    would hold the wrong partner-lane pixels.

    :param panel: anything :meth:`~.panel.Panel.adapt` accepts.
    :param encoding: ``1`` or ``4``.
    :param dithering: a :class:`~.constants.Dithering` value.
    :param policy: a :class:`PartialPolicy`, or ``None`` for the defaults.
    :param state: a :class:`~.encoder.FrameState` to resume from, e.g. one
        restored with :meth:`~.encoder.FrameState.from_bytes` after a restart.
        E-ink keeps its image, so resuming is the difference between a free
        restart and a full-screen push plus a unit of panel wear.
    :param consecutive_partials: the counter to resume with.  Persist it beside
        the state, or pass nothing and spend one extra full refresh.
    """

    def __init__(
        self,
        *,
        panel: Panel,
        encoding: int,
        dithering: int,
        policy: Optional[PartialPolicy] = None,
        state: Optional[FrameState] = None,
        consecutive_partials: int = 0,
        rect_options: int = 0,
        image_type: int = int(ImageType.GRAY),
        rectangle_update_options: Optional[int] = None,
        change_threshold: float = 0.0,
        merge_regions: bool = True,
        max_regions: int = MAX_REGIONS_PER_DISPLAY,
        beautify: bool = False,
        beautify_gamma: float = 1.1,
        bayer_matrix: Optional[np.ndarray] = None,
        blue_noise_matrix: Optional[np.ndarray] = None,
        dither_levels: Optional[int] = None,
    ) -> None:
        self.panel = Panel.adapt(panel)
        self.encoding, self.dithering = _validate(encoding, dithering,
                                                  self.panel)
        self.policy = policy or PartialPolicy()
        self.state = state
        if consecutive_partials < 0:
            raise ValueError("consecutive_partials cannot be negative")
        self._consecutive = int(consecutive_partials)
        self._previous: Optional[FrameState] = None
        self._previous_consecutive = self._consecutive
        self._kwargs = dict(
            rect_options=rect_options, image_type=image_type,
            rectangle_update_options=rectangle_update_options,
            change_threshold=change_threshold, merge_regions=merge_regions,
            max_regions=max_regions, beautify=beautify,
            beautify_gamma=beautify_gamma, bayer_matrix=bayer_matrix,
            blue_noise_matrix=blue_noise_matrix, dither_levels=dither_levels,
        )

    # -- the budget -------------------------------------------------------

    @property
    def consecutive_partials(self) -> int:
        """Partials pushed since the last full-screen frame."""
        return self._consecutive

    @property
    def refresh_due(self) -> bool:
        """Whether the next :meth:`update` will be promoted to full screen.

        Only the ghosting budget; the cost and region gates depend on the
        image and cannot be answered before encoding it.
        """
        return self.policy.refresh_due(self._consecutive)

    # -- frames -----------------------------------------------------------

    def update(self, img: Any, *, rects: Optional[Sequence[Rect]] = None,
               force_full_screen: bool = False,
               inverse: bool = False) -> EncodedFrame:
        """Encode the next frame, partial if that is possible and worthwhile.

        The counter advances only for frames that **carry rectangles**: a
        no-change frame draws nothing, so it spends no ghosting budget, and a
        full-screen frame resets the counter to zero.

        :param rects: canvas-space dirty rectangles, if you know them.
        :param force_full_screen: skip change detection and repaint
            everything; also resets the ghosting counter.
        :param inverse: request the inverse / clearing refresh, which forces
            full screen (spec §1.4).
        """
        frame = encode_partial_frame(
            img, panel=self.panel, encoding=self.encoding,
            dithering=self.dithering, prev_state=self.state, rects=rects,
            policy=self.policy, consecutive_partials=self._consecutive,
            inverse=inverse, force_full_screen=force_full_screen,
            **self._kwargs,
        )
        self._previous = self.state
        self._previous_consecutive = self._consecutive
        self.state = frame.state
        if frame.rectangles:
            self._consecutive = 0 if frame.full_screen else self._consecutive + 1
        return frame

    def rollback(self) -> None:
        """Undo the last :meth:`update`, for a push that did not go out.

        The state image is the tracker's claim about what the glass shows.  If
        a push failed, that claim is wrong, and the next partial would hold
        stale partner-lane pixels and compute a checksum the device will never
        echo.  Only one step of history is kept, so roll back immediately or
        drop the state with :meth:`reset` and take one full push.
        """
        self.state = self._previous
        self._consecutive = self._previous_consecutive
        self._previous = None

    def reset(self, state: Optional[FrameState] = None) -> None:
        """Forget what the panel shows, so the next frame is a full push.

        Pass a ``state`` to adopt one instead, e.g. after restoring a
        persisted :class:`~.encoder.FrameState`.
        """
        self._previous = self.state
        self._previous_consecutive = self._consecutive
        self.state = state
        self._consecutive = 0
