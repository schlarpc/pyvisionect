"""The imaging pipeline: image in, wire-ready rectangles + state tag out.

::

    grey8 -> (Beautify) -> change-detect -> rect list -> merge/limit(<=15)
          -> align to 16-bit quanta -> crop/rotate per display -> dither
          -> bit-pack -> per-display interlace

Everything here is a pure function of its arguments.  No I/O, no clock, no
global mutable state; the only module-level data are the dither matrices.

Spec references are to ``artifacts/visionect/02-imaging-framebuffer.md``.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from typing import Any, Optional, Sequence

import numpy as np

from . import dither as _dither
from . import pack as _pack
from .constants import (MAX_REGIONS_PER_DISPLAY, OPTION_NORMAL_UPDATE,
                        RECTANGLE_UPDATE_OPTIONS_DEFAULT, Dithering, Encoding,
                        ImageType)
from .checksum import state_checksum
from .geometry import (mirror_image, rotate_image, translate_to_real,
                       unrotate_image)
from .grey import beautify as _beautify
from .grey import to_grey8
from .interlace import interlace as _interlace
from .interlace import interlace_pairs
from .panel import Panel
from .rects import Rect, align_to_quantum, detect_changes, limit_regions

__all__ = ["EncodedRect", "EncodedFrame", "FrameState", "encode_frame"]


@dataclass(frozen=True)
class EncodedRect:
    """One wire rectangle: a ``proto.RectangleHeader`` plus its payload.

    Field names match ``RectangleHeader`` (spec §1.1) except that ``w``/``h``
    are spelled short.  All integers are already in range for their wire
    widths (uint16, or uint32 for ``len(data)``).
    """

    x: int
    y: int
    w: int
    h: int
    screen_id: int
    options: int
    data: bytes
    encoding: int = int(Encoding.FOUR_BIT)
    image_type: int = int(ImageType.GRAY)
    #: ``RectangleUpdateOptions``.  Pre-filled with the encoding's default
    #: (``0x0101`` / ``0x0102``) rather than left at 0 for the marshaller to
    #: substitute (``packet/image.go:271-277``, spec §1.2).  The captured
    #: traffic shows ``0x0102``, so either route produces identical bytes.
    rectangle_update_options: int = 0
    #: The :class:`~pyvisionect.imaging.constants.Dithering` used.  Informational;
    #: it never reaches the wire (spec §3.3).
    dithering: int = int(Dithering.NONE)
    #: ``Reserved``; the vendor never writes anything but 0.
    reserved: int = 0

    @property
    def payload_length(self) -> int:
        return len(self.data)


@dataclass(frozen=True)
class FrameState:
    """Opaque encoder state.  Pass it back as ``prev_state`` on the next call.

    :param state_grey: the server's 8-bit model of what the panel shows, in
        **session-canvas** coordinates with ``stride == width`` -- Go's
        ``image.Gray.Pix`` for the canvas rectangle.  This buffer, and nothing
        else, is what the state checksum hashes (spec §6.1).
    :param source_grey: the (post-Beautify) grey input of the last encode, kept
        so that the next call can run change detection against it
        (``canvas.go:411/445``).
    :param checksum: the ``ImageHeader.Checksum`` that went with ``state_grey``.
    :param encoding: encoding of the last encode; a change forces full screen
        (``image-state.go:757-764``).
    :param dithering: likewise for the dithering mode.
    """

    state_grey: np.ndarray
    source_grey: np.ndarray
    checksum: int
    encoding: int
    dithering: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "state_grey",
                           np.ascontiguousarray(self.state_grey, dtype=np.uint8))
        object.__setattr__(self, "source_grey",
                           np.ascontiguousarray(self.source_grey, dtype=np.uint8))

    # ---- persistence ---------------------------------------------------
    #
    # Persisting this across a restart is the difference between a free restart
    # and a full-screen 1.84 MB push plus one unit of panel wear, every restart,
    # on a battery device.  E-ink is persistent: if the device's echoed
    # ``DisplayStateCRC`` still matches the checksum we stored, the right
    # behaviour on first contact is to do nothing at all.
    #
    # The format is owned here, deliberately, so that adding a field to
    # FrameState is not a breaking change for everyone who persisted one.

    SERIAL_MAGIC = b"VNFS"
    SERIAL_VERSION = 1
    _SERIAL_HEADER = struct.Struct("<4sBBBBI2I2I2I")

    def to_bytes(self, *, compress: bool = True) -> bytes:
        """Serialise to a self-describing, versioned blob.

        Both grey planes are zlib-deflated by default, which matters: real
        e-ink content is mostly flat white and compresses enormously, so a
        1440x2560 state usually lands in tens of kilobytes rather than 3.7 MB.

        :param compress: set False to store the planes raw, e.g. if the
            consumer's own store already compresses.
        :returns: bytes safe to hand straight back to :meth:`from_bytes`.
        """
        state = bytes(self.state_grey.tobytes())
        source = bytes(self.source_grey.tobytes())
        if compress:
            state = zlib.compress(state, 6)
            source = zlib.compress(source, 6)
        header = self._SERIAL_HEADER.pack(
            self.SERIAL_MAGIC,
            self.SERIAL_VERSION,
            1 if compress else 0,
            int(self.encoding) & 0xFF,
            int(self.dithering) & 0xFF,
            int(self.checksum) & 0xFFFFFFFF,
            int(self.state_grey.shape[0]), int(self.state_grey.shape[1]),
            int(self.source_grey.shape[0]), int(self.source_grey.shape[1]),
            len(state), len(source),
        )
        return header + state + source

    @classmethod
    def from_bytes(cls, raw: bytes) -> "FrameState":
        """Restore a :class:`FrameState` written by :meth:`to_bytes`.

        :raises ValueError: on a bad magic, an unknown version, or a truncated
            blob.  A future version raises rather than mis-restoring: the
            correct recovery is to discard the blob and push a full frame once,
            not to guess.
        """
        size = cls._SERIAL_HEADER.size
        if len(raw) < size:
            raise ValueError(
                f"FrameState blob is {len(raw)} bytes, need at least {size}"
            )
        (magic, version, flags, encoding, dithering, checksum,
         state_h, state_w, source_h, source_w,
         state_len, source_len) = cls._SERIAL_HEADER.unpack_from(raw, 0)
        if magic != cls.SERIAL_MAGIC:
            raise ValueError(
                f"not a FrameState blob: magic {magic!r} != {cls.SERIAL_MAGIC!r}"
            )
        if version != cls.SERIAL_VERSION:
            raise ValueError(
                f"FrameState blob version {version} is not {cls.SERIAL_VERSION}; "
                "discard it and push a full frame instead of guessing"
            )
        if len(raw) != size + state_len + source_len:
            raise ValueError(
                f"FrameState blob is {len(raw)} bytes, header describes "
                f"{size + state_len + source_len}"
            )
        state = raw[size:size + state_len]
        source = raw[size + state_len:]
        if flags & 1:
            state = zlib.decompress(state)
            source = zlib.decompress(source)
        expected = (state_h * state_w, source_h * source_w)
        if (len(state), len(source)) != expected:
            raise ValueError(
                f"FrameState planes are {(len(state), len(source))} bytes, "
                f"shapes describe {expected}"
            )
        return cls(
            state_grey=np.frombuffer(state, dtype=np.uint8).reshape(state_h, state_w),
            source_grey=np.frombuffer(source, dtype=np.uint8).reshape(
                source_h, source_w
            ),
            checksum=int(checksum),
            encoding=int(encoding),
            dithering=int(dithering),
        )


@dataclass
class EncodedFrame:
    """Result of :func:`encode_frame`.

    :param rectangles: the rectangles to put in the ``ImagePacket``, in wire
        order.  **Empty** means "nothing changed, send no packet".
    :param state_checksum: ``ImageHeader.Checksum`` -- XXHash32(seed 0) of the
        8-bit state image, ``0`` remapped to ``1`` (spec §6.1).  Send it in
        every ``ImagePacket``; the device echoes it back as
        ``DisplayStateCRC``, and a mismatch costs one redundant full-screen
        redraw.
    :param state: opaque; pass back as ``prev_state`` next call.
    :param full_screen: whether this frame covers every display in full.  The
        caller needs it for the inverse-update rule (spec §1.4) and for the
        ``noFullUpdateCnt`` budget (spec §1.8).
    """

    rectangles: list[EncodedRect]
    state_checksum: int
    state: FrameState
    full_screen: bool = False

    #: ``ImageHeader.NrPrimitives`` (``image-state.go:1535``).
    @property
    def nr_primitives(self) -> int:
        return len(self.rectangles)

    @property
    def payload_length(self) -> int:
        """``ImageHeader.PayloadLength``: bytes after the 20-byte header."""
        return sum(24 + len(r.data) for r in self.rectangles)


def _validate(encoding: int, dithering: int, panel: Panel) -> tuple[int, int]:
    enc = int(encoding)
    if enc not in (int(Encoding.ONE_BIT), int(Encoding.FOUR_BIT)):
        raise ValueError(
            f"bad encoding {encoding!r}: only 1 (1 bpp, 2 levels) and 4 "
            "(4 bpp, 16 levels) exist; 0 is 'undefined' and anything else is "
            "'unknown' (spec §2.1)"
        )
    dith = int(dithering)
    if dith == int(Dithering.DEFAULT):
        raise ValueError(
            "DitheringType 0 ('default') is invalid at the encoder; the vendor "
            "rejects it with render.ErrDithering (image-state.go:754). Pass "
            "1 (none), 2 (bayer), 3 (floyd-steinberg) or 4 (blue-noise)."
        )
    if dith not in (1, 2, 3, 4):
        raise ValueError(
            f"unknown DitheringType {dithering!r}; known values are 1 none, "
            "2 bayer, 3 floyd-steinberg, 4 blue-noise (library extension)"
        )
    if panel.driver.color_mask:
        raise NotImplementedError(
            "colour-mask drivers (eink-32-inch-color-mask*) remap through a "
            "palette image (imgproc.GmMapImage, spec §3.2) and are not "
            "implemented; this module only handles the greyscale path"
        )
    return enc, dith


def encode_frame(
    img: Any,
    *,
    panel: Panel,
    encoding: int,
    dithering: int,
    rects: Optional[Sequence[Rect]] = None,
    prev_state: Optional[FrameState] = None,
    inverse: bool = False,
    rect_options: int = 0,
    image_type: int = int(ImageType.GRAY),
    rectangle_update_options: Optional[int] = None,
    change_threshold: float = 0.0,
    merge_regions: bool = True,
    max_regions: int = MAX_REGIONS_PER_DISPLAY,
    force_full_screen: bool = False,
    beautify: bool = False,
    beautify_gamma: float = 1.1,
    bayer_matrix: Optional[np.ndarray] = None,
    blue_noise_matrix: Optional[np.ndarray] = None,
    dither_levels: Optional[int] = None,
) -> EncodedFrame:
    """Encode one frame into wire-ready rectangles and a state checksum.

    .. warning:: **This is CPU-bound and blocking, and it releases no locks.**
       A 1440x2560 canvas goes through grey conversion, change detection,
       dithering (Floyd-Steinberg is inherently serial), bit packing, 4-pixel
       interlacing and LZ4 -- comfortably hundreds of milliseconds on a desktop
       and plausibly seconds on a Raspberry Pi. **Never call it on an asyncio
       event loop.** In Home Assistant, use
       ``hass.async_add_executor_job``; elsewhere,
       ``loop.run_in_executor(None, ...)`` or a thread. Running it on the loop
       stalls everything else for the duration, which presents as "the whole
       instance freezes every hour".

    :param img: a PIL image or a numpy array; anything
        :func:`~pyvisionect.imaging.grey.to_grey8` accepts.  Its size must be
        the panel's session canvas (``panel.canvas_width x
        panel.canvas_height``).
    :param panel: an imaging :class:`~pyvisionect.imaging.panel.Panel`, or
        anything :meth:`~pyvisionect.imaging.panel.Panel.adapt` accepts --
        including a :class:`pyvisionect.devices.panels.Panel` from
        ``devices.panel_for(display_type)``.
    :param encoding: ``1`` or ``4``.  Nothing else exists (spec §2.1).
    :param dithering: a :class:`~pyvisionect.imaging.constants.Dithering`
        value.  ``0`` is rejected.  ``4`` (blue-noise) is a library extension
        and is safe because the mode never reaches the wire (spec §3.3).
    :param rects: dirty rectangles in **canvas** coordinates.  ``None`` means
        "derive them": change-detect against ``prev_state`` if there is one,
        else full screen.
    :param prev_state: the :class:`FrameState` from the previous call.
    :param inverse: request an inverse / ghost-clearing refresh by **clearing**
        ``RectangleHeader.Options`` bit ``0x0002`` on every rectangle (spec
        §1.4, §5).  Only legal on a full-screen frame, which it forces.

        .. note:: **[INFERRED]**
           The server-side bit manipulation
           (``image-state.go:1519-1522``) is verified; the *firmware's
           reaction* to a cleared bit 1 is not.  With the shipped
           ``RectangleFlags = 0`` the bit is already clear, so on a stock
           deployment this signalling is a no-op on the wire -- to use it you
           must pass ``rect_options`` with bit 1 **set**.
    :param rect_options: ``Config.Engine.RectangleFlags`` ->
        ``RectangleHeader.Options`` (spec §1.4).  The server only ever copies
        it, except for the one bit ``inverse`` clears.
    :param rectangle_update_options: override ``RectangleUpdateOptions``.
        ``None`` uses the encoding's marshal-time default, ``0x0101`` (1 bpp)
        or ``0x0102`` (4 bpp) -- spec §1.2.  Pass ``0`` to leave the
        substitution to the packet marshaller instead.
    :param change_threshold: per-pixel absolute grey difference for change
        detection; the vendor's default is ``0``, meaning *any* difference
        counts (spec §4.2).
    :param merge_regions: ``MergeRegions.Enable``.  When false and a display
        has more than ``max_regions`` dirty rectangles, the frame is promoted
        to full screen (``image-state.go:241``).
    :param force_full_screen: skip change detection and repaint everything.
    :param beautify: run the auto-levels + gamma stage (spec §4.3).  **Off by
        default**: it would undo a renderer that already quantised to the
        output ramp.
    :param bayer_matrix: threshold matrix overriding the built-in 8x8 Bayer.
    :param blue_noise_matrix: threshold matrix overriding the shipped 64x64
        void-and-cluster matrix -- drop in the exact matrix your renderer uses
        to get identical output.
    :param dither_levels: force a level count (e.g. ``16`` to make ``bayer``
        multi-level at 4 bpp instead of the vendor's bi-level degeneration).

    :returns: an :class:`EncodedFrame`.  ``rectangles`` is empty when nothing
        changed.
    """
    panel = Panel.adapt(panel)
    enc, dith = _validate(encoding, dithering, panel)
    grey = to_grey8(img)
    if grey.shape != panel.canvas_shape:
        raise ValueError(
            f"image is {grey.shape[1]}x{grey.shape[0]} but panel canvas is "
            f"{panel.canvas_width}x{panel.canvas_height} "
            f"({panel.displays} displays of {panel.width}x{panel.height})"
        )
    if beautify:
        grey = _beautify(grey, gamma=beautify_gamma)

    geometry = panel.geometry()
    canvas = Rect(0, 0, panel.canvas_width, panel.canvas_height)

    # ---- 1. decide what to repaint -------------------------------------
    #
    # `panel.forces_full_screen` is not a shortcut: on HardwareNameID 8
    # getRectangleSupport() is false unconditionally, and structurally the
    # interlacer needs all four full-size display rectangles (spec §1.6/§1.7).
    reencode = prev_state is not None and (prev_state.encoding != enc
                                           or prev_state.dithering != dith)
    full_screen = bool(
        force_full_screen
        or inverse
        or prev_state is None
        or panel.forces_full_screen
        or reencode
    )
    if full_screen:
        dirty: list[Rect] = [canvas]
    elif rects is not None:
        dirty = [r for r in (c.intersection(canvas) for c in rects) if r is not None]
    else:
        dirty = detect_changes(grey, prev_state.source_grey, change_threshold)

    if not dirty:
        assert prev_state is not None
        return EncodedFrame(
            rectangles=[],
            state_checksum=prev_state.checksum,
            state=FrameState(prev_state.state_grey, grey, prev_state.checksum,
                             enc, dith),
            full_screen=False,
        )

    # ---- 2. canvas-wide quantisation for the pixel-local dithers --------
    #
    # Ordered dithers are phased on the *canvas* coordinate, so a rectangle's
    # pixels are identical whether it is encoded alone or as part of a bigger
    # one.  That is what keeps partial updates from seaming.  Floyd-Steinberg
    # is not pixel-local and is therefore done per rectangle, below.
    pixel_local = _dither.is_pixel_local(dith)
    quant_canvas: Optional[np.ndarray] = None
    if pixel_local:
        quant_canvas = _dither.quantise(
            grey, enc, dith, origin=(0, 0), bayer=bayer_matrix,
            blue_noise=blue_noise_matrix, levels=dither_levels,
        )

    # ---- 3. per display: clip, merge, align, cut, rotate, mirror, pack ---
    state = (np.zeros(panel.canvas_shape, dtype=np.uint8) if prev_state is None
             else prev_state.state_grey.copy())
    ruo = (RECTANGLE_UPDATE_OPTIONS_DEFAULT[enc] if rectangle_update_options is None
           else int(rectangle_update_options))
    options = int(rect_options) & ~OPTION_NORMAL_UPDATE if inverse else int(rect_options)

    per_display: list[list[EncodedRect]] = []
    for display in geometry:
        area = Rect(display.x, display.y, display.rotated_width, display.rotated_height)
        clipped = [r for r in (d.intersection(area) for d in dirty) if r is not None]
        if not clipped:
            per_display.append([])
            continue

        merged = limit_regions(clipped, display.width, display.height,
                               merge=merge_regions, max_regions=max_regions)
        if merged is None:
            # ``forceFullScreenUnlocked`` -- restart the whole frame as a
            # full-screen one rather than emit an inconsistent mix.
            return encode_frame(
                img, panel=panel, encoding=enc, dithering=dith, rects=None,
                prev_state=prev_state, inverse=inverse,
                rect_options=rect_options, image_type=image_type,
                rectangle_update_options=rectangle_update_options,
                change_threshold=change_threshold, merge_regions=merge_regions,
                max_regions=max_regions, force_full_screen=True,
                beautify=beautify, beautify_gamma=beautify_gamma,
                bayer_matrix=bayer_matrix, blue_noise_matrix=blue_noise_matrix,
                dither_levels=dither_levels,
            )
        aligned = [align_to_quantum(r, enc, area) for r in merged]

        out: list[EncodedRect] = []
        for session_rect in aligned:
            local = translate_to_real(session_rect, display)
            rows, cols = session_rect.as_slice()
            if pixel_local:
                assert quant_canvas is not None
                tile = quant_canvas[rows, cols]
            else:
                tile = grey[rows, cols]
            tile = rotate_image(tile, display.rotation)
            if panel.driver.mirror:
                tile = mirror_image(tile)
            if not pixel_local:
                tile = _dither.quantise(tile, enc, dith, levels=dither_levels)
            tile = np.ascontiguousarray(tile)

            # The state image models the panel in *session* coordinates, so
            # undo the mirror and the rotation on the quantised pixels.  For
            # Floyd-Steinberg that matters: the dither ran on the mirrored
            # tile, and the state has to record what the panel will actually
            # show, not a re-dither of the unmirrored tile.
            back = mirror_image(tile) if panel.driver.mirror else tile
            state[rows, cols] = unrotate_image(back, display.rotation)

            out.append(EncodedRect(
                x=local.x, y=local.y, w=local.width, h=local.height,
                screen_id=display.id, options=options,
                data=_pack.pack(tile, enc),
                encoding=enc, image_type=int(image_type),
                rectangle_update_options=ruo, dithering=dith,
            ))
        per_display.append(out)

    # ---- 4. transmit-side interlacing (spec §1.6) ----------------------
    if panel.interlace_mode:
        flat = _interlace_displays(per_display, panel, enc, options, ruo,
                                   int(image_type), dith)
    else:
        flat = [r for lst in per_display for r in lst]

    checksum = state_checksum(np.ascontiguousarray(state).tobytes())
    return EncodedFrame(
        rectangles=flat,
        state_checksum=checksum,
        state=FrameState(state, grey, checksum, enc, dith),
        full_screen=full_screen,
    )


def _interlace_displays(per_display: list[list[EncodedRect]], panel: Panel,
                        encoding: int, options: int, ruo: int,
                        image_type: int, dithering: int) -> list[EncodedRect]:
    """Fold N full-size display rectangles into the physical channels.

    ``main.interlacingHack`` (``image_interlacing.go:105-188``) requires
    *exactly* four rectangles, each the full ``1440 x 640``, all with the same
    encoding; anything else is passed through untouched (``:112-118``).  Since
    this hardware also has ``RectangleSupport == false``, that precondition is
    always met here -- but we re-check it rather than assume.
    """
    pairs = interlace_pairs(panel.interlace_mode)
    if len(per_display) != panel.displays or any(len(rs) != 1 for rs in per_display):
        raise ValueError(
            "interlacing needs exactly one full-size rectangle per display "
            f"(got {[len(rs) for rs in per_display]}); a partial update cannot be "
            "interlaced, which is why RectangleSupport is false on this "
            "hardware (spec §1.6/§1.7)"
        )
    singles = [rs[0] for rs in per_display]
    if any(r.w != panel.width or r.h != panel.height for r in singles):
        raise ValueError(
            f"interlacing needs {panel.width}x{panel.height} rectangles, got "
            f"{[(r.w, r.h) for r in singles]}"
        )

    out: list[EncodedRect] = []
    for screen_id, (lane_a, lane_b) in pairs:
        a, b = singles[lane_a], singles[lane_b]
        out.append(EncodedRect(
            # ``interlacingHack`` copies one of the pair and only doubles
            # ``Width`` (:177-180); X/Y are the display-local 0,0 of a
            # full-screen rectangle.
            x=a.x, y=a.y, w=a.w * 2, h=a.h,
            screen_id=screen_id, options=options,
            data=_interlace(a.data, b.data, encoding),
            encoding=encoding, image_type=image_type,
            rectangle_update_options=ruo, dithering=dithering,
        ))
    return out
