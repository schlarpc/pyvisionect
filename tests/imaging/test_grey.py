"""Grey conversion and the optional Beautify stage.  Spec §3.3, §4.3."""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from pyvisionect.imaging import beautify, to_grey8


def test_uint8_2d_passes_through():
    a = np.arange(256, dtype=np.uint8).reshape(16, 16)
    out = to_grey8(a)
    assert np.array_equal(out, a) and out.dtype == np.uint8


def test_rgb_uses_itu_601_luma():
    """Go's ``color.GrayModel``: ``(19595r + 38470g + 7471b + 1<<15) >> 16``."""
    px = np.array([[[255, 0, 0], [0, 255, 0], [0, 0, 255], [255, 255, 255]]],
                  dtype=np.uint8)
    out = to_grey8(px)
    expect = [(19595 * 255 + (1 << 15)) >> 16,
              (38470 * 255 + (1 << 15)) >> 16,
              (7471 * 255 + (1 << 15)) >> 16,
              255]
    assert out.tolist()[0] == expect


def test_rgba_ignores_alpha():
    rgb = np.array([[[10, 20, 30]]], dtype=np.uint8)
    rgba = np.array([[[10, 20, 30, 7]]], dtype=np.uint8)
    assert np.array_equal(to_grey8(rgb), to_grey8(rgba))


def test_single_channel_3d():
    a = np.arange(16, dtype=np.uint8).reshape(4, 4, 1)
    assert np.array_equal(to_grey8(a), a[:, :, 0])


def test_pil_modes():
    a = np.arange(256, dtype=np.uint8).reshape(16, 16)
    assert np.array_equal(to_grey8(Image.fromarray(a, "L")), a)
    assert to_grey8(Image.fromarray(a, "L").convert("RGB")).shape == (16, 16)
    assert to_grey8(Image.fromarray(a, "L").convert("1")).dtype == np.uint8


def test_float_inputs_are_scaled():
    assert to_grey8(np.array([[0.0, 0.5, 1.0]])).tolist() == [[0, 128, 255]]
    assert to_grey8(np.array([[0.0, 128.0, 255.0]])).tolist() == [[0, 128, 255]]


def test_bad_shapes_raise():
    with pytest.raises(ValueError):
        to_grey8(np.zeros((2, 2, 5), dtype=np.uint8))
    with pytest.raises(ValueError):
        to_grey8(np.zeros((2, 2, 2, 2), dtype=np.uint8))


def test_beautify_stretches_the_range():
    a = np.array([[100, 110, 120, 130]], dtype=np.uint8)
    out = beautify(a, gamma=1.0)
    assert out.min() == 0 and out.max() == 255


def test_beautify_handles_a_flat_image():
    a = np.full((4, 4), 77, dtype=np.uint8)
    out = beautify(a, gamma=1.0)
    assert out.shape == a.shape and out.dtype == np.uint8


def test_beautify_gamma_brightens_midtones():
    """gamma 1.1 applies ``v ** (1/gamma)``, which raises mid greys."""
    a = np.array([[0, 128, 255]], dtype=np.uint8)
    assert beautify(a, gamma=1.1)[0, 1] > 128


def test_beautify_is_off_by_default_in_the_encoder():
    from pyvisionect.imaging import Dithering, Panel, encode_frame

    panel = Panel(16, 16, 1, "eink-generic", interlace=0)
    low = np.full(panel.canvas_shape, 100, dtype=np.uint8)
    low[0, 0] = 110
    plain = encode_frame(low, panel=panel, encoding=4, dithering=Dithering.NONE)
    pretty = encode_frame(low, panel=panel, encoding=4,
                          dithering=Dithering.NONE, beautify=True)
    assert plain.rectangles[0].data != pretty.rectangles[0].data
