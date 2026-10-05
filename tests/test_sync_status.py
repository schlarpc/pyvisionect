"""The four-state sync verdict: the fix for the "out of sync" false alarm.

``DeviceState.in_sync`` is right and unusable as an alarm -- it says False for
the whole of the device's draw-and-report cycle after every normal push, which
on the live 31.2" sign is 48 s of a Home Assistant *problem* indicator per
update.  :meth:`DeviceState.sync_status` splits that False into "has not
answered yet" and "is showing the wrong thing".
"""

from __future__ import annotations

import pytest

from pyvisionect.packets import StatusPacket
from pyvisionect.session import ConnectionConfig, DeviceConnection, SyncStatus
from pyvisionect.session.device import (
    CONVERGENCE_CONTACTS,
    DRAW_ALLOWANCE,
    DeviceState,
)

MINUTE = 60.0

DEVICE_ID = bytes.fromhex("21002b00055137303234393600000000")

TAG_DISPLAY_STATE_CRC = 9
TAG_NEXT_STATUS = 27


def status(*, crc: int | None = None, next_status: int | None = 1) -> StatusPacket:
    records: list[tuple[int, int]] = []
    if crc is not None:
        records.append((TAG_DISPLAY_STATE_CRC, crc))
    if next_status is not None:
        records.append((TAG_NEXT_STATUS, next_status))
    return StatusPacket(records=records)


def state(**kwargs) -> DeviceState:
    return DeviceState(device_id=DEVICE_ID, **kwargs)


def test_nothing_pushed_is_unknown_not_a_problem() -> None:
    dev = state()
    dev.apply_status(status(crc=1234))
    assert dev.in_sync is None
    assert dev.sync_status(now=1e9) is SyncStatus.UNKNOWN


def test_matching_checksum_is_in_sync() -> None:
    dev = state()
    dev.apply_status(status(crc=4242))
    dev.note_push(4242, now=100.0)
    assert dev.sync_status(now=100.0) is SyncStatus.IN_SYNC


def test_the_window_right_after_a_push_is_converging_not_a_problem() -> None:
    """The actual bug. The device still reports the *old* checksum here."""
    dev = state()
    dev.apply_status(status(crc=1111))
    dev.note_push(2222, now=0.0)
    assert dev.in_sync is False, "the raw tri-state is still correct"
    assert dev.sync_status(now=0.0) is SyncStatus.CONVERGING
    # 48 s later -- the measured convergence time on the live sign -- and
    # still before the device's second contact.
    assert dev.sync_status(now=48.0) is SyncStatus.CONVERGING


def test_one_contact_is_not_enough() -> None:
    """A push landing just before a heartbeat is acked but not yet drawn."""
    dev = state()
    dev.apply_status(status(crc=1111))
    dev.note_push(2222, now=0.0)
    dev.apply_status(status(crc=1111))  # contact 1: drawn? maybe not
    assert dev.contacts_since_push == 1
    assert dev.sync_status(now=5.0) is SyncStatus.CONVERGING


def test_two_contacts_still_disagreeing_is_a_real_problem() -> None:
    dev = state()
    dev.apply_status(status(crc=1111))
    dev.note_push(2222, now=0.0)
    for _ in range(CONVERGENCE_CONTACTS):
        dev.apply_status(status(crc=1111))
    assert dev.sync_status(now=dev.settle_time()) is SyncStatus.DIVERGED


def test_the_burst_of_contacts_around_a_draw_does_not_count() -> None:
    """The live failure mode this gate exists for.

    The 31.2" sign emits nine status packets in the 55 s after a push and then
    one a minute. Taking the bare counter at face value would call a frame
    diverged fifteen seconds in, while it is still on the wire.
    """
    dev = state()
    dev.apply_status(status(crc=1111, next_status=1))
    dev.note_push(2222, now=0.0)
    for _ in range(9):
        dev.apply_status(status(crc=1111))
    assert dev.contacts_since_push == 9
    assert dev.settle_time() == pytest.approx(MINUTE + DRAW_ALLOWANCE)
    assert dev.sync_status(now=15.0) is SyncStatus.CONVERGING
    assert dev.sync_status(now=55.0) is SyncStatus.CONVERGING
    assert dev.sync_status(now=dev.settle_time()) is SyncStatus.DIVERGED


def test_the_settle_time_clears_the_longest_convergence_we_measured() -> None:
    """48 s was the slowest push-to-echo seen on this sign; 120 s is the gate."""
    dev = state()
    dev.apply_status(status(crc=1111, next_status=1))
    dev.note_push(2222, now=0.0)
    for _ in range(5):
        dev.apply_status(status(crc=1111))
    assert dev.sync_status(now=48.0) is SyncStatus.CONVERGING


def test_the_device_reporting_our_checksum_clears_it() -> None:
    dev = state()
    dev.apply_status(status(crc=1111))
    dev.note_push(2222, now=0.0)
    dev.apply_status(status(crc=1111))
    dev.apply_status(status(crc=2222))
    assert dev.sync_status(now=1e9) is SyncStatus.IN_SYNC


