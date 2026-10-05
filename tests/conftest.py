"""Shared fixtures: the golden capture and a path to the raw pcap if present."""

from __future__ import annotations

import gzip
import json
import os
from pathlib import Path
from typing import Any

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "golden.json.gz"

PCAP_ENV = "PYVISIONECT_PCAP"
DEFAULT_PCAP = Path(
    "device-11113.pcap"
)


@pytest.fixture(scope="session")
def golden() -> dict[str, Any]:
    """The committed fixture extracted from the real device capture."""
    with gzip.open(FIXTURE, "rt", encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="session")
def golden_frames(golden: dict[str, Any]) -> list[dict[str, Any]]:
    return golden["frames"]


@pytest.fixture(scope="session")
def small_frames(golden_frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every frame held verbatim, i.e. all but the 1.84 MB image push."""
    return [f for f in golden_frames if "bytes" in f]


@pytest.fixture(scope="session")
def image_frame(golden_frames: list[dict[str, Any]]) -> dict[str, Any]:
    matches = [f for f in golden_frames if f.get("kind") == "image_push"]
    assert len(matches) == 1
    return matches[0]


@pytest.fixture(scope="session")
def pcap_path() -> Path | None:
    """The raw 1.9 MB pcap, if it happens to be available on this machine."""
    candidate = Path(os.environ.get(PCAP_ENV, DEFAULT_PCAP))
    return candidate if candidate.exists() else None
