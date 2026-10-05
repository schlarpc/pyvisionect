"""Reading a ``.pv2`` file off the device's flash.

A ``.pv2`` is a **stored protocol frame**: the same container the wire uses,
written to the filesystem.  Layout, confirmed byte by byte against
``/image0.pv2`` .. ``/image5.pv2`` on firmware 7.4.4407::

    ProtocolHeader  20 B   Version=2 | Security=0 | Compression=1 | Length | Checksum
    block chain            BlocksMinusOne+1 records of 24 B + payload, 4800 B plaintext each
      +-- DataHeader   36 B   Priority | DeviceID[16] | Type=5 | ID | Length | Reserved
            +-- ImageHeader 20 B + RectangleHeaders   (:mod:`pyvisionect.packets.image`)

So the first 44 bytes -- ``ProtocolHeader`` plus the first block record's
header -- are enough to learn the whole shape of the file without inflating
anything: :class:`StoredFrameHeader`.

Three differences from a frame on the wire, all of them real:

* ``Version`` is **2**, not the 3 that :mod:`pyvisionect.wire.framing`
  requires.  :func:`parse_stored_frame` accepts either rather than refusing a
  file the device plainly wrote itself.
* ``DataHeader.DeviceID`` is 16 zero bytes.
* ``ImageHeader.Checksum`` is **0**.  Nothing we push has a zero checksum (the
  vendor remaps a computed 0 to 1), so this is a reliable tell that a file was
  not written from one of our frames -- and it is why identifying a stored
  frame by its checksum, as ``OPEN-QUESTIONS.md`` B7 proposed, cannot work.

What these files are
--------------------

On this firmware they are Visionect's **shipped demo screens**, not
framebuffers: a wayfinding board, a museum label and four more, each a
full-canvas 4 bpp frame.  Pushing a new image changes none of them -- not their
size, not their first bytes.  See :data:`~pyvisionect.packets.file
.DEVICE_IMAGE_FILES`.

This module is still the right tool for them, and would be the right tool for a
firmware that *does* cache frames: it turns any ``.pv2`` on a device back into
an :class:`~pyvisionect.packets.image.ImagePacket`, and
:func:`pyvisionect.imaging.decode_image_packet` turns that into pixels.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from ..wire.blocks import BLOCK_HEADER_SIZE, BLOCK_PLAINTEXT_SIZE, decode_blocks
from ..wire.errors import PayloadError
from .image import ImagePacket

__all__ = [
    "STORED_FRAME_HEADER_SIZE",
    "StoredFrame",
    "StoredFrameHeader",
    "parse_stored_frame",
    "parse_stored_frame_header",
]

STORED_FRAME_HEADER_SIZE = 20 + BLOCK_HEADER_SIZE
"""44 bytes: ``ProtocolHeader`` plus the first block record's header."""

_PROTO = struct.Struct("<5I")
_BLOCK = struct.Struct("<6I")
_DATA = struct.Struct("<I16sIIII")
_DATA_SIZE = 36

_ACCEPTED_VERSIONS = frozenset({2, 3})


@dataclass(frozen=True, slots=True)
class StoredFrameHeader:
    """What the first :data:`STORED_FRAME_HEADER_SIZE` bytes of a ``.pv2`` say.

    Cheap: five 1024-byte reads get you this plus the whole of block 0, which
    is ~2 s on the link.  A whole file is minutes.
    """

    version: int
    security: int
    compression: int
    length: int
    """``ProtocolHeader.Length`` -- the file's size minus the 20-byte header."""
    checksum: int
    """``ProtocolHeader.Checksum``, the device's own frame CRC. Distinct per
    file, but not a value we can predict for a frame we sent, so it identifies
    a file only against a previous reading of the same file."""
    nblocks: int
    """``BlocksMinusOne + 1`` from the first block record."""
    firstblocklen: int
    """``PayloadLength`` of block 0, i.e. its compressed size."""
    blocksize: int
    """``UncompressedLength`` of block 0 -- 4800 on every file seen."""
    stored: int
    """1 if block 0 is verbatim, 0 if it is a raw LZ4 block."""

    @property
    def total_size(self) -> int:
        """The file's size on flash, as the directory listing reports it."""
        return 20 + self.length

    @property
    def first_block_end(self) -> int:
        """Byte offset just past block 0 -- how much to read to decode it."""
        return STORED_FRAME_HEADER_SIZE + self.firstblocklen

    @property
    def plaintext_size(self) -> int:
        """Upper bound on the inflated size (the last block may be shorter)."""
        return self.nblocks * self.blocksize


