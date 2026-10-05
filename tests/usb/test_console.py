"""The transport: framing, prompt sync, errors, and async-log separation.

The interleaving tests are the point of this file.  Everything else about the
console is mechanical; separating solicited output from the firmware's own log
chatter is the part that fails intermittently in the field if it is wrong, and
intermittent is the one failure mode a hand-written test will not catch by
accident.
"""

from __future__ import annotations

import re

import pytest

from pyvisionect.io.usb import (
    BAUD_RATE,
    PROMPT,
    TERMINATOR,
    CommandResult,
    ConsoleNotOpen,
    ConsoleTimeout,
    IncorrectParameters,
    LogLine,
    PromptNotFound,
    SerialConsole,
    UnknownCommand,
    classify,
)
from pyvisionect.io.usb.fake import CAPTURED, FakeTransport

# The real heartbeat burst, verbatim from a capture off /dev/ttyUSB0.
HEARTBEAT = (
    "sys evt vplatform_heartbeat.c:19, Heartbeat (7)",
    "Received event: Heartbeat (7)",
    "Heart-beat event",
    "sending pv2 status on heartbeat",
    "make packet type=3, id=4",
    "Frame send 552 bytes",
    "Setting heartbeat after 1 min",
    "Got response (id=4)",
)


def console(**kwargs: object) -> SerialConsole:
    return SerialConsole(
        transport=FakeTransport(**kwargs),  # type: ignore[arg-type]
        command_timeout=1.0,
        idle_timeout=0.02,
        read_timeout=0.0,
    )


# ------------------------------------------------------------ line discipline


def test_line_settings_are_the_measured_ones() -> None:
    assert BAUD_RATE == 115200
    assert PROMPT == b"> "


def test_terminator_is_cr_not_crlf() -> None:
    """Measured: LF alone does not submit, and a trailing LF pollutes the next read.

    The first release of this module sent ``\\r\\n``. The device does accept it --
    the CR submits -- but it echoes the LF back after the prompt, which lands in
    the *next* command's frame.
    """
    assert TERMINATOR == b"\r"


def test_lf_alone_does_not_submit_so_the_fake_holds_the_line() -> None:
    """The fake reproduces the real firmware's behaviour, so a wrong terminator fails.

    On the real device, ``cli_version_get\\n`` echoed and then sat in the edit
    buffer, and the following command was appended to it and rejected as one
    word.
    """
    transport = FakeTransport()
    transport.write(b"uptime\n")
    assert transport.commands == [], "LF must not submit"
    transport.write(b"\r")
    assert transport.commands == ["uptime"]


def test_command_is_written_with_a_bare_cr() -> None:
    c = console()
    c.command("uptime")
    assert c._require().written == ["uptime\r"]  # type: ignore[attr-defined]


def test_multiline_commands_are_refused_before_anything_is_written() -> None:
    c = console()
    with pytest.raises(ValueError, match="single line"):
        c.command("uptime\nuptime")
    assert c._require().written == []  # type: ignore[attr-defined]


def test_semicolon_is_refused_because_the_firmware_has_no_separator() -> None:
    """Measured: ``uptime; cli_version_get`` is rejected as one unrecognised name."""
    c = console()
    with pytest.raises(ValueError, match="no statement separator"):
        c.command("uptime; cli_version_get")


# ------------------------------------------------------------------- framing


def test_echo_is_stripped_and_prompt_is_not_a_reply_line() -> None:
    result = console().command("cli_version_get")
    assert result.lines == ("CLI version: 1.2",)
    assert result.echoed is True
    assert result.terminated_by == "prompt"
    assert result.raw.startswith("cli_version_get")
    assert result.raw.endswith("> ")


def test_rv_is_extracted_and_removed_from_the_lines() -> None:
    result = console().command("fs_ls")
    assert result.rv == 0
    assert not any("rv:" in line for line in result.lines)
    assert len(result.lines) == 6


def test_rv_parses_both_spellings_the_firmware_uses() -> None:
    """``fs_ls`` says ``rv: 0``; ``sf_list`` and ``wifi_bssid_get`` say ``rv: 0x0``."""
    c = console()
    assert c.command("fs_ls").rv == 0
    assert c.command("sf_list").rv == 0
    assert c.command("wifi_bssid_get").rv == 0


