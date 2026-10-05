"""End-to-end :func:`encode_frame` behaviour.

Covers every (encoding, dithering) combination through pack -> unpack ->
compare, the full-screen forcing rules, change detection, the state image and
the inverse-update bit.
"""

from __future__ import annotations

import warnings

import numpy as np
import pytest

from pyvisionect.imaging import (DRIVERS, OPTION_NORMAL_UPDATE, PANEL_32INCH,
                                 Dithering, Encoding, INTERLACE_PAIRS, Panel,
                                 Rect, deinterlace, mirror_image,
                                 state_checksum, unpack)
from pyvisionect.imaging.encoder import encode_frame

ENCODINGS = [Encoding.ONE_BIT, Encoding.FOUR_BIT]
DITHERINGS = [Dithering.NONE, Dithering.BAYER, Dithering.FLOYD_STEINBERG,
              Dithering.BLUE_NOISE]

#: A small non-interlacing panel, so partial updates are reachable.
SMALL = Panel(width=64, height=32, displays=2, driver=DRIVERS["eink-generic"],
              interlace=0)
#: Same geometry but mirrored, to exercise the ``eink-flip`` path.
SMALL_FLIP = Panel(width=64, height=32, displays=2,
                   driver=DRIVERS["eink-flip"], interlace=0)


def _canvas(panel: Panel, seed: int = 0) -> np.ndarray:
    y, x = np.mgrid[0:panel.canvas_height, 0:panel.canvas_width]
    v = 128 + 100 * np.sin((x + seed) / 7.0) * np.cos(y / 5.0) + 0.3 * x
    return np.clip(v, 0, 255).astype(np.uint8)


def _encode(*args, **kw):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        return encode_frame(*args, **kw)


# ------------------------------------------------------------ round trips ----

@pytest.mark.parametrize("encoding", ENCODINGS)
@pytest.mark.parametrize("dithering", DITHERINGS)
@pytest.mark.parametrize("panel", [SMALL, SMALL_FLIP],
                         ids=["eink-generic", "eink-flip"])
def test_round_trip_every_encoding_and_dithering(panel, encoding, dithering):
    """Decode each rectangle the way the device/preview does and compare.

    The decoded, un-mirrored rectangle must equal the corresponding region of
    the state image -- which is the module's own claim about what the panel
    will show.
    """
    img = _canvas(panel)
    frame = _encode(img, panel=panel, encoding=encoding, dithering=dithering)
    assert frame.rectangles
    for rect in frame.rectangles:
        px = unpack(rect.data, encoding, rect.w * rect.h).reshape(rect.h, rect.w)
        if panel.driver.mirror:
            px = mirror_image(px)
        y0 = rect.screen_id * panel.height + rect.y
        region = frame.state.state_grey[y0:y0 + rect.h, rect.x:rect.x + rect.w]
        assert np.array_equal(px, region)


@pytest.mark.parametrize("encoding", ENCODINGS)
@pytest.mark.parametrize("dithering", DITHERINGS)
def test_payload_lengths_are_exact(encoding, dithering):
    from pyvisionect.imaging import payload_size

    frame = _encode(_canvas(SMALL), panel=SMALL, encoding=encoding,
                    dithering=dithering)
    for rect in frame.rectangles:
        assert len(rect.data) == payload_size(rect.w, rect.h, encoding)


@pytest.mark.parametrize("encoding", ENCODINGS)
@pytest.mark.parametrize("dithering", DITHERINGS)
def test_state_checksum_is_the_hash_of_the_state_image(encoding, dithering):
    frame = _encode(_canvas(SMALL), panel=SMALL, encoding=encoding,
                    dithering=dithering)
    expected = state_checksum(
        np.ascontiguousarray(frame.state.state_grey).tobytes())
    assert frame.state_checksum == expected == frame.state.checksum
    assert frame.state_checksum != 0


# -------------------------------------------------------------- geometry ----

def test_32inch_wire_shape():
    """Two 2880x640 rectangles at ScreenID 0 and 1 -- spec §1.6."""
    frame = _encode(_canvas(PANEL_32INCH), panel=PANEL_32INCH, encoding=4,
                    dithering=Dithering.BLUE_NOISE)
    assert frame.nr_primitives == 2
    assert [r.screen_id for r in frame.rectangles] == [0, 1]
    for r in frame.rectangles:
        assert (r.x, r.y, r.w, r.h) == (0, 0, 2880, 640)
        assert len(r.data) == 921600
        assert r.rectangle_update_options == 0x0102
        assert r.image_type == 1


