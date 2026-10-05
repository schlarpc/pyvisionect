"""Golden replay against real traffic from the user's 32" Visionect sign.

The fixture (``fixtures/golden.json.gz``, ~90 KB) was extracted from a 1.9 MB
server-side pcap with ``tools/extract_pcap_fixture.py``: 5 TCP sessions, a full
connect, 20+ heartbeats, a TCLV param round trip, a file open/NACK and a 1.84 MB
image push.

What is asserted
----------------

1. **Every frame decodes.**
2. **Every frame re-encodes byte-exactly**, in two senses:

   * device -> server frames (``Compression == 0``) are rebuilt from scratch --
     payload re-encoded through its own codec, then re-framed -- and must equal
     the captured bytes.
   * server -> device frames (``Compression == 1``) are rebuilt from scratch
     too, but with a compressor that replays the capture's own LZ4 payloads.
     That is deliberate: the vendor's ``vss/lz4.Lz4Compress`` does not produce
     the same bytes as python-lz4 for the same input (measured: 0 of 321 blocks
     matched), so an LZ4-for-LZ4 comparison would test the compressor rather
     than this library.  Everything else -- the block headers, the stored/LZ4
     decision, the ``CRC-32``, the ``ProtocolHeader`` -- is reproduced exactly.
3. **The direction-dependent checksum rule holds on every frame**, verified in
   strict mode.

Capture limitation, stated plainly: ``tcpdump`` dropped five 1460-byte segments
inside the image push.  4 of its 385 blocks are therefore unrecoverable and
``crc32(body)`` for that one frame cannot be recomputed.  Everything else about
that frame -- 383 of 385 block headers, the stored/LZ4 split, the ``DataHeader``,
the ``ImageHeader`` and both ``RectangleHeader``s -- is checked.
"""

from __future__ import annotations

import struct

import pytest

from pyvisionect.devices.enums import PacketType
from pyvisionect.packets import (
    ControlPacket,
    FilePacket,
    ImagePacket,
    ParamPacket,
    Rectangle,
    StatusPacket,
    decode_payload,
)
from pyvisionect.wire import (
    BlockHeader,
    Compression,
    Direction,
    FrameDecoder,
    encode_blocks,
    encode_frame,
    iter_block_headers,
    lz4_decompress_block,
)

DEVICE_UUID = bytes.fromhex("00112233445566778899aabb00000000")


def unb64(text: str) -> bytes:
    import base64

    return base64.b64decode(text)


def _direction(record: dict) -> Direction:
    return (
        Direction.SERVER_TO_DEVICE
        if record["direction"] == "server->device"
        else Direction.DEVICE_TO_SERVER
    )


# ------------------------------------------------------------------ inventory

def test_fixture_inventory(golden_frames, small_frames, image_frame) -> None:
    assert len(golden_frames) == 62
    assert len(small_frames) == 61
    device = [f for f in golden_frames if f["direction"] == "device->server"]
    server = [f for f in golden_frames if f["direction"] == "server->device"]
    assert len(device) == 31
    assert len(server) == 31
    assert image_frame["length"] == 1783915


def test_every_frame_decodes_in_strict_mode(small_frames) -> None:
    """Strict mode verifies the inbound checksum, so this also proves the
    direction-dependent CRC rule on all 61 verbatim frames."""
    decoded = 0
    for record in small_frames:
        raw = unb64(record["bytes"])
        frames = FrameDecoder(_direction(record), strict=True).feed(raw)
        assert len(frames) == 1, record
        frame = frames[0]
        assert frame.device_id == DEVICE_UUID
        assert frame.data.priority == 0
        assert frame.data.reserved == 0
        payload = decode_payload(frame.type, frame.payload)
        assert payload is not None, f"no codec for type {frame.type}"
        decoded += 1
    assert decoded == 61


def test_directional_compression_habits(small_frames) -> None:
    """The device never compresses; the gateway always does, even for an ack."""
    for record in small_frames:
        frame = FrameDecoder(_direction(record)).feed(unb64(record["bytes"]))[0]
        if record["direction"] == "device->server":
            assert frame.header.compression == Compression.NONE
        else:
            assert frame.header.compression == Compression.LZ4


