'''Typed results: every getter returns a dataclass, not a string.

Each parser in this module was written against a capture taken off a live sign
(firmware 7.4.4407, 32" board, ``APP: Joan``).  The verbatim replies are in
:mod:`pyvisionect.io.usb.fake`, which is also what the tests run against, so
the parsers and the fixtures cannot drift apart.

Shapes you have to cope with
----------------------------

The replies are *mostly* ``Label: value`` lines, but not uniformly, and the
exceptions are the whole reason this module is longer than a dict comprehension:

* ``uuid_get`` emits a **blank line first**, then space-separated ``0x`` bytes
  with a **trailing space** -- not a dashed UUID string.
* ``certs_config_get`` prints ``rv: 0`` **before** its body, so "the return code
  is the last line" is false.
* ``rv:`` comes in two spellings from different commands in the same firmware:
  ``rv: 0`` (``fs_ls``) and ``rv: 0x0`` (``sf_list``, ``wifi_bssid_get``).
* ``fs_ls`` and ``sf_list`` are tables, not fields; ``sf_list`` has a rule line
  and a trailing blank.
* ``status_get`` is 60 ``KEY: value`` lines in SCREAMING_SNAKE, ended by a
  ``STATUS_END`` sentinel, mixing decimal and ``0x`` hex -- and one key,
  ``PROTOCOL VERSION``, has a **space** where every other key has an underscore.
* ``display_conf_get`` prints **8** VCOMs while ``display_conf_set`` takes
  **16** arguments. The getter is right about this hardware (TCLV has
  ``VCOM_0..7``, ids 20-27); the setter's arity is for a 16-panel board. Do not
  "fix" the parser to expect 16.

Numbers that look wrong and are not
-----------------------------------

* ``conn_fw_ver`` reports ``6.4294967295.3310684876``.  Those are an
  uninitialised ``0xFFFFFFFF`` and a junk word, and the same two values appear
  in ``status_get`` as ``CONN_VER_MINOR: -1`` and
  ``CONN_VER_REVISION: -984282420`` -- the *same bytes* rendered unsigned by one
  command and signed by the other, in one firmware build.
  :class:`ConnectivityFirmware` keeps both views.
* ``ipv4_conf_get`` on a DHCP device shows a stale static address
  (``MODE: 1`` is DHCP, so IP/NM/GW/DNS are unused leftovers).  It is the
  *stored configuration*, never the live lease.  :attr:`Ipv4Config.in_use` says
  so.
* ``SIGNAL_STRENGTH: 31`` in ``status_get`` against ``RSSI:-31 dBm`` from
  ``cc3100_rssi``: the status field is a **dBm magnitude with the sign
  dropped**, which is exactly what
  :mod:`pyvisionect.packets.status` documents for the wire field.  Live
  confirmation of a fact that was read out of a disassembler.
'''

from __future__ import annotations

import re
import uuid as _uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

__all__ = [
    "BatteryConfig",
    "BorderConfig",
    "CertificateConfig",
    "ChargerState",
    "ConnectivityFirmware",
    "ConnectivityOption",
    "ConnectivityState",
    "ConnectivityType",
    "DisplayConfig",
    "EapConfig",
    "EncryptionConfig",
    "EthernetConfig",
    "Features",
    "FileEntry",
    "FilesystemStats",
    "FirmwareVersion",
    "ImageRotateConfig",
    "Ipv4Config",
    "LogConfig",
    "PmicRegisters",
    "ServerConfig",
    "SpiFlashDevice",
    "SpiFlashStatus",
    "StatusDump",
    "SystemConfig",
    "Task",
    "TaskList",
    "WifiConfig",
    "WifiFirmware",
    "WifiMacConfig",
    "parse_battery_config",
    "parse_border",
    "parse_certs_config",
    "parse_charger",
    "parse_conn_firmware",
    "parse_conn_state",
    "parse_conn_type",
    "parse_conn_type_list",
    "parse_display_config",
    "parse_eap_config",
    "parse_encryption_config",
    "parse_eth_config",
    "parse_features",
    "parse_firmware_version",
    "parse_fs_ls",
    "parse_fs_stats",
    "parse_gtin",
    "parse_image_rotate_config",
    "parse_ipv4_config",
    "parse_log_config",
    "parse_mac",
    "parse_rssi",
    "parse_sf_list",
    "parse_status_dump",
    "parse_system_config",
    "parse_task_list",
    "parse_temperature",
    "parse_uptime",
    "parse_uuid",
    "parse_wifi_config",
    "parse_wifi_firmware",
    "parse_wifi_mac_config",
]

_MAC_RE = re.compile(r"^(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")


def _int(text: str) -> int:
    """Parse a decimal or ``0x``-prefixed integer, tolerating a trailing unit.

    The device appends units inconsistently (``"2400 mV"``, ``"24 degC"``,
    ``"75 sec"``, ``"-44 dBm"``), so the unit is dropped rather than demanded.
    """
    token = text.strip().split()[0] if text.strip() else text
    return int(token, 0)


