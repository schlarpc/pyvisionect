"""Per-type payload codecs: round-trip properties and the captured examples."""

from __future__ import annotations

import random
import struct

import pytest

from pyvisionect.devices.enums import (
    CommandType,
    ControlFlags,
    EncodingType,
    FileMode,
    FileOperation,
    PacketType,
    ParamControl,
)
from pyvisionect.packets import (
    RECT_UPDATE_OPTIONS_DEFAULT,
    ButtonPacket,
    CborPacket,
    CommandPacket,
    ControlPacket,
    FileOpen,
    FilePacket,
    GpsPacket,
    ImagePacket,
    ParamItem,
    ParamPacket,
    Rectangle,
    StatusPacket,
    TouchPacket,
    decode_payload,
    encode_value,
    screen_cache_name,
)
from pyvisionect.wire import PayloadError, ReadOnlyParameter


# ------------------------------------------------------------------- control

def test_control_ack_matches_the_captured_bytes() -> None:
    assert ControlPacket.ack().encode() == bytes.fromhex("0100000000000000")


def test_control_nack_with_error_code() -> None:
    """Captured device NACK: control=0, len=4, code=0x00008a00."""
    raw = bytes.fromhex("000000000400000000008a00")
    packet = ControlPacket.decode(raw)
    assert packet.is_nack
    assert not packet.nack_charging
    # The gateway logs the four payload bytes as "0x00008a00"; read as the
    # little-endian uint32 the rest of this protocol uses, that is 0x008a0000.
    assert packet.payload == bytes.fromhex("00008a00")
    assert packet.error_code == 0x008A0000
    assert packet.encode() == raw


def test_control_nack_charging_is_bit_25() -> None:
    packet = ControlPacket(flags=ControlFlags.NACK_CHARGING)
    assert packet.is_nack
    assert packet.nack_charging
    assert ControlFlags.NACK_CHARGING == 1 << 25


def test_control_ack_is_flags_and_one() -> None:
    assert ControlPacket(flags=0x0000_0001).is_ack
    assert ControlPacket(flags=0xFFFF_FFFF).is_ack
    assert not ControlPacket(flags=0x0000_0002).is_ack


def test_control_too_short() -> None:
    with pytest.raises(PayloadError):
        ControlPacket.decode(b"\0" * 7)


# --------------------------------------------------------------------- param

CAPTURED_PARAM_REQUEST = bytes.fromhex(
    "00000000"  # Reserved
    "18000000"  # PayloadLength = 24
    "1d000000"  # id 29 read, len 0
    "12000000"  # id 18
    "13000000"  # id 19
    "41000000"  # id 65
    "42000000"  # id 66
    "02000000"  # id 2
)

CAPTURED_PARAM_REPLY = (
    bytes.fromhex("000000004d000000")
    + bytes.fromhex("1d000004") + bytes.fromhex("01000000")
    + bytes.fromhex("1200001e") + b"visionect.internal.example.com"
    + bytes.fromhex("13000002") + bytes.fromhex("692b")
    + bytes.fromhex("41000009") + b"ExampleAP"
    + bytes.fromhex("42000004") + b"wpa2"
    + bytes.fromhex("02000004") + bytes.fromhex("06000000")
)


def test_param_read_request_matches_the_capture() -> None:
    assert ParamPacket.read([29, 18, 19, 65, 66, 2]).encode() == CAPTURED_PARAM_REQUEST


def test_param_reply_decodes_and_re_encodes_exactly() -> None:
    packet = ParamPacket.decode(CAPTURED_PARAM_REPLY)
    assert len(packet) == 6
    by_id = packet.by_id()
    assert by_id[29].as_int() == 1
    assert by_id[18].as_str() == "visionect.internal.example.com"
    assert by_id[19].as_int() == 11113
    assert by_id[65].as_str() == "ExampleAP"
    assert by_id[66].as_str() == "wpa2"
    assert by_id[2].as_int() == 6
    assert all(item.control == ParamControl.READ for item in packet)
    assert packet.encode() == CAPTURED_PARAM_REPLY


