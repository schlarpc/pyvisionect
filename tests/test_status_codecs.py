"""Status field codecs, checked against the real decoded heartbeat."""

from __future__ import annotations

import random
import struct

from pyvisionect.devices.enums import PV2_FEATURE_BITS
from pyvisionect.packets.status import (
    ABSENT,
    SENTINEL_TAG,
    STATUS_TAG_NAMES,
    STATUS_TAGS,
    StatusCodec,
    StatusPacket,
    _decode_string,
    _encode_string,
    decode_status_fields,
)

# The real captured heartbeat, tag by tag (section 10.2 of the protocol report).
CAPTURED = {
    0: 3, 1: 0, 2: 0xFFFFFFFF, 3: 0x652BE4AD, 4: 6, 5: 0xFFFFFFFF, 6: 6, 7: 2,
    8: 0xC2050128, 9: 0xDB963A1A, 10: 100, 11: 1, 12: 22, 13: 44, 15: 35793,
    16: 7, 17: 4, 18: 4407, 19: 7, 20: 4, 21: 4407, 22: 8, 23: 1, 24: 1, 25: 0,
    26: 0, 27: 1, 29: 0, 30: 0, 31: 0, 32: 0, 34: 4212, 35: 44, 36: 0,
    38: 2880, 39: 640, 40: 3, 43: 585, 45: 22, 46: 0, 47: 0xFFFFFFFF,
    50: 0x30333833, 51: 0x34353630, 52: 0x39373136, 53: 0xFFFF0032,
    54: 1, 56: 2, 60: 8388608, 61: 4575232, 70: 0, 71: 0, 87: 143175, 88: 0,
    89: 100, 90: 0xFFFFFFFF, 91: 0x005E0000, 92: 0x00000053, 99: 688,
    100: 6, 101: 0xFFFFFFFF, 102: 0xC5550ACC,
}


def test_the_captured_record_count() -> None:
    """496 bytes / 8 = 62 records = 61 fields + the sentinel."""
    assert len(CAPTURED) == 61
    packet = StatusPacket(records=list(CAPTURED.items()), sentinel=0x779050DF)
    assert len(packet.encode()) == 496
    assert len(packet.records) + 1 == 62


def test_gtin_reassembles_exactly_as_the_rest_api_reports_it() -> None:
    fields = decode_status_fields(CAPTURED)
    assert fields["GTIN"] == "3830065461792"


def test_bssid_reassembles_exactly_as_the_rest_api_reports_it() -> None:
    fields = decode_status_fields(CAPTURED)
    assert fields["BSSID"] == "00:00:5E:00:53:00"


def test_rssi_is_negated() -> None:
    """The device sends the dBm magnitude with the sign dropped."""
    assert decode_status_fields(CAPTURED)["SignalStrength"] == -44


def test_uptime_is_minutes() -> None:
    fields = decode_status_fields(CAPTURED)
    assert fields["DeviceUptime"] == 35793  # ~24.9 days, not ~10 hours
    tag = next(t for t in STATUS_TAGS if t.name == "DeviceUptime")
    assert tag.unit == "min"
    assert tag.codec is StatusCodec.MINUTES


def test_next_status_is_minutes() -> None:
    tag = next(t for t in STATUS_TAGS if t.name == "NextStatus")
    assert tag.unit == "min"
    assert decode_status_fields(CAPTURED)["NextStatus"] == 1


def test_temperature_is_a_signed_int8_in_the_low_byte() -> None:
    assert decode_status_fields(CAPTURED)["AverageTemperature"] == 22
    assert decode_status_fields({12: 0xFFFFFFFF})["AverageTemperature"] is None
    assert decode_status_fields({12: 0x000000FF})["AverageTemperature"] is None
    assert decode_status_fields({12: 0x000000FE})["AverageTemperature"] == -2
    assert decode_status_fields({12: 0x12345680})["AverageTemperature"] == -128


def test_hardware_id_packing() -> None:
    assert decode_status_fields(CAPTURED)["HardwareID"] is None  # 0xffffffff
    got = decode_status_fields({2: (3 << 24) | (1 << 16) | 0})["HardwareID"]
    assert got == {"id": 3, "family": 1, "family_name": "V-Tablet", "pcb": 0}


def test_versions_span_three_consecutive_tags() -> None:
    fields = decode_status_fields(CAPTURED)
    assert fields["Firmware"] == {"major": 7, "minor": 4, "revision": 4407}
    assert fields["Bootloader"] == {"major": 7, "minor": 4, "revision": 4407}
    assert fields["Hardware"] == {"major": 1, "minor": 1, "revision": 0}
    assert fields["ConnectivityVersion"]["major"] == 6


