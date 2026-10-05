'''The typed command surface: :class:`Sign`.

One method per useful command, each returning a dataclass from
:mod:`pyvisionect.io.usb.parsers` rather than a string.  The gating mirrors
:func:`pyvisionect.devices.tclv.check_writable`: a refusal names the flag that
would allow the call, because a bare "permission denied" just makes the caller
go and read the source.

Three flags, three tiers
------------------------

``allow_writes=False`` (the default)
    Only :data:`~pyvisionect.io.usb.commands.READ_COMMANDS` go out.  A
    :class:`Sign` built this way cannot change anything on the device, which is
    the state you want while exploring somebody's hardware.

``allow_destructive=False``
    Gates the commands that can corrupt the panel, drop the sign off the network
    or make it unreachable -- ``display_conf_set``, every network setter,
    ``app_sleep``, ``reboot``, ``flash_save``, the ``dcm*`` display-driver pokes
    and the battery/temperature simulators.  Implies ``allow_writes``.

``i_really_mean_it=False``
    Gates ``fs_format``, ``cc3100_format``, ``cc3100_fw_upgrade``,
    ``feat_enable``/``feat_disable``, ``cli_password_set``, ``sf_unprot`` and
    ``sf_wrst``.  Formats, firmware flashes, licence keys and a password you
    then have to know.  Implies both of the above.

Nothing persists until ``flash_save``
-------------------------------------

Verbatim from the vendor: *"All changes to device configuration are by default
retained only in the working RAM."*  So a setter on its own is reversible by
power-cycling, and ``flash_save`` is the point of no return.  That is why
``flash_save`` sits in the ``DESTRUCTIVE`` tier alongside the setters it
commits, rather than being treated as a harmless bookkeeping call.

Commands this firmware does not have
------------------------------------

:meth:`Sign.refresh_commands` reads ``help`` and remembers the result, after
which an accessor for a missing command raises
:class:`~pyvisionect.io.usb.errors.CommandNotInFirmware` locally, without
writing anything.  Worth doing once per session: 96 of the 160 documented
commands are absent from 7.4.4407, including ``wifi_ssid_set`` and
``flash_print``.
'''

from __future__ import annotations

import logging
import uuid as _uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from . import parsers as P
from .commands import COMMANDS, Kind, lookup
from .console import CommandResult, SerialConsole
from .errors import (
    CommandNotInFirmware,
    ConsoleAssertionFailed,
    DangerousCommandRefused,
    WriteNotAllowed,
)

__all__ = ["Sign", "SignInfo"]

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SignInfo:
    """Everything :meth:`Sign.dump` could read, in one object.

    Every field is optional: a command may be absent from the firmware, or may
    fail, and a dump that aborted on the first missing command would be useless
    on exactly the hardware you most want to inspect.  :attr:`errors` records
    what went wrong per command so a partial dump is still honest about being
    partial.
    """

    uuid: _uuid.UUID | None = None
    gtin: str | None = None
    firmware: P.FirmwareVersion | None = None
    cli_version: str | None = None
    uptime_minutes: int | None = None
    features: P.Features | None = None
    encryption: P.EncryptionConfig | None = None
    display: P.DisplayConfig | None = None
    border: P.BorderConfig | None = None
    image_rotate: P.ImageRotateConfig | None = None
    connectivity: P.ConnectivityType | None = None
    connectivity_options: tuple[P.ConnectivityOption, ...] = ()
    connectivity_state: P.ConnectivityState | None = None
    connectivity_firmware: P.ConnectivityFirmware | None = None
    conn_retry_minutes: int | None = None
    wifi: P.WifiConfig | None = None
    wifi_mac: P.WifiMacConfig | None = None
    wifi_firmware: P.WifiFirmware | None = None
    bssid: str | None = None
    rssi_dbm: int | None = None
    eap: P.EapConfig | None = None
    certificates: P.CertificateConfig | None = None
    ipv4: P.Ipv4Config | None = None
    ethernet: P.EthernetConfig | None = None
    server: P.ServerConfig | None = None
    battery: P.BatteryConfig | None = None
    charger: P.ChargerState | None = None
    temperature_c: int | None = None
    system: P.SystemConfig | None = None
    logging_config: P.LogConfig | None = None
    files: tuple[P.FileEntry, ...] = ()
    filesystem: P.FilesystemStats | None = None
    spi_flash: tuple[P.SpiFlashDevice, ...] = ()
    tasks: P.TaskList | None = None
    status: P.StatusDump | None = None
    errors: Mapping[str, str] = field(default_factory=dict)

    def describe(self) -> str:
        """A short human summary, for a CLI banner or a log line."""
        bits: list[str] = []
        if self.uuid is not None:
            bits.append(str(self.uuid))
        if self.firmware is not None:
            bits.append(f"fw {self.firmware.firmware}")
            if self.firmware.hardware:
                bits.append(self.firmware.hardware)
            if self.firmware.app:
                bits.append(f"app {self.firmware.app}")
        if self.server is not None and self.server.host:
            bits.append(f"-> {self.server.host}:{self.server.port}")
        if self.connectivity is not None:
            bits.append(f"via {self.connectivity.name}")
        return ", ".join(bits) or "unidentified device"


