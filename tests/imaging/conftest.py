"""Shared fixtures for the imaging tests."""

from __future__ import annotations

import json
import lzma
import pathlib
import sys

import numpy as np
import pytest

# Allow running the suite straight out of the source tree without an installed
# package (src layout).
_SRC = pathlib.Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

DATA = pathlib.Path(__file__).parent / "data"


@pytest.fixture(scope="session")
def golden_meta() -> dict:
    """Headers and payload hashes of the real captured image push."""
    return json.loads((DATA / "golden_capture.json").read_text())


@pytest.fixture(scope="session")
def golden_canvas(golden_meta) -> np.ndarray:
    """The 1440x2560 session canvas reconstructed from the captured payloads.

    This is **real device traffic**, not synthetic: it was de-interlaced,
    nibble-decoded and un-mirrored out of the two 2880x640 rectangles the
    vendor server pushed to the sign.
    """
    with lzma.open(DATA / "golden_canvas_1440x2560_grey8.raw.xz", "rb") as fh:
        raw = fh.read()
    w = golden_meta["canvas"]["width"]
    h = golden_meta["canvas"]["height"]
    assert len(raw) == w * h
    return np.frombuffer(raw, dtype=np.uint8).reshape(h, w)


@pytest.fixture(scope="session")
def golden_payloads(golden_canvas, golden_meta) -> dict[int, bytes]:
    """The two captured wire payloads, rebuilt from the canvas and verified.

    The payload bytes are not committed (1.8 MB of near-incompressible
    dithered data); the canvas plus the interlacing map reproduces them, and
    the committed SHA-256 digests prove the reconstruction is the original.
    """
    import hashlib

    from pyvisionect.imaging import (INTERLACE_PAIRS, interlace_4bpp,
                                     mirror_image, pack_4bpp)

    bands = [mirror_image(golden_canvas[i * 640:(i + 1) * 640]) for i in range(4)]
    packed = [pack_4bpp(np.ascontiguousarray(b)) for b in bands]
    out: dict[int, bytes] = {}
    for screen_id, (lane_a, lane_b) in INTERLACE_PAIRS[2]:
        out[screen_id] = interlace_4bpp(packed[lane_a], packed[lane_b])
    for entry in golden_meta["rectangles"]:
        sid = entry["ScreenID"]
        assert hashlib.sha256(out[sid]).hexdigest() == entry["sha256"], (
            f"golden fixture is corrupt: screen {sid} does not hash to the "
            "digest recorded from the pcap"
        )
    return out