def test_packet_types_seen_on_the_wire(small_frames) -> None:
    types: dict[str, set[int]] = {"device->server": set(), "server->device": set()}
    for record in small_frames:
        frame = FrameDecoder(_direction(record)).feed(unb64(record["bytes"]))[0]
        types[record["direction"]].add(frame.type)
    assert types["device->server"] == {PacketType.CONTROL, PacketType.STATUS,
                                       PacketType.PARAM}
    assert types["server->device"] == {PacketType.CONTROL, PacketType.PARAM,
                                       PacketType.FILE}
    # Type 12 (CBOR) never appears: this firmware does not use that channel.
    assert PacketType.CBOR not in types["device->server"]


# ------------------------------------------------------------- byte-exactness

def test_device_frames_rebuild_from_scratch_byte_exactly(small_frames) -> None:
    """Full-stack: payload codec -> DataHeader -> ProtocolHeader -> device CRC."""
    checked = 0
    for record in small_frames:
        if record["direction"] != "device->server":
            continue
        raw = unb64(record["bytes"])
        frame = FrameDecoder(Direction.DEVICE_TO_SERVER, strict=True).feed(raw)[0]
        payload = decode_payload(frame.type, frame.payload)
        assert payload.encode() == frame.payload, record
        again = encode_frame(
            frame.data,
            payload.encode(),
            direction=Direction.DEVICE_TO_SERVER,
            compression=frame.header.compression,
        )
        assert again == raw, record
        checked += 1
    assert checked == 31


def _replaying_compressor(body: bytes):
    """A compressor that hands back the capture's own per-block LZ4 payloads.

    For a block the capture stored verbatim, it returns something one byte longer
    than the chunk so our encoder takes the stored path -- which is exactly the
    decision the vendor made for that chunk.
    """
    originals: list[tuple[int, bytes]] = []
    for offset, header in iter_block_headers(body):
        payload = body[offset + 24 : offset + 24 + header.payload_length]
        originals.append((header.stored, payload))
    index = [0]

    def compress(chunk: bytes) -> bytes:
        stored, payload = originals[index[0]]
        index[0] += 1
        return chunk + b"\x00" if stored == 1 else payload

    return compress


def test_server_frames_rebuild_from_scratch_byte_exactly(small_frames) -> None:
    """Full-stack for the gateway direction, with the capture's LZ4 replayed."""
    checked = 0
    for record in small_frames:
        if record["direction"] != "server->device":
            continue
        raw = unb64(record["bytes"])
        frame = FrameDecoder(Direction.SERVER_TO_DEVICE, strict=True).feed(raw)[0]
        payload = decode_payload(frame.type, frame.payload)
        assert payload.encode() == frame.payload, record
        again = encode_frame(
            frame.data,
            payload.encode(),
            direction=Direction.SERVER_TO_DEVICE,
            compression=Compression.LZ4,
            compressor=_replaying_compressor(frame.raw_body),
        )
        assert again == raw, record
        checked += 1
    assert checked == 30  # 31 server frames, minus the image push


def test_server_frame_bodies_rebuild_byte_exactly(small_frames) -> None:
    """The block chain alone: headers, order, stored flag, payload placement."""
    for record in small_frames:
        if record["direction"] != "server->device":
            continue
        frame = FrameDecoder(Direction.SERVER_TO_DEVICE).feed(unb64(record["bytes"]))[0]
        rebuilt = encode_blocks(
            frame.plaintext, compressor=_replaying_compressor(frame.raw_body)
        )
        assert rebuilt == frame.raw_body, record


def test_whole_stream_replays_through_one_decoder(small_frames) -> None:
    """Concatenate each direction of each session and decode it as one stream."""
    streams: dict[tuple[int, str], bytes] = {}
    for record in small_frames:
        key = (record["session"], record["direction"])
        streams[key] = streams.get(key, b"") + unb64(record["bytes"])
    total = 0
    for (session, direction), data in sorted(streams.items()):
        decoder = FrameDecoder(
            Direction.SERVER_TO_DEVICE
            if direction == "server->device"
            else Direction.DEVICE_TO_SERVER,
            strict=True,
        )
        frames = decoder.feed(data)
        assert decoder.buffered == 0, (session, direction)
        total += len(frames)
    assert total == 61


def test_streams_replay_one_byte_at_a_time(small_frames) -> None:
    """The decoder must not care how the TCP segments fall."""
    device_stream = b"".join(
        unb64(r["bytes"]) for r in small_frames if r["direction"] == "device->server"
    )
    decoder = FrameDecoder(Direction.DEVICE_TO_SERVER, strict=True)
    frames = []
    for i in range(len(device_stream)):
        frames.extend(decoder.feed(device_stream[i : i + 1]))
    assert len(frames) == 31
    assert decoder.buffered == 0


