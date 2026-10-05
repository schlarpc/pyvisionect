"""Byte-exactness against the real 1.84 MB image push from the live sign.

Source: ``tmp/visionect/agent-live/pcap/device-11113.pcap``, device
``00112233-4455-6677-8899-aabb00000000`` (``HardwareNameID`` 8, hardware
1.1.0, driver ``eink-flip``), session ``DefaultEncoding: 4``,
``DefaultDithering: "none"``.

This is the primary correctness gate for the whole module.  A single pass
pins down, simultaneously:

* 4 bpp nibble order (even pixel in the **low** nibble);
* the ``n * 17`` grey ramp;
* zero row padding / ``stride == width`` linear packing;
* the 4-pixel interlace granularity;
* the **screen-1 lane swap** (screen 0 = bands 0,1 / screen 1 = bands 3,2);
* the ``eink-flip`` horizontal mirror;
* the vertical display stacking order;
* XXHash32 seed 0 over the un-encoded 8-bit state image, with 0 -> 1.

A "which looks more natural" visual check cannot do this: the content is
dithered, so the wrong nibble order still renders a plausible image (it differs
by only a few percent of pixels).  Only a byte diff is conclusive.
"""

from __future__ import annotations

import numpy as np
import pytest

from pyvisionect.imaging import (INTERLACE_PAIRS, PANEL_32INCH, Dithering,
                                 Encoding, deinterlace_4bpp, encode_frame,
                                 mirror_image, state_checksum, unpack_4bpp)


def test_devices_panel_for_the_live_sign_encodes_identically(
    golden_canvas, golden_payloads
):
    """The real integration path: ``devices.panel_for(DisplayType)`` -> wire bytes.

    The live sign reports ``DisplayType = 0xC2050128``, which is not in the
    vendor's enum, so :func:`pyvisionect.devices.panels.panel_for` returns the
    default 4 x 1440x640 ``eink-flip`` row.  Feeding that straight into
    :func:`encode_frame` must reproduce the captured payloads.
    """
    from pyvisionect.devices.panels import panel_for

    frame = encode_frame(golden_canvas, panel=panel_for(0xC2050128),
                         encoding=Encoding.FOUR_BIT, dithering=Dithering.NONE)
    assert frame.state_checksum == 3741864387
    assert [r.data for r in frame.rectangles] == \
        [golden_payloads[0], golden_payloads[1]]


def test_fixture_matches_captured_headers(golden_meta, golden_canvas):
    assert golden_meta["image_header"]["NrPrimitives"] == 2
    assert golden_canvas.shape == (2560, 1440)
    assert golden_canvas.shape == PANEL_32INCH.canvas_shape
    # the capture used DitheringMethod "none", so every value is on the ramp
    assert set(np.unique(golden_canvas)) <= {n * 17 for n in range(16)}


def test_state_checksum_matches_device_echo(golden_meta, golden_canvas):
    """The checksum the device echoed back as ``DisplayStateCRC``."""
    assert state_checksum(golden_canvas.tobytes()) == \
        golden_meta["image_header"]["Checksum"] == 3741864387


def test_encode_reproduces_captured_payloads_byte_for_byte(
    golden_meta, golden_canvas, golden_payloads
):
    frame = encode_frame(golden_canvas, panel=PANEL_32INCH,
                         encoding=Encoding.FOUR_BIT,
                         dithering=Dithering.NONE)

    hdr = golden_meta["image_header"]
    assert frame.nr_primitives == hdr["NrPrimitives"]
    assert frame.payload_length == hdr["PayloadLength"]
    assert frame.state_checksum == hdr["Checksum"]
    assert frame.full_screen is True

    for rect, expected in zip(frame.rectangles, golden_meta["rectangles"]):
        assert rect.image_type == expected["ImageType"]
        assert rect.screen_id == expected["ScreenID"]
        assert (rect.x, rect.y) == (expected["X"], expected["Y"])
        assert (rect.w, rect.h) == (expected["Width"], expected["Height"])
        assert rect.rectangle_update_options == expected["RectangleUpdateOptions"]
        assert rect.options == expected["Options"]
        assert rect.encoding == expected["Encoding"]
        assert rect.reserved == expected["Reserved"]
        assert len(rect.data) == expected["PayloadLength"]
        assert rect.data == golden_payloads[expected["ScreenID"]], (
            f"screen {expected['ScreenID']} payload differs from the captured "
            "bytes"
        )