def test_param_header_is_eight_bytes_not_twelve() -> None:
    """32 - 24 == 8. The proto.Command Reserved word is not emitted here."""
    assert len(CAPTURED_PARAM_REQUEST) - 24 == 8


def test_param_item_names_come_from_the_tclv_table() -> None:
    assert ParamItem(29).name == "Heart beat interval"
    assert ParamItem(18).name == "Server IP"
    assert ParamItem(9999).name == "unknown(9999)"


def test_param_value_longer_than_255_rejected() -> None:
    with pytest.raises(ValueError, match="uint8"):
        ParamItem(108, ParamControl.WRITE, b"x" * 256)


def test_param_write_refuses_network_read_only_ids() -> None:
    with pytest.raises(ReadOnlyParameter) as excinfo:
        ParamPacket.write({18: "10.0.0.1"})
    message = str(excinfo.value)
    assert "server_tcp_set" in message
    assert "flash_save" in message
    assert excinfo.value.param_id == 18


def test_param_write_override_is_explicit() -> None:
    packet = ParamPacket.write({18: "10.0.0.1"}, allow_read_only=True)
    assert packet.items[0].control == ParamControl.WRITE


def test_param_write_allows_a_writable_id() -> None:
    packet = ParamPacket.write({29: 5})
    assert packet.items[0].id == 29
    assert packet.items[0].value == b"\x05\x00\x00\x00"


def test_an_int_write_uses_the_parameters_width_not_the_values() -> None:
    """The device answers a wrong-width write with its own error code.

    Measured on a live sign: a one-byte write to 29 (a uint32) came back
    ``control=3`` with value ``00 00 5a 00``, where a write to an id the
    firmware does not implement comes back ``00 00 58 00``. So width is a
    property of the parameter, and guessing it from the value -- which this
    used to do -- silently loses every write of a small number to a wide
    parameter.
    """
    assert ParamPacket.write({29: 1}).items[0].value == b"\x01\x00\x00\x00"
    # The port is a uint16 whose normal value needs two bytes anyway, so the
    # old heuristic happened to get this one right.
    port = ParamPacket.write({19: 11113}, allow_read_only=True)
    assert port.items[0].value == b"\x69\x2b"
    # Ethernet TCP retry count is genuinely one byte.
    assert ParamPacket.write({9: 8}).items[0].value == b"\x08"
    # flash_save is command-shaped, and one byte is what the device has been
    # accepting all along.
    assert ParamPacket.write({53: 1}).items[0].value == b"\x01"
    # An unmeasured id gets the uint32 default.
    assert ParamPacket.write({250: 1}).items[0].value == b"\x01\x00\x00\x00"


def test_an_explicit_width_still_wins() -> None:
    assert encode_value(1, width=1) == b"\x01"
    assert encode_value(1) == b"\x01\x00\x00\x00"
    assert encode_value(b"\x01") == b"\x01"
    assert encode_value("10.0.0.1") == b"10.0.0.1"


def test_param_round_trip_property() -> None:
    rng = random.Random(11)
    for _ in range(200):
        items = [
            ParamItem(
                rng.randrange(0, 2304),
                rng.randrange(0, 4),
                bytes(rng.randrange(256) for _ in range(rng.randrange(0, 40))),
            )
            for _ in range(rng.randrange(0, 8))
        ]
        packet = ParamPacket(items=items)
        assert ParamPacket.decode(packet.encode()).items == items


def test_param_truncated_value_reported() -> None:
    raw = struct.pack("<II", 0, 8) + struct.pack("<HBB", 29, 0, 40) + b"short"
    with pytest.raises(PayloadError, match="value bytes"):
        ParamPacket.decode(raw)


# -------------------------------------------------------------------- status

