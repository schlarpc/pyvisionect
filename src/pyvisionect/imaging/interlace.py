"""Transmit-side interlacing for the 32" sign (``HardwareNameID == 8``).

Spec §1.6, ``vss/cmd/engine/image_interlacing.go``.

The server models the sign as **4 logical displays of 1440 x 640** stacked
vertically, but the panel has **2 physical 2880 x 640 channels**.  Immediately
before the gRPC hand-off to the gateway, ``main.interlacingHack``
(``image_interlacing.go:105``) folds the four full-size rectangles into two,
interleaving each pair **at 4-pixel granularity** -- which is exactly the
"16-bit quantum" the rectangle sizer enforces (spec §2.4).

4 bpp interleaver (``image_interlacing.go:43-46``), 2 bytes = 4 pixels at a
time::

    for i, io := 0, 0; io+4 <= len(out); io += 4, i += 2 {
        out[io+0]=a[i]; out[io+1]=a[i+1]; out[io+2]=b[i]; out[io+3]=b[i+1]
    }

1 bpp interleaver (``image_interlacing.go:75-76``), one nibble = 4 pixels at a
time::

    out[io+0] = (a[i] & 0xF0) | (b[i] >> 4)
    out[io+1] = (a[i] << 4)   | (b[i] & 0x0F)

Both produce the same pixel sequence: ``a[0:4], b[0:4], a[4:8], b[4:8], ...``.

Pairing
-------
``getInterlacingMode`` (``image_interlacing.go:92``) returns ``2`` for
``HardwareNameID == 8`` at any hardware revision other than 1.0.0 (ours is
1.1.0), and ``1`` for 1.0.0.

**Mode 2's pairing and lane order were recovered from real captured traffic,
not from the disassembly** -- see :data:`INTERLACE_PAIRS` and
``tests/imaging/test_golden_capture.py``.  Mode 1's pairing was *not*
recovered, so it raises.
"""

from __future__ import annotations

import numpy as np

from .constants import Encoding

__all__ = ["INTERLACE_PAIRS", "interlace_4bpp", "deinterlace_4bpp",
           "interlace_1bpp", "deinterlace_1bpp", "interlace", "deinterlace",
           "interlace_pairs", "INTERLACE_GROUP", "SCREEN_X_QUANTUM",
           "lane_span", "screen_span", "lane_of_display", "displays_of_screen"]


#: Pixels per interleave group -- the "4-pixel granularity" of the fold.
#: ``image_interlacing.go:43-46`` moves 2 bytes (4 pixels) of lane A, then 2 of
#: lane B; the 1 bpp interleaver moves one nibble (4 pixels) at a time.  Both
#: produce ``a[0:4], b[0:4], a[4:8], b[4:8], ...``.
INTERLACE_GROUP = 4

#: Screen-space x/width quantum: one lane group plus its partner's.  A screen
#: rectangle whose ``x`` is not a multiple of this starts part-way into a group
#: and the lanes come out swapped; one whose ``width`` is not a multiple of it
#: ends mid-group and the packed lane rows stop being whole numbers of groups.
SCREEN_X_QUANTUM = 2 * INTERLACE_GROUP


#: ``{mode: ((screen_id, (lane_a_display, lane_b_display)), ...)}``
#:
#: Mode 2, **verified byte-exactly** against the 1.84 MB image push in
#: ``tmp/visionect/agent-live/pcap/device-11113.pcap``: de-interlacing the two
#: captured 2880 x 640 payloads with this table, decoding the nibbles,
#: un-mirroring each 1440-wide tile and stacking them as displays 0..3
#: reproduces ``ImageHeader.Checksum == 3741864387`` exactly.  All 24
#: permutations x 2 nibble orders x 2 mirror settings were tried; this is the
#: only combination that matches.
#:
#: Note that **screen 1's lanes are swapped** relative to screen 0 -- that is
#: the ``displayOrder`` argument of the vendor's interleaver
#: (``image_interlacing.go:32-37``) and is a physical panel-wiring detail.
INTERLACE_PAIRS: dict[int, tuple[tuple[int, tuple[int, int]], ...]] = {
    2: ((0, (0, 1)), (1, (3, 2))),
}


