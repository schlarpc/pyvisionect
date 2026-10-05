"""File packets (type 10) -- the device's on-flash filesystem.

Layout::

    FileHeader (8 bytes)
       0   4  Op     uint32 LE   (packet.FileOperation)
       4   4  Length uint32 LE   (= len of the operation payload)
       8 ...  payload, shape selected by Op through packet.filePayloadFunc

Payload shapes:

* ``open``   (0): ``Mode uint32`` + NUL-terminated filename
* ``read``   (2) / ``write`` (3): raw bytes
* ``erase``  (4): a filename
* ``write descriptor`` (5): ``Length uint32`` + ``Checksum uint32``
* ``close``  (1): no payload
* ``event``  (6): **this is how every reply from the device arrives.**

The device answers a read with ``event``, not ``read``
------------------------------------------------------

Measured against firmware 7.4.4407 on 2026-10-05: every reply the device sends
-- the directory listing, and every chunk of a file read -- comes back as a
``packet.File`` whose ``Op`` is ``6`` (``event``), carrying the bytes verbatim
as its payload.  ``Op 2`` (``read``) is the **request** direction only.

That matters because an earlier reading of this module had ``event`` as
"no payload in the observed traffic" and decoded nothing for it, so
:meth:`FilePacket.parsed` returned ``None`` for every reply the device ever
sends and a consumer that checked ``isinstance(parsed, FileBytes)`` silently
saw an empty filesystem.  :data:`_DECODERS` now maps ``event`` to
:class:`FileBytes`.

Reads are capped at 1024 bytes and there is no seek
----------------------------------------------------

The device honours a read request of up to :data:`DEVICE_FILE_READ_CHUNK`
bytes and silently truncates anything larger -- asking for 32768 returns 1024.
There is no seek opcode, so the only cursor control is ``open`` (which rewinds
to zero) and the implicit advance of each read.  A reply that never arrives
therefore costs the whole transfer: you cannot resume, only restart.

Measured throughput is **~2.3 KiB/s** (about 420 ms per 1 KiB round trip), so
one of this sign's 134 KB-1.2 MB stored frames takes 1-9 minutes.  Budget for
that before exposing a file read to a user.

There is no ``list`` opcode
---------------------------

The vendor's directory listing is a **read of ``"."``**: a path of ``"/"`` or
``""`` is rewritten to the one-byte string ``"."``, and ``Read(".", 10240)``
returns an ASCII blob that ``vio.GetFiles`` splits on newline, then on space,
into ``{name: {size, checksum}}``.  :func:`parse_file_listing` does that.
The columns are ``name checksum size``, and **the checksum column reads ``"0"``
for every file on this firmware**, so it identifies nothing.  The order was
confirmed against the files themselves: ``/image0.pv2 0 134409`` and that
file's own ``ProtocolHeader.Length`` is ``134389``, which is ``134409`` minus
the 20-byte header -- so the third column is the byte size and the middle one
is the useless checksum.  Reading them the other way round (which this module
used to do) reports every file as zero bytes long.

``@screen_N`` has nothing to do with image updates
--------------------------------------------------

An earlier reading of this had the gateway opening ``@screen_<N>`` "before
deciding how to update the screen", making the NACK "the mechanism behind
incremental updates".  **That was wrong**, and it invented a constraint that does
not exist: the file protocol is not entangled with image pushes at all.

A scan of the whole executable segment of ``gateway`` for references to
``"@screen_2"`` finds **exactly one**, inside
``main.(*grpcHandlers).handleLiveView`` (``grpc_handlers.go:240``), and the
literal is a hardcoded ``"@screen_2"`` rather than a ``@screen_%d`` format -- so
it is not even per-screen.  The NACKs in the capture are replies to the
``ac-device`` **live-view** request; the admin UI only offers that button for
``HardwareNameID`` in {11, 13, 24, 34, 42} and this sign is 8, so the firmware
NACKs a filename it does not implement.  It says nothing about update strategy.

Separately, and for a different reason: for ``HardwareNameID == 8`` the server's
``getRectangleSupport`` returns false unconditionally, so every push to this
sign really is full-screen.  Do not build session logic that depends on partial
updates landing.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from ..devices.enums import FileMode, FileOperation, PacketType
from ..wire.errors import PayloadError

__all__ = [
    "DEVICE_FILE_READ_CHUNK",
    "DEVICE_IMAGE_FILES",
    "FILE_LIST_PATH",
    "FILE_LIST_LENGTH",
    "FileListEntry",
    "FilePacket",
    "parse_file_listing",
    "FileOpen",
    "FileBytes",
    "FileErase",
    "FileWriteDone",
    "screen_cache_name",
]

_HDR = struct.Struct("<II")

FILE_LIST_PATH = "."
"""The path a listing reads. ``"/"`` and ``""`` are rewritten to this."""

FILE_LIST_LENGTH = 10240
"""The read length the vendor uses for a listing.