def test_32inch_interlacing_round_trips_to_the_state_image():
    """De-interlace + un-mirror + stack must give back the state canvas."""
    img = _canvas(PANEL_32INCH)
    frame = _encode(img, panel=PANEL_32INCH, encoding=4,
                    dithering=Dithering.BLUE_NOISE)
    lanes: dict[int, bytes] = {}
    for rect in frame.rectangles:
        a, b = deinterlace(rect.data, 4)
        lane_a, lane_b = dict(INTERLACE_PAIRS[2])[rect.screen_id]
        lanes[lane_a], lanes[lane_b] = a, b
    rebuilt = np.vstack([
        mirror_image(unpack(lanes[i], 4, 1440 * 640).reshape(640, 1440))
        for i in range(4)
    ])
    assert np.array_equal(rebuilt, frame.state.state_grey)


def test_mirroring_is_actually_applied():
    img = _canvas(SMALL)
    flipped = _encode(img, panel=SMALL_FLIP, encoding=4, dithering=Dithering.NONE)
    plain = _encode(img, panel=SMALL, encoding=4, dithering=Dithering.NONE)
    assert flipped.rectangles[0].data != plain.rectangles[0].data
    # but the state image is in canvas space, so it is identical either way
    assert np.array_equal(flipped.state.state_grey, plain.state.state_grey)
    assert flipped.state_checksum == plain.state_checksum


@pytest.mark.parametrize("rotation", [0, 1, 2, 3])
def test_rotation_round_trips(rotation):
    """Rotation 0 is the verified one; 1/2/3 are self-consistency only."""
    from pyvisionect.imaging import unrotate_image

    panel = Panel(width=32, height=32, displays=2,
                  driver=DRIVERS["eink-generic"], rotation=rotation,
                  interlace=0)
    img = _canvas(panel)
    frame = _encode(img, panel=panel, encoding=4, dithering=Dithering.NONE)
    for rect in frame.rectangles:
        px = unpack(rect.data, 4, rect.w * rect.h).reshape(rect.h, rect.w)
        back = unrotate_image(px, rotation)
        display = panel.geometry()[rect.screen_id]
        from pyvisionect.imaging import translate_to_session
        sess = translate_to_session(Rect(rect.x, rect.y, rect.w, rect.h), display)
        rows, cols = sess.as_slice()
        assert np.array_equal(back, frame.state.state_grey[rows, cols])


# ------------------------------------------------- full-screen / partials ----

def test_first_frame_is_always_full_screen():
    frame = _encode(_canvas(SMALL), panel=SMALL, encoding=4,
                    dithering=Dithering.NONE)
    assert frame.full_screen
    assert len(frame.rectangles) == SMALL.displays


def test_interlacing_hardware_always_sends_full_screen():
    """``getRectangleSupport`` is false for HardwareNameID 8 -- spec §1.7."""
    img = _canvas(PANEL_32INCH)
    first = _encode(img, panel=PANEL_32INCH, encoding=4, dithering=Dithering.NONE)
    changed = img.copy()
    changed[10:20, 10:20] = 0
    second = _encode(changed, panel=PANEL_32INCH, encoding=4,
                     dithering=Dithering.NONE, prev_state=first.state)
    assert second.full_screen
    assert second.nr_primitives == 2
    assert [r.w for r in second.rectangles] == [2880, 2880]


def test_unchanged_frame_emits_nothing():
    img = _canvas(SMALL)
    first = _encode(img, panel=SMALL, encoding=4, dithering=Dithering.NONE)
    second = _encode(img, panel=SMALL, encoding=4, dithering=Dithering.NONE,
                     prev_state=first.state)
    assert second.rectangles == []
    assert second.state_checksum == first.state_checksum
    assert not second.full_screen


def test_change_detection_produces_partial_rectangles():
    img = _canvas(SMALL)
    first = _encode(img, panel=SMALL, encoding=4, dithering=Dithering.NONE)
    changed = img.copy()
    changed[4:12, 8:24] = 0
    second = _encode(changed, panel=SMALL, encoding=4,
                     dithering=Dithering.NONE, prev_state=first.state)
    assert second.rectangles
    assert not second.full_screen
    # the edit is inside display 0 only
    assert {r.screen_id for r in second.rectangles} == {0}
    for r in second.rectangles:
        assert r.w < SMALL.width or r.h < SMALL.height


