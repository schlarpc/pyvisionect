r"""The serial transport: one command in, one structured reply out.

Line discipline, measured on a live sign
----------------------------------------

Firmware 7.4.4407, FTDI FT232 (``0403:6001``) on ``/dev/ttyUSB0``, 115200 8N1.
Everything in this section was observed, not inferred:

* **CR (``\r``) is the submit character. LF is not.**  Sending
  ``cli_version_get\n`` makes the device *echo* the text and then sit there with
  the line still in its edit buffer, so the next command is appended to it and
  the pair is rejected as one unrecognised word::

      >>> write(b"cli_version_get\n")          # echoes, does not execute
      >>> write(b"definitely_not_a_command\r")
      Command 'cli_version_getdefinitely_not_a_command' not recognised.

  ``\r\n`` does work -- the CR submits -- but the trailing LF is echoed back
  *after* the prompt and pollutes the next read.  :data:`TERMINATOR` is
  therefore a bare ``\r``, which is a correction to this module's first release.

* **The device echoes the command line**, terminated ``\r\n``.  That echo is the
  anchor this module synchronises on.

* **The prompt is ``"> "``** with no trailing newline, emitted after every reply.
  So a reply has a real terminator and there is no need to read until the port
  goes quiet.

* **Commands are case-insensitive.**  ``UPTIME`` works.

* **Only some commands emit a return code**, and it is not always last:
  ``certs_config_get`` prints ``rv: 0`` *before* its body.  It appears in two
  spellings, decimal (``rv: 0``) and hex (``rv: 0x0``), from different commands
  in the same firmware.

* Errors are followed by a blank line, then the prompt.

Asynchronous log lines -- the actual problem
--------------------------------------------

The firmware logs to this same UART, unprompted.  A heartbeat fires every minute
and dumps eight lines; an image push dumps a ``Profiling:`` line.  Captured
verbatim between two commands::

    sys evt vplatform_heartbeat.c:19, Heartbeat (7)
    Received event: Heartbeat (7)
    Heart-beat event
    sending pv2 status on heartbeat
    make packet type=3, id=4
    Frame send 552 bytes
    Setting heartbeat after 1 min
    Got response (id=4)

Any of those can land in the middle of a reply, so a parser that assumes "every
line between my command and the prompt is my answer" corrupts intermittently --
which is the worst failure mode, because it passes every test you write by hand.

:class:`SerialConsole` separates the two streams with four mechanisms, in
descending order of how much they are relied on:

1. **Drain before send.**  Anything already in the buffer when
   :meth:`command` is called arrived unsolicited by definition, so it is read
   out and classified as log *before* the command is written.  This alone
   handles the common case, because log bursts are short relative to the gap
   between commands.
2. **Echo anchor.**  The reply frame starts at the echoed command line.
   Anything between the write and that echo is log.
3. **Prompt terminator.**  The frame ends at a ``"> "`` at the start of a line.
   Between those two anchors the extent of the reply is exact; only its
   *contents* can still be contaminated.
4. **Pattern classification.**  Lines inside the frame that match
   :data:`LOG_PATTERNS` are moved to the log stream.  This is the only
   heuristic in the chain, and it is the only one that can be wrong, so it is
   a plain editable tuple of regexes rather than something clever.

**The honest fix is to turn the log sink off**, which the firmware supports:
``vlog_unify_levels 0`` silences the USB destination, and
``vlog_set_default_levels`` puts it back.  See
:meth:`SerialConsole.quiet_logs`.  Those are writes, so they are gated like any
other setter.

One assumption remains: that the firmware emits whole log lines atomically, so
an interloper never splits a reply line down the middle.  It held across every
capture taken for this module, and a FreeRTOS logger taking a mutex per message
is the usual reason, but it is an assumption and not a measurement.
"""

from __future__ import annotations

import logging
import re
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from .errors import (
    ConsoleAssertionFailed,
    ConsoleNotOpen,
    ConsoleTimeout,
    IncorrectParameters,
    PromptNotFound,
    UnknownCommand,
)