class Sign:
    """A typed client for one sign's serial console.

    Args:
        console: an open :class:`~pyvisionect.io.usb.console.SerialConsole`.
        allow_writes: let ordinary setters and actions through.
        allow_destructive: let the panel/network/lifecycle commands through.
        i_really_mean_it: let the format/flash/password commands through.

    Example:
        >>> from pyvisionect.io.usb import SerialConsole, Sign
        >>> with SerialConsole("/dev/ttyUSB0") as console:   # doctest: +SKIP
        ...     sign = Sign(console)
        ...     console.sync()
        ...     print(sign.uuid())
        ...     print(sign.firmware_version().firmware)
        ...     print(sign.dump().describe())
    """

    def __init__(
        self,
        console: SerialConsole,
        *,
        allow_writes: bool = False,
        allow_destructive: bool = False,
        i_really_mean_it: bool = False,
    ) -> None:
        self.console = console
        self.allow_writes = allow_writes or allow_destructive or i_really_mean_it
        self.allow_destructive = allow_destructive or i_really_mean_it
        self.i_really_mean_it = i_really_mean_it
        self._firmware_commands: frozenset[str] | None = None

    # ------------------------------------------------------------ gatekeeping

    def _check(self, command: str) -> None:
        """Refuse a command whose tier is above the flags this object was given."""
        name = command.split()[0].lower() if command.split() else command
        entry = lookup(name)
        kind = entry.kind if entry is not None else Kind.WRITE
        if entry is not None and entry.asserts and not self.i_really_mean_it:
            raise ConsoleAssertionFailed(name, entry.asserts)
        if kind is Kind.READ:
            return
        if kind is Kind.HARD and not self.i_really_mean_it:
            raise DangerousCommandRefused(
                name,
                "i_really_mean_it=True",
                "formats, flashes firmware, or sets a credential you will then "
                "need in order to undo it",
            )
        if kind is Kind.DESTRUCTIVE and not self.allow_destructive:
            raise DangerousCommandRefused(
                name,
                "allow_destructive=True",
                "can corrupt the panel, strand the sign off the network, or make "
                "it unreachable (IPV4_SERVER_IP is USB-only to recover)",
            )
        if not self.allow_writes:
            raise WriteNotAllowed(
                name, "allow_writes=True", "this console was opened read-only"
            )

    def refresh_commands(self) -> frozenset[str]:
        """Read ``help`` and remember which commands this firmware has.

        After this, an accessor for a missing command raises
        :class:`~pyvisionect.io.usb.errors.CommandNotInFirmware` without
        writing anything to the port.

        Returns:
            The command names, which is also stored as
            :attr:`firmware_commands`.
        """
        import re

        text = self.console.command("help").text
        names: set[str] = set()
        for line in text.splitlines():
            match = re.match(r"^([a-z0-9_]+)(?:\s+<[^:]*>)?\s*[: ]", line.strip())
            if match:
                names.add(match.group(1))
        names.discard("rv")
        self._firmware_commands = frozenset(names)
        return self._firmware_commands

    @property
    def firmware_commands(self) -> frozenset[str] | None:
        """What :meth:`refresh_commands` found, or None if it was never called."""
        return self._firmware_commands

    def has(self, command: str) -> bool | None:
        """Whether this firmware has *command*.

        Returns None when :meth:`refresh_commands` has not been called, so a
        caller can tell "no" from "unknown" -- which matters, because the
        library's built-in table describes *one* firmware build and yours may
        differ.
        """
        if self._firmware_commands is None:
            return None
        return command in self._firmware_commands

    def _run(self, command: str, *, check: bool = True, **kwargs: Any) -> CommandResult:
        if check and self._firmware_commands is not None:
            name = command.split()[0].lower() if command.split() else command
            if name not in self._firmware_commands:
                raise CommandNotInFirmware(name)
        self._check(command)
        return self.console.command(command, **kwargs)

    # --------------------------------------------------------------- identity

    def uuid(self) -> _uuid.UUID:
        """``uuid_get`` -- the 16-byte identity the packet header carries.

        Factory-programmed; there is no setter, over USB or over the network.
        """
        return P.parse_uuid(self._run("uuid_get").lines)

    def gtin(self) -> str:
        """``gtin_get`` (TCLV 108), as a string: leading zeros are significant."""
        return P.parse_gtin(self._run("gtin_get").lines)

    def firmware_version(self) -> P.FirmwareVersion:
        """``fw_version_get`` -- app, bootloader, board, BOM, JS app and panel."""
        return P.parse_firmware_version(self._run("fw_version_get").lines)

    def cli_version(self) -> str:
        """``cli_version_get`` -- ``"1.2"`` on firmware 7.4.4407."""
        return self._run("cli_version_get").field("CLI version")

    def uptime(self) -> int:
        """``uptime``, in **minutes**. Same unit as the wire's ``DeviceUptime``."""
        return P.parse_uptime(self._run("uptime").lines)

    def features(self) -> P.Features:
        """``feat_get`` -- which licensed features are on."""
        return P.parse_features(self._run("feat_get").lines)

    # ------------------------------------------------------------- encryption

    def encryption_config(self) -> P.EncryptionConfig:
        """``encryption_config_get`` -- outbound encryption mode and key.

        Read-only and safe.  See :mod:`pyvisionect.io.usb.encryption` for the
        state of knowledge about the key format; the setters are gated at the
        ``DESTRUCTIVE`` tier because enabling encryption with a key the server
        does not share silently takes the sign off its server.
        """
        return P.parse_encryption_config(self._run("encryption_config_get").lines)

    # ---------------------------------------------------------------- display

    def display_config(self) -> P.DisplayConfig:
        """``display_conf_get`` -- the VCOM rails (8, not 16) and display type."""
        return P.parse_display_config(self._run("display_conf_get").lines)

    def border(self) -> P.BorderConfig:
        """``border_get`` (TCLV 109)."""
        return P.parse_border(self._run("border_get").lines)

    def image_rotate_config(self) -> P.ImageRotateConfig:
        """``image_rotate_config_get`` -- the local image carousel."""
        return P.parse_image_rotate_config(self._run("image_rotate_config_get").lines)

    # ----------------------------------------------------------- connectivity

    def connectivity(self) -> P.ConnectivityType:
        """``conn_type_get`` -- the live driver and its ``CONN_TYPE`` value."""
        return P.parse_conn_type(self._run("conn_type_get").lines)

    def connectivity_options(self) -> tuple[P.ConnectivityOption, ...]:
        """``conn_type_list`` -- the **only** trustworthy ``CONN_TYPE`` enum.

        This firmware offers three (``W5500 (3)``, ``CC3100 (6)``, ``None (0)``)
        where the vendor's table lists eight.  Call this before
        :meth:`set_connectivity_type`.
        """
        return P.parse_conn_type_list(self._run("conn_type_list").lines)

    def connectivity_state(self) -> P.ConnectivityState:
        """``conn_state_get`` -- ``"tcp open"`` when the sign holds its session."""
        return P.parse_conn_state(self._run("conn_state_get").lines)

    def connectivity_firmware(self) -> P.ConnectivityFirmware:
        """``conn_fw_ver``. Mostly uninitialised; prefer :meth:`wifi_firmware`."""
        return P.parse_conn_firmware(self._run("conn_fw_ver").lines)

    def conn_retry(self) -> int:
        """``conn_retry_get`` (TCLV 30), in minutes."""
        return P.parse_conn_retry(self._run("conn_retry_get").lines)

    # -------------------------------------------------------------------- WiFi

    def wifi_config(self) -> P.WifiConfig:
        """``wifi_conf_get`` -- SSID, security, band. Does **not** echo the PSK."""
        return P.parse_wifi_config(self._run("wifi_conf_get").lines)

    def wifi_mac(self) -> P.WifiMacConfig:
        """``wifi_mac_conf_get`` (TCLV 110)."""
        return P.parse_wifi_mac_config(self._run("wifi_mac_conf_get").lines)

    def wifi_firmware(self) -> P.WifiFirmware:
        """``cc3100_fw_version`` -- the radio's NWP/MAC/PHY versions."""
        return P.parse_wifi_firmware(self._run("cc3100_fw_version").lines)

    def wifi_mac_address(self) -> str:
        """``cc3100_mac_address`` -- the radio's MAC, straight from the chip.

        Same value as :meth:`wifi_mac`, which reads the stored TCLV 110.
        """
        return P.parse_mac(self._run("cc3100_mac_address").one())

    def bssid(self) -> str:
        """``wifi_bssid_get`` (TCLV 170) -- the AP currently associated.

        Cross-checks against :attr:`~pyvisionect.io.usb.parsers.StatusDump.bssid`.
        """
        return P.parse_bssid(self._run("wifi_bssid_get").lines)

    def rssi(self) -> int:
        """``cc3100_rssi`` -- **signed** dBm."""
        return P.parse_rssi(self._run("cc3100_rssi").lines)

    def eap_config(self) -> P.EapConfig:
        """``wifi_eap_conf_get`` -- WPA2-Enterprise method and username."""
        return P.parse_eap_config(self._run("wifi_eap_conf_get").lines)

    def certificates(self) -> P.CertificateConfig:
        """``certs_config_get`` -- the EAP certificate slots (TCLV 60-64)."""
        return P.parse_certs_config(self._run("certs_config_get").lines)

    # --------------------------------------------------------- network/server

    def ipv4_config(self) -> P.Ipv4Config:
        """``ipv4_conf_get`` -- the **stored static** block, not the live lease."""
        return P.parse_ipv4_config(self._run("ipv4_conf_get").lines)

    def ethernet_config(self) -> P.EthernetConfig:
        """``eth_conf_get`` -- the W5500's MAC and retry policy."""
        return P.parse_eth_config(self._run("eth_conf_get").lines)

    def server_config(self) -> P.ServerConfig:
        """``server_tcp_get`` plus ``server_hb_get`` -- where the sign dials out.

        Both reads, so this is safe on a read-only console even though the pair
        it reads is the pair that USB provisioning exists to write.
        """
        tcp = self._run("server_tcp_get").lines
        try:
            hb = self._run("server_hb_get").lines
        except Exception:  # pragma: no cover - server_hb_get is present on 7.4.4407
            hb = None
        return P.parse_server_config(tcp, hb)

    def heartbeat(self) -> int:
        """``server_hb_get`` (TCLV 29), in minutes."""
        return P.parse_heartbeat(self._run("server_hb_get").lines)

    # ---------------------------------------------------------- power/hardware

    def battery_config(self) -> P.BatteryConfig:
        """``battery_conf_get`` -- the shutdown thresholds (TCLV 36/37/38)."""
        return P.parse_battery_config(self._run("battery_conf_get").lines)

    def charger(self) -> P.ChargerState:
        """``bq24023_mode_get`` -- live charge mode, current and pack voltage."""
        return P.parse_charger(self._run("bq24023_mode_get").lines)

    def temperature(self) -> int:
        """``lmr`` -- the LM75 board sensor, whole degrees Celsius."""
        return P.parse_temperature(self._run("lmr").lines)

    def system_config(self) -> P.SystemConfig:
        """``system_conf_get`` -- battery screens, touch mode, shipping mode."""
        return P.parse_system_config(self._run("system_conf_get").lines)

    def log_config(self) -> P.LogConfig:
        """``log_config_get`` -- per-module debug levels."""
        return P.parse_log_config(self._run("log_config_get").lines)

    def pmic_dump(self, i2c_channel: int) -> P.PmicRegisters:
        """``max17135_dump <i2c_channel>`` -- the display PMIC's registers.

        ``help`` lists this nullary and a bare call is rejected, so the channel
        is required here.  **The reply shape is unverified**: the channel was not
        guessed at on the sign this library was written against, because its
        sibling commands power the panel rails.  See :class:`~pyvisionect.io.usb.parsers.PmicRegisters`.
        """
        return P.parse_pmic_dump(self._run(f"max17135_dump {i2c_channel}").lines)

    # ----------------------------------------------------------- filesystem

    def files(self) -> tuple[P.FileEntry, ...]:
        """``fs_ls`` -- the ``.pv2`` images stored on the device."""
        return P.parse_fs_ls(self._run("fs_ls").lines)

    def filesystem_stats(self) -> P.FilesystemStats:
        """``fs_stats`` -- block usage. 4096 bytes per block on this hardware."""
        return P.parse_fs_stats(self._run("fs_stats").lines)

    def spi_flash_devices(self) -> tuple[P.SpiFlashDevice, ...]:
        """``sf_list`` -- the registered SPI flash chips and their SPI channels.

        The safe member of the family.  :meth:`spi_flash_id` and
        :meth:`spi_flash_status` are not.
        """
        return P.parse_sf_list(self._run("sf_list").lines)

    def spi_flash_id(self) -> CommandResult:
        """``sf_rdid``. **Do not call this.**

        Implemented so the surface is complete and so the hazard is documented
        where someone will find it, but it asserts at
        ``spi_flash_cli.c:139`` on firmware 7.4.4407 and kills ``usb_cli_task``
        -- the console is gone until the device reboots, which it will do on its
        own roughly 25 minutes later when the software watchdog gives up.

        Gated behind ``i_really_mean_it`` for that reason alone; it reads
        nothing and writes nothing.

        Raises:
            ConsoleAssertionFailed: immediately, without sending anything,
                unless ``i_really_mean_it=True``.
        """
        return self._run("sf_rdid", expect_prompt=False)

    def spi_flash_status(self) -> CommandResult:
        """``sf_rdst``. **Do not call this** -- asserts at ``spi_flash_cli.c:188``.

        Same hazard as :meth:`spi_flash_id`.
        """
        return self._run("sf_rdst", expect_prompt=False)

    # ---------------------------------------------------------------- runtime

    def tasks(self) -> P.TaskList:
        """``task_list`` -- the FreeRTOS tasks and the free heap."""
        return P.parse_task_list(self._run("task_list").lines)

    def status(self) -> P.StatusDump:
        """``status_get`` -- the whole PV3 status record, as text.

        The same information a type-3 heartbeat carries, without waiting for one
        or decoding a frame.
        """
        return P.parse_status_dump(self._run("status_get").lines)

    # ----------------------------------------------------------------- setters

    def set_wifi(
        self, ssid: str, psk: str, security: str = "wpa2", band: int = 0
    ) -> CommandResult:
        '''``wifi_conf_set <ssid> <psk> <security> <band>`` -- TCLV 65/67/66/68.

        Note the argument order is SSID, **password**, security, band, while the
        TCLV ids run SSID, security, password: ``(65, 67, 66, 68)``.

        **An SSID or PSK containing whitespace cannot be set on this firmware.**
        The vendor documents ``wifi_ssid_set`` as the escape hatch for exactly
        that case, and firmware 7.4.4407 does not have it -- the command list is
        ``wifi_conf_set``, ``wifi_psk_set`` and ``wifi_security_set``, none of
        which can carry a space through a whitespace-delimited parser.  This
        method refuses rather than silently truncating, and
        :func:`pyvisionect.io.usb.provisioning.plan_wifi` says the same thing
        before anything is sent.

        Raises:
            ValueError: if *ssid* or *psk* contains whitespace.
        '''
        for label, value in (("ssid", ssid), ("psk", psk)):
            if any(c.isspace() for c in value):
                raise ValueError(
                    f"{label} contains whitespace, which the single-line "
                    f"whitespace-delimited CLI cannot carry. The documented "
                    f"workaround is wifi_ssid_set, which firmware 7.4.4407 does "
                    f"not have -- so on this firmware such an {label} cannot be "
                    f"set over USB at all. Rename the network, or point the sign "
                    f"at a new server by DNS instead."
                )
        return self._run(f"wifi_conf_set {ssid} {psk} {security} {band}")

    def set_wifi_psk(self, psk: str) -> CommandResult:
        """``wifi_psk_set <psk>`` -- TCLV 67. Whitespace is still impossible."""
        if any(c.isspace() for c in psk):
            raise ValueError("a PSK containing whitespace cannot be sent over this CLI")
        return self._run(f"wifi_psk_set {psk}")

    def set_wifi_security(self, security: str) -> CommandResult:
        """``wifi_security_set <none|wpa2|wpa2e>`` -- TCLV 66, an ASCII string."""
        return self._run(f"wifi_security_set {security}")

    def set_server(self, host: str, port: int = 11113) -> CommandResult:
        """``server_tcp_set <ip|dns> <port>`` -- TCLV 18 and 19.

        A DNS name works and is what the live sign holds, which is why
        re-pointing that name is a no-touch alternative to writing this field.
        """
        return self._run(f"server_tcp_set {host} {port}")

    def set_connectivity_type(self, value: int) -> CommandResult:
        """``conn_type_set <type>`` -- TCLV 2.

        Enumerate with :meth:`connectivity_options` first.  The vendor's own 4G
        example uses 8 and this firmware offers only 0, 3 and 6.
        """
        return self._run(f"conn_type_set {value}")

    def set_heartbeat(self, minutes: int) -> CommandResult:
        """``server_hb_set <time>`` -- TCLV 29. Network-writable too, unusually."""
        return self._run(f"server_hb_set {minutes}")

    def set_conn_retry(self, minutes: int) -> CommandResult:
        """``conn_retry_set <time>`` -- TCLV 30."""
        return self._run(f"conn_retry_set {minutes}")

    def set_system_config(
        self, battery_mode: int, touch_mode: int, shipping_mode: int = 0
    ) -> CommandResult:
        """``system_conf_set <batt_ind_en> <touch_en> <ship_en>`` -- TCLV 49/50/51.

        Raises:
            DangerousCommandRefused: for ``shipping_mode=1`` without
                ``i_really_mean_it``. A sign in shipping mode does not wake up
                normally, and the argument -- not the command name -- is what
                makes the call dangerous, so it is checked separately.
        """
        if shipping_mode and not self.i_really_mean_it:
            raise DangerousCommandRefused(
                "system_conf_set",
                "i_really_mean_it=True",
                "ship_en=1 enables shipping mode, which the sign does not wake "
                "out of normally",
            )
        return self._run(
            f"system_conf_set {battery_mode} {touch_mode} {shipping_mode}"
        )

    def set_ipv4(
        self, ip: str, netmask: str, gateway: str, dns: str, mode: int
    ) -> CommandResult:
        """``ipv4_conf_set <ip> <nm> <gw> <dns> <mode>`` -- TCLV 13-17.

        ``mode`` is 0 for static and 1 for DHCP.
        """
        return self._run(f"ipv4_conf_set {ip} {netmask} {gateway} {dns} {mode}")

    def set_display_config(
        self, vcoms: Sequence[int], display_id: int
    ) -> CommandResult:
        """``display_conf_set <vcom1> ... <vcom16> <display_id>``.

        ``help`` demands 16 VCOM arguments even though ``display_conf_get``
        prints 8 and TCLV defines ``VCOM_0..7``.  Both counts are accepted here
        and 8 is padded out to 16 with zeros, because an 8-element list is what
        a round-trip from :meth:`display_config` gives you.

        **A wrong VCOM can damage an e-ink panel**, which is why this sits in
        the ``DESTRUCTIVE`` tier.
        """
        values = list(vcoms)
        if len(values) not in (8, 16):
            raise ValueError(
                f"display_conf_set takes 8 or 16 VCOM values, got {len(values)}"
            )
        if len(values) == 8:
            values = values + [0] * 8
        args = " ".join(str(v) for v in values)
        return self._run(f"display_conf_set {args} {display_id}")

    def set_battery_config(
        self, threshold_off_mv: int, threshold_on_mv: int, count: int
    ) -> CommandResult:
        """``battery_conf_set <thr_off> <thr_on> <thr_cnt>`` -- TCLV 36/37/38."""
        return self._run(
            f"battery_conf_set {threshold_off_mv} {threshold_on_mv} {count}"
        )

    def flash_save(self) -> CommandResult:
        """``flash_save`` (TCLV 53) -- **the point of no return**.

        Everything a setter did was RAM-only until this runs.  That cuts both
        ways: it is what makes a mistake permanent, and it is also what makes a
        mistake recoverable right up until you call it.
        """
        return self._run("flash_save")

    def flash_load(self) -> CommandResult:
        """``flash_load`` -- discard RAM changes and reload from flash.

        The undo for an uncommitted setter, which is why it is worth knowing
        about before you need it.
        """
        return self._run("flash_load")

    def reboot(self) -> CommandResult:
        """``reboot``. The link drops, so no prompt comes back.

        The sign takes roughly a minute to come back and chirps when it does
        (with ``touch_mode`` 3, which is the factory default here).
        """
        return self._run("reboot", expect_prompt=False)

    def unify_usb_log_levels(self, level: int) -> CommandResult:
        """``vlog_unify_levels <usb_level>`` -- set one level for every log source
        on the USB destination.

        **The direction of the level scale is unknown, so this may make the
        asynchronous log stream louder rather than quieter.**  Read the whole of
        this docstring before calling it.

        What the device says about it, in full -- this is the entire
        documentation that exists anywhere, because the command is absent from
        the vendor's published reference::

            vlog_unify_levels <usb_level>: Reset logger levels to default for USB

        What that supports.  The ``vlog_*`` family models logging as a matrix:
        ``vlog_set_source_level <source> <level>`` sets a producer's level and
        ``vlog_set_destination_level <destination> <level>`` sets a sink's, so
        every (source, sink) pair has an effective level.  "Unify", plus a single
        level argument, plus "for USB", reads as *collapse one column of that
        matrix to a single value* -- every source set to ``level`` on the USB
        sink.

        What it does **not** support, and why this method takes a mandatory
        argument instead of defaulting to 0:

        * **Which end of the scale is quiet is not known.**  ``0`` may mean "emit
          nothing" or it may mean "emit everything".  If it is the latter, this
          call floods the port.  Nothing observed on hardware distinguishes the
          two: the only level value ever seen is ``log_config_get`` reporting
          ``Mobile: 0``, and the Mobile subsystem is not in use on a WiFi sign,
          so that 0 is equally consistent with "off" and with "default".
        * **The help line contradicts its own signature.**  "Reset ... to
          default" describes a command that ignores its argument; ``<usb_level>``
          describes one that uses it. One of the two is wrong, and this
          firmware's help text is demonstrably unreliable elsewhere --
          ``conn_fw_ver`` is described as "Scan for WiFi APs" and
          ``max17135_dump`` is listed as nullary and rejects a bare call.

        So this is the lever that *should* solve the interleaving problem
        :mod:`pyvisionect.io.usb.console` works around, and it is here so it can
        be tried, but trying it is an experiment and not a fix.  Do it with a
        capture running and :meth:`default_logs` ready to undo it, and note that
        nothing here persists without ``flash_save``.

        Args:
            level: the level to apply. Deliberately has no default.
        """
        return self._run(f"vlog_unify_levels {level}")

    def set_log_source_level(self, source: int | str, level: int) -> CommandResult:
        """``vlog_set_source_level <source> <level>`` -- one producer's level.

        The *source* namespace was never enumerated: no command lists it, and
        ``log_config_get`` reports a single module (``Mobile``) which may or may
        not share that namespace. Unverified, like the level scale.
        """
        return self._run(f"vlog_set_source_level {source} {level}")

    def set_log_destination_level(
        self, destination: int | str, level: int
    ) -> CommandResult:
        """``vlog_set_destination_level <destination> <level>`` -- one sink's level.

        Sinks plausibly include the USB UART, the filesystem (TCLV 161 flushes a
        syslog to it) and the network link, but the namespace was never
        enumerated and no value for *destination* has been observed.
        """
        return self._run(f"vlog_set_destination_level {destination} {level}")

    def default_logs(self) -> CommandResult:
        """``vlog_set_default_levels`` -- put the whole level matrix back.

        The undo for the three ``vlog`` setters above. Nullary, and the one
        member of the family whose help line and signature agree.
        """
        return self._run("vlog_set_default_levels")

    def send_status_packet(self) -> CommandResult:
        """``pss`` -- make the sign emit a PV3 status packet now.

        Very useful while bringing up a listener: it decouples protocol work
        from the one-minute heartbeat.  An ``ACTION``, so it needs
        ``allow_writes`` even though it changes nothing.
        """
        return self._run("pss")

    # -------------------------------------------------------------------- dump

    def dump(self, *, include_status: bool = True) -> SignInfo:
        """Read everything readable, in one pass, tolerating failures.

        This is the replacement for ``flash_print``, which firmware 7.4.4407
        does not have: there is no single-command settings dump, so this walks
        the per-area getters instead.  Only ``READ`` commands are issued, so it
        is safe on a read-only console.

        A command that is missing, errors, or returns something a parser cannot
        read is recorded in :attr:`SignInfo.errors` and the dump continues.

        Returns:
            A :class:`SignInfo`.
        """
        errors: dict[str, str] = {}

        def attempt(name: str, call: Any, default: Any = None) -> Any:
            try:
                return call()
            except Exception as exc:
                errors[name] = f"{type(exc).__name__}: {exc}"
                log.debug("dump: %s failed: %s", name, exc)
                return default

        info = SignInfo(
            uuid=attempt("uuid_get", self.uuid),
            gtin=attempt("gtin_get", self.gtin),
            firmware=attempt("fw_version_get", self.firmware_version),
            cli_version=attempt("cli_version_get", self.cli_version),
            uptime_minutes=attempt("uptime", self.uptime),
            features=attempt("feat_get", self.features),
            encryption=attempt("encryption_config_get", self.encryption_config),
            display=attempt("display_conf_get", self.display_config),
            border=attempt("border_get", self.border),
            image_rotate=attempt("image_rotate_config_get", self.image_rotate_config),
            connectivity=attempt("conn_type_get", self.connectivity),
            connectivity_options=attempt(
                "conn_type_list", self.connectivity_options, ()
            ),
            connectivity_state=attempt("conn_state_get", self.connectivity_state),
            connectivity_firmware=attempt("conn_fw_ver", self.connectivity_firmware),
            conn_retry_minutes=attempt("conn_retry_get", self.conn_retry),
            wifi=attempt("wifi_conf_get", self.wifi_config),
            wifi_mac=attempt("wifi_mac_conf_get", self.wifi_mac),
            wifi_firmware=attempt("cc3100_fw_version", self.wifi_firmware),
            bssid=attempt("wifi_bssid_get", self.bssid),
            rssi_dbm=attempt("cc3100_rssi", self.rssi),
            eap=attempt("wifi_eap_conf_get", self.eap_config),
            certificates=attempt("certs_config_get", self.certificates),
            ipv4=attempt("ipv4_conf_get", self.ipv4_config),
            ethernet=attempt("eth_conf_get", self.ethernet_config),
            server=attempt("server_tcp_get", self.server_config),
            battery=attempt("battery_conf_get", self.battery_config),
            charger=attempt("bq24023_mode_get", self.charger),
            temperature_c=attempt("lmr", self.temperature),
            system=attempt("system_conf_get", self.system_config),
            logging_config=attempt("log_config_get", self.log_config),
            files=attempt("fs_ls", self.files, ()),
            filesystem=attempt("fs_stats", self.filesystem_stats),
            spi_flash=attempt("sf_list", self.spi_flash_devices, ()),
            tasks=attempt("task_list", self.tasks),
            status=attempt("status_get", self.status) if include_status else None,
            errors=errors,
        )
        return info
