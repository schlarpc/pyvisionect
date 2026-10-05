'''The command set this firmware actually has, read off the device.

Why this table exists
---------------------

The vendor's CLI reference documents **162** commands.  ``help`` on firmware
7.4.4407 on a 32" system board prints **111** of them, and the firmware is known
to answer **112**: ``wifi_ssid_set`` is present but unlisted.  Commands are
compiled in behind switches (``USE_CC3100_DRIVER``, ``USE_RS9113_DRIVER``,
``USE_MMA7660_DRIVER``, ``USE_VPLATFORM_SCPU_EXT``, ...), so the device in front
of you is the only authority -- but ``help`` is not a complete index of it, which
is the single most important thing on this page.  The delta is not small: 97
documented commands are unlisted and 47 undocumented ones are printed.

Two findings matter enough to call out:

* **``help`` hides at least one command.**  ``wifi_ssid_set`` is documented by
  the vendor, is not printed by ``help``, and yet answers ``E: Invalid
  argument(s)`` to a bare call -- which a missing command does not; it answers
  ``Command '...' not recognised.``  So it exists.  It is the documented way to
  set an SSID containing a space, and it takes the SSID alone, with no PSK.
  Whether the console's parser will actually carry a space through it is a
  separate, **unverified** question -- see ``A2`` in ``OPEN-QUESTIONS.md`` and
  :func:`pyvisionect.io.usb.provisioning.plan_wifi`.
* **...and exactly one.**  49 of the 97 unlisted commands have since been
  probed by bare invocation on the device -- every one whose vendor-documented
  arity includes a required argument, which is what makes the probe safe --
  and **all 49 answered** ``Command '...' not recognised.``  So
  ``wifi_ssid_set`` is a one-off and not the tip of an iceberg, and the whole
  ``scpu_*``/``touch_*``/``mobile_*``/``frontlight_*`` story really is "not
  compiled into this build".  See :data:`PROBED_ABSENT_7_4_4407` for the list
  and ``A1`` in ``OPEN-QUESTIONS.md`` for the method and the 48 still unprobed.

Note that the existence oracle has **two** positive spellings, not one.  A
present-but-misinvoked command answers either ``E: Invalid argument(s)`` (the
``wifi_*`` and ``vlog_*`` setters) or ``Incorrect command parameter(s).`` (the
``vlog_*`` family on a bare call).  Only ``Command '...' not recognised.``
means the firmware does not have it.  Matching on the error *prefix* is not
enough: the firmware also emits unsolicited ``E: ...`` log lines such as
``E: TCP connection Error: -111``, so a probe has to match the full string.
* **``flash_print`` is not listed**, although the reverse engineering notes call
  it "the single highest-value command" for dumping every stored setting.  There
  is no *known* single-command settings dump here; the per-area getters are the
  whole story, which is why :meth:`pyvisionect.io.usb.device.Sign.dump` walks
  them.  Note this one has not been probed the way ``wifi_ssid_set`` was: it is
  nullary in the vendor reference, and a bare call to a nullary command executes
  it rather than failing, so the existence oracle is unsafe here.

The delta is recorded as data, in :data:`ABSENT_FROM_7_4_4407` and
:data:`UNDOCUMENTED_IN_7_4_4407`, so it stays checkable rather than becoming a
paragraph in a README that rots.

Relationship to the TCLV table
------------------------------

The CLI's key space *is* the TCLV key space -- see
:mod:`pyvisionect.devices.tclv`, whose ``USB_HINTS`` already names the command
that sets each network-read-only id.  This module does not restate that mapping;
:data:`TCLV_FOR` gives the reverse direction for the commands where it is
unambiguous.

Safety tiers
------------

Every command carries a :class:`Kind`.  The gating in
:class:`pyvisionect.io.usb.device.Sign` is built on it, so adding a command here
is enough to make it respect the right flag:

``READ``
    Emits information and changes nothing.  Always allowed.
``ACTION``
    Changes nothing persistent but *does* something: ``pss`` makes the sign send
    a status packet, ``play_music`` chirps, ``conn_scan`` scans.  Needs
    ``allow_writes``.
``WRITE``
    An ordinary setter.  RAM-only until ``flash_save``.  Needs ``allow_writes``.
``DESTRUCTIVE``
    Can corrupt the panel, drop the sign off the network, or make it
    unreachable: the ``dcm*`` display-driver controls, ``display_conf_set``,
    every network setter, ``app_sleep``, ``reboot``, ``flash_save``, the
    battery/temperature simulators.  Needs ``allow_destructive``.
``HARD``
    Formats something, flashes firmware, or sets a password you then have to
    know: ``fs_format``, ``cc3100_format``, ``cc3100_fw_upgrade``,
    ``feat_enable``/``feat_disable``, ``cli_password_set``, ``sf_unprot``,
    ``sf_wrst``.  Needs ``i_really_mean_it``.

The tiers are deliberately coarser than "does it write".  ``server_tcp_set`` is
a plain setter by mechanism but it is the command that strands a sign off the
network, and ``IPV4_SERVER_IP`` is USB-only to recover -- so it sits in
``DESTRUCTIVE`` with the display-driver pokes.
'''

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

