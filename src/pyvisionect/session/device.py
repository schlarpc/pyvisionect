"""Per-device durable state, keyed on the UUID and nothing else.

Why the UUID and nothing else
-----------------------------

A reconnect is a **full IP-stack re-initialisation**, not a socket retry.  The
observed sequence is::

    802.2 XID -> DHCP DISCOVER (from 0.0.0.0, not a unicast renew) -> DHCP REQUEST
      -> ARP who-has <gateway> -> ARP who-has <server> -> TCP SYN :11113
      -> status with the packet ID counter reset to 1

So the source port changes, the packet counter restarts at 1, and the device may
even have a different IP.  **Never key session state on the source port or the
packet counter.**  The 16-byte UUID in the ``DataHeader`` is the only stable
identity, and the gateway treats registration as auto-create on first sight.

Two spontaneous reconnects were observed in a 22-minute window, and one of them
opened and closed carrying no protocol bytes at all, so a connection that
produces nothing is normal and must not evict the device's state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..devices.panels import Panel, panel_for
from ..packets.status import StatusPacket
from .pending import PendingWork

__all__ = [
    "CONVERGENCE_CONTACTS",
    "DEFAULT_CONTACT_INTERVAL",
    "DRAW_ALLOWANCE",
    "DeviceState",
    "DeviceStateStore",
    "RECTANGLE_UNSUPPORTED_HARDWARE",
    "SyncStatus",
]


class SyncStatus(str, Enum):
    """What :attr:`DeviceState.in_sync` means *right now*, with the clock.

    :attr:`DeviceState.in_sync` is a correct tri-state and a bad alarm.  A push
    legitimately leaves the device disagreeing with us for the whole of its
    draw-and-report cycle -- measured at 48 s on the 31.2" sign with a
    one-minute heartbeat -- so "checksums differ" raised immediately is a
    problem indicator after **every normal update**.

    This enum splits ``in_sync is False`` into the two cases that actually
    differ:

    * :attr:`CONVERGING` -- they differ, and the device has not yet had a fair
      chance to tell us otherwise.  Expected, transient, not a problem.
    * :attr:`DIVERGED` -- they differ, and it has had its chance.  The panel is
      showing something other than what we sent.

    "Had its chance" is :meth:`DeviceState.sync_status`; see there.
    """

    UNKNOWN = "unknown"
    """Never pushed, or the device has not reported a ``DisplayStateCRC``."""

    IN_SYNC = "in_sync"
    """The device echoes the checksum of our last push."""

    CONVERGING = "converging"
    """Mismatched, but still inside the window the device is allowed."""

    DIVERGED = "diverged"
    """Mismatched after the device has had its say. A real problem."""


CONVERGENCE_CONTACTS = 2
"""Status packets that must arrive after a push before a mismatch counts.

**Two, not one.**  One is tempting and wrong: a push that lands a second before
a scheduled heartbeat is acked but not yet *drawn*, so the very next status
still carries the old ``DisplayStateCRC``.  The second contact is the first one
that is unambiguously after the draw.  Measured on the 31.2" sign: ack at
6.5 s, new checksum echoed at 48 s, heartbeat interval 60 s -- so contact #1
carried it that time, and would not have if the push had been 15 s later.
"""

DRAW_ALLOWANCE = 60.0
"""Seconds added to the time-based backstop for transfer plus panel draw.

A full-screen push is ~1.84 MB to a CC3100 (acked at 3-7 s) and the panel
itself needs ~3 s of waveform.  60 s is an order of magnitude of headroom, and
the contact counter is the criterion that normally fires first.
"""

DEFAULT_CONTACT_INTERVAL = 60.0
"""Seconds assumed between contacts when the device has not announced one.

``NextStatus`` (status tag 27) is in minutes and is present on this hardware,
so this is only reached before the first status of a fresh install.
"""

RECTANGLE_UNSUPPORTED_HARDWARE: frozenset[int] = frozenset({8})
"""``HardwareNameID`` values for which the server's ``getRectangleSupport``
returns false **unconditionally** (``client.go:41``), before it even consults
``MergeRegions`` / ``ForceRectangleSupport`` / the device option.

