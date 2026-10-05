"""The sans-io device connection state machine.

Contract
--------

::

    conn = DeviceConnection(store=DeviceStateStore())
    events = conn.feed(data)       # bytes in  -> list[Event]
    out    = conn.drain()          # -> bytes the caller must send
    events = conn.advance(now)     # timers, with the clock passed in
    conn.send_image(frame)         # queues; no I/O

Nothing in this module (or anywhere under ``wire/``, ``packets/``, ``session/``,
``devices/``) imports ``socket`` or ``asyncio``, or calls ``time.time()``.  There
is a test that enforces that by AST-scanning the package.

We are the server
-----------------

The sign is a client.  It dials out to TCP 11113 and speaks first; there is no
handshake, no auth and no negotiation.  So this object is driven by whatever the
device sends, and the only thing it must do unprompted is ack.

Timers
------

Two independent ones, plus a read timeout:

* **Device heartbeat** -- device-driven, interval = TCLV parameter 29, and the
  device announces ``NextStatus`` (status tag 27, in **minutes**) in every
  status.  We only watch it; :class:`~pyvisionect.session.events.HeartbeatOverdue`
  is advisory.
* **Server inactivity watchdog** -- ``DeviceStatePolling`` minutes plus
  ``rand(0..10 s)`` of jitter, falling back to 900 s when the config value is 0
  (``pv3.go:1122-1131``).  Reset on every received **status** packet, not on any
  packet.  On expiry the gateway sends ``Command{Type: 11}`` ("status request")
  and closes the connection if that request fails.
  Firmware revisions **at or below 269** get no watchdog at all
  (``pv3.go:1049``); the live sign reports 7.4.4407, so it does.
* **Read timeout** -- ``PacketTimeout``, config default **300 s**
  (``Configuration`` offset 192, ``cfg.go:239-240``).
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Sequence

from ..devices.enums import CommandType, ControlFlags, PacketType
from ..packets import (
    FILE_LIST_LENGTH,
    FILE_LIST_PATH,
    ButtonPacket,
    CborPacket,
    CommandPacket,
    ControlPacket,
    FilePacket,
    GpsPacket,
    ImagePacket,
    ParamPacket,
    Rectangle,
    StatusPacket,
    TouchPacket,
    decode_payload,
    screen_cache_name,
)
from ..devices.enums import FileMode
from ..wire import (
    Compression,
    DataHeader,
    Direction,
    Frame,
    FrameDecoder,
    ProtocolError,
    encode_frame,
    lz4_compress_block,
)
from ..wire.errors import CommandPacketsDisabled
from . import events as ev
from .device import DeviceState, DeviceStateStore
from .pending import (
    PENDING_ORDER,
    SLOT_FLASH_SAVE,
    SLOT_GHOST_CLEAR,
    SLOT_IMAGE,
    SLOT_PARAM_READS,
    SLOT_PARAM_WRITES,
    SLOT_REBOOT,
    SLOT_SLEEP,
    TCLV_CMD_FLASH_SAVE,
    PendingWork,
)

__all__ = [
    "DEFAULT_PACKET_TIMEOUT",
    "DEFAULT_DEVICE_STATE_POLLING",
    "WATCHDOG_FALLBACK",
    "WATCHDOG_JITTER_MAX",
    "MIN_WATCHDOG_FIRMWARE_REVISION",
    "ConnectionConfig",
    "DeviceConnection",
    "SLEEP_MIN_MINUTES",
]

SLEEP_MIN_MINUTES = 1
"""``stdcmd.Sleep.Done`` rejects a zero duration with ``errSleepTime``."""

DEFAULT_PACKET_TIMEOUT = 300.0
"""``Global.PacketTimeout``, template default 300 s (``Configuration`` offset 192)."""

DEFAULT_DEVICE_STATE_POLLING = 15
"""``Global.DeviceStatePolling``, in **minutes**; template default 15
(``main.go:382-383`` ``MOVQ $0xf, Config+232``)."""

WATCHDOG_FALLBACK = 900.0
"""The hardcoded fallback when ``DeviceStatePolling == 0``: ``0xd18c2e2800`` ns."""

WATCHDOG_JITTER_MAX = 10.0
"""``rand.Intn(10000)`` milliseconds of jitter (``pv3.go:1129``/``:1131``)."""

MIN_WATCHDOG_FIRMWARE_REVISION = 269
"""``pv3.go:1049``: ``if fwVersion.Revision <= 269 { return nil }`` -- no watchdog."""


@dataclass(slots=True)
class ConnectionConfig:
    """Knobs that map 1:1 onto the vendor's ``common.Configuration`` fields."""

    packet_timeout: float = DEFAULT_PACKET_TIMEOUT
    device_state_polling_minutes: int = DEFAULT_DEVICE_STATE_POLLING
    strict: bool = False
    """Verify the inbound checksum and block-chain invariants. Off by default;
    the real gateway validates neither."""
    compression: int = Compression.LZ4
    """What the gateway always does, even for an 8-byte ack.

    ``Compression.NONE`` on the **outbound** path is unverified: the gateway was
    never observed sending an uncompressed frame to a device, so whether the
    firmware accepts one is unknown. To drop the LZ4 dependency, keep
    ``Compression.LZ4`` here and set :attr:`compressor` to
    :data:`~pyvisionect.wire.STORED_ONLY` instead -- that produces an ordinary
    block chain with every block ``Stored=1``, which is a code path the device
    demonstrably handles (60 of the captured push's 385 blocks were stored).
    """

    compressor: Callable[[bytes], bytes] = lz4_compress_block
    """Per-block compressor.

    :data:`~pyvisionect.wire.STORED_ONLY` emits every block uncompressed and
    needs no ``lz4`` install. The captured push shows LZ4 buying almost nothing
    on dithered halftone data (mean ratio 0.956), so the trade is roughly 4%
    more bytes for zero C extensions.
    """

    allow_command_packets: bool = True
    """Whether ``packet.Type 2`` (command) may be sent at all.

    Set ``False`` for a "safe mode" that guarantees none are emitted. **Type 2
    has never been observed on the wire** -- not in any direction, in any
    capture -- so every command this library can send is code-read only. See the
    warning at the top of :mod:`pyvisionect.packets.misc` for why that is worth
    being careful about.
    """
    ack_handled_packets: bool = True
    """Ack every packet we handle, as every vendor handler does."""
    require_first_status: bool = True
    """``main.ErrNotFirstStatusPacket``: the first packet of a connection must be
    a status packet."""
    watchdog_enabled: bool = True


