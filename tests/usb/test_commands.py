"""The command table, the gating, and the 111-vs-160 delta.

The delta assertions are the interesting ones.  They are what stops the table
quietly becoming a copy of the vendor's documentation, which is what it would be
if nobody had plugged a sign in.
"""

from __future__ import annotations

import pytest

from pyvisionect.io.usb import (
    ABSENT_FROM_7_4_4407,
    ASSERTS_AND_KILLS_CLI,
    COMMANDS,
    DESTRUCTIVE_COMMANDS,
    DOCUMENTED_COUNT,
    HARD_COMMANDS,
    READ_COMMANDS,
    TCLV_FOR,
    UNDOCUMENTED_IN_7_4_4407,
    CommandNotInFirmware,
    ConsoleAssertionFailed,
    DangerousCommandRefused,
    Kind,
    SerialConsole,
    Sign,
    WriteNotAllowed,
)
from pyvisionect.io.usb.commands import lookup
from pyvisionect.io.usb.fake import HELP_TEXT, FakeTransport


def sign(**kwargs: object) -> Sign:
    return Sign(
        SerialConsole(
            transport=FakeTransport(),
            command_timeout=1.0,
            idle_timeout=0.02,
            read_timeout=0.0,
        ),
        **kwargs,  # type: ignore[arg-type]
    )


# ------------------------------------------------------------------- the delta


def test_this_firmware_has_111_of_the_160_documented_commands() -> None:
    assert len(COMMANDS) == 111
    assert DOCUMENTED_COUNT == 160


def test_the_table_matches_the_devices_own_help_output() -> None:
    """Parse ``help`` the way :meth:`Sign.refresh_commands` does and compare.

    This is the test that keeps the built-in table honest: it is derived from a
    capture, and so is the fixture, so a hand edit to either shows up here.
    """
    names = sign().refresh_commands()
    assert names == frozenset(COMMANDS)


def test_96_documented_commands_are_absent_and_none_of_them_is_in_the_table() -> None:
    assert len(ABSENT_FROM_7_4_4407) == 96
    assert ABSENT_FROM_7_4_4407.isdisjoint(COMMANDS)


def test_47_present_commands_are_undocumented() -> None:
    assert len(UNDOCUMENTED_IN_7_4_4407) == 47
    assert UNDOCUMENTED_IN_7_4_4407 <= frozenset(COMMANDS)
    assert all(not COMMANDS[name].documented for name in UNDOCUMENTED_IN_7_4_4407)


@pytest.mark.parametrize(
    "name",
    [
        "wifi_ssid_set",
        "flash_print",
        "fw_checksum_get",
        "accelerometer_conf_get",
        "mobile_conf_set",
        "scpu_version",
        "touch_fw_update",
        "t2s_speak",
        "frontlight_conf_set",
        "heater_conf_get",
        "rs9113_rssi",
        "sim5320_rssi",
    ],
)
def test_notable_documented_commands_this_firmware_lacks(name: str) -> None:
    """Whole hardware families, plus four one-offs that break documented advice."""
    assert name in ABSENT_FROM_7_4_4407
    assert name not in HELP_TEXT


def test_wifi_ssid_set_is_absent_which_breaks_the_documented_workaround() -> None:
    """The vendor's answer for an SSID with a space is a command that is not here.

    So on this firmware there is no way to set such an SSID over USB at all.
    """
    assert "wifi_ssid_set" in ABSENT_FROM_7_4_4407
    assert "wifi_psk_set" in COMMANDS, "the PSK setter does exist"
    assert "wifi_conf_set" in COMMANDS


def test_flash_print_is_absent_so_there_is_no_one_shot_settings_dump() -> None:
    assert "flash_print" in ABSENT_FROM_7_4_4407


@pytest.mark.parametrize(
    "name",
    [
        "encryption_config_get",
        "encryption_key_set",
        "encryption_mode_set",
        "fs_ls",
        "fs_stats",
        "fs_format",
        "task_list",
        "vlog_unify_levels",
        "sf_rdid",
        "max17135_dump",
        "wifi_bssid_get",
        "lmr",
    ],
)
def test_notable_undocumented_commands_this_firmware_has(name: str) -> None:
    assert name in UNDOCUMENTED_IN_7_4_4407
    assert name in COMMANDS


def test_the_encryption_commands_are_entirely_undocumented() -> None:
    """Which makes the device the only source of information about them."""
    for name in ("encryption_config_get", "encryption_key_set", "encryption_mode_set"):
        assert name in UNDOCUMENTED_IN_7_4_4407


# ------------------------------------------------------------- table integrity


