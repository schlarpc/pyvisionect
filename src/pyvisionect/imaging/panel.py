"""Panel / display geometry and driver capability tables.

.. note::
   These dataclasses are defined here so that ``imaging/`` stays importable
   with nothing but numpy and Pillow -- no dependency on
   :mod:`pyvisionect.devices`, :mod:`pyvisionect.wire` or
   :mod:`pyvisionect.session`.

   :mod:`pyvisionect.devices.panels` defines its own ``Panel``/``Driver`` for
   the capability tables.  :meth:`Panel.adapt` accepts those (or anything else
   exposing ``width``/``height``/``displays`` plus a ``driver`` name or a
   ``driver_spec``), so callers can pass ``devices.panel_for(display_type)``
   straight into :func:`pyvisionect.imaging.encode_frame` and this module never
   has to import it.

Spec references are to ``artifacts/visionect/02-imaging-framebuffer.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

__all__ = ["Driver", "Panel", "DRIVERS", "PANEL_32INCH", "DisplayGeometry"]


@dataclass(frozen=True)
class Driver:
    """The *entire* per-device pixel-path variability -- spec §1.6.

    The vendor registers five drivers; they differ only in two booleans, which
    come straight out of each driver's ``Settings()`` map:

    ===============================  ========  ==========
    ``Name()``                       Mirroring Color
    ===============================  ========  ==========
    ``eink-generic``                 false     false
    ``eink-flip``                    **true**  false
    ``pl-generic``                   false     false
    ``eink-32-inch-color-mask``      false     true
    ``eink-32-inch-color-mask-flip`` **true**  true
    ===============================  ========  ==========

    ``eInkFlip`` is literally ``eInkGeneric`` wrapped so that the four ops
    ``image-encode``, ``image-decode``, ``rectangle-encode`` and
    ``rectangle-decode`` apply a horizontal flip (``imgproc.CvFlip``) --
    ``eink-flip.go:17/167/176``.

    :param mirror: horizontally mirror each display's image before encoding.
    :param color_mask: colour-mask panel (palette remap via ``GmMapImage``).
        **Not implemented** -- the grey path ignores it; passing ``True``
        raises from :func:`pyvisionect.imaging.encode_frame`.
    """

    mirror: bool = False
    color_mask: bool = False
    name: str = ""


#: Vendor driver registry (``vss/pkg/driver/driver.go:46-59``).  Lookup falls
#: back to ``eink-generic`` for ``""`` and ``"unknown"``
#: (``common.Display.GetDriver``, ``display-translations.go:54-58``).
DRIVERS: Mapping[str, Driver] = {
    "eink-generic": Driver(mirror=False, color_mask=False, name="eink-generic"),
    "eink-flip": Driver(mirror=True, color_mask=False, name="eink-flip"),
    "pl-generic": Driver(mirror=False, color_mask=False, name="pl-generic"),
    "eink-32-inch-color-mask": Driver(
        mirror=False, color_mask=True, name="eink-32-inch-color-mask"
    ),
    "eink-32-inch-color-mask-flip": Driver(
        mirror=True, color_mask=True, name="eink-32-inch-color-mask-flip"
    ),
}


def resolve_driver(driver: Driver | str | None) -> Driver:
    """Accept a :class:`Driver`, a vendor driver name, ``None`` or ``"unknown"``.

    Unknown names fall back to ``eink-generic``, exactly as ``driver.Get``
    does (``driver.go:53-59``).
    """
    if isinstance(driver, Driver):
        return driver
    if not driver or driver == "unknown":
        return DRIVERS["eink-generic"]
    return DRIVERS.get(driver, DRIVERS["eink-generic"])


@dataclass(frozen=True)
class DisplayGeometry:
    """One *logical* display's place in the session canvas -- ``common.Display``."""

    id: int
    x: int
    y: int
    width: int
    height: int
    rotation: int = 0

    @property
    def rotated_width(self) -> int:
        """``Display.DimensionsRotated()`` -- ``display-translations.go:62``."""
        return self.height if self.rotation in (1, 3) else self.width

    @property
    def rotated_height(self) -> int:
        return self.width if self.rotation in (1, 3) else self.height


