'''Provisioning flows, every one of them dry-runnable.

A :class:`Plan` is the exact sequence of command lines a flow would send,
produced without touching the port.  Build it, read it, then
:meth:`~Plan.execute` it -- or never execute it at all, which is the point.
Printing the plan is the only way to review a bootstrap *before* it strands a
sign somewhere you cannot reach it, and every command in here is in the
``DESTRUCTIVE`` tier precisely because getting it wrong means fetching a cable.

Why USB at all
--------------

Eight TCLV ids are ``canWrite: false`` in the vendor's own admin-UI descriptor
table, and they are exactly the ones a bootstrap needs: ``CONN_TYPE`` (2),
``IPV4_SERVER_IP`` (18), ``IPV4_SERVER_PORT`` (19), ``WIFI_SSID`` (65),
``WIFI_SECURITY`` (66), ``WIFI_PSK`` (67), ``WIFI_BAND`` (68) and
``MOB_SECURITY`` (70).  The network protocol can read them and will never write
them, so there is no over-the-air escape: you cannot tell a sign about a new
WiFi network over the network it does not have.  See
:data:`pyvisionect.devices.tclv.NETWORK_READ_ONLY`.

The cheaper alternative
-----------------------

**If the sign already holds a hostname, you do not need any of this.**  The live
sign holds ``visionect.internal.example.com`` in TCLV 18, so re-pointing that
name at your own listener moves the sign with zero commands and zero risk.
:func:`describe_alternatives` says so in the output of a dry run, because it is
the first thing someone about to run a bootstrap should consider and the last
thing they will think of.

Whitespace in an SSID or passphrase
-----------------------------------

**A spaced SSID works, and is emitted.**  Measured on firmware 7.4.4407:
``wifi_ssid_set`` takes **everything after the command name and one separating
space, verbatim** -- interior spaces, runs of spaces, leading and trailing
spaces, apostrophes, quotes and backslashes all land in TCLV 65 byte for byte.
There is **no quoting or escaping convention**, so ``"My Home WiFi"`` sets an
SSID whose first and last characters are double quotes.  See ``A2`` in
``OPEN-QUESTIONS.md`` for the full table of what landed for each form.

So when the SSID contains whitespace, :func:`plan_wifi` drops ``wifi_conf_set``
-- which really is positional, and would shift the psk, security and band
arguments along by one -- and emits the three single-argument setters instead::

    wifi_psk_set <psk>
    wifi_security_set <security>
    wifi_ssid_set <ssid>

SSID last, so the field that decides association is the final write.  Two
things do not come free:

* **Band (TCLV 68) cannot be written this way.**  There is no ``wifi_band_set``
  -- not in ``help``, not in the vendor's 162 documented commands --
  ``wifi_conf_set`` is its only writer, and that is the one command the space
  rules out.  A spaced SSID with a non-default *band* is therefore still
  refused; with ``band=0`` the plan carries a note that TCLV 68 is left alone.
* **``help`` still does not list ``wifi_ssid_set``.**  It is hidden, not absent
  (:data:`pyvisionect.io.usb.commands.HIDDEN_IN_7_4_4407`), so
  :meth:`Plan.execute` exempts hidden names from its missing-command check and
  sends them with the firmware-command gate off.  Nothing else about that gate
  changes: ``help`` is still the only evidence available on a firmware this
  library has not seen.

**The passphrase is still refused.**  ``wifi_psk_set`` is a single-argument
command of exactly the same shape, so it very probably carries a space too --
but there is **no read path for TCLV 67**.  ``wifi_conf_get`` returns SSID,
security and band and never the passphrase, so nothing can be read back to
check.  The SSID experiment was safe because every write was read straight
back; a truncated passphrase has no such oracle and fails silently, as a sign
that will not associate and then power-cycles itself on
``E: Max conn errs. Reboot`` with ``ErrorCode`` still ``0x0``.  Rename the
network, or move the sign by re-pointing the DNS name it already holds.
'''

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from .commands import COMMANDS, HIDDEN_IN_7_4_4407, Kind
from .console import CommandResult
from .device import Sign

