"""USB serial provisioning -- the only way to point a sign at a new server.

Why this exists
---------------

Eight TCLV ids are marked ``canWrite: false`` in the vendor's own admin-UI
descriptor table, and they are exactly the ones needed to bootstrap a device:
``CONN_TYPE`` (2), ``IPV4_SERVER_IP`` (18), ``IPV4_SERVER_PORT`` (19),
``WIFI_SSID`` (65), ``WIFI_SECURITY`` (66), ``WIFI_PSK`` (67),
``WIFI_BAND`` (68), ``MOB_SECURITY`` (70).  The network protocol can **read**
them but never write them.  There is no chicken-and-egg escape: you cannot move
a sign to a new WiFi network or a new server over the network it does not have.

So repointing a sign at your own server is either:

* **USB** -- this module: ``server_tcp_set`` then ``flash_save`` then ``reboot``; or
* **DNS** -- if the sign already holds a *hostname* (ours holds
  ``visionect.internal.example.com``), re-point that name at your listener and
  touch nothing on the device.

Transport
---------

A USB-to-UART bridge presenting a serial port on the device's Micro-USB
connector, **115200 8N1**, carrying a line-oriented ASCII command shell: type a
command, press enter, read the text reply.  Not CBOR, not ``ParamTCL`` -- but the
**key space is identical** to the network protocol's, so
:mod:`pyvisionect.devices.tclv` is directly reusable; you just translate id ->
command name instead of id -> uint16.

Evidence quality
----------------

The line settings and the command list are **from the vendor's published CLI
reference**, cross-checked against the shipped admin UI's TCLV tables.  The sign
was never plugged into the analysis machine, so:

* The **VID/PID and kernel driver are unverified.**  macOS names the node
  ``/dev/cu.usbserial-XXXXXXXX``, which is FTDI/Prolific naming rather than
  CDC-ACM's ``usbmodem``, pointing at VID ``0x0403`` -- but that is an inference
  from a filename in a screenshot.  Confirm with ``lsusb`` and ``dmesg``.
* The **prompt string, line terminator (CR / LF / CRLF) and whether the device
  echoes are all undocumented.**  :class:`UsbProvisioner` therefore reads until
  quiet rather than until a prompt, and sends ``\\r\\n`` by default with the
  terminator configurable.

Gotchas the vendor documents
----------------------------

* **Soft sleep kills the console** after ~15 s of accelerometer inactivity.  Set
  ``SLEEP_MODE`` (52) to 1 ("CLI & CPU always on") for a working session and put
  it back afterwards.
* **Everything is RAM-only until ``flash_save``.**  Verbatim from the vendor:
  "All changes to device configuration are by default retained only in the
  working RAM."
* The Configurator GUI gates its console behind Info -> Debug mode -> Console.
  Talking to the serial port directly bypasses that.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass, field
from typing import Iterable, Sequence

__all__ = [
    "BAUD_RATE",
    "DESTRUCTIVE_COMMANDS",
    "DangerousCommandRefused",
    "UsbProvisioner",
    "WifiSecurity",
    "main",
]

log = logging.getLogger(__name__)

BAUD_RATE = 115200
"""115200 8N1, verbatim from the vendor CLI reference."""


class WifiSecurity:
    """``WIFI_SECURITY`` (66) values. ASCII strings, not integers."""

    OPEN = "none"
    WPA2 = "wpa2"
    WPA2_ENTERPRISE = "wpa2e"


DESTRUCTIVE_COMMANDS: frozenset[str] = frozenset(
    {
        "cc3100_format",
        "cc3100_fw_upgrade",
        "scpu_upgrade",
        "touch_fw_update",
        "fw_upgrade",
        "app_upgrade",
        "wifi_fw_upgrade",
        "feat_disable",
        "flash_erase",
        "cli_pwd_reset",
    }
)
"""Refused unless ``allow_destructive=True``.