def _random_server_id() -> int:
    """Server-originated packet IDs are a random uint32.

    ``Conn.SetSecurity`` uses ``math/rand/v2`` and takes the high 32 bits
    (``rand.go:274`` then ``SHRQ $0x20``).  Captured examples: ``0x3FA3A4F1``,
    ``0x691DED39``, ``0xD35B7CC1``.
    """
    return random.getrandbits(32)


def _random_jitter() -> float:
    return random.uniform(0.0, WATCHDOG_JITTER_MAX)


def _perturb_checksum(checksum: int) -> int:
    """Return a value that cannot equal *checksum*, for the full-screen lever.

    The device stores the ``ImageHeader.Checksum`` of the last frame it applied
    and reports it back as ``DisplayStateCRC``. Sending a tag it cannot already
    hold is the verified way to guarantee the frame is treated as new. ``0`` is
    avoided because the vendor reserves it for "unknown" (it remaps a computed
    0 to 1).
    """
    out = (checksum ^ 0x8000_0000) & 0xFFFFFFFF
    return out or 1


class DeviceConnection:
    """One TCP connection to one sign. Pure.

    Args:
        store: the per-UUID durable state map. Share one across connections so a
            reconnect keeps its state. A private one is created if omitted.
        config: timer and strictness knobs.
        id_source: callable returning the next server-originated packet ID.
            Injectable for deterministic tests.
        jitter: callable returning the watchdog's 0..10 s jitter. Injectable.
    """

    def __init__(
        self,
        *,
        store: DeviceStateStore | None = None,
        config: ConnectionConfig | None = None,
        id_source: Callable[[], int] = _random_server_id,
        jitter: Callable[[], float] = _random_jitter,
    ) -> None:
        self.store = store if store is not None else DeviceStateStore()
        self.config = config or ConnectionConfig()
        self._id_source = id_source
        self._jitter = jitter

        self._decoder = FrameDecoder(
            Direction.DEVICE_TO_SERVER, strict=self.config.strict
        )
        self._out = bytearray()
        self._device_id: bytes | None = None
        self._state: DeviceState | None = None
        self._seen_first_packet = False
        self._pending: dict[int, str] = {}
        """Outstanding server-originated request IDs -> a short description."""

        self._last_status_at: float | None = None
        self._last_frame_at: float | None = None
        self._watchdog_deadline: float | None = None
        self._read_deadline: float | None = None
        self._heartbeat_deadline: float | None = None
        self._closed = False

    # ------------------------------------------------------------ identity

    @property
    def device_id(self) -> bytes | None:
        """The 16 UUID bytes, once the first status packet has arrived."""
        return self._device_id

    @property
    def state(self) -> DeviceState | None:
        """The durable per-UUID state, once the device has identified itself."""
        return self._state

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def pending_requests(self) -> dict[int, str]:
        return dict(self._pending)

    # ------------------------------------------------------------- sans-io

    def feed(self, data: bytes) -> list[ev.Event]:
        """Consume *data* and return the resulting events.

        Does not raise on a protocol violation: it emits
        :class:`~pyvisionect.session.events.ProtocolViolation` and marks the
        connection closed, because the caller owns the socket.
        """
        out: list[ev.Event] = []
        try:
            frames = self._decoder.feed(data)
        except ProtocolError as exc:
            self._closed = True
            return [
                ev.ProtocolViolation(
                    reason=str(exc), error=exc, device_id=self._device_id or b""
                )
            ]
        for frame in frames:
            out.extend(self._handle_frame(frame))
        return out

    def drain(self) -> bytes:
        """Return and clear the bytes queued for the wire."""
        data = bytes(self._out)
        self._out.clear()
        return data

    @property
    def pending_bytes(self) -> int:
        return len(self._out)

    def advance(self, now: float) -> list[ev.Event]:
        """Run the timers against the caller's clock.

        *now* is a monotonic-ish float in seconds. The library never reads a
        clock itself; this is the only way time enters it.
        """
        out: list[ev.Event] = []
        if self._last_frame_at is None:
            # Arm the deadlines on the first advance() so a caller that never
            # calls feed() still gets a read timeout.
            self._last_frame_at = now
            self._read_deadline = now + self.config.packet_timeout

        if self._read_deadline is not None and now >= self._read_deadline:
            quiet = now - (self._last_frame_at or now)
            self._read_deadline = None
            out.append(ev.ReadTimeout(quiet_for=quiet, device_id=self._device_id or b""))

        if (
            self._heartbeat_deadline is not None
            and now >= self._heartbeat_deadline
            and self._last_status_at is not None
        ):
            expected = self._heartbeat_deadline - self._last_status_at
            self._heartbeat_deadline = None
            out.append(
                ev.HeartbeatOverdue(
                    expected_within=expected,
                    quiet_for=now - self._last_status_at,
                    device_id=self._device_id or b"",
                )
            )

        if self._watchdog_deadline is not None and now >= self._watchdog_deadline:
            quiet = now - (self._last_status_at or now)
            self._arm_watchdog(now)
            self.request_status()
            out.append(
                ev.WatchdogFired(quiet_for=quiet, device_id=self._device_id or b"")
            )

        return out

    def next_deadline(self) -> float | None:
        """The earliest armed deadline, so an event loop can sleep until it."""
        candidates = [
            d
            for d in (
                self._read_deadline,
                self._watchdog_deadline,
                self._heartbeat_deadline,
            )
            if d is not None
        ]
        return min(candidates) if candidates else None

    # ----------------------------------------------------------- queueing

    def send(self, packet_type: int, payload: bytes, *, packet_id: int | None = None,
             description: str = "") -> int:
        """Queue one frame and return its packet ID.

        Raises:
            RuntimeError: if the device has not identified itself yet. We cannot
                address a frame without its UUID.
        """
        if self._device_id is None:
            raise RuntimeError(
                "the device has not sent its first status packet yet, so its UUID "
                "is unknown and no frame can be addressed to it"
            )
        pid = self._id_source() if packet_id is None else packet_id
        header = DataHeader(
            device_id=self._device_id, type=packet_type, id=pid, length=len(payload)
        )
        self._out += encode_frame(
            header,
            payload,
            direction=Direction.SERVER_TO_DEVICE,
            compression=self.config.compression,
            compressor=self.config.compressor,
        )
        if description:
            self._pending[pid] = description
        return pid

    # -- control ---------------------------------------------------------

    def write_control(self, packet_id: int, flags: int) -> None:
        """``main.(*pv3Connection).WriteControl``.

        ``packet_id == 0xFFFFFFFF`` is a no-op, exactly as ``pv3.go:512``.
        """
        if packet_id == 0xFFFFFFFF:
            return
        self.send(PacketType.CONTROL, ControlPacket(flags=flags).encode(),
                  packet_id=packet_id)

    def ack(self, packet_id: int) -> None:
        """``Control{Flags: 1}`` echoing *packet_id*."""
        self.write_control(packet_id, ControlFlags.ACK)

    def nack(self, packet_id: int) -> None:
        """``Control{Flags: 0}`` -- what the dispatcher sends for an unknown type."""
        self.write_control(packet_id, 0)

    # -- commands --------------------------------------------------------

    def send_command(self, command_type: int, payload: bytes = b"") -> int:
        """Queue a ``packet.Command`` (type 2, server to device only).

        .. warning:: **[UNVERIFIED -- all of packet type 2]**
           Type 2 has never been seen on the wire in either direction. The
           layout here is read out of the Go struct, and there is direct
           precedent for the struct and the wire format disagreeing: the param
           packet's struct is 12 bytes and its wire header is **8**. So the
           12-byte ``CommandHeader`` this emits may be wrong. See
           :mod:`pyvisionect.packets.misc`.

        Raises:
            CommandPacketsDisabled: if
                ``ConnectionConfig(allow_command_packets=False)``.
        """
        if not self.config.allow_command_packets:
            raise CommandPacketsDisabled(
                f"refusing to send packet type 2 (command "
                f"{CommandType.NAMES.get(command_type, command_type)}): "
                "ConnectionConfig(allow_command_packets=False). Type 2 has "
                "never been observed on the wire, so this is an opt-in."
            )
        name = CommandType.NAMES.get(command_type, f"command({command_type})")
        return self.send(
            PacketType.COMMAND,
            CommandPacket(type=command_type, payload=payload).encode(),
            description=name,
        )

    def request_status(self) -> int:
        """``CommandType 11`` -- what the watchdog sends on expiry.

        The one type-2 command with a traced emission site in the vendor server
        (``(*ping).resumePing.func1.1``, ``pv3.go:1087-1088``), so of the
        commands here it is the best attested -- though still never captured.
        """
        return self.send_command(CommandType.STATUS_REQUEST)

    def reboot(self) -> int:
        """``CommandType 0``.

        .. warning:: Unverified, like all of type 2. Worth noting that a reboot
           *is* observable after the fact: the device reconnects with
           ``ConnectReason == 1`` (reboot), which
           :attr:`~pyvisionect.packets.StatusPacket.connect_reason_name`
           reports. That is the cheapest confirmation available that the command
           landed.
        """
        return self.send_command(CommandType.REBOOT)

    def refresh(self) -> int:
        """``CommandType -1`` ("refresh").

        .. warning:: **No emission site for this id exists anywhere in the
           vendor server.** It is an entry in ``packet.CommandType.String``'s
           jump table and nothing more -- nothing in the suite ever sends it.
           Prefer the verified route: re-encode with
           ``encode_frame(force_full_screen=True)`` and push. See
           :meth:`send_image`, which documents both full-screen levers.
        """
        return self.send_command(CommandType.REFRESH)

    def clear_screen(self) -> int:
        """``CommandType -2`` ("clear screen").

        .. warning:: Like :meth:`refresh`, an enum entry with **no emission
           site in the vendor server**. The verified way to blank the panel is
           to push a white full-screen frame.
        """
        return self.send_command(CommandType.CLEAR_SCREEN)

    def sleep(self, minutes: int) -> int:
        """``CommandType 4`` -- deep-sleep for *minutes*.

        The payload framing **is** resolved: ``stdcmd.Sleep.Done``
        (``vss/pkg/command/stdcmd/sleep.go:30-38``) builds ``CommandID = 4``
        with a payload of ``proto.Marshall(uint32 durationMinutes)``, and
        rejects ``duration == 0`` with ``errSleepTime``. So: one little-endian
        uint32, in minutes, non-zero.

        **Order this last-but-one.** The device leaves when it gets this, so the
        image bytes have to be on the wire first. :meth:`apply_pending` enforces
        that ordering; if you queue by hand, do the same.

        Args:
            minutes: a positive number of minutes.

        Raises:
            ValueError: for ``minutes <= 0``, matching ``errSleepTime``.
        """
        if int(minutes) < SLEEP_MIN_MINUTES:
            raise ValueError(
                f"sleep duration must be at least {SLEEP_MIN_MINUTES} minute, "
                f"got {minutes!r}; the vendor rejects 0 with errSleepTime"
            )
        return self.send_command(
            CommandType.SLEEP, int(minutes).to_bytes(4, "little")
        )

    def set_heartbeat(self, minutes: int) -> int:
        """``CommandType 7``. The same setting as TCLV parameter 29.

        .. warning:: Unverified, like all of type 2. Writing TCLV 29 with
           :meth:`write_params` is the attested route -- the param channel was
           captured end to end.
        """
        return self.send_command(
            CommandType.SET_HEARTBEAT, int(minutes).to_bytes(4, "little")
        )

    # -- params ----------------------------------------------------------

    def read_params(self, ids: Sequence[int]) -> int:
        """Queue a TCLV read for *ids*."""
        return self.send(
            PacketType.PARAM,
            ParamPacket.read(list(ids)).encode(),
            description=f"param read {list(ids)}",
        )

    def write_params(
        self, values: dict[int, int | str | bytes], *, allow_read_only: bool = False
    ) -> int:
        """Queue a TCLV write.

        Raises:
            ReadOnlyParameter: for any of the eight network-read-only ids, with
                the USB command that can set it instead.
        """
        packet = ParamPacket.write(values, allow_read_only=allow_read_only)
        return self.send(
            PacketType.PARAM,
            packet.encode(),
            description=f"param write {sorted(values)}",
        )

    # -- files -----------------------------------------------------------

    def file_open(self, filename: str, mode: int = FileMode.READ) -> int:
        return self.send(
            PacketType.FILE,
            FilePacket.open(filename, mode).encode(),
            description=f"file open {filename!r}",
        )

    def file_close(self) -> int:
        return self.send(
            PacketType.FILE, FilePacket.close().encode(), description="file close"
        )

    def file_read(self, length: int) -> int:
        return self.send(
            PacketType.FILE,
            FilePacket.read(length).encode(),
            description=f"file read {length}",
        )

    def file_write(self, data: bytes) -> int:
        return self.send(
            PacketType.FILE,
            FilePacket.write(data).encode(),
            description=f"file write {len(data)}B",
        )

    def file_erase(self, filename: str) -> int:
        return self.send(
            PacketType.FILE,
            FilePacket.erase(filename).encode(),
            description=f"file erase {filename!r}",
        )

    def file_list(self) -> int:
        """List the device's filesystem root.

        **There is no ``list`` opcode.** ``packet.FileOperation`` is
        ``0 open, 1 close, 2 read, 3 write, 4 erase, 5 write-descriptor,
        6 event`` -- and the vendor's directory listing is a plain
        ``Read(".", 10240)``: a path of ``"/"`` or ``""`` is rewritten to the
        one-byte string ``"."``. The reply is an ASCII blob that ``vio.GetFiles``
        splits on newline, then on space, into ``{name: {size, checksum}}``.
        :func:`~pyvisionect.packets.parse_file_listing` does that parsing for
        you. This method queues only the ``open``; once it is acked, follow with
        ``read_file_range(FILE_LIST_LENGTH)`` and parse the reply.

        Note the listing's ``checksum`` column reads ``"0"`` for every file on
        this firmware, so it identifies nothing. To tell *which* cached frame is
        which, read the first ~256 bytes of each ``.pv2`` and match the stored
        ``ImageHeader.Checksum`` against :attr:`DeviceState.pushed_checksum` --
        a value we generated ourselves, so it is a match rather than a guess.
        """
        return self.send(
            PacketType.FILE,
            FilePacket.open(FILE_LIST_PATH, FileMode.READ).encode(),
            description=f"file list {FILE_LIST_PATH!r}",
        )

    def read_file_range(self, length: int) -> int:
        """Read at most *length* bytes from the open file.

        A short read is the point: identifying a cached frame needs ~256 bytes
        (``ProtocolHeader`` -> block 0 -> ``DataHeader`` + ``ImageHeader`` +
        both ``RectangleHeader``s all live in the first block), not the whole
        file. Six 256-byte probes instead of a 3.79 MB sweep, on a link that
        exists for minutes a day.
        """
        return self.file_read(length)

    def open_screen_cache(self, screen_id: int) -> int:
        """Open ``@screen_<N>`` for reading.

        .. note::
           This is the vendor's **live-view** path, not part of the image-update
           path. The only reference to ``"@screen_2"`` in the whole gateway
           binary is a hardcoded literal inside
           ``main.(*grpcHandlers).handleLiveView`` (``grpc_handlers.go:240``) --
           not even a ``@screen_%d`` format. The 32" sign NACKs it with
           ``0x00008a00`` because this firmware does not implement that
           filename; the admin UI only offers the button for
           ``HardwareNameID`` in {11, 13, 24, 34, 42}. It says nothing about
           update strategy.
        """
        return self.file_open(screen_cache_name(screen_id), FileMode.READ)

    # -- images ----------------------------------------------------------

    def send_image(
        self,
        frame: Any,
        *,
        options: int = 0,
        force_full: bool = False,
        checksum_override: int | None = None,
        now: float | None = None,
    ) -> int:
        """Queue an image push from an :class:`~pyvisionect.imaging.EncodedFrame`.

        Accepts any object with ``.rectangles`` (each having ``x``, ``y``, ``w``,
        ``h``, ``screen_id``, ``options``, ``data``) and ``.state_checksum`` --
        i.e. the imaging module's output, without importing it.

        The frame's ``state_checksum`` becomes ``ImageHeader.Checksum``, which is
        what the device echoes back as ``DisplayStateCRC``, and
        ``frame.state`` is stashed in the device's durable state for the next
        call's ``prev_state``.

        **This is CPU-cheap but memory-heavy and byte-heavy.** A full-screen push
        to the 32" panel is ~1.84 MB. Producing *frame* is the expensive part and
        must not happen on an event loop; see :func:`pyvisionect.imaging.encode_frame`.

        Forcing a full-screen redraw
        ----------------------------

        There are two levers, and they are not equivalent:

        * ``encode_frame(..., force_full_screen=True)`` -- the **verified** one.
          It skips change detection and emits rectangles covering every display,
          so the device repaints everything because it was sent everything.
        * ``force_full=True`` here -- the *checksum* lever. It perturbs
          ``ImageHeader.Checksum`` so it cannot match the tag the device
          currently holds. Verified end to end that the device never refuses a
          mismatched tag, and that a mismatch costs exactly one extra
          full-screen redraw. It does **not** change which pixels are sent, so
          on a partial frame it is not a full repaint on its own.

        On ``HardwareNameID == 8`` -- the 32" sign -- ``getRectangleSupport``
        returns false unconditionally and every push is full-screen anyway, so
        ``force_full`` is a no-op there. :attr:`DeviceState.supports_rectangles`
        tells you which case you are in.

        Args:
            options: ``ImageHeader.Options``.
            force_full: perturb the checksum so the device cannot consider
                itself in sync. Mutually exclusive with *checksum_override*.
            checksum_override: send this exact ``ImageHeader.Checksum`` instead
                of the frame's. For replaying a captured push, or for driving
                the mismatch lever by hand.
            now: a clock value, recorded against the push so that
                :meth:`DeviceState.sync_status` can tell "has not answered
                yet" from "is showing the wrong thing". Optional, like
                everywhere else in this library; without it the verdict falls
                back to counting the device's contacts, which needs no clock.
        """
        if force_full and checksum_override is not None:
            raise ValueError(
                "pass force_full or checksum_override, not both: they both set "
                "ImageHeader.Checksum"
            )
        rects = [
            Rectangle(
                x=r.x,
                y=r.y,
                width=r.w,
                height=r.h,
                data=r.data,
                screen_id=r.screen_id,
                encoding=getattr(r, "encoding", 4),
                options=getattr(r, "options", 0),
            )
            for r in frame.rectangles
        ]
        checksum = frame.state_checksum
        if checksum_override is not None:
            checksum = checksum_override & 0xFFFFFFFF
        elif force_full:
            checksum = _perturb_checksum(checksum)
        return self.send_image_packet(
            ImagePacket(checksum=checksum, rectangles=rects, options=options),
            imaging_state=getattr(frame, "state", None),
            now=now,
        )

    def send_image_packet(
        self,
        packet: ImagePacket,
        *,
        imaging_state: Any = None,
        now: float | None = None,
    ) -> int:
        """Queue an already-built :class:`~pyvisionect.packets.ImagePacket`."""
        pid = self.send(
            PacketType.IMAGE,
            packet.encode(),
            description=f"image push ({len(packet.rectangles)} rects)",
        )
        if self._state is not None:
            self._state.note_push(packet.checksum, now=now)
            if imaging_state is not None:
                self._state.imaging_state = imaging_state
        return pid

    # ------------------------------------------------------- pending work

    def apply_pending(
        self,
        work: PendingWork | None = None,
        *,
        now: float | None = None,
        phases: Sequence[str] | None = None,
    ) -> dict[str, int]:
        """Put this device's deferred work on the wire, in the vendor's order.

        Call this once per connection, right after the first status packet --
        i.e. on a :class:`~pyvisionect.session.events.DeviceConnected` event.

        The order is :data:`~pyvisionect.session.pending.PENDING_ORDER` and it
        is not arbitrary. In particular **sleep is queued after the image
        bytes** and **reboot is last**: the device leaves when it is told to
        sleep, and nothing survives a reboot. The vendor waits for its renderer
        before publishing its sleep packet for exactly this reason; get it wrong
        and the user stares at yesterday's dashboard for a day. Because
        :meth:`drain` returns bytes in queue order and TCP preserves order,
        queueing sleep after the image *is* "the image lands first".

        Each slot is recorded as in-flight and clears only when the device acks
        it (feed the library's ``Acked``/``Nacked`` events to
        :meth:`PendingWork.on_acked` / :meth:`PendingWork.on_nacked`), so a
        connection that drops mid-reconcile leaves the remainder queued.

        Args:
            work: the register to drain. Defaults to this device's own
                (``conn.state.pending``).
            now: a clock value, recorded against each slot. Optional; the
                library never reads a clock itself.
            phases: restrict to these slots, for a caller that wants to await an
                ack between phases. Order is still
                :data:`PENDING_ORDER`, whatever order you pass.

        Returns:
            ``{slot: packet_id}`` for the work queued by this call.

        Raises:
            RuntimeError: if the device has not identified itself yet.
        """
        if work is None:
            if self._state is None:
                raise RuntimeError(
                    "no device state yet; apply_pending needs the device's first "
                    "status packet, or an explicit work= argument"
                )
            work = self._state.pending

        wanted = set(phases) if phases is not None else None
        sent: dict[str, int] = {}

        for slot in work.slots():
            if wanted is not None and slot not in wanted:
                continue
            packet_id = self._apply_slot(slot, work, now=now)
            if packet_id is None:
                continue
            sent[slot] = packet_id
            work.note_sent(slot, packet_id, now=now)
        return sent

    def _apply_slot(
        self, slot: str, work: PendingWork, *, now: float | None = None
    ) -> int | None:
        if slot == SLOT_PARAM_READS:
            return self.read_params(sorted(work.param_reads))
        if slot == SLOT_PARAM_WRITES:
            return self.write_params(dict(work.params))
        if slot == SLOT_FLASH_SAVE:
            # TCLV 53 is a command-shaped parameter: writing it persists
            # everything above. Without it every write is RAM-only.
            return self.write_params({TCLV_CMD_FLASH_SAVE: 1})
        if slot == "framebuffer_read":
            return self.file_list()
        if slot == SLOT_IMAGE:
            return self.send_image(work.frame, force_full=work.force_push, now=now)
        if slot == SLOT_GHOST_CLEAR:
            # An inverse / anti-ghosting pass is a second full-screen push of
            # the same content; the caller supplies the inverse-encoded frame by
            # re-encoding with inverse=True. With only the normal frame in hand
            # the honest thing is to repeat it with the checksum lever set.
            return (
                self.send_image(work.frame, force_full=True, now=now)
                if work.frame
                else None
            )
        if slot == SLOT_SLEEP:
            assert work.sleep_minutes is not None
            return self.sleep(work.sleep_minutes)
        if slot == SLOT_REBOOT:
            return self.reboot()
        raise ValueError(f"unknown slot {slot!r}")

    # ------------------------------------------------------------ handling

    def _handle_frame(self, frame: Frame) -> list[ev.Event]:
        out: list[ev.Event] = []
        first = not self._seen_first_packet
        self._seen_first_packet = True

        if first and self.config.require_first_status:
            if frame.type != PacketType.STATUS:
                self._closed = True
                return [
                    ev.ProtocolViolation(
                        reason="first packet is not a status packet",
                        device_id=frame.device_id,
                    )
                ]

        if self._device_id is None:
            self._device_id = frame.device_id
            self._state = self.store.get(frame.device_id)
            self._state.connections += 1
        elif frame.device_id != self._device_id:
            # One connection carries exactly one UUID in practice. Treat a change
            # as a violation rather than silently re-keying.
            self._closed = True
            return [
                ev.ProtocolViolation(
                    reason=(
                        f"DeviceID changed mid-connection: "
                        f"{self._device_id.hex()} -> {frame.device_id.hex()}"
                    ),
                    device_id=self._device_id,
                )
            ]

        try:
            payload = decode_payload(frame.type, frame.payload)
        except ProtocolError as exc:
            out.append(
                ev.ProtocolViolation(
                    reason=f"type {frame.type} payload: {exc}",
                    error=exc,
                    device_id=frame.device_id,
                )
            )
            self.nack(frame.id)
            return out

        kwargs = {"device_id": frame.device_id, "packet_id": frame.id}

        if frame.type == PacketType.CONTROL:
            out.extend(self._handle_control(frame, payload))
            return out

        if payload is None:
            # No handler: NACK, exactly as the dispatcher does at pv3.go:473.
            self.nack(frame.id)
            out.append(
                ev.UnknownPacketType(
                    packet_type=frame.type, payload=frame.payload, **kwargs
                )
            )
            return out

        if frame.type == PacketType.STATUS:
            assert isinstance(payload, StatusPacket)
            if self._state is not None:
                self._state.apply_status(payload)
            if first:
                out.append(
                    ev.DeviceConnected(
                        status=payload,
                        connect_reason=payload.connect_reason,
                        **kwargs,
                    )
                )
            out.append(ev.StatusReceived(status=payload, **kwargs))
        elif frame.type == PacketType.PARAM:
            assert isinstance(payload, ParamPacket)
            out.append(ev.ParamsReceived(params=payload, **kwargs))
        elif frame.type == PacketType.FILE:
            assert isinstance(payload, FilePacket)
            out.append(ev.FileEventReceived(file=payload, **kwargs))
        elif frame.type == PacketType.TOUCH:
            assert isinstance(payload, TouchPacket)
            out.append(ev.TouchReceived(touch=payload, **kwargs))
        elif frame.type == PacketType.BUTTON:
            assert isinstance(payload, ButtonPacket)
            out.append(ev.ButtonReceived(button=payload, **kwargs))
        elif frame.type == PacketType.GPS:
            assert isinstance(payload, GpsPacket)
            out.append(ev.GpsReceived(gps=payload, **kwargs))
        elif frame.type == PacketType.CBOR:
            assert isinstance(payload, CborPacket)
            out.append(ev.CborReceived(cbor=payload, **kwargs))
        else:
            # A type we can decode but do not route (e.g. command/image echoed
            # back). Ack it; do not pretend to understand it.
            out.append(
                ev.UnknownPacketType(
                    packet_type=frame.type, payload=frame.payload, **kwargs
                )
            )

        if self.config.ack_handled_packets:
            self.ack(frame.id)
        return out

    def _handle_control(self, frame: Frame, control: Any) -> list[ev.Event]:
        """``main.(*pv3Connection).handlePacketControl`` (``pv3.go:485-506``).

        Note we do *not* ack a control packet -- that would loop forever.
        """
        assert isinstance(control, ControlPacket)
        self._pending.pop(frame.id, None)
        if control.is_ack:
            return [
                ev.Acked(packet_id=frame.id, control=control, device_id=frame.device_id)
            ]
        return [
            ev.Nacked(
                packet_id=frame.id,
                control=control,
                charging=control.nack_charging,
                error_code=control.error_code,
                device_id=frame.device_id,
            )
        ]

    # ------------------------------------------------------------- timers

    def note_frame(self, now: float) -> None:
        """Record that a frame arrived at *now* (for the read timeout).

        Called for you by :meth:`feed_at`; exposed separately so a caller that
        already knows its own clock can drive the timers explicitly.
        """
        self._last_frame_at = now
        self._read_deadline = now + self.config.packet_timeout

    def feed_at(self, data: bytes, now: float) -> list[ev.Event]:
        """:meth:`feed` plus timer bookkeeping against *now*.

        This is what an io adapter should call.
        """
        self.note_frame(now)
        out = self.feed(data)
        if any(isinstance(e, (ev.StatusReceived, ev.DeviceConnected)) for e in out):
            self._note_status(now)
        return out

    def _note_status(self, now: float) -> None:
        self._last_status_at = now
        self._arm_watchdog(now)
        self._arm_heartbeat(now)

    def _watchdog_enabled_for_device(self) -> bool:
        """``pv3.go:1046-1049``: no version -> no ping; revision <= 269 -> no ping."""
        if not self.config.watchdog_enabled:
            return False
        if self._state is None or self._state.last_status is None:
            return False
        version = self._state.last_status.firmware_version
        if version is None:
            return False
        return version[2] > MIN_WATCHDOG_FIRMWARE_REVISION

    def watchdog_timeout(self) -> float:
        """``main.(*ping).timeoutDuration`` (``pv3.go:1122-1131``)."""
        minutes = self.config.device_state_polling_minutes
        base = WATCHDOG_FALLBACK if minutes == 0 else minutes * 60.0
        return base + self._jitter()

    def _arm_watchdog(self, now: float) -> None:
        if self._watchdog_enabled_for_device():
            self._watchdog_deadline = now + self.watchdog_timeout()
        else:
            self._watchdog_deadline = None

    def _arm_heartbeat(self, now: float) -> None:
        """Arm the advisory heartbeat deadline from status tag 27 (minutes)."""
        if self._state is None or self._state.last_status is None:
            self._heartbeat_deadline = None
            return
        next_status = self._state.last_status.next_status_minutes
        if not next_status:
            self._heartbeat_deadline = None
            return
        # Allow a generous grace period: the observed cadence is 60.0 s with
        # NextStatus == 1, so the unit is minutes and one extra minute of slack
        # costs nothing.
        self._heartbeat_deadline = now + next_status * 60.0 + 60.0
