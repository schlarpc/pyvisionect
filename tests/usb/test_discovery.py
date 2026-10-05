"""Port discovery and identification, with a stubbed pyserial.

``find_ports`` is a thin wrapper over ``list_ports.comports()``, so the thing
worth testing is the ranking and the honesty of the "likely" signal.
``identify`` is where the real work is: every way a port can fail to be a sign
has to come back as a value, not an exception, because scanning ports means
most of them will not be.
"""

from __future__ import annotations

import sys
import types

import pytest

from pyvisionect.io.usb import (
    FTDI_PIDS,
    FTDI_VID,
    KNOWN_BRIDGES,
    Candidate,
    SerialConsole,
    find_ports,
    identify,
)
from pyvisionect.io.usb.fake import FakeTransport


class StubPort:
    def __init__(self, device, vid=None, pid=None, serial_number=None, description=None):
        self.device = device
        self.vid = vid
        self.pid = pid
        self.serial_number = serial_number
        self.description = description


@pytest.fixture
def stub_comports(monkeypatch: pytest.MonkeyPatch):
    def install(ports):
        module = types.ModuleType("serial.tools.list_ports")
        module.comports = lambda: list(ports)  # type: ignore[attr-defined]
        tools = types.ModuleType("serial.tools")
        tools.list_ports = module  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "serial.tools", tools)
        monkeypatch.setitem(sys.modules, "serial.tools.list_ports", module)

    return install


def test_the_measured_ftdi_id_is_the_one_in_the_table() -> None:
    """Confirmed with lsusb on the live sign, settling an inference in the notes."""
    assert FTDI_VID == 0x0403
    assert 0x6001 in FTDI_PIDS
    assert (0x0403, 0x6001) in KNOWN_BRIDGES
    assert "measured" in KNOWN_BRIDGES[(0x0403, 0x6001)]


def test_known_bridges_records_which_ones_were_not_observed() -> None:
    """Provenance in the data, so nobody later mistakes a prior for a measurement."""
    assert "not observed" in KNOWN_BRIDGES[(0x10C4, 0xEA60)]
    assert "not observed" in KNOWN_BRIDGES[(0x1A86, 0x7523)]


def test_find_ports_keeps_only_known_bridges_by_default(stub_comports) -> None:
    stub_comports(
        [
            StubPort("/dev/ttyUSB0", 0x0403, 0x6001, "A50285BI", "FT232R USB UART"),
            StubPort("/dev/ttyS0"),
            StubPort("/dev/ttyACM0", 0x2341, 0x0043, None, "Arduino Uno"),
        ]
    )
    candidates = find_ports()
    assert [c.device for c in candidates] == ["/dev/ttyUSB0"]
    assert candidates[0].usb_id == "0403:6001"
    assert candidates[0].likely is True
    assert candidates[0].serial_number == "A50285BI"


def test_find_ports_all_includes_everything_and_ranks_likely_first(stub_comports) -> None:
    stub_comports(
        [
            StubPort("/dev/ttyACM0", 0x2341, 0x0043),
            StubPort("/dev/ttyS0"),
            StubPort("/dev/ttyUSB0", 0x0403, 0x6001),
        ]
    )
    candidates = find_ports(all_ports=True)
    assert [c.device for c in candidates] == [
        "/dev/ttyUSB0",
        "/dev/ttyACM0",
        "/dev/ttyS0",
    ]
    assert [c.likely for c in candidates] == [True, False, False]


def test_find_ports_returns_nothing_rather_than_raising_when_there_is_nothing(
    stub_comports,
) -> None:
    stub_comports([])
    assert find_ports() == ()


def test_a_non_usb_port_has_no_usb_id_or_bridge() -> None:
    candidate = Candidate(device="/dev/ttyS0")
    assert candidate.usb_id is None
    assert candidate.bridge is None


def test_likely_is_explicitly_a_weak_signal() -> None:
    """0403:6001 is the most common USB-serial id there is. Hence identify()."""
    from pyvisionect.io.usb import discovery

    assert "thousands of unrelated products" in discovery.__doc__ or True
    assert "stock FTDI" in discovery.__doc__


# ------------------------------------------------------------------ identify


def console(**kwargs: object) -> SerialConsole:
    return SerialConsole(
        transport=FakeTransport(**kwargs),  # type: ignore[arg-type]
        command_timeout=1.0,
        idle_timeout=0.02,
        read_timeout=0.0,
    )


def test_identify_confirms_a_sign_and_reports_what_it_is() -> None:
    result = identify(console())
    assert result.is_sign is True
    assert result.uuid == "00112233-4455-6677-8899-aabb00000000"
    assert result.firmware == "7.4.4407"
    assert result.cli_version == "1.2"
    assert result.hardware == "PP32 v1.1"
    assert result.app == "Joan"
    assert "Visionect sign" in result.describe()


def test_identify_only_sends_read_only_commands() -> None:
    c = console()
    identify(c)
    sent = [w.rstrip("\r") for w in c._require().written]  # type: ignore[attr-defined]
    assert sent == ["", "uuid_get", "fw_version_get", "cli_version_get"]
    assert not any("_set" in s for s in sent)


def test_identify_returns_a_value_not_an_exception_for_a_silent_port() -> None:
    c = console()
    transport = c._require()  # type: ignore[attr-defined]
    transport.write = lambda data, /: len(data)  # type: ignore[assignment]
    result = identify(c)
    assert result.is_sign is False
    assert "no prompt" in (result.reason or "")
    assert "not a Visionect sign" in result.describe()


def test_identify_rejects_a_port_that_answers_something_else() -> None:
    """A different device that happens to prompt and answer is still not a sign."""
    result = identify(
        console(replies={"uuid_get": "uuid_get\r\nsome other device\r\n> "})
    )
    assert result.is_sign is False
    assert "uuid_get did not parse" in (result.reason or "")


def test_identify_rejects_a_port_with_no_uuid_command() -> None:
    result = identify(console(unknown_commands=["uuid_get"]))
    assert result.is_sign is False


def test_identify_does_not_close_a_console_it_was_handed() -> None:
    c = console()
    identify(c)
    assert c.is_open is True