``cc3100_format`` formats the radio's SPI flash; the ``*_upgrade`` commands
flash firmware; ``feat_disable`` can turn off features permanently.  Shipping
mode (``system_conf_set`` with ``ship_en=1``, i.e. ``SYS_SHIP_MODE`` = 1) is
refused separately by :meth:`UsbProvisioner.system_conf_set` because it is an
argument, not a command name -- a sign in shipping mode will not wake up
normally.
"""


class DangerousCommandRefused(RuntimeError):
    """A destructive command was issued without the explicit opt-in."""


@dataclass
class UsbProvisioner:
    """A thin, honest wrapper around the device's ASCII serial CLI.

    Args:
        port: the serial device, e.g. ``/dev/ttyUSB0`` or
            ``/dev/cu.usbserial-AU046N37``.
        timeout: per-read timeout in seconds.
        terminator: what to append to each command. The device's expected
            terminator is **not documented**; ``\\r\\n`` is the safe default.
        quiet_time: how long the reply must be silent before we call it finished.
        allow_destructive: unlock :data:`DESTRUCTIVE_COMMANDS` and shipping mode.
            Off by default, deliberately.
        serial: an already-open pyserial-compatible object, for tests.

    Example:
        >>> with UsbProvisioner("/dev/ttyUSB0") as cli:        # doctest: +SKIP
        ...     print(cli.send("uuid_get"))
        ...     cli.repoint("MySSID", "secret", "homeassistant.local", 11113)
    """

    port: str | None = None
    timeout: float = 1.0
    terminator: str = "\r\n"
    quiet_time: float = 0.3
    allow_destructive: bool = False
    serial: object | None = None
    transcript: list[tuple[str, str]] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        if self.serial is None:
            if self.port is None:
                raise ValueError("either port or serial must be given")
            try:
                import serial as pyserial
            except ImportError as exc:  # pragma: no cover
                raise ImportError(
                    "USB provisioning needs pyserial: pip install 'pyvisionect[usb]'"
                ) from exc
            self.serial = pyserial.Serial(
                self.port,
                BAUD_RATE,
                bytesize=8,
                parity="N",
                stopbits=1,
                timeout=self.timeout,
            )

    # ----------------------------------------------------------------- raw

    def __enter__(self) -> "UsbProvisioner":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        closer = getattr(self.serial, "close", None)
        if closer is not None:
            closer()

    def send(self, command: str, *, read_reply: bool = True) -> str:
        """Send one command line and return the reply text.

        Raises:
            DangerousCommandRefused: for a command in
                :data:`DESTRUCTIVE_COMMANDS` without ``allow_destructive``.
        """
        name = command.split()[0] if command.strip() else ""
        if name in DESTRUCTIVE_COMMANDS and not self.allow_destructive:
            raise DangerousCommandRefused(
                f"{name!r} is destructive and is refused by default. "
                "Pass allow_destructive=True (or --allow-destructive) if you "
                "really mean it, and read the command's documentation first."
            )
        log.debug("-> %s", command)
        assert self.serial is not None
        self.serial.write((command + self.terminator).encode("ascii"))  # type: ignore[attr-defined]
        flush = getattr(self.serial, "flush", None)
        if flush is not None:
            flush()
        reply = self.read_reply() if read_reply else ""
        self.transcript.append((command, reply))
        return reply

    def read_reply(self) -> str:
        """Read until the port has been quiet for :attr:`quiet_time`.

        The device's prompt string is undocumented, so there is nothing reliable
        to read *until*.
        """
        assert self.serial is not None
        chunks: list[bytes] = []
        deadline = time.monotonic() + self.timeout
        last_data = time.monotonic()
        while time.monotonic() < deadline:
            waiting = getattr(self.serial, "in_waiting", 0)
            data = self.serial.read(waiting or 1)  # type: ignore[attr-defined]
            if data:
                chunks.append(data)
                last_data = time.monotonic()
                deadline = time.monotonic() + self.timeout
            elif chunks and time.monotonic() - last_data >= self.quiet_time:
                break
        text = b"".join(chunks).decode("ascii", errors="replace")
        log.debug("<- %s", text.strip())
        return text

    # ------------------------------------------------------------ read-only

    def help(self) -> str:
        """Dump the command set this firmware actually has.

        The vendor doc describes the latest firmware; ``help`` on the device is
        authoritative, because commands are guarded by compile-time switches
        (``USE_CC3100_DRIVER``, ``USE_RS9113_DRIVER``, ...).
        """
        return self.send("help")

    def flash_print(self) -> str:
        """Dump **every stored setting**. Run this first; it is the single
        highest-value command for understanding a device."""
        return self.send("flash_print")

    def uuid_get(self) -> str:
        """The device UUID. Read-only -- there is no setter; it is factory-programmed."""
        return self.send("uuid_get")

    def gtin_get(self) -> str:
        return self.send("gtin_get")

    def fw_version_get(self) -> str:
        return self.send("fw_version_get")

    def server_tcp_get(self) -> str:
        return self.send("server_tcp_get")

    def wifi_conf_get(self) -> str:
        return self.send("wifi_conf_get")

    def conn_type_list(self) -> str:
        """Enumerate this firmware's real ``CONN_TYPE`` values."""
        return self.send("conn_type_list")

    def send_status_packet(self) -> str:
        """``pss`` -- make the device emit a PV3 status packet on demand.

        Very useful while bringing up a listener: it decouples protocol work
        from the 60 s heartbeat.
        """
        return self.send("pss")

    # ------------------------------------------------------------- setters

    def wifi_conf_set(
        self, ssid: str, psk: str, security: str = WifiSecurity.WPA2, band: int = 0
    ) -> str:
        """``wifi_conf_set <ssid> <psk> <security> <band>`` -- TCLV 65/67/66/68.

        The CLI is whitespace-delimited, so an SSID or PSK containing a space
        must go through :meth:`wifi_ssid_set` / :meth:`wifi_psk_set` instead;
        this method refuses rather than silently truncating.
        """
        for label, value in (("ssid", ssid), ("psk", psk)):
            if any(c.isspace() for c in value):
                raise ValueError(
                    f"{label} contains whitespace, which the single-line CLI "
                    f"cannot carry; use wifi_{label}_set instead"
                )
        return self.send(f"wifi_conf_set {ssid} {psk} {security} {band}")

    def wifi_ssid_set(self, ssid: str) -> str:
        """``wifi_ssid_set <ssid>`` -- TCLV 65. Use for an SSID with spaces."""
        return self.send(f"wifi_ssid_set {ssid}")

    def wifi_psk_set(self, psk: str) -> str:
        """``wifi_psk_set <psk>`` -- TCLV 67."""
        return self.send(f"wifi_psk_set {psk}")

    def wifi_security_set(self, security: str) -> str:
        """``wifi_security_set <none|wpa2|wpa2e>`` -- TCLV 66."""
        return self.send(f"wifi_security_set {security}")

    def server_tcp_set(self, host: str, port: int = 11113) -> str:
        """``server_tcp_set <ip|dns> <port>`` -- TCLV 18 and 19.

        A DNS name works and is what the live sign holds, which is why a DNS
        re-point is an alternative to re-flashing this field.
        """
        return self.send(f"server_tcp_set {host} {port}")

    def conn_type_set(self, conn_type: int) -> str:
        """``conn_type_set <type>`` -- TCLV 2.

        Known values: 0 none, 1/4/5/6 WiFi (different radio drivers), 2 3G,
        3 Ethernet. The live sign is 6 (``ti CC3100``). The vendor's 4G example
        uses 8, which is past the admin UI's table -- run
        :meth:`conn_type_list` on the device rather than guessing.
        """
        return self.send(f"conn_type_set {conn_type}")

    def server_hb_set(self, minutes: int) -> str:
        """``server_hb_set <time>`` -- TCLV 29. This one *is* network-writable too."""
        return self.send(f"server_hb_set {minutes}")

    def sleep_mode_set(self, mode: int) -> str:
        """``SLEEP_MODE`` (52): 0 wake on accelerometer, 1 CLI & CPU always on,
        2 CLI disabled when asleep.

        Set 1 before a long console session or soft sleep will cut you off after
        ~15 s, and set it back when you are done.
        """
        return self.send(f"sleep_conf_set {mode}")

    def system_conf_set(
        self, battery_indicator: int, touch: int, ship: int = 0
    ) -> str:
        """``system_conf_set <batt_ind> <touch> <ship>`` -- TCLV 49/50/51.

        Raises:
            DangerousCommandRefused: for ``ship=1`` without the opt-in. Shipping
                mode puts the sign to sleep in a way that is awkward to undo.
        """
        if ship and not self.allow_destructive:
            raise DangerousCommandRefused(
                "ship_en=1 enables shipping mode, which the sign does not wake "
                "out of normally. Pass allow_destructive=True if you mean it."
            )
        return self.send(f"system_conf_set {battery_indicator} {touch} {ship}")

    def flash_save(self) -> str:
        """``flash_save`` -- TCLV 53. **Mandatory**, or everything above is lost."""
        return self.send("flash_save")

    def reboot(self) -> str:
        """``reboot`` -- apply. No reply is expected; the link drops."""
        return self.send("reboot", read_reply=False)

    # ----------------------------------------------------------- sequences

    def repoint(
        self,
        ssid: str | None,
        psk: str | None,
        server: str,
        port: int = 11113,
        *,
        security: str = WifiSecurity.WPA2,
        band: int = 0,
        reboot: bool = True,
    ) -> list[tuple[str, str]]:
        """The bootstrap sequence, verbatim from the vendor reference.

        ``wifi_conf_set`` -> ``server_tcp_set`` -> ``flash_save`` -> ``reboot``.

        Pass ``ssid=None`` to leave the WiFi configuration alone and only move
        the sign to a new server.

        Returns:
            The ``(command, reply)`` transcript of this call.
        """
        start = len(self.transcript)
        if ssid is not None:
            if psk is None:
                raise ValueError("psk is required when ssid is given")
            if any(c.isspace() for c in ssid) or any(c.isspace() for c in psk):
                self.wifi_ssid_set(ssid)
                self.wifi_psk_set(psk)
                self.wifi_security_set(security)
            else:
                self.wifi_conf_set(ssid, psk, security, band)
        self.server_tcp_set(server, port)
        self.flash_save()
        if reboot:
            self.reboot()
        return self.transcript[start:]