def test_every_command_has_a_kind_and_the_tiers_partition_the_set() -> None:
    tiers = [
        READ_COMMANDS,
        DESTRUCTIVE_COMMANDS,
        HARD_COMMANDS,
        frozenset(n for n, c in COMMANDS.items() if c.kind is Kind.WRITE),
        frozenset(n for n, c in COMMANDS.items() if c.kind is Kind.ACTION),
    ]
    union: set[str] = set()
    for tier in tiers:
        assert union.isdisjoint(tier), "tiers must not overlap"
        union |= tier
    assert union == set(COMMANDS)


def test_lookup_is_case_insensitive_like_the_device() -> None:
    assert lookup("UPTIME") is COMMANDS["uptime"]
    assert lookup("  Uptime  ") is COMMANDS["uptime"]
    assert lookup("nope") is None


def test_arity_and_syntax() -> None:
    assert COMMANDS["server_tcp_set"].arity == 2
    assert COMMANDS["server_tcp_set"].syntax == "server_tcp_set <ip> <port>"
    assert COMMANDS["uptime"].arity == 0


def test_the_help_text_lies_about_two_commands() -> None:
    """Both read straight out of the device's own ``help``, and both are wrong."""
    assert COMMANDS["max17135_dump"].args == "", "listed nullary, rejects a bare call"
    assert COMMANDS["conn_fw_ver"].description == "Scan for WiFi APs", (
        "copy-paste error: it reports the connectivity firmware version"
    )


def test_every_tclv_id_referenced_is_in_the_tclv_table() -> None:
    from pyvisionect.devices.tclv import TCLV

    for name, ids in TCLV_FOR.items():
        assert name in COMMANDS, name
        for param_id in ids:
            assert param_id in TCLV, (name, param_id)


def test_the_network_read_only_ids_all_have_a_usb_command_here() -> None:
    """The point of the whole package: every id the network cannot write, USB can.

    Except the two mobile ones, which need commands this firmware was not built
    with -- a WiFi-only build has no cellular setters.
    """
    from pyvisionect.devices.tclv import NETWORK_READ_ONLY

    settable = {param_id for ids in TCLV_FOR.values() for param_id in ids}
    missing = NETWORK_READ_ONLY - settable
    assert missing == {70}, "MOB_SECURITY needs mobile_* commands, absent from this build"


def test_wifi_conf_set_argument_order_is_not_ascending_by_id() -> None:
    """ssid, psk, security, band -> 65, 67, 66, 68. Getting this wrong writes the
    PSK into the security field."""
    assert TCLV_FOR["wifi_conf_set"] == (65, 67, 66, 68)


# ----------------------------------------------------------- the assert hazard


def test_the_two_asserting_commands_are_recorded_with_their_locations() -> None:
    assert ASSERTS_AND_KILLS_CLI == {
        "sf_rdid": "spi_flash_cli.c:139",
        "sf_rdst": "spi_flash_cli.c:188",
    }


def test_sf_list_is_not_in_the_hazard_set() -> None:
    """It is the safe member of the family, and how you find what to select."""
    assert "sf_list" not in ASSERTS_AND_KILLS_CLI
    assert COMMANDS["sf_list"].asserts is None


@pytest.mark.parametrize("name", sorted(ASSERTS_AND_KILLS_CLI))
def test_an_asserting_command_is_refused_before_anything_is_written(name: str) -> None:
    s = sign(allow_destructive=True)
    with pytest.raises(ConsoleAssertionFailed, match="spi_flash_cli"):
        s._run(name, expect_prompt=False)
    assert s.console._require().written == []  # type: ignore[attr-defined]


def test_the_asserting_accessors_refuse_through_the_typed_surface() -> None:
    s = sign(allow_destructive=True)
    with pytest.raises(ConsoleAssertionFailed):
        s.spi_flash_id()
    with pytest.raises(ConsoleAssertionFailed):
        s.spi_flash_status()


# ------------------------------------------------------------------- gating


def test_reads_are_allowed_by_default() -> None:
    assert sign().uptime() == 5


def test_a_setter_is_refused_on_a_read_only_sign() -> None:
    """``server_hb_set`` is a plain ``WRITE``: harmless, and network-writable too.

    So the refusal names the gentlest flag that would allow it, rather than
    scaring the caller with the destructive one.
    """
    s = sign()
    with pytest.raises(WriteNotAllowed, match="allow_writes=True"):
        s.set_heartbeat(5)
    assert s.console._require().written == []  # type: ignore[attr-defined]


def test_a_network_setter_is_refused_with_the_destructive_flag_named() -> None:
    """``server_tcp_set`` is the command that strands a sign. Different tier."""
    s = sign(allow_writes=True)
    with pytest.raises(DangerousCommandRefused, match="allow_destructive=True"):
        s.set_server("example.com")


