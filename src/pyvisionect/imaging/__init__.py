"""``pyvisionect.imaging`` -- the Visionect e-ink imaging pipeline.

Pure, sans-io.  Give it a PIL image or a numpy array and a
:class:`~pyvisionect.imaging.panel.Panel`; get back the rectangles to put in an
``ImagePacket`` and the ``ImageHeader.Checksum`` to stamp on it.

    >>> from pyvisionect.imaging import encode_frame, PANEL_32INCH, Dithering
    >>> frame = encode_frame(img, panel=PANEL_32INCH, encoding=4,
    ...                      dithering=Dithering.BLUE_NOISE)
    >>> len(frame.rectangles), frame.rectangles[0].w, frame.state_checksum
    (2, 2880, 1234567890)

Everything the panel shows is decided here: the device receives
already-quantised, already-bit-packed 1 bpp or 4 bpp data and only drives the
waveform (spec §3.3).

This module is CPU-bound -- run it in an executor
-------------------------------------------------
:func:`encode_frame` is **blocking, CPU-bound, and releases no locks**.  A
1440x2560 canvas goes through grey conversion, change detection, dithering
(Floyd-Steinberg is inherently serial), bit packing, 4-pixel interlacing and
LZ4: comfortably hundreds of milliseconds on a desktop and plausibly seconds on
a Raspberry Pi.  **Never call it on an asyncio event loop.**

    >>> frame = await hass.async_add_executor_job(                 # doctest: +SKIP
    ...     functools.partial(encode_frame, img, panel=panel,
    ...                       encoding=4, dithering=3, prev_state=prev))

Calling it on the loop stalls everything else for the duration, which presents
to a user as "the whole instance freezes every hour".  The resulting ~1.84 MB
socket write is a different matter and is fine on the loop -- it is just a
``write`` plus a ``drain``.

:meth:`FrameState.to_bytes` / :meth:`FrameState.from_bytes` persist the encoder
state across a restart, which is what turns a restart from "a full-screen push
plus a unit of panel wear" into "nothing to do" -- e-ink keeps its image, so if
the device's echoed ``DisplayStateCRC`` still matches the checksum you stored,
the correct action on first contact is none.

Verified against real device bytes
---------------------------------
The 1.84 MB image push captured in
``tmp/visionect/agent-live/pcap/device-11113.pcap`` is reproduced
**byte-exactly**: both 2880x640 payloads and
``ImageHeader.Checksum == 3741864387``.  That one test pins down the nibble
order, the 4-pixel interlace granularity, the screen-1 lane swap, the
``eink-flip`` mirror, the display stacking order and the XXHash32 state tag all
at once -- see ``tests/imaging/test_golden_capture.py``.

Spec: ``artifacts/visionect/02-imaging-framebuffer.md``.
"""

from __future__ import annotations

from .checksum import state_checksum, xxh32
from .constants import (ALIGN_QUANTUM, BITS_PER_PIXEL, ENCODING_LEVELS,
                        LEVEL_STEP, MAX_NO_FULL_UPDATE,
                        MAX_REGIONS_PER_DISPLAY, OPTION_NORMAL_UPDATE,
                        RECTANGLE_UPDATE_OPTIONS_DEFAULT, VENDOR_DITHERING,
                        Dithering, Encoding, ImageType)
from .dither import (BAYER_8, bayer_matrix, blue_noise_matrix, quantise,
                     quantise_floyd_steinberg, quantise_none, quantise_ordered)
from .encoder import EncodedFrame, EncodedRect, FrameState, encode_frame
from .geometry import (mirror_image, rotate_image, translate_to_real,
                       translate_to_session, unrotate_image)
from .grey import beautify, to_grey8
from .interlace import (INTERLACE_PAIRS, deinterlace, deinterlace_1bpp,
                        deinterlace_4bpp, interlace, interlace_1bpp,
                        interlace_4bpp)
from .pack import (pack, pack_1bpp, pack_4bpp, payload_size, unpack,
                   unpack_1bpp, unpack_4bpp)
from .panel import DRIVERS, PANEL_32INCH, DisplayGeometry, Driver, Panel
from .rects import (Rect, align_to_quantum, apply_region_count_limit,
                    detect_changes, is_aligned, join_contours, limit_regions,
                    unite_overlapping_regions)

__all__ = [
    # the interface the rest of the library codes against
    "encode_frame",
    "EncodedFrame",
    "EncodedRect",
    "FrameState",
    # panels / drivers (may move to pyvisionect.devices and be re-exported)
    "Panel",
    "Driver",
    "DisplayGeometry",
    "DRIVERS",
    "PANEL_32INCH",
    # constants
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
    "VENDOR_DITHERING",
    # building blocks
    "Rect",
    "detect_changes",
    "unite_overlapping_regions",
    "join_contours",
    "apply_region_count_limit",
    "limit_regions",
    "align_to_quantum",
    "is_aligned",
    "to_grey8",
    "beautify",
    "quantise",
    "quantise_none",
    "quantise_ordered",
    "quantise_floyd_steinberg",
    "bayer_matrix",
    "BAYER_8",
    "blue_noise_matrix",
    "pack",
    "unpack",
    "pack_1bpp",
    "unpack_1bpp",
    "pack_4bpp",
    "unpack_4bpp",
    "payload_size",
    "interlace",
    "deinterlace",
    "interlace_1bpp",
    "deinterlace_1bpp",
    "interlace_4bpp",
    "deinterlace_4bpp",
    "INTERLACE_PAIRS",
    "translate_to_real",
    "translate_to_session",
    "rotate_image",
    "unrotate_image",
    "mirror_image",
    "xxh32",
    "state_checksum",
]
