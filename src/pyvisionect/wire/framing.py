"""``ProtocolHeader`` / ``DataHeader`` framing and the incremental frame decoder.

Three nested fixed-width little-endian headers::

    ProtocolHeader  20 B   Version | Security | Compression | Length | Checksum
      +-- if Compression != 0: a chain of 24-B block records (see wire.blocks)
      +-- DataHeader  36 B  Priority | DeviceID[16] | Type | ID | Length | Reserved
            +-- payload (per packet type)

It is not CBOR.  ``packet.Type 12 = CBOR`` is a real enum member with a real
handler, but it is the newer "AC"/JS-app device generation's channel; firmware
7.4.4407 never emits it.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Callable, Iterator

from .blocks import encode_blocks, decode_blocks, lz4_compress_block
from .checksum import Direction, frame_checksum, verify_checksum
from .errors import (
    ChecksumMismatch,
    FrameTooLarge,
    ShortBuffer,
    UnsupportedCompression,
    UnsupportedSecurity,
    UnsupportedVersion,
)

__all__ = [
    "PROTOCOL_VERSION",
    "PROTOCOL_HEADER_SIZE",
    "DATA_HEADER_SIZE",
    "MAX_FRAME_LENGTH",
    "Compression",
    "Security",
    "ProtocolHeader",
    "DataHeader",
    "Frame",
    "FrameDecoder",
    "encode_frame",
]

PROTOCOL_VERSION = 3
"""Hardcoded by ``encoder.go:323`` and required by ``decoder.go:80``."""

PROTOCOL_HEADER_SIZE = 20
DATA_HEADER_SIZE = 36

MAX_FRAME_LENGTH = 0x3200000
"""52,428,800 bytes = 50 MiB. ``decoder.go:84``: ``"packet too large: %d"``."""

_PROTO = struct.Struct("<5I")
_DATA = struct.Struct("<I16sIIII")


class Compression:
    """``proto.CompressionType``."""

    NONE = 0
    LZ4 = 1


class Security:
    """``proto.SecurityType``.

    Only ``NONE`` is implemented.  ``2``/``3`` are AES-128-CBC under a session key
    the gateway escrows with a Visionect HTTPS service; that key material is not
    in the server image, so a self-hosted deployment is plaintext by construction.
    ``1`` is the firmware-container variant, not a TCP mode.
    """

    NONE = 0
    SESSION = 2
    SECURITY_PACKET = 3


@dataclass(frozen=True, slots=True)
class ProtocolHeader:
    """The outer 20-byte frame header."""

    version: int = PROTOCOL_VERSION
    security: int = Security.NONE
    compression: int = Compression.NONE
    length: int = 0
    checksum: int = 0

    def pack(self) -> bytes:
        return _PROTO.pack(
            self.version, self.security, self.compression, self.length, self.checksum
        )

    def pack_prefix(self) -> bytes:
        """The first 16 bytes, i.e. everything the device's CRC covers."""
        return struct.pack(
            "<4I", self.version, self.security, self.compression, self.length
        )

    @classmethod
    def unpack_from(cls, buf: bytes, offset: int = 0) -> "ProtocolHeader":
        if len(buf) - offset < PROTOCOL_HEADER_SIZE:
            raise ShortBuffer(
                f"need {PROTOCOL_HEADER_SIZE} bytes for a ProtocolHeader, "
                f"have {len(buf) - offset}"
            )
        return cls(*_PROTO.unpack_from(buf, offset))


@dataclass(frozen=True, slots=True)
class DataHeader:
    """The inner 36-byte ``packet.DataHeader``.

    ``device_id`` is literally the 16 UUID bytes; there is no hashing, no
    encoding.  ``priority`` is 0 in every observed frame in both directions and
    the gateway never writes it.
    """

    device_id: bytes
    type: int
    id: int
    length: int
    priority: int = 0
    reserved: int = 0

    def pack(self) -> bytes:
        if len(self.device_id) != 16:
            raise ValueError(f"device_id must be 16 bytes, got {len(self.device_id)}")
        return _DATA.pack(
            self.priority,
            self.device_id,
            self.type,
            self.id,
            self.length,
            self.reserved,
        )

    @classmethod
    def unpack_from(cls, buf: bytes, offset: int = 0) -> "DataHeader":
        if len(buf) - offset < DATA_HEADER_SIZE:
            raise ShortBuffer(
                f"need {DATA_HEADER_SIZE} bytes for a DataHeader, have {len(buf) - offset}"
            )
        priority, device_id, type_, id_, length, reserved = _DATA.unpack_from(buf, offset)
        return cls(
            device_id=device_id,
            type=type_,
            id=id_,
            length=length,
            priority=priority,
            reserved=reserved,
        )


@dataclass(frozen=True, slots=True)
class Frame:
    """One decoded frame: outer header, inner header, raw payload bytes.

    ``raw_body`` is kept so a test can re-emit a captured frame byte-for-byte
    without having to reproduce the vendor's LZ4 compressor.
    """

    header: ProtocolHeader
    data: DataHeader
    payload: bytes
    raw_body: bytes = field(repr=False, default=b"")
    plaintext: bytes = field(repr=False, default=b"")
    """``DataHeader`` + payload, after block decoding."""

    @property
    def device_id(self) -> bytes:
        return self.data.device_id

    @property
    def type(self) -> int:
        return self.data.type

    @property
    def id(self) -> int:
        return self.data.id