__all__ = [
    "DEFAULT_SERVER_PORT",
    "Plan",
    "Step",
    "WifiSecurity",
    "describe_alternatives",
    "plan_bootstrap",
    "plan_repoint",
    "plan_wifi",
]

DEFAULT_SERVER_PORT = 11113
"""The port a sign dials out to, and the port :mod:`pyvisionect.io.tcp` listens on."""


class WifiSecurity:
    """``WIFI_SECURITY`` (TCLV 66) values. **ASCII strings, not integers.**

    Confirmed on the live sign, which reports ``Security: wpa2`` from
    ``wifi_conf_get``.
    """

    OPEN = "none"
    WPA2 = "wpa2"
    WPA2_ENTERPRISE = "wpa2e"

    ALL = (OPEN, WPA2, WPA2_ENTERPRISE)


@dataclass(frozen=True, slots=True)
class Step:
    """One command in a :class:`Plan`.

    Attributes:
        command: the exact line that would be written.
        why: one sentence on what it does and what it costs. Shown by
            :meth:`Plan.describe`, so a dry run reads as an explanation rather
            than a list of strings.
        expect_prompt: False for ``reboot``, which never prompts again.
    """

    command: str
    why: str
    expect_prompt: bool = True

    @property
    def name(self) -> str:
        return self.command.split()[0] if self.command.split() else ""

    @property
    def kind(self) -> Kind:
        entry = COMMANDS.get(self.name)
        return entry.kind if entry is not None else Kind.WRITE

    @property
    def in_firmware(self) -> bool:
        """Whether 7.4.4407 is known to have this command.

        Checked against :data:`~pyvisionect.io.usb.commands.COMMANDS`, which is
        every command known to exist -- including the ones ``help`` does not
        print.  False therefore means "not in the table", which is weaker than
        "not in the firmware": see
        :data:`~pyvisionect.io.usb.commands.HIDDEN_IN_7_4_4407`.
        """
        return self.name in COMMANDS

    @property
    def hidden(self) -> bool:
        """Whether ``help`` omits this command although the firmware has it.

        True only for :data:`~pyvisionect.io.usb.commands.HIDDEN_IN_7_4_4407`
        -- ``wifi_ssid_set``, and nothing else as of 7.4.4407.  Such a step has
        to bypass :meth:`~pyvisionect.io.usb.device.Sign`'s ``help``-derived
        gate, which would otherwise refuse a command that demonstrably works.
        """
        return self.name in HIDDEN_IN_7_4_4407