def parse_stored_frame_header(raw: bytes) -> StoredFrameHeader:
    """Read the 44-byte prefix of a ``.pv2``.

    Raises:
        PayloadError: if *raw* is shorter than
            :data:`STORED_FRAME_HEADER_SIZE` or does not look like a stored
            frame.
    """
    if len(raw) < STORED_FRAME_HEADER_SIZE:
        raise PayloadError(
            f"a stored frame header is {STORED_FRAME_HEADER_SIZE} bytes, "
            f"got {len(raw)}"
        )
    version, security, compression, length, checksum = _PROTO.unpack_from(raw, 0)
    if version not in _ACCEPTED_VERSIONS:
        raise PayloadError(
            f"stored frame ProtocolHeader.Version is {version}; expected 2 "
            "(what the device writes to flash) or 3 (the wire version)"
        )
    index, blocks_minus_one, payload_length, uncompressed, stored, _reserved = (
        _BLOCK.unpack_from(raw, 20)
    )
    if index != 0:
        raise PayloadError(f"first block record has BlockIndex {index}, expected 0")
    return StoredFrameHeader(
        version=version,
        security=security,
        compression=compression,
        length=length,
        checksum=checksum,
        nblocks=blocks_minus_one + 1,
        firstblocklen=payload_length,
        blocksize=uncompressed or BLOCK_PLAINTEXT_SIZE,
        stored=stored,
    )


@dataclass(frozen=True, slots=True)
class StoredFrame:
    """A fully decoded ``.pv2``."""

    header: StoredFrameHeader
    device_id: bytes
    """``DataHeader.DeviceID``. 16 zero bytes in every shipped demo file."""
    packet_type: int
    """``DataHeader.Type``. 5 (image) in every file seen."""
    packet_id: int
    image: ImagePacket

    @property
    def checksum(self) -> int:
        """``ImageHeader.Checksum``. **0** in every shipped demo file."""
        return self.image.checksum

    @property
    def looks_like_our_push(self) -> bool:
        """Whether this file could have come from a frame we sent.

        A frame we built always carries a non-zero ``ImageHeader.Checksum``
        and the device's real UUID.  A file that has neither was written by
        something else -- on this firmware, by the factory.
        """
        return self.checksum != 0 and self.device_id != b"\x00" * 16


def parse_stored_frame(raw: bytes, *, strict: bool = False) -> StoredFrame:
    """Decode a whole ``.pv2`` into its :class:`~pyvisionect.packets.image.ImagePacket`.

    Args:
        raw: the complete file, starting at its ``ProtocolHeader``.
        strict: also validate the block chain's indices and invariants.

    Raises:
        PayloadError: on a truncated file or a header that is not a stored frame.
        BlockError: on a malformed block chain.
    """
    header = parse_stored_frame_header(raw)
    end = 20 + header.length
    if len(raw) < end:
        raise PayloadError(
            f"stored frame declares {header.length} body bytes "
            f"({end} total) but only {len(raw)} are present; the read was "
            "short -- there is no seek opcode, so restart it from offset 0"
        )
    plain = decode_blocks(raw[20:end], strict=strict)
    if len(plain) < _DATA_SIZE:
        raise PayloadError(
            f"stored frame inflated to {len(plain)} bytes, too short for a "
            f"{_DATA_SIZE}-byte DataHeader"
        )
    _priority, device_id, packet_type, packet_id, length, _reserved = _DATA.unpack_from(
        plain, 0
    )
    body = plain[_DATA_SIZE : _DATA_SIZE + length]
    if len(body) < length:
        raise PayloadError(
            f"DataHeader declares a {length}-byte payload, only {len(body)} "
            "bytes follow it"
        )
    return StoredFrame(
        header=header,
        device_id=device_id,
        packet_type=packet_type,
        packet_id=packet_id,
        image=ImagePacket.decode(body),
    )
