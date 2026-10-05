"""The sans-io DeviceConnection state machine."""

from __future__ import annotations

import itertools
import struct

import pytest

from pyvisionect.devices.enums import CommandType, ControlFlags, PacketType
from pyvisionect.packets import ControlPacket, ParamPacket, StatusPacket
from pyvisionect.session import (
    Acked,
    ConnectionConfig,
    DeviceConnected,
    DeviceConnection,
    DeviceStateStore,
    HeartbeatOverdue,
    Nacked,
    ParamsReceived,
    ProtocolViolation,
    ReadTimeout,
    StatusReceived,
    UnknownPacketType,
    WatchdogFired,
)
from pyvisionect.session.connection import (
    DEFAULT_PACKET_TIMEOUT,
    WATCHDOG_FALLBACK,
)
from pyvisionect.wire import (
    Compression,
    DataHeader,
    Direction,
    FrameDecoder,
    ReadOnlyParameter,
    encode_frame,
)

UUID_A = bytes.fromhex("00112233445566778899aabb00000000")
UUID_B = bytes.fromhex("ffffffffffffffffffffffffffffffff")

# A minimal but realistic status: firmware 7.4.4407 (so the watchdog arms),
# NextStatus = 1 minute, HardwareNameID 8, DisplayType 0xC2050128.
STATUS_TAGS = [
    (0, 3), (1, 0), (8, 0xC2050128), (9, 0xDB963A1A), (10, 100),
    (16, 7), (17, 4), (18, 4407), (22, 8), (27, 1), (38, 2880), (39, 640),
    (40, 3), (43, 585), (54, 1),
]


def status_payload(connect_reason: int = 3) -> bytes:
    tags = [(0, connect_reason)] + STATUS_TAGS[1:]
    return StatusPacket(records=tags, sentinel=0xDEADBEEF).encode()


def device_frame(packet_type: int, payload: bytes, packet_id: int,
                 uuid: bytes = UUID_A) -> bytes:
    return encode_frame(
        DataHeader(device_id=uuid, type=packet_type, id=packet_id,
                   length=len(payload)),
        payload,
        direction=Direction.DEVICE_TO_SERVER,
        compression=Compression.NONE,
    )


def sent_frames(conn: DeviceConnection) -> list:
    return FrameDecoder(Direction.SERVER_TO_DEVICE, strict=True).feed(conn.drain())


def make_conn(**kwargs) -> DeviceConnection:
    counter = itertools.count(0x1000_0000)
    return DeviceConnection(
        id_source=lambda: next(counter), jitter=lambda: 0.0, **kwargs
    )


# ---------------------------------------------------------------- connect

def test_first_status_identifies_the_device() -> None:
    conn = make_conn()
    events = conn.feed(device_frame(PacketType.STATUS, status_payload(5), 1))
    assert [type(e) for e in events] == [DeviceConnected, StatusReceived]
    assert conn.device_id == UUID_A
    assert events[0].uuid == "00112233-4455-6677-8899-aabb00000000"
    assert events[0].connect_reason == 5
    assert conn.state is not None
    assert conn.state.connections == 1


def test_first_status_is_acked_with_flags_one() -> None:
    conn = make_conn()
    conn.feed(device_frame(PacketType.STATUS, status_payload(), 1))
    frames = sent_frames(conn)
    assert len(frames) == 1
    assert frames[0].type == PacketType.CONTROL
    assert frames[0].id == 1  # the originator's id, echoed
    assert ControlPacket.decode(frames[0].payload).flags == ControlFlags.ACK


def test_a_non_status_first_packet_is_a_violation() -> None:
    conn = make_conn()
    events = conn.feed(device_frame(PacketType.BUTTON, struct.pack("<2I", 1, 0), 1))
    assert len(events) == 1
    assert isinstance(events[0], ProtocolViolation)
    assert "first packet is not a status packet" in events[0].reason
    assert conn.closed


def test_require_first_status_can_be_relaxed() -> None:
    conn = make_conn(config=ConnectionConfig(require_first_status=False))
    events = conn.feed(device_frame(PacketType.BUTTON, struct.pack("<2I", 1, 0), 1))
    assert not conn.closed
    assert not any(isinstance(e, ProtocolViolation) for e in events)


def test_sending_before_the_uuid_is_known_is_refused() -> None:
    conn = make_conn()
    with pytest.raises(RuntimeError, match="UUID is unknown"):
        conn.request_status()


