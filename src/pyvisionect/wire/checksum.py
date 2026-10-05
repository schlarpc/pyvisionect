"""The direction-dependent ``ProtocolHeader.Checksum``.

This is the single nastiest detail in the Visionect wire protocol, and the one
most likely to be implemented symmetrically and then fail silently.  There is
exactly one function here, it takes an explicit :class:`Direction`, and nothing
else in the library is allowed to call :func:`crc32_ieee` on a frame directly.

What is actually CRC'd
----------------------

===================  ========================================  ==================================
direction            covered bytes                             where this came from
===================  ========================================  ==================================
``SERVER_TO_DEVICE``  the whole body, after compression,        ``encoder.go:314`` computes
                      before encryption                        ``crc32.ChecksumIEEE(body)`` on the
                                                                same slice later handed to
                                                                ``encrypt``; stored at
                                                                ``encoder.go:327``.
``DEVICE_TO_SERVER``  ``header[0:16]`` -- Version, Security,    No code evidence (the firmware was
                      Compression, Length.  **It does not       not disassembled); 26/26 captured
                      cover the payload at all.**               device frames match this rule and
                                                                nothing else.
===================  ========================================  ==================================

The proof that the device rule really is header-only: two status packets ten
minutes apart, with different packet IDs and different battery/uptime values,
carry the *identical* checksum ``0x198ceb01``, because their
``(Version=3, Security=0, Compression=0, Length=532)`` prefix is identical.
Conversely two server acks with identical headers ``(3, 0, 1, 68)`` carry
different checksums because their bodies differ.

Consequences for an implementation
----------------------------------

* As a **server** (which is what this library is), emit ``crc32(body)``.
* The inbound field is only verifiable in strict mode -- see
  :func:`verify_checksum`.  The real gateway never validates it: there is no
  ``crc32`` call anywhere in ``(*Decoder).DecodeWithTimeout``.
* As a **device emulator** (used by the test suite to re-encode captured device
  frames byte-exactly), emit ``crc32(header[0:16])``.
"""

from __future__ import annotations

import enum
import zlib

__all__ = ["Direction", "crc32_ieee", "frame_checksum", "verify_checksum"]


class Direction(enum.Enum):
    """Which end of the link produced a frame.

    There is no default and no "either": every call site must say which it is.
    """

    SERVER_TO_DEVICE = "server->device"
    """We are the server. Checksum covers the body."""

    DEVICE_TO_SERVER = "device->server"
    """The sign produced the frame. Checksum covers only ``header[0:16]``."""


def crc32_ieee(data: bytes) -> int:
    """CRC-32/IEEE 802.3 (the ``zlib``/``crc32.ChecksumIEEE`` polynomial), as uint32."""
    return zlib.crc32(data) & 0xFFFFFFFF


def frame_checksum(direction: Direction, *, header16: bytes, body: bytes) -> int:
    """Compute ``ProtocolHeader.Checksum`` for *direction*.

    Args:
        direction: who is sending. Not optional, on purpose.
        header16: the first 16 bytes of the 20-byte ``ProtocolHeader``, i.e.
            ``Version | Security | Compression | Length`` already packed. The
            ``Length`` field must already hold the final body length, because
            the device's CRC covers it.
        body: the frame body as it will appear on the wire (post-compression,
            pre-encryption -- encryption is not implemented).

    Returns:
        The uint32 to store at offset 16 of the ``ProtocolHeader``.

    Raises:
        ValueError: if *header16* is not exactly 16 bytes, or *direction* is not
            a :class:`Direction`.
    """
    if not isinstance(direction, Direction):
        raise ValueError(
            f"direction must be a wire.Direction, not {type(direction).__name__}; "
            "the checksum rule is not symmetric"
        )
    if len(header16) != 16:
        raise ValueError(f"header16 must be exactly 16 bytes, got {len(header16)}")

    if direction is Direction.SERVER_TO_DEVICE:
        return crc32_ieee(body)
    # DEVICE_TO_SERVER: header-only. Yes, really; see the module docstring.
    return crc32_ieee(header16)


def verify_checksum(
    direction: Direction, *, header16: bytes, body: bytes, checksum: int
) -> bool:
    """Return whether *checksum* matches the rule for *direction*.

    Used by the decoder only when ``strict=True``.  It is off by default
    because:

    * the real gateway does not check it, so a correct-but-strict
      reimplementation would reject frames the vendor server accepts; and
    * the device rule covers no payload bytes, so the check cannot detect
      payload corruption and buys nothing.
    """
    return frame_checksum(direction, header16=header16, body=body) == checksum