__all__ = [
    "ABSENT_FROM_7_4_4407",
    "HIDDEN_IN_7_4_4407",
    "LISTED_BY_HELP",
    "ASSERTS_AND_KILLS_CLI",
    "COMMANDS",
    "DESTRUCTIVE_COMMANDS",
    "DOCUMENTED_COUNT",
    "HARD_COMMANDS",
    "Kind",
    "PROBED_ABSENT_7_4_4407",
    "READ_COMMANDS",
    "TCLV_FOR",
    "UNDOCUMENTED_IN_7_4_4407",
    "VLOG_DESTINATIONS",
    "VLOG_LEVELS",
    "VLOG_QUIET",
    "VLOG_VERBOSE",
    "Command",
    "lookup",
]

DOCUMENTED_COUNT = 162
"""Commands in the vendor's published CLI reference.

Counted off the page itself (one ``<h*>`` section per command) rather than by
hand: it is **162**, not the 160 this module claimed until 2026-10-05.  The two
that were missed are ``rs9110_scan`` and ``rs9113_scan``, which were absent from
:data:`ABSENT_FROM_7_4_4407` as well, so both counts moved together.
"""

VLOG_LEVELS: range = range(1, 6)
"""Valid ``<level>`` arguments for every ``vlog_*`` setter: **1 to 5**.

Measured, not inferred.  All three of ``vlog_unify_levels``,
``vlog_set_source_level`` and ``vlog_set_destination_level`` answer
``E: Invalid argument(s)`` to ``-1``, ``0``, ``6``, ``99`` and a non-numeric
argument, and accept 1..5 silently.

**``0`` is not a valid level.**  That retires the question ``A3`` was built on
("does 0 mean emit nothing or emit everything?") -- it means neither, because
the firmware rejects it.
"""

VLOG_QUIET: int = 1
"""The silent end of :data:`VLOG_LEVELS`.

``vlog_unify_levels 1`` reduces the USB log stream to nothing: 0 lines in 90 s,
and 0/0/1 lines across three ``pss``-triggered trials, against 18-61 lines per
90 s at level 5 on a healthy link and ~1 400 lines/min while the radio was
flapping.  Verified in four separate runs on 2026-10-05, including one with the
level order reversed.  See :data:`VLOG_VERBOSE` and
:meth:`pyvisionect.io.usb.device.Sign.unify_usb_log_levels`.
"""

VLOG_VERBOSE: int = 5
"""The loud end of :data:`VLOG_LEVELS`: everything, including image and frame detail.

Levels are a severity threshold and the scale is ordered quiet-to-loud.  Level 1
emits nothing; 2 and 3 emit only ``E:`` error lines; 4 adds the informational
state narration (``From state N going to state N``, ``New connectivity state``,
DHCP/IP/DNS); 5 adds debug detail (``Frame send N bytes``, ``Image transfer
pending``, ``EPD temperature``).
"""

VLOG_DESTINATIONS: range = range(0, 9)
"""Valid ``<destination>`` ids for ``vlog_set_destination_level``: **0 to 8**.

The firmware validates this one -- ``-1``, ``9``, ``16``, ``100``, ``255`` and
``9999`` are all refused -- so there are exactly nine sinks.  Which id is the
USB UART was **not** established; see ``A3`` in ``OPEN-QUESTIONS.md``.

.. warning::

   ``vlog_set_source_level`` validates its *level* but **not** its *source id*.
   ``-1``, ``14``, ``32``, ``100``, ``255`` and ``9999`` are all accepted
   silently, so there is no way to enumerate the source namespace by probing and
   an out-of-range id is an unchecked index into firmware state.  Validate it
   caller-side; :meth:`pyvisionect.io.usb.device.Sign.set_log_source_level`
   does.
"""


class Kind(str, Enum):
    """How dangerous a command is. See the module docstring for the tiers."""

    READ = "READ"
    ACTION = "ACTION"
    WRITE = "WRITE"
    DESTRUCTIVE = "DESTRUCTIVE"
    HARD = "HARD"

    @property
    def is_write(self) -> bool:
        return self is not Kind.READ


