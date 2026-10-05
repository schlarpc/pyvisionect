"""The direction-dependent checksum. The single most dangerous detail here."""

from __future__ import annotations

import random
import struct

import pytest

from pyvisionect.wire import Direction, crc32_ieee, frame_checksum, verify_checksum

# From the real capture: every device->server status frame carries this constant
# because (Version=3, Security=0, Compression=0, Length=532) is identical in all
# of them -- the device's CRC does not cover the payload.
CAPTURED_DEVICE_HEADER16 = struct.pack("<4I", 3, 0, 0, 532)
CAPTURED_DEVICE_CHECKSUM = 0x198CEB01


def test_device_checksum_matches_the_captured_constant() -> None:
    assert (
        frame_checksum(
            Direction.DEVICE_TO_SERVER,
            header16=CAPTURED_DEVICE_HEADER16,
            body=b"anything at all",
        )
        == CAPTURED_DEVICE_CHECKSUM
    )


def test_device_checksum_ignores_the_body() -> None:
    """Two statuses ten minutes apart carried the same checksum. That is why."""
    a = frame_checksum(
        Direction.DEVICE_TO_SERVER, header16=CAPTURED_DEVICE_HEADER16, body=b"A" * 532
    )
    b = frame_checksum(
        Direction.DEVICE_TO_SERVER, header16=CAPTURED_DEVICE_HEADER16, body=b"B" * 532
    )
    assert a == b == CAPTURED_DEVICE_CHECKSUM


def test_server_checksum_ignores_the_header() -> None:
    body = b"the gateway CRCs the body, and only the body"
    a = frame_checksum(
        Direction.SERVER_TO_DEVICE, header16=struct.pack("<4I", 3, 0, 1, 68), body=body
    )
    b = frame_checksum(
        Direction.SERVER_TO_DEVICE,
        header16=struct.pack("<4I", 3, 0, 1, 99999),
        body=body,
    )
    assert a == b == crc32_ieee(body)


def test_the_two_rules_really_differ() -> None:
    """A symmetric implementation would pass a naive test and fail on the wire."""
    header16 = struct.pack("<4I", 3, 0, 1, 44)
    body = b"x" * 44
    assert frame_checksum(
        Direction.SERVER_TO_DEVICE, header16=header16, body=body
    ) != frame_checksum(Direction.DEVICE_TO_SERVER, header16=header16, body=body)


def test_direction_is_not_optional() -> None:
    with pytest.raises(ValueError, match="not symmetric"):
        frame_checksum("server", header16=b"\0" * 16, body=b"")  # type: ignore[arg-type]


def test_header16_length_is_checked() -> None:
    with pytest.raises(ValueError, match="exactly 16 bytes"):
        frame_checksum(Direction.SERVER_TO_DEVICE, header16=b"\0" * 20, body=b"")


def test_verify_round_trips_in_both_directions() -> None:
    rng = random.Random(1312)
    for _ in range(200):
        length = rng.randrange(0, 4096)
        body = bytes(rng.randrange(256) for _ in range(min(length, 64)))
        header16 = struct.pack("<4I", 3, 0, rng.choice([0, 1]), length)
        for direction in Direction:
            checksum = frame_checksum(direction, header16=header16, body=body)
            assert verify_checksum(
                direction, header16=header16, body=body, checksum=checksum
            )


def test_crc32_is_the_ieee_polynomial() -> None:
    # The standard check value for CRC-32/ISO-HDLC over "123456789".
    assert crc32_ieee(b"123456789") == 0xCBF43926