def test_a_uuid_change_mid_connection_is_a_violation() -> None:
    conn = make_conn()
    conn.feed(device_frame(PacketType.STATUS, status_payload(), 1))
    events = conn.feed(device_frame(PacketType.STATUS, status_payload(), 2, UUID_B))
    assert isinstance(events[0], ProtocolViolation)
    assert "DeviceID changed" in events[0].reason
    assert conn.closed


# ------------------------------------------------------------ reconnection

def test_state_is_keyed_on_the_uuid_not_the_connection() -> None:
    """A reconnect is a full IP-stack re-init: new port, counter reset to 1."""
    store = DeviceStateStore()
    first = make_conn(store=store)
    first.feed(device_frame(PacketType.STATUS, status_payload(), 1307))
    first.feed(device_frame(PacketType.STATUS, status_payload(), 1308))
    first.state.imaging_state = "opaque-blob"
    first.state.pushed_checksum = 0xDB963A1A

    second = make_conn(store=store)
    second.feed(device_frame(PacketType.STATUS, status_payload(), 1))  # counter reset
    assert second.state is first.state
    assert second.state.imaging_state == "opaque-blob"
    assert second.state.connections == 2
    assert len(store) == 1


def test_in_sync_compares_the_pushed_checksum_to_the_reported_one() -> None:
    conn = make_conn()
    conn.feed(device_frame(PacketType.STATUS, status_payload(), 1))
    assert conn.state.pushed_checksum is None
    assert not conn.state.in_sync
    conn.state.pushed_checksum = 0xDB963A1A  # == status tag 9 above
    assert conn.state.in_sync
    conn.state.pushed_checksum = 1
    assert not conn.state.in_sync


def test_device_capabilities_come_from_the_device_not_the_model() -> None:
    conn = make_conn()
    conn.feed(device_frame(PacketType.STATUS, status_payload(), 1))
    state = conn.state
    assert state.display_type == 0xC2050128
    assert (state.panel.width, state.panel.height, state.panel.displays) == (
        1440, 640, 4
    )
    assert state.panel.driver == "eink-flip"
    assert state.features["WiFiOTA"] is True
    assert state.features["FileSystem"] is True
    assert state.hardware_name_id == 8
    # HardwareNameID 8 -> getRectangleSupport is false unconditionally.
    assert state.supports_rectangles is False
    # ...and the same device was measured accepting screen-space partials.
    # The two properties disagree on purpose; see OPEN-QUESTIONS.md A10.
    assert state.accepts_screen_rectangles is True


def test_supports_rectangles_defaults_false_before_we_know() -> None:
    store = DeviceStateStore()
    state = store.get(UUID_A)
    assert state.supports_rectangles is False
    assert state.accepts_screen_rectangles is False


# -------------------------------------------------------------- dispatch

def test_param_reply_is_routed_and_acked() -> None:
    conn = make_conn()
    conn.feed(device_frame(PacketType.STATUS, status_payload(), 1))
    conn.drain()
    reply = ParamPacket(items=[]).encode()
    events = conn.feed(device_frame(PacketType.PARAM, reply, 2))
    assert [type(e) for e in events] == [ParamsReceived]
    assert sent_frames(conn)[0].type == PacketType.CONTROL


def test_unknown_packet_type_is_nacked() -> None:
    conn = make_conn()
    conn.feed(device_frame(PacketType.STATUS, status_payload(), 1))
    conn.drain()
    events = conn.feed(device_frame(4, b"\0" * 8, 2))  # 4 = input, a hole
    assert [type(e) for e in events] == [UnknownPacketType]
    frames = sent_frames(conn)
    assert len(frames) == 1
    assert ControlPacket.decode(frames[0].payload).flags == 0  # NACK


def test_control_packets_are_not_acked_back() -> None:
    conn = make_conn()
    conn.feed(device_frame(PacketType.STATUS, status_payload(), 1))
    conn.drain()
    pid = conn.read_params([29])
    conn.drain()
    events = conn.feed(
        device_frame(PacketType.CONTROL, ControlPacket.ack().encode(), pid)
    )
    assert [type(e) for e in events] == [Acked]
    assert conn.drain() == b"", "acking an ack would loop forever"
    assert pid not in conn.pending_requests