@dataclass(frozen=True, slots=True)
class Command:
    """One row of the device's own ``help`` output.

    Attributes:
        name: the command word. Matching is case-insensitive on the device
            (``UPTIME`` works), but the canonical spelling is lowercase.
        args: the argument placeholder string exactly as ``help`` prints it,
            e.g. ``"<ip> <port>"``, or ``""`` for a nullary command. **Not
            always correct**: ``max17135_dump`` is listed nullary and rejects a
            bare invocation with ``Incorrect command parameter(s).``
        description: the help text, verbatim. Also not always correct:
            ``conn_fw_ver`` is described as "Scan for WiFi APs".
        kind: the safety tier.
        documented: whether the vendor's reference lists this command.
        asserts: the ``file:line`` of a firmware assertion this command trips,
            or None. Non-None means calling it kills the CLI task.
    """

    name: str
    args: str
    description: str
    kind: Kind
    documented: bool = True
    asserts: str | None = None

    @property
    def arity(self) -> int:
        """How many placeholders ``help`` shows. See the caveat on :attr:`args`."""
        return self.args.count("<")

    @property
    def syntax(self) -> str:
        """``"name <arg> <arg>"``, for an error message or a dry-run plan."""
        return f"{self.name} {self.args}".strip()


_ROWS = (
    ('24aa256_test', '', 'EEPROM pattern read/write test', 'DESTRUCTIVE'),
    ('app_sleep', '<minutes>', 'Put app into deep sleep', 'DESTRUCTIVE'),
    ('app_wakeup', '', 'Wake-up app from the deep sleep', 'ACTION'),
    ('autoconn', '<timeout>', 'Disable auto-connect', 'DESTRUCTIVE'),
    ('battery_conf_get', '', 'Display battery cfg', 'READ'),
    ('battery_conf_set', '<thr_off> <thr_on> <thr_cnt>', 'Set battery cfg', 'WRITE'),
    ('border_get', '', 'Get border mode', 'READ'),
    ('bq24023_mode_get', '', 'Display state', 'READ'),
    ('bsim', '<mode> <force>', 'Simulate battery state', 'DESTRUCTIVE'),
    ('bsimi', '<mode> <current>', 'Simulate battery charging current', 'DESTRUCTIVE'),
    ('bsimv', '<voltage>', 'Simulate battery voltage', 'DESTRUCTIVE'),
    ('cc3100_dns', '<url>', 'Resolve IP from URL', 'WRITE'),
    ('cc3100_format', '', 'Formats the CC3100 SPI FLASH', 'HARD'),
    ('cc3100_fw_upgrade', '', 'Force FW upgrade', 'HARD'),
    ('cc3100_fw_version', '', 'Display WiFi FW version', 'READ'),
    ('cc3100_mac_address', '', 'Display WiFi MAC address', 'READ'),
    ('cc3100_rssi', '', 'Read RSSI value from CC3100 WiFi module', 'READ'),
    ('cc3100_scan', '', 'Scan for WiFi APs', 'ACTION'),
    ('certs_config_get', '', 'Display WiFi certificate cfg', 'READ'),
    ('cli_password_set', '<password> <password>', 'Sets CLI password', 'HARD'),
    ('cli_version_get', '', 'Display CLI version', 'READ'),
    ('conn_fw_ver', '', 'Scan for WiFi APs', 'READ'),
    ('conn_retry_get', '', 'Display conn error retry interval', 'READ'),
    ('conn_retry_set', '<time>', 'Set conn. error retry interval', 'WRITE'),
    ('conn_scan', '', 'Scan for WiFi APs', 'ACTION'),
    ('conn_state_get', '', 'Display connection state', 'READ'),
    ('conn_type_get', '', 'Display connectivity type', 'READ'),
    ('conn_type_list', '', 'List available connections', 'READ'),
    ('conn_type_set', '<type>', 'Set connectivity type', 'DESTRUCTIVE'),
    ('cs', '<state>', 'Change connection state', 'DESTRUCTIVE'),
    ('dcmb', '<mode>', 'Sets the border mode', 'DESTRUCTIVE'),
    ('dcmc', '<color>', 'Clears the display to a specified color', 'DESTRUCTIVE'),
    ('dcmd', '', 'DCM Power-down display driver', 'DESTRUCTIVE'),
    ('dcmh', '<mode>', 'Runs EPD pre/post-update hook', 'DESTRUCTIVE'),
    ('dcms', '', 'Put display driver to sleep', 'DESTRUCTIVE'),
    ('dcmt', '<driver_no>', 'DCM Select ESPON driver to communicate with', 'DESTRUCTIVE'),
    ('dcmu', '', 'DCM Power-up display driver an put it to sleep', 'DESTRUCTIVE'),
    ('dcmw', '', 'DCM Wake-up display driver', 'DESTRUCTIVE'),
    ('display_conf_get', '', 'Display VCOMs and display type', 'READ'),
    ('display_conf_set', '<vcom1> <vcom2> ... <vcom16> <display_id>', 'Set display cfg', 'DESTRUCTIVE'),
    ('encryption_config_get', '', 'Display encryption settings', 'READ'),
    ('encryption_key_set', '<key>', 'Set encryption key', 'DESTRUCTIVE'),
    ('encryption_mode_set', '<mode>', 'Set encryption mode', 'DESTRUCTIVE'),
    ('eth_conf_ext_set', '<mac> <trc> <trt>', 'Set extented Eth cfg', 'DESTRUCTIVE'),
    ('eth_conf_get', '', 'Display Eth cfg', 'READ'),
    ('eth_conf_mac_reset', '', 'Reset Eth MAC cfg', 'DESTRUCTIVE'),
    ('eth_conf_set', '<mac>', 'Set Eth MAC', 'DESTRUCTIVE'),
    ('feat_disable', '<key>', 'Disable feature', 'HARD'),
    ('feat_enable', '<key>', 'Enable feature', 'HARD'),
    ('feat_get', '', 'List features status', 'READ'),
    ('flash_load', '', 'Reload settings', 'DESTRUCTIVE'),
    ('flash_save', '', 'Save settings', 'DESTRUCTIVE'),
    ('fs_format', '', 'Formats the file system', 'HARD'),
    ('fs_ls', '', 'List files on the file system.', 'READ'),
    ('fs_stats', '', 'Displays block usage statistics', 'READ'),
    ('fw_version_get', '', 'Show system FW version', 'READ'),
    ('gtin_get', '', 'Get GTIN code', 'READ'),
    ('help', '', 'Lists all the registered commands', 'READ'),
    ('image_rotate_config_get', '', 'Display image rotate cfg', 'READ'),
    ('image_rotate_config_set', '<mode> <timeout> <num_images>', 'Sets image rotate cfg', 'WRITE'),
    ('ipv4_conf_get', '', 'Display IPv4 cfg', 'READ'),
    ('ipv4_conf_set', '<ip> <nm> <gw> <dns> <mode>', 'Set IPv4 Eth cfg', 'DESTRUCTIVE'),
    ('lmr', '', 'Read temperature from LM75', 'READ'),
    ('lms', '<mode> <value>', 'Simulates LM75 temperature', 'DESTRUCTIVE'),
    ('log_config_get', '', 'Display debug logging', 'READ'),
    ('log_config_set', '<module> <value>', 'Set debug logging', 'WRITE'),
    ('m2m_mode', '<timeout>', 'Disable autonomous connectivity reconnection', 'DESTRUCTIVE'),
    ('max17135_dump', '', 'Dump register values', 'READ'),
    ('max17135_pwr', '<enable>', 'Enables/disables MAX17135 power', 'DESTRUCTIVE'),
    ('max17135_selftest', '<i2c_channel>', 'Performs MAX17135 selftest', 'DESTRUCTIVE'),
    ('max17135_sleep', '<i2c_channel>', 'Puts MAX17135 to sleep', 'DESTRUCTIVE'),
    ('max17135_wakeup', '<i2c_channel>', 'Wakes up MAX17135', 'DESTRUCTIVE'),
    ('pbs', '', 'PV2 send button', 'ACTION'),
    ('pgs', '', 'PV2 send GPS', 'ACTION'),
    ('play_music', '', 'Play built-in song', 'ACTION'),
    ('pss', '', 'PV2 send status', 'ACTION'),
    ('pts', '<x> <y>', 'PV2 send touch', 'WRITE'),
    ('reboot', '', 'Reboot device', 'DESTRUCTIVE'),
    ('server_hb_get', '', 'Display HB', 'READ'),
    ('server_hb_set', '<time>', 'Set heart-beat', 'WRITE'),
    ('server_tcp_get', '', "Display server's IP and port", 'READ'),
    ('server_tcp_set', '<ip> <port>', "Set server's IP and port", 'DESTRUCTIVE'),
    ('sf_list', '', 'Lists all registered SPI FLASH devices', 'READ'),
    ('sf_rdid', '', 'Reads SPI FLASH chip ID', 'READ'),
    ('sf_rdst', '', 'Reads SPI FLASH chip status', 'READ'),
    ('sf_select', '', 'Selects a SPI FLASH decice from the list', 'DESTRUCTIVE'),
    ('sf_unprot', '', 'Unprotects the SPI FLASH chip', 'HARD'),
    ('sf_wrst', '', 'Writes 0x00 to SPI FLASH chip status register', 'HARD'),
    ('status_get', '', 'Display status packet', 'READ'),
    ('system_conf_get', '', 'Display system cfg', 'READ'),
    ('system_conf_set', '<batt_ind_en> <touch_en> <ship_en>', 'Set system cfg', 'WRITE'),
    ('task_list', '', 'Show registered tasks', 'READ'),
    ('uptime', '', 'Display system uptime', 'READ'),
    ('uuid_get', '', "Display device's UUID", 'READ'),
    ('vlog_set_default_levels', '', 'Reset logger levels to default', 'WRITE'),
    ('vlog_set_destination_level', '<destination> <level>', 'Set logger level on a destination side', 'WRITE'),
    ('vlog_set_source_level', '<source> <level>', 'Set logger level on a source side', 'WRITE'),
    ('vlog_unify_levels', '<usb_level>', 'Reset logger levels to default for USB', 'WRITE'),
    ('wifi_bssid_get', '', 'Get WiFi BSSID', 'READ'),
    ('wifi_conf_get', '', 'Get WiFi cfg', 'READ'),
    ('wifi_conf_set', '<ssid> <psk> <security> <band>', 'Set WiFi connection', 'DESTRUCTIVE'),
    ('wifi_eap_conf_em_set', '<eap_method>', 'Set WiFi EAP method', 'DESTRUCTIVE'),
    ('wifi_eap_conf_get', '', 'Display WiFi EAP cfg', 'READ'),
    ('wifi_eap_conf_pwd_set', '<password>', 'Set WiFi EAP password', 'DESTRUCTIVE'),
    ('wifi_eap_conf_usr_set', '<username>', 'Set WiFi EAP username', 'DESTRUCTIVE'),
    ('wifi_ext_mode_set', '<mode>', 'Sets WiFi mode', 'DESTRUCTIVE'),
    ('wifi_ext_region_set', '<region>', 'Set WiFi region cfg', 'DESTRUCTIVE'),
    ('wifi_mac_conf_get', '', 'Display WiFi MAC cfg', 'READ'),
    ('wifi_mac_conf_set', '<mac>', 'Set WiFi MAC cfg', 'DESTRUCTIVE'),
    # Hidden: absent from `help`, but present and functional. A bare call answers
    # `E: Invalid argument(s)`, not `Command ... not recognised`. Verified on
    # 7.4.4407. This is the documented way to set an SSID containing spaces,
    # and it takes the SSID alone -- no PSK needed.
    ('wifi_ssid_set', '<ssid>', 'Set WiFi SSID (hidden; handles spaces)', 'DESTRUCTIVE'),
    ('wifi_psk_set', '<psk>', 'Set wpa2 password', 'DESTRUCTIVE'),
    ('wifi_security_set', '<security>', 'Set WiFi security mode', 'DESTRUCTIVE'),
)