def test_rv_before_the_body_is_still_found() -> None:
    """``certs_config_get`` prints ``rv: 0`` *then* its body. Not a parser bug."""
    result = console().command("certs_config_get")
    assert result.rv == 0
    assert result.lines == ("No EAP cert found!",)


def test_most_getters_emit_no_rv_at_all() -> None:
    result = console().command("uptime")
    assert result.rv is None
    assert result.ok is True, "no rv means success, not failure"


def test_blank_lines_are_dropped_from_lines_but_kept_in_raw() -> None:
    """``uuid_get`` emits a blank line before its payload."""
    result = console().command("uuid_get")
    assert result.lines == (
        "UUID: 0x00 0x11 0x22 0x33 0x44 0x55 0x66 0x77 0x88 0x99 0xaa 0xbb "
        "0x00 0x00 0x00 0x00",
    )
    assert "\r\n\r\n" in result.raw


def test_field_and_fields_helpers() -> None:
    result = console().command("eth_conf_get")
    assert result.field("TRC") == "8"
    assert result.fields() == {
        "MAC": "02:00:5E:00:53:00",
        "TRC": "8",
        "TRT": "2000",
    }
    with pytest.raises(KeyError, match="NOPE"):
        result.field("NOPE")


def test_field_strips_the_firmwares_column_padding() -> None:
    """``feat_get`` pads to a column: ``"Feature EAP:   enabled"``."""
    assert console().command("feat_get").field("Feature EAP") == "enabled"


def test_one_refuses_to_guess_when_there_are_several_lines() -> None:
    c = console()
    assert c.command("cc3100_mac_address").one() == "00:00:5e:00:53:00"
    with pytest.raises(ValueError, match="expected 1"):
        c.command("fs_ls").one()


def test_help_is_read_to_the_prompt_not_to_a_fixed_sleep() -> None:
    """5 KB of output across many reads, terminated only by the prompt."""
    result = console().command("help")
    assert len(result.raw) > 4000
    assert result.terminated_by == "prompt"
    assert "wifi_conf_set" in result.text


# ------------------------------------------------------- asynchronous logs


@pytest.mark.parametrize("line", HEARTBEAT)
def test_every_real_heartbeat_line_is_classified_as_log(line: str) -> None:
    assert classify(line) is True


@pytest.mark.parametrize(
    "line",
    [
        "W WD task timeout: cli_USB",
        "Profiling:Total=6184,RxHole=0,Decrypt=0,Decomp=69,EpdUpd=5298",
        "Frame send 64 bytes",
    ],
)
def test_other_captured_log_lines_are_classified(line: str) -> None:
    assert classify(line) is True


@pytest.mark.parametrize(
    "line",
    [
        "CLI version: 1.2",
        "Uptime in min: 5",
        "SSID: ExampleAP",
        "Server IP/DNS: visionect.internal.example.com",
        "/image0.pv2 0 134409",
        "Vcom 0: 2400 mV",
        "total blocks: 2048, in use: 931",
        "CC3100 (6) active",
        "RSSI:-31 dBm",
        "Conn: tcp open",
        "STATUS_END",
        "No EAP cert found!",
    ],
)
def test_real_reply_lines_are_not_misclassified_as_logs(line: str) -> None:
    """A false positive here silently eats a reply line, which is the worst case."""
    assert classify(line) is False


def test_every_captured_reply_line_survives_classification() -> None:
    """The strongest form of the above: nothing in any real capture is eaten."""
    eaten: list[tuple[str, str]] = []
    for command, frame in CAPTURED.items():
        for line in frame.replace("\r\n", "\n").split("\n"):
            stripped = line.strip()
            if stripped and stripped != ">" and classify(stripped):
                eaten.append((command, stripped))
    assert eaten == []


def test_logs_arriving_before_a_command_are_drained_not_attributed() -> None:
    """Mechanism 1: bytes buffered before the write cannot be the reply.

    ``during=None`` records that: these lines were not attributed to any
    command's window, because they demonstrably predate it.
    """
    c = console()
    c._require()._out += (  # type: ignore[attr-defined]
        "".join(line + "\r\n" for line in HEARTBEAT).encode("ascii")
    )
    result = c.command("uptime")
    assert result.lines == ("Uptime in min: 5",)
    assert tuple(entry.text for entry in result.logs) == HEARTBEAT
    assert [entry.during for entry in result.logs] == [None] * len(HEARTBEAT)


