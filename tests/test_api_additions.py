"""The remaining API changes the Home Assistant design asked for."""

from __future__ import annotations

import pytest

from pyvisionect.devices import panel_for
from pyvisionect.devices.enums import PacketType
from pyvisionect.packets import (
    FILE_LIST_LENGTH,
    FILE_LIST_PATH,
    FileOpen,
    FilePacket,
    ImagePacket,
    StatusPacket,
    parse_file_listing,
)
from pyvisionect.session import ConnectionConfig, DeviceState, DeviceStateStore
from pyvisionect.wire import (
    STORED_ONLY,
    BlockHeader,
    Compression,
    Direction,
    FrameDecoder,
    have_lz4,
    iter_block_headers,
    stored_only,
)
from pyvisionect.wire.errors import CommandPacketsDisabled

from test_pending import FakeFrame  # noqa: E402
from test_session import (  # noqa: E402
    UUID_A,
    device_frame,
    make_conn,
    sent_frames,
    status_payload,
)


def _connected(**kwargs):
    conn = make_conn(**kwargs)
    conn.feed(device_frame(PacketType.STATUS, status_payload(), 1))
    conn.drain()
    return conn


# ------------------------------------------------------- tri-state in_sync

def test_in_sync_is_tri_state() -> None:
    state = DeviceState(device_id=UUID_A)
    assert state.in_sync is None, "never pushed is not the same as out of sync"
    assert state.has_pushed is False

    state.apply_status(StatusPacket(records=[(9, 0xDB963A1A)], sentinel=1))
    assert state.in_sync is None, "still never pushed"

    state.pushed_checksum = 0xDB963A1A
    assert state.has_pushed is True
    assert state.in_sync is True

    state.pushed_checksum = 1
    assert state.in_sync is False


def test_in_sync_is_unknown_when_the_device_reports_no_state() -> None:
    state = DeviceState(device_id=UUID_A)
    state.pushed_checksum = 5
    state.apply_status(StatusPacket(records=[(10, 100)], sentinel=1))
    assert state.in_sync is None


# ----------------------------------------------- packet type 2 is unverified

def test_command_packets_can_be_refused_wholesale() -> None:
    conn = _connected(config=ConnectionConfig(allow_command_packets=False))
    for call in (conn.reboot, conn.refresh, conn.clear_screen, conn.request_status):
        with pytest.raises(CommandPacketsDisabled, match="never been observed"):
            call()
    with pytest.raises(CommandPacketsDisabled):
        conn.sleep(5)
    assert conn.drain() == b"", "not one type-2 byte may escape"


def test_command_packets_are_allowed_by_default() -> None:
    conn = _connected()
    conn.reboot()
    assert len(sent_frames(conn)) == 1


def test_the_type_2_warning_is_in_the_module_docstring() -> None:
    """A consumer routing around type 2 needs the caveat visible in the API."""
    import pyvisionect.packets.misc as misc

    doc = " ".join((misc.__doc__ or "").split())
    assert "has never been observed on the wire" in doc
    assert "param packet's on-wire header is **8**" in doc, (
        "cite the 12-vs-8 param-header precedent explicitly"
    )
    assert "no emission site anywhere in the vendor server" in doc


def test_refresh_and_clear_screen_say_they_have_no_emission_site() -> None:
    from pyvisionect.session import DeviceConnection

    for method in (DeviceConnection.refresh, DeviceConnection.clear_screen):
        doc = " ".join((method.__doc__ or "").split()).lower()
        assert "no emission site" in doc
        assert "vendor server" in doc


# ------------------------------------------------------------ sleep minutes

@pytest.mark.parametrize("bad", [0, -1, -60])
def test_sleep_refuses_a_non_positive_duration(bad: int) -> None:
    conn = _connected()
    with pytest.raises(ValueError, match="errSleepTime"):
        conn.sleep(bad)
    assert conn.drain() == b""