_ABSENT = (
    'accelerometer_conf_get',
    'accelerometer_conf_set',
    'bq24022_mode_get',
    'bq24023_mode_set',
    'bq_veps_mode_get',
    'cc3100_pwr',
    'ext_conf_get',
    'ext_conf_set',
    'flash_print',
    'frontlight_conf_get',
    'frontlight_conf_ldr_set',
    'frontlight_conf_pwm_set',
    'frontlight_conf_set',
    'fw_checksum_get',
    'get_sw_wd',
    'heater_conf_get',
    'heater_conf_set',
    'mobile_apn_set',
    'mobile_conf_get',
    'mobile_conf_set',
    'mobile_password_set',
    'mobile_security_set',
    'mobile_username_set',
    'roaming_conf_get',
    'roaming_conf_set',
    'rs9110_fw_version',
    'rs9110_mac_address',
    'rs9110_rssi',
    'rs9110_scan',
    'rs9113_fw_version',
    'rs9113_mac_address',
    'rs9113_rssi',
    'rs9113_scan',
    'scpu_config_get',
    'scpu_config_save',
    'scpu_config_set',
    'scpu_dev_batt_thr',
    'scpu_dev_ctl',
    'scpu_dev_mode',
    'scpu_dev_toff_max',
    'scpu_dev_toff_min',
    'scpu_fl_ctl',
    'scpu_fl_ldr_set',
    'scpu_fl_mode',
    'scpu_fl_postf',
    'scpu_fl_pref',
    'scpu_fl_pwm_set',
    'scpu_fl_pwmf',
    'scpu_fl_pwmlim',
    'scpu_fl_senst',
    'scpu_fl_sr',
    'scpu_ht_mode',
    'scpu_ht_toff',
    'scpu_ht_ton',
    'scpu_meas',
    'scpu_power',
    'scpu_psu_config_get',
    'scpu_psu_config_save',
    'scpu_psu_config_set',
    'scpu_psu_dev_ctl',
    'scpu_psu_dev_mode',
    'scpu_psu_disp_ctl',
    'scpu_psu_meas',
    'scpu_psu_power',
    'scpu_psu_rail_level',
    'scpu_psu_reset',
    'scpu_psu_sleep_mode',
    'scpu_psu_status',
    'scpu_psu_upgrade',
    'scpu_psu_version',
    'scpu_reset',
    'scpu_sleep_mode',
    'scpu_stats_get',
    'scpu_status',
    'scpu_upgrade',
    'scpu_version',
    'sim5320_gps_read',
    'sim5320_gps_sw',
    'sim5320_power_cmd',
    'sim5320_rssi',
    't2s_config_get',
    't2s_config_set',
    't2s_speak',
    'touch_cal_get',
    'touch_cal_set',
    'touch_fw_get',
    'touch_fw_update',
    'touch_hw_pwr',
    'touch_palm_get',
    'touch_palm_set',
    'touch_pwr_get',
    'touch_pwr_set',
    'touch_sens_get',
    'touch_sens_set',
    'touch_sr_set',
    'touch_test',
    'vcom_test',
)