def test_logs_spliced_into_the_middle_of_a_reply_are_separated() -> None:
    """Mechanism 4, and the case that corrupts a naive parser.

    The heartbeat burst lands *between* two VCOM lines. The reply must come back
    with all nine of its own lines and none of the eight log lines.
    """
    c = console(
        interleave=[("display_conf_get", 3, line) for line in HEARTBEAT]
    )
    result = c.command("display_conf_get")
    assert result.lines == (
        "Vcom 0: 2400 mV",
        "Vcom 1: 2400 mV",
        "Vcom 2: 2400 mV",
        "Vcom 3: 2400 mV",
        "Vcom 4: 0 mV",
        "Vcom 5: 0 mV",
        "Vcom 6: 0 mV",
        "Vcom 7: 0 mV",
        "Display type: 3255107880",
    )
    assert {entry.text for entry in result.logs} == set(HEARTBEAT)
    assert all(entry.during == "display_conf_get" for entry in result.logs)


def test_interleaving_does_not_corrupt_a_parsed_value() -> None:
    """The end-to-end version: a spliced log burst must not change the answer."""
    from pyvisionect.io.usb.parsers import parse_status_dump

    clean = parse_status_dump(console().command("status_get").lines)
    noisy = parse_status_dump(
        console(interleave=[("status_get", i, line) for i, line in enumerate(HEARTBEAT)])
        .command("status_get")
        .lines
    )
    assert clean.values == noisy.values
    assert noisy.rssi_dbm == -31
    assert noisy.bssid == "00:00:5e:00:53:00"


def test_a_log_line_before_the_echo_is_separated() -> None:
    """Mechanism 2: the echo anchors the start of the reply."""
    c = console(pre_echo=[("uptime", "Frame send 552 bytes")])
    result = c.command("uptime")
    assert result.lines == ("Uptime in min: 5",)
    assert [entry.text for entry in result.logs] == ["Frame send 552 bytes"]
    assert result.echoed is True, "the echo is still found, just not first"


def test_logs_reach_the_callback_and_the_history() -> None:
    seen: list[LogLine] = []
    c = SerialConsole(
        transport=FakeTransport(async_lines=[(0, HEARTBEAT[0])]),
        command_timeout=1.0,
        idle_timeout=0.02,
        read_timeout=0.0,
        on_log=seen.append,
    )
    c.command("uptime")
    assert [entry.text for entry in seen] == [HEARTBEAT[0]]
    assert [entry.text for entry in c.logs] == [HEARTBEAT[0]]


def test_a_raising_callback_does_not_break_the_command_in_flight() -> None:
    def explode(entry: LogLine) -> None:
        raise RuntimeError("the callback's problem, not the console's")

    c = SerialConsole(
        transport=FakeTransport(async_lines=[(0, HEARTBEAT[0])]),
        command_timeout=1.0,
        idle_timeout=0.02,
        read_timeout=0.0,
        on_log=explode,
    )
    assert c.command("uptime").lines == ("Uptime in min: 5",)


def test_extra_log_patterns_can_be_supplied() -> None:
    pattern = re.compile(r"^some new firmware chatter")
    c = SerialConsole(
        transport=FakeTransport(
            interleave=[("uptime", 0, "some new firmware chatter here")]
        ),
        command_timeout=1.0,
        idle_timeout=0.02,
        read_timeout=0.0,
        log_patterns=(pattern,),
    )
    result = c.command("uptime")
    assert result.lines == ("Uptime in min: 5",)
    assert [entry.text for entry in result.logs] == ["some new firmware chatter here"]


def test_a_log_line_split_across_two_drains_is_reported_once() -> None:
    """The holdover: a partial trailing line is not a log line yet."""
    c = console()
    transport = c._require()  # type: ignore[attr-defined]
    transport._out += b"Frame send 55"
    c.drain_logs()
    assert list(c.logs) == []
    transport._out += b"2 bytes\r\n"
    c.drain_logs()
    assert [entry.text for entry in c.logs] == ["Frame send 552 bytes"]


# -------------------------------------------------------------------- errors


