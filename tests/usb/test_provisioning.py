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


# ---------------------------------------------- what this firmware cannot do


def test_an_ssid_with_a_space_is_refused_with_the_reason() -> None:
    """``wifi_ssid_set`` -- the documented workaround -- is not in this firmware."""
    with pytest.raises(ValueError, match="wifi_ssid_set"):
        plan_wifi("My Home WiFi", "psk")


def test_a_psk_with_a_space_is_refused_too() -> None:
    with pytest.raises(ValueError, match="whitespace"):
        plan_wifi("ExampleAP", "pass word")


def test_the_refusal_points_at_the_dns_alternative() -> None:
    with pytest.raises(ValueError, match="re-pointing the DNS name"):
        plan_wifi("My SSID", "psk")


def test_bootstrap_refuses_a_spaced_ssid_before_building_anything() -> None:
    with pytest.raises(ValueError, match="whitespace"):
        plan_bootstrap("My SSID", "psk", "host")


def test_the_typed_setter_refuses_the_same_way() -> None:
    with pytest.raises(ValueError, match="firmware 7.4.4407 does not have"):
        sign(allow_destructive=True).set_wifi("My SSID", "psk")


def test_a_plan_naming_a_missing_command_reports_it_rather_than_half_running() -> None:
    bad = Plan(
        name="hypothetical",
        steps=(
            type(plan_repoint("h").steps[0])("wifi_ssid_set Foo", "documented, absent"),
        ),
    )
    assert bad.unavailable == ("wifi_ssid_set",)
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