__all__ = [
    "BAUD_RATE",
    "LOG_PATTERNS",
    "PROMPT",
    "TERMINATOR",
    "CommandResult",
    "LogLine",
    "SerialConsole",
    "SerialTransport",
    "classify",
]

log = logging.getLogger(__name__)

BAUD_RATE = 115200
"""115200 8N1 -- from the vendor reference, confirmed on the wire."""

TERMINATOR = b"\r"
r"""Bare CR. See the module docstring: LF alone does not submit a line."""

PROMPT = b"> "
"""The device prompt. No trailing newline, which is why it is matched on bytes."""

_UNKNOWN_RE = re.compile(
    r"^Command '(?P<cmd>.*)' not recognised\.\s+Enter 'help' to view a list of "
    r"available commands\.$"
)
_BAD_PARAMS = (
    'Incorrect command parameter(s).  Enter "help" to view a list of available commands.'
)
_ASSERT_RE = re.compile(r"^assert: (?P<where>\S+:\d+)$")
_RV_RE = re.compile(r"^rv:\s*(?P<value>-?(?:0[xX][0-9a-fA-F]+|\d+))$")

LOG_PATTERNS: tuple[re.Pattern[str], ...] = (
    # vlog severity prefix: "W WD task timeout: cli_USB", "E ...", "I ...".
    re.compile(r"^[DIWEF] \S"),
    # "sys evt vplatform_heartbeat.c:19, Heartbeat (7)" -- source file and line.
    re.compile(r"^sys evt \S+\.c:\d+,"),
    re.compile(r"^Received event: "),
    re.compile(r"^Heart-beat event$"),
    re.compile(r"^sending pv2 status"),
    re.compile(r"^make packet type=\d+"),
    re.compile(r"^Frame send \d+ bytes$"),
    re.compile(r"^Frame recv \d+ bytes$"),
    re.compile(r"^Setting heartbeat after "),
    re.compile(r"^Got response \(id=\d+\)$"),
    re.compile(r"^Profiling:"),
    re.compile(r"^WD task timeout:"),
)
"""Regexes that identify an *unsolicited* line.

Every entry was taken from a real capture off ``/dev/ttyUSB0``; none is
speculative.  The list is deliberately a module-level tuple so a caller whose
firmware logs something new can extend it::

    console = SerialConsole("/dev/ttyUSB0", log_patterns=LOG_PATTERNS + (
        re.compile(r"^my new log line"),
    ))

A false positive here *silently eats a reply line*, so prefer anchored,
specific patterns over loose ones.
"""


def classify(line: str, patterns: Sequence[re.Pattern[str]] = LOG_PATTERNS) -> bool:
    """True if *line* looks like an asynchronous log line rather than a reply.

    Pure, so it is the one piece of the interleaving logic that can be tested
    without any transport at all.
    """
    return any(p.search(line) for p in patterns)


@runtime_checkable
class SerialTransport(Protocol):
    """The slice of ``serial.Serial`` this module uses.

    Deliberately four members.  Anything satisfying it can be injected, which is
    how the whole module is tested without hardware; see
    :class:`pyvisionect.io.usb.fake.FakeTransport`.
    """

    @property
    def in_waiting(self) -> int:
        """Bytes buffered and readable without blocking."""

    def write(self, data: bytes, /) -> int | None: ...

    def read(self, size: int = 1, /) -> bytes: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class LogLine:
    """One asynchronous line, with the moment it was read.

    Attributes:
        text: the line, without its terminator.
        monotonic: ``time.monotonic()`` at the read that completed it. Monotonic
            rather than wall-clock because the only useful question is
            "how long before/after that command?".
        during: the command whose reply window this arrived in, or None if it
            was drained between commands.
    """

    text: str
    monotonic: float
    during: str | None = None