def interlace_pairs(mode: int) -> tuple[tuple[int, tuple[int, int]], ...]:
    """Lane table for an interlacing mode, or raise for an unknown one."""
    try:
        return INTERLACE_PAIRS[mode]
    except KeyError:
        if mode == 1:
            raise NotImplementedError(
                "interlacing mode 1 (HardwareNameID 8 at hardware revision "
                "1.0.0) uses a different pairing (image_interlacing.go:167-170) "
                "that was not recovered from the binary and could not be "
                "verified against hardware; refusing to guess"
            ) from None
        raise ValueError(f"unknown interlacing mode {mode!r}") from None


def _pair(a: bytes, b: bytes) -> tuple[np.ndarray, np.ndarray]:
    aa = np.frombuffer(a, dtype=np.uint8)
    bb = np.frombuffer(b, dtype=np.uint8)
    if aa.size != bb.size:
        raise ValueError(f"interlace halves differ in size: {aa.size} != {bb.size}")
    return aa, bb


def interlace_4bpp(a: bytes, b: bytes) -> bytes:
    """Interleave two 4 bpp payloads, 2 bytes (4 pixels) at a time."""
    aa, bb = _pair(a, b)
    if aa.size % 2:
        raise ValueError("4 bpp interlace needs an even byte count per half")
    out = np.empty((aa.size // 2, 4), dtype=np.uint8)
    out[:, 0:2] = aa.reshape(-1, 2)
    out[:, 2:4] = bb.reshape(-1, 2)
    return out.reshape(-1).tobytes()


def deinterlace_4bpp(data: bytes) -> tuple[bytes, bytes]:
    """Inverse of :func:`interlace_4bpp` (``liveview/helpers.go:21``)."""
    g = np.frombuffer(data, dtype=np.uint8)
    if g.size % 4:
        raise ValueError("4 bpp de-interlace needs a byte count divisible by 4")
    g = g.reshape(-1, 4)
    return (g[:, 0:2].reshape(-1).tobytes(), g[:, 2:4].reshape(-1).tobytes())


def interlace_1bpp(a: bytes, b: bytes) -> bytes:
    """Interleave two 1 bpp payloads, one nibble (4 pixels) at a time."""
    aa, bb = _pair(a, b)
    out = np.empty((aa.size, 2), dtype=np.uint8)
    out[:, 0] = (aa & 0xF0) | (bb >> 4)
    out[:, 1] = ((aa << 4) & 0xFF) | (bb & 0x0F)
    return out.reshape(-1).tobytes()


def deinterlace_1bpp(data: bytes) -> tuple[bytes, bytes]:
    """Inverse of :func:`interlace_1bpp` (``liveview/helpers.go:43``)."""
    g = np.frombuffer(data, dtype=np.uint8)
    if g.size % 2:
        raise ValueError("1 bpp de-interlace needs an even byte count")
    g = g.reshape(-1, 2)
    a = (g[:, 0] & 0xF0) | (g[:, 1] >> 4)
    b = ((g[:, 0] << 4) & 0xFF) | (g[:, 1] & 0x0F)
    return (a.tobytes(), b.tobytes())


def interlace(a: bytes, b: bytes, encoding: int) -> bytes:
    """Dispatch on encoding -- ``image_interlacing.go:131-133``."""
    if encoding == Encoding.ONE_BIT:
        return interlace_1bpp(a, b)
    if encoding == Encoding.FOUR_BIT:
        return interlace_4bpp(a, b)
    raise ValueError(f"bad encoding {encoding!r}; only 1 and 4 exist (spec §2.1)")


def deinterlace(data: bytes, encoding: int) -> tuple[bytes, bytes]:
    """Inverse of :func:`interlace`."""
    if encoding == Encoding.ONE_BIT:
        return deinterlace_1bpp(data)
    if encoding == Encoding.FOUR_BIT:
        return deinterlace_4bpp(data)
    raise ValueError(f"bad encoding {encoding!r}; only 1 and 4 exist (spec §2.1)")


# --------------------------------------------------------------------------
# sub-range geometry -- what a *partial* screen rectangle needs
# --------------------------------------------------------------------------
#
# The fold is a pure permutation of columns, so it restricts to a sub-range.
# Screen column ``s`` carries lane ``(s // INTERLACE_GROUP) % 2`` at lane
# column ``(s // SCREEN_X_QUANTUM) * INTERLACE_GROUP + s % INTERLACE_GROUP``.
# Inverting that for a whole number of *paired* groups:
#
#     lane columns [c0, c1)  <->  screen columns [2*c0, 2*c1)
#
# with ``c0`` and ``c1`` multiples of ``INTERLACE_GROUP``.  Which is why
# :data:`SCREEN_X_QUANTUM` is 8 and not 4: 4 would address one lane's group
# without its partner's, and there is no way to say that on the wire.


def lane_span(screen_x: int, screen_width: int) -> tuple[int, int]:
    """Lane-column range ``[c0, c1)`` covered by a screen-space x-range.

    Both lanes of the screen cover the *same* lane columns -- that is the
    whole reason a partial update has to carry the partner lane.

    :raises ValueError: if ``screen_x`` or ``screen_width`` is not a multiple
        of :data:`SCREEN_X_QUANTUM`, or either is negative.
    """
    if screen_x < 0 or screen_width < 0:
        raise ValueError(f"negative screen x-range {screen_x}+{screen_width}")
    if screen_x % SCREEN_X_QUANTUM or screen_width % SCREEN_X_QUANTUM:
        raise ValueError(
            f"screen x={screen_x} width={screen_width} must both be multiples "
            f"of {SCREEN_X_QUANTUM} so the interleave lanes stay paired"
        )
    return (screen_x // 2, (screen_x + screen_width) // 2)


def screen_span(lane_x: int, lane_width: int) -> tuple[int, int]:
    """Inverse of :func:`lane_span`: ``(screen_x, screen_width)``.

    :raises ValueError: if ``lane_x`` or ``lane_width`` is not a multiple of
        :data:`INTERLACE_GROUP`, or either is negative.
    """
    if lane_x < 0 or lane_width < 0:
        raise ValueError(f"negative lane x-range {lane_x}+{lane_width}")
    if lane_x % INTERLACE_GROUP or lane_width % INTERLACE_GROUP:
        raise ValueError(
            f"lane x={lane_x} width={lane_width} must both be multiples of "
            f"{INTERLACE_GROUP}, the interleave group"
        )
    return (lane_x * 2, lane_width * 2)


def lane_of_display(mode: int, display: int) -> tuple[int, int]:
    """``(screen_id, lane_index)`` carrying a logical display's band.

    ``lane_index`` is ``0`` for lane A (the first half of each interleave
    group) and ``1`` for lane B.  On mode 2, display 2 is **screen 1's lane
    B** and display 3 is screen 1's lane A -- the swap.

    :raises ValueError: if no screen carries that display.
    """
    for screen_id, lanes in interlace_pairs(mode):
        for index, lane_display in enumerate(lanes):
            if lane_display == display:
                return (screen_id, index)
    raise ValueError(
        f"interlacing mode {mode} has no lane for display {display}"
    )


def displays_of_screen(mode: int, screen_id: int) -> tuple[int, int]:
    """``(lane_a_display, lane_b_display)`` for a ``ScreenID``.

    :raises ValueError: if the mode has no such screen.
    """
    for sid, lanes in interlace_pairs(mode):
        if sid == screen_id:
            return lanes
    known = sorted(sid for sid, _ in interlace_pairs(mode))
    raise ValueError(
        f"interlacing mode {mode} has no ScreenID {screen_id}; known "
        f"screens are {known}"
    )