@dataclass(frozen=True, slots=True)
class Plan:
    """A provisioning sequence that has not happened yet.

    Attributes:
        name: what the plan is for, e.g. ``"bootstrap"``.
        steps: the commands, in order.
        notes: caveats worth reading before executing, including the
            "you may not need this at all" alternatives.

    Example:
        >>> plan = plan_repoint("my-server.example.com")
        >>> print(plan.describe())                       # doctest: +ELLIPSIS
        bootstrap plan: repoint
        ...
        >>> plan.commands
        ('server_tcp_set my-server.example.com 11113', 'flash_save', 'reboot')
    """

    name: str
    steps: tuple[Step, ...]
    notes: tuple[str, ...] = ()

    @property
    def commands(self) -> tuple[str, ...]:
        """Just the command lines -- what a dry run is usually asked for."""
        return tuple(step.command for step in self.steps)

    @property
    def unavailable(self) -> tuple[str, ...]:
        """Steps whose command firmware 7.4.4407 does not have.

        Non-empty means this plan cannot run as written on that firmware.
        """
        return tuple(step.name for step in self.steps if not step.in_firmware)

    def describe(self) -> str:
        """The plan as readable text: every command, what it does, and the caveats."""
        out = [f"bootstrap plan: {self.name}", ""]
        for index, step in enumerate(self.steps, 1):
            if not step.in_firmware:
                flag = "   [NOT IN FIRMWARE 7.4.4407]"
            elif step.hidden:
                flag = "   [HIDDEN FROM help, BUT PRESENT AND VERIFIED]"
            else:
                flag = ""
            out.append(f"{index}. {step.command}{flag}")
            out.append(f"     {step.why}")
        if self.notes:
            out.append("")
            out.append("Before you run this:")
            out.extend(f"  - {note}" for note in self.notes)
        return "\n".join(out)

    def execute(self, sign: Sign) -> list[CommandResult]:
        """Send every step, in order.

        *sign* must have been built with ``allow_destructive=True``; every step
        in every plan here is in that tier, so a read-only or write-only
        :class:`~pyvisionect.io.usb.device.Sign` refuses at the first command
        and nothing partial happens.

        Returns:
            One :class:`~pyvisionect.io.usb.console.CommandResult` per step.

        Raises:
            RuntimeError: if the plan contains a command this firmware lacks and
                the firmware's command list is known. Better to refuse than to
                run half a bootstrap and leave the sign with new WiFi and an old
                server, which is the worst of both. A :attr:`Step.hidden`
                command is exempt: ``help`` does not list ``wifi_ssid_set`` and
                the device answers it anyway, so refusing on ``help`` alone
                would block a verified command.
        """
        missing = [
            step.name
            for step in self.steps
            if sign.has(step.name) is False and not step.hidden
        ]
        if missing:
            raise RuntimeError(
                f"this firmware does not have {missing!r}, so the {self.name!r} "
                f"plan cannot run as written. Running it part-way would leave the "
                f"sign half-provisioned."
            )
        results: list[CommandResult] = []
        for step in self.steps:
            results.append(
                sign._run(
                    step.command,
                    check=not step.hidden,
                    expect_prompt=step.expect_prompt,
                )
            )
        return results


def describe_alternatives(server: str) -> tuple[str, ...]:
    """The notes every plan carries: what to consider instead of writing flash."""
    return (
        "Nothing persists until flash_save, so you can run the setters, read "
        "them back, and power-cycle to undo everything up to that point.",
        "If the sign already holds a DNS *name* in TCLV 18 (the live one holds "
        "visionect.internal.example.com), re-pointing that name at your "
        "listener moves it with no commands at all. Check server_tcp_get first.",
        f"After reboot the sign dials out to {server}; if it cannot reach it, "
        "IPV4_SERVER_IP is read-only over the network, so USB is the only way "
        "back. Have the cable to hand.",
        "The sign holds its network session while on USB, so you can watch the "
        "old server drop and the new one pick up.",
    )


