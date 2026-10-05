"""The durable, coalescing per-device work register."""

from __future__ import annotations

import pytest

from pyvisionect.devices.enums import CommandType, PacketType
from pyvisionect.packets import CommandPacket, ImagePacket, ParamPacket
from pyvisionect.session import (
    PENDING_ORDER,
    SLOT_FLASH_SAVE,
    SLOT_GHOST_CLEAR,
    SLOT_IMAGE,
    SLOT_PARAM_READS,
    SLOT_PARAM_WRITES,
    SLOT_REBOOT,
    SLOT_SLEEP,
    DeviceStateStore,
    PendingWork,
)
from pyvisionect.wire import ReadOnlyParameter

from test_session import (  # noqa: E402
    UUID_A,
    device_frame,
    make_conn,
    sent_frames,
    status_payload,
)


class FakeRect:
    def __init__(self, screen_id: int = 0) -> None:
        self.x = self.y = 0
        self.w, self.h = 8, 2
        self.screen_id = screen_id
        self.options = 0
        self.encoding = 4
        self.data = b"\x00" * 8


class FakeFrame:
    def __init__(self, checksum: int = 0x11111111) -> None:
        self.rectangles = [FakeRect()]
        self.state_checksum = checksum
        self.state = f"state-{checksum}"


# ------------------------------------------------------------- coalescing

def test_empty_by_default() -> None:
    work = PendingWork()
    assert work.is_empty
    assert work.slots() == []


def test_content_is_last_write_wins_with_depth_one() -> None:
    """Pushing ten images to a sleeping sign must show the tenth, not all ten."""
    work = PendingWork()
    frames = [FakeFrame(i) for i in range(10)]
    for frame in frames:
        work.set_image(frame)
    assert work.frame is frames[-1]
    assert work.content_revision == 10
    assert work.slots() == [SLOT_IMAGE]


def test_param_writes_are_last_write_wins_per_id() -> None:
    work = PendingWork()
    work.write_param(29, 5)
    work.write_param(29, 30)
    work.write_param(30, 7)
    assert work.params == {29: 30, 30: 7}


def test_param_reads_are_a_set_union() -> None:
    work = PendingWork()
    work.read_params([29, 18])
    work.read_params([18, 19])
    assert work.param_reads == {18, 19, 29}


def test_commands_have_set_semantics() -> None:
    work = PendingWork()
    work.reboot()
    work.reboot()
    work.ghost_clear()
    work.ghost_clear()
    assert work.want_reboot is True
    assert work.want_ghost_clear is True
    assert work.slots() == [SLOT_GHOST_CLEAR, SLOT_REBOOT]


def test_sleep_replaces_rather_than_accumulating() -> None:
    work = PendingWork()
    work.sleep(10)
    work.sleep(60)
    assert work.sleep_minutes == 60


def test_sleep_refuses_zero_and_negative() -> None:
    work = PendingWork()
    for bad in (0, -1):
        with pytest.raises(ValueError, match="errSleepTime"):
            work.sleep(bad)
    assert work.sleep_minutes is None


def test_framebuffer_read_is_a_single_slot() -> None:
    work = PendingWork()
    work.read_framebuffer("auto")
    work.read_framebuffer("all")
    assert work.framebuffer_read == "all"


def test_write_to_a_read_only_param_is_refused_at_queue_time() -> None:
    """Fail now, not in an hour when the device finally wakes up."""
    work = PendingWork()
    with pytest.raises(ReadOnlyParameter, match="server_tcp_set"):
        work.write_param(18, "10.0.0.1")
    assert work.params == {}


def test_command_dispatch_by_name() -> None:
    work = PendingWork()
    work.command("ghost_clear")
    work.command("reboot")
    work.command("sleep", minutes=5)
    assert (work.want_ghost_clear, work.want_reboot, work.sleep_minutes) == (
        True, True, 5
    )
    with pytest.raises(ValueError, match="unknown command kind"):
        work.command("explode")


# ----------------------------------------------------------------- order

def test_pending_order_is_the_vendor_s() -> None:
    assert PENDING_ORDER == (
        "param_reads",
        "param_writes",
        "flash_save",
        "framebuffer_read",
        "image",
        "ghost_clear",
        "sleep",
        "reboot",
    )
    # The two constraints that actually matter.
    assert PENDING_ORDER.index(SLOT_SLEEP) > PENDING_ORDER.index(SLOT_IMAGE)
    assert PENDING_ORDER[-1] == SLOT_REBOOT
    assert PENDING_ORDER.index(SLOT_FLASH_SAVE) > PENDING_ORDER.index(
        SLOT_PARAM_WRITES
    )
    assert PENDING_ORDER.index(SLOT_PARAM_READS) < PENDING_ORDER.index(
        SLOT_PARAM_WRITES
    )