Larger than :data:`DEVICE_FILE_READ_CHUNK` on purpose and harmless: the
listing of this firmware's six files is 129 bytes, so it fits in one reply
whatever you ask for.
"""

DEVICE_FILE_READ_CHUNK = 1024
"""The most bytes one ``read`` reply ever carries.

Measured: asking for 256 returns 256, asking for 1024 returns 1024, and asking
for 4824, 10240 or 32768 all return 1024.  Ask for more and you merely waste
the round trip accounting.
"""

DEVICE_IMAGE_FILES: tuple[str, ...] = tuple(f"/image{i}.pv2" for i in range(6))
"""The six ``.pv2`` files firmware 7.4.4407 holds on its flash.

**They are not framebuffers.**  Each is a complete, stored type-5 image packet
for the whole 1440x2560 canvas, and they are Visionect's shipped demo content
-- a wayfinding board ("Welcome to Nanotech inc."), a museum label ("Room 35 --
Michelangelo and the Florentines") and four more.  Verified by pulling two of
them whole and rendering them, and by pushing a distinctive frame and
re-listing: **not one of the six changed size, and ``/image0.pv2``'s first
bytes were identical before and after.**  They also carry
``DataHeader.DeviceID == 00000000...`` and ``ImageHeader.Checksum == 0``, which
no frame we send ever does.

So there is no device-side readback of what the panel is currently showing on
this firmware, and the ``ImageHeader.Checksum``-matching trick that
``OPEN-QUESTIONS.md`` B7 proposed cannot work -- the field is zero in all six.
``DisplayStateCRC`` (status tag 9) remains the only way to ask the device what
it is displaying, and it is enough.
"""


@dataclass(frozen=True, slots=True)
class FileListEntry:
    """One row of a directory listing."""

    name: str
    size: int
    """Bytes on flash, including the 20-byte ``ProtocolHeader``."""
    checksum: str
    """Verbatim text from the middle column. Reads ``"0"`` for every file on
    this firmware, so it identifies nothing -- do not build on it."""


def parse_file_listing(raw: bytes | str) -> dict[str, FileListEntry]:
    """Parse the ASCII blob a ``Read(".", 10240)`` returns.

    The vendor's ``vio.GetFiles`` splits on newline, then on space, into
    ``{name: {size, checksum}}``. Rows that do not have at least a name and a
    size are skipped rather than raising, because this is a device-formatted
    blob and a strict parser would be the wrong trade.
    """
    text = raw.decode("ascii", errors="replace") if isinstance(raw, bytes) else raw
    out: dict[str, FileListEntry] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        name = parts[0]
        # "name checksum size". A two-column row is not something this firmware
        # emits; read its one number as the size rather than dropping the row.
        checksum, raw_size = ("", parts[1]) if len(parts) == 2 else (parts[1], parts[2])
        try:
            size = int(raw_size)
        except ValueError:
            continue
        out[name] = FileListEntry(name=name, size=size, checksum=checksum)
    return out


def screen_cache_name(screen_id: int) -> str:
    """The device-side cached-frame filename, ``@screen_<N>``.

    Captured verbatim as ``"@screen_2\\0"`` in a ``file/open`` with mode ``read``.
    """
    return f"@screen_{screen_id}"


@dataclass(frozen=True, slots=True)
class FileOpen:
    """``FilePayloadOpen{Mode uint32; Filename string}``."""

    filename: str
    mode: int = FileMode.READ

    def encode(self) -> bytes:
        return struct.pack("<I", self.mode) + self.filename.encode("ascii") + b"\x00"

    @classmethod
    def decode(cls, raw: bytes) -> "FileOpen":
        if len(raw) < 4:
            raise PayloadError("file/open payload needs at least a 4-byte Mode")
        (mode,) = struct.unpack_from("<I", raw, 0)
        name = raw[4:].split(b"\x00")[0].decode("ascii", errors="replace")
        return cls(filename=name, mode=mode)


@dataclass(frozen=True, slots=True)
class FileBytes:
    """``FilePayloadBytes`` -- raw bytes for ``read``/``write``."""

    data: bytes

    def encode(self) -> bytes:
        return self.data

    @classmethod
    def decode(cls, raw: bytes) -> "FileBytes":
        return cls(data=raw)


@dataclass(frozen=True, slots=True)
class FileErase:
    """``FilePayloadErase`` -- a filename."""

    filename: str

    def encode(self) -> bytes:
        return self.filename.encode("ascii") + b"\x00"

    @classmethod
    def decode(cls, raw: bytes) -> "FileErase":
        return cls(filename=raw.split(b"\x00")[0].decode("ascii", errors="replace"))


@dataclass(frozen=True, slots=True)
class FileWriteDone:
    """``FilePayloadWriteDone{Length uint32; Checksum uint32}``."""

    length: int
    checksum: int

    def encode(self) -> bytes:
        return struct.pack("<II", self.length, self.checksum)

    @classmethod
    def decode(cls, raw: bytes) -> "FileWriteDone":
        if len(raw) < 8:
            raise PayloadError("file/write-descriptor payload needs 8 bytes")
        return cls(*struct.unpack_from("<II", raw, 0))


_DECODERS = {
    FileOperation.OPEN: FileOpen.decode,
    FileOperation.READ: FileBytes.decode,
    FileOperation.WRITE: FileBytes.decode,
    FileOperation.ERASE: FileErase.decode,
    FileOperation.WRITE_DESCRIPTOR: FileWriteDone.decode,
    # Every reply the device sends is op 6 with the bytes in the payload.
    FileOperation.EVENT: FileBytes.decode,
}


@dataclass(slots=True)
class FilePacket:
    """A ``packet.File`` payload."""

    op: int
    raw: bytes = b""
    """The operation payload bytes, kept verbatim so re-encoding is exact."""

    TYPE = PacketType.FILE

    @property
    def op_name(self) -> str:
        return FileOperation.NAMES.get(self.op, "(unknown)")

    def parsed(self) -> FileOpen | FileBytes | FileErase | FileWriteDone | None:
        """Decode :attr:`raw` into the shape *op* selects, or None if unknown."""
        decoder = _DECODERS.get(self.op)
        return decoder(self.raw) if decoder else None

    @classmethod
    def open(cls, filename: str, mode: int = FileMode.READ) -> "FilePacket":
        return cls(op=FileOperation.OPEN, raw=FileOpen(filename, mode).encode())

    @classmethod
    def close(cls) -> "FilePacket":
        return cls(op=FileOperation.CLOSE, raw=b"")

    @classmethod
    def read(cls, length: int) -> "FilePacket":
        return cls(op=FileOperation.READ, raw=struct.pack("<I", length))

    @classmethod
    def write(cls, data: bytes) -> "FilePacket":
        return cls(op=FileOperation.WRITE, raw=data)

    @classmethod
    def erase(cls, filename: str) -> "FilePacket":
        return cls(op=FileOperation.ERASE, raw=FileErase(filename).encode())

    @classmethod
    def write_descriptor(cls, length: int, checksum: int) -> "FilePacket":
        return cls(
            op=FileOperation.WRITE_DESCRIPTOR,
            raw=FileWriteDone(length, checksum).encode(),
        )

    @classmethod
    def decode(cls, payload: bytes) -> "FilePacket":
        if len(payload) < 8:
            raise PayloadError(f"file payload is {len(payload)} bytes, need >= 8")
        op, length = _HDR.unpack_from(payload, 0)
        return cls(op=op, raw=payload[8 : 8 + length])

    def encode(self) -> bytes:
        return _HDR.pack(self.op, len(self.raw)) + self.raw