def test_an_ordinary_write_tier_command_names_allow_writes() -> None:
    s = sign()
    with pytest.raises(WriteNotAllowed, match="allow_writes=True"):
        s._run("vlog_set_default_levels")


@pytest.mark.parametrize("name", sorted(DESTRUCTIVE_COMMANDS))
def test_every_destructive_command_needs_allow_destructive(name: str) -> None:
    s = sign(allow_writes=True)
    if name in ASSERTS_AND_KILLS_CLI:
        pytest.skip("covered by the assert tests, which refuse even earlier")
    with pytest.raises(DangerousCommandRefused, match="allow_destructive=True"):
        s._run(name)
    assert s.console._require().written == []  # type: ignore[attr-defined]


@pytest.mark.parametrize("name", sorted(HARD_COMMANDS))
def test_every_hard_command_needs_the_loud_flag(name: str) -> None:
    s = sign(allow_destructive=True)
    with pytest.raises(DangerousCommandRefused, match="i_really_mean_it=True"):
        s._run(name)
    assert s.console._require().written == []  # type: ignore[attr-defined]


def test_the_hard_tier_is_the_set_the_brief_calls_out() -> None:
    assert HARD_COMMANDS == frozenset(
        {
            "fs_format",
            "cc3100_format",
            "cc3100_fw_upgrade",
            "feat_enable",
            "feat_disable",
            "cli_password_set",
            "sf_unprot",
            "sf_wrst",
        }
    )


def test_the_flags_imply_each_other_downwards() -> None:
    assert sign(i_really_mean_it=True).allow_destructive is True
    assert sign(i_really_mean_it=True).allow_writes is True
    assert sign(allow_destructive=True).allow_writes is True
    assert sign(allow_destructive=True).i_really_mean_it is False


def test_shipping_mode_is_refused_on_the_argument_not_the_command_name() -> None:
    s = sign(allow_destructive=True)
    with pytest.raises(DangerousCommandRefused, match="shipping mode"):
        s.set_system_config(1, 1, shipping_mode=1)
    assert s.console._require().written == []  # type: ignore[attr-defined]
    s = Sign(
        SerialConsole(
            transport=FakeTransport(replies={"system_conf_set": "OK"}),
            command_timeout=1.0,
            idle_timeout=0.02,
            read_timeout=0.0,
        ),
        allow_destructive=True,
    )
    s.set_system_config(1, 1, shipping_mode=0)
    assert s.console._require().written == ["system_conf_set 1 1 0\r"]  # type: ignore[attr-defined]


def test_flash_save_is_in_the_destructive_tier_because_it_commits() -> None:
    """A setter is undoable by power-cycling right up until this runs."""
    assert COMMANDS["flash_save"].kind is Kind.DESTRUCTIVE


def test_pss_is_an_action_so_it_still_needs_allow_writes() -> None:
    """It changes nothing, but it makes the sign do something."""
    assert COMMANDS["pss"].kind is Kind.ACTION
    with pytest.raises(WriteNotAllowed, match="allow_writes=True"):
        sign().send_status_packet()
    s = Sign(
        SerialConsole(
            transport=FakeTransport(replies={"pss": "pss\r\nrv: 0\r\n> "}),
            command_timeout=1.0,
            idle_timeout=0.02,
            read_timeout=0.0,
        ),
        allow_writes=True,
    )
    assert s.send_status_packet().rv == 0


# -------------------------------------------------- firmware-awareness check


def test_a_missing_command_is_refused_locally_once_help_has_been_read() -> None:
    s = sign()
    assert s.has("flash_print") is None, "unknown before refresh_commands"
    s.refresh_commands()
    assert s.has("flash_print") is False
    assert s.has("uptime") is True
    with pytest.raises(CommandNotInFirmware, match="flash_print"):
        s._run("flash_print")
    assert "flash_print" not in "".join(
        s.console._require().written  # type: ignore[attr-defined]
    )


def test_check_false_sends_it_anyway() -> None:
    """And the device, not the library, is then the one that says no."""
    from pyvisionect.io.usb import UnknownCommand

    s = sign(allow_writes=True)
    s.refresh_commands()
    with pytest.raises(UnknownCommand):
        s._run("flash_print", check=False)


def test_a_command_the_library_has_never_heard_of_defaults_to_the_write_tier() -> None:
    """Fail safe: an unknown name is treated as a write, not as a read.

    A firmware this library has not seen may have commands it has not seen, and
    guessing "probably harmless" about an unknown name on someone's hardware is
    the wrong default.
    """
    s = sign()
    with pytest.raises(WriteNotAllowed):
        s._run("some_future_command", check=False)