def test_status_round_trip_property() -> None:
    rng = random.Random(22)
    for _ in range(200):
        records = [
            (rng.randrange(0, 200), rng.getrandbits(32))
            for _ in range(rng.randrange(0, 70))
        ]
        packet = StatusPacket(records=records, sentinel=rng.getrandbits(32))
        assert StatusPacket.decode(packet.encode()).records == records


def test_status_stops_at_the_sentinel() -> None:
    raw = struct.pack("<II", 10, 100) + struct.pack("<II", 0xFFFFFFFF, 0xABCD)
    raw += struct.pack("<II", 11, 1)  # must be ignored
    packet = StatusPacket.decode(raw)
    assert packet.records == [(10, 100)]
    assert packet.sentinel == 0xABCD
    assert packet.trailing == struct.pack("<II", 11, 1)
    assert packet.encode() == raw


def test_status_rejects_a_non_multiple_of_eight() -> None:
    with pytest.raises(PayloadError, match="multiple of 8"):
        StatusPacket.decode(b"\0" * 9)


# ---------------------------------------------------------------------- file

CAPTURED_FILE_OPEN = (
    bytes.fromhex("00000000")       # Op = 0 (open)
    + bytes.fromhex("0e000000")     # Length = 14
    + bytes.fromhex("02000000")     # Mode = 2 (read)
    + b"@screen_2\x00"
)


def test_file_open_matches_the_capture() -> None:
    packet = FilePacket.decode(CAPTURED_FILE_OPEN)
    assert packet.op == FileOperation.OPEN
    assert packet.op_name == "open"
    parsed = packet.parsed()
    assert isinstance(parsed, FileOpen)
    assert parsed.filename == "@screen_2"
    assert parsed.mode == FileMode.READ
    assert packet.encode() == CAPTURED_FILE_OPEN
    assert FilePacket.open("@screen_2", FileMode.READ).encode() == CAPTURED_FILE_OPEN


def test_screen_cache_name() -> None:
    assert screen_cache_name(2) == "@screen_2"


def test_file_write_descriptor_round_trip() -> None:
    packet = FilePacket.write_descriptor(1234, 0xDEADBEEF)
    again = FilePacket.decode(packet.encode())
    parsed = again.parsed()
    assert parsed.length == 1234 and parsed.checksum == 0xDEADBEEF


def test_file_round_trip_property() -> None:
    rng = random.Random(33)
    for _ in range(100):
        packet = FilePacket(
            op=rng.randrange(0, 7),
            raw=bytes(rng.randrange(256) for _ in range(rng.randrange(0, 50))),
        )
        assert FilePacket.decode(packet.encode()) == packet


# --------------------------------------------------------------------- image

CAPTURED_IMAGE_HEADER = bytes.fromhex("c35108df020000000000000030201c0000000000")
CAPTURED_RECT0 = bytes.fromhex(
    "0100"      # ImageType = 1 (gray)
    "0000"      # ScreenID = 0
    "0000"      # X
    "0000"      # Y
    "400b"      # Width = 2880
    "8002"      # Height = 640
    "0201"      # RectangleUpdateOptions = 0x0102 (auto-filled for 4-bit)
    "0000"      # Options
    "0400"      # Encoding = 4
    "0000"      # Reserved
    "00100e00"  # PayloadLength = 921600
)


def test_captured_image_header_decodes() -> None:
    (checksum, count, options, length, reserved) = struct.unpack(
        "<5I", CAPTURED_IMAGE_HEADER
    )
    assert checksum == 3741864387  # == the device's reported DisplayStateCRC
    assert count == 2
    assert options == 0
    assert length == 1843248
    assert reserved == 0