def test_slots_are_reported_in_order_whatever_order_they_were_queued() -> None:
    work = PendingWork()
    work.reboot()
    work.sleep(5)
    work.set_image(FakeFrame())
    work.write_param(29, 1)
    work.read_params([18])
    assert work.slots() == [
        SLOT_PARAM_READS,
        SLOT_PARAM_WRITES,
        SLOT_FLASH_SAVE,
        SLOT_IMAGE,
        SLOT_SLEEP,
        SLOT_REBOOT,
    ]


def test_flash_save_only_when_there_are_writes() -> None:
    work = PendingWork()
    work.read_params([1])
    assert SLOT_FLASH_SAVE not in work.slots()
    work.write_param(29, 1)
    assert SLOT_FLASH_SAVE in work.slots()
    work.persist_params = False
    assert SLOT_FLASH_SAVE not in work.slots()


# --------------------------------------------------------- apply on connect

def _connected():
    conn = make_conn()
    conn.feed(device_frame(PacketType.STATUS, status_payload(), 1))
    conn.drain()
    return conn


def test_apply_pending_puts_everything_on_the_wire_in_order() -> None:
    conn = _connected()
    work = conn.state.pending
    work.reboot()
    work.sleep(30)
    work.set_image(FakeFrame())
    work.write_param(29, 10)
    work.read_params([18, 19])

    sent = conn.apply_pending(now=100.0)
    assert list(sent) == [
        SLOT_PARAM_READS,
        SLOT_PARAM_WRITES,
        SLOT_FLASH_SAVE,
        SLOT_IMAGE,
        SLOT_SLEEP,
        SLOT_REBOOT,
    ]

    frames = sent_frames(conn)
    types = [f.type for f in frames]
    assert types == [
        PacketType.PARAM,   # reads
        PacketType.PARAM,   # writes
        PacketType.PARAM,   # flash_save
        PacketType.IMAGE,
        PacketType.COMMAND, # sleep
        PacketType.COMMAND, # reboot
    ]
    # The ordering fact that matters: the image bytes precede the sleep.
    image_index = types.index(PacketType.IMAGE)
    sleep_index = next(
        i for i, f in enumerate(frames)
        if f.type == PacketType.COMMAND
        and CommandPacket.decode(f.payload).type == CommandType.SLEEP
    )
    assert image_index < sleep_index
    assert CommandPacket.decode(frames[-1].payload).type == CommandType.REBOOT


def test_sleep_payload_is_a_little_endian_uint32_of_minutes() -> None:
    conn = _connected()
    conn.state.pending.sleep(45)
    conn.apply_pending()
    frame = sent_frames(conn)[0]
    command = CommandPacket.decode(frame.payload)
    assert command.type == CommandType.SLEEP
    assert command.payload == (45).to_bytes(4, "little")


def test_flash_save_writes_tclv_53() -> None:
    conn = _connected()
    conn.state.pending.write_param(29, 3)
    conn.apply_pending()
    frames = sent_frames(conn)
    save = ParamPacket.decode(frames[-1].payload)
    assert [item.id for item in save] == [53]


def test_apply_pending_phases_lets_a_caller_wait_between_steps() -> None:
    conn = _connected()
    work = conn.state.pending
    work.read_params([18])
    work.set_image(FakeFrame())
    work.sleep(5)

    first = conn.apply_pending(phases=[SLOT_PARAM_READS])
    assert list(first) == [SLOT_PARAM_READS]
    assert len(sent_frames(conn)) == 1

    rest = conn.apply_pending(phases=[SLOT_SLEEP, SLOT_IMAGE])
    # Order is PENDING_ORDER's, not the caller's.
    assert list(rest) == [SLOT_IMAGE, SLOT_SLEEP]


def test_image_push_uses_the_frame_checksum_and_stores_the_state() -> None:
    conn = _connected()
    conn.state.pending.set_image(FakeFrame(0xABCDEF01))
    conn.apply_pending()
    packet = ImagePacket.decode(sent_frames(conn)[0].payload)
    assert packet.checksum == 0xABCDEF01
    assert conn.state.pushed_checksum == 0xABCDEF01
    assert conn.state.imaging_state == "state-2882400001"


def test_force_push_perturbs_the_checksum() -> None:
    conn = _connected()
    conn.state.pending.set_image(FakeFrame(0x11111111), force=True)
    conn.apply_pending()
    packet = ImagePacket.decode(sent_frames(conn)[0].payload)
    assert packet.checksum != 0x11111111
    assert packet.checksum == 0x11111111 ^ 0x80000000


def test_apply_pending_without_a_device_raises() -> None:
    conn = make_conn()
    with pytest.raises(RuntimeError, match="first status packet"):
        conn.apply_pending()


