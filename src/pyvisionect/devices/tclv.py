"""The TCLV parameter table -- 235 entries, shipped as **data**.

Source: the v1 package's ``proto.paramTypes``
(``map[uint16]proto.paramType``, global @ ``0x26f5760``, built by
``vss/proto.map.init.3`` from ``tclv-table.go``, ``makemap(235)``).  The vendor
covers eleven hardware families with one code path by deriving nothing from the
model, so this is a lookup table and not a class hierarchy.

Three id banks:

* ``0..178``      main MCU  (178 entries -- id 94 genuinely does not exist)
* ``1000..1022``  SCPU, the sensor / front-light co-processor
* ``1300..1303``  SCPU control commands
* ``2000..2025``  DPU, the display power unit
* ``2300..2303``  DPU control commands

The **same numeric id space** is used by the device's USB ASCII CLI -- its ~160
commands map 1:1 onto these ids.  Only the *encoding* differs: USB speaks
``<command> <args>`` text, the network speaks ``packet.ParamPayload`` TLVs.

``network_writable``
--------------------

Eight ids are marked ``canWrite: false`` in the server's own admin-UI descriptor
table (``vss/data/admin/all.js``): the server can **read** them over the network
but will never write them.  They are exactly the network/bootstrap
configuration, which is why USB provisioning is unavoidable -- you cannot tell a
sign about a new WiFi network over the network it does not have yet.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "TclvEntry",
    "TCLV",
    "NETWORK_READ_ONLY",
    "USB_HINTS",
    "BANK_MCU",
    "BANK_SCPU",
    "BANK_DPU",
    "lookup",
    "name_of",
    "check_writable",
]

BANK_MCU = range(0, 179)
BANK_SCPU = range(1000, 1304)
BANK_DPU = range(2000, 2304)


@dataclass(frozen=True, slots=True)
class TclvEntry:
    """One row of the vendor's TCLV table."""

    id: int
    name: str
    """Verbatim from the binary's string table."""

    network_writable: bool
    """False for the eight ids the admin UI marks ``canWrite: false``."""

    v1_set_handler: str | None = None
    """Name of the v1 ``valueSet`` function, or None. Tells you the value's text form."""

    v1_get_handler: str | None = None
    """Name of the v1 ``valueHandler`` function, or None."""

    @property
    def bank(self) -> str:
        if self.id in BANK_MCU:
            return "mcu"
        if self.id in BANK_SCPU:
            return "scpu"
        if self.id in BANK_DPU:
            return "dpu"
        return "unknown"


NETWORK_READ_ONLY: frozenset[int] = frozenset({2, 18, 19, 65, 66, 67, 68, 70})
"""``canWrite: false`` in ``vss/data/admin/all.js``: CONN_TYPE, IPV4_SERVER_IP,
IPV4_SERVER_PORT, WIFI_SSID, WIFI_SECURITY, WIFI_PSK, WIFI_BAND, MOB_SECURITY."""

USB_HINTS: dict[int, str] = {
    2: "conn_type_set <type>",
    18: "server_tcp_set <ip|dns> <port>",
    19: "server_tcp_set <ip|dns> <port>",
    65: "wifi_ssid_set <ssid>   (or wifi_conf_set <ssid> <psk> <security> <band>)",
    66: "wifi_security_set <none|wpa2|wpa2e>   (or wifi_conf_set arg 3)",
    67: "wifi_psk_set <psk>   (or wifi_conf_set arg 2)",
    68: "wifi_conf_set <ssid> <psk> <security> <band>   (arg 4)",
    70: "mobile_conf_set <apn> <security> <user> <psk> <band>   (arg 2)",
}
"""The USB CLI command that *can* set each network-read-only id.

From the vendor CLI reference (docs.visionect.com PlaceAndPlay/DeviceConfiguration/CLI).
"""

