"""The command table, the gating, and the 112-vs-111-vs-160 delta.

The delta assertions are the interesting ones.  They are what stops the table
quietly becoming a copy of the vendor's documentation, which is what it would be
if nobody had plugged a sign in.

Three numbers, and keeping them apart is the point: the vendor documents **160**
commands, this firmware's ``help`` prints **111**, and the firmware is known to
answer **112** -- ``wifi_ssid_set`` is present and simply never listed.  So
``help`` is not a complete index of the firmware, and a test that reads "absent
from ``help``" as "absent from the firmware" is asserting something the device
has already disproved.
"""

from __future__ import annotations

import pytest

from pyvisionect.io.usb import (
    ABSENT_FROM_7_4_4407,
    ASSERTS_AND_KILLS_CLI,
    COMMANDS,
    HIDDEN_IN_7_4_4407,
    LISTED_BY_HELP,
    DESTRUCTIVE_COMMANDS,
    DOCUMENTED_COUNT,
    HARD_COMMANDS,
    PROBED_ABSENT_7_4_4407,
    READ_COMMANDS,
    TCLV_FOR,
    UNDOCUMENTED_IN_7_4_4407,
    VLOG_DESTINATIONS,
    VLOG_LEVELS,
    VLOG_QUIET,
    VLOG_VERBOSE,
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


def test_this_firmware_answers_112_commands_and_help_lists_111_of_the_162() -> None:
    """The two counts are different numbers and mean different things."""
    assert len(COMMANDS) == 112, "known to exist, help-listed plus hidden"
    assert len(LISTED_BY_HELP) == 111, "what help actually prints"
    assert DOCUMENTED_COUNT == 162


def test_the_three_counts_are_arithmetically_consistent() -> None:
    """documented = (present and documented) + (documented but unlisted).

    This is the check that was failing silently while ``DOCUMENTED_COUNT`` said
    160 and ``ABSENT_FROM_7_4_4407`` had 95 entries: ``rs9110_scan`` and
    ``rs9113_scan`` are on the vendor's page and were in neither set, so the
    books were out by two at both ends and still looked self-consistent.
    """
    present_and_documented = frozenset(COMMANDS) - UNDOCUMENTED_IN_7_4_4407
    assert len(present_and_documented) + len(ABSENT_FROM_7_4_4407) == DOCUMENTED_COUNT
    assert present_and_documented.isdisjoint(ABSENT_FROM_7_4_4407)


def test_the_table_matches_the_devices_own_help_output() -> None:
    """Parse ``help`` the way :meth:`Sign.refresh_commands` does and compare.

    This is the test that keeps the built-in table honest: it is derived from a
    capture, and so is the fixture, so a hand edit to either shows up here.

    The comparison is against :data:`LISTED_BY_HELP`, **not** :data:`COMMANDS`.
    The capture is one ``help`` frame, and ``help`` does not print every command
    the firmware has, so the hidden ones are legitimately missing from it.
    """
    names = sign().refresh_commands()
    assert names == LISTED_BY_HELP, (
        "this compares a captured `help` frame against the set of commands help "
        "is expected to print. It is LISTED_BY_HELP and not COMMANDS because "
        "COMMANDS also carries the hidden commands -- present on the device, "
        "never printed by help -- which cannot appear in a help capture. A "
        "difference here means either the capture or the table was hand-edited."
    )
    assert frozenset(COMMANDS) - names == HIDDEN_IN_7_4_4407, (
        "everything in the table that help did not print must be accounted for "
        "as hidden, not quietly dropped"
    )


def test_97_documented_commands_are_unlisted_and_none_of_them_is_in_the_table() -> None:
    """"Unlisted", not "absent" -- ``wifi_ssid_set`` is why that distinction exists."""
    assert len(ABSENT_FROM_7_4_4407) == 97
    assert ABSENT_FROM_7_4_4407.isdisjoint(COMMANDS)
    assert ABSENT_FROM_7_4_4407.isdisjoint(LISTED_BY_HELP)


def test_49_of_the_unlisted_commands_were_probed_and_are_genuinely_absent() -> None:
    """The A1 result: the oracle was pointed at every candidate it can safely hit.

    "Safely" is the whole constraint. The probe is a *bare* invocation, which is
    only rejected before anything happens if the command has a required
    argument; a nullary command sent bare runs. So the 49 are exactly the
    unlisted commands the vendor reference gives a required argument, minus the
    ones that flash firmware or whose arity the sources disagree about.
    """
    assert len(PROBED_ABSENT_7_4_4407) == 49
    assert PROBED_ABSENT_7_4_4407 <= ABSENT_FROM_7_4_4407, "a subset of the unlisted"
    assert PROBED_ABSENT_7_4_4407.isdisjoint(COMMANDS), "proven absent, so not present"
    assert PROBED_ABSENT_7_4_4407.isdisjoint(HIDDEN_IN_7_4_4407)
    assert len(ABSENT_FROM_7_4_4407 - PROBED_ABSENT_7_4_4407) == 48, "still unprobed"


def test_the_unprobed_remainder_is_unprobed_for_a_stated_reason() -> None:
    """Each of these is a command the bare-invocation oracle must not be used on.

    Spot-checks of the four reasons, so that "we did not probe it" stays a
    decision with a justification rather than an oversight: nullary in the
    vendor reference, flashes firmware, arity disputed between the reference and
    the field notes, or would make noise.
    """
    unprobed = ABSENT_FROM_7_4_4407 - PROBED_ABSENT_7_4_4407
    for name in (
        "flash_print",  # nullary: a bare call would dump and is the one we want
        "fw_checksum_get",  # nullary
        "scpu_version",  # nullary
        "touch_fw_update",  # flashes the touch controller
        "scpu_upgrade",  # flashes the co-processor
        "vcom_test",  # arity disputed, and drives the panel's VCOM rail
        "touch_test",  # arity disputed
        "scpu_reset",  # arity disputed, and asserts a reset line
        "t2s_speak",  # would speak if the documented arity is wrong
    ):
        assert name in unprobed, f"{name} must not be bare-probed"


def test_the_hidden_set_bridges_the_table_and_what_help_prints() -> None:
    """The invariants that keep "exists" and "is listed" from merging again.

    ``wifi_ssid_set`` is in the table *and* missing from ``help``, so it has to
    be somewhere; if it were nowhere, one of the three sets would be lying.
    """
    assert HIDDEN_IN_7_4_4407 <= frozenset(COMMANDS), (
        "hidden means hidden-but-present, so it must be in the table"
    )
    assert HIDDEN_IN_7_4_4407.isdisjoint(ABSENT_FROM_7_4_4407), (
        "a command cannot be both proven present and recorded as unlisted-absent"
    )
    assert LISTED_BY_HELP | HIDDEN_IN_7_4_4407 == frozenset(COMMANDS), (
        "every known command is either printed by help or hidden; no third state"
    )
    assert LISTED_BY_HELP.isdisjoint(HIDDEN_IN_7_4_4407)


def test_wifi_ssid_set_is_the_only_hidden_command() -> None:
    """And now on evidence: the probing run (``A1``) found no others.

    49 of the 97 unlisted commands were probed bare on 2026-10-05 and every one
    answered ``Command '<x>' not recognised.`` So this set is one entry not for
    want of looking -- ``wifi_ssid_set`` really is a one-off. It could still
    grow via the 48 the oracle cannot safely touch, which is why
    :data:`PROBED_ABSENT_7_4_4407` is recorded separately from
    :data:`ABSENT_FROM_7_4_4407` rather than the two being merged.
    """
    assert HIDDEN_IN_7_4_4407 == frozenset({"wifi_ssid_set"})
    assert HIDDEN_IN_7_4_4407.isdisjoint(PROBED_ABSENT_7_4_4407), (
        "nothing can be both proven present and proven absent"
    )


def test_47_present_commands_are_undocumented() -> None:
    assert len(UNDOCUMENTED_IN_7_4_4407) == 47
    assert UNDOCUMENTED_IN_7_4_4407 <= frozenset(COMMANDS)
    assert all(not COMMANDS[name].documented for name in UNDOCUMENTED_IN_7_4_4407)


# These are documented by the vendor and not printed by this firmware's `help`.
# That is *all* that is asserted. None of them has been probed by bare
# invocation, so "not in help" does not license the claim "not in the firmware"
# -- `wifi_ssid_set` was in this list until the device said otherwise. Several
# of these are nullary (`flash_print`, `fw_checksum_get`, `scpu_version`), where
# the bare-invocation oracle is unsafe because the command would simply run.
@pytest.mark.parametrize(
    "name",
    [
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
def test_notable_documented_commands_this_firmware_does_not_list(name: str) -> None:
    """Whole hardware families, plus the one-offs that break documented advice.

    Unverified: absent from ``help``, which no longer implies absent from the
    firmware. See the comment above the cases.
    """
    assert name in ABSENT_FROM_7_4_4407
    assert name not in HELP_TEXT


def test_wifi_ssid_set_is_hidden_from_help_but_present() -> None:
    """The vendor's answer for a spaced SSID is here after all -- just not listed.

    Verified on the device: a bare ``wifi_ssid_set`` answers ``E: Invalid
    argument(s)``, which is argument validation rejecting the call, whereas a
    command the firmware does not have answers ``Command '<x>' not recognised.``
    So the command exists and runs.

    Why it matters: it takes the **SSID alone**, with no PSK, where
    ``wifi_conf_set`` takes four whitespace-separated arguments. That makes it
    the only candidate route to an SSID containing a space, and it needs no
    credentials to try. It has since been tried, and the space survives: the
    command takes the rest of the line verbatim (``OPEN-QUESTIONS.md`` A2).
    This test still claims only the command's existence and its arity -- the
    behaviour is pinned in ``test_provisioning.py``, where the code that emits
    it lives.
    """
    assert "wifi_ssid_set" in COMMANDS, "present on 7.4.4407"
    assert "wifi_ssid_set" in HIDDEN_IN_7_4_4407
    assert "wifi_ssid_set" not in ABSENT_FROM_7_4_4407
    assert "wifi_ssid_set" not in LISTED_BY_HELP
    assert "wifi_ssid_set" not in HELP_TEXT, "the capture is help, and help omits it"

    entry = COMMANDS["wifi_ssid_set"]
    assert entry.arity == 1, "the SSID alone -- no PSK, so it needs no credentials"
    assert entry.syntax == "wifi_ssid_set <ssid>"
    assert entry.documented is True, "the vendor's reference does list it"
    assert entry.kind is Kind.DESTRUCTIVE, "it rewrites TCLV 65 on a live radio"

    assert "wifi_conf_set" in COMMANDS
    assert COMMANDS["wifi_conf_set"].arity == 4, "which is why a space breaks it"
    assert "wifi_psk_set" in COMMANDS, "the PSK setter does exist too"


def test_wifi_ssid_set_is_the_usb_setter_for_tclv_65() -> None:
    """TCLV 65 is network-read-only, so a USB setter for it is the whole point."""
    assert TCLV_FOR["wifi_ssid_set"] == (65,)
    assert TCLV_FOR["wifi_conf_set"][0] == 65


def test_flash_print_is_unlisted_so_there_is_no_known_one_shot_settings_dump() -> None:
    """Unlisted, and unlike ``wifi_ssid_set`` it cannot be cheaply probed.

    The vendor lists it as nullary, and the bare-invocation existence oracle is
    unsafe for a nullary command: the call would execute rather than be rejected
    for bad arguments. So this stays "not listed", and
    :meth:`Sign.dump` keeps walking the per-area getters.
    """
    assert "flash_print" in ABSENT_FROM_7_4_4407
    assert "flash_print" not in COMMANDS
    assert "flash_print" not in HIDDEN_IN_7_4_4407, "never probed, so not claimed"


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


def test_a_hidden_command_is_still_refused_after_a_refresh() -> None:
    """Known limitation, pinned so it is a decision and not a surprise.

    The local gate is built on ``help`` alone, because on firmware this library
    has not seen ``help`` is the only evidence there is. ``wifi_ssid_set`` is
    the case where that is too conservative: we know it works. ``check=False``
    is the escape hatch, and the device is then the one that answers -- which
    is exactly what :meth:`Sign.set_wifi` and ``Plan.execute`` now take for
    that one command, and for no other.
    """
    s = sign(allow_destructive=True)
    s.refresh_commands()
    assert "wifi_ssid_set" in COMMANDS, "the table knows it exists"
    assert s.has("wifi_ssid_set") is False, "but help did not say so"
    with pytest.raises(CommandNotInFirmware, match="wifi_ssid_set"):
        s._run("wifi_ssid_set Foo")


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


# ------------------------------------------------------------------- vlog_*


def test_the_vlog_family_is_a_matrix_not_a_single_level() -> None:
    """Levels are set per source and per destination independently.

    Which is why ``log_config_get``'s single ``{"Mobile": 0}`` is at most one
    cell of it, and why there is no "the" log level to read back.
    """
    assert COMMANDS["vlog_set_source_level"].args == "<source> <level>"
    assert COMMANDS["vlog_set_destination_level"].args == "<destination> <level>"
    assert COMMANDS["vlog_unify_levels"].args == "<usb_level>"
    assert COMMANDS["vlog_set_default_levels"].args == ""


def test_the_unify_help_line_contradicts_its_own_signature() -> None:
    """It takes ``<usb_level>`` and says it "resets to default". Both cannot hold.

    Settled on hardware (``A3``): the signature is right and the help line is
    wrong. The command uses its argument -- level 1 silences the port and level
    5 floods it -- and ``vlog_set_default_levels`` is the one that resets.
    Pinned because the description is the device's own text, verbatim, and the
    table must keep recording what the device says even where it is wrong.
    """
    entry = COMMANDS["vlog_unify_levels"]
    assert entry.description == "Reset logger levels to default for USB"
    assert entry.arity == 1


def test_the_vlog_level_scale_is_1_to_5_and_0_is_not_a_level() -> None:
    """Measured on 7.4.4407, and the reason ``A3``'s original question dissolved.

    ``A3`` asked whether ``0`` meant "emit nothing" or "emit everything". It
    means neither: the firmware answers ``E: Invalid argument(s)`` to ``0``,
    ``-1``, ``6``, ``99`` and a non-numeric argument, and accepts 1..5.
    """
    assert VLOG_LEVELS == range(1, 6)
    assert 0 not in VLOG_LEVELS, "the question A3 was built on"
    assert 6 not in VLOG_LEVELS
    assert VLOG_QUIET == 1, "the silent end"
    assert VLOG_VERBOSE == 5, "everything, including image and frame detail"
    assert VLOG_QUIET in VLOG_LEVELS and VLOG_VERBOSE in VLOG_LEVELS


def test_there_are_nine_log_destinations_and_the_source_space_is_unbounded() -> None:
    """The asymmetry is the finding, and it is why one of them gets validated here.

    ``vlog_set_destination_level`` range-checks its id (``-1`` and ``9`` up are
    refused, so there are exactly nine sinks). ``vlog_set_source_level`` does
    **not** -- it accepted ``-1``, ``32`` and ``9999`` silently -- so the source
    namespace cannot be enumerated by probing and a bad id is an unchecked
    index. :meth:`Sign.set_log_source_level` refuses a negative source itself
    because the device will not.
    """
    assert VLOG_DESTINATIONS == range(0, 9)
    assert 9 not in VLOG_DESTINATIONS
    assert -1 not in VLOG_DESTINATIONS


def test_the_whole_vlog_family_is_undocumented_by_the_vendor() -> None:
    for name in (
        "vlog_set_default_levels",
        "vlog_set_destination_level",
        "vlog_set_source_level",
        "vlog_unify_levels",
    ):
        assert name in UNDOCUMENTED_IN_7_4_4407


def test_unify_usb_log_levels_requires_an_explicit_level() -> None:
    """Still no default, now for a different reason.

    The scale's direction is known, but which end a caller wants is not: a
    console driver wants 1, someone debugging a dead link wants 3 or 5. So the
    argument stays mandatory, and :meth:`Sign.silence_usb_logs` is the
    convenience for the common case.
    """
    import inspect

    signature = inspect.signature(Sign.unify_usb_log_levels)
    assert signature.parameters["level"].default is inspect.Parameter.empty


def test_the_vlog_setters_are_gated_as_writes() -> None:
    s = sign()
    for call in (
        lambda: s.unify_usb_log_levels(VLOG_QUIET),
        lambda: s.silence_usb_logs(),
        lambda: s.set_log_source_level(1, 1),
        lambda: s.set_log_destination_level(1, 1),
        lambda: s.default_logs(),
    ):
        with pytest.raises(WriteNotAllowed, match="allow_writes=True"):
            call()
    assert s.console._require().written == []  # type: ignore[attr-defined]


def _writable_sign(reply_for: str) -> Sign:
    return Sign(
        SerialConsole(
            transport=FakeTransport(replies={reply_for: "rv: 0"}),
            command_timeout=1.0,
            idle_timeout=0.02,
            read_timeout=0.0,
        ),
        allow_writes=True,
    )


def test_unify_sends_the_level_it_was_given() -> None:
    s = _writable_sign("vlog_unify_levels")
    s.unify_usb_log_levels(5)
    assert s.console._require().written == ["vlog_unify_levels 5\r"]  # type: ignore[attr-defined]


def test_silence_usb_logs_sends_the_quiet_end_of_the_scale() -> None:
    """The whole point: a caller should not have to remember which end is quiet."""
    s = _writable_sign("vlog_unify_levels")
    s.silence_usb_logs()
    assert s.console._require().written == ["vlog_unify_levels 1\r"]  # type: ignore[attr-defined]


@pytest.mark.parametrize("level", [-1, 0, 6, 99])
def test_an_invalid_level_is_refused_without_a_round_trip(level: int) -> None:
    """The device would answer ``E: Invalid argument(s)``; say so without asking.

    ``0`` is in here deliberately. It was the value the old API invited callers
    to try, and it is not a level at all.
    """
    s = _writable_sign("vlog_unify_levels")
    with pytest.raises(ValueError, match="1..5"):
        s.unify_usb_log_levels(level)
    with pytest.raises(ValueError, match="1..5"):
        s.set_log_source_level(1, level)
    with pytest.raises(ValueError, match="1..5"):
        s.set_log_destination_level(1, level)
    assert s.console._require().written == [], (  # type: ignore[attr-defined]
        "nothing should reach the device"
    )


def test_an_out_of_range_destination_is_refused_but_the_device_would_too() -> None:
    s = _writable_sign("vlog_set_destination_level")
    for destination in (-1, 9, 9999):
        with pytest.raises(ValueError, match="0..8"):
            s.set_log_destination_level(destination, 1)
    s.set_log_destination_level(8, 1)
    assert s.console._require().written == [  # type: ignore[attr-defined]
        "vlog_set_destination_level 8 1\r"
    ]


def test_a_negative_log_source_is_refused_although_the_device_accepts_it() -> None:
    """The one place this library is stricter than the firmware, on purpose.

    ``vlog_set_source_level -1 1`` is accepted by 7.4.4407 without complaint,
    which makes it an unchecked index into firmware state rather than a
    no-op. There is no upper bound to enforce because probing cannot find one.
    """
    s = _writable_sign("vlog_set_source_level")
    with pytest.raises(ValueError, match="non-negative"):
        s.set_log_source_level(-1, 1)
    assert s.console._require().written == []  # type: ignore[attr-defined]
    s.set_log_source_level(9999, 1)
    assert s.console._require().written == [  # type: ignore[attr-defined]
        "vlog_set_source_level 9999 1\r"
    ], "no upper bound is claimed, because none was found"