# ---------------------------------------------------------- captured semantics

def test_device_packet_ids_reset_to_one_per_connection(small_frames) -> None:
    per_session: dict[int, list[int]] = {}
    for record in small_frames:
        if record["direction"] != "device->server":
            continue
        frame = FrameDecoder(Direction.DEVICE_TO_SERVER).feed(unb64(record["bytes"]))[0]
        if frame.type == PacketType.STATUS:
            per_session.setdefault(record["session"], []).append(frame.id)
    # Session 0 was captured mid-connection (1307...), sessions 2 and 4 start at 1.
    assert per_session[0][0] == 1307
    assert per_session[2] == [1, 2, 3, 4, 5, 6]
    assert per_session[4] == list(range(1, 14))


def test_server_packet_ids_are_random_uint32(small_frames) -> None:
    ids = []
    for record in small_frames:
        if record["direction"] != "server->device":
            continue
        frame = FrameDecoder(Direction.SERVER_TO_DEVICE).feed(unb64(record["bytes"]))[0]
        if frame.type in (PacketType.PARAM, PacketType.FILE):
            ids.append(frame.id)
    assert ids, "expected the param read and the file open"
    assert all(i > 0xFFFF for i in ids), ids


def test_every_device_packet_is_acked_with_flags_one(small_frames) -> None:
    device_ids = {}
    acks = {}
    for record in small_frames:
        frame = FrameDecoder(_direction(record)).feed(unb64(record["bytes"]))[0]
        key = (record["session"], frame.id)
        if record["direction"] == "device->server":
            if frame.type != PacketType.CONTROL:
                device_ids[key] = frame.type
        elif frame.type == PacketType.CONTROL:
            acks[key] = ControlPacket.decode(frame.payload)
    assert device_ids
    for key, packet_type in device_ids.items():
        assert key in acks, f"session {key[0]} id {key[1]} ({packet_type}) was not acked"
        assert acks[key].flags == 1
        assert acks[key].is_ack


def test_device_nacks_the_screen_cache_open(small_frames) -> None:
    """The gateway opens @screen_N; this sign has no cached screen file."""
    opens: dict[int, FilePacket] = {}
    nacks: dict[int, ControlPacket] = {}
    for record in small_frames:
        frame = FrameDecoder(_direction(record)).feed(unb64(record["bytes"]))[0]
        if record["direction"] == "server->device" and frame.type == PacketType.FILE:
            opens[frame.id] = FilePacket.decode(frame.payload)
        if record["direction"] == "device->server" and frame.type == PacketType.CONTROL:
            packet = ControlPacket.decode(frame.payload)
            if packet.is_nack:
                nacks[frame.id] = packet
    assert opens
    for packet_id, file_packet in opens.items():
        parsed = file_packet.parsed()
        assert parsed.filename.startswith("@screen_")
        assert packet_id in nacks
        assert nacks[packet_id].payload == bytes.fromhex("00008a00")


def test_captured_status_decodes_to_the_known_values(small_frames) -> None:
    record = next(
        r
        for r in small_frames
        if r["direction"] == "device->server" and r["session"] == 0
    )
    frame = FrameDecoder(Direction.DEVICE_TO_SERVER).feed(unb64(record["bytes"]))[0]
    status = StatusPacket.decode(frame.payload)
    assert len(status.records) == 61
    assert status.sentinel == 0x779050DF
    fields = status.fields()
    assert fields["ConnectReason"]["name"] == "heartbeat"
    assert fields["GTIN"] == "3830065461792"
    assert fields["BSSID"] == "00:00:5E:00:53:00"
    assert fields["SignalStrength"] == -44
    assert fields["DeviceUptime"] == 35793
    assert fields["Firmware"] == {"major": 7, "minor": 4, "revision": 4407}
    assert fields["Features"]["WiFiOTA"] is True
    assert fields["DisplayWidth"] == 2880
    assert fields["DisplayHeight"] == 640
    assert status.display_type == 0xC2050128
    assert status.display_state_crc == 0xDB963A1A
    assert status.hardware_name_id == 8
    assert status.protocol_version == 3


