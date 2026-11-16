"""Asyncio driver for an explicitly authorized bootloader transfer.

The application chooses the build and device before rebooting it. No file is
downloaded here, and a transfer is never retried. A successful return means
all records were acknowledged; verify the version on the next normal status.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import struct
import time
from collections.abc import Callable

from ..bootloader import BootloaderSession, Event, Hello, SessionConfig, State
from .firmware import Build

__all__ = ["run_bootloader"]


def _hello_refusal(build: Build, device_id: bytes, hello: Hello) -> str | None:
    if hello.device_id != device_id:
        return f"unexpected device {hello.uuid}"
    if hello.bootloader[:2] != (6, 3):
        return "firmware transfer has only been measured on bootloader 6.3"
    if hello.hardware != build.hardware:
        return "the firmware is for a different hardware revision"
    if hello.hardware_name_id != build.hardware_name_id:
        return "the firmware is for a different hardware name id"
    if hello.display_id != build.display_id:
        return "the firmware is for a different display"
    return None


async def run_bootloader(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    first: bytes,
    *,
    firmware: bytes,
    build: Build,
    device_id: bytes,
    on_events: Callable[[list[Event]], None] | None = None,
    config: SessionConfig | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> BootloaderSession:
    """Transfer firmware on a connection handed to ``on_bootloader``.

    ``first`` contains the bytes already read by the listener. The remaining
    hello is read with the session's hello timeout. The device id, bootloader
    family, hardware revision, hardware name and display must all match
    before an offer is sent. Firmware must pass structural validation and
    any known SHA-256 pin before the connection is used.

    Returns the session in ``DONE`` or ``FAILED``. Inspect ``error``, ``acked``
    and ``sent_any_record`` on failure. Protocol and network errors stop the
    transfer; cancellation propagates. The socket closes on every exit,
    including exceptions from the synchronous event callback.

    ``DONE`` is transfer completion, not confirmation of the installed
    firmware. Keep the requested version/checksum until the device reconnects
    and reports them. An interrupted transfer may need a power cycle or the
    vendor's USB Configurator; it is never automatically restarted.
    """
    try:
        if len(device_id) != 16:
            raise ValueError("device_id must contain 16 bytes")
        build.validate_firmware(firmware)
        session = BootloaderSession(
            firmware, build.checksum,
            accept_hello=lambda hello: _hello_refusal(build, device_id, hello),
            config=config, now=clock(),
        )

        def emit(events: list[Event]) -> None:
            if events and on_events is not None:
                on_events(events)

        async def flush() -> None:
            data = session.drain()
            if data:
                writer.write(data)
                await asyncio.wait_for(writer.drain(), session.config.ack_timeout_s)

        try:
            emit(session.feed_at(first, clock()))
            while not session.finished:
                await flush()
                try:
                    data = await asyncio.wait_for(reader.read(65536), 0.05)
                except TimeoutError:
                    data = None
                now = clock()
                if data == b"":
                    emit(session.feed_eof(now))
                    break
                if data:
                    emit(session.feed_at(data, now))
                emit(session.advance(now))
            await flush()
        except (OSError, TimeoutError):
            emit(session.feed_eof(clock()))
        except asyncio.CancelledError:
            emit(session.feed_eof(clock()))
            raise

        if session.state is State.DONE:
            # The Suite resets 4.6 ms after the last ack. Preserve the reset
            # and a short gap, as in the measured Tesserae implementation.
            await asyncio.sleep(0.05)
            sock = writer.get_extra_info("socket")
            if sock is not None:
                with contextlib.suppress(OSError):
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        return session
    finally:
        writer.transport.abort()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
