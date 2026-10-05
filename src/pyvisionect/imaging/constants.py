"""Protocol constants for the Visionect imaging path.

Every value here is cited to a section of
``artifacts/visionect/02-imaging-framebuffer.md`` (the reverse-engineering
report), which in turn cites the vendor's Go/C sources by file:line.
"""

from __future__ import annotations

from enum import IntEnum

__all__ = [
    "Encoding",
    "Dithering",
    "ImageType",
    "BITS_PER_PIXEL",
    "ENCODING_LEVELS",
    "LEVEL_STEP",
    "ALIGN_QUANTUM",
    "RECTANGLE_UPDATE_OPTIONS_DEFAULT",
    "OPTION_NORMAL_UPDATE",
    "MAX_REGIONS_PER_DISPLAY",
    "MAX_NO_FULL_UPDATE",
    "MERGE_THRESHOLD_PERCENT",
    "MERGE_THRESHOLD_STEP_PERCENT",
    "VENDOR_DITHERING",
]


class Encoding(IntEnum):
    """``proto.EncodingType`` -- spec §2.1.

    Only ``1`` and ``4`` exist.  ``0`` is ``"undefined"`` and is rejected by
    the vendor's ``updateDefaultRectUnlocked`` with ``render.ErrEncoding``;
    anything else is ``"unknown"`` and rejected by
    ``driver.isDividableTo16BitQuants``.
    """

    ONE_BIT = 1
    FOUR_BIT = 4


class Dithering(IntEnum):
    """``common.DitheringType`` -- spec §3.1, plus one library extension.

    ``0``..``3`` are the vendor's values (``common/types.go:123``):

    * ``DEFAULT`` (0) -- *invalid at the encoder*.  The vendor's
      ``updateDefaultRectUnlocked`` returns ``render.ErrDithering`` for it
      (``image-state.go:754``), and so do we.
    * ``NONE`` (1) -- no dithering: plain truncation of the top nibble (4 bpp)
      or an MSB threshold at 128 (1 bpp).
    * ``BAYER`` (2) -- GraphicsMagick ``OrderedDitherImage``.
    * ``FLOYD_STEINBERG`` (3) -- GraphicsMagick ``QuantizeImage(dither=1)``.

    ``BLUE_NOISE`` (4) is **ours, not the vendor's.**  It is safe to invent a
    value here because *the dithering mode never appears on the wire*: the
    device receives already-quantised, already-bit-packed 1 bpp/4 bpp data and
    performs only waveform drive (spec §3.3).  ``DitheringType`` is a
    server-side-only knob, and we replace the server, so the enum is ours to
    extend.  Nothing downstream of :func:`pyvisionect.imaging.encode_frame`
    ever sees this number.
    """

    DEFAULT = 0
    NONE = 1
    BAYER = 2
    FLOYD_STEINBERG = 3
    BLUE_NOISE = 4


#: The subset of :class:`Dithering` that the vendor server understands.  Values
#: outside this set are library extensions (see :class:`Dithering`).
VENDOR_DITHERING = frozenset({Dithering.DEFAULT, Dithering.NONE, Dithering.BAYER,
                              Dithering.FLOYD_STEINBERG})


class ImageType(IntEnum):
    """``proto.VimageType`` -- spec §2.2.  Only ``GRAY`` is meaningful."""

    UNKNOWN = 0
    GRAY = 1


#: Bits per pixel on the wire, per encoding.  Spec §2.3.
BITS_PER_PIXEL: dict[int, int] = {Encoding.ONE_BIT: 1, Encoding.FOUR_BIT: 4}

#: Grey levels per encoding -- ``driver.encodingLevels``, ``encode.go:17``:
#: ``2 ** (1 if enc == 1 else 4)``.  Spec §2.1.
ENCODING_LEVELS: dict[int, int] = {Encoding.ONE_BIT: 2, Encoding.FOUR_BIT: 16}

#: Distance between adjacent output levels in 8-bit grey.  4 bpp nibble ``n``
#: decodes to ``n * 17`` (``decode_four_to_eight``), 1 bpp bit ``b`` decodes to
#: ``b * 255`` (``decode_one_to_eight``).  Spec §2.3.
LEVEL_STEP: dict[int, int] = {Encoding.ONE_BIT: 255, Encoding.FOUR_BIT: 17}

#: Sub-pixels per 16-bit word -- ``noSubpixelsIn16BitQuant``,
#: ``sixteen-bit-limitation.go:15``: ``16 // bpp``.  The constraint is on
#: ``width * height``, not on width alone, because the packer runs linearly
#: over the whole rectangle with no row padding.  Spec §2.4.
ALIGN_QUANTUM: dict[int, int] = {Encoding.ONE_BIT: 16, Encoding.FOUR_BIT: 4}

#: ``packet.(*RectangleHeader).MarshalBinaryTo`` substitutes these when
#: ``RectangleUpdateOptions == 0`` (``packet/image.go:271-277``).  Spec §1.2.
RECTANGLE_UPDATE_OPTIONS_DEFAULT: dict[int, int] = {
    Encoding.ONE_BIT: 0x0101,
    Encoding.FOUR_BIT: 0x0102,
}

#: ``RectangleHeader.Options`` bit 1 is the "normal update" bit.  The server
#: *clears* it on every rectangle of one full-screen packet to request an
#: inverse / ghost-clearing refresh (``image-state.go:1519-1522``).  Spec §1.4,
#: §5.
OPTION_NORMAL_UPDATE = 0x0002

#: ``maxRegions`` -- hard constant in ``displayRects.update``
#: (``image-state.go:221``).  Spec §4.1.
MAX_REGIONS_PER_DISPLAY = 15

#: ``noFullUpdateMax`` -- at most this many partial updates between full ones
#: (``vss/cmd/engine/client.go:217``).  Informational; the caller owns the
#: counter, this module is stateless about it.  Spec §1.8.
MAX_NO_FULL_UPDATE = 10

#: ``MergeRegions`` defaults, as **percentages of the display diagonal**
#: (``image-state.go:234-235``, ``NewMergeParameters``).  Spec §4.1.
MERGE_THRESHOLD_PERCENT = 5.0
MERGE_THRESHOLD_STEP_PERCENT = 10.0
