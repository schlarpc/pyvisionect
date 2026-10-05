r'''A fake transport carrying the real sign's real replies.

Every string in :data:`CAPTURED` was read off ``/dev/ttyUSB0`` from a live
Visionect 32" sign running firmware 7.4.4407, then put through one scrubbing
pass: the device UUID, the WiFi SSID, every MAC and BSSID, the server hostname
and the stale static IPv4 block were replaced with documentation-range
placeholders.  **Nothing else was touched** -- the padding, the inconsistent
quoting, the blank line before the UUID, the ``rv: 0`` that comes before
``certs_config_get``'s body and the ``PROTOCOL VERSION`` key with a space in it
are all exactly as the firmware emits them.

That matters more than it sounds.  A fixture written by hand from a parser's
point of view tests the parser against its own assumptions; these strings test
it against the device.  Several of the parsers in
:mod:`pyvisionect.io.usb.parsers` are shaped the way they are only because of a
detail visible here and nowhere in the vendor's documentation.

Scrub map, for reference when comparing against a fresh capture:

====================================  ================================
real                                  published
====================================  ================================
device UUID (12 bytes + 4 NULs)       ``00112233-4455-6677-8899-aabb...``
WiFi SSID                             ``ExampleAP``
WiFi and BSSID MACs                   ``00:00:5e:00:53:00``
Ethernet MAC                          ``02:00:5e:00:53:00``
server hostname                       ``visionect.internal.example.com``
stored static IPv4 block              ``192.0.2.0/24``
stored EAP username                   ``exampleuser``
====================================  ================================

The Ethernet MAC keeps its leading ``02``, because that bit -- the
locally-administered flag -- is the evidence that the field holds a synthesised
default rather than a real vendor address.  Scrubbing it to ``00:`` would have
quietly deleted the finding.

The UUID's trailing zero bytes are **deliberately preserved**: the factory
identifier is 12 bytes zero-padded to 16, so a scrubbed UUID that ended in
random hex would misrepresent the format.
'''

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

__all__ = ["ASYNC_LOG_CAPTURE", "CAPTURED", "HELP_TEXT", "FakeTransport", "fake_console"]