def test_pv2_features_is_ten_bits_and_keeps_bit_nine() -> None:
    """585 = bits 0, 3, 6, 9. The vendor's legacy parser drops bit 9; we do not."""
    assert len(PV2_FEATURE_BITS) == 10
    features = decode_status_fields(CAPTURED)["Features"]
    assert features["_mask"] == 585
    assert features["RemoteBootloaderUpgrade"]
    assert features["FileSystem"]
    assert features["FastBoot"]
    assert features["WiFiOTA"], "bit 9 must survive -- this is the vendor's bug"
    assert not features["TouchDisabled"]
    assert not features["DoubleBuffer"]
    assert sum(1 for k, v in features.items() if not k.startswith("_") and v) == 4


def test_scpu_sensor_status_is_eight_bits() -> None:
    got = decode_status_fields({44: 0b0000_0101})["SCPUSensorStatus"]
    assert got["SCPUTemperature"] and got["Humidity"]
    assert not got["ExternalTemperature"]


def test_absent_sentinel_on_exactly_the_expected_fields() -> None:
    fields = decode_status_fields(CAPTURED)
    for name in ("HardwareID", "MobileModuleID", "MobileLinkType", "ProximityThreshold"):
        assert fields[name] is None, name


def test_error_code_is_a_reason_not_a_fault() -> None:
    assert decode_status_fields({1: 0})["ErrorCode"]["name"] == "no error"
    assert decode_status_fields({1: 13})["ErrorCode"]["name"] == "deep sleep request"
    assert decode_status_fields({1: 7})["ErrorCode"]["name"] == "heartbeat"


def test_connect_reason_values() -> None:
    assert decode_status_fields({0: 3})["ConnectReason"]["name"] == "heartbeat"
    assert decode_status_fields({0: 5})["ConnectReason"]["name"] == "connection error"
    assert decode_status_fields({0: 99})["ConnectReason"]["name"] == "n/a"


def test_display_type_stays_an_opaque_uint32() -> None:
    """0xC2050128 is not in the vendor enum but is a valid firmware-service key."""
    packet = StatusPacket(records=list(CAPTURED.items()), sentinel=1)
    assert packet.display_type == 0xC2050128
    assert decode_status_fields(CAPTURED)["DisplayType"] == 0xC2050128


def test_image_push_allowed_is_a_bool() -> None:
    assert decode_status_fields(CAPTURED)["ImagePushAllowed"] is True


def test_unknown_tags_are_preserved() -> None:
    fields = decode_status_fields({9999: 7, 10: 50})
    assert fields["Unknown"] == {9999: 7}
    assert fields["BatteryLevel"] == 50


def test_string_codec_round_trip() -> None:
    for value in ("", "a", "ab", "abc", "3830065461792", "0123456789abcde"):
        words = _encode_string(value, 4 if len(value) < 16 else 10)
        assert _decode_string(words) == value


def test_string_codec_handles_the_captured_ff_padding() -> None:
    assert _decode_string([0x30333833, 0x34353630, 0x39373136, 0xFFFF0032]) == (
        "3830065461792"
    )


def test_tag_names_cover_the_continuations() -> None:
    assert STATUS_TAG_NAMES[16] == "Firmware"
    assert STATUS_TAG_NAMES[17] == "Firmware+1"
    assert STATUS_TAG_NAMES[18] == "Firmware+2"
    assert STATUS_TAG_NAMES[53] == "GTIN+3"
    assert STATUS_TAG_NAMES[92] == "BSSID+1"
    assert STATUS_TAG_NAMES[SENTINEL_TAG] == "LastStatus"


def test_tag_table_has_no_overlapping_ranges() -> None:
    seen: dict[int, str] = {}
    for tag in STATUS_TAGS:
        for number in tag.tags:
            assert number not in seen, f"tag {number}: {tag.name} vs {seen[number]}"
            seen[number] = tag.name


def test_v1_aliases_recorded_where_the_two_tables_disagree() -> None:
    by_tag = {t.tag: t for t in STATUS_TAGS}
    assert by_tag[2].name == "HardwareID" and by_tag[2].v1_name == "BootloaderVersion"
    assert by_tag[3].name == "FirmwareID" and by_tag[3].v1_name == "ApplicationVersion"
    assert by_tag[8].name == "DisplayType" and by_tag[8].v1_name == "DisplayIds"
    assert by_tag[46].name == "FLButtonCount" and by_tag[46].v1_name == "ButtonCount"


def test_decode_is_total_over_random_tag_maps() -> None:
    rng = random.Random(44)
    for _ in range(300):
        raw = {rng.randrange(0, 120): rng.getrandbits(32)
               for _ in range(rng.randrange(0, 40))}
        decode_status_fields(raw)  # must not raise