def plan_wifi(
    ssid: str,
    psk: str,
    *,
    security: str = WifiSecurity.WPA2,
    band: int = 0,
) -> Plan:
    """Set the WiFi credentials -- TCLV 65/67/66/68.

    Normally one ``wifi_conf_set``.  When *ssid* contains whitespace that
    command cannot be used, because it is positional and the space would shift
    *psk*, *security* and *band* along by one; the plan becomes
    ``wifi_psk_set`` -> ``wifi_security_set`` -> ``wifi_ssid_set`` instead, with
    the SSID written last.  ``wifi_ssid_set`` takes the rest of the line
    verbatim -- verified on 7.4.4407, see the module docstring -- so no quoting
    is applied, and none would help.

    Args:
        ssid: TCLV 65. May contain spaces. May **not** contain a tab: the
            console's line editor treats TAB as its usage-lookup key and eats
            it, so the SSID that lands is the two halves concatenated.
        psk: TCLV 67. Whitespace is refused; see the module docstring.
        security: TCLV 66. One of :attr:`WifiSecurity.ALL`.
        band: TCLV 68 -- 0 dual, 1 2.4 GHz, 2 5 GHz. Only ``wifi_conf_set``
            writes it, so a spaced *ssid* requires the default ``0``.

    Raises:
        ValueError: if *security* is not a known value; if *psk* contains
            whitespace; if *ssid* contains a tab, a carriage return or a
            newline; or if *ssid* contains whitespace and *band* is not 0.
    """
    if security not in WifiSecurity.ALL:
        raise ValueError(
            f"security must be one of {WifiSecurity.ALL}, got {security!r}; "
            "these are ASCII strings, not integers"
        )
    if any(c.isspace() for c in psk):
        raise ValueError(
            "psk contains whitespace. wifi_psk_set is a single-argument "
            "command and so probably would carry it -- wifi_ssid_set, the same "
            "shape, provably does -- but there is no read path for TCLV 67: "
            "wifi_conf_get returns SSID, security and band and never the "
            "passphrase, so there is no way to check what landed. A truncated "
            "passphrase does not fail loudly, it just stops the sign "
            "associating, and this firmware then power-cycles itself on "
            "'E: Max conn errs. Reboot' with ErrorCode still 0x0. Rename the "
            "network, or leave WiFi alone and move the sign by re-pointing the "
            "DNS name it already holds."
        )
    bad = {"\t": "a tab", "\r": "a carriage return", "\n": "a newline"}
    for char, name in bad.items():
        if char in ssid:
            raise ValueError(
                f"ssid contains {name}, which this console cannot carry. CR "
                "and LF submit the line; TAB is the line editor's "
                "usage-lookup key and is swallowed, so 'Two<TAB>Words' sets "
                "'TwoWords' -- measured on 7.4.4407. Spaces are fine."
            )
    if not any(c.isspace() for c in ssid):
        return Plan(
            name="wifi",
            steps=(
                Step(
                    f"wifi_conf_set {ssid} {psk} {security} {band}",
                    "writes TCLV 65/67/66/68 (SSID, password, security, band) to RAM. "
                    "Note the argument order is ssid, psk, security, band while the "
                    "ids run 65, 67, 66, 68.",
                ),
            ),
            notes=(
                "RAM only. Follow with flash_save, or use plan_bootstrap.",
                "wifi_conf_get will read back the SSID, security and band but never "
                "the passphrase -- there is no read path for TCLV 67.",
            ),
        )
    if band != 0:
        raise ValueError(
            f"ssid {ssid!r} contains whitespace, so wifi_conf_set is out, and "
            f"wifi_conf_set is the only command that writes the band (TCLV 68) "
            f"-- there is no wifi_band_set in help or in the vendor's 162 "
            f"documented commands. So band={band} cannot be combined with a "
            f"spaced SSID. Pass band=0 (dual, the default) to leave TCLV 68 as "
            f"the device already has it, or rename the network."
        )
    return Plan(
        name="wifi",
        steps=(
            Step(
                f"wifi_psk_set {psk}",
                "writes TCLV 67. Separate from the SSID because wifi_conf_set is "
                "positional and the space in the SSID would shift this argument.",
            ),
            Step(
                f"wifi_security_set {security}",
                "writes TCLV 66, an ASCII string -- 'none', 'wpa2' or 'wpa2e'.",
            ),
            Step(
                f"wifi_ssid_set {ssid}",
                "writes TCLV 65. Hidden from help on 7.4.4407 but present, and it "
                "takes the rest of the line verbatim, which is what carries the "
                "space. Last on purpose: the SSID is the field that decides "
                "association, so it is the one written once everything else is in "
                "place.",
            ),
        ),
        notes=(
            "RAM only. Follow with flash_save, or use plan_bootstrap.",
            f"The SSID has whitespace, so this is the three-setter route and "
            f"wifi_conf_set is not used. The SSID is sent unquoted and "
            f"unescaped, because wifi_ssid_set takes the rest of the line "
            f"literally: it will land as exactly {ssid!r}, quotes and "
            f"backslashes included if you put any there.",
            "Band (TCLV 68) is NOT written by this route -- only wifi_conf_set "
            "writes it and there is no wifi_band_set. The device keeps whatever "
            "it already has; read it back with wifi_conf_get.",
            "wifi_ssid_set is absent from help, so Sign's help-derived command "
            "gate is bypassed for that one step. The device answers it; help is "
            "simply an incomplete index.",
            "wifi_conf_get will read back the SSID, security and band but never "
            "the passphrase -- there is no read path for TCLV 67.",
        ),
    )