def test_sleep_docstring_cites_the_emission_site() -> None:
    from pyvisionect.session import DeviceConnection

    doc = DeviceConnection.sleep.__doc__ or ""
    assert "stdcmd.Sleep.Done" in doc
    assert "[GAP]" not in doc, "this gap is resolved"


# -------------------------------------------------- force-full-screen levers

def test_force_full_perturbs_the_checksum() -> None:
    conn = _connected()
    conn.send_image(FakeFrame(0x1234ABCD), force_full=True)
    packet = ImagePacket.decode(sent_frames(conn)[0].payload)
    assert packet.checksum == 0x1234ABCD ^ 0x80000000
    assert conn.state.pushed_checksum == packet.checksum


def test_force_full_never_emits_zero() -> None:
    """0 is reserved for "unknown"; the vendor remaps a computed 0 to 1."""
    conn = _connected()
    conn.send_image(FakeFrame(0x80000000), force_full=True)
    assert ImagePacket.decode(sent_frames(conn)[0].payload).checksum != 0


def test_checksum_override_sends_an_exact_value() -> None:
    conn = _connected()
    conn.send_image(FakeFrame(1), checksum_override=3741864387)
    assert ImagePacket.decode(sent_frames(conn)[0].payload).checksum == 3741864387


def test_force_full_and_checksum_override_are_mutually_exclusive() -> None:
    conn = _connected()
    with pytest.raises(ValueError, match="not both"):
        conn.send_image(FakeFrame(), force_full=True, checksum_override=7)


def test_send_image_documents_both_levers_and_the_hardware_8_no_op() -> None:
    from pyvisionect.session import DeviceConnection

    doc = DeviceConnection.send_image.__doc__ or ""
    assert "force_full_screen=True" in doc
    assert "HardwareNameID == 8" in doc


# ------------------------------------------------- all-stored / no-lz4 mode

def test_stored_only_emits_every_block_verbatim() -> None:
    from pyvisionect.wire import encode_blocks

    plain = b"\x00" * (4800 * 2 + 17)  # highly compressible
    body = encode_blocks(plain, compressor=STORED_ONLY)
    headers = [h for _o, h in iter_block_headers(body)]
    assert len(headers) == 3
    assert all(h.stored == 1 for h in headers)
    assert all(h.payload_length == h.uncompressed_length for h in headers)
    assert {h.blocks_minus_one for h in headers} == {2}


def test_stored_only_frames_decode_without_touching_lz4() -> None:
    conn = _connected(
        config=ConnectionConfig(compressor=STORED_ONLY)
    )
    conn.send_image(FakeFrame())
    raw = conn.drain()
    frame = FrameDecoder(Direction.SERVER_TO_DEVICE, strict=True).feed(raw)[0]
    assert frame.header.compression == Compression.LZ4  # chain framing, all stored
    assert all(h.stored == 1 for _o, h in iter_block_headers(frame.raw_body))
    assert ImagePacket.decode(frame.payload).checksum == 0x11111111


def test_stored_only_keeps_the_chain_framing_not_compression_none() -> None:
    """Compression.NONE *to* a device was never observed; do not default to it.

    The lz4-free route is the compressor, not the compression field: keep the
    block-chain framing the device has been seen receiving and mark every block
    stored.
    """
    assert ConnectionConfig().compression == Compression.LZ4
    assert ConnectionConfig(compressor=STORED_ONLY).compression == Compression.LZ4
    # stored_only returns one byte MORE than its input, which is what makes
    # encode_blocks take the stored branch for every chunk.
    assert stored_only(b"ab") == b"ab\x00"
    assert len(stored_only(b"x" * 4800)) > 4800


def test_lz4_is_importable_here_but_not_required_for_stored_only() -> None:
    assert have_lz4() in (True, False)
    # The stored path must not call into lz4 at all.
    from pyvisionect.wire import encode_blocks

    encode_blocks(b"x" * 100, compressor=STORED_ONLY)