The live 32" sign reports ``HardwareNameID == 8``, so every push to it is
full-screen.  Two further gates would stop partial updates anyway: the
interlacing step needs exactly four rectangles of exactly 1440x640 with equal
encoding, which small dirty rects can never satisfy, and ``noFullUpdateMax = 10``
caps consecutive partials regardless.
"""


@dataclass(slots=True)
class DeviceState:
    """Everything we remember about one sign across reconnects.

    Attributes:
        device_id: the raw 16 UUID bytes.
        last_status: the most recent status packet.
        imaging_state: the opaque ``EncodedFrame.state`` from the last successful
            push, to hand back to :func:`pyvisionect.imaging.encode_frame` as
            ``prev_state``.
        pushed_checksum: the ``ImageHeader.Checksum`` of our last push. Compare
            against the device's reported ``DisplayStateCRC`` (status tag 9) to
            tell whether the sign is in sync.
        connections: how many TCP connections this UUID has opened.
    """

    device_id: bytes
    last_status: StatusPacket | None = None
    pending: PendingWork = field(default_factory=PendingWork)
    """Durable, coalescing work for this device.

    Queue here rather than on a connection: the device is absent most of the
    time. See :mod:`pyvisionect.session.pending`.
    """
    imaging_state: Any = None
    pushed_checksum: int | None = None
    connections: int = 0
    pushes: int = 0
    """How many image packets we have queued for this device."""
    statuses_seen: int = 0
    """How many status packets this device has sent us, ever."""
    statuses_at_push: int | None = None
    """:attr:`statuses_seen` as it stood when the last push was queued.

    The difference is :attr:`contacts_since_push`, which is the whole of "has
    the device had a chance to report?" expressed without a clock.
    """
    last_push_at: float | None = None
    """Caller-supplied clock value of the last push, or None.

    Deliberately **not** persisted: it is whatever clock the caller passed
    (``time.monotonic`` for the bundled listener), and a monotonic value means
    nothing after a restart.  A restored state therefore falls back to the
    contact counter alone, which is the safe direction.
    """
    options: dict[str, str] = field(default_factory=dict)
    """Server-side, string-keyed device options.

    This is a *separate* namespace from the TCLV ids -- the vendor's
    ``common.Device.Options`` map.  Only ``"Secure"`` was traced end-to-end into
    the protocol; a systematic Options -> TCLV mapping was not recovered. [GAP]

    ``MergeRegions`` has the grammar ``"<bool>[,threshold=N][,thresholdStep=N]"``
    and **N is a percentage of the display diagonal, not pixels** -- 5%/10% on a
    1440x640 display is 78.8/157.6 px.
    """

    @property
    def uuid(self) -> str:
        h = self.device_id.hex()
        return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"

    @property
    def display_type(self) -> int | None:
        """The raw uint32 ``DisplayType`` the device last reported (status tag 8)."""
        return self.last_status.display_type if self.last_status else None

    @property
    def panel(self) -> Panel:
        """Modelled geometry, from the raw ``DisplayType`` with a default row."""
        return panel_for(self.display_type if self.display_type is not None else -1)

    @property
    def hardware_name_id(self) -> int | None:
        return self.last_status.hardware_name_id if self.last_status else None

    @property
    def supports_rectangles(self) -> bool:
        """Whether the **vendor stack** would ever send this device a partial.

        False for ``HardwareNameID == 8``, unconditionally. Defaults to False
        when the hardware id is not yet known, so nothing depends on partials
        before we have heard from the device.

        .. warning::
           This is a statement about ``getRectangleSupport``
           (``vss/cmd/engine/client.go:41``), **not** about the panel. The
           31.2" sign was measured accepting partial rectangles on 2026-10-05:
           it acks them, draws only the addressed region, and echoes back the
           state checksum. What it will not take is a rectangle in *canvas*
           coordinates, because those have to survive the interlaced fold; a
           rectangle addressed in *screen* coordinates bypasses the fold
           entirely. See ``OPEN-QUESTIONS.md`` A10. This property is left
           False so that nothing silently starts emitting partials, but it is
           the wrong question to ask of the hardware.
        """
        hw = self.hardware_name_id
        if hw is None:
            return False
        return hw not in RECTANGLE_UNSUPPORTED_HARDWARE

    @property
    def features(self) -> dict[str, bool]:
        """The device's own ``PV2Features`` advertisement (status tag 43).

        Capabilities come from here, never from the model.
        """
        if not self.last_status:
            return {}
        fields = self.last_status.fields()
        feats = fields.get("Features")
        if not isinstance(feats, dict):
            return {}
        return {k: v for k, v in feats.items() if not k.startswith("_")}

    @property
    def has_pushed(self) -> bool:
        """Whether we have ever pushed a frame to this device."""
        return self.pushed_checksum is not None

    @property
    def in_sync(self) -> bool | None:
        """Tri-state: in sync, out of sync, or **not known**.

        * ``True``  -- the device's reported ``DisplayStateCRC`` equals the
          ``ImageHeader.Checksum`` of our last push. It is showing what we sent.
        * ``False`` -- they differ. A push is needed.
        * ``None``  -- we have never pushed anything, or the device has not told
          us its state yet. That is a fresh install, not a problem, and callers
          should present it differently from ``False``.
        """
        if self.pushed_checksum is None or self.last_status is None:
            return None
        reported = self.last_status.display_state_crc
        if reported is None:
            return None
        return reported == self.pushed_checksum

    @property
    def contacts_since_push(self) -> int:
        """Status packets received since the last push was queued.

        ``0`` when nothing has been pushed, so a fresh device never looks like
        it has failed to converge.
        """
        if self.statuses_at_push is None:
            return 0
        return max(0, self.statuses_seen - self.statuses_at_push)

    @property
    def expected_contact_interval(self) -> float | None:
        """Seconds until the device said it would next be in touch.

        ``NextStatus`` (status tag 27) in **minutes**, converted. ``None``
        before the first status packet.  This is the device's own announcement
        and is what "a chance to report" has to be measured against: a sign on
        a one-hour heartbeat is not broken for the 59 minutes it is away.
        """
        if self.last_status is None:
            return None
        minutes = self.last_status.next_status_minutes
        if not minutes:
            return None
        return float(minutes) * 60.0

    def convergence_grace(
        self,
        *,
        contacts: int = CONVERGENCE_CONTACTS,
        interval: float | None = None,
    ) -> float:
        """Seconds a push is allowed before a mismatch is called a problem.

        ``contacts * interval + DRAW_ALLOWANCE``, where *interval* defaults to
        :attr:`expected_contact_interval` and then to
        :data:`DEFAULT_CONTACT_INTERVAL`.  It scales with the device's own
        announced schedule rather than being a flat timeout, which is the
        difference between "wait for the sign" and "wait long enough that the
        alarm is useless".
        """
        if interval is None:
            interval = self.expected_contact_interval
        if not interval or interval <= 0:
            interval = DEFAULT_CONTACT_INTERVAL
        return contacts * interval + DRAW_ALLOWANCE

    def convergence_deadline(
        self,
        *,
        contacts: int = CONVERGENCE_CONTACTS,
        interval: float | None = None,
    ) -> float | None:
        """Clock value past which a mismatch counts, or None if unknown."""
        if self.last_push_at is None:
            return None
        return self.last_push_at + self.convergence_grace(
            contacts=contacts, interval=interval
        )

    def sync_status(
        self,
        now: float | None = None,
        *,
        contacts: int = CONVERGENCE_CONTACTS,
        interval: float | None = None,
    ) -> "SyncStatus":
        """:attr:`in_sync`, but with "it has not answered yet" split out.

        The rule has two halves and they fail in opposite directions, so it is
        an **or**:

        * the device has been in touch :data:`CONVERGENCE_CONTACTS` times since
          the push and still disagrees -- it has had its say;
        * more than :meth:`convergence_grace` seconds have passed -- it has
          stopped saying anything, and a silent sign is not an excuse for a
          stale panel.

        The counter is the one that normally fires, and it needs no clock.  The
        deadline is the backstop for a device that simply stopped calling, and
        it is skipped when *now* is not given or the push predates a restart.

        Args:
            now: a clock value on the same scale as the one passed to
                :meth:`DeviceConnection.send_image`.  Omit it to use the
                contact counter alone.
            contacts: how many contacts count as "had its say".
            interval: override the device's announced contact interval, in
                seconds.
        """
        state = self.in_sync
        if state is None:
            return SyncStatus.UNKNOWN
        if state:
            return SyncStatus.IN_SYNC
        if self.contacts_since_push >= contacts:
            return SyncStatus.DIVERGED
        deadline = self.convergence_deadline(contacts=contacts, interval=interval)
        if now is not None and deadline is not None and now >= deadline:
            return SyncStatus.DIVERGED
        return SyncStatus.CONVERGING

    def note_push(self, checksum: int, *, now: float | None = None) -> None:
        """Record that *checksum* was put on the wire.

        Called for you by :meth:`DeviceConnection.send_image_packet`.
        """
        self.pushed_checksum = checksum
        self.pushes += 1
        self.statuses_at_push = self.statuses_seen
        self.last_push_at = now

    def apply_status(self, status: StatusPacket) -> None:
        self.last_status = status
        self.statuses_seen += 1

    # ------------------------------------------------------- persistence

    VERSION = 1

    def to_dict(self) -> dict[str, Any]:
        """A JSON-safe snapshot, so a consumer can persist the store wholesale.

        ``imaging_state`` is **not** included: it is two numpy planes and has
        its own, much better, binary format --
        :meth:`pyvisionect.imaging.FrameState.to_bytes`. Persist that
        separately and hand it back via :attr:`imaging_state`.
        """
        status = self.last_status
        return {
            "v": self.VERSION,
            "device_id": self.device_id.hex(),
            "pushed_checksum": self.pushed_checksum,
            "connections": self.connections,
            "pushes": self.pushes,
            "statuses_seen": self.statuses_seen,
            "statuses_at_push": self.statuses_at_push,
            "options": dict(self.options),
            "pending": self.pending.to_dict(),
            "last_status": (
                {
                    "records": [list(r) for r in status.records],
                    "sentinel": status.sentinel,
                }
                if status is not None
                else None
            ),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "DeviceState":
        """Restore from :meth:`to_dict`.

        An unknown version raises rather than silently mis-restoring.
        """
        version = raw.get("v", 1)
        if version != cls.VERSION:
            raise ValueError(
                f"DeviceState snapshot version {version} is not {cls.VERSION}; "
                "discard it and let the device re-register instead of guessing"
            )
        status_raw = raw.get("last_status")
        status = None
        if status_raw:
            status = StatusPacket(
                records=[(int(t), int(v)) for t, v in status_raw["records"]],
                sentinel=status_raw.get("sentinel"),
            )
        return cls(
            device_id=bytes.fromhex(raw["device_id"]),
            last_status=status,
            pending=PendingWork.from_dict(raw["pending"]) if raw.get("pending") else PendingWork(),
            pushed_checksum=raw.get("pushed_checksum"),
            connections=int(raw.get("connections", 0)),
            pushes=int(raw.get("pushes", 0)),
            statuses_seen=int(raw.get("statuses_seen", 0)),
            statuses_at_push=(
                None
                if raw.get("statuses_at_push") is None
                else int(raw["statuses_at_push"])
            ),
            options=dict(raw.get("options") or {}),
        )


class DeviceStateStore:
    """A ``{uuid_bytes: DeviceState}`` map. Auto-creates on first sight.

    Pure. Hold one of these for the lifetime of the listener, not per connection.
    """

    def __init__(self) -> None:
        self._states: dict[bytes, DeviceState] = {}

    def get(self, device_id: bytes) -> DeviceState:
        state = self._states.get(device_id)
        if state is None:
            state = DeviceState(device_id=device_id)
            self._states[device_id] = state
        return state

    def queue(self, device_id: bytes) -> PendingWork:
        """The durable, coalescing work register for *device_id*.

        Auto-creates, like :meth:`get`. Queue here whether or not the device is
        currently connected -- :meth:`DeviceConnection.apply_pending` drains it
        on the next connect, in the right order.
        """
        return self.get(device_id).pending

    def restore(self, state: DeviceState) -> DeviceState:
        """Insert a :class:`DeviceState` restored from :meth:`DeviceState.from_dict`."""
        self._states[state.device_id] = state
        return state

    def to_dict(self) -> dict[str, Any]:
        """Snapshot every known device, keyed on the hex UUID."""
        return {
            state.device_id.hex(): state.to_dict() for state in self._states.values()
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "DeviceStateStore":
        store = cls()
        for snapshot in raw.values():
            store.restore(DeviceState.from_dict(snapshot))
        return store

    def __contains__(self, device_id: bytes) -> bool:
        return device_id in self._states

    def __len__(self) -> int:
        return len(self._states)

    def __iter__(self):
        return iter(self._states.values())

    def known(self) -> list[DeviceState]:
        return list(self._states.values())