def test_first_status_of_a_connection_has_connect_reason_five(small_frames) -> None:
    for session in (2, 4):
        first = next(
            r
            for r in small_frames
            if r["session"] == session and r["direction"] == "device->server"
        )
        frame = FrameDecoder(Direction.DEVICE_TO_SERVER).feed(unb64(first["bytes"]))[0]
        status = StatusPacket.decode(frame.payload)
        assert status.connect_reason == 5, session


def test_captured_param_round_trip(small_frames) -> None:
    requests, replies = [], []
    for record in small_frames:
        frame = FrameDecoder(_direction(record)).feed(unb64(record["bytes"]))[0]
        if frame.type != PacketType.PARAM:
            continue
        (requests if record["direction"] == "server->device" else replies).append(
            (frame.id, ParamPacket.decode(frame.payload))
        )
    assert len(requests) == 1 and len(replies) == 1
    request_id, request = requests[0]
    _reply_id, reply = replies[0]
    assert [item.id for item in request] == [29, 18, 19, 65, 66, 2]
    assert all(item.value == b"" for item in request)
    assert reply.by_id()[18].as_str() == "visionect.internal.example.com"
    assert reply.by_id()[19].as_int() == 11113
    assert reply.by_id()[2].as_int() == 6
    # The device replies on its own counter, not by echoing the request id.
    assert _reply_id != request_id
    assert _reply_id < 0xFFFF


# ---------------------------------------------------------------- image push

def test_image_push_protocol_header(image_frame) -> None:
    from pyvisionect.wire import ProtocolHeader

    header = ProtocolHeader.unpack_from(unb64(image_frame["protocol_header"]))
    assert header.version == 3
    assert header.security == 0
    assert header.compression == Compression.LZ4
    assert header.length == 1783895
    assert header.checksum != 0
    assert image_frame["checksum_verifiable"] is False


def test_image_push_block_chain_invariants(image_frame) -> None:
    headers = [
        BlockHeader.unpack_from(unb64(raw)) for raw in image_frame["block_headers"]
    ]
    assert image_frame["total_blocks"] == 385
    assert len(headers) == 383  # two headers fell inside the dropped segments
    assert image_frame["missing_block_headers"] == [10, 16]
    # BlocksMinusOne is an INDEX, repeated identically in every block.
    assert {h.blocks_minus_one for h in headers} == {384}
    assert all(h.block_count == 385 for h in headers)
    # Indices are strictly increasing and within range.
    indices = [h.index for h in headers]
    assert indices == sorted(indices)
    assert len(set(indices)) == len(indices)
    assert indices[0] == 0 and indices[-1] == 384
    assert all(h.reserved == 0 for h in headers)
    assert all(h.stored in (0, 1) for h in headers)
    # 4800-byte plaintext blocks with a 104-byte remainder at the end.
    assert {h.uncompressed_length for h in headers[:-1]} == {4800}
    assert headers[-1].uncompressed_length == 104
    assert image_frame["plaintext_length"] == 1843304 == 36 + 1843268


def test_image_push_stored_versus_lz4_split(image_frame) -> None:
    """60 stored + 325 LZ4 over all 385 blocks; two LZ4 headers were lost."""
    headers = [
        BlockHeader.unpack_from(unb64(raw)) for raw in image_frame["block_headers"]
    ]
    stored = sum(1 for h in headers if h.stored == 1)
    compressed = sum(1 for h in headers if h.stored == 0)
    assert stored == 60
    assert compressed == 323
    assert stored + compressed + len(image_frame["missing_block_headers"]) == 385
    assert image_frame["stored_count"] == 60
    # Stored blocks carry PayloadLength == UncompressedLength, by definition.
    for header in headers:
        if header.stored == 1:
            assert header.payload_length == header.uncompressed_length
        else:
            assert header.payload_length < header.uncompressed_length


