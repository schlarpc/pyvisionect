"""Per-type payload codecs. Pure: no sockets, no clock.

Every codec is a plain dataclass with ``decode(payload) -> Packet`` and
``encode() -> bytes``, and every one of them round-trips byte-exactly on the
captured traffic.

Dispatch is a dict, not a class hierarchy, because that is what the gateway does
(``map[packet.Type]handlers.Handler``, seven entries, built with seven
``mapassign_fast32`` calls).
"""

from __future__ import annotations

from typing import Any, Callable

from ..devices.enums import PacketType
from .control import NO_ACK_ID, ControlPacket
from .file import (
    FILE_LIST_LENGTH,
    FILE_LIST_PATH,
    FileBytes,
    FileErase,
    FileListEntry,
    FileOpen,
    FilePacket,
    FileWriteDone,
    parse_file_listing,
    screen_cache_name,
)
from .image import (
    IMAGE_HEADER_SIZE,
    MAX_RECTANGLES,
    RECT_HEADER_SIZE,
    RECT_UPDATE_OPTIONS_DEFAULT,
    ImagePacket,
    Rectangle,
)
from .misc import ButtonPacket, CborPacket, CommandPacket, GpsPacket, TouchPacket
from .param import ParamItem, ParamPacket, decode_value, encode_value
from .status import (
    ABSENT,
    SENTINEL_TAG,
    STATUS_TAG_NAMES,
    STATUS_TAGS,
    StatusCodec,
    StatusPacket,
    StatusTag,
    decode_status_fields,
)

__all__ = [
    "ABSENT",
    "FILE_LIST_LENGTH",
    "FILE_LIST_PATH",
    "FileListEntry",
    "parse_file_listing",
    "ButtonPacket",
    "CborPacket",
    "CommandPacket",
    "ControlPacket",
    "DECODERS",
    "FileBytes",
    "FileErase",
    "FileOpen",
    "FilePacket",
    "FileWriteDone",
    "GpsPacket",
    "IMAGE_HEADER_SIZE",
    "ImagePacket",
    "MAX_RECTANGLES",
    "NO_ACK_ID",
    "ParamItem",
    "ParamPacket",
    "RECT_HEADER_SIZE",
    "RECT_UPDATE_OPTIONS_DEFAULT",
    "Rectangle",
    "SENTINEL_TAG",
    "STATUS_TAGS",
    "STATUS_TAG_NAMES",
    "StatusCodec",
    "StatusPacket",
    "StatusTag",
    "TouchPacket",
    "decode_payload",
    "decode_status_fields",
    "decode_value",
    "encode_value",
    "screen_cache_name",
]

DECODERS: dict[int, Callable[[bytes], Any]] = {
    PacketType.CONTROL: ControlPacket.decode,
    PacketType.COMMAND: CommandPacket.decode,
    PacketType.STATUS: StatusPacket.decode,
    PacketType.IMAGE: ImagePacket.decode,
    PacketType.TOUCH: TouchPacket.decode,
    PacketType.GPS: GpsPacket.decode,
    PacketType.PARAM: ParamPacket.decode,
    PacketType.FILE: FilePacket.decode,
    PacketType.BUTTON: ButtonPacket.decode,
    PacketType.CBOR: CborPacket.decode,
}
"""``packet.Type`` -> payload decoder.

Types 4 (``input``) and 9 are holes in the vendor's own tables, and the
bootloader type moved out to its own mux protocol versions, so neither appears
here.
"""


def decode_payload(packet_type: int, payload: bytes) -> Any | None:
    """Decode *payload* for *packet_type*, or return None for an unknown type.

    Returning None rather than raising mirrors the gateway's dispatcher, which
    NACKs an unknown type (``WriteControl(id, 0)``, ``pv3.go:473``) instead of
    dropping the connection.
    """
    decoder = DECODERS.get(packet_type)
    return decoder(payload) if decoder else None
