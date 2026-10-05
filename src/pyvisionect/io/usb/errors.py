"""Errors raised by the USB serial console layer.

All of these subclass :class:`pyvisionect.wire.errors.VisionectError`, so a
caller can catch the whole library with one ``except``.
"""

from __future__ import annotations

from ...wire.errors import VisionectError

__all__ = [
    "ConsoleError",
    "ConsoleNotOpen",
    "ConsoleTimeout",
    "PromptNotFound",
    "UnknownCommand",
    "IncorrectParameters",
    "ConsoleAssertionFailed",
    "CommandNotInFirmware",
    "WriteNotAllowed",
    "DangerousCommandRefused",
]


class ConsoleError(VisionectError):
    """Base class for serial-console faults."""


class ConsoleNotOpen(ConsoleError):
    """A command was issued on a console that is closed."""


class ConsoleTimeout(ConsoleError):
    """No prompt arrived inside the command timeout.

    Attributes:
        command: the line that was sent.
        partial: whatever bytes did arrive, decoded. Worth printing: a firmware
            assertion (see :class:`ConsoleAssertionFailed`) shows up here.
    """

    def __init__(self, command: str, partial: str, timeout: float) -> None:
        self.command = command
        self.partial = partial
        self.timeout = timeout
        super().__init__(
            f"no prompt within {timeout:g}s after {command!r}; "
            f"got {partial!r}. If the partial output contains 'assert:', the "
            f"firmware's CLI task has died and only a device reboot brings it back."
        )


class PromptNotFound(ConsoleError):
    """:meth:`~pyvisionect.io.usb.console.SerialConsole.sync` could not find the prompt.

    Either nothing is listening on the port, the baud rate is wrong, or the
    firmware's ``usb_cli_task`` has died (see :class:`ConsoleAssertionFailed`).
    """


class UnknownCommand(ConsoleError):
    """The firmware does not have that command.

    Verbatim from the device::

        Command 'xyz' not recognised.  Enter 'help' to view a list of available commands.

    The documented command set is a superset of any one firmware's: commands are
    guarded by compile-time switches.  ``help`` on firmware 7.4.4407 on a 32"
    board lists 111 of the 160 documented commands.  See
    :data:`pyvisionect.io.usb.commands.ABSENT_FROM_7_4_4407` -- and note that
    membership there means "not listed", not "not there": this error is the only
    reply that *proves* a command is missing, which is why
    :data:`pyvisionect.io.usb.commands.HIDDEN_IN_7_4_4407` exists.
    """

    def __init__(self, command: str) -> None:
        self.command = command
        super().__init__(
            f"the firmware does not recognise {command!r}. Run `help` -- it is "
            f"authoritative for the firmware in front of you; the vendor's "
            f"reference documents commands this build was not compiled with."
        )


class IncorrectParameters(ConsoleError):
    """Wrong argument count or an unparseable argument.

    Verbatim from the device::

        Incorrect command parameter(s).  Enter "help" to view a list of available commands.

    Note the device uses *double* quotes around ``help`` here and *single*
    quotes in the unknown-command message.  Both strings are matched literally.

    This also fires for commands whose ``help`` line is simply wrong about its
    arity: ``max17135_dump`` is documented as taking no arguments and rejects a
    bare invocation.
    """

    def __init__(self, command: str) -> None:
        self.command = command
        super().__init__(
            f"the firmware rejected the arguments to {command!r}. Check `help` "
            f"for the real arity -- a few help lines on 7.4.4407 understate it "
            f"(max17135_dump is documented as nullary but requires an i2c channel)."
        )


class ConsoleAssertionFailed(ConsoleError):
    """The firmware hit an ``assert:`` and the CLI task is now dead.

    **Observed live on firmware 7.4.4407.**  ``sf_rdid`` and ``sf_rdst`` both
    assert immediately -- ``assert: spi_flash_cli.c:139`` and
    ``assert: spi_flash_cli.c:188`` -- because neither checks that
    ``sf_select`` has chosen a device first.  The assertion kills
    ``usb_cli_task`` (``task_list`` stops answering, the prompt never returns)
    while the rest of the firmware keeps running: the sign stays on the network,
    keeps its heartbeat, and keeps its picture.  The software watchdog notices
    (``W WD task timeout: cli_USB`` appears on the UART) but does not restart
    the task; the console comes back only after a device reboot.

    Attributes:
        command: the command that asserted.
        location: e.g. ``"spi_flash_cli.c:139"``.
    """

    def __init__(self, command: str, location: str) -> None:
        self.command = command
        self.location = location
        super().__init__(
            f"{command!r} tripped a firmware assertion at {location}. The CLI "
            f"task is dead and will not recover on its own -- the sign itself is "
            f"fine (network, heartbeat and display keep running), but the serial "
            f"console needs a device reboot. Known offenders on 7.4.4407: "
            f"sf_rdid, sf_rdst."
        )


class CommandNotInFirmware(ConsoleError):
    """A typed accessor was called for a command this firmware lacks.

    Raised locally, before anything is written, when the console has learned the
    firmware's command list (via :meth:`~pyvisionect.io.usb.device.Sign.refresh_commands`)
    and the command is not in it.
    """

    def __init__(self, command: str) -> None:
        self.command = command
        super().__init__(
            f"{command!r} is not in this firmware's `help` output, so it was not "
            f"sent. Pass check=False to send it anyway."
        )


class WriteNotAllowed(ConsoleError):
    """A setter was called on a console opened read-only.

    Mirrors :class:`pyvisionect.wire.errors.ReadOnlyParameter`: the message
    names the flag that unlocks the call rather than just refusing.
    """

    def __init__(self, command: str, flag: str, why: str) -> None:
        self.command = command
        self.flag = flag
        super().__init__(f"{command!r} is a write and {why}. Pass {flag} to allow it.")


class DangerousCommandRefused(WriteNotAllowed):
    """A destructive command was issued without its explicit opt-in.

    Kept as a distinct type (and a distinct name, unchanged from the first
    release of this module) because the opt-in it names is a different, louder
    flag than the one ordinary setters need.
    """