def test_nack_surfaces_the_error_code_and_charging_bit() -> None:
    conn = make_conn()
    conn.feed(device_frame(PacketType.STATUS, status_payload(), 1))
    conn.drain()
    pid = conn.open_screen_cache(2)
    conn.drain()
    payload = ControlPacket.nack(0x008A0000).encode()
    events = conn.feed(device_frame(PacketType.CONTROL, payload, pid))
    assert isinstance(events[0], Nacked)
    assert events[0].error_code == 0x008A0000
    assert events[0].charging is False

    charging = ControlPacket(flags=ControlFlags.NACK_CHARGING).encode()
    events = conn.feed(device_frame(PacketType.CONTROL, charging, pid))
    assert isinstance(events[0], Nacked)
    assert events[0].charging is True


def test_truncated_frames_across_feeds() -> None:
    conn = make_conn()
    raw = device_frame(PacketType.STATUS, status_payload(), 1)
    assert conn.feed(raw[:30]) == []
    events = conn.feed(raw[30:])
    assert [type(e) for e in events] == [DeviceConnected, StatusReceived]


def test_a_bad_version_is_reported_not_raised() -> None:
    conn = make_conn()
    events = conn.feed(struct.pack("<5I", 99, 0, 0, 0, 0))
    assert isinstance(events[0], ProtocolViolation)
    assert conn.closed


# ---------------------------------------------------------------- queueing

def test_queued_requests_are_framed_for_the_server_direction() -> None:
    conn = make_conn()
    conn.feed(device_frame(PacketType.STATUS, status_payload(), 1))
    conn.drain()
    pid = conn.read_params([29, 18, 19])
    frames = sent_frames(conn)  # strict=True proves the server CRC rule
    assert len(frames) == 1
    assert frames[0].type == PacketType.PARAM
    assert frames[0].id == pid == 0x1000_0000
    assert frames[0].header.compression == Compression.LZ4
    assert [i.id for i in ParamPacket.decode(frames[0].payload)] == [29, 18, 19]
    assert conn.pending_requests[pid].startswith("param read")


def test_write_to_a_network_read_only_param_is_refused() -> None:
    conn = make_conn()
    conn.feed(device_frame(PacketType.STATUS, status_payload(), 1))
    conn.drain()
    with pytest.raises(ReadOnlyParameter) as excinfo:
        conn.write_params({18: "10.0.0.5"})
    assert "server_tcp_set" in str(excinfo.value)
    assert conn.drain() == b"", "nothing should have been queued"


def test_commands_carry_the_right_type() -> None:
    conn = make_conn()
    conn.feed(device_frame(PacketType.STATUS, status_payload(), 1))
    conn.drain()
    conn.request_status()
    conn.refresh()
    conn.clear_screen()
    frames = sent_frames(conn)
    from pyvisionect.packets import CommandPacket

    types = [CommandPacket.decode(f.payload).type for f in frames]
    assert types == [
        CommandType.STATUS_REQUEST,
        CommandType.REFRESH,
        CommandType.CLEAR_SCREEN,
    ]


def test_no_ack_sentinel_id_is_a_no_op() -> None:
    conn = make_conn()
    conn.feed(device_frame(PacketType.STATUS, status_payload(), 1))
    conn.drain()
    conn.write_control(0xFFFFFFFF, 1)
    assert conn.drain() == b""


class _Rect:
    def __init__(self, screen_id: int) -> None:
        self.x = self.y = 0
        self.w, self.h = 2880, 640
        self.screen_id = screen_id
        self.options = 0
        self.encoding = 4
        self.data = b"\x00" * 921600


class _Frame:
    def __init__(self) -> None:
        self.rectangles = [_Rect(0), _Rect(1)]
        self.state_checksum = 3741864387
        self.state = "opaque"


def test_send_image_duck_types_the_imaging_module() -> None:
    conn = make_conn()
    conn.feed(device_frame(PacketType.STATUS, status_payload(), 1))
    conn.drain()
    conn.send_image(_Frame())
    frames = sent_frames(conn)
    assert len(frames) == 1
    assert frames[0].type == PacketType.IMAGE
    from pyvisionect.packets import ImagePacket

    packet = ImagePacket.decode(frames[0].payload)
    assert packet.checksum == 3741864387
    assert [r.screen_id for r in packet.rectangles] == [0, 1]
    assert all(r.update_options == 0x0102 for r in packet.rectangles)
    assert conn.state.pushed_checksum == 3741864387
    assert conn.state.imaging_state == "opaque"


# ------------------------------------------------------------------ timers

