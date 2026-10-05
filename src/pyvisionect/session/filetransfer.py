"""Reading a file off the device, as a sans-io state machine.

The file protocol has no "read file" call.  It has ``open``, ``read``,
``close``, and a reply channel, and the device's answers come back as
asynchronous ``packet.File`` events rather than as returns.  So a caller who
wants a file ends up writing a small sequencer; this is that sequencer, written
once, pure, and testable without a socket.

    seq = FileRead("/image0.pv2", length=134409)
    seq.start(conn)                      # queues the open
    for event in conn.feed(data):        # as events arrive
        seq.on_event(event, conn)        # queues the next read, or the close
    if seq.done:
        raw = seq.data

What the device actually does
-----------------------------

Measured against firmware 7.4.4407:

* **Replies arrive as ``Op 6`` (``event``), not ``Op 2`` (``read``).**  ``read``
  is the request direction only.
* **One reply carries at most 1024 bytes**
  (:data:`~pyvisionect.packets.file.DEVICE_FILE_READ_CHUNK`), whatever you ask
  for.  Asking for 32768 gets you 1024 and nothing else.
* **There is no seek.**  ``open`` rewinds to zero and each reply advances the
  cursor; nothing else moves it.  So a reply that never comes cannot be
  resumed past -- :attr:`FileRead.restart` exists because the only recovery is
  to start the file again from offset 0.
* Throughput is **~2.3 KiB/s** (~420 ms per round trip).  A 1.2 MB file is
  nine minutes of chat.  Do not put one behind a service call without telling
  the user.

Timeouts are the caller's
-------------------------

Nothing here reads a clock.  :attr:`FileRead.awaiting_reply` tells an io layer
when it is waiting on the device, and that is where a deadline belongs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..devices.enums import FileMode
from ..packets.file import DEVICE_FILE_READ_CHUNK
from . import events as ev

__all__ = ["FileRead", "FileReadState"]


class FileReadState:
    """Where a :class:`FileRead` has got to."""

    IDLE = "idle"
    OPENING = "opening"
    READING = "reading"
    CLOSING = "closing"
    DONE = "done"
    FAILED = "failed"


@dataclass
class FileRead:
    """Sequence ``open -> read* -> close`` for one file on one device.

    Args:
        filename: the device path, e.g. ``"/image0.pv2"``. A listing gives
            these with their leading slash; pass them through unchanged.
        length: how many bytes to read. Take it from the directory listing's
            size column.
        chunk: bytes per request. Values above
            :data:`~pyvisionect.packets.file.DEVICE_FILE_READ_CHUNK` are
            clamped, because the device would truncate them anyway and a
            caller that believes its own number ends up mis-counting progress.
    """

    filename: str
    length: int
    chunk: int = DEVICE_FILE_READ_CHUNK
    stop_on_short: bool = False
    """Treat a reply shorter than the request as end-of-file.

    Off for a real file, where *length* comes from the directory listing and a
    short reply means something went wrong.  On for a **directory listing**,
    which is "read up to 10240 bytes of ``'.'``" and answers with however many
    bytes the listing happens to be -- 129 on this firmware.  Without this the
    sequencer would keep asking for the remaining 10111 bytes of a file that
    does not have them.
    """

    state: str = FileReadState.IDLE
    error: str | None = None
    buffer: bytearray = field(default_factory=bytearray, repr=False)
    reads: int = 0
    """How many reply chunks have been accepted."""
    restarts: int = 0
    """How many times :meth:`restart` has thrown the buffer away."""

    _last_request: int = field(default=0, repr=False)
    _open_id: int | None = field(default=None, repr=False)
    _close_id: int | None = field(default=None, repr=False)
    _read_id: int | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.length < 0:
            raise ValueError(f"length must not be negative, got {self.length}")
        self.chunk = max(1, min(int(self.chunk), DEVICE_FILE_READ_CHUNK))

    # ------------------------------------------------------------- status

    @property
    def done(self) -> bool:
        """Whether the whole file is in :attr:`data`."""
        return self.state == FileReadState.DONE

    @property
    def failed(self) -> bool:
        return self.state == FileReadState.FAILED

    @property
    def finished(self) -> bool:
        """Done or failed -- i.e. nothing more will happen without a restart."""
        return self.state in (FileReadState.DONE, FileReadState.FAILED)

    @property
    def awaiting_reply(self) -> bool:
        """Whether the device owes us something. Where a timeout belongs."""
        return self.state in (
            FileReadState.OPENING,
            FileReadState.READING,
            FileReadState.CLOSING,
        )

    @property
    def data(self) -> bytes:
        return bytes(self.buffer)

    @property
    def bytes_read(self) -> int:
        return len(self.buffer)

    @property
    def progress(self) -> float:
        """0.0 .. 1.0. Returns 1.0 for a zero-length file."""
        if self.length <= 0:
            return 1.0
        return min(1.0, len(self.buffer) / self.length)

    # ------------------------------------------------------------ driving

    def start(self, conn: Any) -> int:
        """Queue the ``open``. Returns its packet id."""
        self.state = FileReadState.OPENING
        self.error = None
        self._open_id = conn.file_open(self.filename, FileMode.READ)
        return self._open_id

    def restart(self, conn: Any) -> int:
        """Throw away what we have and open the file again from offset 0.

        The only recovery the protocol offers: there is no seek, so a lost
        reply means the cursor is in a place we cannot name.
        """
        self.restarts += 1
        self.buffer.clear()
        self.reads = 0
        self._read_id = None
        self._close_id = None
        return self.start(conn)

    def fail(self, reason: str) -> None:
        """Give up with *reason*. Idempotent-ish; the first reason wins."""
        if self.state != FileReadState.FAILED:
            self.state = FileReadState.FAILED
            self.error = reason

    def on_event(self, event: Any, conn: Any) -> bool:
        """Feed one library event. Returns True if it belonged to this read.

        Queues whatever comes next on *conn*; the caller still has to drain
        and write ``conn.drain()``.
        """
        if isinstance(event, ev.Nacked):
            if event.packet_id in (self._open_id, self._read_id, self._close_id):
                which = (
                    "open"
                    if event.packet_id == self._open_id
                    else "read"
                    if event.packet_id == self._read_id
                    else "close"
                )
                code = (
                    f" (error {event.error_code:#010x})"
                    if event.error_code is not None
                    else ""
                )
                self.fail(f"device refused the {which} of {self.filename!r}{code}")
                return True
            return False

        if isinstance(event, ev.Acked):
            if event.packet_id == self._open_id and self.state == FileReadState.OPENING:
                if self.length == 0:
                    self._finish(conn)
                else:
                    self._request_next(conn)
                return True
            if event.packet_id == self._close_id:
                self.state = FileReadState.DONE
                return True
            # An ack for a read request is fine and carries nothing; the bytes
            # come separately as a file event.
            return event.packet_id == self._read_id

        if isinstance(event, ev.FileEventReceived):
            if self.state != FileReadState.READING:
                return False
            payload = event.file.raw if event.file is not None else b""
            if not payload:
                if self.stop_on_short:
                    self._finish(conn)
                else:
                    self.fail(
                        f"empty reply at offset {len(self.buffer)} of "
                        f"{self.filename!r}"
                    )
                return True
            short = len(payload) < self._last_request
            self.buffer += payload
            self.reads += 1
            if len(self.buffer) >= self.length or (self.stop_on_short and short):
                del self.buffer[self.length :]
                self._finish(conn)
            else:
                self._request_next(conn)
            return True

        return False

    # ------------------------------------------------------------ internals

    def _request_next(self, conn: Any) -> None:
        self.state = FileReadState.READING
        self._last_request = min(self.chunk, self.length - len(self.buffer))
        self._read_id = conn.file_read(self._last_request)

    def _finish(self, conn: Any) -> None:
        self.state = FileReadState.CLOSING
        self._close_id = conn.file_close()
