"""Serialising what must survive a consumer restart.

E-ink is persistent. If a restart loses the encoder state, the only safe move is
a full-screen push -- 1.84 MB and one unit of panel wear, every restart, on a
battery device. If it keeps the state and the device's echoed
``DisplayStateCRC`` still matches, the correct action on first contact is none.
"""

from __future__ import annotations

import json

import pytest

from pyvisionect.devices.enums import PacketType
from pyvisionect.packets import StatusPacket
from pyvisionect.session import DeviceState, DeviceStateStore, PendingWork

np = pytest.importorskip("numpy", reason="FrameState holds numpy planes")

UUID = bytes.fromhex("00112233445566778899aabb00000000")

STATUS_TAGS = [
    (0, 3), (8, 0xC2050128), (9, 0xDB963A1A), (10, 100),
    (16, 7), (17, 4), (18, 4407), (22, 8), (27, 1), (43, 585), (54, 1),
]


# ------------------------------------------------------------- FrameState

@pytest.fixture
def frame_state():
    from pyvisionect.imaging import FrameState

    rng = np.random.default_rng(7)
    return FrameState(
        state_grey=rng.integers(0, 256, (64, 48), dtype=np.uint8),
        source_grey=rng.integers(0, 256, (64, 48), dtype=np.uint8),
        checksum=0xDEADBEEF,
        encoding=4,
        dithering=3,
    )


def test_frame_state_round_trips(frame_state) -> None:
    from pyvisionect.imaging import FrameState

    blob = frame_state.to_bytes()
    restored = FrameState.from_bytes(blob)
    assert np.array_equal(restored.state_grey, frame_state.state_grey)
    assert np.array_equal(restored.source_grey, frame_state.source_grey)
    assert restored.checksum == frame_state.checksum
    assert restored.encoding == frame_state.encoding
    assert restored.dithering == frame_state.dithering


def test_frame_state_round_trips_uncompressed(frame_state) -> None:
    from pyvisionect.imaging import FrameState

    blob = frame_state.to_bytes(compress=False)
    restored = FrameState.from_bytes(blob)
    assert np.array_equal(restored.state_grey, frame_state.state_grey)


def test_frame_state_blob_is_self_describing_and_versioned(frame_state) -> None:
    from pyvisionect.imaging import FrameState

    blob = frame_state.to_bytes()
    assert blob[:4] == FrameState.SERIAL_MAGIC == b"VNFS"
    assert blob[4] == FrameState.SERIAL_VERSION == 1


def test_frame_state_is_tiny_for_real_e_ink_content() -> None:
    """Mostly-white content is the normal case and compresses enormously."""
    from pyvisionect.imaging import FrameState

    state = FrameState(
        state_grey=np.full((2560, 1440), 255, np.uint8),
        source_grey=np.zeros((2560, 1440), np.uint8),
        checksum=1,
        encoding=4,
        dithering=1,
    )
    blob = state.to_bytes()
    raw = 2 * 2560 * 1440
    assert len(blob) < raw // 100, f"{len(blob)} vs {raw} raw"
    assert np.array_equal(FrameState.from_bytes(blob).state_grey, state.state_grey)


def test_frame_state_rejects_a_foreign_blob() -> None:
    from pyvisionect.imaging import FrameState

    with pytest.raises(ValueError, match="magic"):
        FrameState.from_bytes(b"PNG\x00" + b"\x00" * 40)
    with pytest.raises(ValueError, match="at least"):
        FrameState.from_bytes(b"VNFS")


def test_frame_state_rejects_a_future_version(frame_state) -> None:
    from pyvisionect.imaging import FrameState

    blob = bytearray(frame_state.to_bytes())
    blob[4] = 99
    with pytest.raises(ValueError, match="version 99"):
        FrameState.from_bytes(bytes(blob))


def test_frame_state_rejects_a_truncated_blob(frame_state) -> None:
    from pyvisionect.imaging import FrameState

    blob = frame_state.to_bytes()
    with pytest.raises(ValueError, match="header describes"):
        FrameState.from_bytes(blob[:-5])


def test_frame_state_survives_a_round_trip_through_a_json_store(frame_state) -> None:
    """How a consumer actually persists it: base64 inside a JSON blob."""
    import base64

    from pyvisionect.imaging import FrameState

    stored = json.dumps({"state": base64.b64encode(frame_state.to_bytes()).decode()})
    restored = FrameState.from_bytes(base64.b64decode(json.loads(stored)["state"]))
    assert restored.checksum == frame_state.checksum


# ------------------------------------------------------------ DeviceState

def test_device_state_round_trips() -> None:
    state = DeviceState(device_id=UUID)
    state.apply_status(StatusPacket(records=STATUS_TAGS, sentinel=0x779050DF))
    state.pushed_checksum = 0xDB963A1A
    state.connections = 3
    state.options["MergeRegions"] = "true,threshold=5"
    state.pending.read_params([18, 19])
    state.pending.sleep(30)

    restored = DeviceState.from_dict(json.loads(json.dumps(state.to_dict())))
    assert restored.device_id == UUID
    assert restored.pushed_checksum == 0xDB963A1A
    assert restored.connections == 3
    assert restored.options == {"MergeRegions": "true,threshold=5"}
    assert restored.last_status.records == STATUS_TAGS
    assert restored.last_status.sentinel == 0x779050DF
    assert restored.pending.param_reads == {18, 19}
    assert restored.pending.sleep_minutes == 30
    # And the derived views still work off the restored status.
    assert restored.display_type == 0xC2050128
    assert restored.hardware_name_id == 8
    assert restored.in_sync is True


def test_device_state_round_trips_with_no_status_yet() -> None:
    state = DeviceState(device_id=UUID)
    restored = DeviceState.from_dict(state.to_dict())
    assert restored.last_status is None
    assert restored.in_sync is None


def test_device_state_omits_the_imaging_state() -> None:
    """It is two numpy planes; FrameState.to_bytes owns that format."""
    state = DeviceState(device_id=UUID)
    state.imaging_state = object()
    assert "imaging_state" not in state.to_dict()


def test_device_state_rejects_a_future_version() -> None:
    raw = DeviceState(device_id=UUID).to_dict()
    raw["v"] = 42
    with pytest.raises(ValueError, match="version 42"):
        DeviceState.from_dict(raw)


def test_store_round_trips_wholesale() -> None:
    store = DeviceStateStore()
    first = store.get(UUID)
    first.pushed_checksum = 7
    store.get(b"\x01" * 16).connections = 2

    restored = DeviceStateStore.from_dict(json.loads(json.dumps(store.to_dict())))
    assert len(restored) == 2
    assert restored.get(UUID).pushed_checksum == 7
    assert restored.get(b"\x01" * 16).connections == 2


def test_restored_store_drives_a_connection() -> None:
    from test_session import device_frame, make_conn, status_payload

    store = DeviceStateStore()
    store.queue(UUID).read_params([29])
    store.get(UUID).pushed_checksum = 0xDB963A1A

    revived = DeviceStateStore.from_dict(store.to_dict())
    conn = make_conn(store=revived)
    conn.feed(device_frame(PacketType.STATUS, status_payload(), 1))
    conn.drain()
    assert conn.state.pushed_checksum == 0xDB963A1A
    assert conn.state.pending.param_reads == {29}
    assert "param_reads" in conn.apply_pending()