CAPTURED: Mapping[str, str] = {
    'battery_conf_get': 'battery_conf_get\r\nThreshold OFF: 3500 mV\r\nThreshold ON:  3400 mV\r\nThreshold CNT: 1\r\n> ',
    'border_get': 'border_get\r\nBorder support: No\r\nBorder: Server\r\n> ',
    'bq24023_mode_get': 'bq24023_mode_get\r\nBQ: fast charge\r\nIbatt: 50 mA\r\nVbatt: 4214 mV\r\n> ',
    'cc3100_fw_version': 'cc3100_fw_version\r\nNWP 2.12.2.8\r\nMAC 1.5.0.10\r\nPHY 1.0.3.37\r\nChipId 67108864\r\nROM 13107\r\n> ',
    'cc3100_mac_address': 'cc3100_mac_address\r\n00:00:5e:00:53:00\r\nrv: 0\r\n> ',
    'cc3100_rssi': 'cc3100_rssi\r\nRSSI:-31 dBm\r\n> ',
    'certs_config_get': 'certs_config_get\r\nrv: 0\r\nNo EAP cert found!\r\n> ',
    'cli_version_get': 'cli_version_get\r\nCLI version: 1.2\r\n> ',
    'conn_fw_ver': 'conn_fw_ver\r\n6.4294967295.3310684876\r\nrv: 0\r\n> ',
    'conn_retry_get': 'conn_retry_get\r\nNet error retry: 1\r\n> ',
    'conn_state_get': 'conn_state_get\r\nConn: tcp open\r\n> ',
    'conn_type_get': 'conn_type_get\r\nCC3100 (6) active\r\n> ',
    'conn_type_list': 'conn_type_list\r\nW5500 (3)\r\nCC3100 (6)\r\nNone (0)\r\n> ',
    'display_conf_get': 'display_conf_get\r\nVcom 0: 2400 mV\r\nVcom 1: 2400 mV\r\nVcom 2: 2400 mV\r\nVcom 3: 2400 mV\r\nVcom 4: 0 mV\r\nVcom 5: 0 mV\r\nVcom 6: 0 mV\r\nVcom 7: 0 mV\r\nDisplay type: 3255107880\r\n> ',
    'encryption_config_get': "encryption_config_get\r\nOutbound enc: Disabled\r\nKey: ''\r\n> ",
    'eth_conf_get': 'eth_conf_get\r\nMAC: 02:00:5E:00:53:00\r\nTRC: 8\r\nTRT: 2000\r\n> ',
    'feat_get': 'feat_get\r\nFeature touch: enabled\r\nFeature EAP:   enabled\r\n> ',
    'fs_ls': 'fs_ls\r\n/image0.pv2 0 134409\r\n/image1.pv2 0 199047\r\n/image2.pv2 0 1213841\r\n/image3.pv2 0 781996\r\n/image4.pv2 0 453496\r\n/image5.pv2 0 1007217\r\nrv: 0\r\n> ',
    'fs_stats': 'fs_stats\r\ntotal blocks: 2048, in use: 931\r\n> ',
    'fw_version_get': 'fw_version_get\r\nFW Version: 7.4.4407\r\nFW Build date: 10 9 2025\r\nFW Crc=0x652be4ad, Hash=0x8281f596, Length=408876\r\nBL Version: 7.4.4407\r\nBL Build date: 10 9 2025\r\nBuild Version: 7.4.4407\r\nHW: PP32 v1.1, BOM: 0, APP: Joan\r\nEPD: 31.2",2x2880x640,WF=31.2_C296,IC=31.2_p224rev0050\r\n> ',
    'gtin_get': 'gtin_get\r\nGTIN: 3830065461792\r\n> ',
    'image_rotate_config_get': 'image_rotate_config_get\r\nMode: off\r\nTimeout: 75 sec\r\nNumber of images: 0\r\n> ',
    'ipv4_conf_get': 'ipv4_conf_get\r\nIP:  192.0.2.31\r\nNM:  255.255.255.0\r\nGW:  192.0.2.250\r\nDNS: 0.0.0.0\r\nMODE: 1\r\n> ',
    'lmr': 'lmr\r\nLM75: 24 degC\r\n> ',
    'log_config_get': 'log_config_get\r\nMobile: 0\r\n> ',
    'max17135_dump': 'max17135_dump\r\nIncorrect command parameter(s).  Enter "help" to view a list of available commands.\r\n\r\n> ',
    'server_hb_get': 'server_hb_get\r\nHB interval: 1\r\n> ',
    'server_tcp_get': 'server_tcp_get\r\nServer IP/DNS: visionect.internal.example.com\r\nServer port: 11113\r\n> ',
    'sf_list': 'sf_list\r\nIDX | SPI_CHANNEL\r\n----|------------\r\n 00 |  2\r\n\r\nrv: 0x0\r\n> ',
    'sf_rdid': 'sf_rdid\r\nassert: spi_flash_cli.c:139\r\n',
    'sf_rdst': 'sf_rdst\r\nassert: spi_flash_cli.c:188\r\n',
    'status_get': 'status_get\r\nCONNECT_REASON: 0x0\r\nERROR_CODE: 0x0\r\nHW_ID: 0xFFFFFFFF\r\nFW_CRC: 0x652BE4AD\r\nWIFI_MODULE_ID: 0x6\r\nMOBILE_MODULE_ID: 0xFFFFFFFF\r\nCONNECTIVITY_USED: 6\r\nNUMBER_OF_SUPPORTED_DISPLAYS: 2\r\nDISPLAYS_ID: 0xC2050128\r\nDISPLAY_STATE_CRC: 0x8C3650A8\r\nBATTERY_LEVEL: 100\r\nCHARGING_STATUS: 0x1\r\nMIN_TEMPERATURE: 24\r\nSIGNAL_STRENGTH: 31\r\nSYSTEM_UPTIME: 5\r\nFIRMWARE_VERSION_MAJOR: 7\r\nFIRMWARE_VERSION_MINOR: 4\r\nFIRMWARE_VERSION_REVISION: 4407\r\nBOOTLOADER_VERSION_MAJOR: 7\r\nBOOTLOADER_VERSION_MINOR: 4\r\nBOOTLOADER_VERSION_REVISION: 4407\r\nHARDWARE_NAME_ID: 0x8\r\nHARDWARE_VERSION_MAJOR: 1\r\nHARDWARE_VERSION_MINOR: 1\r\nHARDWARE_VERSION_REVISION: 0\r\nHARDWARE_FIRMWARE_INTERFACE: 0x0\r\nHEART_BEAT_INTERVAL: 1\r\nTOUCH_TYPE: 0x0\r\nTOUCH_FW_VERSION_MAJOR: 0\r\nTOUCH_FW_VERSION_MINOR: 0\r\nTOUCH_EVT_COUNT: 0\r\nBATTERY_VOLTAGE: 4214\r\nBATTERY_CURRENT: 48\r\nEVENT_QUEUE_OVERFLOW: 0\r\nDISPLAY_WIDTH: 2880\r\nDISPLAY_HEIGHT: 640\r\nPROTOCOL VERSION: 3\r\nFEATURES1: 585\r\nEPD_TEMP_SENSOR: 24\r\nFL_BUTTON_COUNT: 0\r\nWIRELESS_LINK_TYPE: -1\r\nGTIN: 3830065461792\r\nIMAGE_ALLOW: 1\r\nMOB_SECURITY: 2\r\nFS_TOTAL_SIZE: 8388608\r\nFS_FREE_SIZE: 4575232\r\nPROXIMITY_COUNT: 0\r\nNETWORK_ERROR_COUNT: 0\r\nMCU_AWAKE_COUNT: 20\r\nDCM_STATE: 0\r\nWIFI_DTIM: 100\r\nPROXIMITY_THRESHOLD: -1\r\nBSSID_1: 0x5E0000\r\nBSSID_2: 0x53\r\nLAST_STATUS: 0x282296CD\r\nDISPLAY_UPDATE_COUNT: 1\r\nCONN_VER_MAJOR: 6\r\nCONN_VER_MINOR: -1\r\nCONN_VER_REVISION: -984282420\r\nSTATUS_END\r\n> ',
    'system_conf_get': 'system_conf_get\r\nBattery mode: 0\r\nTouch mode: 3\r\nShipping mode: 0\r\n> ',
    'task_list': 'task_list\r\ntask name                      \tstate\tstk free\ttask nr\r\nusb_cli_task                   \tX\t402\t6\r\nIDLE                           \tR\t222\t2\r\nvp_debounce_task               \tB\t220\t9\r\nW5500                          \tB\t988\t10\r\ntouch_hal                      \tB\t450\t13\r\nconnectivity_task              \tB\t300\t7\r\ntimer ms                       \tB\t480\t3\r\ntimer rtc                      \tB\t184\t4\r\nmain                           \tB\t754\t5\r\nCC3100                         \tB\t932\t11\r\npv2_receiver_task              \tB\t376\t8\r\nCC3100 RX                      \tB\t418\t14\r\npv_tx                          \tB\t474\t12\r\ninit                           \tB\t432\t1\r\nHeap free: 19136\r\n> ',
    'uptime': 'uptime\r\nUptime in min: 5\r\n> ',
    'uuid_get': 'uuid_get\r\n\r\nUUID: 0x00 0x11 0x22 0x33 0x44 0x55 0x66 0x77 0x88 0x99 0xaa 0xbb 0x00 0x00 0x00 0x00 \r\n> ',
    'wifi_bssid_get': 'wifi_bssid_get\r\n00:00:5e:00:53:00\r\nrv: 0x0\r\n> ',
    'wifi_conf_get': 'wifi_conf_get\r\nSSID: ExampleAP\r\nSecurity: wpa2\r\nBand: 0\r\n> ',
    'wifi_eap_conf_get': 'wifi_eap_conf_get\r\nEAP method:   PEAP/MSCHAPV2\r\nUsername:     exampleuser\r\n> ',
    'wifi_mac_conf_get': 'wifi_mac_conf_get\r\nWiFi MAC: 00:00:5e:00:53:00\r\n> ',
}
"""Command -> the complete frame the device sent back, echo and prompt included.

Keyed by the command exactly as it was sent.  The value starts with the device's
echo of that command and ends with the ``"> "`` prompt, because that is what a
reader has to cope with.
"""