def _fields(lines: Sequence[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in lines:
        name, sep, value = line.partition(":")
        if sep:
            out[name.strip()] = value.strip()
    return out


def parse_mac(text: str) -> str:
    """Normalise a MAC to lowercase colon form.

    The firmware is inconsistent about case in its own output --
    ``wifi_mac_conf_get`` lowercases, ``eth_conf_get`` uppercases -- so
    comparing two of its MACs as strings without this is a bug waiting to
    happen.

    Raises:
        ValueError: if *text* is not six colon-separated octets.
    """
    token = text.strip()
    if not _MAC_RE.match(token):
        raise ValueError(f"not a MAC address: {text!r}")
    return token.lower()


# ---------------------------------------------------------------- identity


@dataclass(frozen=True, slots=True)
class FirmwareVersion:
    """``fw_version_get``: eight lines covering app, bootloader, board and panel.

    Attributes:
        firmware: e.g. ``"7.4.4407"``.
        firmware_build_date: the three integers from ``"FW Build date: 10 9 2025"``
            as ``(10, 9, 2025)``. **Field order is not stated by the device**;
            day-month-year is the reading that makes 10/9/2025 a plausible build
            date either way, so this is kept as a raw triple rather than being
            turned into a ``date`` that would be wrong half the year.
        crc: the ``FW Crc=0x...`` word. Matches ``FW_CRC`` in ``status_get``
            and ``FirmwareCRC`` on the wire.
        image_hash: the ``Hash=0x...`` word.
        image_length: ``Length=`` in bytes.
        bootloader: the ``BL Version`` string.
        build: the ``Build Version`` string.
        hardware: ``"PP32 v1.1"`` -- board name and revision.
        bom: the ``BOM:`` integer.
        app: ``"Joan"`` -- the JS application identity.
        panel: the raw ``EPD:`` line, e.g.
            ``'31.2",2x2880x640,WF=31.2_C296,IC=31.2_p224rev0050'``.
    """

    firmware: str
    firmware_build_date: tuple[int, int, int] | None
    crc: int | None
    image_hash: int | None
    image_length: int | None
    bootloader: str | None
    bootloader_build_date: tuple[int, int, int] | None
    build: str | None
    hardware: str | None
    bom: int | None
    app: str | None
    panel: str | None

    @property
    def version_tuple(self) -> tuple[int, ...]:
        """:attr:`firmware` split on dots, for comparison."""
        return tuple(int(p) for p in self.firmware.split(".") if p.isdigit())

    @property
    def panel_count(self) -> int | None:
        """How many physical panels the ``EPD:`` line names.

        ``"2x2880x640"`` means two 2880x640 panels.  Worth having because
        ``status_get``'s ``NUMBER_OF_SUPPORTED_DISPLAYS`` agrees (2) while the
        reverse engineering notes said four 1440x640 panels -- the panel line is
        the one that comes from the firmware's own display table.
        """
        if not self.panel:
            return None
        match = re.search(r"(\d+)x(\d+)x(\d+)", self.panel)
        return int(match.group(1)) if match else None

    @property
    def panel_size(self) -> tuple[int, int] | None:
        """``(width, height)`` of a single panel from the ``EPD:`` line."""
        if not self.panel:
            return None
        match = re.search(r"(\d+)x(\d+)x(\d+)", self.panel)
        return (int(match.group(2)), int(match.group(3))) if match else None


def _date_triple(text: str) -> tuple[int, int, int] | None:
    parts = text.split()
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        a, b, c = (int(p) for p in parts)
        return (a, b, c)
    return None


def parse_firmware_version(lines: Sequence[str]) -> FirmwareVersion:
    """Parse ``fw_version_get``."""
    f = _fields(lines)
    crc = image_hash = image_length = None
    for line in lines:
        if line.startswith("FW Crc="):
            for key, value in re.findall(r"(\w+)=((?:0x)?[0-9a-fA-F]+)", line):
                if key == "Crc":
                    crc = int(value, 16)
                elif key == "Hash":
                    image_hash = int(value, 16)
                elif key == "Length":
                    image_length = int(value, 0)
    hardware = bom = app = None
    if "HW" in f:
        # "PP32 v1.1, BOM: 0, APP: Joan" -- the line has nested "k: v" pairs, so
        # it cannot go through _fields.
        raw = f["HW"]
        hardware = raw.split(",")[0].strip()
        bom_match = re.search(r"BOM:\s*(\d+)", raw)
        bom = int(bom_match.group(1)) if bom_match else None
        app_match = re.search(r"APP:\s*(\S+)", raw)
        app = app_match.group(1) if app_match else None
    return FirmwareVersion(
        firmware=f.get("FW Version", ""),
        firmware_build_date=_date_triple(f.get("FW Build date", "")),
        crc=crc,
        image_hash=image_hash,
        image_length=image_length,
        bootloader=f.get("BL Version"),
        bootloader_build_date=_date_triple(f.get("BL Build date", "")),
        build=f.get("Build Version"),
        hardware=hardware,
        bom=bom,
        app=app,
        panel=f.get("EPD"),
    )


def parse_uuid(lines: Sequence[str]) -> _uuid.UUID:
    """Parse ``uuid_get`` into a :class:`uuid.UUID`.

    The device prints a blank line, then 16 space-separated ``0x`` bytes with a
    trailing space -- *not* a dashed UUID.  The trailing NUL bytes are real and
    are preserved: a 12-byte factory identifier zero-padded to 16, so the
    rendered form ends ``...00000000`` and that is correct, not truncation.

    Use :func:`uuid.UUID.bytes` to get the 16 bytes the packet header carries.

    Raises:
        ValueError: if the reply does not carry exactly 16 byte literals.
    """
    tokens: list[str] = []
    for line in lines:
        _, _, tail = line.partition("UUID:")
        tokens.extend(re.findall(r"0x([0-9a-fA-F]{1,2})", tail or line))
    if len(tokens) != 16:
        raise ValueError(
            f"uuid_get should give 16 bytes, got {len(tokens)}: {list(lines)!r}"
        )
    return _uuid.UUID(bytes=bytes(int(t, 16) for t in tokens))


def parse_gtin(lines: Sequence[str]) -> str:
    """Parse ``gtin_get``. Kept as a string: a GTIN has significant leading zeros."""
    return _fields(lines)["GTIN"]


def parse_uptime(lines: Sequence[str]) -> int:
    """Parse ``uptime``. **Minutes**, not seconds -- same unit as the wire field."""
    return _int(_fields(lines)["Uptime in min"])


@dataclass(frozen=True, slots=True)
class Features:
    """``feat_get``: which licensed features are on.

    Attributes:
        enabled: feature name -> bool. Names come from the device
            (``"touch"``, ``"EAP"``), case as printed.

    Turning one of these off is ``feat_disable <key>``, which is in the ``HARD``
    tier: the key is a licence key you have to have in order to put it back.
    """

    enabled: Mapping[str, bool]

    def __getitem__(self, name: str) -> bool:
        return self.enabled[name]

    @property
    def touch(self) -> bool | None:
        return self.enabled.get("touch")

    @property
    def eap(self) -> bool | None:
        return self.enabled.get("EAP")


def parse_features(lines: Sequence[str]) -> Features:
    """Parse ``feat_get``.

    The reply pads the label to a column (``"Feature EAP:   enabled"``), which
    is why the value is stripped rather than sliced.
    """
    out: dict[str, bool] = {}
    for line in lines:
        match = re.match(r"^Feature\s+(\S+):\s*(\S+)\s*$", line)
        if match:
            out[match.group(1)] = match.group(2).lower() == "enabled"
    return Features(enabled=out)


# -------------------------------------------------------------- encryption


@dataclass(frozen=True, slots=True)
class EncryptionConfig:
    """``encryption_config_get``: outbound link encryption state.

    Attributes:
        enabled: from ``"Outbound enc: Disabled"`` / ``"Enabled"``. Maps to
            TCLV 130, documented in the vendor table as
            *"Outbound encryption: 0=Disabled, 1=Enabled"*.
        key: whatever the device echoes back between the single quotes of
            ``"Key: ''"``. Empty on an unprovisioned sign.

    See :mod:`pyvisionect.io.usb.encryption` for what is and is not known about
    the key format. Short version: the only two modes the TCLV table admits are
    0 and 1, and the key material has never been observed non-empty.
    """

    enabled: bool
    key: str

    @property
    def configured(self) -> bool:
        """True when a key is present, whether or not the mode is on."""
        return bool(self.key)


def parse_encryption_config(lines: Sequence[str]) -> EncryptionConfig:
    """Parse ``encryption_config_get``."""
    f = _fields(lines)
    raw_key = f.get("Key", "")
    if len(raw_key) >= 2 and raw_key[0] == raw_key[-1] == "'":
        raw_key = raw_key[1:-1]
    return EncryptionConfig(
        enabled=f.get("Outbound enc", "").strip().lower() == "enabled",
        key=raw_key,
    )


# ----------------------------------------------------------------- display


@dataclass(frozen=True, slots=True)
class DisplayConfig:
    """``display_conf_get``: the VCOM rails and the display identifier.

    Attributes:
        vcom_mv: one entry per VCOM rail the getter prints, in millivolts.
            **Eight on this hardware, not sixteen.** ``display_conf_set`` takes
            16 VCOM arguments, but TCLV only defines ``VCOM_0..7`` (ids 20-27)
            and the getter prints 8. On the live 32" sign the first four are
            2400 mV and the last four are 0, i.e. four rails wired and four not.
        display_type: the ``Display type:`` integer, printed unsigned decimal.
            Compare :data:`pyvisionect.devices.panels.DISPLAY_TYPE_NAMES`.

    Writing this is ``display_conf_set``, which is ``DESTRUCTIVE``: a wrong VCOM
    is a way to damage an e-ink panel, not merely to make it look bad.
    """

    vcom_mv: tuple[int, ...]
    display_type: int | None

    @property
    def active_rails(self) -> int:
        """How many VCOM rails are non-zero."""
        return sum(1 for v in self.vcom_mv if v)

    @property
    def display_type_hex(self) -> str:
        """:attr:`display_type` as ``0x...``, which is how ``status_get`` prints it."""
        return "" if self.display_type is None else f"0x{self.display_type:08X}"


def parse_display_config(lines: Sequence[str]) -> DisplayConfig:
    """Parse ``display_conf_get``."""
    vcoms: list[tuple[int, int]] = []
    display_type = None
    for line in lines:
        match = re.match(r"^Vcom\s+(\d+):\s*(-?\d+)\s*mV\s*$", line)
        if match:
            vcoms.append((int(match.group(1)), int(match.group(2))))
            continue
        match = re.match(r"^Display type:\s*(\d+)\s*$", line)
        if match:
            display_type = int(match.group(1))
    return DisplayConfig(
        vcom_mv=tuple(v for _, v in sorted(vcoms)), display_type=display_type
    )


@dataclass(frozen=True, slots=True)
class BorderConfig:
    """``border_get``: the EPD border mode (TCLV 109).

    Attributes:
        supported: ``"Border support: No"`` on the 32" board -- the panel has no
            controllable border, so :attr:`mode` is moot there.
        mode: ``"Server"``, ``"White"`` or ``"Black"``, matching TCLV 109's
            *"0=server, 1=white, 2=black"*.
    """

    supported: bool
    mode: str


def parse_border(lines: Sequence[str]) -> BorderConfig:
    """Parse ``border_get``."""
    f = _fields(lines)
    return BorderConfig(
        supported=f.get("Border support", "").strip().lower() in ("yes", "true", "1"),
        mode=f.get("Border", ""),
    )


@dataclass(frozen=True, slots=True)
class ImageRotateConfig:
    """``image_rotate_config_get``: the local image carousel.

    Attributes:
        mode: ``"off"`` on the live sign.
        timeout_s: seconds between images.
        image_count: how many images are in the rotation. Zero while off,
            even though ``fs_ls`` shows six ``.pv2`` files -- the carousel count
            is configuration, not a directory listing.
    """

    mode: str
    timeout_s: int | None
    image_count: int | None

    @property
    def enabled(self) -> bool:
        return self.mode.strip().lower() not in ("off", "", "0", "disabled")


def parse_image_rotate_config(lines: Sequence[str]) -> ImageRotateConfig:
    """Parse ``image_rotate_config_get``."""
    f = _fields(lines)
    return ImageRotateConfig(
        mode=f.get("Mode", ""),
        timeout_s=_int(f["Timeout"]) if "Timeout" in f else None,
        image_count=_int(f["Number of images"]) if "Number of images" in f else None,
    )


# ------------------------------------------------------------ connectivity


@dataclass(frozen=True, slots=True)
class ConnectivityOption:
    """One row of ``conn_type_list``: a driver this firmware was built with.

    Attributes:
        name: the driver name, e.g. ``"CC3100"`` (TI WiFi) or ``"W5500"``
            (WIZnet Ethernet MAC+PHY).
        value: the ``CONN_TYPE`` (TCLV 2) integer to pass to ``conn_type_set``.
    """

    name: str
    value: int


def parse_conn_type_list(lines: Sequence[str]) -> tuple[ConnectivityOption, ...]:
    """Parse ``conn_type_list``.

    **This is the only trustworthy source for ``CONN_TYPE`` values.**  The
    vendor's admin UI table lists 0-7 and the vendor's own CLI documentation
    uses ``conn_type_set 8`` in a 4G example; this firmware offers exactly
    three, ``W5500 (3)``, ``CC3100 (6)`` and ``None (0)``, and would reject the
    rest.  Enumerate, never guess.
    """
    out: list[ConnectivityOption] = []
    for line in lines:
        match = re.match(r"^(.+?)\s*\((\d+)\)\s*$", line)
        if match:
            out.append(
                ConnectivityOption(name=match.group(1).strip(), value=int(match.group(2)))
            )
    return tuple(out)


@dataclass(frozen=True, slots=True)
class ConnectivityType:
    """``conn_type_get``: which driver is live right now.

    Attributes:
        name: e.g. ``"CC3100"``.
        value: the ``CONN_TYPE`` integer, 6 on the live sign.
        active: whether the reply said ``active``.
    """

    name: str
    value: int
    active: bool = True


def parse_conn_type(lines: Sequence[str]) -> ConnectivityType:
    """Parse ``conn_type_get`` (``"CC3100 (6) active"``)."""
    for line in lines:
        match = re.match(r"^(.+?)\s*\((\d+)\)\s*(\w+)?\s*$", line)
        if match:
            return ConnectivityType(
                name=match.group(1).strip(),
                value=int(match.group(2)),
                active=(match.group(3) or "").lower() == "active",
            )
    raise ValueError(f"cannot parse conn_type_get: {list(lines)!r}")


@dataclass(frozen=True, slots=True)
class ConnectivityState:
    """``conn_state_get``: the socket's state in words.

    Attributes:
        state: verbatim, e.g. ``"tcp open"``.
    """

    state: str

    @property
    def connected(self) -> bool:
        """True for ``"tcp open"``.

        Deliberately a substring test on ``"open"``: the firmware's state
        vocabulary is not documented anywhere, so an unknown state is reported
        as not-connected rather than crashing.
        """
        return "open" in self.state.lower()


def parse_conn_state(lines: Sequence[str]) -> ConnectivityState:
    """Parse ``conn_state_get``."""
    return ConnectivityState(state=_fields(lines).get("Conn", "").strip())


@dataclass(frozen=True, slots=True)
class ConnectivityFirmware:
    """``conn_fw_ver``: the radio firmware triple, as the device renders it.

    Attributes:
        raw: the line verbatim, e.g. ``"6.4294967295.3310684876"``.
        parts_unsigned: the three words read as unsigned.
        parts_signed: the same words read as signed int32.

    Both views are kept because **the firmware disagrees with itself**: this
    command prints the words unsigned while ``status_get`` prints the identical
    values as ``CONN_VER_MINOR: -1`` and ``CONN_VER_REVISION: -984282420``.
    4294967295 is ``0xFFFFFFFF``, i.e. never written, so on this hardware the
    minor and revision are simply uninitialised and only the major (6, matching
    ``CONN_TYPE`` 6) means anything.  Use ``cc3100_fw_version`` for a real radio
    version.
    """

    raw: str
    parts_unsigned: tuple[int, ...]
    parts_signed: tuple[int, ...]

    @property
    def initialised(self) -> bool:
        """False when any word is ``0xFFFFFFFF``."""
        return all(p != 0xFFFFFFFF for p in self.parts_unsigned)


def parse_conn_firmware(lines: Sequence[str]) -> ConnectivityFirmware:
    """Parse ``conn_fw_ver``."""
    raw = lines[0].strip() if lines else ""
    unsigned = tuple(int(p) for p in raw.split(".") if p.lstrip("-").isdigit())
    signed = tuple(p - 0x100000000 if p >= 0x80000000 else p for p in unsigned)
    return ConnectivityFirmware(raw=raw, parts_unsigned=unsigned, parts_signed=signed)


def parse_conn_retry(lines: Sequence[str]) -> int:
    """Parse ``conn_retry_get`` (TCLV 30), in minutes."""
    return _int(_fields(lines)["Net error retry"])


# -------------------------------------------------------------------- WiFi


@dataclass(frozen=True, slots=True)
class WifiConfig:
    """``wifi_conf_get``: SSID, security and band. **No PSK.**

    Attributes:
        ssid: TCLV 65.
        security: TCLV 66, an ASCII string (``"none"``, ``"wpa2"``, ``"wpa2e"``),
            not an integer.
        band: TCLV 68 -- 0 dual, 1 2.4 GHz, 2 5 GHz.

    The getter does **not** echo the passphrase, which is worth knowing before
    you run it on someone's sign: there is no read path for TCLV 67 over USB
    either.
    """

    ssid: str
    security: str
    band: int | None


def parse_wifi_config(lines: Sequence[str]) -> WifiConfig:
    """Parse ``wifi_conf_get``."""
    f = _fields(lines)
    return WifiConfig(
        ssid=f.get("SSID", ""),
        security=f.get("Security", ""),
        band=_int(f["Band"]) if "Band" in f else None,
    )


@dataclass(frozen=True, slots=True)
class WifiMacConfig:
    """``wifi_mac_conf_get``: the radio's MAC (TCLV 110).

    Lowercased by :func:`parse_mac`, because this command prints lowercase and
    ``eth_conf_get`` prints uppercase for the same kind of value.
    """

    mac: str


def parse_wifi_mac_config(lines: Sequence[str]) -> WifiMacConfig:
    """Parse ``wifi_mac_conf_get``."""
    return WifiMacConfig(mac=parse_mac(_fields(lines)["WiFi MAC"]))


@dataclass(frozen=True, slots=True)
class EapConfig:
    """``wifi_eap_conf_get``: WPA2-Enterprise identity (TCLV 74/76).

    Attributes:
        method: e.g. ``"PEAP/MSCHAPV2"`` (TCLV 74).
        username: TCLV 76.

    The password (TCLV 75) is write-only; there is no read path.
    """

    method: str
    username: str


def parse_eap_config(lines: Sequence[str]) -> EapConfig:
    """Parse ``wifi_eap_conf_get``."""
    f = _fields(lines)
    return EapConfig(method=f.get("EAP method", ""), username=f.get("Username", ""))


@dataclass(frozen=True, slots=True)
class CertificateConfig:
    """``certs_config_get``: the EAP certificate slots (TCLV 60-64).

    Attributes:
        present: False when the device says ``"No EAP cert found!"``.
        lines: the reply verbatim, because the populated form has never been
            observed -- the live sign has no certificate, so anything this
            parser claimed about a loaded one would be invented.

    Note this command prints its ``rv: 0`` **before** the body, which is the
    counterexample to "the return code is the last line".
    """

    present: bool
    lines: tuple[str, ...]


def parse_certs_config(lines: Sequence[str]) -> CertificateConfig:
    """Parse ``certs_config_get``."""
    text = " ".join(lines).lower()
    return CertificateConfig(present="no eap cert" not in text, lines=tuple(lines))


def parse_rssi(lines: Sequence[str]) -> int:
    """Parse ``cc3100_rssi`` into **signed dBm** (``-44``).

    The reply is ``"RSSI:-44 dBm"`` with no space after the colon.  Note that
    ``status_get``'s ``SIGNAL_STRENGTH`` carries the same reading with the sign
    *dropped*, so the two differ by a negation and not by a measurement.
    """
    for line in lines:
        match = re.search(r"RSSI\s*:\s*(-?\d+)", line)
        if match:
            return int(match.group(1))
    raise ValueError(f"cannot parse cc3100_rssi: {list(lines)!r}")


@dataclass(frozen=True, slots=True)
class WifiFirmware:
    """``cc3100_fw_version``: the TI CC3100's own version words.

    Attributes:
        nwp: network processor, e.g. ``"2.12.2.8"``.
        mac: MAC layer, e.g. ``"1.5.0.10"``.
        phy: PHY, e.g. ``"1.0.3.37"``.
        chip_id: ``67108864`` == ``0x04000000``.
        rom: ``13107``.

    Unlike ``conn_fw_ver``, these are real. Use this one.
    """

    nwp: str
    mac: str
    phy: str
    chip_id: int | None
    rom: int | None


def parse_wifi_firmware(lines: Sequence[str]) -> WifiFirmware:
    """Parse ``cc3100_fw_version``.

    The reply is space-separated (``"NWP 2.12.2.8"``), not colon-separated, so
    it does not go through :func:`_fields`.
    """
    out: dict[str, str] = {}
    for line in lines:
        parts = line.split()
        if len(parts) >= 2:
            out[parts[0]] = parts[1]
    return WifiFirmware(
        nwp=out.get("NWP", ""),
        mac=out.get("MAC", ""),
        phy=out.get("PHY", ""),
        chip_id=_int(out["ChipId"]) if "ChipId" in out else None,
        rom=_int(out["ROM"]) if "ROM" in out else None,
    )


def parse_bssid(lines: Sequence[str]) -> str:
    """Parse ``wifi_bssid_get`` (TCLV 170) into a normalised MAC.

    Cross-check: ``status_get`` carries the same six bytes split over two
    uint32s, ``BSSID_1`` little-endian over bytes 0-3 and ``BSSID_2`` over bytes
    4-5.  :attr:`StatusDump.bssid` reassembles them and the two agree.
    """
    for line in lines:
        if _MAC_RE.match(line.strip()):
            return parse_mac(line)
    raise ValueError(f"cannot parse wifi_bssid_get: {list(lines)!r}")


# --------------------------------------------------------- network / server


@dataclass(frozen=True, slots=True)
class Ipv4Config:
    """``ipv4_conf_get``: the **stored static** IPv4 configuration (TCLV 13-17).

    Attributes:
        ip, netmask, gateway, dns: TCLV 13, 14, 15, 16.
        mode: TCLV 17 -- 0 static, 1 DHCP.

    **This is configuration, not the live lease.**  On the sign this was read
    from, ``mode`` is 1 (DHCP) and the four addresses are a stale static block
    from some earlier network -- the device was answering on a completely
    different address at the time.  :attr:`in_use` is the guard against
    reporting those as the device's address.
    """

    ip: str
    netmask: str
    gateway: str
    dns: str
    mode: int | None

    @property
    def dhcp(self) -> bool:
        return self.mode == 1

    @property
    def in_use(self) -> bool:
        """True only when ``mode`` is static, so these addresses are the live ones."""
        return self.mode == 0


def parse_ipv4_config(lines: Sequence[str]) -> Ipv4Config:
    """Parse ``ipv4_conf_get``."""
    f = _fields(lines)
    return Ipv4Config(
        ip=f.get("IP", ""),
        netmask=f.get("NM", ""),
        gateway=f.get("GW", ""),
        dns=f.get("DNS", ""),
        mode=_int(f["MODE"]) if "MODE" in f else None,
    )


@dataclass(frozen=True, slots=True)
class EthernetConfig:
    """``eth_conf_get``: the W5500's MAC and TCP retry policy (TCLV 8/9/10).

    Attributes:
        mac: TCLV 8. On the live WiFi sign this is a
            locally-administered address -- the ``02`` bit of the first octet
            is set -- i.e. a synthesised default for an Ethernet MAC that is
            present in the build but not wired up, rather than a real
            vendor-assigned address. :attr:`locally_administered` is the test.
        retry_count: TCLV 9.
        retry_timeout_ms: TCLV 10. The device prints ``TRT: 2000``; the TCLV
            table calls the field "Ethernet TCP retry timeout [100ms]", so the
            **unit is disputed** -- 2000 is a round number of milliseconds and
            an odd number of 100 ms ticks. Reported verbatim and named for what
            the device prints.
    """

    mac: str
    retry_count: int | None
    retry_timeout_ms: int | None

    @property
    def locally_administered(self) -> bool:
        """True when bit 1 of the first octet is set, i.e. not a vendor MAC."""
        return bool(int(self.mac.split(":")[0], 16) & 0x02) if self.mac else False


def parse_eth_config(lines: Sequence[str]) -> EthernetConfig:
    """Parse ``eth_conf_get``."""
    f = _fields(lines)
    return EthernetConfig(
        mac=parse_mac(f["MAC"]) if "MAC" in f else "",
        retry_count=_int(f["TRC"]) if "TRC" in f else None,
        retry_timeout_ms=_int(f["TRT"]) if "TRT" in f else None,
    )


@dataclass(frozen=True, slots=True)
class ServerConfig:
    """``server_tcp_get`` plus ``server_hb_get``: where the sign dials out to.

    Attributes:
        host: TCLV 18. **A DNS name works and is what the live sign holds**,
            which is why re-pointing that name is an alternative to touching
            the device at all.
        port: TCLV 19, 11113 by default.
        heartbeat_minutes: TCLV 29, from ``server_hb_get``. None when only
            ``server_tcp_get`` was parsed.

    TCLV 18 and 19 are both ``canWrite: false`` over the network, so this is the
    pair that makes USB provisioning unavoidable.
    """

    host: str
    port: int | None
    heartbeat_minutes: int | None = None

    @property
    def is_hostname(self) -> bool:
        """True when :attr:`host` is a name rather than a dotted quad."""
        return bool(self.host) and not re.match(r"^\d+\.\d+\.\d+\.\d+$", self.host)


def parse_server_config(
    lines: Sequence[str], heartbeat: Sequence[str] | None = None
) -> ServerConfig:
    """Parse ``server_tcp_get``, optionally folding in ``server_hb_get``."""
    f = _fields(lines)
    hb = None
    if heartbeat is not None:
        hb_fields = _fields(heartbeat)
        if "HB interval" in hb_fields:
            hb = _int(hb_fields["HB interval"])
    return ServerConfig(
        host=f.get("Server IP/DNS", ""),
        port=_int(f["Server port"]) if "Server port" in f else None,
        heartbeat_minutes=hb,
    )


def parse_heartbeat(lines: Sequence[str]) -> int:
    """Parse ``server_hb_get`` (TCLV 29), in minutes."""
    return _int(_fields(lines)["HB interval"])


# ----------------------------------------------------------- power / system


@dataclass(frozen=True, slots=True)
class BatteryConfig:
    """``battery_conf_get``: the low-battery shutdown thresholds (TCLV 36/37/38).

    Attributes:
        threshold_off_mv: TCLV 36 -- below this the sign powers down.
        threshold_on_mv: TCLV 37 -- above this it comes back.
        threshold_count: TCLV 38 -- consecutive readings before acting.

    Note ``off`` (3500 mV) is *higher* than ``on`` (3400 mV) on the live sign,
    which reads like inverted hysteresis. Reported as found; the firmware's
    comparison direction was not traced.
    """

    threshold_off_mv: int | None
    threshold_on_mv: int | None
    threshold_count: int | None


def parse_battery_config(lines: Sequence[str]) -> BatteryConfig:
    """Parse ``battery_conf_get``."""
    f = _fields(lines)
    return BatteryConfig(
        threshold_off_mv=_int(f["Threshold OFF"]) if "Threshold OFF" in f else None,
        threshold_on_mv=_int(f["Threshold ON"]) if "Threshold ON" in f else None,
        threshold_count=_int(f["Threshold CNT"]) if "Threshold CNT" in f else None,
    )


@dataclass(frozen=True, slots=True)
class ChargerState:
    """``bq24023_mode_get``: live readings from the battery charger.

    Attributes:
        mode: e.g. ``"fast charge"``.
        current_ma: ``Ibatt``.
        voltage_mv: ``Vbatt``. 4214 mV on a full pack.
    """

    mode: str
    current_ma: int | None
    voltage_mv: int | None


def parse_charger(lines: Sequence[str]) -> ChargerState:
    """Parse ``bq24023_mode_get``."""
    f = _fields(lines)
    return ChargerState(
        mode=f.get("BQ", ""),
        current_ma=_int(f["Ibatt"]) if "Ibatt" in f else None,
        voltage_mv=_int(f["Vbatt"]) if "Vbatt" in f else None,
    )


def parse_temperature(lines: Sequence[str]) -> int:
    """Parse ``lmr`` -- the LM75 board sensor, in whole degrees Celsius.

    **A different sensor from the panel's.**  ``status_get``'s
    ``EPD_TEMP_SENSOR`` is the panel's own reading, and the two were observed one
    degree apart on a live sign (26 against 25) a second apart.  They agree often
    enough to look like the same value and they are not, so do not use one to
    validate the other.
    """
    return _int(_fields(lines)["LM75"])


@dataclass(frozen=True, slots=True)
class SystemConfig:
    """``system_conf_get``: the three system flags (TCLV 49/50/51).

    Attributes:
        battery_mode: TCLV 49, *"System screens: 1=Battery, 2=Not connected,
            3=Battery+Not connected"*.
        touch_mode: TCLV 50, *"0=OFF, 1=ON, 3=ON+Beep"*. 3 on the live sign --
            which is why it chirps.
        shipping_mode: TCLV 51. **1 means the sign does not wake up normally**,
            so setting it is gated behind ``allow_destructive``.
    """

    battery_mode: int | None
    touch_mode: int | None
    shipping_mode: int | None

    @property
    def touch_enabled(self) -> bool:
        return bool(self.touch_mode)

    @property
    def beeps(self) -> bool:
        return self.touch_mode == 3


def parse_system_config(lines: Sequence[str]) -> SystemConfig:
    """Parse ``system_conf_get``."""
    f = _fields(lines)
    return SystemConfig(
        battery_mode=_int(f["Battery mode"]) if "Battery mode" in f else None,
        touch_mode=_int(f["Touch mode"]) if "Touch mode" in f else None,
        shipping_mode=_int(f["Shipping mode"]) if "Shipping mode" in f else None,
    )


@dataclass(frozen=True, slots=True)
class LogConfig:
    """``log_config_get``: per-module debug logging levels.

    Attributes:
        modules: module name -> level. The live sign reports only
            ``{"Mobile": 0}`` -- one line, although ``log_config_set`` takes a
            ``<module> <value>`` pair and the ``vlog_*`` family implies a richer
            scheme. The getter simply does not print the rest.

    The four ``vlog_*`` commands are the ones that matter for this library:
    ``vlog_unify_levels 0`` silences the USB log destination, which is the clean
    fix for the interleaving problem :mod:`pyvisionect.io.usb.console` works
    around.
    """

    modules: Mapping[str, int]


def parse_log_config(lines: Sequence[str]) -> LogConfig:
    """Parse ``log_config_get``."""
    out: dict[str, int] = {}
    for name, value in _fields(lines).items():
        try:
            out[name] = _int(value)
        except (ValueError, IndexError):
            continue
    return LogConfig(modules=out)


@dataclass(frozen=True, slots=True)
class PmicRegisters:
    """``max17135_dump <i2c_channel>``: the display PMIC's registers.

    Attributes:
        registers: whatever ``name: value`` pairs the dump printed.
        lines: the reply verbatim.

    **Unverified contents.**  ``help`` lists this command as nullary
    (``"max17135_dump: Dump register values"``) and a bare call is rejected with
    ``Incorrect command parameter(s).`` -- the help line is wrong and the command
    wants an I2C channel like its ``max17135_sleep``/``_wakeup``/``_selftest``
    siblings.  The channel was not guessed at on the sign this was written
    against, because the neighbouring commands in that family power the panel
    rails up and down.  So this parser is written to the generic
    ``name: value`` shape and has never seen real output.
    """

    registers: Mapping[str, int]
    lines: tuple[str, ...]


def parse_pmic_dump(lines: Sequence[str]) -> PmicRegisters:
    """Parse ``max17135_dump``. See the caveat on :class:`PmicRegisters`."""
    out: dict[str, int] = {}
    for name, value in _fields(lines).items():
        try:
            out[name] = _int(value)
        except (ValueError, IndexError):
            continue
    return PmicRegisters(registers=out, lines=tuple(lines))


# ------------------------------------------------------------- filesystem


@dataclass(frozen=True, slots=True)
class FileEntry:
    """One row of ``fs_ls``.

    The row is three whitespace-separated columns, ``"/image0.pv2 0 134409"``.
    Only the first and last are labelled anywhere; the middle one is 0 for every
    file on the live sign.

    Attributes:
        path: ``"/image0.pv2"``.
        flags: the middle column. **Meaning unknown** -- 0 for all six files, so
            there is no evidence to generalise from. Named ``flags`` rather than
            guessed at.
        size: bytes.
    """

    path: str
    flags: int
    size: int

    @property
    def name(self) -> str:
        return self.path.lstrip("/")


def parse_fs_ls(lines: Sequence[str]) -> tuple[FileEntry, ...]:
    """Parse ``fs_ls``.

    The live sign holds six ``.pv2`` files -- pre-rendered images in the device's
    own packed format, which is what ``image_rotate_config_set`` cycles through
    and what :mod:`pyvisionect.imaging` produces.
    """
    out: list[FileEntry] = []
    for line in lines:
        parts = line.split()
        if len(parts) == 3 and parts[0].startswith("/"):
            try:
                out.append(
                    FileEntry(path=parts[0], flags=_int(parts[1]), size=_int(parts[2]))
                )
            except ValueError:
                continue
    return tuple(out)


@dataclass(frozen=True, slots=True)
class FilesystemStats:
    """``fs_stats``: block usage.

    Attributes:
        total_blocks: 2048 on the live sign.
        used_blocks: 931.

    ``status_get`` reports the same filesystem in **bytes**
    (``FS_TOTAL_SIZE: 8388608``, ``FS_FREE_SIZE: 4575232``), from which the
    block size falls out: 8388608 / 2048 = **4096 bytes per block**, and
    (2048 - 931) * 4096 = 4575232 exactly. The two commands agree, which is how
    :attr:`block_size` is derived rather than assumed.
    """

    total_blocks: int | None
    used_blocks: int | None

    @property
    def free_blocks(self) -> int | None:
        if self.total_blocks is None or self.used_blocks is None:
            return None
        return self.total_blocks - self.used_blocks

    def block_size(self, total_bytes: int) -> int | None:
        """Bytes per block, given ``FS_TOTAL_SIZE`` from ``status_get``."""
        if not self.total_blocks:
            return None
        return total_bytes // self.total_blocks


def parse_fs_stats(lines: Sequence[str]) -> FilesystemStats:
    """Parse ``fs_stats`` (``"total blocks: 2048, in use: 931"``).

    One line carrying two fields separated by a comma, so it cannot go through
    :func:`_fields`.
    """
    total = used = None
    for line in lines:
        match = re.search(r"total blocks:\s*(\d+)", line)
        if match:
            total = int(match.group(1))
        match = re.search(r"in use:\s*(\d+)", line)
        if match:
            used = int(match.group(1))
    return FilesystemStats(total_blocks=total, used_blocks=used)


# -------------------------------------------------------------- SPI flash


@dataclass(frozen=True, slots=True)
class SpiFlashDevice:
    """One row of ``sf_list``: a registered SPI flash and its bus.

    Attributes:
        index: the ``IDX`` column, which is what ``sf_select`` takes.
        spi_channel: the ``SPI_CHANNEL`` column.

    ``sf_list`` is the *safe* member of this family. ``sf_rdid`` and ``sf_rdst``
    both assert and kill the CLI task -- see
    :data:`pyvisionect.io.usb.commands.ASSERTS_AND_KILLS_CLI`.
    """

    index: int
    spi_channel: int


def parse_sf_list(lines: Sequence[str]) -> tuple[SpiFlashDevice, ...]:
    """Parse ``sf_list``.

    The reply is an ASCII table with a header and a rule line::

        IDX | SPI_CHANNEL
        ----|------------
         00 |  2

    Rows are matched structurally rather than by skipping a fixed line count, so
    a wider table would still parse.
    """
    out: list[SpiFlashDevice] = []
    for line in lines:
        if "|" not in line or set(line) <= set("-| "):
            continue
        left, _, right = line.partition("|")
        if left.strip().isdigit() and right.strip().isdigit():
            out.append(
                SpiFlashDevice(index=int(left.strip(), 10), spi_channel=int(right.strip()))
            )
    return tuple(out)


@dataclass(frozen=True, slots=True)
class SpiFlashStatus:
    """``sf_rdst``'s reply. **Never observed.**

    ``sf_rdst`` asserts at ``spi_flash_cli.c:188`` on firmware 7.4.4407 and
    takes ``usb_cli_task`` with it, so no sample of a successful reply exists.
    This class is here so the accessor has a return type; its fields are a
    guess and are documented as such.
    """

    raw: tuple[str, ...]


# ------------------------------------------------------------ status dump


_STATUS_SENTINEL = "STATUS_END"


@dataclass(frozen=True, slots=True)
class StatusDump:
    """``status_get``: the whole PV3 status record as text.

    This is the same information the sign puts in a type-3 status packet every
    heartbeat -- :mod:`pyvisionect.packets.status` decodes the binary form -- but
    rendered as 60 ``KEY: value`` lines ending in a ``STATUS_END`` sentinel.
    Reading it over USB is the cheapest way to check a value without waiting for
    a heartbeat or decoding a frame.

    Attributes:
        values: the keys verbatim, in the device's SCREAMING_SNAKE spelling,
            with values parsed to ``int`` where they parse. Note one key,
            ``PROTOCOL VERSION``, has a **space** where every other key has an
            underscore -- an inconsistency in the firmware's own table, kept
            as-is rather than normalised, so that a lookup matches what the
            device actually prints.

    Several fields need interpreting rather than reading; the properties below
    do that, and each one is cross-checked against another command in the test
    suite.
    """

    values: Mapping[str, int | str]

    def __getitem__(self, key: str) -> int | str:
        return self.values[key]

    def get(self, key: str, default: object = None) -> object:
        return self.values.get(key, default)

    @property
    def rssi_dbm(self) -> int | None:
        """``SIGNAL_STRENGTH`` negated.

        The field is a dBm magnitude with the sign dropped: ``status_get`` says
        31, ``cc3100_rssi`` says ``-31 dBm``, same moment, same radio.
        """
        value = self.values.get("SIGNAL_STRENGTH")
        return -abs(int(value)) if isinstance(value, int) else None

    @property
    def uptime_minutes(self) -> int | None:
        """``SYSTEM_UPTIME``, in minutes -- agrees with the ``uptime`` command."""
        value = self.values.get("SYSTEM_UPTIME")
        return int(value) if isinstance(value, int) else None

    @property
    def bssid(self) -> str | None:
        """The AP's BSSID, rebuilt from ``BSSID_1`` and ``BSSID_2``.

        Six bytes split over two uint32s: ``BSSID_1`` holds bytes 0-3
        little-endian, ``BSSID_2`` holds bytes 4-5.  Verified against
        ``wifi_bssid_get``, which prints the same address directly.
        """
        one = self.values.get("BSSID_1")
        two = self.values.get("BSSID_2")
        if not isinstance(one, int) or not isinstance(two, int):
            return None
        octets = list(int(one).to_bytes(4, "little")) + list(
            int(two).to_bytes(4, "little")[:2]
        )
        return ":".join(f"{b:02x}" for b in octets)

    @property
    def display_size(self) -> tuple[int, int] | None:
        """``(DISPLAY_WIDTH, DISPLAY_HEIGHT)`` -- of **one** panel.

        2880x640 on the live sign, with ``NUMBER_OF_SUPPORTED_DISPLAYS`` 2, so
        the whole canvas is 2880x1280 and these two keys do not describe it.
        """
        width = self.values.get("DISPLAY_WIDTH")
        height = self.values.get("DISPLAY_HEIGHT")
        if isinstance(width, int) and isinstance(height, int):
            return (int(width), int(height))
        return None

    @property
    def firmware(self) -> str | None:
        """``"7.4.4407"``, assembled from the three ``FIRMWARE_VERSION_*`` keys."""
        parts = [
            self.values.get(f"FIRMWARE_VERSION_{part}")
            for part in ("MAJOR", "MINOR", "REVISION")
        ]
        if all(isinstance(p, int) for p in parts):
            return ".".join(str(p) for p in parts)
        return None

    @property
    def gtin(self) -> str | None:
        """``GTIN``, as a string. Matches ``gtin_get``."""
        value = self.values.get("GTIN")
        return None if value is None else str(value)

    @property
    def filesystem_block_size(self) -> int | None:
        """``FS_TOTAL_SIZE`` divided by ``fs_stats``' 2048 blocks, if both known.

        Kept as a method-like property rather than a constant because the block
        count comes from a different command; see
        :meth:`FilesystemStats.block_size`.
        """
        total = self.values.get("FS_TOTAL_SIZE")
        return int(total) // 2048 if isinstance(total, int) else None


def parse_status_dump(lines: Sequence[str]) -> StatusDump:
    """Parse ``status_get``.

    Stops at the ``STATUS_END`` sentinel.  Values that look like integers
    (decimal, signed decimal, or ``0x`` hex) become ``int``; anything else stays
    a string, so a future firmware adding a textual field does not break this.
    """
    out: dict[str, int | str] = {}
    for line in lines:
        if line.strip() == _STATUS_SENTINEL:
            break
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        try:
            out[key] = int(value, 0)
        except ValueError:
            out[key] = value
    return StatusDump(values=out)


# ------------------------------------------------------------------ tasks


@dataclass(frozen=True, slots=True)
class Task:
    """One row of ``task_list`` -- a FreeRTOS task.

    Attributes:
        name: as registered, e.g. ``"usb_cli_task"``, ``"CC3100 RX"``. Names can
            contain spaces, which is why the reply is tab-separated.
        state: the FreeRTOS one-letter state. ``X`` running, ``R`` ready,
            ``B`` blocked, ``S`` suspended, ``D`` deleted. The task that is
            answering the command is always ``X``.
        stack_free: the high-water mark in **words**, not bytes -- the
            ``uxTaskGetStackHighWaterMark`` unit.
        number: the task number.
    """

    name: str
    state: str
    stack_free: int
    number: int

    @property
    def running(self) -> bool:
        return self.state == "X"

    @property
    def blocked(self) -> bool:
        return self.state == "B"


@dataclass(frozen=True, slots=True)
class TaskList:
    """``task_list``: the scheduler's tasks plus the free-heap figure.

    Attributes:
        tasks: one :class:`Task` per row.
        heap_free: the ``Heap free:`` figure in bytes -- 19136 on the live sign,
            which is not a lot of headroom and is worth watching if you add
            work to the device.

    ``usb_cli_task`` in here is the one that dies when ``sf_rdid`` asserts.
    """

    tasks: tuple[Task, ...]
    heap_free: int | None = None

    def __getitem__(self, name: str) -> Task:
        for task in self.tasks:
            if task.name == name:
                return task
        raise KeyError(name)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(t.name for t in self.tasks)


def parse_task_list(lines: Sequence[str]) -> TaskList:
    """Parse ``task_list``.

    The columns are **tab**-separated and the name column is space-padded to a
    fixed width, so splitting on whitespace would shred a name like
    ``"CC3100 RX"`` or ``"timer ms"``.
    """
    tasks: list[Task] = []
    heap_free = None
    for line in lines:
        if line.startswith("Heap free"):
            try:
                heap_free = _int(line.partition(":")[2])
            except (ValueError, IndexError):
                pass
            continue
        parts = line.split("\t")
        if len(parts) != 4 or parts[0].strip() == "task name":
            continue
        name, state, stack, number = (p.strip() for p in parts)
        if not stack.isdigit() or not number.isdigit():
            continue
        tasks.append(
            Task(name=name, state=state, stack_free=int(stack), number=int(number))
        )
    return TaskList(tasks=tuple(tasks), heap_free=heap_free)