# ------------------------------------------------------------ file listing

def test_file_list_is_a_read_of_dot_not_a_list_opcode() -> None:
    assert FILE_LIST_PATH == "."
    assert FILE_LIST_LENGTH == 10240
    conn = _connected()
    packet_id = conn.file_list()
    frame = sent_frames(conn)[0]
    assert frame.type == PacketType.FILE
    assert frame.id == packet_id
    file_packet = FilePacket.decode(frame.payload)
    parsed = file_packet.parsed()
    assert isinstance(parsed, FileOpen)
    assert parsed.filename == "."
    assert parsed.mode == 2  # read


def test_read_file_range_is_a_short_read() -> None:
    conn = _connected()
    conn.read_file_range(256)
    frame = sent_frames(conn)[0]
    assert FilePacket.decode(frame.payload).raw == (256).to_bytes(4, "little")


def test_parse_file_listing() -> None:
    blob = b"image0.pv2 1843915 0\nimage1.pv2 1843915 0\n@syslog 4096 0\n"
    listing = parse_file_listing(blob)
    assert set(listing) == {"image0.pv2", "image1.pv2", "@syslog"}
    assert listing["image0.pv2"].size == 1843915
    assert listing["image0.pv2"].checksum == "0"


def test_parse_file_listing_is_forgiving_of_device_formatting() -> None:
    listing = parse_file_listing("good 10 0\n\nbroken\nalso bad\n")
    assert set(listing) == {"good"}


def test_file_module_docstring_no_longer_claims_screen_n_drives_updates() -> None:
    import pyvisionect.packets.file as file_module

    doc = file_module.__doc__ or ""
    assert "handleLiveView" in doc
    assert "That was wrong" in doc
    assert "no ``list`` opcode" in doc


def test_open_screen_cache_docstring_is_corrected() -> None:
    from pyvisionect.session import DeviceConnection

    doc = DeviceConnection.open_screen_cache.__doc__ or ""
    assert "live-view" in doc
    assert "update strategy" in doc


# ---------------------------------------------------------------- diagnostics

def test_panel_reports_whether_it_is_a_guess() -> None:
    ours = panel_for(0xC2050128)
    assert ours.is_default is True
    assert ours.name == "DISPLAY_UNKNOWN"
    assert ours.display_type == 0xC2050128
    assert "unrecognised panel" in ours.describe()
    assert "1440x2560" in ours.describe()

    known = panel_for(13)
    assert known.is_default is False
    assert known.name == "DISPLAY_EINK_31_2_D039"
    assert "unrecognised" not in known.describe()
    # Same geometry either way -- the flag is about honesty, not behaviour.
    assert (known.width, known.height, known.displays) == (
        ours.width, ours.height, ours.displays
    )


def test_connect_reason_name() -> None:
    assert StatusPacket(records=[(0, 1)]).connect_reason_name == "reboot"
    assert StatusPacket(records=[(0, 2)]).connect_reason_name == "wakeup"
    assert StatusPacket(records=[(0, 3)]).connect_reason_name == "heartbeat"
    assert StatusPacket(records=[(0, 8)]).connect_reason_name == "by server request"
    assert StatusPacket(records=[(0, 200)]).connect_reason_name == "n/a"
    assert StatusPacket(records=[]).connect_reason_name == "n/a"


def test_connection_for_none_is_documented_as_the_common_case() -> None:
    from pyvisionect.io import VisionectServer

    doc = VisionectServer.connection_for.__doc__ or ""
    assert "common case" in doc
    assert "store.queue" in doc


def test_encode_frame_is_documented_as_cpu_bound() -> None:
    pytest.importorskip("numpy")
    from pyvisionect.imaging import encode_frame
    import pyvisionect.imaging as imaging

    assert "CPU-bound" in (encode_frame.__doc__ or "")
    assert "event loop" in (encode_frame.__doc__ or "")
    assert "executor" in (imaging.__doc__ or "")
