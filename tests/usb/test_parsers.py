"""Every parser, against the real sign's real replies.

The fixtures in :mod:`pyvisionect.io.usb.fake` are captures, scrubbed of
identifiers and otherwise untouched.  So these tests assert what the device
actually said, which is a different and stronger claim than asserting what the
parser was written to expect.

Several tests here are **cross-checks**: two commands that report the same
underlying value, compared.  Those are the ones that would catch a parser that
is self-consistently wrong.
"""

from __future__ import annotations

import uuid as _uuid

import pytest

from pyvisionect.io.usb import SerialConsole, Sign
from pyvisionect.io.usb import parsers as P
from pyvisionect.io.usb.fake import FakeTransport


@pytest.fixture
def sign() -> Sign:
    return Sign(
        SerialConsole(
            transport=FakeTransport(),
            command_timeout=1.0,
            idle_timeout=0.02,
            read_timeout=0.0,
        )
    )


# ---------------------------------------------------------------- identity


def test_uuid_is_parsed_from_space_separated_0x_bytes(sign: Sign) -> None:
    """Not a dashed UUID: the device prints 16 ``0x`` literals after a blank line."""
    assert sign.uuid() == _uuid.UUID("00112233-4455-6677-8899-aabb00000000")


def test_uuid_trailing_zero_bytes_are_real_not_truncation(sign: Sign) -> None:
    """A 12-byte factory id zero-padded to 16. The zeros are the format."""
    assert sign.uuid().bytes[-4:] == b"\x00\x00\x00\x00"
    assert len(sign.uuid().bytes) == 16


def test_uuid_rejects_a_short_reply() -> None:
    with pytest.raises(ValueError, match="16 bytes"):
        P.parse_uuid(["UUID: 0x00 0x11 0x22"])


def test_gtin_stays_a_string(sign: Sign) -> None:
    """A GTIN's leading zeros are significant, so int() would be wrong."""
    assert sign.gtin() == "3830065461792"
    assert isinstance(sign.gtin(), str)


def test_uptime_is_minutes(sign: Sign) -> None:
    assert sign.uptime() == 5


def test_cli_version(sign: Sign) -> None:
    assert sign.cli_version() == "1.2"


def test_firmware_version_full_decode(sign: Sign) -> None:
    fw = sign.firmware_version()
    assert fw.firmware == "7.4.4407"
    assert fw.version_tuple == (7, 4, 4407)
    assert fw.firmware_build_date == (10, 9, 2025)
    assert fw.crc == 0x652BE4AD
    assert fw.image_hash == 0x8281F596
    assert fw.image_length == 408876
    assert fw.bootloader == "7.4.4407"
    assert fw.build == "7.4.4407"
    assert fw.hardware == "PP32 v1.1"
    assert fw.bom == 0
    assert fw.app == "Joan"


def test_firmware_panel_line_gives_the_real_panel_geometry(sign: Sign) -> None:
    """``EPD: 31.2",2x2880x640,...`` -- **two** 2880x640 panels.

    The reverse engineering notes said four 1440x640. The firmware's own display
    table says two of 2880x640, and ``status_get``'s
    ``NUMBER_OF_SUPPORTED_DISPLAYS`` agrees with the firmware.
    """
    fw = sign.firmware_version()
    assert fw.panel_count == 2
    assert fw.panel_size == (2880, 640)


def test_features(sign: Sign) -> None:
    features = sign.features()
    assert features.touch is True
    assert features.eap is True
    assert features["touch"] is True


# -------------------------------------------------------------- encryption


def test_encryption_is_disabled_with_an_empty_key(sign: Sign) -> None:
    config = sign.encryption_config()
    assert config.enabled is False
    assert config.key == "", "the quotes are stripped, leaving a genuinely empty key"
    assert config.configured is False


def test_encryption_key_quotes_are_stripped_not_kept() -> None:
    config = P.parse_encryption_config(["Outbound enc: Enabled", "Key: 'F@%gtb7;xLmXV$9a'"])
    assert config.enabled is True
    assert config.key == "F@%gtb7;xLmXV$9a"
    assert config.configured is True


