"""The **only** layer that touches I/O.

* :mod:`pyvisionect.io.tcp` -- an asyncio listener on port 11113.
* :mod:`pyvisionect.io.usb` -- a pyserial provisioning helper for the device's
  ASCII serial CLI.

Both are thin. Everything interesting is in the sans-io layers below them.

``pyserial`` is an optional dependency (``pip install 'pyvisionect[usb]'``), so
:mod:`~pyvisionect.io.usb` is imported lazily and this package's ``__init__``
exposes only the TCP side eagerly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .tcp import DEFAULT_PORT, VisionectServer

if TYPE_CHECKING:  # pragma: no cover
    from .usb import UsbProvisioner

__all__ = ["DEFAULT_PORT", "VisionectServer", "UsbProvisioner"]


def __getattr__(name: str) -> Any:
    if name == "UsbProvisioner":
        from .usb import UsbProvisioner

        return UsbProvisioner
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