def test_wire_payload_round_trips_back_to_the_canvas(golden_canvas, golden_payloads):
    """Decode the wire bytes the way the vendor's own preview does.

    ``liveview.DeviceView.Update`` de-interlaces, nibble-decodes and blits per
    display (``device-view.go:89-158``).  Doing the same must give back exactly
    the canvas we started from.
    """
    lanes: dict[int, bytes] = {}
    for screen_id, (lane_a, lane_b) in INTERLACE_PAIRS[2]:
        a, b = deinterlace_4bpp(golden_payloads[screen_id])
        lanes[lane_a], lanes[lane_b] = a, b
    rebuilt = np.vstack([
        mirror_image(unpack_4bpp(lanes[i]).reshape(640, 1440)) for i in range(4)
    ])
    assert np.array_equal(rebuilt, golden_canvas)


def test_wrong_nibble_order_is_visually_invisible(golden_payloads):
    """Why only a byte diff can decide the nibble order.

    Swapping the nibbles swaps *adjacent* pixels, so a box downsample by any
    even factor is **bit-identical** between the two decodings -- while 60 % of
    the full-resolution pixels differ.  Any "which rendering looks more
    natural" heuristic is therefore blind to this, which is how an earlier
    pass through this capture concluded "high nibble first" (it is low-nibble
    first; see ``encode_eight_to_four``, spec §2.3).
    """
    from PIL import Image

    data = np.frombuffer(golden_payloads[0], dtype=np.uint8)
    low_first = np.empty(data.size * 2, dtype=np.uint8)
    low_first[0::2] = (data & 0x0F) * 17
    low_first[1::2] = (data >> 4) * 17
    high_first = np.empty(data.size * 2, dtype=np.uint8)
    high_first[0::2] = (data >> 4) * 17
    high_first[1::2] = (data & 0x0F) * 17

    differing = np.count_nonzero(low_first != high_first) / low_first.size
    assert differing > 0.5, "expected the two nibble orders to differ widely"

    a = Image.fromarray(low_first.reshape(640, 2880), "L").resize((360, 80), Image.BOX)
    b = Image.fromarray(high_first.reshape(640, 2880), "L").resize((360, 80), Image.BOX)
    assert np.array_equal(np.asarray(a), np.asarray(b)), (
        "a box downsample should hide the swap entirely"
    )


def test_screen1_lane_swap_is_not_symmetric(golden_canvas, golden_payloads):
    """Getting the screen-1 swap wrong scrambles the bottom half.

    Re-encode with the swap removed and assert the payload changes, so the
    table in :data:`INTERLACE_PAIRS` is load-bearing and not a coin flip.
    """
    from pyvisionect.imaging import interlace_4bpp, pack_4bpp

    bands = [mirror_image(golden_canvas[i * 640:(i + 1) * 640]) for i in range(4)]
    packed = [pack_4bpp(np.ascontiguousarray(b)) for b in bands]
    unswapped = interlace_4bpp(packed[2], packed[3])
    assert unswapped != golden_payloads[1]
    assert interlace_4bpp(packed[3], packed[2]) == golden_payloads[1]


def test_checksum_implementations_agree(golden_canvas):
    from pyvisionect.imaging.checksum import (HAVE_NATIVE_XXHASH, xxh32,
                                              xxh32_python)

    raw = golden_canvas.tobytes()
    assert xxh32_python(raw, 0) == 3741864387
    if HAVE_NATIVE_XXHASH:
        assert xxh32(raw, 0) == xxh32_python(raw, 0)


@pytest.mark.parametrize("vector,expected", [
    (b"", 0x02CC5D05),
    (b"a", 0x550D7456),
    (b"abc", 0x32D153FF),
])
def test_xxh32_reference_vectors(vector, expected):
    from pyvisionect.imaging.checksum import xxh32, xxh32_python

    assert xxh32_python(vector, 0) == expected
    assert xxh32(vector, 0) == expected
