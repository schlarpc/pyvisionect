"""``decode_image_packet``: wire rectangles back to a canvas.

The gate is the golden capture: encoding the canvas and decoding the result
must give the canvas back, bit for bit, through the interlaced fold and the
``eink-flip`` mirror.
"""

from __future__ import annotations

import numpy as np
import pytest

from pyvisionect.imaging import (DRIVERS, PANEL_32INCH, Dithering, Encoding,
                                 Panel, decode_image_packet, encode_frame,
                                 state_checksum)
from pyvisionect.packets import ImagePacket, Rectangle


def _packet(frame) -> ImagePacket:
    return ImagePacket(
        checksum=frame.state_checksum,
        rectangles=[
            Rectangle(
                x=r.x, y=r.y, width=r.w, height=r.h, data=r.data,
                screen_id=r.screen_id, encoding=r.encoding,
            )
            for r in frame.rectangles
        ],
    )


def test_a_full_screen_push_decodes_back_to_the_canvas(golden_canvas) -> None:
    frame = encode_frame(golden_canvas, panel=PANEL_32INCH,
                         encoding=Encoding.FOUR_BIT, dithering=Dithering.NONE)
    decoded = decode_image_packet(_packet(frame), PANEL_32INCH)
    assert decoded.canvas.shape == PANEL_32INCH.canvas_shape
    assert decoded.full is True
    assert decoded.screens == (0, 1)
    assert np.array_equal(decoded.canvas, golden_canvas)


def test_the_decoded_canvas_reproduces_the_checksum_the_device_echoes(
    golden_canvas,
) -> None:
    """Which is what makes a readback a real in-sync test and not a vibe."""
    frame = encode_frame(golden_canvas, panel=PANEL_32INCH,
                         encoding=Encoding.FOUR_BIT, dithering=Dithering.NONE)
    decoded = decode_image_packet(_packet(frame), PANEL_32INCH)
    assert state_checksum(decoded.canvas.tobytes()) == frame.state_checksum
    assert frame.state_checksum == 3741864387


def test_decoding_the_captured_wire_payloads_directly(
    golden_canvas, golden_payloads
) -> None:
    """Not our re-encoding: the bytes the vendor's server actually sent."""
    packet = ImagePacket(
        checksum=3741864387,
        rectangles=[
            Rectangle(x=0, y=0, width=2880, height=640,
                      data=golden_payloads[sid], screen_id=sid, encoding=4)
            for sid in (0, 1)
        ],
    )
    decoded = decode_image_packet(packet, PANEL_32INCH)
    assert np.array_equal(decoded.canvas, golden_canvas)


def test_a_dithered_frame_round_trips_too(golden_canvas) -> None:
    """Dithering happens before packing, so it is inside the round trip."""
    frame = encode_frame(golden_canvas, panel=PANEL_32INCH,
                         encoding=Encoding.FOUR_BIT,
                         dithering=Dithering.BLUE_NOISE)
    decoded = decode_image_packet(_packet(frame), PANEL_32INCH)
    assert state_checksum(decoded.canvas.tobytes()) == frame.state_checksum


def test_one_bit_encoding_round_trips() -> None:
    panel = Panel(width=64, height=32, displays=1,
                  driver=DRIVERS["eink-generic"], interlace=0)
    source = np.where(
        np.indices((32, 64)).sum(axis=0) % 2 == 0, np.uint8(255), np.uint8(0)
    ).astype(np.uint8)
    frame = encode_frame(source, panel=panel, encoding=Encoding.ONE_BIT,
                         dithering=Dithering.NONE)
    decoded = decode_image_packet(_packet(frame), panel)
    assert np.array_equal(decoded.canvas, source)


def test_a_non_mirrored_panel_is_not_flipped() -> None:
    panel = Panel(width=16, height=4, displays=1,
                  driver=DRIVERS["eink-generic"], interlace=0)
    source = np.tile(np.arange(16, dtype=np.uint8) * 17, (4, 1))
    frame = encode_frame(source, panel=panel, encoding=Encoding.FOUR_BIT,
                         dithering=Dithering.NONE)
    decoded = decode_image_packet(_packet(frame), panel)
    assert np.array_equal(decoded.canvas, source)


def test_a_mirrored_panel_is_unflipped_on_the_way_back() -> None:
    """If the mirror were skipped the picture would come back reversed."""
    panel = Panel(width=16, height=4, displays=1,
                  driver=DRIVERS["eink-flip"], interlace=0)
    source = np.tile(np.arange(16, dtype=np.uint8) * 17, (4, 1))
    frame = encode_frame(source, panel=panel, encoding=Encoding.FOUR_BIT,
                         dithering=Dithering.NONE)
    packet = _packet(frame)
    assert np.array_equal(decode_image_packet(packet, panel).canvas, source)
    unmirrored = Panel(width=16, height=4, displays=1,
                       driver=DRIVERS["eink-generic"], interlace=0)
    assert not np.array_equal(
        decode_image_packet(packet, unmirrored).canvas, source
    )


def test_an_uncovered_canvas_reports_itself_as_partial() -> None:
    panel = Panel(width=32, height=8, displays=1,
                  driver=DRIVERS["eink-generic"], interlace=0)
    packet = ImagePacket(
        rectangles=[
            Rectangle(x=0, y=0, width=8, height=4, data=bytes(16),
                      screen_id=0, encoding=4)
        ]
    )
    decoded = decode_image_packet(packet, panel, background=0xFF)
    assert decoded.full is False
    assert decoded.covered == ((0, 0, 8, 4),)
    assert decoded.canvas[0, 0] == 0
    assert decoded.canvas[0, 31] == 0xFF, "untouched area keeps the background"


def test_a_short_payload_is_an_error_not_a_crash() -> None:
    panel = Panel(width=32, height=8, displays=1,
                  driver=DRIVERS["eink-generic"], interlace=0)
    packet = ImagePacket(
        rectangles=[
            Rectangle(x=0, y=0, width=32, height=8, data=b"\x00" * 4,
                      screen_id=0, encoding=4)
        ]
    )
    with pytest.raises(ValueError, match="expected 256"):
        decode_image_packet(packet, panel)


def test_an_unknown_screen_id_on_an_interlaced_panel_is_named() -> None:
    packet = ImagePacket(
        rectangles=[
            Rectangle(x=0, y=0, width=2880, height=640,
                      data=bytes(921600), screen_id=5, encoding=4)
        ]
    )
    with pytest.raises(ValueError, match="no lane pair for ScreenID 5"):
        decode_image_packet(packet, PANEL_32INCH)


def test_to_image_gives_a_pil_image(golden_canvas) -> None:
    frame = encode_frame(golden_canvas, panel=PANEL_32INCH,
                         encoding=Encoding.FOUR_BIT, dithering=Dithering.NONE)
    image = decode_image_packet(_packet(frame), PANEL_32INCH).to_image()
    assert image.mode == "L"
    assert image.size == (1440, 2560)
