"""Durable, coalescing per-device work, applied in the vendor's order on connect.

Why this is in the library
--------------------------

:meth:`DeviceConnection.send_image` and friends queue onto a **connection**, and
a sign is absent most of the time -- a battery device wakes for a minute a day.
So ``connection_for(uuid)`` returns ``None`` in the common case, and every
consumer would otherwise build its own deferral layer.  Worse, the *coalescing*
rules are correctness, not style: a buffer that grows while the device sleeps is
a bug, and pushing ten images to a sleeping sign must show the tenth, not replay
all ten.

So the model here is a **desired-state register**, not a queue.  Depth for
content is exactly one.

Coalescing rules
----------------

===================  ==========================================  ===============================
work                 rule                                        why
===================  ==========================================  ===============================
content              revision counter, **last-write-wins**       ten pushes to a sleeping sign
                                                                  must show the tenth
TCLV write           per-id last-write-wins                      heartbeat 5 then 30 means 30
TCLV read            set union                                   reads are free and idempotent
ghost clear, reboot  boolean, set semantics                      two reboots is one reboot
sleep                **replaces** any prior sleep                two sleeps is a logic error
framebuffer read     single slot, later replaces earlier         a diagnostic, not a log
===================  ==========================================  ===============================

Ordering on connect
-------------------

:data:`PENDING_ORDER` is not arbitrary and is copied from the vendor's
reconciliation:

1. param **reads** -- correct our view from the device before anything acts on
   it, and before a write can race them;
2. param **writes** -- cheap, small, and may change heartbeat or power behaviour;
3. ``flash_save`` (TCLV 53) -- persist step 2 before anything can reboot;
4. framebuffer read -- expensive, so do it while the link definitely exists, but
   after the cheap work has landed;
5. **image push** -- the big one; it must land before the device leaves;
6. ghost clear -- a second full-screen push, only after 5;
7. **sleep** -- the device leaves, so everything above must already be on the
   wire.  The vendor waits for its renderer before publishing its sleep packet;
   get this wrong and the user stares at yesterday's dashboard for a day;
8. **reboot** -- last, unconditionally.  Nothing survives it.

:meth:`PendingWork.apply` enqueues in exactly that order, and
:meth:`DeviceConnection.apply_pending` is the normal entry point.  Because
``drain()`` returns bytes in queue order and TCP preserves order, queueing sleep
after the image bytes *is* "the image lands first".  A caller that additionally
wants each phase **acked** before the next can drive it in stages with the
``phases=`` argument.

Slots clear on ack, not on send
-------------------------------

:meth:`PendingWork.on_acked` / :meth:`PendingWork.on_nacked` take the packet id
from the library's own ``Acked``/``Nacked`` events, so a connection that drops
mid-reconcile leaves the remainder queued for next time.

``Nacked.charging`` (control flag bit 25) deserves its own branch and gets one
here: it is a **retry-later**, not a failure.  Treating it as a hard failure
would make an integration appear to break whenever someone plugs the sign in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

__all__ = [
    "PENDING_ORDER",
    "SLOT_PARAM_READS",
    "SLOT_PARAM_WRITES",
    "SLOT_FLASH_SAVE",
    "SLOT_FRAMEBUFFER_READ",
    "SLOT_IMAGE",
    "SLOT_GHOST_CLEAR",
    "SLOT_SLEEP",
    "SLOT_REBOOT",
    "PendingWork",
]

SLOT_PARAM_READS = "param_reads"
SLOT_PARAM_WRITES = "param_writes"
SLOT_FLASH_SAVE = "flash_save"
SLOT_FRAMEBUFFER_READ = "framebuffer_read"
SLOT_IMAGE = "image"
SLOT_GHOST_CLEAR = "ghost_clear"
SLOT_SLEEP = "sleep"
SLOT_REBOOT = "reboot"

PENDING_ORDER: tuple[str, ...] = (
    SLOT_PARAM_READS,
    SLOT_PARAM_WRITES,
    SLOT_FLASH_SAVE,
    SLOT_FRAMEBUFFER_READ,
    SLOT_IMAGE,
    SLOT_GHOST_CLEAR,
    SLOT_SLEEP,
    SLOT_REBOOT,
)
"""The order work is put on the wire. See the module docstring; do not reorder.