@dataclass(frozen=True, slots=True)
class CommandResult:
    """The structured outcome of one command.

    Attributes:
        command: exactly what was written, minus the terminator.
        raw: the whole decoded frame, echo and prompt included. Keep this: it is
            the only lossless record, and it is what belongs in a bug report.
        lines: the reply's own lines -- echo removed, prompt removed, ``rv:``
            removed, blank lines removed, asynchronous log lines removed.
        rv: the integer from the ``rv:`` line, or None when the command does not
            emit one (most getters do not). Parsed from either spelling, so
            ``rv: 0`` and ``rv: 0x0`` both give ``0``.
        logs: the asynchronous lines that landed inside this command's window.
            Non-empty is normal, not an error.
        echoed: whether the device's echo of the command line was found. False
            means the stream was out of sync, so treat ``lines`` with suspicion
            and call :meth:`SerialConsole.sync`.
        terminated_by: ``"prompt"`` normally. ``"idle"`` or ``"timeout"`` only
            when ``expect_prompt=False`` was passed, since otherwise the
            absence of a prompt raises.
    """

    command: str
    raw: str
    lines: tuple[str, ...]
    rv: int | None = None
    logs: tuple[LogLine, ...] = ()
    echoed: bool = True
    terminated_by: str = "prompt"

    @property
    def text(self) -> str:
        """:attr:`lines` joined with newlines -- for printing, not for parsing."""
        return "\n".join(self.lines)

    @property
    def ok(self) -> bool:
        """True when no ``rv:`` was emitted, or the one emitted was zero."""
        return self.rv in (None, 0)

    def one(self) -> str:
        """The single reply line, for commands that emit exactly one.

        Raises:
            ValueError: if there was not exactly one line. Better than
                ``lines[0]`` silently reading the first of several, which is how
                a changed firmware reply turns into a wrong value instead of an
                error.
        """
        if len(self.lines) != 1:
            raise ValueError(
                f"{self.command!r} returned {len(self.lines)} reply lines, expected 1: "
                f"{self.lines!r}"
            )
        return self.lines[0]

    def field(self, label: str) -> str:
        """The value of the ``"<label>: <value>"`` line with this *label*.

        The device's reply format is overwhelmingly ``Label: value``, with
        padding after the colon on some lines (``"Feature EAP:   enabled"``), so
        the value is stripped.

        Raises:
            KeyError: if no line carries that label.
        """
        for line in self.lines:
            name, sep, value = line.partition(":")
            if sep and name.strip() == label:
                return value.strip()
        raise KeyError(f"{label!r} not in reply to {self.command!r}: {self.lines!r}")

    def fields(self) -> dict[str, str]:
        """Every ``"<label>: <value>"`` line as a dict.

        Later duplicates win. Lines without a colon are skipped, so a reply that
        mixes prose and fields (``certs_config_get`` emits ``No EAP cert
        found!``) still yields its fields.
        """
        out: dict[str, str] = {}
        for line in self.lines:
            name, sep, value = line.partition(":")
            if sep:
                out[name.strip()] = value.strip()
        return out