def plan_repoint(
    server: str, port: int = DEFAULT_SERVER_PORT, *, reboot: bool = True
) -> Plan:
    """Move a sign to a new server, leaving WiFi alone.

    ``server_tcp_set`` -> ``flash_save`` -> ``reboot``.  The common case: the
    sign is already on a network you control and you only want it talking to
    your listener instead of the vendor's.

    Args:
        server: an IP or a DNS name. A name works and is what the live sign
            holds.
        port: TCLV 19, :data:`DEFAULT_SERVER_PORT` by default.
        reboot: drop the final reboot, to batch it with other changes.
    """
    steps = [
        Step(
            f"server_tcp_set {server} {port}",
            "writes TCLV 18 (server IP or DNS name) and 19 (port) to RAM. Both "
            "are canWrite:false over the network, which is why this needs USB.",
        ),
        Step(
            "flash_save",
            "TCLV 53. Commits the above to flash. Until this runs, a power-cycle "
            "undoes everything; after it, USB is the only way back.",
        ),
    ]
    if reboot:
        steps.append(
            Step(
                "reboot",
                "applies the new server. The console goes away and does not "
                "prompt again; the sign is back in about a minute and chirps.",
                expect_prompt=False,
            )
        )
    return Plan(
        name="repoint", steps=tuple(steps), notes=describe_alternatives(server)
    )


def plan_bootstrap(
    ssid: str | None,
    psk: str | None,
    server: str,
    port: int = DEFAULT_SERVER_PORT,
    *,
    security: str = WifiSecurity.WPA2,
    band: int = 0,
    conn_type: int | None = None,
    reboot: bool = True,
) -> Plan:
    """The full first-time sequence, verbatim from the vendor reference.

    ``wifi_conf_set`` -> ``server_tcp_set`` -> ``flash_save`` -> ``reboot``,
    with the WiFi step expanding to three commands when the SSID has a space in
    it (see :func:`plan_wifi`).

    Args:
        ssid: TCLV 65, or None to leave WiFi untouched -- in which case this is
            :func:`plan_repoint` with extra notes.
        psk: TCLV 67. Required when *ssid* is given.
        server: TCLV 18.
        port: TCLV 19.
        security: TCLV 66.
        band: TCLV 68.
        conn_type: TCLV 2, inserted before the WiFi step when given. **Enumerate
            with ``conn_type_list`` first**: firmware 7.4.4407 offers 0, 3 and 6,
            while the vendor's own 4G example passes 8.
        reboot: drop the final reboot.

    Raises:
        ValueError: if *ssid* is given without *psk*, or whatever
            :func:`plan_wifi` refuses -- a *psk* containing whitespace, a tab
            in the *ssid*, or a spaced *ssid* with a non-default *band*. A
            spaced *ssid* on its own is fine and expands to the three-setter
            route.
    """
    steps: list[Step] = []
    if conn_type is not None:
        steps.append(
            Step(
                f"conn_type_set {conn_type}",
                f"writes TCLV 2. Check conn_type_list first -- this firmware only "
                f"offers the values it lists, and {conn_type} may not be one.",
            )
        )
    if ssid is not None:
        if psk is None:
            raise ValueError("psk is required when ssid is given")
        steps.extend(plan_wifi(ssid, psk, security=security, band=band).steps)
    steps.extend(plan_repoint(server, port, reboot=reboot).steps)
    return Plan(
        name="bootstrap",
        steps=tuple(steps),
        notes=describe_alternatives(server)
        + (
            "Read the whole plan back with .describe() before executing. A "
            "half-run bootstrap leaves the sign on the new WiFi and the old "
            "server, or vice versa.",
        ),
    )