HELP_TEXT = "help\n\n24aa256_test: EEPROM pattern read/write test\n\napp_sleep <minutes>: Put app into deep sleep\n\napp_wakeup: Wake-up app from the deep sleep\n\nautoconn  <timeout>: Disable auto-connect\n\nbattery_conf_get: Display battery cfg\n\nbattery_conf_set <thr_off> <thr_on> <thr_cnt>: Set battery cfg\n\nborder_get: Get border mode\n\nbq24023_mode_get: Display state\n\nbsim <mode> <force>: Simulate battery state\n\nbsimi <mode> <current>: Simulate battery charging current\n\nbsimv <voltage>: Simulate battery voltage\n\ncc3100_dns <url>: Resolve IP from URL\n\ncc3100_format: Formats the CC3100 SPI FLASH\n\ncc3100_fw_upgrade: Force FW upgrade\n\ncc3100_fw_version: Display WiFi FW version\n\ncc3100_mac_address: Display WiFi MAC address\n\ncc3100_rssi: Read RSSI value from CC3100 WiFi module\n\ncc3100_scan: Scan for WiFi APs\n\ncerts_config_get: Display WiFi certificate cfg\n\ncli_password_set <password> <password>: Sets CLI password\n\ncli_version_get: Display CLI version\n\nconn_fw_ver: Scan for WiFi APs\n\nconn_retry_get: Display conn error retry interval\n\nconn_retry_set <time>: Set conn. error retry interval\n\nconn_scan: Scan for WiFi APs\n\nconn_state_get: Display connection state\n\nconn_type_get: Display connectivity type\n\nconn_type_list: List available connections\n\nconn_type_set <type>: Set connectivity type\n\ncs <state>: Change connection state\n\ndcmb <mode>: Sets the border mode\n\ndcmc <color>: Clears the display to a specified color\n\ndcmd: DCM Power-down display driver\n\ndcmh <mode>: Runs EPD pre/post-update hook\n\ndcms: Put display driver to sleep\n\ndcmt <driver_no>: DCM Select ESPON driver to communicate with\n\ndcmu: DCM Power-up display driver an put it to sleep\n\ndcmw: DCM Wake-up display driver\n\ndisplay_conf_get: Display VCOMs and display type\n\ndisplay_conf_set <vcom1> <vcom2> ... <vcom16> <display_id>: Set display cfg\n\nencryption_config_get: Display encryption settings\n\nencryption_key_set <key>: Set encryption key\n\nencryption_mode_set <mode>: Set encryption mode\n\neth_conf_ext_set <mac> <trc> <trt>: Set extented Eth cfg\n\neth_conf_get: Display Eth cfg\n\neth_conf_mac_reset Reset Eth MAC cfg\n\neth_conf_set <mac>: Set Eth MAC\n\nfeat_disable <key>: Disable feature\n\nfeat_enable <key>: Enable feature\n\nfeat_get: List features status\n\nflash_load: Reload settings\n\nflash_save: Save settings\n\nfs_format: Formats the file system\n\nfs_ls: List files on the file system.\n\nfs_stats: Displays block usage statistics\n\nfw_version_get: Show system FW version\n\ngtin_get: Get GTIN code\n\nhelp: Lists all the registered commands\n\nimage_rotate_config_get: Display image rotate cfg\n\nimage_rotate_config_set <mode> <timeout> <num_images>: Sets image rotate cfg\n\nipv4_conf_get: Display IPv4 cfg\n\nipv4_conf_set <ip> <nm> <gw> <dns> <mode>: Set IPv4 Eth cfg\n\nlmr: Read temperature from LM75\n\nlms <mode> <value>: Simulates LM75 temperature\n\nlog_config_get: Display debug logging\n\nlog_config_set <module> <value>: Set debug logging\n\nm2m_mode  <timeout>: Disable autonomous connectivity reconnection\n\nmax17135_dump: Dump register values\n\nmax17135_pwr <enable>: Enables/disables MAX17135 power\n\nmax17135_selftest <i2c_channel>: Performs MAX17135 selftest\n\nmax17135_sleep <i2c_channel>: Puts MAX17135 to sleep\n\nmax17135_wakeup <i2c_channel>: Wakes up MAX17135\n\npbs: PV2 send button\n\npgs: PV2 send GPS\n\nplay_music: Play built-in song\n\npss: PV2 send status\n\npts <x> <y>: PV2 send touch\n\nreboot: Reboot device\n\nserver_hb_get: Display HB\n\nserver_hb_set <time>: Set heart-beat\n\nserver_tcp_get: Display server's IP and port\n\nserver_tcp_set <ip> <port>: Set server's IP and port\n\nsf_list: Lists all registered SPI FLASH devices\n\nsf_rdid: Reads SPI FLASH chip ID\n\nsf_rdst: Reads SPI FLASH chip status\n\nsf_select: Selects a SPI FLASH decice from the list\n\nsf_unprot: Unprotects the SPI FLASH chip\n\nsf_wrst: Writes 0x00 to SPI FLASH chip status register\n\nstatus_get: Display status packet\n\nsystem_conf_get: Display system cfg\n\nsystem_conf_set <batt_ind_en> <touch_en> <ship_en>: Set system cfg\n\ntask_list: Show registered tasks\n\nuptime: Display system uptime\n\nuuid_get: Display device's UUID\n\nvlog_set_default_levels: Reset logger levels to default\n\nvlog_set_destination_level <destination> <level>: Set logger level on a destination side\n\nvlog_set_source_level <source> <level>: Set logger level on a source side\n\nvlog_unify_levels <usb_level>: Reset logger levels to default for USB\n\nwifi_bssid_get: Get WiFi BSSID\n\nwifi_conf_get: Get WiFi cfg\n\nwifi_conf_set <ssid> <psk> <security> <band>: Set WiFi connection\n\nwifi_eap_conf_em_set <eap_method>: Set WiFi EAP method\n\nwifi_eap_conf_get: Display WiFi EAP cfg\n\nwifi_eap_conf_pwd_set <password>: Set WiFi EAP password\n\nwifi_eap_conf_usr_set <username>: Set WiFi EAP username\n\nwifi_ext_mode_set <mode>: Sets WiFi mode\n\nwifi_ext_region_set <region>: Set WiFi region cfg\n\nwifi_mac_conf_get: Display WiFi MAC cfg\n\nwifi_mac_conf_set <mac>: Set WiFi MAC cfg\n\nwifi_psk_set <psk>: Set wpa2 password\n\nwifi_security_set <security>: Set WiFi security mode\n\nrv: 0\n> "
"""The full ``help`` frame, ~5 KB: the 111 commands this firmware's ``help`` prints.

Not every command the firmware *has*: ``wifi_ssid_set`` is present and answers,
and is deliberately not in here, because it is not in the capture either.  See
:data:`pyvisionect.io.usb.commands.HIDDEN_IN_7_4_4407`.
"""

