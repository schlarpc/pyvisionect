"""Screen-space partial updates: the fold's sub-range, and the policy layer.

The claims under test, in rough order of how much they would cost to get wrong:

* a screen rectangle's payload is **byte-identical to the same window of the
  real captured full-screen payload** -- checked against the golden pcap
  fixture, so the mapping is pinned to device bytes and not to our own
  arithmetic;
* a partial leaves the encoder's state image and ``ImageHeader.Checksum``
  **exactly where a full push of the same image would have left them**, which
  is what keeps the device's echoed ``DisplayStateCRC`` matching;
* decoding a partial on top of the previous frame gives the same canvas as
  decoding a full push of the new image;
* the partner lane is carried unchanged, and the ``ScreenID 1`` lane swap is
  honoured;
* every fallback trigger fires, including the ghosting budget.
"""

from __future__ import annotations

import numpy as np
import pytest

from pyvisionect.imaging import (DRIVERS, INTERLACE_GROUP, MAX_NO_FULL_UPDATE,
                                 PANEL_32INCH, SCREEN_X_QUANTUM, DirtyTracker,
                                 Dithering, Encoding, Panel, PartialPolicy,
                                 Rect, align_and_unite_screen_rects,
                                 align_screen_rect,
                                 canvas_regions_for_screen_rect,
                                 decode_image_packet, deinterlace,
                                 displays_of_screen, encode_frame,
                                 encode_partial_frame, encode_screen_rect,
                                 full_screen_cost, is_aligned,
                                 lane_of_display, lane_span, partial_cost,
                                 screen_bounds,
                                 screen_rects_for_canvas_rect, screen_span,
                                 state_checksum, unpack,
                                 validate_screen_rect)

P = PANEL_32INCH
BAND = P.rotated_height          # 640
LANE_W = P.rotated_width         # 1440

#: Pixel-local dithers only.  Floyd-Steinberg is not pixel-local, so a partial
#: *cannot* reproduce a full push's pixels under it -- the error diffusion
#: starts somewhere else.  That is a documented property of the dither, not a
#: bug in the partial path, and it is asserted separately below.
PIXEL_LOCAL = [Dithering.NONE, Dithering.BAYER, Dithering.BLUE_NOISE]
ENCODINGS = [Encoding.ONE_BIT, Encoding.FOUR_BIT]

#: A non-interlacing panel, to check that the tracker is usable where there is
#: no fold to work around.
FLAT = Panel(width=64, height=32, displays=2, driver=DRIVERS["eink-generic"],
             interlace=0)


def levels(dithering, encoding) -> int | None:
    """``dither_levels`` that keeps 4 bpp Bayer from warning about bi-level.

    ``Dithering.BAYER`` at 4 bpp reproduces GraphicsMagick's bi-level
    degeneration and warns about it; the suite turns warnings into errors.
    Forcing 16 levels is the documented opt-out and does not change what this
    file is testing.
    """
    if dithering == Dithering.BAYER and int(encoding) == int(Encoding.FOUR_BIT):
        return 16
    return None


def canvas(panel: Panel = P, seed: int = 0) -> np.ndarray:
    y, x = np.mgrid[0:panel.canvas_height, 0:panel.canvas_width]
    v = 128 + 100 * np.sin((x + seed) / 37.0) * np.cos(y / 29.0) + 0.02 * x
    return np.clip(v, 0, 255).astype(np.uint8)


class _Rect:
    """``decode_image_packet``'s duck type, from an :class:`EncodedRect`."""

    def __init__(self, enc) -> None:
        self.screen_id, self.x, self.y = enc.screen_id, enc.x, enc.y
        self.width, self.height = enc.w, enc.h
        self.encoding, self.data = enc.encoding, enc.data


class _Packet:
    def __init__(self, rects) -> None:
        self.rectangles = [_Rect(r) for r in rects]


def decode(frame, panel: Panel = P):
    return decode_image_packet(_Packet(frame.rectangles), panel)


# ==========================================================================
# the fold restricted to a sub-range
# ==========================================================================

def test_lane_span_and_screen_span_are_inverses():
    for screen_x in range(0, 64, SCREEN_X_QUANTUM):
        for screen_w in range(SCREEN_X_QUANTUM, 64, SCREEN_X_QUANTUM):
            c0, c1 = lane_span(screen_x, screen_w)
            assert screen_span(c0, c1 - c0) == (screen_x, screen_w)
            assert c0 % INTERLACE_GROUP == 0
            assert (c1 - c0) % INTERLACE_GROUP == 0


@pytest.mark.parametrize("x,w", [(4, 8), (8, 4), (1, 8), (8, 12), (-8, 8)])
def test_lane_span_refuses_an_unpaired_span(x, w):
    with pytest.raises(ValueError):
        lane_span(x, w)


@pytest.mark.parametrize("x,w", [(2, 4), (4, 2), (-4, 4)])
def test_screen_span_refuses_a_part_group(x, w):
    with pytest.raises(ValueError):
        screen_span(x, w)


def test_lane_of_display_encodes_the_screen_1_swap():
    """Display 3 is screen 1's lane **A**, display 2 its lane B."""
    assert lane_of_display(2, 0) == (0, 0)
    assert lane_of_display(2, 1) == (0, 1)
    assert lane_of_display(2, 3) == (1, 0)
    assert lane_of_display(2, 2) == (1, 1)
    assert displays_of_screen(2, 0) == (0, 1)
    assert displays_of_screen(2, 1) == (3, 2)
    with pytest.raises(ValueError):
        lane_of_display(2, 4)
    with pytest.raises(ValueError):
        displays_of_screen(2, 2)