# Well-known ids worth naming in code rather than spelling as integers.
ID_TCLV_MAGIC = 0
ID_TCLV_VERSION = 1
ID_CONN_TYPE = 2
ID_IPV4_SERVER_IP = 18
ID_IPV4_SERVER_PORT = 19
ID_DISPLAY_TYPE = 28
ID_HEARTBEAT = 29
ID_CMD_FLASH_SAVE = 53
ID_WIFI_SSID = 65
ID_WIFI_SECURITY = 66
ID_WIFI_PSK = 67
ID_WIFI_BAND = 68
ID_SLEEP_MODE = 52
ID_GTIN = 108
ID_DEVICE_UUID = 89
ID_TLS_MODE = 145
ID_DISPLAY_WIDTH = 152
ID_DISPLAY_HEIGHT = 153

_ROWS: tuple[tuple[int, str, str | None, str | None], ...] = (
    (0, 'TCLV Magic number', None, None),
    (1, 'TCLV table version', None, None),
    (2, 'Connectivity type', None, None),
    (3, 'Wifi ACK or NACK timeout and Waiting for next block timeout', None, None),
    (4, 'Wifi power saving timeout', None, None),
    (5, 'WiFi DTIM skip interval in msec', None, None),
    (6, 'Mobile ACK or NACK timeout and Waiting for next block timeout', None, None),
    (7, 'Mobile power saving timeout', None, None),
    (8, 'Ethernet MAC address', None, None),
    (9, 'Ethernet TCP retry count', None, None),
    (10, 'Ethernet TCP retry timeout [100ms]', None, None),
    (11, 'Ethernet ACK or NACK timeout and Waiting for next block timeout', None, None),
    (12, 'Ethernet power saving timeout', None, None),
    (13, 'IPv4 static IP', None, None),
    (14, 'IPv4 static Netmask', None, None),
    (15, 'IPv4 static Gateway', None, None),
    (16, 'IPv4 static DNS server', None, None),
    (17, 'IPv4 mode: 0=Static IP, 1=DHCP', None, None),
    (18, 'Server IP', None, None),
    (19, 'Server port', 'u16ValueSet', None),
    (20, 'VCOM of Display 0', 'u16ValueSet', None),
    (21, 'VCOM of Display 1', 'u16ValueSet', None),
    (22, 'VCOM of Display 2', 'u16ValueSet', None),
    (23, 'VCOM of Display 3', 'u16ValueSet', None),
    (24, 'VCOM of Display 4', 'u16ValueSet', None),
    (25, 'VCOM of Display 5', 'u16ValueSet', None),
    (26, 'VCOM of Display 6', 'u16ValueSet', None),
    (27, 'VCOM of Display 7', 'u16ValueSet', None),
    (28, 'Display Type', None, None),
    (29, 'Heart beat interval', None, None),
    (30, 'Network error retry interval', None, None),
    (31, 'Proximity: offset calibration parameter', None, None),
    (32, 'Proximity: threshold in %', None, None),
    (33, 'Proximity: diode current in 10mA', None, None),
    (34, 'Accelerometer threshold count', None, None),
    (35, 'Accelerometer debounce count', None, None),
    (36, 'Battery OFF threshold', 'u16ValueSet', None),
    (37, 'Battery ON threshold', 'u16ValueSet', None),
    (38, 'Battery threshold count', None, None),
    (39, 'Front light map 0: PWM+LDR', 'tclvHandlerFlightSet', 'tclvHandlerFlightGet'),
    (40, 'Front light map 1: PWM+LDR', 'tclvHandlerFlightSet', 'tclvHandlerFlightGet'),
    (41, 'Front light map 2: PWM+LDR', 'tclvHandlerFlightSet', 'tclvHandlerFlightGet'),
    (42, 'Front light map 3: PWM+LDR', 'tclvHandlerFlightSet', 'tclvHandlerFlightGet'),
    (43, 'Front light map 4: PWM+LDR', 'tclvHandlerFlightSet', 'tclvHandlerFlightGet'),
    (44, 'Front light map 5: PWM+LDR', 'tclvHandlerFlightSet', 'tclvHandlerFlightGet'),
    (45, 'Front light map 6: PWM+LDR', 'tclvHandlerFlightSet', 'tclvHandlerFlightGet'),
    (46, 'Front light map 7: PWM+LDR', 'tclvHandlerFlightSet', 'tclvHandlerFlightGet'),
    (47, 'Front light threshold', None, None),
    (48, 'Front light server control', None, None),
    (49, 'System screens: 1=Battery, 2=Not connected, 3=Battery+Not connected', None, None),
    (50, 'Touch mode: 0=OFF, 1=ON, 3=ON+Beep', None, None),
    (51, 'Shipping mode: 0=OFF, 1=ON', None, None),
    (52, 'Sleep mode disable: 0=Sleep mode enabled, 1=Sleep mode disabled', None, None),
    (53, 'Command to save parameters to flash', None, None),
    (54, 'Roaming mode: 0=Roaming disabled, 1=Roaming enabled', None, None),
    (55, 'Roaming threshold in dBm', None, None),
    (56, 'Roaming hysteresis in dBm', None, None),
    (57, 'Writes EAP certificate chunk', None, None),
    (58, 'Writes EAP certificate descriptor', None, None),
    (59, 'Erases EAP certificate', None, None),
    (60, 'Certificate entry 0', 'tclvStringValue', None),
    (61, 'Certificate entry 1', 'tclvStringValue', None),
    (62, 'Certificate entry 2', 'tclvStringValue', None),
    (63, 'Certificate entry 3', 'tclvStringValue', None),
    (64, 'Certificate entry 4', 'tclvStringValue', None),
    (65, 'WiFi SSID', 'tclvStringValue', None),
    (66, 'WiFi security mode', 'tclvStringValue', None),
    (67, 'WiFi password', None, None),
    (68, 'WiFi band: 0=dual, 1=2.4GHz, 2=5GHz', None, None),
    (69, 'Mobile APN string', None, None),
    (70, 'Mobile security mode', 'tclvStringValue', None),
    (71, 'Mobile username', 'tclvStringValue', None),
    (72, 'Mobile password', None, None),
    (73, 'Mobile: not used. Set to 0', None, None),
    (74, 'WiFi EAP method', 'tclvStringValue', None),
    (75, 'Set WiFi EAP password', 'tclvStringValue', None),
    (76, 'Set WiFi EAP username', 'tclvStringValue', None),
    (77, 'Enable feature', None, None),
    (78, 'Disable feature', None, None),
    (79, 'Write: Play RTTTL song [text]. Read: play status: 0=Idle, 1=Playing', None, None),
    (80, 'Force connection establish. Read requested connection status', None, None),
    (81, 'Read connection status', None, None),
    (82, 'Application name', None, None),
    (83, 'Connectivity support: bit0=Wifi, bit1=Ethernet, bit2=Mobile', None, None),
    (84, 'Connectivity drivers: see CLI command conn_type_list', None, None),
    (85, 'Disable connectivity for N minutes', None, None),
    (86, 'HW version: ID, Major, Minor, BOM', None, None),
    (87, 'Application version: Major, Minor, Revision', None, None),
    (88, 'Bootloader version: Major, Minor, Revision', None, None),
    (89, 'Device UUID', None, None),
    (90, 'Application ready status: 0:Not ready, 1:Ready', None, None),
    (91, 'Reboot device, optional argument: 1 = upgrade, 0 = boot', None, None),
    (92, 'Jump to application', None, None),
    (93, 'Connectivity FW version', None, None),
    (95, 'Touch FW version', None, None),
    (96, 'If 1, device successfully sent status packet to server, 0 otherwise', None, None),
    (97, 'Frontlight LDR filter coefficient', None, None),
    (98, 'Frontlight PWM filter coefficient', None, None),
    (99, 'Front light sensor timeout', None, None),
    (100, 'Heater low temperature limit', None, None),
    (101, 'Heater high temperature limit', None, None),
    (102, 'Heater operation mode', None, None),
    (103, 'Extension battery threshold. Extension powered off if battery voltage below this', None, None),
    (104, 'Extension operation minimal temperature', None, None),
    (105, 'Extension operation maximal temperature', None, None),
    (106, 'Extension operation mode', None, None),
    (107, 'Used (compiled) display type', None, None),
    (108, 'GTIN', 'stringValueSet', None),
    (109, 'EPD border mode: 0=server, 1=white, 2=black', None, None),
    (110, 'WiFi MAC', None, None),
    (111, 'T2S enable', None, None),
    (112, 'T2S speech voice', 'u8ValueSet', None),
    (113, 'T2S speech rate', 'u16ValueSet', None),
    (114, 'T2S volume', 'u8ValueSet', None),
    (115, 'T2S timeout', 'u8ValueSet', None),
    (116, 'T2S speak', None, None),
    (117, 'External battery used for battery screens (0 = disabled, 1 = enabled', None, None),
    (118, 'EPD temperature limit mode', None, None),
    (119, 'Temp high', 'u16ValueSet', None),
    (120, 'Temp low', 'u16ValueSet', None),
    (121, 'Displays preloaded local image', None, None),
    (122, 'SNTP server address', None, None),
    (123, 'SNTP server port', None, None),
    (124, 'HTP server URL', None, None),
    (125, 'HTP server port', None, None),
    (126, 'Sets CLI password', None, None),
    (127, 'Resets CLI password and all security related settings', None, None),
    (128, 'CLI login', None, None),
    (129, 'CLI security state: 1:unlocked 0:locked', None, None),
    (130, 'Outbound encryption: 0=Disabled, 1=Enabled', None, None),
    (131, 'Encryption key', None, None),
    (132, 'Mobile connection IMEI', None, None),
    (133, 'Executes application upgrade', None, None),
    (134, 'T2S button 1 text', 'string128ValueSet', None),
    (135, 'T2S button 2 text', 'string128ValueSet', None),
    (136, 'T2S button 3 text', 'string128ValueSet', None),
    (137, 'T2S button 4 text', 'string128ValueSet', None),
    (138, 'BLE MAC', None, None),
    (139, 'Performs WiFi scan', None, None),
    (140, 'BLE advertising data', None, None),
    (141, 'MPPT: VFB voltage [mV]', None, None),
    (142, 'MPPT: MPPT voltage [mV]', None, None),
    (143, 'Executes WiFi module upgrade', None, None),
    (144, 'Set WiFi region: 0=Default, 1=US, 2=EUROPE, 3=JAPAN', None, None),
    (145, 'TLS mode: 0=disabled, 1=TLS 1.3', None, None),
    (146, 'EPD count', None, None),
    (147, 'Panel orientation: 0=Portrait, 1=Landscape', None, None),
    (148, 'Panel alignment: 0=Vertical, 1=Horizontal', None, None),
    (149, 'Panel rotation: 0=No rotation, 1=180deg rotation', None, None),
    (150, 'Panel update order: 0=Ascending, 1=Descending', None, None),
    (151, 'Restart JS engine', None, None),
    (152, 'Display width in pixels', None, None),
    (153, 'Display height in pixels', None, None),
    (154, 'Mobile preferred mode: 0=Disabled, 2=Auto, 38=LTE only,...', None, None),
    (155, 'BLE mode: 0=Disabled, 1=Enabled', None, None),
    (156, 'EPD temperature', None, None),
    (157, 'Format the file system', None, None),
    (158, 'Reads the 16-byte wall-mount ID', None, None),
    (159, 'Sets the PoE wall-mount LEDs', None, None),
    (160, 'JS engine status', None, None),
    (161, 'Flushes the syslog to the file system', None, None),
    (162, 'Halt JS app execution', None, None),
    (163, 'Sends device to sleep mode.', None, None),
    (164, 'JSE eval', 'stringValueSet', None),
    (165, 'Reads Wall-mount HW version: ID, Major, Minor, BOM', None, None),
    (166, 'Changes the server URL and/or port: [Server URL]:[Server port]', 'stringValueSet', None),
    (167, 'Connectivity link type: 0=None, 1=WiFi, 2=Mobile, 3=Ethernet', None, None),
    (168, 'Executes JS application patch upgrade', None, None),
    (169, 'Cleans the file system of any previous JS upgrades', None, None),
    (170, 'Read BSSID of the currently connected AP', 'tclvStringValue', None),
    (171, 'Set M2M mode for N minutes (0 to disable) or read remaining M2M time in minutes', None, None),
    (172, 'Sets battery charging mode: 0 = Normal (default), 1 = Charging disabled', None, None),
    (173, 'Set WiFi mode: 0=Default, 1=Disable broadcast filtering 2=enable Enhanced max PSP mode, 3=Disable broadcast+enable Enhanced max PSP', None, None),
    (174, 'Opens a file for reading [filename]:[r] or writing [filename]:[w]', 'stringValueSet', None),
    (175, 'Reads/writes file data', 'stringValueSet', 'tclvStringValue'),
    (176, 'Closes an opened file', None, None),
    (177, 'Returns a S-box hash over a file content and size of a file specified with [filename]', 'stringValueSet', None),
    (178, 'Removes a file from the file system specified with [filename]', 'stringValueSet', None),
    (1000, 'TCLV table version', None, None),
    (1001, 'TCLV SCPU Frontlight Mode', None, None),
    (1002, 'SCPU Front light map 0: PWM+LDR', 'tclvHandlerFlightSet', 'tclvHandlerFlightGet'),
    (1003, 'SCPU Front light map 1: PWM+LDR', 'tclvHandlerFlightSet', 'tclvHandlerFlightGet'),
    (1004, 'SCPU Front light map 2: PWM+LDR', 'tclvHandlerFlightSet', 'tclvHandlerFlightGet'),
    (1005, 'SCPU Front light map 3: PWM+LDR', 'tclvHandlerFlightSet', 'tclvHandlerFlightGet'),
    (1006, 'SCPU Front light map 4: PWM+LDR', 'tclvHandlerFlightSet', 'tclvHandlerFlightGet'),
    (1007, 'SCPU Front light map 5: PWM+LDR', 'tclvHandlerFlightSet', 'tclvHandlerFlightGet'),
    (1008, 'SCPU Front light map 6: PWM+LDR', 'tclvHandlerFlightSet', 'tclvHandlerFlightGet'),
    (1009, 'SCPU Front light map 7: PWM+LDR', 'tclvHandlerFlightSet', 'tclvHandlerFlightGet'),
    (1010, 'SCPU Frontlight PWM frequency in Hz', 'u16ValueSet', None),
    (1011, 'SCPU Frontlight samplerate in sec', 'u16ValueSet', None),
    (1012, 'SCPU Frontlight prefilter [0 .. 100]', 'u16ValueSet', None),
    (1013, 'SCPU Frontlight postfilter [0 .. 100]', 'u16ValueSet', None),
    (1014, 'TCLV SCPU Frontlight sensor timeout in sec', None, None),
    (1015, 'TCLV SCPU Heater Mode', None, None),
    (1016, 'SCPU Heater OFF temperature in °C', 'u16ValueSet', None),
    (1017, 'SCPU Heater ON temperature in °C', 'u16ValueSet', None),
    (1018, 'TCLV SCPU Device Mode', None, None),
    (1019, 'SCPU Minimal OFF temperature in °C', 'u16ValueSet', None),
    (1020, 'SCPU Maximal OFF temperature in °C', 'u16ValueSet', None),
    (1021, 'TCLV SCPU Battery threshold in mV', None, None),
    (1022, 'SCPU Frontlight PWM limit in %', 'u16ValueSet', None),
    (1300, 'Command to push parameters to SCPU', None, None),
    (1301, 'Command to pull parameters from SCPU', None, None),
    (1302, 'Command to save parameters to SCPU', None, None),
    (1303, 'Reboots the SCPU', None, None),
    (2000, 'TCLV table version', None, None),
    (2001, 'TCLV DPU mode', None, None),
    (2002, 'TCLV DPU VSPOS rail voltage in mV', None, None),
    (2003, 'TCLV DPU VSNEG rail voltage in mV', None, None),
    (2004, 'TCLV DPU VGPOS rail voltage in mV', None, None),
    (2005, 'TCLV DPU VGNEG rail voltage in mV', None, None),
    (2006, 'TCLV DPU VCOM1 rail voltage in mV', None, None),
    (2007, 'TCLV DPU VCOM2 rail voltage in mV', None, None),
    (2008, 'TCLV DPU VCOM3 rail voltage in mV', None, None),
    (2009, 'TCLV DPU VCOM4 rail voltage in mV', None, None),
    (2010, 'TCLV DPU sequence 0 ON: delay in ms + rail ID', None, None),
    (2011, 'TCLV DPU sequence 1 ON: delay in ms + rail ID', None, None),
    (2012, 'TCLV DPU sequence 2 ON: delay in ms + rail ID', None, None),
    (2013, 'TCLV DPU sequence 3 ON: delay in ms + rail ID', None, None),
    (2014, 'TCLV DPU sequence 4 ON: delay in ms + rail ID', None, None),
    (2015, 'TCLV DPU sequence 5 ON: delay in ms + rail ID', None, None),
    (2016, 'TCLV DPU sequence 6 ON: delay in ms + rail ID', None, None),
    (2017, 'TCLV DPU sequence 7 ON: delay in ms + rail ID', None, None),
    (2018, 'TCLV DPU sequence 0 OFF: delay in ms + rail ID', None, None),
    (2019, 'TCLV DPU sequence 1 OFF: delay in ms + rail ID', None, None),
    (2020, 'TCLV DPU sequence 2 OFF: delay in ms + rail ID', None, None),
    (2021, 'TCLV DPU sequence 3 OFF: delay in ms + rail ID', None, None),
    (2022, 'TCLV DPU sequence 4 OFF: delay in ms + rail ID', None, None),
    (2023, 'TCLV DPU sequence 5 OFF: delay in ms + rail ID', None, None),
    (2024, 'TCLV DPU sequence 6 OFF: delay in ms + rail ID', None, None),
    (2025, 'TCLV DPU sequence 7 OFF: delay in ms + rail ID', None, None),
    (2300, 'Command to push parameters to SCPU', None, None),
    (2301, 'Command to pull parameters from SCPU', None, None),
    (2302, 'Command to save parameters to SCPU', None, None),
    (2303, 'Reboots the SCPU', None, None),
)

