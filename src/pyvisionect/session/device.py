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
from typing import Any

from ..devices.panels import Panel, panel_for
from ..packets.status import StatusPacket
from .pending import PendingWork

__all__ = ["DeviceState", "DeviceStateStore", "RECTANGLE_UNSUPPORTED_HARDWARE"]

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

    def apply_status(self, status: StatusPacket) -> None:
        self.last_status = status

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