def test_read_timeout_fires_after_packet_timeout() -> None:
    conn = make_conn(config=ConnectionConfig(watchdog_enabled=False))
    conn.feed_at(device_frame(PacketType.STATUS, status_payload(), 1), now=100.0)
    conn.drain()
    early = conn.advance(100.0 + DEFAULT_PACKET_TIMEOUT - 1)
    assert not any(isinstance(e, ReadTimeout) for e in early)
    events = conn.advance(100.0 + DEFAULT_PACKET_TIMEOUT)
    assert any(isinstance(e, ReadTimeout) for e in events)
    assert events[0].quiet_for == pytest.approx(DEFAULT_PACKET_TIMEOUT)


def test_watchdog_fires_and_queues_a_status_request() -> None:
    config = ConnectionConfig(device_state_polling_minutes=15,
                              packet_timeout=100_000.0)
    conn = make_conn(config=config)
    conn.feed_at(device_frame(PacketType.STATUS, status_payload(), 1), now=0.0)
    conn.drain()
    assert conn.watchdog_timeout() == 900.0  # 15 min + 0 jitter
    assert not any(isinstance(e, WatchdogFired) for e in conn.advance(899.0))
    events = conn.advance(900.0)
    assert any(isinstance(e, WatchdogFired) for e in events)
    from pyvisionect.packets import CommandPacket

    frames = sent_frames(conn)
    assert CommandPacket.decode(frames[0].payload).type == CommandType.STATUS_REQUEST


def test_watchdog_falls_back_to_900_seconds_when_polling_is_zero() -> None:
    conn = make_conn(config=ConnectionConfig(device_state_polling_minutes=0))
    conn.feed_at(device_frame(PacketType.STATUS, status_payload(), 1), now=0.0)
    assert conn.watchdog_timeout() == WATCHDOG_FALLBACK == 900.0


def test_watchdog_jitter_is_bounded() -> None:
    conn = DeviceConnection(config=ConnectionConfig(device_state_polling_minutes=1))
    conn.feed_at(device_frame(PacketType.STATUS, status_payload(), 1), now=0.0)
    for _ in range(50):
        timeout = conn.watchdog_timeout()
        assert 60.0 <= timeout <= 70.0


def test_watchdog_is_not_armed_for_old_firmware() -> None:
    """pv3.go:1049 -- revision <= 269 gets no watchdog."""
    tags = [(t, v) for t, v in STATUS_TAGS if t != 18] + [(18, 269)]
    payload = StatusPacket(records=sorted(tags), sentinel=1).encode()
    conn = make_conn(config=ConnectionConfig(device_state_polling_minutes=1))
    conn.feed_at(device_frame(PacketType.STATUS, payload, 1), now=0.0)
    conn.drain()
    assert not any(isinstance(e, WatchdogFired) for e in conn.advance(10_000.0))


def test_watchdog_resets_on_every_status() -> None:
    conn = make_conn(config=ConnectionConfig(device_state_polling_minutes=1))
    conn.feed_at(device_frame(PacketType.STATUS, status_payload(), 1), now=0.0)
    conn.drain()
    conn.feed_at(device_frame(PacketType.STATUS, status_payload(), 2), now=50.0)
    conn.drain()
    assert not any(isinstance(e, WatchdogFired) for e in conn.advance(59.0))
    assert any(isinstance(e, WatchdogFired) for e in conn.advance(111.0))


def test_heartbeat_overdue_uses_next_status_in_minutes() -> None:
    conn = make_conn(config=ConnectionConfig(watchdog_enabled=False))
    conn.feed_at(device_frame(PacketType.STATUS, status_payload(), 1), now=0.0)
    conn.drain()
    # NextStatus == 1 minute, plus a minute of slack.
    assert not any(isinstance(e, HeartbeatOverdue) for e in conn.advance(119.0))
    assert any(isinstance(e, HeartbeatOverdue) for e in conn.advance(121.0))


def test_next_deadline_is_the_earliest_armed_timer() -> None:
    conn = make_conn(config=ConnectionConfig(device_state_polling_minutes=1))
    conn.feed_at(device_frame(PacketType.STATUS, status_payload(), 1), now=0.0)
    # watchdog 60 s < heartbeat 120 s < read timeout 300 s
    assert conn.next_deadline() == pytest.approx(60.0)


def test_advance_never_reads_a_clock_itself() -> None:
    """A connection that is never fed still times out, from the caller's clock."""
    conn = make_conn()
    assert conn.advance(0.0) == []
    events = conn.advance(DEFAULT_PACKET_TIMEOUT)
    assert any(isinstance(e, ReadTimeout) for e in events)