def test_partial_update_only_touches_its_own_region_of_the_state():
    img = _canvas(SMALL)
    first = _encode(img, panel=SMALL, encoding=4, dithering=Dithering.NONE)
    changed = img.copy()
    changed[4:12, 8:24] = 0
    second = _encode(changed, panel=SMALL, encoding=4,
                     dithering=Dithering.NONE, prev_state=first.state)
    diff = first.state.state_grey != second.state.state_grey
    ys, xs = np.nonzero(diff)
    assert ys.min() >= 4 and ys.max() < 12
    assert xs.min() >= 8 and xs.max() < 24


def test_changing_encoding_forces_full_screen():
    img = _canvas(SMALL)
    first = _encode(img, panel=SMALL, encoding=4, dithering=Dithering.NONE)
    second = _encode(img, panel=SMALL, encoding=1, dithering=Dithering.NONE,
                     prev_state=first.state)
    assert second.full_screen


def test_changing_dithering_forces_full_screen():
    img = _canvas(SMALL)
    first = _encode(img, panel=SMALL, encoding=4, dithering=Dithering.NONE)
    second = _encode(img, panel=SMALL, encoding=4,
                     dithering=Dithering.BLUE_NOISE, prev_state=first.state)
    assert second.full_screen


def test_force_full_screen():
    img = _canvas(SMALL)
    first = _encode(img, panel=SMALL, encoding=4, dithering=Dithering.NONE)
    second = _encode(img, panel=SMALL, encoding=4, dithering=Dithering.NONE,
                     prev_state=first.state, force_full_screen=True)
    assert second.full_screen
    assert len(second.rectangles) == SMALL.displays


def test_explicit_rects_are_honoured():
    img = _canvas(SMALL)
    first = _encode(img, panel=SMALL, encoding=4, dithering=Dithering.NONE)
    second = _encode(img, panel=SMALL, encoding=4, dithering=Dithering.NONE,
                     prev_state=first.state, rects=[Rect(0, 0, 32, 16)])
    assert len(second.rectangles) == 1
    r = second.rectangles[0]
    assert (r.screen_id, r.x, r.y, r.w, r.h) == (0, 0, 0, 32, 16)


def test_merge_disabled_over_the_limit_promotes_to_full_screen():
    """``image-state.go:241`` -- ``forceFullScreenUnlocked``."""
    img = _canvas(SMALL)
    first = _encode(img, panel=SMALL, encoding=4, dithering=Dithering.NONE)
    many = [Rect(2 * i, 2 * i, 1, 1) for i in range(16)]
    frame = _encode(img, panel=SMALL, encoding=4, dithering=Dithering.NONE,
                    prev_state=first.state, rects=many, merge_regions=False)
    assert frame.full_screen


def test_merge_enabled_over_the_limit_merges():
    img = _canvas(SMALL)
    first = _encode(img, panel=SMALL, encoding=4, dithering=Dithering.NONE)
    many = [Rect(2 * i, 2 * i, 1, 1) for i in range(20)]
    frame = _encode(img, panel=SMALL, encoding=4, dithering=Dithering.NONE,
                    prev_state=first.state, rects=many, merge_regions=True)
    assert not frame.full_screen
    assert len(frame.rectangles) <= 15


# -------------------------------------------------------- options / flags ----

def test_rect_options_are_copied_verbatim():
    frame = _encode(_canvas(SMALL), panel=SMALL, encoding=4,
                    dithering=Dithering.NONE, rect_options=0x1234)
    assert all(r.options == 0x1234 for r in frame.rectangles)


def test_inverse_update_clears_bit_1_on_a_full_screen_frame():
    """Spec §1.4/§5: ``Options &= ^uint16(0x0002)``."""
    frame = _encode(_canvas(SMALL), panel=SMALL, encoding=4,
                    dithering=Dithering.NONE,
                    rect_options=OPTION_NORMAL_UPDATE | 0x0004, inverse=True)
    assert frame.full_screen
    assert all(r.options == 0x0004 for r in frame.rectangles)


def test_inverse_update_is_a_noop_with_the_shipped_rectangle_flags():
    """``RectangleFlags = 0`` means the bit is already clear -- spec §1.4."""
    plain = _encode(_canvas(SMALL), panel=SMALL, encoding=4,
                    dithering=Dithering.NONE, rect_options=0)
    inv = _encode(_canvas(SMALL), panel=SMALL, encoding=4,
                  dithering=Dithering.NONE, rect_options=0, inverse=True)
    assert [r.options for r in plain.rectangles] == \
        [r.options for r in inv.rectangles] == [0] * len(plain.rectangles)