def test_unknown_command_raises_with_the_firmwares_own_wording() -> None:
    c = console()
    with pytest.raises(UnknownCommand, match="definitely_not_a_command"):
        c.command("definitely_not_a_command")


def test_unknown_command_can_be_returned_instead_of_raised() -> None:
    result = console().command("definitely_not_a_command", raise_on_error=False)
    assert "not recognised" in result.lines[0]


def test_exists_probes_without_raising() -> None:
    c = console()
    assert c.exists("uptime") is True
    assert c.exists("flash_print") is False, "absent from firmware 7.4.4407"


def test_incorrect_parameters_raises() -> None:
    """``max17135_dump`` is documented nullary and rejects a bare call."""
    c = console()
    with pytest.raises(IncorrectParameters, match="max17135_dump"):
        c.command("max17135_dump")


def test_incorrect_parameters_message_points_at_the_wrong_help_line() -> None:
    c = console()
    with pytest.raises(IncorrectParameters, match="max17135_dump is documented as nullary"):
        c.command("max17135_dump")


def test_timeout_carries_the_partial_output() -> None:
    c = console(replies={"uptime": "uptime\r\nUptime in min: 5\r\n"}, prompt="")
    with pytest.raises(ConsoleTimeout) as caught:
        c.command("uptime", timeout=0.1)
    assert "Uptime in min: 5" in caught.value.partial
    assert "assert:" in str(caught.value), "the message must mention the CLI-death case"


def test_a_firmware_assertion_raises_the_specific_error() -> None:
    """The real ``sf_rdid`` frame: an assert line and then silence, forever.

    Captured verbatim. Distinguishing this from an ordinary timeout matters
    because the remedy is completely different -- nothing you send will help,
    the device has to reboot.
    """
    from pyvisionect.io.usb import ConsoleAssertionFailed

    c = console(
        replies={"sf_rdid": "sf_rdid\r\nassert: spi_flash_cli.c:139\r\n"}, prompt=""
    )
    with pytest.raises(ConsoleAssertionFailed) as caught:
        c.command("sf_rdid", timeout=0.1)
    assert caught.value.location == "spi_flash_cli.c:139"
    assert "reboot" in str(caught.value)


def test_missing_echo_keeps_the_reply_rather_than_discarding_it() -> None:
    c = console(echo=False)
    result = c.command("uptime")
    assert result.echoed is False
    assert result.lines == ("Uptime in min: 5",)


# ------------------------------------------------------------------ lifecycle


def test_sync_finds_the_prompt_and_clears_the_buffer() -> None:
    """Whatever was pending becomes log, and the next command starts clean."""
    c = console()
    c._require()._out += (HEARTBEAT[0] + "\r\n").encode("ascii")  # type: ignore[attr-defined]
    c.sync()
    assert [entry.text for entry in c.logs] == [HEARTBEAT[0]]
    assert c.command("uptime").logs == ()


def test_sync_raises_a_useful_error_on_a_dead_console() -> None:
    c = console(replies={})
    c._require()._out.clear()  # type: ignore[attr-defined]
    transport = c._require()  # type: ignore[attr-defined]

    def swallow(data: bytes, /) -> int:
        return len(data)

    transport.write = swallow  # type: ignore[assignment]
    with pytest.raises(PromptNotFound, match="usb_cli_task"):
        c.sync(attempts=1)


def test_context_manager_closes_an_owned_transport() -> None:
    transport = FakeTransport()
    with SerialConsole(transport=transport, read_timeout=0.0):
        pass
    assert transport.closed is False, "an injected transport is not ours to close"


def test_close_then_command_raises() -> None:
    c = console()
    c.close()
    with pytest.raises(ConsoleNotOpen):
        c.command("uptime")


def test_port_or_transport_is_required() -> None:
    with pytest.raises(ValueError, match="port or transport"):
        SerialConsole()


def test_transcript_records_every_exchange() -> None:
    c = console()
    c.command("uptime")
    c.command("cli_version_get")
    assert [r.command for r in c.transcript] == ["uptime", "cli_version_get"]
    assert all(isinstance(r, CommandResult) for r in c.transcript)


def test_commands_are_case_insensitive_on_the_device() -> None:
    """Measured: ``UPTIME`` works. The fake lowercases its lookup to match."""
    assert console().command("UPTIME").lines == ("Uptime in min: 5",)