TCLV: dict[int, TclvEntry] = {
    pid: TclvEntry(
        id=pid,
        name=name,
        network_writable=pid not in NETWORK_READ_ONLY,
        v1_set_handler=setter,
        v1_get_handler=getter,
    )
    for pid, name, setter, getter in _ROWS
}

assert len(TCLV) == 235, f"the vendor table has 235 entries, got {len(TCLV)}"


def lookup(param_id: int) -> TclvEntry | None:
    """Return the table row for *param_id*, or None if it is not a known id."""
    return TCLV.get(param_id)


def name_of(param_id: int) -> str:
    """Human name for *param_id*, or ``"unknown(<id>)"``."""
    entry = TCLV.get(param_id)
    return entry.name if entry else f"unknown({param_id})"


def check_writable(param_id: int) -> None:
    """Raise :class:`~pyvisionect.wire.errors.ReadOnlyParameter` if a network write is futile.

    The eight network-read-only ids are the ones a first-time provisioning
    actually needs, so the error names the USB command that can do the job.
    """
    from ..wire.errors import ReadOnlyParameter

    if param_id in NETWORK_READ_ONLY:
        raise ReadOnlyParameter(
            param_id, name_of(param_id), USB_HINTS.get(param_id, "see the USB CLI reference")
        )