@pytest.mark.parametrize("encoding,expected", [(1, 0x0101), (4, 0x0102)])
def test_rectangle_update_options_default(encoding, expected):
    frame = _encode(_canvas(SMALL), panel=SMALL, encoding=encoding,
                    dithering=Dithering.NONE)
    assert all(r.rectangle_update_options == expected for r in frame.rectangles)


def test_rectangle_update_options_can_be_left_for_the_marshaller():
    frame = _encode(_canvas(SMALL), panel=SMALL, encoding=4,
                    dithering=Dithering.NONE, rectangle_update_options=0)
    assert all(r.rectangle_update_options == 0 for r in frame.rectangles)


# ---------------------------------------------------------------- errors ----

@pytest.mark.parametrize("encoding", [0, 2, 3, 8])
def test_bad_encoding_rejected(encoding):
    with pytest.raises(ValueError, match="only 1"):
        encode_frame(_canvas(SMALL), panel=SMALL, encoding=encoding,
                     dithering=Dithering.NONE)


def test_dithering_default_rejected():
    with pytest.raises(ValueError, match="DitheringType 0"):
        encode_frame(_canvas(SMALL), panel=SMALL, encoding=4, dithering=0)


def test_unknown_dithering_rejected():
    with pytest.raises(ValueError, match="unknown DitheringType"):
        encode_frame(_canvas(SMALL), panel=SMALL, encoding=4, dithering=7)


def test_wrong_image_size_rejected():
    with pytest.raises(ValueError, match="panel canvas"):
        encode_frame(np.zeros((10, 10), dtype=np.uint8), panel=SMALL,
                     encoding=4, dithering=Dithering.NONE)


def test_colour_mask_driver_rejected():
    panel = Panel(width=64, height=32, displays=1,
                  driver=DRIVERS["eink-32-inch-color-mask"], interlace=0)
    with pytest.raises(NotImplementedError, match="colour-mask"):
        encode_frame(_canvas(panel), panel=panel, encoding=4,
                     dithering=Dithering.NONE)


def test_pil_input_is_accepted():
    from PIL import Image

    img = Image.fromarray(_canvas(SMALL), "L").convert("RGB")
    frame = _encode(img, panel=SMALL, encoding=4, dithering=Dithering.NONE)
    assert frame.nr_primitives == SMALL.displays


def test_imaging_imports_nothing_that_does_io():
    """The sans-io contract, asserted mechanically on the import graph.

    ``imaging/`` must not reach the network, the clock or a global RNG.  The
    one permitted file read is :mod:`importlib.resources` loading the
    committed blue-noise matrix, which is package data, not I/O against the
    outside world.
    """
    import ast
    import pathlib

    import pyvisionect.imaging as pkg

    forbidden = {"socket", "asyncio", "time", "datetime", "random",
                 "threading", "subprocess", "requests", "urllib", "http",
                 "selectors", "ssl"}
    root = pathlib.Path(pkg.__file__).parent
    offenders: list[str] = []
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or "").split(".")[0]]
            else:
                continue
            for name in names:
                if name in forbidden:
                    offenders.append(f"{path.name}: {name}")
    assert not offenders, offenders


def test_encoding_is_deterministic():
    """Same inputs, same bytes -- no hidden RNG or clock dependence."""
    img = _canvas(SMALL)
    a = _encode(img, panel=SMALL, encoding=4, dithering=Dithering.BLUE_NOISE)
    b = _encode(img, panel=SMALL, encoding=4, dithering=Dithering.BLUE_NOISE)
    assert [r.data for r in a.rectangles] == [r.data for r in b.rectangles]
    assert a.state_checksum == b.state_checksum


def test_input_image_is_not_mutated():
    img = _canvas(SMALL)
    before = img.copy()
    _encode(img, panel=SMALL, encoding=4, dithering=Dithering.BLUE_NOISE)
    assert np.array_equal(img, before)


def test_prev_state_is_not_mutated():
    img = _canvas(SMALL)
    first = _encode(img, panel=SMALL, encoding=4, dithering=Dithering.NONE)
    snapshot = first.state.state_grey.copy()
    changed = img.copy()
    changed[4:12, 8:24] = 0
    _encode(changed, panel=SMALL, encoding=4, dithering=Dithering.NONE,
            prev_state=first.state)
    assert np.array_equal(first.state.state_grey, snapshot)
