'''Finding the port, and confirming what is on the other end of it.

Two separate questions, and they need separate answers.  :func:`find_ports`
narrows the field by USB identity, which is cheap and wrong often enough to
matter -- ``0403:6001`` is the stock FTDI FT232 ID and sits in thousands of
unrelated products.  :func:`identify` settles it by asking the device, which
costs two read-only commands and cannot be fooled.

The bridge, settled
-------------------

The reverse engineering notes inferred FTDI from a macOS device filename in a
screenshot and flagged it as unconfirmed.  **Confirmed on the live sign:**

    Bus 001 Device 014: ID 0403:6001 Future Technology Devices International,
    Ltd FT232 Serial (UART) IC

So: FTDI FT232, VID ``0x0403``, PID ``0x6001``, driven by ``ftdi_sio`` on Linux,
appearing as ``/dev/ttyUSB*``.  On macOS the FTDI driver names it
``/dev/cu.usbserial-<eeprom serial>``, which is where the original inference
came from.

Permissions
-----------

On Linux the node is ``root:dialout`` mode 660, so you need to be in ``dialout``
(or ``uucp`` on Arch), or use ``sudo``.  A udev rule is tidier::

    SUBSYSTEM=="tty", ATTRS{idVendor}=="0403", ATTRS{idProduct}=="6001", \\
        MODE="0660", GROUP="dialout"

Note that this matches *every* FT232, not just a sign.  There is no USB-level
way to tell them apart: the sign ships a stock FTDI EEPROM, so the product
string, manufacturer string and serial are FTDI's, not Visionect's.  That is
precisely why :func:`identify` exists.
'''

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

__all__ = [
    "FTDI_PIDS",
    "FTDI_VID",
    "KNOWN_BRIDGES",
    "Candidate",
    "Identity",
    "find_ports",
    "identify",
]

log = logging.getLogger(__name__)

FTDI_VID = 0x0403
"""Future Technology Devices International. Confirmed on the live sign."""

FTDI_PIDS: frozenset[int] = frozenset({0x6001, 0x6010, 0x6011, 0x6014, 0x6015})
"""FT232 (``0x6001``, measured) plus the other common FTDI UART PIDs.

The rest are included because the vendor's hardware range spans several board
revisions and only one of them was in front of this library.  A match on any of
them is a *candidate*, never an identification.
"""

KNOWN_BRIDGES: dict[tuple[int, int], str] = {
    (0x0403, 0x6001): "FTDI FT232 (measured on a 32\" sign, firmware 7.4.4407)",
    (0x0403, 0x6015): "FTDI FT231X",
    (0x0403, 0x6014): "FTDI FT232H",
    (0x10C4, 0xEA60): "Silicon Labs CP2102 -- not observed on a sign",
    (0x1A86, 0x7523): "WCH CH340 -- not observed on a sign",
}
"""USB IDs and what they are, with provenance. Only the first was measured."""


@dataclass(frozen=True, slots=True)
class Candidate:
    """A serial port that might be a sign.

    Attributes:
        device: the node path, e.g. ``/dev/ttyUSB0``.
        vid: USB vendor id, or None if the port is not USB.
        pid: USB product id, or None.
        serial_number: the bridge's EEPROM serial. **The FTDI chip's, not the
            sign's** -- it has nothing to do with the device UUID.
        description: whatever pyserial reports.
        likely: True when the USB id is in :data:`KNOWN_BRIDGES`. A weak signal:
            ``0403:6001`` is the most common USB-serial id in existence.
    """

    device: str
    vid: int | None = None
    pid: int | None = None
    serial_number: str | None = None
    description: str | None = None
    likely: bool = False

    @property
    def usb_id(self) -> str | None:
        """``"0403:6001"``, or None for a non-USB port."""
        if self.vid is None or self.pid is None:
            return None
        return f"{self.vid:04x}:{self.pid:04x}"

    @property
    def bridge(self) -> str | None:
        """What :data:`KNOWN_BRIDGES` says this chip is."""
        if self.vid is None or self.pid is None:
            return None
        return KNOWN_BRIDGES.get((self.vid, self.pid))


