"""USB provisioning: against a fake serial port, so no hardware is needed."""

from __future__ import annotations

import pytest

from pyvisionect.io.usb import (
    BAUD_RATE,
    DESTRUCTIVE_COMMANDS,
    DangerousCommandRefused,
    UsbProvisioner,
    WifiSecurity,
)


class FakeSerial:
    """Minimal pyserial stand-in: echoes a canned reply per command."""

    def __init__(self, replies: dict[str, str] | None = None) -> None:
        self.written: list[str] = []
        self.replies = replies or {}
        self._pending = b""
        self.closed = False

    @property
    def in_waiting(self) -> int:
        return len(self._pending)

    def write(self, data: bytes) -> int:
        text = data.decode("ascii")
        self.written.append(text)
        command = text.strip()
        self._pending = (self.replies.get(command, "OK") + "\r\n").encode("ascii")
        return len(data)

    def flush(self) -> None:
        pass

    def read(self, size: int = 1) -> bytes:
        out, self._pending = self._pending[:size], self._pending[size:]
        return out

    def close(self) -> None:
        self.closed = True


def make_cli(**kwargs) -> tuple[UsbProvisioner, FakeSerial]:
    fake = FakeSerial(kwargs.pop("replies", None))
    cli = UsbProvisioner(serial=fake, quiet_time=0.0, timeout=0.05, **kwargs)
    return cli, fake


def test_line_settings_are_the_documented_ones() -> None:
    assert BAUD_RATE == 115200


def test_terminator_is_configurable_because_it_is_undocumented() -> None:
    cli, fake = make_cli()
    assert cli.terminator == "\r\n"
    cli.terminator = "\r"
    cli.send("uuid_get")
    assert fake.written == ["uuid_get\r"]


def test_bootstrap_sequence_is_the_vendor_s() -> None:
    cli, fake = make_cli()
    cli.repoint("MySSID", "s3cret", "homeassistant.local", 11113)
    assert [line.strip() for line in fake.written] == [
        "wifi_conf_set MySSID s3cret wpa2 0",
        "server_tcp_set homeassistant.local 11113",
        "flash_save",
        "reboot",
    ]


def test_flash_save_is_not_optional_in_the_sequence() -> None:
    cli, fake = make_cli()
    cli.repoint(None, None, "10.0.0.5")
    commands = [line.strip() for line in fake.written]
    assert commands == ["server_tcp_set 10.0.0.5 11113", "flash_save", "reboot"]
    assert "flash_save" in commands, "settings are RAM-only without it"


def test_repoint_can_leave_wifi_alone() -> None:
    cli, fake = make_cli()
    cli.repoint(None, None, "homeassistant.local")
    assert not any("wifi" in line for line in fake.written)


def test_no_reboot_option() -> None:
    cli, fake = make_cli()
    cli.repoint(None, None, "h", reboot=False)
    assert not any("reboot" in line for line in fake.written)


def test_ssid_with_spaces_uses_the_single_value_setters() -> None:
    cli, fake = make_cli()
    cli.repoint("My Home WiFi", "pass word", "h")
    commands = [line.strip() for line in fake.written]
    assert commands[0] == "wifi_ssid_set My Home WiFi"
    assert commands[1] == "wifi_psk_set pass word"
    assert commands[2] == "wifi_security_set wpa2"


def test_wifi_conf_set_refuses_whitespace_rather_than_truncating() -> None:
    cli, _ = make_cli()
    with pytest.raises(ValueError, match="whitespace"):
        cli.wifi_conf_set("My SSID", "psk")
    with pytest.raises(ValueError, match="whitespace"):
        cli.wifi_conf_set("SSID", "p sk")


def test_psk_required_with_ssid() -> None:
    cli, _ = make_cli()
    with pytest.raises(ValueError, match="psk is required"):
        cli.repoint("SSID", None, "host")


@pytest.mark.parametrize("command", sorted(DESTRUCTIVE_COMMANDS))
def test_destructive_commands_are_refused_by_default(command: str) -> None:
    cli, fake = make_cli()
    with pytest.raises(DangerousCommandRefused, match=command):
        cli.send(command)
    assert fake.written == [], "nothing may reach the device"


def test_destructive_commands_need_an_explicit_opt_in() -> None:
    cli, fake = make_cli(allow_destructive=True)
    cli.send("cc3100_format")
    assert fake.written == ["cc3100_format\r\n"]


def test_the_refused_set_covers_the_documented_dangerous_commands() -> None:
    for command in ("cc3100_format", "cc3100_fw_upgrade", "scpu_upgrade",
                    "touch_fw_update", "feat_disable"):
        assert command in DESTRUCTIVE_COMMANDS


def test_shipping_mode_is_refused_by_default() -> None:
    cli, fake = make_cli()
    with pytest.raises(DangerousCommandRefused, match="shipping mode"):
        cli.system_conf_set(1, 1, ship=1)
    assert fake.written == []
    cli.system_conf_set(1, 1, ship=0)
    assert fake.written == ["system_conf_set 1 1 0\r\n"]


def test_shipping_mode_with_the_opt_in() -> None:
    cli, fake = make_cli(allow_destructive=True)
    cli.system_conf_set(1, 1, ship=1)
    assert fake.written == ["system_conf_set 1 1 1\r\n"]


def test_read_only_helpers() -> None:
    cli, fake = make_cli(
        replies={
            "uuid_get": "00112233-4455-6677-8899-aabb00000000",
            "server_tcp_get": "visionect.internal.example.com:11113",
        }
    )
    assert "00112233" in cli.uuid_get()
    assert "11113" in cli.server_tcp_get()
    assert [line.strip() for line in fake.written] == ["uuid_get", "server_tcp_get"]


def test_transcript_is_recorded() -> None:
    cli, _ = make_cli(replies={"help": "a lot of commands"})
    cli.help()
    assert cli.transcript[0][0] == "help"
    assert "commands" in cli.transcript[0][1]


def test_wifi_security_values_are_strings_not_integers() -> None:
    assert WifiSecurity.WPA2 == "wpa2"
    assert WifiSecurity.OPEN == "none"
    assert WifiSecurity.WPA2_ENTERPRISE == "wpa2e"


def test_context_manager_closes_the_port() -> None:
    fake = FakeSerial()
    with UsbProvisioner(serial=fake, quiet_time=0.0, timeout=0.05):
        pass
    assert fake.closed


def test_port_or_serial_required() -> None:
    with pytest.raises(ValueError, match="port or serial"):
        UsbProvisioner()


def test_pss_forces_a_status_packet() -> None:
    cli, fake = make_cli()
    cli.send_status_packet()
    assert fake.written == ["pss\r\n"]


def test_cli_main_requires_a_server_unless_dumping() -> None:
    from pyvisionect.io.usb import main

    with pytest.raises(SystemExit):
        main(["/dev/null"])
