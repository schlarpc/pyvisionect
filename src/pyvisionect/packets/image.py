"""Image packets (type 5) -- server to device only.

The device receives **already-quantised, already-bit-packed** data and does only
waveform drive, so the whole imaging pipeline is the server's job.  This module
only does the framing; :mod:`pyvisionect.imaging` produces the pixel payloads.

Layout (all little-endian)::

    ImageHeader (20 bytes)
       0   4  Checksum      uint32   XXHash32 over the 8-bit state image
       4   4  NrPrimitives  uint32   number of rectangles
       8   4  Options       uint32
      12   4  PayloadLength uint32   total bytes of rectangles that follow
      16   4  Reserved      uint32

    RectangleHeader (24 bytes) + PayloadLength bytes, NrPrimitives times
       0   2  ImageType              uint16  (1 = gray)
       2   2  ScreenID               uint16
       4   2  X                      uint16
       6   2  Y                      uint16
       8   2  Width                  uint16
      10   2  Height                 uint16
      12   2  RectangleUpdateOptions uint16
      14   2  Options                uint16
      16   2  Encoding               uint16  (packet.EncodingType: 1 or 4)
      18   2  Reserved               uint16
      20   4  PayloadLength          uint32

Two things that bite
--------------------

* ``RectangleUpdateOptions`` is **auto-filled at marshal time when it is 0**
  (``packet/image.go:271-277``): ``0x0101`` for 1-bit, ``0x0102`` for 4-bit.
  The captured push carries ``0x0102``, which proves it left the renderer as 0.
  :meth:`Rectangle.encode` reproduces that auto-fill, so a re-encode of the
  capture is byte-exact.
* ``ImageHeader.Checksum`` is literally what the device reports back as
  ``DisplayStateCRC`` (status tag 9) -- the server's free "is the device in sync"
  test.  It is **XXHash32 (seed 0) over the raw 8-bit state image**, not a CRC
  and not over the encoded data.  Getting it wrong causes silent redundant
  redraws.  :mod:`pyvisionect.imaging` computes it.

``Rectangle.options`` bit ``0x0002`` **cleared** on all rectangles of one
full-screen packet requests an inverse update.  Verified on hardware by A/B:
``options=0`` makes the firmware log ``inv: 1`` and ``Force Inverse and full
area update``, ``options=2`` makes it log ``inv: 0`` with no such line.  So
*clear* means inverse, and the vendor's shipped default of 0 inverts on every
full-screen push.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from ..devices.enums import EncodingType, ImageType, PacketType
from ..wire.errors import PayloadError

__all__ = [
    "IMAGE_HEADER_SIZE",
    "RECT_HEADER_SIZE",
    "RECT_UPDATE_OPTIONS_DEFAULT",
    "MAX_RECTANGLES",
    "Rectangle",
    "ImagePacket",
]

IMAGE_HEADER_SIZE = 20
RECT_HEADER_SIZE = 24

RECT_UPDATE_OPTIONS_DEFAULT: dict[int, int] = {
    EncodingType.ONE_BIT: 0x0101,
    EncodingType.FOUR_BIT: 0x0102,
}
"""``packet/image.go:271-277``: when ``RectangleUpdateOptions == 0`` the marshaller
substitutes these per-encoding constants."""

MAX_RECTANGLES = 15
"""The server merges/limits dirty regions to at most 15 rectangles per packet."""

OPTION_NOT_INVERSE = 0x0002
"""``RectangleHeader.Options`` bit. Cleared on every rectangle of a full-screen
packet = inverse update; set = ordinary update. Verified on hardware (A/B
against the firmware's ``inv:`` log line), and the vendor's admin UI labels the
value ``0`` "Enable" and ``2`` "Disable" for exactly this key."""

_IMG = struct.Struct("<5I")
_RECT = struct.Struct("<10HI")


@dataclass(frozen=True, slots=True)
class Rectangle:
    """One ``RectangleHeader`` plus its pixel payload."""

    x: int
    y: int
    width: int
    height: int
    data: bytes = field(repr=False, default=b"")
    screen_id: int = 0
    encoding: int = EncodingType.FOUR_BIT
    image_type: int = ImageType.GRAY
    options: int = 0
    update_options: int = 0
    """0 means "let the marshaller fill it in" -- see
    :data:`RECT_UPDATE_OPTIONS_DEFAULT`."""
    reserved: int = 0

    @property
    def effective_update_options(self) -> int:
        """What actually goes on the wire."""
        if self.update_options:
            return self.update_options
        return RECT_UPDATE_OPTIONS_DEFAULT.get(self.encoding, 0)

    @property
    def expected_data_length(self) -> int:
        """Bytes of packed pixel data for this geometry and encoding."""
        bits = self.width * self.height * self.encoding
        return (bits + 7) // 8

    def encode(self) -> bytes:
        return (
            _RECT.pack(
                self.image_type,
                self.screen_id,
                self.x,
                self.y,
                self.width,
                self.height,
                self.effective_update_options,
                self.options,
                self.encoding,
                self.reserved,
                len(self.data),
            )
            + self.data
        )

    @classmethod
    def decode_from(cls, payload: bytes, offset: int) -> tuple["Rectangle", int]:
        """Decode one rectangle at *offset*; return it and the next offset."""
        if len(payload) - offset < RECT_HEADER_SIZE:
            raise PayloadError(
                f"need {RECT_HEADER_SIZE} bytes for a RectangleHeader at {offset}"
            )
        (
            image_type,
            screen_id,
            x,
            y,
            width,
            height,
            update_options,
            options,
            encoding,
            reserved,
            length,
        ) = _RECT.unpack_from(payload, offset)
        start = offset + RECT_HEADER_SIZE
        end = start + length
        if end > len(payload):
            raise PayloadError(
                f"rectangle at {offset} declares {length} bytes, only "
                f"{len(payload) - start} remain"
            )
        return (
            cls(
                x=x,
                y=y,
                width=width,
                height=height,
                data=payload[start:end],
                screen_id=screen_id,
                encoding=encoding,
                image_type=image_type,
                options=options,
                update_options=update_options,
                reserved=reserved,
            ),
            end,
        )


@dataclass(slots=True)
class ImagePacket:
    """A ``packet.Image`` payload."""

    checksum: int = 0
    """XXHash32 of the 8-bit state image. Echoed back as status tag 9."""

    rectangles: list[Rectangle] = field(default_factory=list)
    options: int = 0
    reserved: int = 0

    TYPE = PacketType.IMAGE

    @classmethod
    def decode(cls, payload: bytes) -> "ImagePacket":
        if len(payload) < IMAGE_HEADER_SIZE:
            raise PayloadError(
                f"image payload is {len(payload)} bytes, need >= {IMAGE_HEADER_SIZE}"
            )
        checksum, count, options, declared, reserved = _IMG.unpack_from(payload, 0)
        offset = IMAGE_HEADER_SIZE
        rects: list[Rectangle] = []
        for _ in range(count):
            rect, offset = Rectangle.decode_from(payload, offset)
            rects.append(rect)
        body_len = offset - IMAGE_HEADER_SIZE
        if declared and declared != body_len:
            raise PayloadError(
                f"ImageHeader.PayloadLength = {declared} but the rectangles "
                f"consumed {body_len} bytes"
            )
        return cls(
            checksum=checksum, rectangles=rects, options=options, reserved=reserved
        )

    def encode(self) -> bytes:
        body = b"".join(rect.encode() for rect in self.rectangles)
        return (
            _IMG.pack(
                self.checksum,
                len(self.rectangles),
                self.options,
                len(body),
                self.reserved,
            )
            + body
        )

    def __len__(self) -> int:
        return len(self.rectangles)