def test_mode_1_still_refuses_rather_than_guessing():
    with pytest.raises(NotImplementedError):
        lane_of_display(1, 0)


# ==========================================================================
# pinned to real device bytes
# ==========================================================================

@pytest.mark.parametrize("screen_id,sx,sy,sw,sh", [
    (0, 0, 256, 2880, 128),      # the full-width strip A10 pushed
    (1, 0, 256, 2880, 128),      # the same on the swapped screen
    (0, 1024, 448, 512, 160),    # A10's off-axis block
    (0, 1600, 96, 256, 128),     # the 292-byte block, twelve times over
    (0, 8, 0, 8, 4),             # the smallest legal rectangle, at the edge
    (1, 2872, 636, 8, 4),        # and at the far corner
])
def test_screen_rect_is_a_window_of_the_real_captured_payload(
    golden_canvas, golden_payloads, screen_id, sx, sy, sw, sh
):
    """The strongest statement available: the partial's bytes are device bytes.

    ``golden_payloads`` are the two ``2880x640`` payloads the **vendor's
    server** pushed to this sign, recovered from a pcap and pinned by
    committed SHA-256 digests.  If a partial rectangle cut from the same canvas
    is byte-identical to that payload's window, then the only thing left
    untested is whether the device honours a sub-rectangle -- which is what
    A10 measured on glass.
    """
    rect = Rect(sx, sy, sw, sh)
    state = ((golden_canvas >> 4) * 17).astype(np.uint8)
    encoded = encode_screen_rect(state, screen_id=screen_id, rect=rect,
                                 panel=P, encoding=4)
    stride = P.screen_width // 2          # bytes per screen row at 4 bpp
    reference = np.frombuffer(golden_payloads[screen_id], dtype=np.uint8)
    reference = reference.reshape(P.screen_height, stride)
    want = reference[sy:sy + sh, sx // 2:(sx + sw) // 2]
    got = np.frombuffer(encoded.data, dtype=np.uint8).reshape(sh, sw // 2)
    assert np.array_equal(want, got)
    assert (encoded.x, encoded.y, encoded.w, encoded.h) == (sx, sy, sw, sh)
    assert encoded.screen_id == screen_id
    assert encoded.rectangle_update_options == 0x0102


def test_golden_canvas_is_already_on_the_ramp(golden_canvas):
    """The cut-from-state trick relies on packing being idempotent there."""
    assert set(np.unique(golden_canvas)) <= {n * 17 for n in range(16)}


# ==========================================================================
# canvas <-> screen mapping
# ==========================================================================

@pytest.mark.parametrize("display,screen_id", [(0, 0), (1, 0), (2, 1), (3, 1)])
def test_a_rect_in_one_band_maps_to_that_band_s_screen(display, screen_id):
    rect = Rect(600, display * BAND + 300, 256, 128)
    mapped = screen_rects_for_canvas_rect(rect, P)
    assert len(mapped) == 1
    sid, screen = mapped[0]
    assert sid == screen_id
    # the eink-flip mirror reverses the column order, then x doubles
    assert screen == Rect(2 * (LANE_W - 856), 300, 512, 128)
    assert screen.x == 1168


def test_a_rect_straddling_a_seam_maps_to_both_bands():
    rect = Rect(600, BAND - 64, 256, 128)
    mapped = screen_rects_for_canvas_rect(rect, P)
    assert [sid for sid, _ in mapped] == [0, 0]
    assert [r.y for _, r in mapped] == [BAND - 64, 0]
    assert [r.height for _, r in mapped] == [64, 64]


def test_a_canvas_wide_rect_maps_to_four_two_per_screen():
    whole = Rect(0, 0, P.canvas_width, P.canvas_height)
    mapped = screen_rects_for_canvas_rect(whole, P)
    assert [sid for sid, _ in mapped] == [0, 0, 1, 1]
    for _sid, r in mapped:
        assert r == Rect(0, 0, P.screen_width, P.screen_height)


def test_the_group_snap_only_ever_grows_the_covered_region():
    """A rect that does not start on a group boundary grows outward, not in."""
    rect = Rect(601, 300, 250, 128)       # neither edge on a group
    (sid, screen), = screen_rects_for_canvas_rect(rect, P)
    regions = dict(canvas_regions_for_screen_rect(sid, screen, P))
    assert regions[0].intersection(rect) == rect
    assert regions[0].x <= rect.x and regions[0].far_x >= rect.far_x


@pytest.mark.parametrize("display", [0, 1, 2, 3])
def test_canvas_regions_round_trips_the_mapping(display):
    rect = Rect(608, display * BAND + 300, 256, 128)
    (sid, screen), = screen_rects_for_canvas_rect(rect, P)
    regions = canvas_regions_for_screen_rect(sid, screen, P)
    assert len(regions) == 2
    assert dict(regions)[display] == rect
    partner = [d for d, _ in regions if d != display]
    assert len(partner) == 1
    # the partner region is the same columns, the same rows, 640 away
    assert dict(regions)[partner[0]].x == rect.x
    assert dict(regions)[partner[0]].width == rect.width
    assert abs(dict(regions)[partner[0]].y - rect.y) == BAND


def test_a_non_mirroring_interlaced_panel_does_not_reverse_columns():
    panel = Panel(width=1440, height=640, displays=4,
                  driver=DRIVERS["eink-generic"])
    (sid, screen), = screen_rects_for_canvas_rect(Rect(608, 0, 256, 128), panel)
    assert (sid, screen) == (0, Rect(1216, 0, 512, 128))
    assert dict(canvas_regions_for_screen_rect(sid, screen, panel))[0] == \
        Rect(608, 0, 256, 128)


# ==========================================================================
# alignment, in screen space, at both bit depths
# ==========================================================================

def test_alignment_snaps_x_and_width_to_the_paired_group():
    bounds = screen_bounds(P)
    out = align_screen_rect(Rect(13, 7, 19, 5), 4, bounds)
    assert out.x % SCREEN_X_QUANTUM == 0 and out.width % SCREEN_X_QUANTUM == 0
    assert out.x <= 13 and out.far_x >= 32
    assert out.y == 7 and out.height == 5


@pytest.mark.parametrize("encoding", ENCODINGS)
@pytest.mark.parametrize("h", [1, 2, 3, 4, 5, 7, 8, 15, 16, 128, 640])
@pytest.mark.parametrize("x,w", [(0, 1), (0, 8), (3, 2), (2871, 9), (1600, 256)])
def test_alignment_always_produces_a_legal_rectangle(encoding, h, x, w):
    bounds = screen_bounds(P)
    rect = Rect(x, 0, w, h)
    out = align_screen_rect(rect, encoding, bounds)
    # legal, containing, and in bounds
    validate_screen_rect(0, out, P, encoding)
    assert out.intersection(rect) == rect.intersection(bounds)
    assert is_aligned(out, encoding)


def test_one_bpp_odd_height_needs_a_16_wide_rectangle():
    """At 1 bpp the quantum is 16 sub-pixels, so an odd height forces w%16."""
    out = align_screen_rect(Rect(0, 0, 8, 3), Encoding.ONE_BIT, screen_bounds(P))
    assert (out.width, out.height) == (16, 3)
    # 4 bpp needs nothing extra: w % 8 == 0 already divides the 4-sub-pixel quantum
    out4 = align_screen_rect(Rect(0, 0, 8, 3), Encoding.FOUR_BIT,
                             screen_bounds(P))
    assert (out4.width, out4.height) == (8, 3)


def test_alignment_grows_left_when_it_cannot_grow_right():
    bounds = screen_bounds(P)
    out = align_screen_rect(Rect(2872, 0, 8, 3), Encoding.ONE_BIT, bounds)
    assert out.far_x == bounds.far_x
    assert (out.x, out.width) == (2864, 16)


def test_alignment_refuses_when_nothing_legal_fits():
    tiny = Rect(0, 0, 8, 1)
    with pytest.raises(ValueError, match="cannot be aligned"):
        align_screen_rect(Rect(0, 0, 8, 1), Encoding.ONE_BIT, tiny)


def test_align_and_unite_settles_and_does_not_overlap():
    bounds = screen_bounds(P)
    out = align_and_unite_screen_rects(
        [Rect(0, 0, 12, 3), Rect(8, 0, 12, 3), Rect(2000, 100, 9, 7)],
        Encoding.ONE_BIT, bounds)
    for r in out:
        assert is_aligned(r, Encoding.ONE_BIT)
    for i, a in enumerate(out):
        for b in out[i + 1:]:
            assert not a.overlaps(b)


# ==========================================================================
# bounds are refused, never clamped
# ==========================================================================

@pytest.mark.parametrize("rect", [
    Rect(2880, 0, 8, 8),          # starts past the right edge
    Rect(2876, 0, 8, 8),          # ends past it
    Rect(0, 640, 8, 8),           # starts past the bottom
    Rect(0, 636, 8, 8),           # ends past it
])
def test_out_of_bounds_screen_rects_are_refused(rect):
    with pytest.raises(ValueError, match="leaves the panel bounds"):
        validate_screen_rect(0, rect, P, 4)


def test_an_unpaired_screen_rect_is_refused():
    with pytest.raises(ValueError, match="multiples of"):
        validate_screen_rect(0, Rect(4, 0, 8, 8), P, 4)


def test_an_unquantised_screen_rect_is_refused():
    with pytest.raises(ValueError, match="16-bit"):
        validate_screen_rect(0, Rect(0, 0, 8, 1), P, Encoding.ONE_BIT)


def test_an_unknown_screen_id_is_refused():
    with pytest.raises(ValueError, match="ScreenID"):
        validate_screen_rect(2, Rect(0, 0, 8, 8), P, 4)


def test_a_panel_without_screen_addressing_is_refused():
    with pytest.raises(ValueError, match="cannot address screen-space"):
        validate_screen_rect(0, Rect(0, 0, 8, 8), FLAT, 4)
    with pytest.raises(ValueError, match="no screen-space addressing"):
        screen_rects_for_canvas_rect(Rect(0, 0, 8, 8), FLAT)


def test_encode_screen_rect_checks_the_state_shape():
    with pytest.raises(ValueError, match="panel canvas"):
        encode_screen_rect(np.zeros((8, 8), dtype=np.uint8), screen_id=0,
                           rect=Rect(0, 0, 8, 8), panel=P, encoding=4)


def test_a_rotated_interlaced_panel_has_no_screen_path():
    rotated = Panel(width=1440, height=640, displays=4,
                    driver=DRIVERS["eink-flip"], rotation=1)
    assert rotated.supports_screen_rectangles is False


# ==========================================================================
# the partner lane
# ==========================================================================

@pytest.mark.parametrize("display", [0, 1, 2, 3])
def test_the_partner_lane_carries_the_unchanged_state(display):
    """One band changes; the rectangle still carries the other band's pixels.

    This is the 2x cost, and it has to be *correct* 2x: the partner half of
    the payload must decode back to what the state already held, or the push
    repaints the partner band with garbage.
    """
    base = canvas()
    first = encode_frame(base, panel=P, encoding=4, dithering=Dithering.NONE)
    changed = base.copy()
    changed[display * BAND + 64:display * BAND + 256, 256:512] = 0
    frame = encode_partial_frame(changed, panel=P, encoding=4,
                                 dithering=Dithering.NONE,
                                 prev_state=first.state)
    assert frame.screen_space and not frame.full_screen
    assert len(frame.rectangles) == 1
    rect = frame.rectangles[0]
    screen_id, lane_index = lane_of_display(2, display)
    assert rect.screen_id == screen_id

    halves = deinterlace(rect.data, 4)
    partner_display = displays_of_screen(2, screen_id)[1 - lane_index]
    lane_w = rect.w // 2
    partner = unpack(halves[1 - lane_index], 4,
                     lane_w * rect.h).reshape(rect.h, lane_w)
    # un-mirror, and compare against the *previous* state at those rows
    c0, _c1 = lane_span(rect.x, rect.w)
    x0 = LANE_W - c0 - lane_w
    y0 = partner_display * BAND + rect.y
    held = first.state.state_grey[y0:y0 + rect.h, x0:x0 + lane_w]
    assert np.array_equal(partner[:, ::-1], held)


def test_a_change_in_both_partner_bands_rides_one_rectangle():
    """Bands 0 and 1 share ScreenID 0, so both changes fit in one rect."""
    base = canvas()
    first = encode_frame(base, panel=P, encoding=4, dithering=Dithering.NONE)
    changed = base.copy()
    changed[300:428, 608:864] = 0
    changed[BAND + 300:BAND + 428, 608:864] = 255
    frame = encode_partial_frame(changed, panel=P, encoding=4,
                                 dithering=Dithering.NONE,
                                 prev_state=first.state)
    assert [r.screen_id for r in frame.rectangles] == [0]
    assert len(frame.dirty_rects) == 2
    assert decode(frame).covered == ((608, 300, 256, 128),
                                     (608, BAND + 300, 256, 128))


# ==========================================================================
# the properties that matter: state, checksum, and the decoded glass
# ==========================================================================

@pytest.mark.parametrize("encoding", ENCODINGS)
@pytest.mark.parametrize("dithering", PIXEL_LOCAL)
def test_a_partial_leaves_the_state_exactly_where_a_full_push_would(
    encoding, dithering
):
    """The checksum claim, which the device checks for us on every push."""
    lv = levels(dithering, encoding)
    base = canvas()
    first = encode_frame(base, panel=P, encoding=encoding, dithering=dithering,
                         dither_levels=lv)
    changed = base.copy()
    changed[300:428, 608:864] = 0
    changed[2000:2100, 100:400] = 255

    partial = encode_partial_frame(changed, panel=P, encoding=encoding,
                                   dithering=dithering, dither_levels=lv,
                                   prev_state=first.state)
    full = encode_frame(changed, panel=P, encoding=encoding,
                        dithering=dithering, dither_levels=lv,
                        force_full_screen=True)
    assert partial.screen_space and not partial.full_screen
    assert np.array_equal(partial.state.state_grey, full.state.state_grey)
    assert partial.state_checksum == full.state_checksum
    assert partial.state_checksum == state_checksum(
        np.ascontiguousarray(partial.state.state_grey).tobytes())


@pytest.mark.parametrize("encoding", ENCODINGS)
@pytest.mark.parametrize("dithering", PIXEL_LOCAL)
def test_a_partial_then_a_decode_gives_the_same_glass_as_a_full_push(
    encoding, dithering
):
    """The property the brief asks for, run through the real decoder.

    Push a full baseline, decode it to get "what the glass shows"; push a
    partial, decode it and blit only what it covered; the result must equal a
    decode of a full push of the same image.
    """
    lv = levels(dithering, encoding)
    base = canvas()
    first = encode_frame(base, panel=P, encoding=encoding, dithering=dithering,
                         dither_levels=lv)
    changed = base.copy()
    changed[300:428, 608:864] = 0
    changed[1500:1600, 1000:1300] = 255
    changed[2300:2310, 7:19] = 128

    partial = encode_partial_frame(changed, panel=P, encoding=encoding,
                                   dithering=dithering, dither_levels=lv,
                                   prev_state=first.state)
    full = encode_frame(changed, panel=P, encoding=encoding,
                        dithering=dithering, dither_levels=lv,
                        force_full_screen=True)

    glass = decode(first).canvas.copy()
    decoded = decode(partial)
    assert not decoded.full
    assert decoded.screens  # it addressed something
    for x, y, w, h in decoded.covered:
        glass[y:y + h, x:x + w] = decoded.canvas[y:y + h, x:x + w]
    assert np.array_equal(glass, decode(full).canvas)


@pytest.mark.parametrize("encoding", ENCODINGS)
def test_payload_lengths_are_exactly_what_the_geometry_implies(encoding):
    from pyvisionect.imaging import payload_size

    base = canvas()
    first = encode_frame(base, panel=P, encoding=encoding,
                         dithering=Dithering.BLUE_NOISE)
    changed = base.copy()
    changed[300:428, 608:864] = 0
    frame = encode_partial_frame(changed, panel=P, encoding=encoding,
                                 dithering=Dithering.BLUE_NOISE,
                                 prev_state=first.state)
    for rect in frame.rectangles:
        assert len(rect.data) == payload_size(rect.w, rect.h, encoding)
        validate_screen_rect(rect.screen_id, Rect(rect.x, rect.y, rect.w,
                                                  rect.h), P, encoding)


def test_floyd_steinberg_partials_do_not_claim_to_match_a_full_push():
    """Documented limitation, asserted so it cannot quietly become untrue.

    Floyd-Steinberg is not pixel-local: the error diffusion that reaches a
    region depends on everything above and left of it.  A partial re-dithers
    only its own band slice, so its pixels differ from a full push's.  The
    partial is still *self*-consistent -- its state and checksum describe the
    bytes it sent -- which is all the device checks.
    """
    base = canvas()
    first = encode_frame(base, panel=P, encoding=4,
                         dithering=Dithering.FLOYD_STEINBERG)
    changed = base.copy()
    changed[300:428, 608:864] = 0
    partial = encode_partial_frame(changed, panel=P, encoding=4,
                                   dithering=Dithering.FLOYD_STEINBERG,
                                   prev_state=first.state)
    full = encode_frame(changed, panel=P, encoding=4,
                        dithering=Dithering.FLOYD_STEINBERG,
                        force_full_screen=True)
    assert partial.screen_space
    assert not np.array_equal(partial.state.state_grey, full.state.state_grey)
    # self-consistent, which is the part the device checks
    assert partial.state_checksum == state_checksum(
        np.ascontiguousarray(partial.state.state_grey).tobytes())
    glass = decode(first).canvas.copy()
    decoded = decode(partial)
    for x, y, w, h in decoded.covered:
        glass[y:y + h, x:x + w] = decoded.canvas[y:y + h, x:x + w]
    assert np.array_equal(glass, partial.state.state_grey)


# ==========================================================================
# what it saves
# ==========================================================================

def test_a_small_change_costs_a_fraction_of_a_full_push():
    base = canvas()
    first = encode_frame(base, panel=P, encoding=4, dithering=Dithering.NONE)
    changed = base.copy()
    changed[96:224, 608:736] = 0          # a 128x128 block, one band
    frame = encode_partial_frame(changed, panel=P, encoding=4,
                                 dithering=Dithering.NONE,
                                 prev_state=first.state)
    cost = partial_cost(frame.rectangles)
    full = full_screen_cost(P, 4)
    assert full == 2 * (24 + 921600)
    # 256x128 screen rect: the 2x partner cost is in here already
    assert cost == 24 + 256 * 128 // 2
    assert cost * 100 < full


def test_full_screen_cost_matches_a_real_full_frame():
    frame = encode_frame(canvas(), panel=P, encoding=4,
                         dithering=Dithering.NONE)
    assert partial_cost(frame.rectangles) == full_screen_cost(P, 4)


# ==========================================================================
# every fallback trigger
# ==========================================================================

def _first():
    base = canvas()
    return base, encode_frame(base, panel=P, encoding=4,
                              dithering=Dithering.NONE)


def _changed(base):
    out = base.copy()
    out[300:428, 608:864] = 0
    return out


@pytest.mark.parametrize("reason,kwargs", [
    ("no-previous-state", {"prev_state": None}),
    ("forced", {"force_full_screen": True}),
    ("inverse", {"inverse": True}),
])
def test_structural_fallbacks(reason, kwargs):
    base, first = _first()
    kwargs = {"prev_state": first.state, **kwargs}
    frame = encode_partial_frame(_changed(base), panel=P, encoding=4,
                                 dithering=Dithering.NONE, **kwargs)
    assert frame.fallback_reason == reason
    assert frame.full_screen and not frame.screen_space
    assert [(r.w, r.h) for r in frame.rectangles] == [(2880, 640)] * 2


def test_re_encode_falls_back():
    base, first = _first()
    frame = encode_partial_frame(_changed(base), panel=P,
                                 encoding=Encoding.ONE_BIT,
                                 dithering=Dithering.NONE,
                                 prev_state=first.state)
    assert frame.fallback_reason == "re-encode"
    frame = encode_partial_frame(_changed(base), panel=P, encoding=4,
                                 dithering=Dithering.BLUE_NOISE,
                                 prev_state=first.state)
    assert frame.fallback_reason == "re-encode"


def test_the_ghosting_budget_falls_back_and_then_resets():
    base, first = _first()
    policy = PartialPolicy(max_consecutive_partials=3)
    for n in range(3):
        frame = encode_partial_frame(_changed(base), panel=P, encoding=4,
                                     dithering=Dithering.NONE,
                                     prev_state=first.state, policy=policy,
                                     consecutive_partials=n)
        assert frame.fallback_reason is None and frame.screen_space
    frame = encode_partial_frame(_changed(base), panel=P, encoding=4,
                                 dithering=Dithering.NONE,
                                 prev_state=first.state, policy=policy,
                                 consecutive_partials=3)
    assert frame.fallback_reason == "ghosting-refresh-due"
    assert frame.full_screen


def test_a_zero_budget_means_never_partial():
    base, first = _first()
    frame = encode_partial_frame(_changed(base), panel=P, encoding=4,
                                 dithering=Dithering.NONE,
                                 prev_state=first.state,
                                 policy=PartialPolicy(
                                     max_consecutive_partials=0))
    assert frame.fallback_reason == "ghosting-refresh-due"


def test_a_negative_budget_never_forces_a_refresh():
    policy = PartialPolicy(max_consecutive_partials=-1)
    assert policy.refresh_due(0) is False
    assert policy.refresh_due(10_000) is False


def test_the_cost_gate_falls_back_on_a_big_change():
    base, first = _first()
    changed = base.copy()
    changed[:1500] = 0                    # most of two bands
    frame = encode_partial_frame(changed, panel=P, encoding=4,
                                 dithering=Dithering.NONE,
                                 prev_state=first.state)
    assert frame.fallback_reason == "not-worth-it"
    assert frame.full_screen
    # and a generous fraction lets the same frame through as a partial
    frame = encode_partial_frame(changed, panel=P, encoding=4,
                                 dithering=Dithering.NONE,
                                 prev_state=first.state,
                                 policy=PartialPolicy(max_cost_fraction=1.0))
    assert frame.screen_space and not frame.full_screen


def test_too_many_regions_falls_back_when_merging_is_off():
    base, first = _first()
    changed = base.copy()
    for i in range(20):
        changed[64 + i * 24:72 + i * 24, 8 + i * 48:24 + i * 48] = 0
    frame = encode_partial_frame(changed, panel=P, encoding=4,
                                 dithering=Dithering.NONE,
                                 prev_state=first.state, merge_regions=False)
    assert frame.fallback_reason == "too-many-regions"
    assert frame.full_screen
    # with merging on, the same frame survives as a partial
    frame = encode_partial_frame(changed, panel=P, encoding=4,
                                 dithering=Dithering.NONE,
                                 prev_state=first.state, merge_regions=True)
    assert frame.screen_space
    assert len(frame.rectangles) <= 15


def test_an_interlaced_panel_with_no_screen_addressing_falls_back():
    """Interlaced, so no canvas rect survives the fold; but the lane columns
    are not a whole number of 4-pixel groups, so no screen rect is legal
    either.  The only answer left is a full screen.
    """
    panel = Panel(width=10, height=8, displays=4, driver=DRIVERS["eink-flip"])
    assert panel.interlace_mode == 2
    assert panel.supports_screen_rectangles is False
    base = canvas(panel)
    first = encode_frame(base, panel=panel, encoding=4,
                         dithering=Dithering.NONE)
    changed = base.copy()
    changed[2:6, 1:5] = 0
    frame = encode_partial_frame(changed, panel=panel, encoding=4,
                                 dithering=Dithering.NONE,
                                 prev_state=first.state)
    assert frame.fallback_reason == "panel-unsupported"
    assert frame.full_screen
    assert [(r.w, r.h) for r in frame.rectangles] == [(20, 8)] * 2


def test_interlacing_mode_1_is_refused_rather_than_guessed():
    """Mode 1's lane pairing was never recovered, so neither path can encode
    it -- the partial gate reports ``panel-unsupported`` and the full-screen
    fallback then raises, which is the right failure for a map we do not have.
    """
    panel = Panel(width=1440, height=640, displays=4,
                  driver=DRIVERS["eink-flip"], interlace=1)
    assert panel.supports_screen_rectangles is False
    base = canvas()
    first = encode_frame(base, panel=P, encoding=4, dithering=Dithering.NONE)
    with pytest.raises(NotImplementedError, match="interlacing mode 1"):
        encode_partial_frame(_changed(base), panel=panel, encoding=4,
                             dithering=Dithering.NONE,
                             prev_state=first.state)


def test_nothing_changed_sends_nothing():
    base, first = _first()
    frame = encode_partial_frame(base, panel=P, encoding=4,
                                 dithering=Dithering.NONE,
                                 prev_state=first.state)
    assert frame.rectangles == []
    assert frame.state_checksum == first.state_checksum
    assert frame.dirty_rects == ()
    assert not frame.full_screen


def test_a_mismatched_canvas_is_an_error_not_a_fallback():
    _base, first = _first()
    with pytest.raises(ValueError, match="panel canvas is"):
        encode_partial_frame(np.zeros((16, 16), dtype=np.uint8), panel=P,
                             encoding=4, dithering=Dithering.NONE,
                             prev_state=first.state)


def test_a_mismatched_prev_state_is_an_error():
    from pyvisionect.imaging import FrameState

    base = canvas()
    bogus = FrameState(np.zeros((16, 16), dtype=np.uint8),
                       np.zeros(P.canvas_shape, dtype=np.uint8), 7, 4,
                       int(Dithering.NONE))
    with pytest.raises(ValueError, match="state image is"):
        encode_partial_frame(_changed(base), panel=P, encoding=4,
                             dithering=Dithering.NONE, prev_state=bogus)


def test_caller_supplied_rects_are_clipped_to_the_canvas():
    base, first = _first()
    changed = _changed(base)
    frame = encode_partial_frame(
        changed, panel=P, encoding=4, dithering=Dithering.NONE,
        prev_state=first.state,
        rects=[Rect(-100, -100, 400, 400), Rect(P.canvas_width, 0, 8, 8)])
    assert frame.screen_space
    for rect in frame.dirty_rects:
        assert rect.x >= 0 and rect.y >= 0
        assert rect.far_x <= P.canvas_width and rect.far_y <= P.canvas_height


def test_a_bad_policy_is_refused():
    with pytest.raises(ValueError, match="max_cost_fraction"):
        PartialPolicy(max_cost_fraction=0.0)
    with pytest.raises(ValueError, match="max_cost_fraction"):
        PartialPolicy(max_cost_fraction=1.5)


# ==========================================================================
# the non-interlaced path: no fold, so a canvas rect is the wire rect
# ==========================================================================

def test_a_flat_panel_gets_canvas_space_partials():
    base = canvas(FLAT)
    first = encode_frame(base, panel=FLAT, encoding=4,
                         dithering=Dithering.NONE)
    changed = base.copy()
    changed[4:12, 8:24] = 0
    frame = encode_partial_frame(changed, panel=FLAT, encoding=4,
                                 dithering=Dithering.NONE,
                                 prev_state=first.state)
    assert not frame.screen_space        # there is no screen space here
    assert not frame.full_screen
    assert frame.rectangles
    assert all(r.w < FLAT.width or r.h < FLAT.height for r in frame.rectangles)
    full = encode_frame(changed, panel=FLAT, encoding=4,
                        dithering=Dithering.NONE, force_full_screen=True)
    assert frame.state_checksum == full.state_checksum


def test_a_flat_panel_still_honours_the_ghosting_budget():
    base = canvas(FLAT)
    first = encode_frame(base, panel=FLAT, encoding=4,
                         dithering=Dithering.NONE)
    changed = base.copy()
    changed[4:12, 8:24] = 0
    frame = encode_partial_frame(changed, panel=FLAT, encoding=4,
                                 dithering=Dithering.NONE,
                                 prev_state=first.state,
                                 policy=PartialPolicy(
                                     max_consecutive_partials=2),
                                 consecutive_partials=2)
    assert frame.fallback_reason == "ghosting-refresh-due"


# ==========================================================================
# the tracker
# ==========================================================================

def test_the_tracker_opens_with_a_full_screen_then_goes_partial():
    base = canvas()
    tracker = DirtyTracker(panel=P, encoding=4, dithering=Dithering.NONE)
    first = tracker.update(base)
    assert first.full_screen and first.fallback_reason == "no-previous-state"
    assert tracker.consecutive_partials == 0

    changed = _changed(base)
    second = tracker.update(changed)
    assert second.screen_space and not second.full_screen
    assert tracker.consecutive_partials == 1
    # and the state it kept is the one a full push would have produced
    full = encode_frame(changed, panel=P, encoding=4,
                        dithering=Dithering.NONE, force_full_screen=True)
    assert tracker.state is not None
    assert tracker.state.checksum == full.state_checksum


def test_the_tracker_forces_a_full_refresh_at_the_threshold():
    base = canvas()
    tracker = DirtyTracker(panel=P, encoding=4, dithering=Dithering.NONE)
    tracker.update(base)
    history: list[tuple[bool, int]] = []
    for i in range(1, MAX_NO_FULL_UPDATE + 4):
        img = base.copy()
        img[300:428, 608 + 8 * i:864 + 8 * i] = 0
        frame = tracker.update(img)
        history.append((frame.full_screen, tracker.consecutive_partials))
    fulls = [i for i, (full, _n) in enumerate(history) if full]
    # exactly one forced full screen, after exactly MAX_NO_FULL_UPDATE partials
    assert fulls == [MAX_NO_FULL_UPDATE]
    assert history[MAX_NO_FULL_UPDATE - 1] == (False, MAX_NO_FULL_UPDATE)
    assert history[MAX_NO_FULL_UPDATE] == (True, 0)
    assert history[MAX_NO_FULL_UPDATE + 1] == (False, 1)


def test_the_tracker_does_not_spend_budget_on_an_unchanged_frame():
    base = canvas()
    tracker = DirtyTracker(panel=P, encoding=4, dithering=Dithering.NONE)
    tracker.update(base)
    tracker.update(_changed(base))
    assert tracker.consecutive_partials == 1
    for _ in range(5):
        frame = tracker.update(_changed(base))
        assert frame.rectangles == []
    assert tracker.consecutive_partials == 1


def test_refresh_due_predicts_the_promotion():
    base = canvas()
    tracker = DirtyTracker(panel=P, encoding=4, dithering=Dithering.NONE,
                           policy=PartialPolicy(max_consecutive_partials=2))
    tracker.update(base)
    assert tracker.refresh_due is False
    for i in (1, 2):
        img = base.copy()
        img[300:428, 608 + 8 * i:864 + 8 * i] = 0
        assert tracker.update(img).screen_space
    assert tracker.refresh_due is True
    img = base.copy()
    img[300:428, 900:1156] = 0
    assert tracker.update(img).full_screen


def test_rollback_restores_the_state_a_failed_push_invalidated():
    base = canvas()
    tracker = DirtyTracker(panel=P, encoding=4, dithering=Dithering.NONE)
    tracker.update(base)
    opening = tracker.state
    assert opening is not None
    frame = tracker.update(_changed(base))
    assert tracker.state is not opening and tracker.consecutive_partials == 1
    tracker.rollback()
    assert tracker.state is opening
    assert tracker.consecutive_partials == 0
    # re-encoding the same frame now produces the same bytes again
    again = tracker.update(_changed(base))
    assert [r.data for r in again.rectangles] == [r.data for r in frame.rectangles]


def test_reset_makes_the_next_frame_a_full_push():
    base = canvas()
    tracker = DirtyTracker(panel=P, encoding=4, dithering=Dithering.NONE)
    tracker.update(base)
    tracker.update(_changed(base))
    tracker.reset()
    assert tracker.state is None
    frame = tracker.update(_changed(base))
    assert frame.full_screen and frame.fallback_reason == "no-previous-state"


def test_a_tracker_can_resume_from_a_persisted_state():
    from pyvisionect.imaging import FrameState

    base = canvas()
    opening = encode_frame(base, panel=P, encoding=4,
                           dithering=Dithering.NONE)
    blob = opening.state.to_bytes()
    tracker = DirtyTracker(panel=P, encoding=4, dithering=Dithering.NONE,
                           state=FrameState.from_bytes(blob),
                           consecutive_partials=4)
    assert tracker.consecutive_partials == 4
    frame = tracker.update(_changed(base))
    assert frame.screen_space and not frame.full_screen
    assert tracker.consecutive_partials == 5


def test_the_tracker_forwards_its_encode_options():
    base = canvas()
    tracker = DirtyTracker(panel=P, encoding=4, dithering=Dithering.NONE,
                           rect_options=0x0002,
                           rectangle_update_options=0x0999)
    tracker.update(base)
    frame = tracker.update(_changed(base))
    for rect in frame.rectangles:
        assert rect.options == 0x0002
        assert rect.rectangle_update_options == 0x0999


def test_the_tracker_rejects_a_negative_counter():
    with pytest.raises(ValueError, match="negative"):
        DirtyTracker(panel=P, encoding=4, dithering=Dithering.NONE,
                     consecutive_partials=-1)


def test_the_tracker_validates_encoding_and_dithering_up_front():
    with pytest.raises(ValueError, match="bad encoding"):
        DirtyTracker(panel=P, encoding=3, dithering=Dithering.NONE)
    with pytest.raises(ValueError, match="DitheringType 0"):
        DirtyTracker(panel=P, encoding=4, dithering=0)


# ==========================================================================
# the public API did not change under anyone
# ==========================================================================

def test_encode_frame_is_unchanged_by_default():
    """The default path must be byte-for-byte what it was before `partial`."""
    base = canvas()
    first = encode_frame(base, panel=P, encoding=4, dithering=Dithering.NONE)
    changed = _changed(base)
    frame = encode_frame(changed, panel=P, encoding=4,
                         dithering=Dithering.NONE, prev_state=first.state)
    assert frame.full_screen is True
    assert frame.screen_space is False
    assert frame.fallback_reason is None
    assert frame.dirty_rects == ()
    assert [(r.screen_id, r.x, r.y, r.w, r.h) for r in frame.rectangles] == [
        (0, 0, 0, 2880, 640), (1, 0, 0, 2880, 640)]


def test_encode_frame_partial_true_is_the_partial_encoder():
    base = canvas()
    first = encode_frame(base, panel=P, encoding=4, dithering=Dithering.NONE)
    changed = _changed(base)
    through_flag = encode_frame(changed, panel=P, encoding=4,
                                dithering=Dithering.NONE,
                                prev_state=first.state, partial=True)
    direct = encode_partial_frame(changed, panel=P, encoding=4,
                                  dithering=Dithering.NONE,
                                  prev_state=first.state)
    assert through_flag.screen_space and direct.screen_space
    assert through_flag.state_checksum == direct.state_checksum
    assert [(r.screen_id, r.x, r.y, r.w, r.h, r.data)
            for r in through_flag.rectangles] == \
        [(r.screen_id, r.x, r.y, r.w, r.h, r.data) for r in direct.rectangles]


def test_encode_frame_partial_true_forwards_the_policy_and_counter():
    base = canvas()
    first = encode_frame(base, panel=P, encoding=4, dithering=Dithering.NONE)
    frame = encode_frame(_changed(base), panel=P, encoding=4,
                         dithering=Dithering.NONE, prev_state=first.state,
                         partial=True,
                         partial_policy=PartialPolicy(
                             max_consecutive_partials=2),
                         consecutive_partials=2)
    assert frame.fallback_reason == "ghosting-refresh-due"


def test_panel_capability_properties():
    assert P.supports_screen_rectangles is True
    assert P.forces_full_screen is True      # still true for *canvas* rects
    assert (P.screens, P.screen_width, P.screen_height) == (2, 2880, 640)
    assert FLAT.supports_screen_rectangles is False
    assert (FLAT.screens, FLAT.screen_width, FLAT.screen_height) == (2, 64, 32)
