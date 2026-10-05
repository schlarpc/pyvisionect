#!/usr/bin/env python3
"""Turn a device pcap into the small golden fixture the test suite replays.

Usage::

    python tools/extract_pcap_fixture.py device-11113.pcap tests/fixtures/golden.json.gz

The fixture holds every frame verbatim except the 1.84 MB image push, for which
it keeps the full 383-record block-header chain, a sample of complete blocks, and
the 76 bytes of decoded plaintext headers (``DataHeader`` + ``ImageHeader`` +
both ``RectangleHeader``s).  That keeps the committed fixture around 150 KB while
still exercising everything the big frame proves.

Needs ``dpkt`` (``pip install 'pyvisionect[test]'``).  Only used to *build* the
fixture; the test suite does not need the pcap or dpkt.

A note on the source capture: ``tcpdump`` dropped five 1460-byte segments inside
the image push.  Their offsets are recorded as ``gaps`` and the blocks that
overlap them as ``damaged_blocks``; the extractor resynchronises the chain by
scanning for the next header whose ``BlocksMinusOne`` matches, so the other 381
blocks are recovered intact.
"""

from __future__ import annotations

import argparse
import base64
import collections
import gzip
import json
import socket
import struct
import sys
from typing import Any

SLL2 = 276
SLL = 113

RETAIN_BLOCKS = list(range(0, 12)) + list(range(18, 24)) + list(range(380, 385))
"""Which image-push blocks to keep whole: a run from the start (where the
``ImageHeader`` lives), a run just past the damaged region, and the tail
including the short final block."""


def reassemble(path: str) -> dict[tuple[int, tuple], tuple[bytes, list[tuple[int, int]]]]:
    """Reassemble both directions of every TCP session in *path*.

    Returns ``{(session_index, 4-tuple): (stream_bytes, gaps)}`` where *gaps* is
    ``[(offset, length), ...]`` of zero-filled holes left by dropped packets.
    """
    import dpkt

    with open(path, "rb") as handle:
        reader = dpkt.pcap.Reader(handle)
        datalink = reader.datalink()
        packets = list(reader)
    flows: dict[tuple, dict] = {}
    order: list[tuple] = []

    for _ts, buf in packets:
        if datalink == SLL2:
            proto = struct.unpack_from(">H", buf, 0)[0]
            payload = buf[20:]
        elif datalink == SLL:
            proto = struct.unpack_from(">H", buf, 14)[0]
            payload = buf[16:]
        else:
            eth = dpkt.ethernet.Ethernet(buf)
            proto, payload = eth.type, bytes(eth.data.pack())
        if proto != 0x0800:
            continue
        ip = dpkt.ip.IP(payload)
        tcp = ip.data
        if not isinstance(tcp, dpkt.tcp.TCP):
            continue
        key = (
            socket.inet_ntoa(ip.src),
            tcp.sport,
            socket.inet_ntoa(ip.dst),
            tcp.dport,
        )
        session = tuple(sorted([(key[0], key[1]), (key[2], key[3])]))
        if session not in flows:
            flows[session] = {}
            order.append(session)
        side = flows[session].setdefault(key, {})
        if len(tcp.data):
            side.setdefault("segs", {})[tcp.seq] = bytes(tcp.data)

    out: dict[tuple[int, tuple], tuple[bytes, list[tuple[int, int]]]] = {}
    for index, session in enumerate(order):
        for key, side in flows[session].items():
            segs = side.get("segs")
            if not segs:
                continue
            buf = bytearray()
            cursor = min(segs)
            gaps: list[tuple[int, int]] = []
            for seq, data in sorted(segs.items()):
                if seq < cursor:  # retransmission overlap
                    overlap = cursor - seq
                    if overlap < len(data):
                        buf += data[overlap:]
                        cursor = seq + len(data)
                elif seq == cursor:
                    buf += data
                    cursor = seq + len(data)
                else:
                    hole = seq - cursor
                    gaps.append((len(buf), hole))
                    buf += b"\0" * hole
                    buf += data
                    cursor = seq + len(data)
            out[(index, key)] = (bytes(buf), gaps)
    return out


def split_frames(buf: bytes) -> list[tuple[int, int]]:
    """Return ``[(offset, total_length), ...]`` for each frame in *buf*."""
    frames = []
    pos = 0
    while pos + 20 <= len(buf):
        _ver, _sec, _comp, length, _ck = struct.unpack_from("<5I", buf, pos)
        total = 20 + length
        if pos + total > len(buf):
            break
        frames.append((pos, total))
        pos += total
    return frames