# Probed by bare invocation on 7.4.4407 on 2026-10-05 and answered
# `Command '<x>' not recognised.` -- i.e. genuinely not in the firmware, not
# merely unlisted.  Every one of these takes at least one required argument in
# the vendor reference, which is what made the probe safe: argument validation
# rejects the call before the command body runs.  See PROBED_ABSENT_7_4_4407.
_PROBED_ABSENT = (
    'accelerometer_conf_set',
    'bq24023_mode_set',
    'cc3100_pwr',
    'ext_conf_get',
    'ext_conf_set',
    'frontlight_conf_ldr_set',
    'frontlight_conf_pwm_set',
    'frontlight_conf_set',
    'heater_conf_set',
    'mobile_apn_set',
    'mobile_conf_set',
    'mobile_password_set',
    'mobile_security_set',
    'mobile_username_set',
    'roaming_conf_set',
    'scpu_dev_batt_thr',
    'scpu_dev_ctl',
    'scpu_dev_mode',
    'scpu_dev_toff_max',
    'scpu_dev_toff_min',
    'scpu_fl_ctl',
    'scpu_fl_ldr_set',
    'scpu_fl_mode',
    'scpu_fl_postf',
    'scpu_fl_pref',
    'scpu_fl_pwm_set',
    'scpu_fl_pwmf',
    'scpu_fl_pwmlim',
    'scpu_fl_senst',
    'scpu_fl_sr',
    'scpu_ht_mode',
    'scpu_ht_toff',
    'scpu_ht_ton',
    'scpu_power',
    'scpu_psu_dev_ctl',
    'scpu_psu_dev_mode',
    'scpu_psu_disp_ctl',
    'scpu_psu_power',
    'scpu_psu_rail_level',
    'scpu_psu_sleep_mode',
    'scpu_sleep_mode',
    'sim5320_gps_sw',
    'sim5320_power_cmd',
    't2s_config_set',
    'touch_hw_pwr',
    'touch_palm_set',
    'touch_pwr_set',
    'touch_sens_set',
    'touch_sr_set',
)