# ----------------------------------------------------------------- display


def test_display_config_has_eight_vcoms_not_sixteen(sign: Sign) -> None:
    """``display_conf_set`` takes 16 arguments; the getter prints 8. The getter is right.

    TCLV defines ``VCOM_0..7`` (ids 20-27), and this hardware has four rails
    wired and four not.
    """
    config = sign.display_config()
    assert len(config.vcom_mv) == 8
    assert config.vcom_mv == (2400, 2400, 2400, 2400, 0, 0, 0, 0)
    assert config.active_rails == 4


def test_display_type_matches_the_status_dumps_hex(sign: Sign) -> None:
    """``display_conf_get`` prints it unsigned decimal, ``status_get`` prints hex."""
    config = sign.display_config()
    assert config.display_type == 3255107880
    assert config.display_type_hex == "0xC2050128"
    assert sign.status()["DISPLAYS_ID"] == 0xC2050128
    assert config.display_type == sign.status()["DISPLAYS_ID"]


def test_border_is_unsupported_on_this_panel(sign: Sign) -> None:
    border = sign.border()
    assert border.supported is False
    assert border.mode == "Server"


def test_image_rotate_is_off(sign: Sign) -> None:
    rotate = sign.image_rotate_config()
    assert rotate.enabled is False
    assert rotate.mode == "off"
    assert rotate.timeout_s == 75
    assert rotate.image_count == 0


def test_image_count_is_config_not_a_directory_listing(sign: Sign) -> None:
    """Zero images in the carousel while ``fs_ls`` shows six files. Both correct."""
    assert sign.image_rotate_config().image_count == 0
    assert len(sign.files()) == 6


# ------------------------------------------------------------ connectivity


def test_conn_type_get(sign: Sign) -> None:
    conn = sign.connectivity()
    assert conn.name == "CC3100"
    assert conn.value == 6
    assert conn.active is True


def test_conn_type_list_is_three_values_not_the_documented_eight(sign: Sign) -> None:
    """The vendor's table lists 0-7 and its 4G example passes 8. This firmware has 3."""
    options = sign.connectivity_options()
    assert [(o.name, o.value) for o in options] == [
        ("W5500", 3),
        ("CC3100", 6),
        ("None", 0),
    ]
    assert 8 not in {o.value for o in options}


def test_the_active_conn_type_is_one_the_list_offers(sign: Sign) -> None:
    assert sign.connectivity().value in {o.value for o in sign.connectivity_options()}


def test_conn_state(sign: Sign) -> None:
    state = sign.connectivity_state()
    assert state.state == "tcp open"
    assert state.connected is True


def test_conn_firmware_is_uninitialised_and_says_so(sign: Sign) -> None:
    """``6.4294967295.3310684876`` -- an 0xFFFFFFFF and a junk word."""
    conn = sign.connectivity_firmware()
    assert conn.parts_unsigned == (6, 4294967295, 3310684876)
    assert conn.initialised is False


def test_conn_firmware_signed_view_matches_the_status_dump(sign: Sign) -> None:
    """The same bytes, rendered unsigned by one command and signed by the other."""
    conn = sign.connectivity_firmware()
    status = sign.status()
    assert conn.parts_signed == (
        status["CONN_VER_MAJOR"],
        status["CONN_VER_MINOR"],
        status["CONN_VER_REVISION"],
    )
    assert conn.parts_signed[1] == -1
    assert conn.parts_unsigned[1] == 0xFFFFFFFF


def test_conn_retry(sign: Sign) -> None:
    assert sign.conn_retry() == 1


# -------------------------------------------------------------------- WiFi


def test_wifi_config_does_not_include_the_psk(sign: Sign) -> None:
    """There is no read path for TCLV 67, over USB or over the network."""
    config = sign.wifi_config()
    assert config.ssid == "ExampleAP"
    assert config.security == "wpa2"
    assert config.band == 0
    assert not hasattr(config, "psk")


