'''USB serial access to a Visionect sign -- the only way to bootstrap one.

Why this package exists
-----------------------

Eight TCLV ids are ``canWrite: false`` in the vendor's own admin-UI descriptor
table, and they are exactly the ones a first-time setup needs: ``CONN_TYPE`` (2),
``IPV4_SERVER_IP`` (18), ``IPV4_SERVER_PORT`` (19), ``WIFI_SSID`` (65),
``WIFI_SECURITY`` (66), ``WIFI_PSK`` (67), ``WIFI_BAND`` (68),
``MOB_SECURITY`` (70).  The network protocol reads them and never writes them, so
there is no over-the-air escape -- you cannot tell a sign about a new WiFi
network over the network it does not have.  USB, or nothing.

(Or DNS: if the sign already holds a *hostname* in TCLV 18, re-point that name
and touch nothing.  :func:`~pyvisionect.io.usb.provisioning.describe_alternatives`
puts this in the output of every dry run, because it is the cheapest answer and
the one nobody thinks of.)

The transport is a USB-to-UART bridge -- **FTDI FT232, ``0403:6001``, confirmed
on hardware** -- carrying a line-oriented ASCII shell at **115200 8N1**.  Submit
with ``\\r``; the prompt is ``"> "``; the device echoes.  The firmware also logs
to the same UART unprompted, which is the one genuinely hard problem here and the
subject of :mod:`~pyvisionect.io.usb.console`.

Layout
------

================================================  =============================
:mod:`~pyvisionect.io.usb.console`                the transport: frames, prompt
                                                  sync, async-log separation
:mod:`~pyvisionect.io.usb.commands`               the 112 commands this firmware
                                                  has, the 111 ``help`` admits
                                                  to, and the 95 unlisted ones
:mod:`~pyvisionect.io.usb.parsers`                one dataclass per getter
:mod:`~pyvisionect.io.usb.device`                 :class:`Sign` -- the typed
                                                  surface, with write gating
:mod:`~pyvisionect.io.usb.provisioning`           dry-runnable bootstrap flows
:mod:`~pyvisionect.io.usb.discovery`              find the port, confirm the sign
:mod:`~pyvisionect.io.usb.encryption`             what is known about the
                                                  encryption setters, and how
:mod:`~pyvisionect.io.usb.fake`                   the real sign's real replies,
                                                  for tests
================================================  =============================

Quick start
-----------

Read-only, which is the default and cannot change anything::

    from pyvisionect.io.usb import SerialConsole, Sign

    with SerialConsole("/dev/ttyUSB0") as console:
        console.sync()
        sign = Sign(console)
        print(sign.dump().describe())

Repointing a sign at your own server, reviewed before it happens::

    from pyvisionect.io.usb import Sign, SerialConsole, plan_repoint

    plan = plan_repoint("my-listener.example.com")
    print(plan.describe())              # read this. it is the point.

    with SerialConsole("/dev/ttyUSB0") as console:
        console.sync()
        sign = Sign(console, allow_destructive=True)
        plan.execute(sign)

Two things that will bite you
-----------------------------

* **Nothing persists until ``flash_save``.**  Verbatim from the vendor: *"All
  changes to device configuration are by default retained only in the working
  RAM."*  That is a safety net right up until you use it.
* **``sf_rdid`` and ``sf_rdst`` kill the console.**  They assert in the firmware
  and take ``usb_cli_task`` with them; the sign keeps running but the prompt is
  gone until it reboots, which it does on its own about 25 minutes later when the
  software watchdog gives up.  Both are gated behind ``i_really_mean_it`` for
  that reason alone -- they read nothing and write nothing.  Measured, not
  theorised.

Soft sleep
----------

The vendor documents a "soft sleep" that cuts the console after 15 seconds of
accelerometer inactivity, and recommends setting ``SLEEP_MODE`` (52) to 1 for a
working session.  **It did not happen on firmware 7.4.4407.**  A console session
held a prompt across an undisturbed 70-second idle window and answered
immediately afterwards, on a sign whose uptime was 25 days -- so the CLI had been
reachable that whole time without anyone touching it.  This package therefore
does **not** send a keepalive and does **not** touch TCLV 52, both of which would
be writes on a read-only session.  If you meet a build where it does bite,
``SLEEP_MODE`` is the documented lever.
'''