_UNDOCUMENTED = (
    '24aa256_test',
    'bsimi',
    'cli_password_set',
    'conn_fw_ver',
    'conn_scan',
    'dcmb',
    'dcmc',
    'dcmd',
    'dcmh',
    'dcms',
    'dcmt',
    'dcmu',
    'dcmw',
    'encryption_config_get',
    'encryption_key_set',
    'encryption_mode_set',
    'eth_conf_mac_reset',
    'fs_format',
    'fs_ls',
    'fs_stats',
    'image_rotate_config_get',
    'image_rotate_config_set',
    'lmr',
    'lms',
    'm2m_mode',
    'max17135_dump',
    'max17135_pwr',
    'max17135_selftest',
    'max17135_sleep',
    'max17135_wakeup',
    'pbs',
    'pgs',
    'pts',
    'sf_list',
    'sf_rdid',
    'sf_rdst',
    'sf_select',
    'sf_unprot',
    'sf_wrst',
    'task_list',
    'vlog_set_default_levels',
    'vlog_set_destination_level',
    'vlog_set_source_level',
    'vlog_unify_levels',
    'wifi_bssid_get',
    'wifi_ext_mode_set',
    'wifi_ext_region_set',
)

_ASSERTS = {
    'sf_rdid': 'spi_flash_cli.c:139',
    'sf_rdst': 'spi_flash_cli.c:188',
}

ASSERTS_AND_KILLS_CLI: dict[str, str] = dict(_ASSERTS)
"""Commands that trip a firmware assertion and take ``usb_cli_task`` with them.

Measured on 7.4.4407, not inferred.  Both entries are in the SPI-flash family
and both read a *selected* device without checking that ``sf_select`` has
chosen one, so a bare call dereferences nothing and asserts::

    > sf_rdid
    assert: spi_flash_cli.c:139
    > sf_rdst
    assert: spi_flash_cli.c:188

The rest of the firmware survives -- the sign stays online, keeps its heartbeat
and keeps its picture -- but the console is gone until the device reboots.  The
software watchdog does notice (``W WD task timeout: cli_USB`` appears on the
UART) and does not act on it.

``sf_list`` is fine and is how you find out what there is to select.
"""