def encode_frame(
    data: DataHeader,
    payload: bytes,
    *,
    direction: Direction,
    compression: int = Compression.LZ4,
    compressor: Callable[[bytes], bytes] = lz4_compress_block,
    raw_body: bytes | None = None,
) -> bytes:
    """Marshal one frame.

    The order of operations is the vendor's, from ``encoder.go:214-339``:
    marshal -> compress -> CRC-32 -> (encrypt) -> header.  The CRC therefore
    covers the *compressed* body, and ``Length`` is the final body length.

    Args:
        data: the inner header. Its ``length`` field is overwritten with
            ``len(payload)``.
        payload: the type-specific payload bytes.
        direction: who is sending. Decides the checksum rule; see
            :mod:`pyvisionect.wire.checksum`.
        compression: ``Compression.LZ4`` (what the gateway always does, even for
            an 8-byte ack) or ``Compression.NONE`` (what the device always does).
        compressor: raw-LZ4-block compressor, for byte-exact replay experiments.
        raw_body: if given, used verbatim as the body instead of re-marshalling.
            Only for replay tests.

    Returns:
        The complete frame, ready to put on the wire.
    """
    if raw_body is not None:
        body = raw_body
    else:
        plain = (
            DataHeader(
                device_id=data.device_id,
                type=data.type,
                id=data.id,
                length=len(payload),
                priority=data.priority,
                reserved=data.reserved,
            ).pack()
            + payload
        )
        body = encode_blocks(plain, compressor=compressor) if compression else plain

    if len(body) > MAX_FRAME_LENGTH:
        raise FrameTooLarge(f"body is {len(body)} bytes, limit is {MAX_FRAME_LENGTH}")

    header = ProtocolHeader(
        version=PROTOCOL_VERSION,
        security=Security.NONE,
        compression=compression,
        length=len(body),
    )
    checksum = frame_checksum(
        direction, header16=header.pack_prefix(), body=body
    )
    return ProtocolHeader(
        version=header.version,
        security=header.security,
        compression=header.compression,
        length=header.length,
        checksum=checksum,
    ).pack() + body


class FrameDecoder:
    """Incremental, pure frame decoder. Feed it bytes, pull out frames.

    Sans-io: no sockets, no clock, no buffering policy beyond "keep the
    remainder".  A partial frame simply stays in the buffer.

    Args:
        direction: the direction of the stream being decoded.  As a server this
            is ``DEVICE_TO_SERVER``.
        strict: verify the inbound checksum and the block-chain invariants.
            **Off by default**: the real gateway validates neither, and the
            device's checksum covers no payload bytes.
    """

    def __init__(
        self, direction: Direction = Direction.DEVICE_TO_SERVER, *, strict: bool = False
    ) -> None:
        self.direction = direction
        self.strict = strict
        self._buf = bytearray()

    @property
    def buffered(self) -> int:
        """Bytes held back because they are a partial frame."""
        return len(self._buf)

    def feed(self, data: bytes) -> list[Frame]:
        """Append *data* and return every frame that is now complete."""
        self._buf += data
        return list(self._drain_frames())

    def _drain_frames(self) -> Iterator[Frame]:
        while True:
            buf = self._buf
            if len(buf) < PROTOCOL_HEADER_SIZE:
                return
            header = ProtocolHeader.unpack_from(buf)
            if header.version != PROTOCOL_VERSION:
                raise UnsupportedVersion(
                    f"unsupported protocol version {header.version}"
                )
            if header.length > MAX_FRAME_LENGTH:
                raise FrameTooLarge(f"packet too large: {header.length}")
            total = PROTOCOL_HEADER_SIZE + header.length
            if len(buf) < total:
                return
            body = bytes(buf[PROTOCOL_HEADER_SIZE:total])
            del self._buf[:total]
            yield self._finish(header, body)

    def _finish(self, header: ProtocolHeader, body: bytes) -> Frame:
        if self.strict and not verify_checksum(
            self.direction,
            header16=header.pack_prefix(),
            body=body,
            checksum=header.checksum,
        ):
            raise ChecksumMismatch(
                f"checksum 0x{header.checksum:08x} does not match the "
                f"{self.direction.value} rule"
            )
        if header.security != Security.NONE:
            raise UnsupportedSecurity(
                f"ProtocolHeader.Security = {header.security}; link encryption is "
                "not implemented (the session key is escrowed off-box)"
            )
        if header.compression == Compression.NONE:
            plain = body
        elif header.compression == Compression.LZ4:
            plain = decode_blocks(body, strict=self.strict)
        else:
            raise UnsupportedCompression(
                f"unsupported compression {header.compression}"
            )

        data = DataHeader.unpack_from(plain)
        payload = plain[DATA_HEADER_SIZE : DATA_HEADER_SIZE + data.length]
        return Frame(
            header=header,
            data=data,
            payload=payload,
            raw_body=body,
            plaintext=plain,
        )