class SerialConsole:
    """A line-oriented client for the sign's ASCII serial console.

    Either pass *port* and let this open a ``serial.Serial``, or pass
    *transport* (anything matching :class:`SerialTransport`) and keep I/O out of
    it entirely.

    Args:
        port: e.g. ``/dev/ttyUSB0``, or ``/dev/cu.usbserial-XXXXXXXX`` on macOS.
            Ignored when *transport* is given.
        transport: an already-open transport, for tests or for a port someone
            else owns.
        baudrate: :data:`BAUD_RATE` unless you have a reason.
        terminator: :data:`TERMINATOR`. Changing it to ``b"\\n"`` will not work;
            the firmware does not submit on LF.
        read_timeout: the underlying port's per-``read`` timeout. Small, because
            this class does its own waiting.
        command_timeout: how long :meth:`command` waits for the prompt. ``help``
            emits ~5 KB and still lands well inside the default.
        idle_timeout: how long a silent port counts as "the reply is over", used
            only when ``expect_prompt=False``.
        on_log: called with each :class:`LogLine` as it is classified. Exceptions
            from it are logged and swallowed -- a noisy callback must not break
            the command that happened to be in flight.
        log_history: how many :class:`LogLine` to keep in :attr:`logs`.
        log_patterns: see :data:`LOG_PATTERNS`.
        encoding_errors: how to handle non-ASCII bytes. ``"replace"`` by
            default: the port emits the occasional NUL at open and a decode
            error there would be a useless failure.

    Example:
        >>> with SerialConsole("/dev/ttyUSB0") as c:        # doctest: +SKIP
        ...     c.sync()
        ...     print(c.command("uptime").field("Uptime in min"))
        ...     for entry in c.logs:
        ...         print("async:", entry.text)
    """

    def __init__(
        self,
        port: str | None = None,
        *,
        transport: SerialTransport | None = None,
        baudrate: int = BAUD_RATE,
        terminator: bytes = TERMINATOR,
        read_timeout: float = 0.05,
        command_timeout: float = 25.0,
        idle_timeout: float = 1.2,
        on_log: Callable[[LogLine], None] | None = None,
        log_history: int = 512,
        log_patterns: Sequence[re.Pattern[str]] = LOG_PATTERNS,
        encoding_errors: str = "replace",
    ) -> None:
        if transport is None and port is None:
            raise ValueError("either port or transport must be given")
        self.port = port
        self.baudrate = baudrate
        self.terminator = terminator
        self.read_timeout = read_timeout
        self.command_timeout = command_timeout
        self.idle_timeout = idle_timeout
        self.on_log = on_log
        self.log_patterns = tuple(log_patterns)
        self.encoding_errors = encoding_errors
        self.logs: deque[LogLine] = deque(maxlen=log_history)
        self.transcript: list[CommandResult] = []
        self._holdover = ""
        self._transport: SerialTransport | None = transport
        self._owned = transport is None
        if transport is None:
            self.open()

    # --------------------------------------------------------------- lifecycle

    def open(self) -> None:
        """Open the port. A no-op if it is already open."""
        if self._transport is not None:
            return
        assert self.port is not None
        try:
            import serial as pyserial
        except ImportError as exc:  # pragma: no cover - depends on the install
            raise ImportError(
                "the USB console needs pyserial: pip install 'pyvisionect[usb]'"
            ) from exc
        self._transport = pyserial.Serial(
            self.port,
            self.baudrate,
            bytesize=8,
            parity="N",
            stopbits=1,
            timeout=self.read_timeout,
        )
        self._owned = True

    def close(self) -> None:
        """Close the port, if this object opened it."""
        transport, self._transport = self._transport, None
        if transport is not None and self._owned:
            transport.close()

    @property
    def is_open(self) -> bool:
        return self._transport is not None

    def __enter__(self) -> SerialConsole:
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _require(self) -> SerialTransport:
        if self._transport is None:
            raise ConsoleNotOpen("the console is closed; call open() first")
        return self._transport

    # ------------------------------------------------------------------- reads

    def _emit_log(self, text: str, during: str | None) -> LogLine:
        entry = LogLine(text=text, monotonic=time.monotonic(), during=during)
        self.logs.append(entry)
        log.debug("async <- %s", text)
        if self.on_log is not None:
            try:
                self.on_log(entry)
            except Exception:  # pragma: no cover - a callback's problem, not ours
                log.exception("on_log callback raised for %r", text)
        return entry

    def drain_logs(self, *, during: str | None = None) -> tuple[LogLine, ...]:
        """Read everything already buffered and record it as asynchronous.

        Correct by construction: bytes sitting in the buffer before a command is
        written cannot be that command's reply.  Called automatically at the top
        of :meth:`command`.

        A partial trailing line (one with no terminator yet) is kept in an
        internal holdover and prepended to the next read, so a log line split
        across two drains is not reported as two lines.
        """
        transport = self._require()
        waiting = transport.in_waiting
        data = transport.read(waiting) if waiting else b""
        if not data:
            return ()
        return tuple(self._lines_as_logs(data, during))

    def _lines_as_logs(self, data: bytes, during: str | None) -> Iterator[LogLine]:
        text = self._holdover + data.decode("ascii", self.encoding_errors)
        parts = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        self._holdover = parts.pop()  # incomplete tail, or "" if data ended cleanly
        # A bare prompt at the end of the holdover is not a log line.
        if self._holdover.strip() in ("", ">"):
            self._holdover = ""
        for part in parts:
            if part.strip() and part.strip() != ">":
                yield self._emit_log(part.rstrip(), during)

    def _read_frame(
        self, command: str, timeout: float, expect_prompt: bool
    ) -> tuple[bytes, str]:
        """Read until the prompt, or until idle/timeout. Returns (bytes, how)."""
        transport = self._require()
        buf = bytearray()
        start = last = time.monotonic()
        while time.monotonic() - start < timeout:
            waiting = transport.in_waiting
            chunk = transport.read(waiting) if waiting else transport.read(1)
            if chunk:
                buf += chunk
                last = time.monotonic()
                if self._at_prompt(buf):
                    return bytes(buf), "prompt"
                # A dead CLI task never sends a prompt. Fail fast instead of
                # burning the whole timeout: the assertion text is already here.
                if not expect_prompt and _ASSERT_RE.search(
                    buf.decode("ascii", "replace").strip().rsplit("\n", 1)[-1].strip()
                ):
                    return bytes(buf), "assert"
            elif time.monotonic() - last >= self.idle_timeout:
                return bytes(buf), "idle"
        return bytes(buf), "timeout"

    @staticmethod
    def _at_prompt(buf: bytes | bytearray) -> bool:
        """True when *buf* ends with a prompt that starts a line.

        The ``len == 2`` arm matters: after a bare CR the device replies
        ``b"\\r\\n> "``, but a freshly-synced console can also see just
        ``b"> "``.
        """
        if not buf.endswith(PROMPT):
            return False
        return len(buf) == len(PROMPT) or buf[-3:-2] in (b"\n", b"\r")

    # ---------------------------------------------------------------- commands

    def sync(self, *, attempts: int = 3) -> None:
        """Get the stream to a known boundary: at a fresh prompt, buffer empty.

        Sends a bare CR and reads to the prompt, discarding whatever comes back
        as log.  Cheap, and worth doing at the start of a session and after any
        result whose :attr:`~CommandResult.echoed` is False.

        Raises:
            PromptNotFound: after *attempts* tries with no prompt. On a sign
                whose ``usb_cli_task`` has asserted, this is what you get, and
                only a reboot fixes it.
        """
        transport = self._require()
        for _ in range(attempts):
            self.drain_logs()
            transport.write(self.terminator)
            raw, how = self._read_frame("<sync>", min(self.command_timeout, 5.0), True)
            if how == "prompt":
                self._holdover = ""
                tail = raw[: -len(PROMPT)]
                if tail:
                    tuple(self._lines_as_logs(tail, None))
                return
        raise PromptNotFound(
            "no prompt on the serial port after "
            f"{attempts} attempts at {self.baudrate} baud. Either nothing is "
            "listening, the baud rate is wrong, or the firmware's usb_cli_task "
            "has died on an assertion (sf_rdid and sf_rdst do that on 7.4.4407) "
            "-- in which case only a device reboot brings the console back."
        )

    def command(
        self,
        line: str,
        *,
        timeout: float | None = None,
        expect_prompt: bool = True,
        raise_on_error: bool = True,
    ) -> CommandResult:
        """Send one command line and return its parsed reply.

        Args:
            line: the command and its arguments, no terminator. Must be one
                line: the firmware has no statement separator, so ``"a; b"`` is
                rejected as a single unrecognised command, and an embedded
                newline would desynchronise the stream. Both are refused here.
            timeout: override :attr:`command_timeout`.
            expect_prompt: when False, a reply that ends in silence instead of a
                prompt is returned with ``terminated_by="idle"`` rather than
                raising. Needed for ``reboot``, which never prompts again.
            raise_on_error: when False, the firmware's own error replies are
                returned as :attr:`~CommandResult.lines` instead of raising.
                Useful for probing whether a command exists.

        Returns:
            A :class:`CommandResult`. Asynchronous log lines seen during the
            exchange are in :attr:`~CommandResult.logs`, never in
            :attr:`~CommandResult.lines`.

        Raises:
            UnknownCommand: the firmware has no such command.
            IncorrectParameters: wrong arity or an unparseable argument.
            ConsoleAssertionFailed: the command tripped a firmware assertion and
                took the CLI task down with it.
            ConsoleTimeout: no prompt arrived in time.
        """
        if "\n" in line or "\r" in line:
            raise ValueError(
                f"a command must be a single line; {line!r} contains a line break"
            )
        if ";" in line:
            raise ValueError(
                f"the firmware has no statement separator, so {line!r} would be "
                "sent as one command name and rejected; issue the commands separately"
            )
        transport = self._require()
        timeout = self.command_timeout if timeout is None else timeout

        # 1. whatever is buffered now is unsolicited, by construction.
        before = self.drain_logs(during=None)

        log.debug("-> %s", line)
        transport.write(line.encode("ascii") + self.terminator)
        flush = getattr(transport, "flush", None)
        if flush is not None:
            flush()

        raw, how = self._read_frame(line, timeout, expect_prompt)
        text = raw.decode("ascii", self.encoding_errors)
        result = self._parse_frame(line, text, how, before)
        self.transcript.append(result)

        if how in ("timeout", "assert") or (how == "idle" and expect_prompt):
            assertion = self._find_assertion(text)
            if assertion is not None:
                raise ConsoleAssertionFailed(line, assertion)
            raise ConsoleTimeout(line, text, timeout)

        if raise_on_error:
            self._raise_for_error(line, result)
        return result

    def _parse_frame(
        self, line: str, text: str, how: str, before: Iterable[LogLine]
    ) -> CommandResult:
        """Split one frame into reply lines, a return code and log lines.

        Pure apart from recording log lines, and the reason the whole
        interleaving story is testable: hand it a frame with a heartbeat burst
        spliced into the middle and the burst comes out in ``logs``.
        """
        body = text[: -len(PROMPT.decode())] if how == "prompt" else text
        parts = body.replace("\r\n", "\n").replace("\r", "\n").split("\n")

        # 2. the echo anchors the start of the reply.
        echoed = False
        pre: list[str] = []
        while parts:
            head = parts.pop(0)
            if head.strip().lower() == line.strip().lower():
                echoed = True
                break
            pre.append(head)
        if not echoed:
            # No echo: the stream was out of sync. Keep everything as reply
            # candidates rather than throwing the answer away.
            parts = pre + parts
            pre = []

        during: list[LogLine] = list(before)
        for stray in pre:
            if stray.strip():
                during.append(self._emit_log(stray.rstrip(), line))

        # 3. classify, and pull the return code out wherever it sits.
        lines: list[str] = []
        rv: int | None = None
        for part in parts:
            stripped = part.strip()
            if not stripped or stripped == ">":
                continue
            match = _RV_RE.match(stripped)
            if match is not None:
                rv = int(match.group("value"), 0)
                continue
            if classify(stripped, self.log_patterns):
                during.append(self._emit_log(part.rstrip(), line))
                continue
            lines.append(stripped)

        return CommandResult(
            command=line,
            raw=text,
            lines=tuple(lines),
            rv=rv,
            logs=tuple(during),
            echoed=echoed,
            terminated_by=how,
        )

    @staticmethod
    def _find_assertion(text: str) -> str | None:
        for part in text.replace("\r\n", "\n").split("\n"):
            match = _ASSERT_RE.match(part.strip())
            if match is not None:
                return match.group("where")
        return None

    @staticmethod
    def _raise_for_error(line: str, result: CommandResult) -> None:
        for reply in result.lines:
            match = _UNKNOWN_RE.match(reply)
            if match is not None:
                raise UnknownCommand(match.group("cmd"))
            if reply == _BAD_PARAMS:
                raise IncorrectParameters(line)

    # ------------------------------------------------------------- convenience

    def exists(self, command: str) -> bool:
        """Probe whether the firmware has *command*, by sending it bare.

        Only safe for nullary getters: a setter sent bare may or may not be
        rejected before it writes.  Prefer
        :meth:`pyvisionect.io.usb.device.Sign.has` once ``help`` has been read,
        which costs nothing and risks nothing.
        """
        result = self.command(command, raise_on_error=False)
        return not any(_UNKNOWN_RE.match(reply) for reply in result.lines)

    def help_text(self) -> str:
        """The raw ``help`` output. ~5 KB on firmware 7.4.4407."""
        return self.command("help").text