def find_ports(*, all_ports: bool = False) -> tuple[Candidate, ...]:
    """List serial ports, likely candidates first.

    Args:
        all_ports: include ports whose USB id is not a known bridge. Useful when
            a board revision uses a chip this module has not seen, and necessary
            for a USB-serial adapter plugged into a sign's UART header rather
            than its own connector.

    Returns:
        :class:`Candidate` objects, those with ``likely=True`` first.

    Raises:
        ImportError: if pyserial is not installed.

    Note:
        This cannot tell a sign from any other FT232. Follow it with
        :func:`identify`.
    """
    try:
        from serial.tools import list_ports
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise ImportError(
            "port discovery needs pyserial: pip install 'pyvisionect[usb]'"
        ) from exc

    out: list[Candidate] = []
    for port in list_ports.comports():
        vid = getattr(port, "vid", None)
        pid = getattr(port, "pid", None)
        likely = (vid, pid) in KNOWN_BRIDGES
        if not likely and not all_ports:
            continue
        out.append(
            Candidate(
                device=port.device,
                vid=vid,
                pid=pid,
                serial_number=getattr(port, "serial_number", None),
                description=getattr(port, "description", None),
                likely=likely,
            )
        )
    return tuple(sorted(out, key=lambda c: (not c.likely, c.device)))


@dataclass(frozen=True, slots=True)
class Identity:
    """The answer to "is this a Visionect sign, and which one?".

    Attributes:
        is_sign: True only when both probes succeeded and agreed.
        uuid: the device UUID as a string, or None.
        firmware: e.g. ``"7.4.4407"``, or None.
        cli_version: e.g. ``"1.2"``, or None.
        hardware: e.g. ``"PP32 v1.1"``, or None.
        app: the JS application name, e.g. ``"Joan"``.
        reason: why :attr:`is_sign` is False, or None when it is True.
    """

    is_sign: bool
    uuid: str | None = None
    firmware: str | None = None
    cli_version: str | None = None
    hardware: str | None = None
    app: str | None = None
    reason: str | None = None

    def describe(self) -> str:
        if not self.is_sign:
            return f"not a Visionect sign: {self.reason}"
        bits = [b for b in (self.uuid, self.firmware, self.hardware, self.app) if b]
        return "Visionect sign: " + ", ".join(bits)


def identify(port_or_console: object, *, timeout: float = 4.0) -> Identity:
    """Confirm a port is a sign, by asking it.

    Sends ``uuid_get`` and ``fw_version_get`` -- two read-only commands, nothing
    else.  A port that is not a sign will time out, answer nothing, or answer
    something that does not parse as 16 UUID bytes, and all three come back as
    ``is_sign=False`` with a reason rather than an exception: scanning ports
    means expecting most of them to fail.

    Args:
        port_or_console: a device path, or an open
            :class:`~pyvisionect.io.usb.console.SerialConsole`.
        timeout: per-command, kept short because this is run across several
            ports.

    Returns:
        An :class:`Identity`.

    Example:
        >>> for candidate in find_ports():                 # doctest: +SKIP
        ...     print(candidate.device, identify(candidate.device).describe())
    """
    from . import parsers as P
    from .console import SerialConsole

    console: SerialConsole | None = None
    owned = False
    try:
        if isinstance(port_or_console, SerialConsole):
            console = port_or_console
        else:
            console = SerialConsole(
                str(port_or_console), command_timeout=timeout, idle_timeout=0.6
            )
            owned = True
        try:
            console.sync()
        except Exception as exc:
            return Identity(is_sign=False, reason=f"no prompt: {exc}")

        try:
            device_uuid = P.parse_uuid(console.command("uuid_get", timeout=timeout).lines)
        except Exception as exc:
            return Identity(is_sign=False, reason=f"uuid_get did not parse: {exc}")

        firmware = None
        try:
            firmware = P.parse_firmware_version(
                console.command("fw_version_get", timeout=timeout).lines
            )
        except Exception as exc:  # pragma: no cover - a sign always answers this
            log.debug("fw_version_get failed on an otherwise-identified sign: %s", exc)

        cli = None
        try:
            cli = console.command("cli_version_get", timeout=timeout).field("CLI version")
        except Exception:  # pragma: no cover
            pass

        return Identity(
            is_sign=True,
            uuid=str(device_uuid),
            firmware=firmware.firmware if firmware else None,
            cli_version=cli,
            hardware=firmware.hardware if firmware else None,
            app=firmware.app if firmware else None,
        )
    except Exception as exc:
        return Identity(is_sign=False, reason=f"{type(exc).__name__}: {exc}")
    finally:
        if owned and console is not None:
            console.close()
