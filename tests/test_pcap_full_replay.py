"""Full replay against the raw 1.9 MB pcap, when it is available.

Skipped unless the capture is present (``PYVISIONECT_PCAP`` or the default path)
and ``dpkt`` is installed.  The committed fixture covers the same ground in
~90 KB; this test exists so that the extraction itself can be re-verified
against the original bytes, including the whole 1.84 MB image push.
"""

from __future__ import annotations

import pytest

from pyvisionect.devices.enums import PacketType
from pyvisionect.packets import decode_payload
from pyvisionect.wire import (
    Compression,
    Direction,
    FrameDecoder,
    encode_blocks,
    encode_frame,
    iter_block_headers,
)

dpkt = pytest.importorskip("dpkt", reason="pcap replay needs dpkt")


@pytest.fixture(scope="module")
def streams(pcap_path):
    if pcap_path is None:
        pytest.skip("the raw pcap is not on this machine; the fixture test covers it")
    import sys
    from pathlib import Path

    tools = Path(__file__).resolve().parents[1] / "tools"
    sys.path.insert(0, str(tools))
    try:
        from extract_pcap_fixture import reassemble, split_frames
    finally:
        sys.path.remove(str(tools))
    return reassemble(str(pcap_path)), split_frames


def test_every_frame_in_the_pcap_decodes(streams) -> None:
    reassembled, split_frames = streams
    totals = {"frames": 0, "bytes": 0, "exact": 0, "skipped": 0}
    for (_index, key), (buf, gaps) in reassembled.items():
        direction = (
            Direction.SERVER_TO_DEVICE if key[1] == 11113 else Direction.DEVICE_TO_SERVER
        )
        for offset, total in split_frames(buf):
            raw = buf[offset : offset + total]
            damaged = any(offset <= g < offset + total for g, _n in gaps)
            if damaged:
                totals["skipped"] += 1
                continue
            frames = FrameDecoder(direction, strict=True).feed(raw)
            assert len(frames) == 1
            frame = frames[0]
            payload = decode_payload(frame.type, frame.payload)
            assert payload is not None
            assert payload.encode() == frame.payload
            totals["frames"] += 1
            totals["bytes"] += total

            def replay(chunk, _it=iter(
                [
                    (h.stored, frame.raw_body[o + 24 : o + 24 + h.payload_length])
                    for o, h in iter_block_headers(frame.raw_body)
                ]
            )):
                stored, original = next(_it)
                return chunk + b"\x00" if stored == 1 else original

            again = encode_frame(
                frame.data,
                payload.encode(),
                direction=direction,
                compression=frame.header.compression,
                compressor=replay if frame.header.compression else encode_blocks,
            )
            if again == raw:
                totals["exact"] += 1
    assert totals["frames"] == 61
    assert totals["exact"] == 61
    assert totals["skipped"] == 1  # the image push, which lost 5 segments


def test_the_image_push_block_chain_in_full(streams) -> None:
    """Walk the real 385-block chain, resynchronising around the dropped segments."""
    import sys
    from pathlib import Path

    tools = Path(__file__).resolve().parents[1] / "tools"
    sys.path.insert(0, str(tools))
    try:
        from extract_pcap_fixture import parse_block_chain
    finally:
        sys.path.remove(str(tools))

    reassembled, split_frames = streams
    found = None
    for (_index, key), (buf, gaps) in reassembled.items():
        if key[1] != 11113:
            continue
        for offset, total in split_frames(buf):
            if total > 100_000:
                found = (buf[offset + 20 : offset + total],
                         [(g - offset - 20, n) for g, n in gaps])
    assert found is not None
    body, frame_gaps = found
    blocks, damaged = parse_block_chain(body, frame_gaps)
    assert len(blocks) == 383
    assert blocks[0][1][1] == 384  # BlocksMinusOne, in every block
    assert all(fields[1] == 384 for _o, fields in blocks)
    assert sum(1 for _o, f in blocks if f[4] == 1) == 60
    assert damaged == [9, 10, 15, 16]