def test_wifi_security_is_a_string_not_an_integer(sign: Sign) -> None:
    assert sign.wifi_config().security == "wpa2"


def test_wifi_mac_and_the_radios_own_mac_agree(sign: Sign) -> None:
    """``wifi_mac_conf_get`` reads stored TCLV 110; ``cc3100_mac_address`` asks the chip."""
    assert sign.wifi_mac().mac == sign.wifi_mac_address() == "00:00:5e:00:53:00"


def test_mac_case_is_normalised_because_the_firmware_is_inconsistent(sign: Sign) -> None:
    """``wifi_mac_conf_get`` lowercases, ``eth_conf_get`` uppercases. Same kind of value."""
    assert sign.wifi_mac().mac.islower()
    assert sign.ethernet_config().mac.islower(), "eth_conf_get prints uppercase"


def test_parse_mac_rejects_rubbish() -> None:
    with pytest.raises(ValueError, match="not a MAC"):
        P.parse_mac("hello")


def test_rssi_is_signed_dbm(sign: Sign) -> None:
    assert sign.rssi() == -31


def test_status_signal_strength_is_the_magnitude_of_the_rssi(sign: Sign) -> None:
    """Live confirmation of a fact that was read out of a disassembler.

    ``status_get`` carries 31; ``cc3100_rssi`` says -31 dBm, same radio, same
    moment. The wire field drops the sign.
    """
    assert sign.status()["SIGNAL_STRENGTH"] == 31
    assert sign.status().rssi_dbm == sign.rssi() == -31


def test_wifi_firmware(sign: Sign) -> None:
    fw = sign.wifi_firmware()
    assert fw.nwp == "2.12.2.8"
    assert fw.mac == "1.5.0.10"
    assert fw.phy == "1.0.3.37"
    assert fw.chip_id == 67108864 == 0x04000000
    assert fw.rom == 13107


def test_bssid_from_two_commands_agrees(sign: Sign) -> None:
    """``wifi_bssid_get`` prints it; ``status_get`` splits it over two uint32s.

    ``BSSID_1`` is bytes 0-3 little-endian, ``BSSID_2`` is bytes 4-5. If the
    byte order were wrong these two would differ.
    """
    assert sign.bssid() == sign.status().bssid == "00:00:5e:00:53:00"


def test_eap_config(sign: Sign) -> None:
    eap = sign.eap_config()
    assert eap.method == "PEAP/MSCHAPV2"
    assert eap.username == "exampleuser"


def test_certificates_absent(sign: Sign) -> None:
    certs = sign.certificates()
    assert certs.present is False
    assert certs.lines == ("No EAP cert found!",)


# --------------------------------------------------------- network/server


def test_ipv4_config_is_stored_config_not_the_live_lease(sign: Sign) -> None:
    """``MODE: 1`` is DHCP, so the four addresses are unused leftovers.

    The sign this was captured from was answering on an entirely different
    address at the time.
    """
    config = sign.ipv4_config()
    assert config.mode == 1
    assert config.dhcp is True
    assert config.in_use is False, "DHCP: these addresses are not the device's"
    assert config.ip == "192.0.2.31"
    assert config.dns == "0.0.0.0"


def test_ethernet_mac_is_locally_administered(sign: Sign) -> None:
    """A synthesised default for a MAC that is compiled in but not wired up.

    The real capture's first octet is ``02``, the locally-administered bit, and
    the published fixture keeps that octet for that reason -- scrubbing it to
    ``00:`` would have deleted the only evidence that this field holds a
    synthesised default rather than a real vendor address.
    """
    eth = sign.ethernet_config()
    assert eth.locally_administered is True
    assert eth.mac.startswith("02:")
    assert eth.retry_count == 8
    assert eth.retry_timeout_ms == 2000


def test_server_config_holds_a_hostname_not_an_ip(sign: Sign) -> None:
    """Which is why a DNS re-point is an alternative to touching the device."""
    server = sign.server_config()
    assert server.host == "visionect.internal.example.com"
    assert server.port == 11113
    assert server.is_hostname is True
    assert server.heartbeat_minutes == 1