ASYNC_LOG_CAPTURE = '\nuptime\nUptime in min: 0\n> border (0 1)\nUPD_FULL\nborder (0 1)\nUPD_FULL\nborder (0 1)\nUPD_FULL\nborder (0 1)\nUPD_FULL\nborder (0 1)\nUPD_FULL\nborder (0 1)\nUPD_FULL\ndisplay update id: 0x8c3650a8\nDisplay updated!\nmake packet type=1, id=891437714\nFrame send 64 bytes\nSetting heartbeat after 1 min\nProfiling:Total=6184,RxHole=0,Decrypt=0,Decomp=69,EpdLoad=494,EpdWake=49,EpdSleep=2,EpdStart=0,EpdEnd=0,EpdOther=244,EpdUpd=5298,Pv2Len=20508,RawLen=1843200,ImgOpt=0x00003024,ImgId=0x8c3650a8\nuptime\nUptime in min: 0\n> uptime\nUptime in min: 0\n> sys evt vplatform_heartbeat.c:19, Heartbeat (7)\nReceived event: Heartbeat (7)\nHeart-beat event\nsending pv2 status on heartbeat\nmake packet type=3, id=2\nFrame send 552 bytes\nSetting heartbeat after 1 min\nGot response (id=2)\nSetting heartbeat after 1 min\nuptime\nUptime in min: 1\n> uptime\nUptime in min: 1\n> sys evt vplatform_heartbeat.c:19, Heartbeat (7)\nReceived event: Heartbeat (7)\nHeart-beat event\nsending pv2 status on heartbeat\nmake packet type=3, id=3\nFrame send 552 bytes\nSetting heartbeat after 1 min\nGot response (id=3)\nSetting heartbeat after 1 min\nuptime\nUptime in min: 2\n> uptime\nUptime in min: 2\n> sys evt vplatform_heartbeat.c:19, Heartbeat (7)\nReceived event: Heartbeat (7)\nHeart-beat event\nsending pv2 status on heartbeat\nmake packet type=3, id=4\nFrame send 552 bytes\nSetting heartbeat after 1 min\nGot response (id=4)\nSetting heartbeat after 1 min\nuptime\nUptime in min: 3\n> uptime\nUptime in min: 3\n> uptime\nUptime in min: 3\n> sys evt vplatform_heartbeat.c:19, Heartbeat (7)\nReceived event: Heartbeat (7)\nHeart-beat event\nsending pv2 status on heartbeat\nmake packet type=3, id=5\nFrame send 552 bytes\nSetting heartbeat after 1 min\nGot response (id=5)\nSetting heartbeat after 1 min\nuptime\nUptime in min: 4\n> '
"""Four and a half minutes of unsolicited UART output, verbatim.

Captured by sitting on the port and nudging it with ``uptime`` every 25
seconds, which is why reply frames and log bursts are interleaved here exactly
as they arrive in practice.  Contains four heartbeat bursts, a ``Profiling:``
line from an image push, and the prompt/echo pattern around each ``uptime``.

This is the fixture behind the interleaving tests: the heartbeat burst is a real
eight-line block that really does land between a command and its prompt.
"""