def test_rectangle_update_options_autofill() -> None:
    """image.go:271-277 substitutes 0x0101 / 0x0102 when the field is 0."""
    assert RECT_UPDATE_OPTIONS_DEFAULT[EncodingType.ONE_BIT] == 0x0101
    assert RECT_UPDATE_OPTIONS_DEFAULT[EncodingType.FOUR_BIT] == 0x0102
    four = Rectangle(0, 0, 8, 2, b"\0" * 8, encoding=EncodingType.FOUR_BIT)
    one = Rectangle(0, 0, 16, 2, b"\0" * 4, encoding=EncodingType.ONE_BIT)
    assert four.effective_update_options == 0x0102
    assert one.effective_update_options == 0x0101
    assert struct.unpack_from("<H", four.encode(), 12)[0] == 0x0102
    assert struct.unpack_from("<H", one.encode(), 12)[0] == 0x0101


def test_explicit_update_options_survive() -> None:
    rect = Rectangle(0, 0, 4, 4, b"\0" * 8, update_options=0xBEEF)
    assert struct.unpack_from("<H", rect.encode(), 12)[0] == 0xBEEF


def test_image_packet_round_trip_with_the_captured_geometry() -> None:
    rects = [
        Rectangle(0, 0, 2880, 640, b"\0" * 921600, screen_id=sid, encoding=4)
        for sid in (0, 1)
    ]
    packet = ImagePacket(checksum=3741864387, rectangles=rects)
    raw = packet.encode()
    assert raw[:20] == CAPTURED_IMAGE_HEADER
    assert raw[20:44] == CAPTURED_RECT0
    again = ImagePacket.decode(raw)
    assert len(again) == 2
    assert [r.screen_id for r in again.rectangles] == [0, 1]
    assert again.encode() == raw


def test_rectangle_expected_data_length() -> None:
    assert Rectangle(0, 0, 2880, 640, encoding=4).expected_data_length == 921600
    assert Rectangle(0, 0, 2880, 640, encoding=1).expected_data_length == 230400


def test_image_payload_length_mismatch_reported() -> None:
    rect = Rectangle(0, 0, 2, 2, b"\0" * 2, encoding=4)
    raw = bytearray(ImagePacket(rectangles=[rect]).encode())
    struct.pack_into("<I", raw, 12, 999)
    with pytest.raises(PayloadError, match="PayloadLength"):
        ImagePacket.decode(bytes(raw))


# ---------------------------------------------------------------------- misc

def test_command_negative_types_survive() -> None:
    for command_type in (-1, -2, -3, -4, 0, 11, 22):
        packet = CommandPacket(type=command_type, payload=b"ab")
        again = CommandPacket.decode(packet.encode())
        assert again.type == command_type
        assert again.payload == b"ab"
    assert CommandPacket(type=-1).type_name == "refresh"
    assert CommandPacket(type=CommandType.STATUS_REQUEST).type_name == "status request"


def test_touch_button_gps_cbor_round_trip() -> None:
    touch = TouchPacket(1, 2, 3, 4, 5)
    assert TouchPacket.decode(touch.encode()) == touch
    button = ButtonPacket(1, 0)
    assert ButtonPacket.decode(button.encode()) == button
    gps = GpsPacket(power_state=1, raw=b"45.0,13.0\x00")
    assert GpsPacket.decode(gps.encode()) == gps
    assert gps.coordinates == "45.0,13.0"
    cbor = CborPacket(user_data=7, data=b"\xa1\x01\x02")
    assert CborPacket.decode(cbor.encode()) == cbor


def test_dispatch_table_matches_the_gateway() -> None:
    from pyvisionect.packets import DECODERS

    assert set(DECODERS) == {1, 2, 3, 5, 6, 7, 8, 10, 11, 12}
    # 4 (input) and 9 are holes in both of the vendor's own v2 tables.
    assert 4 not in DECODERS
    assert 9 not in DECODERS
    assert PacketType.BOOTLOADER not in DECODERS


def test_decode_payload_returns_none_for_an_unknown_type() -> None:
    assert decode_payload(4, b"\0" * 8) is None