def test_a_silent_device_trips_the_deadline() -> None:
    """The contact counter cannot fire if the device stops calling."""
    dev = state()
    dev.apply_status(status(crc=1111, next_status=1))
    dev.note_push(2222, now=0.0)
    grace = dev.convergence_grace()
    assert grace == pytest.approx(CONVERGENCE_CONTACTS * MINUTE + DRAW_ALLOWANCE)
    assert dev.contacts_since_push == 0, "it never called back"
    assert dev.sync_status(now=grace - 1) is SyncStatus.CONVERGING
    assert dev.sync_status(now=grace) is SyncStatus.DIVERGED


def test_the_deadline_follows_the_device_announced_schedule() -> None:
    """A sign on an hourly heartbeat is not broken for the 59 idle minutes.

    This is the difference between a timeout chosen by us and a window the
    device itself declared in ``NextStatus``.
    """
    dev = state()
    dev.apply_status(status(crc=1111, next_status=60))
    dev.note_push(2222, now=0.0)
    assert dev.expected_contact_interval == 3600.0
    assert dev.sync_status(now=3600.0) is SyncStatus.CONVERGING
    assert dev.sync_status(now=2 * 3600.0 + DRAW_ALLOWANCE) is SyncStatus.DIVERGED


def test_a_chatty_device_on_a_long_heartbeat_is_judged_sooner() -> None:
    """The point of keeping the counter at all.

    Waiting two hours to notice a sign is showing the wrong thing, when it has
    told us twice in the meantime, would be absurd.
    """
    dev = state()
    dev.apply_status(status(crc=1111, next_status=60))
    dev.note_push(2222, now=0.0)
    dev.apply_status(status(crc=1111))
    dev.apply_status(status(crc=1111))
    assert dev.sync_status(now=dev.settle_time()) is SyncStatus.DIVERGED
    assert dev.settle_time() < dev.convergence_grace()


def test_without_a_clock_only_the_contact_counter_applies() -> None:
    """A restored state has no usable push timestamp and must not guess."""
    dev = state()
    dev.apply_status(status(crc=1111))
    dev.note_push(2222, now=None)
    assert dev.convergence_deadline() is None
    assert dev.sync_status(now=1e9) is SyncStatus.CONVERGING
    dev.apply_status(status(crc=1111))
    dev.apply_status(status(crc=1111))
    assert dev.sync_status() is SyncStatus.DIVERGED
    assert dev.sync_status(now=1e9) is SyncStatus.DIVERGED


def test_a_device_that_reports_no_crc_at_all_is_unknown() -> None:
    dev = state()
    dev.apply_status(status(crc=None))
    dev.note_push(2222, now=0.0)
    assert dev.sync_status(now=1e9) is SyncStatus.UNKNOWN


def test_a_second_push_resets_the_window() -> None:
    dev = state()
    dev.apply_status(status(crc=1111))
    dev.note_push(2222, now=0.0)
    dev.apply_status(status(crc=1111))
    dev.apply_status(status(crc=1111))
    assert dev.sync_status(now=500.0) is SyncStatus.DIVERGED
    dev.note_push(3333, now=500.0)
    assert dev.contacts_since_push == 0
    assert dev.sync_status(now=500.0) is SyncStatus.CONVERGING


def test_counters_survive_a_round_trip_but_the_clock_does_not() -> None:
    dev = state()
    dev.apply_status(status(crc=1111))
    dev.note_push(2222, now=123.0)
    dev.apply_status(status(crc=1111))
    restored = DeviceState.from_dict(dev.to_dict())
    assert restored.pushes == 1
    assert restored.contacts_since_push == 1
    assert restored.last_push_at is None, (
        "a monotonic clock value means nothing after a restart and must not "
        "be persisted"
    )


def test_a_pre_existing_snapshot_still_restores() -> None:
    """Old snapshots have none of the new keys and must not raise."""
    old = {
        "v": 1,
        "device_id": DEVICE_ID.hex(),
        "pushed_checksum": 7,
        "connections": 3,
        "options": {},
        "pending": None,
        "last_status": None,
    }
    restored = DeviceState.from_dict(old)
    assert restored.pushed_checksum == 7
    assert restored.pushes == 0
    assert restored.statuses_at_push is None
    assert restored.contacts_since_push == 0


def _connected() -> DeviceConnection:
    conn = DeviceConnection(config=ConnectionConfig(require_first_status=False))
    conn._device_id = DEVICE_ID
    conn._state = conn.store.get(DEVICE_ID)
    return conn


def test_send_image_records_the_push_against_the_clock() -> None:
    conn = _connected()

    class Frame:
        rectangles: list = []
        state_checksum = 99
        state = None

    conn.send_image(Frame(), now=777.0)
    assert conn.state.pushed_checksum == 99
    assert conn.state.pushes == 1
    assert conn.state.last_push_at == 777.0


def test_apply_pending_passes_its_clock_to_the_push() -> None:
    conn = _connected()

    class Frame:
        rectangles: list = []
        state_checksum = 5
        state = None

    conn.state.pending.set_image(Frame(), now=10.0)
    conn.apply_pending(now=10.0)
    assert conn.state.last_push_at == 10.0