In particular :data:`SLOT_SLEEP` must come after :data:`SLOT_IMAGE` and
:data:`SLOT_REBOOT` must be last.
"""

TCLV_CMD_FLASH_SAVE = 53


@dataclass(slots=True)
class PendingWork:
    """Desired state for one device UUID. Serialisable, coalescing, durable.

    Obtain one with :meth:`DeviceStateStore.queue`; persist it with
    :meth:`to_dict` and restore with :meth:`from_dict` so that a consumer
    restart does not silently drop a push the user queued ten minutes ago.
    """

    content_revision: int = 0
    """Bumped by :meth:`set_image`. Compared against :attr:`pushed_revision`."""

    pushed_revision: int = 0
    """The revision last acked by the device."""

    force_push: bool = False
    """Push even when the device reports it is already in sync."""

    frame: Any = None
    """The single pending :class:`~pyvisionect.imaging.EncodedFrame`.

    Depth exactly one: :meth:`set_image` replaces it. Not included in
    :meth:`to_dict` -- it is megabytes of pixels and cheaper to re-encode than
    to persist. Persist the *content source* instead and re-encode on restore;
    :attr:`content_revision` survives so the intent is not lost.
    """

    params: dict[int, Any] = field(default_factory=dict)
    """TCLV writes, last-write-wins per id."""

    param_reads: set[int] = field(default_factory=set)
    """TCLV reads, set union."""

    persist_params: bool = True
    """Issue ``flash_save`` (TCLV 53) after the writes.

    On by default because without it every write is RAM-only and is lost at the
    next reboot -- the same trap the USB CLI has.
    """

    want_ghost_clear: bool = False
    want_reboot: bool = False

    sleep_minutes: int | None = None
    """Replaces any prior sleep. ``None`` means "do not sleep"."""

    framebuffer_read: str | None = None
    """A single pending framebuffer read request; a later one replaces it."""

    queued_at: dict[str, float] = field(default_factory=dict)
    """Slot -> the clock value at which it was queued. Purely informational."""

    attempts: dict[str, int] = field(default_factory=dict)
    """Slot -> how many times we have put it on the wire."""

    inflight: dict[int, str] = field(default_factory=dict)
    """Packet id -> slot, for the work currently awaiting an ack."""

    last_nack_charging: bool = False
    """Whether the most recent NACK was "refused because charging"."""

    # ------------------------------------------------------------ queueing

    def _touch(self, slot: str, now: float | None) -> None:
        if now is not None:
            self.queued_at[slot] = now

    def set_image(self, frame: Any, *, force: bool = False,
                  now: float | None = None) -> int:
        """Queue *frame* as the content to display. Last-write-wins.

        Returns:
            The new :attr:`content_revision`.
        """
        self.frame = frame
        self.content_revision += 1
        if force:
            self.force_push = True
        self._touch(SLOT_IMAGE, now)
        return self.content_revision

    def write_param(self, param_id: int, value: Any, *,
                    now: float | None = None) -> None:
        """Queue a TCLV write. Last-write-wins per id.

        Raises:
            ReadOnlyParameter: for the eight ids the network cannot write, with
                the USB command that can. Raised here rather than at send time
                so the caller learns immediately instead of in an hour.
        """
        from ..devices.tclv import check_writable

        check_writable(param_id)
        self.params[param_id] = value
        self._touch(SLOT_PARAM_WRITES, now)

    def write_params(self, values: dict[int, Any], *,
                     now: float | None = None) -> None:
        for param_id, value in values.items():
            self.write_param(param_id, value, now=now)

    def read_params(self, ids: Iterable[int], *, now: float | None = None) -> None:
        """Queue TCLV reads. Set union; reads are idempotent and nearly free."""
        self.param_reads.update(int(i) for i in ids)
        self._touch(SLOT_PARAM_READS, now)

    def read_framebuffer(self, what: str = "auto", *,
                         now: float | None = None) -> None:
        """Queue a framebuffer/file read. Single slot; a later call replaces it."""
        self.framebuffer_read = what
        self._touch(SLOT_FRAMEBUFFER_READ, now)

    def ghost_clear(self, *, now: float | None = None) -> None:
        """Queue an inverse / anti-ghosting full-screen refresh. Set semantics."""
        self.want_ghost_clear = True
        self._touch(SLOT_GHOST_CLEAR, now)

    def reboot(self, *, now: float | None = None) -> None:
        """Queue a reboot. Set semantics -- pressing it twice is one reboot.

        Note this sends a ``packet.Type 2`` command, which has never been
        observed on the wire; see :mod:`pyvisionect.packets.misc`.
        """
        self.want_reboot = True
        self._touch(SLOT_REBOOT, now)

    def sleep(self, minutes: int, *, now: float | None = None) -> None:
        """Queue a sleep. **Replaces** any prior sleep.

        Raises:
            ValueError: for ``minutes <= 0``; the vendor rejects a zero
                duration with ``errSleepTime``.
        """
        if int(minutes) <= 0:
            raise ValueError(
                f"sleep duration must be a positive number of minutes, got "
                f"{minutes!r}; the vendor's stdcmd.Sleep.Done rejects 0 with "
                "errSleepTime"
            )
        self.sleep_minutes = int(minutes)
        self._touch(SLOT_SLEEP, now)

    def command(self, kind: str, **kwargs: Any) -> None:
        """Generic entry point: ``"ghost_clear"``, ``"reboot"`` or ``"sleep"``."""
        if kind == "ghost_clear":
            self.ghost_clear(**kwargs)
        elif kind == "reboot":
            self.reboot(**kwargs)
        elif kind == "sleep":
            self.sleep(**kwargs)
        else:
            raise ValueError(
                f"unknown command kind {kind!r}; known kinds are "
                "'ghost_clear', 'reboot', 'sleep'"
            )

    # -------------------------------------------------------------- state

    @property
    def content_pending(self) -> bool:
        """Whether there is content to push."""
        if self.frame is None:
            return False
        return self.force_push or self.content_revision != self.pushed_revision

    @property
    def is_empty(self) -> bool:
        """Whether there is nothing at all left to do."""
        return not any(
            (
                self.param_reads,
                self.params,
                self.framebuffer_read,
                self.content_pending,
                self.want_ghost_clear,
                self.sleep_minutes is not None,
                self.want_reboot,
            )
        )

    def slots(self) -> list[str]:
        """The pending slots, in :data:`PENDING_ORDER`."""
        present = {
            SLOT_PARAM_READS: bool(self.param_reads),
            SLOT_PARAM_WRITES: bool(self.params),
            SLOT_FLASH_SAVE: bool(self.params) and self.persist_params,
            SLOT_FRAMEBUFFER_READ: self.framebuffer_read is not None,
            SLOT_IMAGE: self.content_pending,
            SLOT_GHOST_CLEAR: self.want_ghost_clear,
            SLOT_SLEEP: self.sleep_minutes is not None,
            SLOT_REBOOT: self.want_reboot,
        }
        return [slot for slot in PENDING_ORDER if present[slot]]

    def clear_slot(self, slot: str) -> None:
        """Mark one slot done. Called for you by :meth:`on_acked`."""
        if slot == SLOT_PARAM_READS:
            self.param_reads.clear()
        elif slot == SLOT_PARAM_WRITES:
            self.params.clear()
        elif slot == SLOT_FLASH_SAVE:
            pass  # derived from params; cleared with them
        elif slot == SLOT_FRAMEBUFFER_READ:
            self.framebuffer_read = None
        elif slot == SLOT_IMAGE:
            self.pushed_revision = self.content_revision
            self.force_push = False
        elif slot == SLOT_GHOST_CLEAR:
            self.want_ghost_clear = False
        elif slot == SLOT_SLEEP:
            self.sleep_minutes = None
        elif slot == SLOT_REBOOT:
            self.want_reboot = False
        else:
            raise ValueError(f"unknown slot {slot!r}")

    def note_sent(self, slot: str, packet_id: int, *,
                  now: float | None = None) -> None:
        """Record that *slot* went out as *packet_id*, awaiting an ack."""
        self.inflight[packet_id] = slot
        self.attempts[slot] = self.attempts.get(slot, 0) + 1
        self._touch(slot, now)

    def on_acked(self, packet_id: int) -> str | None:
        """Clear whatever slot *packet_id* belonged to. Returns the slot, if any."""
        slot = self.inflight.pop(packet_id, None)
        if slot is None:
            return None
        self.last_nack_charging = False
        self.clear_slot(slot)
        return slot

    def on_nacked(self, packet_id: int, *, charging: bool = False) -> str | None:
        """Leave the slot queued for the next connection. Returns the slot.

        A charging NACK is a retry-later and is recorded as such; the work stays
        queued either way, because a NACK means the device did not do it.
        """
        slot = self.inflight.pop(packet_id, None)
        self.last_nack_charging = charging
        return slot

    def reset_inflight(self) -> None:
        """Forget what was in flight. Call when a connection drops.

        Nothing is cleared: a packet that was never acked was never done.
        """
        self.inflight.clear()

    # ------------------------------------------------------- persistence

    VERSION = 1

    def to_dict(self) -> dict[str, Any]:
        """A JSON-safe snapshot. :attr:`frame` is deliberately **not** included."""
        return {
            "v": self.VERSION,
            "content_revision": self.content_revision,
            "pushed_revision": self.pushed_revision,
            "force_push": self.force_push,
            "params": {str(k): _jsonable(v) for k, v in self.params.items()},
            "param_reads": sorted(self.param_reads),
            "persist_params": self.persist_params,
            "want_ghost_clear": self.want_ghost_clear,
            "want_reboot": self.want_reboot,
            "sleep_minutes": self.sleep_minutes,
            "framebuffer_read": self.framebuffer_read,
            "queued_at": dict(self.queued_at),
            "attempts": dict(self.attempts),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PendingWork":
        """Restore from :meth:`to_dict`.

        Unknown future versions raise rather than silently mis-restoring.
        """
        version = raw.get("v", 1)
        if version != cls.VERSION:
            raise ValueError(
                f"PendingWork snapshot version {version} is not {cls.VERSION}; "
                "discard it and start fresh rather than guessing"
            )
        work = cls(
            content_revision=int(raw.get("content_revision", 0)),
            pushed_revision=int(raw.get("pushed_revision", 0)),
            force_push=bool(raw.get("force_push", False)),
            params={int(k): _unjsonable(v) for k, v in (raw.get("params") or {}).items()},
            param_reads={int(i) for i in (raw.get("param_reads") or [])},
            persist_params=bool(raw.get("persist_params", True)),
            want_ghost_clear=bool(raw.get("want_ghost_clear", False)),
            want_reboot=bool(raw.get("want_reboot", False)),
            sleep_minutes=raw.get("sleep_minutes"),
            framebuffer_read=raw.get("framebuffer_read"),
            queued_at=dict(raw.get("queued_at") or {}),
            attempts=dict(raw.get("attempts") or {}),
        )
        return work


def _jsonable(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"__bytes__": value.hex()}
    return value


def _unjsonable(value: Any) -> Any:
    if isinstance(value, dict) and "__bytes__" in value:
        return bytes.fromhex(value["__bytes__"])
    return value