ABSENT_FROM_7_4_4407: frozenset[str] = frozenset(_ABSENT)
"""Documented by the vendor but **not listed by this firmware's** ``help``.

Membership here means "absent from ``help``", which is not automatically the
same as "absent from the firmware" -- ``wifi_ssid_set`` was in this set until a
bare invocation proved it present.  **49 of these 97 have since been probed and
are genuinely absent** (:data:`PROBED_ABSENT_7_4_4407`); the remaining 48 are
still only "unlisted".

.. warning::

   The probe is safe only for commands taking at least one **required**
   argument, because argument validation then rejects the call before anything
   happens. A bare call to a *nullary* command (``fs_format``, ``scpu_reset``,
   ``cc3100_format``, ``scpu_upgrade``) would **execute** it.  That is why the
   48 unprobed entries stay unprobed: 39 of them are nullary in the vendor
   reference, 4 have an arity the reference and the field notes disagree about
   (``vcom_test``, ``touch_test``, ``scpu_reset``, ``scpu_psu_reset``), 3 flash
   firmware, and ``t2s_speak`` would make noise if its documented arity is
   wrong.

The 97 entries are mostly whole hardware families that were not compiled in:
every ``scpu_*`` (sensor/front-light co-processor), ``touch_*`` calibration,
``t2s_*`` text-to-speech, ``frontlight_*``, ``heater_*``, ``mobile_*``,
``rs911x_*`` (the other radio), ``sim5320_*`` (cellular).  Plus three one-offs
worth knowing about: ``flash_print``, ``fw_checksum_get`` and
``accelerometer_conf_get``/``_set``.
"""

PROBED_ABSENT_7_4_4407: frozenset[str] = frozenset(_PROBED_ABSENT)
"""The subset of :data:`ABSENT_FROM_7_4_4407` **proven** absent on the device.

Each of these 49 was sent bare to firmware 7.4.4407 on 2026-10-05 and answered
``Command '<x>' not recognised.``  That is the negative arm of the same oracle
that proved ``wifi_ssid_set`` present, so these are not inferences from ``help``
-- the firmware was asked directly and said no.

The oracle was validated in the same session, before, between and after the
batch: a bare ``wifi_ssid_set`` answered ``E: Invalid argument(s)`` every time
and a nonsense name answered ``not recognised`` every time, so a false
"absent" would have had to survive a working positive control.

**Nothing new was found hidden.**  The practical upshot is that
:data:`HIDDEN_IN_7_4_4407` is a set of one on this firmware, and the hardware
families in the unlisted set are genuinely not compiled into this build -- so
the library is not refusing capabilities the device actually has.
"""

UNDOCUMENTED_IN_7_4_4407: frozenset[str] = frozenset(_UNDOCUMENTED)
"""The 47 commands this firmware has that the vendor's reference omits.

Three groups, and the first two are the interesting ones:

* **Encryption** -- ``encryption_config_get``, ``encryption_key_set``,
  ``encryption_mode_set``.  The only documentation of the sign's outbound
  encryption anywhere outside the firmware itself.  See
  :mod:`pyvisionect.io.usb.encryption`.
* **Filesystem and logging** -- ``fs_ls``, ``fs_stats``, ``fs_format``, and the
  four ``vlog_*`` level controls that can silence the asynchronous log stream.
* Hardware pokes: ``dcm*`` (display driver), ``max17135_*`` (PMIC),
  ``sf_*`` (SPI flash), ``lmr``/``lms`` (temperature), ``24aa256_test``
  (EEPROM), ``bsim*`` (battery simulation), ``pbs``/``pgs``/``pts`` (synthetic
  button/GPS/touch events), ``task_list``.
"""

COMMANDS: dict[str, Command] = {
    name: Command(
        name=name,
        args=args,
        description=description,
        kind=Kind(kind),
        documented=name not in UNDOCUMENTED_IN_7_4_4407,
        asserts=ASSERTS_AND_KILLS_CLI.get(name),
    )
    for name, args, description, kind in _ROWS
}
"""Every command firmware 7.4.4407 is known to answer, keyed by name.

Mostly built from the device's own ``help`` rather than the vendor's reference --
but ``help`` is not a complete index, so this table is a superset of it: see
:data:`HIDDEN_IN_7_4_4407`.
"""

HIDDEN_IN_7_4_4407: frozenset[str] = frozenset({"wifi_ssid_set"})
"""Present and working, but **not listed by** ``help``.

Found by bare invocation: a hidden command answers ``E: Invalid argument(s)``
while a genuinely missing one answers ``Command '...' not recognised.``.

**Still a set of one, and now on evidence rather than for want of looking.**
49 of the 97 unlisted commands -- every one the oracle can safely be pointed at
-- were probed on 2026-10-05 and all 49 were genuinely absent
(:data:`PROBED_ABSENT_7_4_4407`).  The 48 that remain unprobed are nullary,
firmware-flashing, or of disputed arity, so this set could still grow, but not
by much and not cheaply: ``flash_print`` is the one worth wanting and it is
exactly the kind the oracle cannot touch.
"""