from __future__ import annotations

from .commands import (
    ABSENT_FROM_7_4_4407,
    HIDDEN_IN_7_4_4407,
    LISTED_BY_HELP,
    ASSERTS_AND_KILLS_CLI,
    COMMANDS,
    DESTRUCTIVE_COMMANDS,
    DOCUMENTED_COUNT,
    HARD_COMMANDS,
    READ_COMMANDS,
    TCLV_FOR,
    UNDOCUMENTED_IN_7_4_4407,
    Command,
    Kind,
)
from .console import (
    BAUD_RATE,
    LOG_PATTERNS,
    PROMPT,
    TERMINATOR,
    CommandResult,
    LogLine,
    SerialConsole,
    SerialTransport,
    classify,
)
from .device import Sign, SignInfo
from .discovery import (
    FTDI_PIDS,
    FTDI_VID,
    KNOWN_BRIDGES,
    Candidate,
    Identity,
    find_ports,
    identify,
)
from .errors import (
    CommandNotInFirmware,
    ConsoleAssertionFailed,
    ConsoleError,
    ConsoleNotOpen,
    ConsoleTimeout,
    DangerousCommandRefused,
    IncorrectParameters,
    PromptNotFound,
    UnknownCommand,
    WriteNotAllowed,
)
from .provisioning import (
    DEFAULT_SERVER_PORT,
    Plan,
    Step,
    WifiSecurity,
    describe_alternatives,
    plan_bootstrap,
    plan_repoint,
    plan_wifi,
)

__all__ = [
    "ABSENT_FROM_7_4_4407",
    "HIDDEN_IN_7_4_4407",
    "LISTED_BY_HELP",
    "ASSERTS_AND_KILLS_CLI",
    "BAUD_RATE",
    "COMMANDS",
    "DEFAULT_SERVER_PORT",
    "DESTRUCTIVE_COMMANDS",
    "DOCUMENTED_COUNT",
    "FTDI_PIDS",
    "FTDI_VID",
    "HARD_COMMANDS",
    "KNOWN_BRIDGES",
    "LOG_PATTERNS",
    "PROMPT",
    "READ_COMMANDS",
    "TCLV_FOR",
    "TERMINATOR",
    "UNDOCUMENTED_IN_7_4_4407",
    "Candidate",
    "Command",
    "CommandNotInFirmware",
    "CommandResult",
    "ConsoleAssertionFailed",
    "ConsoleError",
    "ConsoleNotOpen",
    "ConsoleTimeout",
    "DangerousCommandRefused",
    "Identity",
    "IncorrectParameters",
    "Kind",
    "LogLine",
    "Plan",
    "PromptNotFound",
    "SerialConsole",
    "SerialTransport",
    "Sign",
    "SignInfo",
    "Step",
    "UnknownCommand",
    "WifiSecurity",
    "WriteNotAllowed",
    "classify",
    "describe_alternatives",
    "find_ports",
    "identify",
    "main",
    "parsers",
    "plan_bootstrap",
    "plan_repoint",
    "plan_wifi",
]


def __getattr__(name: str) -> object:
    """Lazy re-exports.

    ``main`` pulls in :mod:`argparse` and ``parsers`` is only needed by name when
    someone wants the dataclasses without the typed surface; neither belongs in
    the import cost of ``from pyvisionect.io.usb import Sign``.
    """
    if name == "main":
        from .cli import main

        return main
    if name == "parsers":
        from . import parsers

        return parsers
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