_UNKNOWN = (
    "Command {command!r} not recognised.  Enter 'help' to view a list of "
    "available commands."
).replace("{command!r}", "'{command}'")

_PROMPT = "> "


class FakeTransport:
    r"""A :class:`~pyvisionect.io.usb.console.SerialTransport` with no hardware.

    Answers from :data:`CAPTURED` by default, so a test exercises the real
    parser against the real bytes.  Unknown commands get the firmware's real
    unrecognised-command string, so the error path is exercised too.

    Args:
        replies: command -> the frame to send back. Defaults to
            :data:`CAPTURED`. A value may omit the echo and the prompt; this
            class adds whichever is missing, so a hand-written reply is just its
            body.
        echo: whether to echo the command line, as the real device does.
        prompt: the prompt to append.
        async_lines: lines to inject as unsolicited log output. Each entry is
            ``(after_n_commands, text)``: the text is queued to arrive just
            *before* the reply to command number ``after_n_commands``
            (0-based), which is the case that a drain-before-send handles.
        interleave: ``(command, after_line_index, text)`` entries that splice a
            log line **into the middle of a reply**, after the given reply line.
            This is the failure mode that matters, and it is hard to provoke on
            real hardware on demand, so it is injectable here.
        pre_echo: ``(command, text)`` entries emitted after the command is
            written but **before** the device's echo of it, which is the real
            arrival pattern for a log line that fires while the command is in
            flight. Exercises the echo anchor.
        unknown_commands: names to answer as unrecognised even if they are in
            *replies*. For testing the firmware-delta path.
        terminator_required: the byte that must end a written line before the
            fake will answer. Defaults to ``b"\r"``, matching the device: a
            line ending only in ``\n`` is echoed and then **held**, exactly as
            the real firmware holds it, so a test can catch a wrong terminator.

    Example:
        >>> from pyvisionect.io.usb import SerialConsole
        >>> console = SerialConsole(transport=FakeTransport())
        >>> console.command("uptime").field("Uptime in min")
        '5'
    """

    def __init__(
        self,
        replies: Mapping[str, str] | None = None,
        *,
        echo: bool = True,
        prompt: str = _PROMPT,
        async_lines: Sequence[tuple[int, str]] = (),
        interleave: Sequence[tuple[str, int, str]] = (),
        pre_echo: Sequence[tuple[str, str]] = (),
        unknown_commands: Iterable[str] = (),
        terminator_required: bytes = b"\r",
    ) -> None:
        self.replies = dict(replies) if replies is not None else {
            **CAPTURED,
            "help": HELP_TEXT,
        }
        self.echo = echo
        self.prompt = prompt
        self.async_lines = list(async_lines)
        self.interleave = list(interleave)
        self.pre_echo = list(pre_echo)
        self._fired: set[int] = set()
        self.unknown_commands = set(unknown_commands)
        self.terminator_required = terminator_required
        self.written: list[str] = []
        self.commands: list[str] = []
        self.closed = False
        self._out = bytearray()
        self._held = ""

    # ---------------------------------------------------- transport protocol

    @property
    def in_waiting(self) -> int:
        return len(self._out)

    def write(self, data: bytes, /) -> int:
        text = data.decode("ascii")
        self.written.append(text)
        self._held += text
        while self.terminator_required.decode("ascii") in self._held:
            line, _, self._held = self._held.partition(
                self.terminator_required.decode("ascii")
            )
            self._submit(line.strip("\r\n"))
        return len(data)

    def flush(self) -> None:
        pass

    def read(self, size: int = 1, /) -> bytes:
        out, self._out = self._out[:size], self._out[size:]
        return bytes(out)

    def close(self) -> None:
        self.closed = True

    # -------------------------------------------------------------- internals

    def _submit(self, line: str) -> None:
        index = len(self.commands)
        for slot, (after, text) in enumerate(self.async_lines):
            if after == index and slot not in self._fired:
                self._fired.add(slot)
                self._out += (text + "\r\n").encode("ascii")
        if not line:
            # bare CR: the device answers "\r\n> "
            self._out += ("\r\n" + self.prompt).encode("ascii")
            return
        self.commands.append(line)
        self._out += self._frame(line).encode("ascii")

    def _frame(self, line: str) -> str:
        name = line.split()[0].lower() if line.split() else ""
        if name in self.unknown_commands or (
            line not in self.replies and name not in self.replies
        ):
            body = [_UNKNOWN.format(command=line), ""]
            captured = None
        else:
            captured = self.replies.get(line, self.replies.get(name, ""))
            body = self._body_of(captured, line)

        splices = [(i, t) for cmd, i, t in self.interleave if cmd == line]
        if splices:
            out: list[str] = [t for at, t in splices if at < 0]
            for i, reply_line in enumerate(body):
                out.append(reply_line)
                for at, text in splices:
                    if at == i:
                        out.append(text)
            body = out

        head = "".join(
            f"{text}\r\n" for cmd, text in self.pre_echo if cmd == line
        )
        head += f"{line}\r\n" if self.echo else ""
        tail = "".join(f"{reply_line}\r\n" for reply_line in body)
        return head + tail + self.prompt

    def _body_of(self, captured: str, line: str) -> list[str]:
        """Strip a captured frame back to its reply lines."""
        text = captured
        if self.prompt and text.endswith(self.prompt):
            text = text[: -len(self.prompt)]
        parts = text.replace("\r\n", "\n").split("\n")
        if parts and parts[0].strip().lower() == line.strip().lower():
            parts = parts[1:]
        while parts and not parts[-1].strip():
            parts.pop()
        return parts


def fake_console(**kwargs: object) -> object:
    """A :class:`~pyvisionect.io.usb.console.SerialConsole` on a :class:`FakeTransport`.

    Timeouts are set small so a test that provokes a timeout does not take
    seconds.  Keyword arguments go to :class:`FakeTransport`.
    """
    from .console import SerialConsole

    return SerialConsole(
        transport=FakeTransport(**kwargs),  # type: ignore[arg-type]
        command_timeout=1.0,
        idle_timeout=0.05,
        read_timeout=0.0,
    )