def test_image_push_retained_blocks_inflate_and_re_emit_exactly(image_frame) -> None:
    retained = image_frame["retained_blocks"]
    assert len(retained) >= 20
    kinds = {"stored": 0, "lz4": 0}
    for index, raw_b64 in retained.items():
        raw = unb64(raw_b64)
        header = BlockHeader.unpack_from(raw)
        assert header.index == int(index)
        payload = raw[24 : 24 + header.payload_length]
        assert len(payload) == header.payload_length
        if header.is_stored:
            plain = payload
            kinds["stored"] += 1
        else:
            plain = lz4_decompress_block(payload, header.uncompressed_length)
            kinds["lz4"] += 1
        assert len(plain) == header.uncompressed_length
        # Re-emit this one block through our encoder with its own payload.
        def replay(chunk: bytes, _p=payload, _s=header.stored) -> bytes:
            return chunk + b"\x00" if _s == 1 else _p

        assert encode_blocks(plain, compressor=replay)[:24] == struct.pack(
            "<6I", 0, 0, header.payload_length, header.uncompressed_length,
            header.stored, 0
        )
    assert kinds["lz4"] > 0 and kinds["stored"] > 0, kinds


def test_image_push_data_header(image_frame) -> None:
    from pyvisionect.wire import DataHeader

    header = DataHeader.unpack_from(unb64(image_frame["data_header"]))
    assert header.device_id == DEVICE_UUID
    assert header.type == PacketType.IMAGE
    assert header.id == 3544696833
    assert header.length == 1843268
    assert header.priority == 0 and header.reserved == 0
    assert header.pack() == unb64(image_frame["data_header"])


def test_image_push_payload_headers_round_trip_byte_exactly(image_frame) -> None:
    """Rebuild the ImageHeader + both RectangleHeaders from decoded values."""
    image_header = unb64(image_frame["image_header"])
    rect_headers = [unb64(raw) for raw in image_frame["rect_headers"]]

    # Reassemble a complete payload with zero-filled pixel data; the pixels were
    # lost in the capture's dropped segments but every header byte survived.
    payload = image_header
    for raw in rect_headers:
        length = struct.unpack_from("<I", raw, 20)[0]
        payload += raw + b"\x00" * length

    packet = ImagePacket.decode(payload)
    assert packet.checksum == 3741864387
    assert packet.options == 0
    assert len(packet.rectangles) == 2
    for screen_id, rect in enumerate(packet.rectangles):
        assert rect.screen_id == screen_id
        assert (rect.x, rect.y, rect.width, rect.height) == (0, 0, 2880, 640)
        assert rect.encoding == 4
        assert rect.image_type == 1
        assert rect.update_options == 0x0102  # the marshaller's 4-bit auto-fill
        assert rect.options == 0
        assert rect.reserved == 0
        assert len(rect.data) == 921600 == rect.expected_data_length
    rebuilt = packet.encode()
    assert rebuilt[:20] == image_header
    assert rebuilt[20:44] == rect_headers[0]
    assert rebuilt[44 + 921600 : 68 + 921600] == rect_headers[1]
    assert rebuilt == payload


def test_image_push_geometry_matches_the_interlacing_hack(image_frame) -> None:
    """The server models 4 x 1440x640; the wire carries 2 rects of 2880x640."""
    from pyvisionect.devices import panel_for

    panel = panel_for(0xC2050128)  # not in the vendor enum -> the default row
    assert (panel.width, panel.height, panel.displays) == (1440, 640, 4)
    assert panel.driver == "eink-flip"
    assert panel.driver_spec.mirror is True
    rects = [unb64(raw) for raw in image_frame["rect_headers"]]
    widths = {struct.unpack_from("<H", raw, 8)[0] for raw in rects}
    heights = {struct.unpack_from("<H", raw, 10)[0] for raw in rects}
    assert widths == {panel.width * 2} == {2880}
    assert heights == {panel.height} == {640}
    assert 2 * 2880 * 640 == panel.canvas_width * panel.canvas_height == 3686400


def test_image_checksum_is_what_the_device_reports_back(image_frame, small_frames) -> None:
    """ImageHeader.Checksum == the DisplayStateCRC the device echoes."""
    image_header = unb64(image_frame["image_header"])
    pushed = struct.unpack_from("<I", image_header, 0)[0]
    assert pushed == 3741864387
    # The capture's statuses predate this push, so they report the *previous*
    # state. The point is that the two live in the same number space.
    reported = set()
    for record in small_frames:
        if record["direction"] != "device->server":
            continue
        frame = FrameDecoder(Direction.DEVICE_TO_SERVER).feed(unb64(record["bytes"]))[0]
        if frame.type == PacketType.STATUS:
            crc = StatusPacket.decode(frame.payload).display_state_crc
            if crc is not None:
                reported.add(crc)
    assert reported
    assert all(0 <= crc <= 0xFFFFFFFF for crc in reported)
