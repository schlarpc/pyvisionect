"""Panel / driver tables and derived geometry.  Spec §1.5, §1.6, §1.7."""

from __future__ import annotations

import pytest

from pyvisionect.imaging import DRIVERS, PANEL_32INCH, Driver, Panel
from pyvisionect.imaging.panel import resolve_driver


def test_driver_table_matches_the_vendor_settings_maps():
    expected = {
        "eink-generic": (False, False),
        "eink-flip": (True, False),
        "pl-generic": (False, False),
        "eink-32-inch-color-mask": (False, True),
        "eink-32-inch-color-mask-flip": (True, True),
    }
    assert {k: (v.mirror, v.color_mask) for k, v in DRIVERS.items()} == expected


@pytest.mark.parametrize("name", ["", "unknown", "nope", None])
def test_unknown_driver_falls_back_to_eink_generic(name):
    assert resolve_driver(name) is DRIVERS["eink-generic"]


def test_panel_accepts_a_driver_name_or_object():
    assert Panel(10, 10, 1, "eink-flip").driver is DRIVERS["eink-flip"]
    assert Panel(10, 10, 1, DRIVERS["eink-flip"]).driver is DRIVERS["eink-flip"]
    assert isinstance(Panel(10, 10, 1).driver, Driver)


def test_32inch_geometry():
    """4 x 1440x640 stacked -> canvas 1440x2560 -- ``Displays.Borders``."""
    p = PANEL_32INCH
    assert (p.width, p.height, p.displays) == (1440, 640, 4)
    assert p.driver.mirror is True
    assert (p.canvas_width, p.canvas_height) == (1440, 2560)
    assert p.canvas_shape == (2560, 1440)
    assert [(d.id, d.x, d.y) for d in p.geometry()] == \
        [(0, 0, 0), (1, 0, 640), (2, 0, 1280), (3, 0, 1920)]


def test_32inch_interlacing_and_rectangle_support():
    assert PANEL_32INCH.interlace_mode == 2
    assert PANEL_32INCH.forces_full_screen is True


def test_non_four_display_panels_do_not_interlace():
    assert Panel(600, 800, 1).interlace_mode == 0
    assert Panel(600, 800, 1).forces_full_screen is False
    assert Panel(600, 800, 2).interlace_mode == 0


def test_interlace_can_be_overridden():
    assert Panel(1440, 640, 4, interlace=0).interlace_mode == 0
    assert Panel(1440, 640, 4, interlace=0).forces_full_screen is False


@pytest.mark.parametrize("rotation,shape", [
    (0, (1600, 600)), (1, (2400, 400)), (2, (1600, 600)), (3, (2400, 400)),
])
def test_rotation_swaps_the_canvas_dimensions(rotation, shape):
    """``Display.DimensionsRotated`` -- ``display-translations.go:62``."""
    p = Panel(600, 400, 4, rotation=rotation)
    assert p.canvas_shape == shape


@pytest.mark.parametrize("kwargs", [
    dict(width=0, height=10, displays=1),
    dict(width=10, height=-1, displays=1),
    dict(width=10, height=10, displays=0),
    dict(width=10, height=10, displays=1, rotation=4),
])
def test_invalid_panels_raise(kwargs):
    with pytest.raises(ValueError):
        Panel(**kwargs)


def test_panel_is_frozen():
    import dataclasses

    with pytest.raises(dataclasses.FrozenInstanceError):
        PANEL_32INCH.width = 1  # type: ignore[misc]


def test_adapt_accepts_the_devices_panel():
    """Interop: ``devices.panel_for(display_type)`` goes straight in.

    :mod:`pyvisionect.devices.panels` has its own ``Panel`` with ``driver`` as
    a *string* and no rotation/interlace fields.  :meth:`Panel.adapt` has to
    bridge it without ``imaging/`` importing ``devices/``.
    """
    from pyvisionect.devices.panels import panel_for

    adapted = Panel.adapt(panel_for(0xC2050128))  # the live sign's DisplayType
    assert (adapted.width, adapted.height, adapted.displays) == (1440, 640, 4)
    assert adapted.driver.mirror is True
    assert adapted.rotation == 0
    assert adapted.interlace_mode == 2
    assert adapted.canvas_shape == (2560, 1440)


def test_adapt_is_idempotent():
    assert Panel.adapt(PANEL_32INCH) is PANEL_32INCH


def test_adapt_rejects_non_panels():
    with pytest.raises(TypeError, match="panel-like"):
        Panel.adapt(object())


def test_adapt_reads_a_driver_spec():
    class Fake:
        width, height, displays = 100, 50, 2
        driver_spec = Driver(mirror=True, color_mask=False, name="x")

    assert Panel.adapt(Fake()).driver.mirror is True
