"""The TCLV parameter table."""

from __future__ import annotations

import pytest

from pyvisionect.devices import tclv
from pyvisionect.wire import ReadOnlyParameter


def test_the_table_has_the_vendor_s_235_entries() -> None:
    assert len(tclv.TCLV) == 235


def test_bank_layout() -> None:
    mcu = [i for i in tclv.TCLV if i < 1000]
    scpu = [i for i in tclv.TCLV if 1000 <= i < 2000]
    dpu = [i for i in tclv.TCLV if i >= 2000]
    assert len(mcu) == 178  # 0..178 with id 94 genuinely absent
    assert 94 not in tclv.TCLV
    assert len(scpu) == 27  # 1000..1022 plus 1300..1303
    assert len(dpu) == 30   # 2000..2025 plus 2300..2303
    assert tclv.TCLV[0].bank == "mcu"
    assert tclv.TCLV[1002].bank == "scpu"
    assert tclv.TCLV[2002].bank == "dpu"


def test_known_names_are_verbatim() -> None:
    assert tclv.TCLV[0].name == "TCLV Magic number"
    assert tclv.TCLV[29].name == "Heart beat interval"
    assert tclv.TCLV[53].name == "Command to save parameters to flash"
    assert tclv.TCLV[65].name == "WiFi SSID"
    assert tclv.TCLV[145].name == "TLS mode: 0=disabled, 1=TLS 1.3"
    assert tclv.name_of(9999) == "unknown(9999)"


def test_network_read_only_set_is_exactly_the_eight_bootstrap_ids() -> None:
    assert tclv.NETWORK_READ_ONLY == frozenset({2, 18, 19, 65, 66, 67, 68, 70})
    for param_id in tclv.NETWORK_READ_ONLY:
        assert not tclv.TCLV[param_id].network_writable


def test_everything_else_is_writable() -> None:
    writable = [i for i, e in tclv.TCLV.items() if e.network_writable]
    assert len(writable) == 235 - 8


@pytest.mark.parametrize("param_id", sorted(tclv.NETWORK_READ_ONLY))
def test_check_writable_names_the_usb_command(param_id: int) -> None:
    with pytest.raises(ReadOnlyParameter) as excinfo:
        tclv.check_writable(param_id)
    error = excinfo.value
    assert error.param_id == param_id
    assert error.name == tclv.TCLV[param_id].name
    assert error.usb_hint == tclv.USB_HINTS[param_id]
    assert "flash_save" in str(error)
    assert "reboot" in str(error)


def test_check_writable_passes_a_writable_id() -> None:
    tclv.check_writable(29)


def test_usb_hints_cover_the_whole_read_only_set() -> None:
    assert set(tclv.USB_HINTS) == set(tclv.NETWORK_READ_ONLY)


def test_well_known_ids() -> None:
    assert tclv.ID_HEARTBEAT == 29
    assert tclv.ID_IPV4_SERVER_IP == 18
    assert tclv.ID_IPV4_SERVER_PORT == 19
    assert tclv.ID_CMD_FLASH_SAVE == 53
    assert tclv.ID_WIFI_SSID == 65