def test_heartbeat_agrees_with_the_status_dump(sign: Sign) -> None:
    assert sign.heartbeat() == sign.status()["HEART_BEAT_INTERVAL"] == 1


# -------------------------------------------------------- power / hardware


def test_battery_thresholds_read_as_inverted_hysteresis(sign: Sign) -> None:
    """OFF (3500) is *higher* than ON (3400). Reported as found, not corrected."""
    battery = sign.battery_config()
    assert battery.threshold_off_mv == 3500
    assert battery.threshold_on_mv == 3400
    assert battery.threshold_count == 1
    assert battery.threshold_off_mv > battery.threshold_on_mv


def test_charger_state(sign: Sign) -> None:
    charger = sign.charger()
    assert charger.mode == "fast charge"
    assert charger.current_ma == 50
    assert charger.voltage_mv == 4214


def test_charger_voltage_matches_the_status_dump(sign: Sign) -> None:
    assert sign.charger().voltage_mv == sign.status()["BATTERY_VOLTAGE"]


def test_temperature_reads_the_same_in_this_snapshot(sign: Sign) -> None:
    """All three read 24 degC here -- but they are **not** the same sensor.

    ``lmr`` is the LM75 on the board; ``EPD_TEMP_SENSOR`` is the panel's own.
    They agreed in this capture and a later live run had them one degree apart
    (26 and 25), so this test asserts the fixture, not an invariant. Do not
    tighten it into a cross-check: it would fail on hardware for a good reason.
    """
    status = sign.status()
    assert sign.temperature() == 24
    assert status["MIN_TEMPERATURE"] == 24
    assert status["EPD_TEMP_SENSOR"] == 24


def test_system_config_explains_the_chirp(sign: Sign) -> None:
    """``Touch mode: 3`` is "ON + Beep", which is why the sign beeps when it boots."""
    config = sign.system_config()
    assert config.touch_mode == 3
    assert config.touch_enabled is True
    assert config.beeps is True
    assert config.shipping_mode == 0


def test_log_config_reports_only_one_module(sign: Sign) -> None:
    assert sign.log_config().modules == {"Mobile": 0}


# ------------------------------------------------------------- filesystem


def test_fs_ls(sign: Sign) -> None:
    files = sign.files()
    assert len(files) == 6
    assert files[0].path == "/image0.pv2"
    assert files[0].name == "image0.pv2"
    assert files[0].size == 134409
    assert all(f.flags == 0 for f in files), "middle column is 0 for every file"


def test_fs_stats(sign: Sign) -> None:
    stats = sign.filesystem_stats()
    assert stats.total_blocks == 2048
    assert stats.used_blocks == 931
    assert stats.free_blocks == 1117


def test_block_size_falls_out_of_two_commands_agreeing(sign: Sign) -> None:
    """``fs_stats`` counts blocks, ``status_get`` counts bytes. 4096 bytes/block.

    And the free figures reconcile exactly, which is what makes this a
    derivation rather than an assumption.
    """
    stats = sign.filesystem_stats()
    status = sign.status()
    total_bytes = status["FS_TOTAL_SIZE"]
    assert total_bytes == 8388608
    assert stats.block_size(int(total_bytes)) == 4096
    assert stats.free_blocks is not None
    assert stats.free_blocks * 4096 == status["FS_FREE_SIZE"] == 4575232


def test_sf_list_parses_the_ascii_table(sign: Sign) -> None:
    devices = sign.spi_flash_devices()
    assert len(devices) == 1
    assert devices[0].index == 0
    assert devices[0].spi_channel == 2


def test_sf_list_skips_the_rule_line() -> None:
    assert P.parse_sf_list(["IDX | SPI_CHANNEL", "----|------------", " 00 |  2"]) == (
        P.SpiFlashDevice(index=0, spi_channel=2),
    )


# ------------------------------------------------------------------- tasks