#: Commands this firmware's ``help`` actually prints.
LISTED_BY_HELP: frozenset[str] = frozenset(COMMANDS) - HIDDEN_IN_7_4_4407

assert len(LISTED_BY_HELP) == 111, f"help on 7.4.4407 lists 111, got {len(LISTED_BY_HELP)}"
assert len(COMMANDS) == 111 + len(HIDDEN_IN_7_4_4407)
assert HIDDEN_IN_7_4_4407 <= frozenset(COMMANDS)
assert not (HIDDEN_IN_7_4_4407 & ABSENT_FROM_7_4_4407), "a hidden command is not absent"
assert len(ABSENT_FROM_7_4_4407) == 97
assert len(UNDOCUMENTED_IN_7_4_4407) == 47
assert len(PROBED_ABSENT_7_4_4407) == 49
assert PROBED_ABSENT_7_4_4407 <= ABSENT_FROM_7_4_4407, "probed-absent is a subset of unlisted"
assert PROBED_ABSENT_7_4_4407.isdisjoint(COMMANDS), "proven absent, so not in the table"
# Documented = present-and-documented + unlisted. Keeps the three counts in step:
# this is the arithmetic that was wrong while DOCUMENTED_COUNT said 160.
assert (
    len(frozenset(COMMANDS) - UNDOCUMENTED_IN_7_4_4407) + len(ABSENT_FROM_7_4_4407)
    == DOCUMENTED_COUNT
)

READ_COMMANDS: frozenset[str] = frozenset(
    name for name, c in COMMANDS.items() if c.kind is Kind.READ
)
"""The commands that change nothing. Always allowed, ``allow_writes`` or not."""

DESTRUCTIVE_COMMANDS: frozenset[str] = frozenset(
    name for name, c in COMMANDS.items() if c.kind is Kind.DESTRUCTIVE
)
"""Needs ``allow_destructive``. See the tier list in the module docstring."""

HARD_COMMANDS: frozenset[str] = frozenset(
    name for name, c in COMMANDS.items() if c.kind is Kind.HARD
)
"""Needs ``i_really_mean_it``: formats, firmware flashes, feature keys, passwords."""

TCLV_FOR: dict[str, tuple[int, ...]] = {
    "battery_conf_set": (36, 37, 38),
    "border_get": (109,),
    "cli_password_set": (126,),
    "conn_retry_set": (30,),
    "conn_type_set": (2,),
    "display_conf_set": (20, 21, 22, 23, 24, 25, 26, 27, 28),
    "encryption_key_set": (131,),
    "encryption_mode_set": (130,),
    "eth_conf_ext_set": (8, 9, 10),
    "eth_conf_set": (8,),
    "feat_disable": (78,),
    "feat_enable": (77,),
    "flash_save": (53,),
    "fs_format": (157,),
    "gtin_get": (108,),
    "ipv4_conf_set": (13, 14, 15, 16, 17),
    "m2m_mode": (171,),
    "server_hb_set": (29,),
    "server_tcp_set": (18, 19),
    "system_conf_set": (49, 50, 51),
    "uuid_get": (89,),
    "wifi_bssid_get": (170,),
    "wifi_conf_set": (65, 67, 66, 68),
    "wifi_eap_conf_em_set": (74,),
    "wifi_eap_conf_pwd_set": (75,),
    "wifi_eap_conf_usr_set": (76,),
    "wifi_ext_mode_set": (173,),
    "wifi_ext_region_set": (144,),
    "wifi_mac_conf_set": (110,),
    "wifi_psk_set": (67,),
    "wifi_security_set": (66,),
    "wifi_ssid_set": (65,),
}
"""TCLV ids each command touches, in argument order where there are several.

Only the unambiguous ones.  ``wifi_conf_set <ssid> <psk> <security> <band>`` is
``(65, 67, 66, 68)`` -- note that is *not* ascending, because the command's
argument order is SSID, password, security, band while the ids run SSID,
security, password, band.  Getting that backwards writes the PSK into the
security field.

``wifi_ssid_set`` is in here too, even though ``help`` never mentions it: TCLV
65 is the id it writes, and :data:`pyvisionect.devices.tclv.USB_HINTS` has named
it as the setter for 65 all along.

Cross-reference :mod:`pyvisionect.devices.tclv`: every id here is in that table,
and :data:`~pyvisionect.devices.tclv.NETWORK_READ_ONLY` says which of them the
network protocol refuses to write -- which is the whole reason this module
exists.
"""


def lookup(name: str) -> Command | None:
    """The :class:`Command` for *name*, or None if this firmware lacks it.

    Case-insensitive, matching the device.
    """
    return COMMANDS.get(name.strip().lower())