@dataclass(frozen=True)
class Panel:
    """A panel's logical geometry, as the *server* models it.

    The numbers here are the **post-** ``SetupDisplaysType`` values, not the
    device's own ``DisplayWidth``/``DisplayHeight`` status fields.  For the 32"
    sign the device reports ``2880 x 640`` with ``NumSupportedDisplays = 2``,
    and ``device-config/config.go:262`` rewrites that into **4 logical
    displays of 1440 x 640**, stacked vertically (spec §1.5).  The two physical
    2880-wide panel channels are then rebuilt on the wire by the interlacer
    (spec §1.6, :mod:`pyvisionect.imaging.interlace`).

    :param width: one logical display's width in pixels.
    :param height: one logical display's height in pixels.
    :param displays: number of logical displays, stacked vertically.
    :param driver: a :class:`Driver`, or a vendor driver name string.
    :param rotation: ``common.DisplayRotation`` 0/1/2/3 == 0/90/180/270,
        applied to every display.  Only ``0`` is verified against real captured
        traffic; 1/2/3 implement §1.5's formulas but are untested on hardware.
    :param interlace: transmit-side interlacing mode (``getInterlacingMode``,
        ``image_interlacing.go:92``).  ``None`` means *auto*: mode ``2`` when
        ``displays == 4``, else ``0`` (no interlacing).  That equivalence holds
        because ``SetupDisplaysType`` only ever produces 4 displays for
        ``HardwareNameID == 8``, which is exactly the hardware
        ``getInterlacingMode`` returns non-zero for.  Pass an explicit value to
        override (``1`` is hardware revision 1.0.0 and is **not implemented** --
        its pairing was not recovered).
    """

    width: int
    height: int
    displays: int
    driver: Driver | str = field(default_factory=lambda: DRIVERS["eink-generic"])
    rotation: int = 0
    interlace: int | None = None

    # -- interop ----------------------------------------------------------

    @classmethod
    def adapt(cls, panel: "Panel | Any") -> "Panel":
        """Coerce any panel-like object into an imaging :class:`Panel`.

        Accepts an imaging :class:`Panel` unchanged.  Otherwise reads
        ``width``, ``height``, ``displays`` and a driver -- from ``driver``
        (a :class:`Driver` or a vendor name string) or from a ``driver_spec``
        attribute, which is what :class:`pyvisionect.devices.panels.Panel`
        exposes.  ``rotation`` and ``interlace`` are taken if present and
        default to ``0`` / auto.
        """
        if isinstance(panel, cls):
            return panel
        try:
            width = int(panel.width)
            height = int(panel.height)
            displays = int(panel.displays)
        except AttributeError as exc:
            raise TypeError(
                f"{panel!r} is not panel-like: expected width/height/displays"
            ) from exc
        driver = getattr(panel, "driver", None)
        if not isinstance(driver, (Driver, str, type(None))):
            driver = None
        if driver is None:
            spec = getattr(panel, "driver_spec", None)
            if spec is not None:
                driver = Driver(mirror=bool(spec.mirror),
                                color_mask=bool(spec.color_mask),
                                name=str(getattr(spec, "name", "")))
        return cls(
            width=width,
            height=height,
            displays=displays,
            driver=driver,
            rotation=int(getattr(panel, "rotation", 0) or 0),
            interlace=getattr(panel, "interlace", None),
        )

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("panel width and height must be positive")
        if self.displays <= 0:
            raise ValueError("panel must have at least one display")
        if self.rotation not in (0, 1, 2, 3):
            raise ValueError(f"bad display rotation {self.rotation!r}; expected 0..3")
        object.__setattr__(self, "driver", resolve_driver(self.driver))

    # -- derived geometry --------------------------------------------------

    @property
    def rotated_width(self) -> int:
        return self.height if self.rotation in (1, 3) else self.width

    @property
    def rotated_height(self) -> int:
        return self.width if self.rotation in (1, 3) else self.height

    @property
    def canvas_width(self) -> int:
        """Session-canvas width -- ``Displays.Borders()``, spec §1.5."""
        return self.rotated_width

    @property
    def canvas_height(self) -> int:
        """Session-canvas height: the displays are stacked vertically.

        ``Config.ParsePageLayout()`` arrangement ``2`` (``PageLayout: "0"``) is
        the vertical stack we observe on the sign (``config.go:273-280``).
        Other arrangements are not implemented.
        """
        return self.rotated_height * self.displays

    @property
    def canvas_shape(self) -> tuple[int, int]:
        """``(height, width)`` -- numpy order."""
        return (self.canvas_height, self.canvas_width)

    def geometry(self) -> tuple[DisplayGeometry, ...]:
        """The logical displays, top to bottom, ids ``0 .. displays-1``."""
        return tuple(
            DisplayGeometry(
                id=i,
                x=0,
                y=i * self.rotated_height,
                width=self.width,
                height=self.height,
                rotation=self.rotation,
            )
            for i in range(self.displays)
        )

    @property
    def interlace_mode(self) -> int:
        """Resolved interlacing mode (see the ``interlace`` parameter)."""
        if self.interlace is not None:
            return self.interlace
        return 2 if self.displays == 4 else 0

    @property
    def screens(self) -> int:
        """Number of *physical* panel channels the wire addresses.

        Without interlacing this is just the logical display count: one
        ``ScreenID`` per display.  With the mode-2 fold, two logical displays
        share one physical ``2880 x 640`` channel, so it is ``displays // 2``.
        """
        return self.displays // 2 if self.interlace_mode else self.displays

    @property
    def screen_width(self) -> int:
        """Width of one physical channel -- what ``RectangleHeader.X`` indexes.

        ``2 x 1440 = 2880`` on the interlaced sign, because a screen row is
        two logical displays' rows interleaved 4 pixels at a time.
        """
        return self.rotated_width * 2 if self.interlace_mode else self.rotated_width

    @property
    def screen_height(self) -> int:
        """Height of one physical channel.  The fold is columns-only."""
        return self.rotated_height

    @property
    def supports_screen_rectangles(self) -> bool:
        """Whether a **screen-space** partial rectangle is expressible here.

        This is the complement of :attr:`forces_full_screen`, which is about
        *canvas*-space rectangles.  A canvas rectangle cannot survive the fold;
        a screen rectangle never enters it.  See
        :func:`pyvisionect.imaging.partial.encode_partial_frame`.

        True only for interlacing mode 2 at rotation 0 on a greyscale driver:

        * mode 1's lane pairing was never recovered (:data:`INTERLACE_PAIRS`),
          so there is no map to address it with;
        * mode 0 needs no screen-space path at all -- a canvas rectangle *is*
          the wire rectangle there, and
          :func:`~pyvisionect.imaging.encoder.encode_frame` already emits one;
        * a rotated display would have to rotate each lane's sub-tile
          independently and no rotated interlaced hardware exists to check it
          against;
        * colour-mask drivers are not implemented at all;
        * and a logical display whose width is not a whole number of 4-pixel
          interleave groups has no screen column range that covers a whole
          number of lane groups, so there is nothing legal to address.

        .. warning::
           True here means "the library can build the bytes", **not** "your
           device will draw them".  Only ``HardwareNameID 8`` hardware was
           measured accepting these (``OPEN-QUESTIONS.md`` A10).
        """
        return (
            self.interlace_mode == 2
            and self.rotation == 0
            and self.rotated_width % 4 == 0
            and not self.driver.color_mask  # type: ignore[union-attr]
        )

    @property
    def forces_full_screen(self) -> bool:
        """True when every update must be a full-screen one.

        ``getRectangleSupport`` returns ``false`` unconditionally for
        ``HardwareNameID == 8`` (``vss/cmd/engine/client.go:41-42``), and for a
        **canvas-space** rectangle the reason is structural:
        :func:`interlace_payloads` needs all four full-size display rectangles,
        so a partial one cannot be interlaced (spec §1.6, §1.7).  Neither
        ``ForceRectangleSupport`` nor ``MergeRegions=true`` can turn it on.

        .. note::
           The device itself is not the obstacle.  A rectangle addressed in
           **screen** coordinates never enters the fold, and the 31.2" sign was
           measured accepting those on 2026-10-05 (``OPEN-QUESTIONS.md`` A10).
           :func:`~pyvisionect.imaging.encode_frame` has no screen-space path,
           so this property stays True for it.
        """
        return self.interlace_mode != 0


#: Our live device: ``00112233-4455-6677-8899-aabb00000000``, ``HardwareNameID``
#: 8, hardware 1.1.0, driver ``eink-flip``.  Server-side geometry is 4 x
#: 1440 x 640 stacked (canvas 1440 x 2560); the wire carries 2 interlaced
#: rectangles of 2880 x 640.  Spec §0, §1.5, §1.6.
PANEL_32INCH = Panel(
    width=1440,
    height=640,
    displays=4,
    driver=DRIVERS["eink-flip"],
    rotation=0,
    interlace=None,  # auto -> 2
)
