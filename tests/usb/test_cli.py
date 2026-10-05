"""The command line, on the paths that need no hardware.

The provisioning subcommand's default is the thing worth pinning: it prints the
plan and exits. A tool that writes to someone's sign because they forgot a flag
is the wrong shape, so "did nothing" has to be what happens when you type the
obvious thing.
"""

from __future__ import annotations

import pytest

from pyvisionect.io.usb.cli import main


def test_provision_is_a_dry_run_by_default(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["provision", "/dev/null", "--server", "listener.example.com"])
    out = capsys.readouterr().out
    assert code == 0
    assert "server_tcp_set listener.example.com 11113" in out
    assert "flash_save" in out
    assert "Nothing was sent" in out


def test_the_dry_run_explains_every_step(capsys: pytest.CaptureFixture[str]) -> None:
    main(["provision", "/dev/null", "--server", "h"])
    out = capsys.readouterr().out
    assert "TCLV 18" in out and "TCLV 53" in out
    assert "re-pointing that name" in out, "the cheaper alternative is offered"


def test_provision_refuses_a_spaced_ssid_without_a_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(["provision", "/dev/null", "--server", "h", "--ssid", "My AP", "--psk", "p"])
    assert code == 2
    assert "wifi_ssid_set" in capsys.readouterr().err


def test_provision_requires_a_psk_with_an_ssid() -> None:
    with pytest.raises(SystemExit):
        main(["provision", "/dev/null", "--server", "h", "--ssid", "AP"])


def test_commands_reports_the_delta(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["commands"]) == 0
    out = capsys.readouterr().out
    assert "111 commands in firmware 7.4.4407" in out
    assert "160 documented" in out
    assert "96 documented but absent" in out
    assert "47 present but undocumented" in out


def test_commands_flags_the_asserting_ones(capsys: pytest.CaptureFixture[str]) -> None:
    main(["commands"])
    out = capsys.readouterr().out
    assert "ASSERTS@spi_flash_cli.c:139" in out
    assert "ASSERTS@spi_flash_cli.c:188" in out


def test_encryption_prints_the_verdict_and_the_alternative(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["encryption"]) == 0
    out = capsys.readouterr().out
    assert "does not buy you self-hosted link encryption" in out
    assert "0 = Disabled" in out and "1 = Enabled" in out
    assert "TLS 1.3" in out
    assert "16 printable ASCII characters" in out


def test_a_subcommand_is_required() -> None:
    with pytest.raises(SystemExit):
        main([])