def test_task_list_splits_on_tabs_so_names_with_spaces_survive(sign: Sign) -> None:
    """``"CC3100 RX"`` and ``"timer ms"`` would be shredded by a whitespace split."""
    tasks = sign.tasks()
    assert len(tasks.tasks) == 14
    assert "CC3100 RX" in tasks.names
    assert "timer ms" in tasks.names
    assert tasks.heap_free == 19136


def test_the_task_answering_the_command_is_the_running_one(sign: Sign) -> None:
    """``usb_cli_task`` is ``X``. It is also the one ``sf_rdid`` kills."""
    tasks = sign.tasks()
    assert tasks["usb_cli_task"].running is True
    assert tasks["usb_cli_task"].state == "X"
    assert [t.name for t in tasks.tasks if t.running] == ["usb_cli_task"]


def test_task_stack_free_is_words_not_bytes(sign: Sign) -> None:
    """The ``uxTaskGetStackHighWaterMark`` unit. 402 words is ~1.6 KB."""
    assert sign.tasks()["usb_cli_task"].stack_free == 402


# ------------------------------------------------------------ status dump


def test_status_dump_has_every_field_and_stops_at_the_sentinel(sign: Sign) -> None:
    status = sign.status()
    assert len(status.values) == 59
    assert "STATUS_END" not in status.values


def test_status_mixes_hex_and_decimal_and_both_parse(sign: Sign) -> None:
    status = sign.status()
    assert status["FW_CRC"] == 0x652BE4AD
    assert status["BATTERY_LEVEL"] == 100
    assert status["CHARGING_STATUS"] == 1


def test_the_firmwares_own_key_naming_is_inconsistent(sign: Sign) -> None:
    """``PROTOCOL VERSION`` has a space where all 59 others have an underscore.

    Kept verbatim rather than normalised, so a lookup matches what the device
    prints.
    """
    status = sign.status()
    assert status["PROTOCOL VERSION"] == 3
    assert "PROTOCOL_VERSION" not in status.values
    spaced = [k for k in status.values if " " in k]
    assert spaced == ["PROTOCOL VERSION"]


def test_status_firmware_matches_fw_version_get(sign: Sign) -> None:
    assert sign.status().firmware == sign.firmware_version().firmware == "7.4.4407"


def test_status_crc_matches_fw_version_get(sign: Sign) -> None:
    assert sign.status()["FW_CRC"] == sign.firmware_version().crc


def test_status_gtin_matches_gtin_get(sign: Sign) -> None:
    assert sign.status().gtin == sign.gtin()


def test_status_uptime_matches_the_uptime_command(sign: Sign) -> None:
    assert sign.status().uptime_minutes == sign.uptime() == 5


def test_status_display_size_is_one_panel_not_the_canvas(sign: Sign) -> None:
    """2880x640 with two panels, so the canvas is 2880x1280 and these keys are not it."""
    status = sign.status()
    assert status.display_size == (2880, 640)
    assert status["NUMBER_OF_SUPPORTED_DISPLAYS"] == 2
    assert sign.firmware_version().panel_count == 2


def test_status_connectivity_matches_conn_type_get(sign: Sign) -> None:
    assert sign.status()["CONNECTIVITY_USED"] == sign.connectivity().value == 6


def test_status_filesystem_block_size(sign: Sign) -> None:
    assert sign.status().filesystem_block_size == 4096


def test_a_textual_status_value_would_survive() -> None:
    """Future-proofing: an unparseable value stays a string rather than raising."""
    status = P.parse_status_dump(["FOO: 1", "BAR: something textual", "STATUS_END"])
    assert status.values == {"FOO": 1, "BAR": "something textual"}


# ------------------------------------------------------------------- units


def test_int_parsing_tolerates_the_firmwares_trailing_units() -> None:
    """``"2400 mV"``, ``"24 degC"``, ``"75 sec"``, ``"-44 dBm"`` -- all real."""
    assert P._int("2400 mV") == 2400
    assert P._int("24 degC") == 24
    assert P._int("75 sec") == 75
    assert P._int("0x1F") == 31
    assert P._int("-1") == -1