def parse_block_chain(
    body: bytes, gaps: list[tuple[int, int]]
) -> tuple[list[tuple[int, Any]], list[int]]:
    """Walk a block chain that may contain zero-filled holes.

    Resynchronises by scanning for the next header whose ``BlocksMinusOne``
    matches the chain's, which is the one field that is constant in every block.
    """
    offset = 0
    index = 0
    blocks: list[tuple[int, Any]] = []
    damaged: list[int] = []
    expected_last: int | None = None
    while offset + 24 <= len(body):
        fields = struct.unpack_from("<6I", body, offset)
        bi, last, payload_len, uncompressed, stored, reserved = fields
        if expected_last is None and bi == 0 and 0 < payload_len and 0 < uncompressed:
            expected_last = last
        plausible = (
            expected_last is not None
            and last == expected_last
            and bi == index
            and 0 < payload_len <= 4900
            and 0 < uncompressed <= 4800
            and stored in (0, 1)
            and reserved == 0
        )
        if not plausible:
            found = None
            for probe in range(offset + 1, min(offset + 10000, len(body) - 24)):
                a, b, c, d, e, f = struct.unpack_from("<6I", body, probe)
                if (
                    b == expected_last
                    and 0 < c <= 4900
                    and 0 < d <= 4800
                    and e in (0, 1)
                    and f == 0
                    and a >= index
                ):
                    found = (probe, a)
                    break
            if found is None:
                break
            probe, a = found
            damaged.extend(range(index, a))
            offset, index = probe, a
            continue
        end = offset + 24 + payload_len
        if any(offset <= g < end or g <= offset < g + n for g, n in gaps):
            damaged.append(bi)
        blocks.append((offset, fields))
        index += 1
        offset = end
    return blocks, sorted(set(damaged))


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def build(pcap: str) -> dict[str, Any]:
    import lz4.block

    streams = reassemble(pcap)
    frames_out: list[dict[str, Any]] = []
    image_out: dict[str, Any] | None = None

    for (index, key), (buf, gaps) in sorted(streams.items(), key=lambda kv: kv[0][0]):
        src_ip, src_port, dst_ip, dst_port = key
        direction = "server->device" if src_port == 11113 else "device->server"
        for fi, (offset, total) in enumerate(split_frames(buf)):
            frame = buf[offset : offset + total]
            record: dict[str, Any] = {
                "session": index,
                "direction": direction,
                "src": f"{src_ip}:{src_port}",
                "dst": f"{dst_ip}:{dst_port}",
                "frame_index": fi,
                "length": total,
            }
            # The one frame too big to commit: the 1.84 MB image push.
            if total > 100_000:
                body = frame[20:]
                frame_gaps = [
                    (g - offset - 20, n)
                    for g, n in gaps
                    if offset + 20 <= g < offset + total
                ]
                blocks, damaged = parse_block_chain(body, frame_gaps)
                # Rebuild the plaintext, padding every block we could not
                # recover with 4800 zero bytes so that all later offsets -- and
                # therefore the second RectangleHeader -- stay correct.
                by_index = {fields[0]: (boff, fields) for boff, fields in blocks}
                total_blocks = (blocks[0][1][1] + 1) if blocks else 0
                plain = bytearray()
                for bi in range(total_blocks):
                    entry = by_index.get(bi)
                    if entry is None or bi in damaged:
                        plain += b"\0" * (
                            entry[1][3] if entry is not None else 4800
                        )
                        continue
                    boff, fields = entry
                    _bi, _last, plen, ulen, stored, _rv = fields
                    payload = body[boff + 24 : boff + 24 + plen]
                    plain += (
                        payload
                        if stored == 1
                        else lz4.block.decompress(payload, uncompressed_size=ulen)
                    )
                retained = {}
                for boff, fields in blocks:
                    if fields[0] in RETAIN_BLOCKS and fields[0] not in damaged:
                        retained[str(fields[0])] = b64(
                            body[boff : boff + 24 + fields[2]]
                        )
                record.update(
                    {
                        "kind": "image_push",
                        "protocol_header": b64(frame[:20]),
                        "block_headers": [
                            b64(struct.pack("<6I", *fields)) for _o, fields in blocks
                        ],
                        "block_offsets": [o for o, _f in blocks],
                        "retained_blocks": retained,
                        "damaged_blocks": damaged,
                        "missing_block_headers": sorted(
                            set(range(total_blocks)) - set(by_index)
                        ),
                        "gaps": frame_gaps,
                        "total_blocks": total_blocks,
                        "stored_count": sum(
                            1 for _o, f in blocks if f[4] == 1
                        ),
                        "lz4_count": sum(1 for _o, f in blocks if f[4] == 0),
                        "data_header": b64(bytes(plain[:36])),
                        "image_header": b64(bytes(plain[36:56])),
                        "rect_headers": [
                            b64(bytes(plain[56:80])),
                            b64(bytes(plain[80 + 921600 : 104 + 921600])),
                        ],
                        "plaintext_length": len(plain),
                        "checksum_verifiable": False,
                        "note": (
                            "tcpdump dropped 5 x 1460 bytes inside this frame, so "
                            "crc32(body) cannot be recomputed and 4 of 385 blocks "
                            "are unrecoverable"
                        ),
                    }
                )
                image_out = record
            else:
                record["bytes"] = b64(frame)
            frames_out.append(record)

    return {
        "meta": {
            "source": pcap,
            "device_uuid": "00112233-4455-6677-8899-aabb00000000",
            "frame_count": len(frames_out),
            "note": (
                "Real traffic from a 32\" Visionect sign, server-side capture. "
                "Every frame verbatim except the image push; see its 'note'."
            ),
        },
        "frames": frames_out,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pcap")
    parser.add_argument("out")
    args = parser.parse_args(argv)
    fixture = build(args.pcap)
    with gzip.open(args.out, "wt", encoding="utf-8", compresslevel=9) as fh:
        json.dump(fixture, fh)
    import os

    print(
        f"wrote {args.out}: {fixture['meta']['frame_count']} frames, "
        f"{os.path.getsize(args.out)} bytes"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