def main(argv: Sequence[str] | None = None) -> int:
    """``pyvisionect-provision`` -- repoint a sign at your own server."""
    parser = argparse.ArgumentParser(
        prog="pyvisionect-provision",
        description=(
            "Point a Visionect sign at your own server over its USB serial CLI. "
            "This is the only way to change the server address or WiFi "
            "credentials: those TCLV ids are read-only over the network."
        ),
    )
    parser.add_argument("port", help="serial device, e.g. /dev/ttyUSB0")
    parser.add_argument("--server", help="server IP or DNS name (TCLV 18)")
    parser.add_argument("--server-port", type=int, default=11113, help="TCLV 19")
    parser.add_argument("--ssid", help="WiFi SSID (TCLV 65)")
    parser.add_argument("--psk", help="WiFi passphrase (TCLV 67)")
    parser.add_argument(
        "--security", default=WifiSecurity.WPA2, choices=["none", "wpa2", "wpa2e"]
    )
    parser.add_argument("--band", type=int, default=0, help="0 dual, 1 2.4GHz, 2 5GHz")
    parser.add_argument("--no-reboot", action="store_true")
    parser.add_argument(
        "--dump", action="store_true", help="run flash_print and exit, changing nothing"
    )
    parser.add_argument(
        "--allow-destructive",
        action="store_true",
        help="unlock the firmware-flashing and shipping-mode commands",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s"
    )

    if not args.dump and not args.server:
        parser.error("--server is required unless --dump is given")

    with UsbProvisioner(
        args.port, allow_destructive=args.allow_destructive
    ) as cli:
        if args.dump:
            print(cli.flash_print())
            return 0
        for command, reply in cli.repoint(
            args.ssid,
            args.psk,
            args.server,
            args.server_port,
            security=args.security,
            band=args.band,
            reboot=not args.no_reboot,
        ):
            print(f"$ {command}\n{reply.strip()}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
