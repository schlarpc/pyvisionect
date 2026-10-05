"""Panel geometry and display drivers -- data, not subclasses.

The vendor covers eleven hardware families with one code path by deriving
nothing from the model, and ``driver.driverList`` has exactly **five** drivers
whose entire per-device variability is two booleans plus a pixel filter.  We
mirror that.

``DisplayType`` must stay an **opaque uint32**.  The live 32" sign reports
``0xC2050128``, which is *not* in the vendor's enum (it falls through to
``DISPLAY_UNKNOWN``) and yet is a perfectly valid firmware-service key.  So:
look the raw value up, fall back to a default row, and take *capabilities* from
the device's own ``PV2Features`` advertisement -- never from the model.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

__all__ = [
    "Driver",
    "DRIVERS",
    "Panel",
    "PANELS",
    "DEFAULT_PANEL",
    "DISPLAY_TYPE_NAMES",
    "DISPLAY_TYPE_DRIVER",
    "panel_for",
    "driver_for",
]


@dataclass(frozen=True, slots=True)
class Driver:
    """The entire per-device variability in the vendor's display layer.

    Args:
        name: the vendor's driver-class string.
        mirror: horizontal mirror. True for the ``*-flip`` drivers; the live 32"
            sign is ``eink-flip``, and an un-mirrored push lands backwards.
        color_mask: the 32" colour-mask pixel filter.
    """

    name: str
    mirror: bool
    color_mask: bool


DRIVERS: dict[str, Driver] = {
    "eink-generic": Driver("eink-generic", mirror=False, color_mask=False),
    "eink-flip": Driver("eink-flip", mirror=True, color_mask=False),
    "pl-generic": Driver("pl-generic", mirror=False, color_mask=False),
    "eink-32-inch-color-mask": Driver(
        "eink-32-inch-color-mask", mirror=False, color_mask=True
    ),
    "eink-32-inch-color-mask-flip": Driver(
        "eink-32-inch-color-mask-flip", mirror=True, color_mask=True
    ),
    # Named by DISPLAY_TYPE_DRIVER but not in driverList; treated as generic.
    "eink-42-v2": Driver("eink-42-v2", mirror=False, color_mask=False),
    "eink-42-flip": Driver("eink-42-flip", mirror=True, color_mask=False),
    "no display": Driver("no display", mirror=False, color_mask=False),
    "unknown": Driver("unknown", mirror=False, color_mask=False),
}


@dataclass(frozen=True, slots=True)
class Panel:
    """Geometry the server models for a given raw ``DisplayType``.

    ``width``/``height`` are the dimensions of **one virtual display**; the full
    canvas is ``displays`` of them stacked.  For the 32" sign the server models
    ``4 x 1440x640`` (canvas 1440x2560) and then the ``eink-flip`` driver's
    interlacing step emits **two** rectangles of ``2880x640`` on the wire, with
    ``ScreenID`` 0 and 1.  Both the repack and the mirror have to be reproduced
    or the image lands garbled.
    """

    width: int
    height: int
    displays: int
    driver: str
    display_type: int | None = None
    """The raw ``DisplayType`` this row was looked up with, when known."""

    is_default: bool = False
    """True when this is the fallback row rather than a recognised panel.

    Worth surfacing in diagnostics: our sign's ``0xC2050128`` is not in the
    vendor's enum at all, so "unrecognised panel, assuming 2880x640" is an
    honest thing to tell a user, and silently using a guess is not. Capabilities
    still come from the device's own ``PV2Features``, not from this.
    """

    @property
    def name(self) -> str:
        """The firmware-service name, or ``DISPLAY_UNKNOWN``."""
        if self.display_type is None:
            return "DISPLAY_UNSPECIFIED"
        return DISPLAY_TYPE_NAMES.get(self.display_type, "DISPLAY_UNKNOWN")

    def describe(self) -> str:
        """A one-line human description, for diagnostics."""
        prefix = "unrecognised panel, assuming" if self.is_default else self.name
        return (
            f"{prefix} {self.canvas_width}x{self.canvas_height} "
            f"({self.displays} x {self.width}x{self.height}, driver {self.driver})"
        )

    @property
    def canvas_width(self) -> int:
        return self.width

    @property
    def canvas_height(self) -> int:
        return self.height * self.displays

    @property
    def driver_spec(self) -> Driver:
        return DRIVERS.get(self.driver, DRIVERS["unknown"])


DISPLAY_TYPE_NAMES: dict[int, str] = {
    0: "DISPLAY_NONE",
    1: "DISPLAY_EINK_6_0",
    2: "DISPLAY_EINK_9_7",
    3: "DISPLAY_EINK_13_3",
    4: "DISPLAY_PLASTIC_LOGIC_10_7",
    5: "DISPLAY_EINK_30_0",
    6: "DISPLAY_EINK_6_0_NEW",
    7: "DISPLAY_EINK_9_7_C228",
    8: "DISPLAY_EINK_6_0_C219",
    9: "DISPLAY_EINK_6_0_C246",
    10: "DISPLAY_EINK_9_7_C242",
    11: "DISPLAY_EINK_9_7_HVOLT",
    12: "DISPLAY_EINK_13_3_C222",
    13: "DISPLAY_EINK_31_2_D039",
    14: "DISPLAY_EINK_31_2_D041",
    15: "DISPLAY_EINK_31_2_R064",
    16: "DISPLAY_EINK_31_2_W001",
    17: "DISPLAY_EINK_31_2_C247",
    0x60110154: "DISPLAY_EINK_42_V2",
    0x60110191: "DISPLAY_EINK_42_V2",
    0x80000000: "DISPLAY_EINK_42",
    0xA30500F3: "DISPLAY_EINK_9_7_C243",
    0xAD0500F3: "DISPLAY_EINK_13_3_C243",
}
"""``status.(*DisplayType).FwServerString`` @ ``0x69baa0``.

