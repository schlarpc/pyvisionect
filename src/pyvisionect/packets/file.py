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
* ``close``  (1) / ``event`` (6): no payload in the observed traffic

There is no ``list`` opcode
---------------------------

The vendor's directory listing is a **read of ``"."``**: a path of ``"/"`` or
``""`` is rewritten to the one-byte string ``"."``, and ``Read(".", 10240)``
returns an ASCII blob that ``vio.GetFiles`` splits on newline, then on space,
into ``{name: {size, checksum}}``.  :func:`parse_file_listing` does that.
On this firmware the listing's ``checksum`` column reads ``"0"`` for every file,
so it identifies nothing.

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
"""The read length the vendor uses for a listing."""


@dataclass(frozen=True, slots=True)
class FileListEntry:
    """One row of a directory listing."""

    name: str
    size: int
    checksum: str
    """Verbatim text. Reads ``"0"`` for every file on this firmware, so it
    identifies nothing -- do not build on it."""


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
        try:
            size = int(parts[1])
        except ValueError:
            continue
        out[name] = FileListEntry(
            name=name, size=size, checksum=parts[2] if len(parts) > 2 else ""
        )
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
