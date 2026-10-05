"""Provisioning plans: the dry run, the refusals, and the execution path.

A plan that cannot be inspected before it runs is a plan that strands somebody's
sign, so most of this file is about the inspection.
"""

from __future__ import annotations

import pytest

from pyvisionect.io.usb import (
    DEFAULT_SERVER_PORT,
    DangerousCommandRefused,
    Kind,
    Plan,
    SerialConsole,
    Sign,
    WifiSecurity,
    plan_bootstrap,
    plan_repoint,
    plan_wifi,
)
from pyvisionect.io.usb.fake import CAPTURED, HELP_TEXT, FakeTransport

SETTER_REPLIES = {
    **CAPTURED,
    "help": HELP_TEXT,
    "wifi_conf_set": "rv: 0",
    "wifi_ssid_set": "WiFi SSID set",
    "wifi_psk_set": "rv: 0",
    "wifi_security_set": "rv: 0",
    "server_tcp_set": "rv: 0",
    "flash_save": "rv: 0",
    "conn_type_set": "rv: 0",
    "reboot": "rv: 0",
}


def sign(**kwargs: object) -> Sign:
    return Sign(
        SerialConsole(
            transport=FakeTransport(replies=SETTER_REPLIES),
            command_timeout=1.0,
            idle_timeout=0.02,
            read_timeout=0.0,
        ),
        **kwargs,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------- the dry run


def test_a_plan_produces_commands_without_any_transport() -> None:
    """The whole point: no port, no device, no risk, and you can read it."""
    plan = plan_repoint("my-listener.example.com")
    assert plan.commands == (
        "server_tcp_set my-listener.example.com 11113",
        "flash_save",
        "reboot",
    )


def test_the_default_port_is_the_one_the_listener_uses() -> None:
    assert DEFAULT_SERVER_PORT == 11113


def test_the_bootstrap_sequence_is_the_vendors() -> None:
    plan = plan_bootstrap("ExampleAP", "s3cret", "listener.example.com")
    assert plan.commands == (
        "wifi_conf_set ExampleAP s3cret wpa2 0",
        "server_tcp_set listener.example.com 11113",
        "flash_save",
        "reboot",
    )


def test_flash_save_is_never_optional_in_a_plan() -> None:
    """Without it the whole exercise is undone by the reboot that follows it."""
    for plan in (
        plan_repoint("h"),
        plan_bootstrap("s", "p", "h"),
        plan_bootstrap(None, None, "h"),
    ):
        assert "flash_save" in plan.commands
        assert plan.commands.index("flash_save") < plan.commands.index("reboot")


def test_flash_save_comes_after_every_setter() -> None:
    plan = plan_bootstrap("s", "p", "h", conn_type=6)
    commands = list(plan.commands)
    save = commands.index("flash_save")
    assert all(not c.endswith("_set") for c in commands[save:])
    assert any("_set" in c for c in commands[:save])


def test_reboot_is_the_only_step_that_expects_no_prompt() -> None:
    plan = plan_repoint("h")
    assert [s.command for s in plan.steps if not s.expect_prompt] == ["reboot"]


def test_reboot_can_be_dropped() -> None:
    assert "reboot" not in plan_repoint("h", reboot=False).commands


def test_wifi_can_be_left_alone() -> None:
    plan = plan_bootstrap(None, None, "h")
    assert not any("wifi" in c for c in plan.commands)


def test_conn_type_is_inserted_first_when_given() -> None:
    plan = plan_bootstrap("s", "p", "h", conn_type=6)
    assert plan.commands[0] == "conn_type_set 6"


def test_describe_explains_every_step_and_lists_the_caveats() -> None:
    text = plan_repoint("listener.example.com").describe()
    assert "server_tcp_set listener.example.com 11113" in text
    assert "TCLV 18" in text
    assert "TCLV 53" in text
    assert "Before you run this:" in text


def test_describe_offers_the_cheaper_alternative_first() -> None:
    """A DNS re-point needs no commands at all, and nobody thinks of it."""
    text = plan_repoint("listener.example.com").describe()
    assert "re-pointing that name" in text
    assert "no commands at all" in text


def test_describe_warns_that_recovery_is_usb_only() -> None:
    assert "USB is the only way" in plan_repoint("h").describe()


def test_describe_says_nothing_persists_until_flash_save() -> None:
    assert "power-cycle to undo" in plan_repoint("h").describe()


def test_every_step_is_in_the_destructive_tier() -> None:
    """Which is why :meth:`Plan.execute` needs ``allow_destructive``."""
    for step in plan_bootstrap("s", "p", "h", conn_type=6).steps:
        assert step.kind is Kind.DESTRUCTIVE, step.command


def test_every_step_is_a_command_this_firmware_has() -> None:
    plan = plan_bootstrap("s", "p", "h", conn_type=6)
    assert plan.unavailable == ()
    assert all(step.in_firmware for step in plan.steps)


# -------------------------------------------- whitespace in the credentials
#
# `wifi_ssid_set` takes the rest of the line verbatim -- measured on 7.4.4407,
# `OPEN-QUESTIONS.md` A2.  There is no quoting convention, so the SSID goes out
# bare and anything quote-like in it lands in the SSID.


def test_a_spaced_ssid_uses_the_three_setter_route() -> None:
    """``wifi_conf_set`` is positional, so a spaced SSID cannot use it."""
    plan = plan_wifi("My Home WiFi", "secret")
    assert plan.commands == (
        "wifi_psk_set secret",
        "wifi_security_set wpa2",
        "wifi_ssid_set My Home WiFi",
    )


def test_the_ssid_is_written_last() -> None:
    """It is the field that decides association, so it goes in once the rest is."""
    assert plan_wifi("My Home WiFi", "secret").commands[-1].startswith("wifi_ssid_set ")


def test_the_spaced_ssid_is_sent_bare_not_quoted_or_escaped() -> None:
    """Quoting would corrupt it: the quotes land *in* the SSID.

    Measured: ``wifi_ssid_set "Two Words"`` read back as ``"Two Words"``,
    quotes included, and ``Two\\ Words`` read back with the backslash.
    """
    command = plan_wifi("McDonald's Free WiFi", "p").commands[-1]
    assert command == "wifi_ssid_set McDonald's Free WiFi"
    assert '"' not in command
    assert "\\" not in command


def test_a_spaceless_ssid_still_uses_the_one_shot_wifi_conf_set() -> None:
    """The common case is unchanged -- one command, all four fields."""
    assert plan_wifi("ExampleAP", "secret", band=2).commands == (
        "wifi_conf_set ExampleAP secret wpa2 2",
    )


def test_a_spaced_ssid_with_a_non_default_band_is_refused() -> None:
    """There is no ``wifi_band_set``, in help or in the vendor reference.

    ``wifi_conf_set`` is TCLV 68's only writer and it is the command the space
    rules out, so the two requests are genuinely incompatible. Saying so beats
    silently ignoring the band.
    """
    with pytest.raises(ValueError, match="wifi_band_set") as excinfo:
        plan_wifi("My Home WiFi", "p", band=2)
    assert "band=2 cannot be combined with a spaced SSID" in str(excinfo.value)


def test_a_spaced_ssid_with_the_default_band_says_band_is_untouched() -> None:
    notes = " ".join(plan_wifi("My Home WiFi", "p").notes)
    assert "Band (TCLV 68) is NOT written by this route" in notes


def test_the_plan_quotes_the_exact_ssid_that_will_land() -> None:
    """A dry run has to show the bytes, because there is no escaping to undo."""
    notes = " ".join(plan_wifi('My "Home" WiFi', "p").notes)
    assert repr('My "Home" WiFi') in notes


def test_a_tab_in_the_ssid_is_refused_because_the_line_editor_eats_it() -> None:
    """Measured: ``wifi_ssid_set Two<TAB>Words`` set ``TwoWords``.

    TAB is the console's usage-lookup key -- it printed
    ``wifi_ssid_set <ssid>: Set WiFi SSID`` and redisplayed the buffer with the
    tab gone. So this is the one whitespace character that really cannot be
    carried, and it fails *silently* rather than erroring, which is why it is
    refused here.
    """
    with pytest.raises(ValueError, match="a tab") as excinfo:
        plan_wifi("Two\tWords", "p")
    assert "TwoWords" in str(excinfo.value)


@pytest.mark.parametrize("char", ["\r", "\n"])
def test_cr_and_lf_in_the_ssid_are_refused(char: str) -> None:
    """CR submits the line; LF desynchronises the stream."""
    with pytest.raises(ValueError):
        plan_wifi(f"Two{char}Words", "p")


def test_the_wifi_ssid_set_step_is_marked_hidden() -> None:
    """So ``Plan.execute`` knows to bypass the help-derived gate for it."""
    steps = {step.name: step for step in plan_wifi("My Home WiFi", "p").steps}
    assert steps["wifi_ssid_set"].hidden is True
    assert steps["wifi_ssid_set"].in_firmware is True, "it is in the command table"
    assert steps["wifi_psk_set"].hidden is False


def test_the_dry_run_flags_the_hidden_step() -> None:
    text = plan_wifi("My Home WiFi", "p").describe()
    assert "[HIDDEN FROM help, BUT PRESENT AND VERIFIED]" in text


def test_nothing_in_a_spaceless_plan_is_flagged_hidden() -> None:
    assert "HIDDEN" not in plan_wifi("ExampleAP", "p").describe()


def test_a_hidden_step_is_executed_rather_than_refused_after_a_refresh() -> None:
    """``help`` does not list ``wifi_ssid_set``, and it works anyway.

    Without the exemption this plan would raise ``RuntimeError`` from
    ``execute``'s missing-command check, or ``CommandNotInFirmware`` from
    ``_run``'s gate -- refusing a command the device answers.
    """
    s = sign(allow_destructive=True)
    s.refresh_commands()
    assert s.has("wifi_ssid_set") is False, "help really does omit it"
    results = plan_wifi("My Home WiFi", "secret").execute(s)
    assert [r.command for r in results] == [
        "wifi_psk_set secret",
        "wifi_security_set wpa2",
        "wifi_ssid_set My Home WiFi",
    ]


def test_a_psk_with_a_space_is_refused_for_the_read_back_reason() -> None:
    """Not "the parser cannot carry it" -- that reason is now known to be wrong.

    ``wifi_psk_set`` is the same shape as ``wifi_ssid_set``, which provably
    takes the rest of the line. The reason to refuse is that TCLV 67 has no
    read path, so nothing can be checked, and the failure is silent.
    """
    with pytest.raises(ValueError, match="whitespace") as excinfo:
        plan_wifi("ExampleAP", "pass word")
    message = str(excinfo.value)
    assert "no read path for TCLV 67" in message
    assert "whitespace-delimited" not in message, "that was the old, wrong reason"
    assert "Max conn errs" in message, "it says what a wrong guess looks like"


def test_the_psk_refusal_points_at_the_dns_alternative() -> None:
    with pytest.raises(ValueError, match="re-pointing the DNS name"):
        plan_wifi("ExampleAP", "pass word")


def test_bootstrap_carries_a_spaced_ssid_all_the_way_through() -> None:
    plan = plan_bootstrap("My Home WiFi", "secret", "host")
    assert plan.commands[:3] == (
        "wifi_psk_set secret",
        "wifi_security_set wpa2",
        "wifi_ssid_set My Home WiFi",
    )
    assert plan.unavailable == (), "every step is a command the table knows"


def test_bootstrap_still_refuses_a_spaced_psk_before_building_anything() -> None:
    with pytest.raises(ValueError, match="no read path for TCLV 67"):
        plan_bootstrap("ExampleAP", "pass word", "host")


def test_the_typed_setter_takes_the_same_route() -> None:
    """``Sign.set_wifi`` delegates to ``plan_wifi`` so the two cannot drift."""
    s = sign(allow_destructive=True)
    result = s.set_wifi("My Home WiFi", "secret")
    assert result.command == "wifi_ssid_set My Home WiFi", "the last step's result"
    assert [r.command for r in s.console.transcript] == [
        "wifi_psk_set secret",
        "wifi_security_set wpa2",
        "wifi_ssid_set My Home WiFi",
    ]


def test_the_typed_setter_still_sends_one_command_for_a_spaceless_ssid() -> None:
    s = sign(allow_destructive=True)
    s.set_wifi("ExampleAP", "secret")
    assert [r.command for r in s.console.transcript] == [
        "wifi_conf_set ExampleAP secret wpa2 0"
    ]


def test_the_typed_setter_refuses_a_spaced_psk_the_same_way() -> None:
    with pytest.raises(ValueError, match="no read path for TCLV 67"):
        sign(allow_destructive=True).set_wifi("ExampleAP", "pass word")


def test_the_spaced_route_writes_the_security_string_too() -> None:
    """``wifi_conf_set`` wrote TCLV 66 as argument 3; the split route needs its own."""
    assert plan_wifi("My Home WiFi", "p", security=WifiSecurity.WPA2_ENTERPRISE).commands == (
        "wifi_psk_set p",
        "wifi_security_set wpa2e",
        "wifi_ssid_set My Home WiFi",
    )


def test_the_typed_setter_now_validates_security() -> None:
    """New: it delegates to ``plan_wifi``, which has always checked this.

    ``set_wifi_security`` remains the unvalidated door, for a firmware that
    takes a value this library has not heard of.
    """
    with pytest.raises(ValueError, match="ASCII strings, not integers"):
        sign(allow_destructive=True).set_wifi("ExampleAP", "p", security="WPA3")


def test_the_typed_setter_still_needs_allow_destructive_for_the_hidden_route() -> None:
    """``check=False`` bypasses the firmware gate, not the danger tier."""
    with pytest.raises(DangerousCommandRefused):
        sign().set_wifi("My Home WiFi", "secret")


def test_a_plan_naming_a_missing_command_reports_it_rather_than_half_running() -> None:
    """The example is an invented name, on purpose.

    It used to be ``wifi_ssid_set``, which turned out to exist. Anything drawn
    from :data:`ABSENT_FROM_7_4_4407` could go the same way -- that set is
    "unlisted by ``help``", and ``help`` is not a complete index -- so this test
    uses a name no firmware will ever have and stops depending on the contents
    of that set at all.
    """
    bad = Plan(
        name="hypothetical",
        steps=(
            type(plan_repoint("h").steps[0])(
                "definitely_not_a_command Foo", "invented, so certainly missing"
            ),
        ),
    )
    assert bad.unavailable == ("definitely_not_a_command",)
    s = sign(allow_destructive=True)
    s.refresh_commands()
    with pytest.raises(RuntimeError, match="half-provisioned"):
        bad.execute(s)
    assert s.console._require().written == ["help\r"]  # type: ignore[attr-defined]


# -------------------------------------------------------------- the arguments


def test_wifi_security_values_are_strings() -> None:
    assert WifiSecurity.WPA2 == "wpa2"
    assert WifiSecurity.OPEN == "none"
    assert WifiSecurity.WPA2_ENTERPRISE == "wpa2e"
    assert WifiSecurity.ALL == ("none", "wpa2", "wpa2e")


def test_an_unknown_security_value_is_refused() -> None:
    with pytest.raises(ValueError, match="ASCII strings, not integers"):
        plan_wifi("s", "p", security="WPA2")


def test_psk_is_required_with_an_ssid() -> None:
    with pytest.raises(ValueError, match="psk is required"):
        plan_bootstrap("ExampleAP", None, "host")


# --------------------------------------------------------------- execution


def test_execute_sends_exactly_the_planned_commands() -> None:
    s = sign(allow_destructive=True)
    plan = plan_repoint("listener.example.com")
    plan.execute(s)
    assert [w.rstrip("\r") for w in s.console._require().written] == list(  # type: ignore[attr-defined]
        plan.commands
    )


def test_execute_refuses_on_a_read_only_sign_before_the_first_command() -> None:
    s = sign()
    with pytest.raises(DangerousCommandRefused):
        plan_repoint("h").execute(s)
    assert s.console._require().written == [], "nothing partial"  # type: ignore[attr-defined]


def test_execute_refuses_with_only_allow_writes() -> None:
    s = sign(allow_writes=True)
    with pytest.raises(DangerousCommandRefused, match="allow_destructive"):
        plan_repoint("h").execute(s)
    assert s.console._require().written == []  # type: ignore[attr-defined]


def test_execute_returns_one_result_per_step() -> None:
    s = sign(allow_destructive=True)
    plan = plan_bootstrap("ExampleAP", "s3cret", "h")
    results = plan.execute(s)
    assert len(results) == len(plan.steps)
    assert [r.command for r in results] == list(plan.commands)