# --------------------------------------------------------- ack bookkeeping

def test_slots_clear_only_on_ack() -> None:
    conn = _connected()
    work = conn.state.pending
    work.read_params([18])
    work.write_param(29, 1)
    sent = conn.apply_pending()
    # Nothing is cleared merely by being sent.
    assert work.param_reads == {18}
    assert work.params == {29: 1}

    assert work.on_acked(sent[SLOT_PARAM_READS]) == SLOT_PARAM_READS
    assert work.param_reads == set()
    assert work.params == {29: 1}

    assert work.on_acked(sent[SLOT_PARAM_WRITES]) == SLOT_PARAM_WRITES
    assert work.params == {}


def test_a_nack_leaves_the_work_queued() -> None:
    conn = _connected()
    work = conn.state.pending
    work.sleep(10)
    sent = conn.apply_pending()
    assert work.on_nacked(sent[SLOT_SLEEP]) == SLOT_SLEEP
    assert work.sleep_minutes == 10, "a NACK means it did not happen"


def test_charging_nack_is_recorded_as_retry_later() -> None:
    """Control bit 25 is 'refused because charging' -- not a failure."""
    work = PendingWork()
    work.set_image(FakeFrame())
    work.note_sent(SLOT_IMAGE, 7)
    work.on_nacked(7, charging=True)
    assert work.last_nack_charging is True
    assert work.content_pending, "the push must stay queued"
    work.note_sent(SLOT_IMAGE, 8)
    work.on_acked(8)
    assert work.last_nack_charging is False
    assert not work.content_pending


def test_a_dropped_connection_mid_reconcile_leaves_the_rest_queued() -> None:
    conn = _connected()
    work = conn.state.pending
    work.read_params([18])
    work.set_image(FakeFrame())
    work.sleep(5)
    sent = conn.apply_pending()
    work.on_acked(sent[SLOT_PARAM_READS])     # only the first landed
    work.reset_inflight()                     # the link dropped
    assert work.slots() == [SLOT_IMAGE, SLOT_SLEEP]
    assert not work.is_empty


def test_attempts_are_counted() -> None:
    work = PendingWork()
    work.note_sent(SLOT_IMAGE, 1)
    work.note_sent(SLOT_IMAGE, 2)
    assert work.attempts[SLOT_IMAGE] == 2


def test_queued_at_records_the_callers_clock() -> None:
    work = PendingWork()
    work.read_params([1], now=12.5)
    assert work.queued_at[SLOT_PARAM_READS] == 12.5


# ----------------------------------------------------------- persistence

def test_round_trips_through_to_dict() -> None:
    work = PendingWork()
    work.read_params([18, 19], now=1.0)
    work.write_param(29, 30)
    work.write_param(108, "3830065461792")
    work.write_param(109, b"\x02")
    work.sleep(90)
    work.reboot()
    work.ghost_clear()
    work.read_framebuffer("auto")
    work.set_image(FakeFrame())

    restored = PendingWork.from_dict(work.to_dict())
    assert restored.params == work.params
    assert restored.param_reads == work.param_reads
    assert restored.sleep_minutes == 90
    assert restored.want_reboot and restored.want_ghost_clear
    assert restored.framebuffer_read == "auto"
    assert restored.content_revision == work.content_revision
    # The frame itself is not persisted: megabytes of pixels, cheaper to
    # re-encode. The revision survives so the intent is not lost.
    assert restored.frame is None
    assert not restored.content_pending


def test_to_dict_is_json_safe() -> None:
    import json

    work = PendingWork()
    work.write_param(109, b"\xde\xad")
    work.read_params([1])
    blob = json.dumps(work.to_dict())
    assert PendingWork.from_dict(json.loads(blob)).params == {109: b"\xde\xad"}


def test_unknown_version_raises_rather_than_mis_restoring() -> None:
    raw = PendingWork().to_dict()
    raw["v"] = 99
    with pytest.raises(ValueError, match="version 99"):
        PendingWork.from_dict(raw)


# ------------------------------------------------------------------ store

def test_store_queue_is_per_uuid_and_auto_creates() -> None:
    store = DeviceStateStore()
    first = store.queue(UUID_A)
    first.sleep(5)
    assert store.queue(UUID_A) is first
    assert store.get(UUID_A).pending is first


def test_queue_survives_a_reconnect() -> None:
    store = DeviceStateStore()
    store.queue(UUID_A).set_image(FakeFrame())

    conn = make_conn(store=store)
    conn.feed(device_frame(PacketType.STATUS, status_payload(), 1))
    conn.drain()
    assert conn.state.pending.content_pending
    sent = conn.apply_pending()
    assert SLOT_IMAGE in sent