Anything absent is ``DISPLAY_UNKNOWN`` -- including our sign's ``0xC2050128``.
"""

DISPLAY_TYPE_DRIVER: dict[int, str] = {
    0: "no display",
    1: "eink-generic",
    2: "eink-generic",
    3: "eink-generic",
    4: "pl-generic",
    5: "eink-flip",
    6: "eink-generic",
    7: "eink-generic",
    8: "eink-generic",
    9: "eink-generic",
    10: "eink-generic",
    11: "eink-generic",
    12: "eink-generic",
    13: "eink-flip",
    14: "eink-flip",
    15: "eink-flip",
    16: "eink-flip",
    17: "eink-flip",
    0x60110154: "eink-42-v2",
    0x60110191: "eink-42-v2",
    0x7FFFFFFD: "eink-generic",
    0x80000000: "eink-42-flip",
    0xA30500F3: "eink-generic",
    0xAD0500F3: "eink-generic",
}
"""``status.(*DisplayType).String`` @ ``0x69b9c0`` -- the driver class."""


DEFAULT_PANEL = Panel(
    width=1440, height=640, displays=4, driver="eink-flip", is_default=True
)
"""The ``default:`` arm of ``SetupDisplaysType``.

The vendor's switch sends ``{5, 13, 14, 15, 16, 17, default}`` to
``1440x640 forced to 4 displays``, logging
``"We expect 4 virtual displays for one 32\\" eink display!"`` and rewriting a
device-reported ``2880x640`` into ``4 x 1440x640``.  Our sign's unknown
``0xC2050128`` lands here, which is why it works at all.
"""

PANELS: dict[int, Panel] = {
    0x7FFFFFFD: Panel(80, 32, 1, "eink-generic"),
    1: Panel(600, 800, 1, "eink-generic"),
    6: Panel(1024, 758, 1, "eink-generic"),
    2: Panel(1200, 825, 1, "eink-generic"),
    7: Panel(1200, 825, 1, "eink-generic"),
    8: Panel(1200, 825, 1, "eink-generic"),
    9: Panel(1200, 825, 1, "eink-generic"),
    10: Panel(1200, 825, 1, "eink-generic"),
    11: Panel(1200, 825, 1, "eink-generic"),
    0xA30500F3: Panel(1200, 825, 1, "eink-generic"),
    3: Panel(1600, 1200, 1, "eink-generic"),
    12: Panel(1600, 1200, 1, "eink-generic"),
    0xAD0500F3: Panel(1600, 1200, 1, "eink-generic"),
    4: Panel(1280, 960, 1, "pl-generic"),
    5: Panel(1440, 640, 4, "eink-flip"),
    13: Panel(1440, 640, 4, "eink-flip"),
    14: Panel(1440, 640, 4, "eink-flip"),
    15: Panel(1440, 640, 4, "eink-flip"),
    16: Panel(1440, 640, 4, "eink-flip"),
    17: Panel(1440, 640, 4, "eink-flip"),
}
"""Keyed on the **raw uint32** ``DisplayType``, with :data:`DEFAULT_PANEL` as the
default row.  Geometry from ``SetupDisplaysType``."""


def panel_for(display_type: int) -> Panel:
    """Panel geometry for a raw ``DisplayType``, defaulting to the 32" 4x1440x640 row.

    The returned :class:`Panel` carries the ``display_type`` it was looked up
    with and an :attr:`Panel.is_default` flag, so a caller can say
    "unrecognised panel, assuming 2880x640" instead of silently guessing.
    """
    found = PANELS.get(display_type)
    if found is None:
        return replace(DEFAULT_PANEL, display_type=display_type)
    return replace(found, display_type=display_type)


def driver_for(display_type: int) -> Driver:
    """Driver spec for a raw ``DisplayType``."""
    name = DISPLAY_TYPE_DRIVER.get(display_type)
    if name is None:
        name = panel_for(display_type).driver
    return DRIVERS.get(name, DRIVERS["unknown"])


def display_type_name(display_type: int) -> str:
    """Firmware-service name for a raw ``DisplayType``, or ``DISPLAY_UNKNOWN``."""
    return DISPLAY_TYPE_NAMES.get(display_type, "DISPLAY_UNKNOWN")
