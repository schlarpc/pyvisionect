"""Exceptions raised by the wire layer.

All of these are protocol-level faults: a frame that cannot be parsed, a checksum
that does not match in strict mode, or a frame that exceeds a limit the real
gateway enforces.
"""

from __future__ import annotations

__all__ = [
    "VisionectError",
    "ProtocolError",
    "ShortBuffer",
    "UnsupportedVersion",
    "UnsupportedCompression",
    "UnsupportedSecurity",
    "FrameTooLarge",
    "ChecksumMismatch",
    "BlockError",
    "PayloadError",
    "ReadOnlyParameter",
    "ListenError",
    "CommandPacketsDisabled",
    "MissingLz4",
]


class VisionectError(Exception):
    """Base class for every error this library raises."""


class ProtocolError(VisionectError):
    """A received byte stream does not conform to the protocol."""


class ShortBuffer(ProtocolError):
    """A fixed-width header was handed fewer bytes than its size.

    Mirrors ``io.ErrShortBuffer`` in ``proto/v2.(*ProtocolHeader).UnmarshalBinary``.
    """


class UnsupportedVersion(ProtocolError):
    """``ProtocolHeader.Version != 3``.

    The gateway's mux drops such a connection with
    ``"unsupported protocol version %d"`` (``decoder.go:81``).
    """


class UnsupportedCompression(ProtocolError):
    """``ProtocolHeader.Compression`` is neither 0 (none) nor 1 (LZ4 block list).

    ``decoder.go:127``: ``"unsupported compression %d"``.
    """


class UnsupportedSecurity(ProtocolError):
    """``ProtocolHeader.Security != 0``.

    Link encryption is out of scope for v1: the session key is escrowed with a
    Visionect HTTPS service and the key material is not present in the server
    image, so a self-hosted deployment is plaintext by construction.
    """


class FrameTooLarge(ProtocolError):
    """``ProtocolHeader.Length`` exceeds ``MAX_FRAME_LENGTH`` (50 MiB).

    ``decoder.go:84``: ``hdr.Length > 0x3200000`` -> ``"packet too large: %d"``.
    """


class ChecksumMismatch(ProtocolError):
    """The CRC-32 in a received ``ProtocolHeader`` did not match.

    Only raised in strict mode. The real gateway never validates the inbound
    field at all, and a device CRC that covers only the header cannot detect
    payload corruption anyway.
    """


class BlockError(ProtocolError):
    """The LZ4 block chain is malformed (bad length, bad index, bad LZ4 block)."""


class PayloadError(ProtocolError):
    """A per-type payload could not be decoded."""


class ReadOnlyParameter(VisionectError):
    """A write was attempted to a TCLV parameter the network cannot write.

    Carries the id, the name and the USB CLI command that *can* set it.
    """

    def __init__(self, param_id: int, name: str, usb_hint: str) -> None:
        self.param_id = param_id
        self.name = name
        self.usb_hint = usb_hint
        super().__init__(
            f"TCLV parameter {param_id} ({name!r}) is read-only over the network "
            f"(canWrite:false in the vendor's own descriptor table). "
            f"Set it over the USB serial CLI instead: {usb_hint}, then `flash_save`, then `reboot`."
        )


class ListenError(VisionectError):
    """Binding the device listener failed.

    ``asyncio.start_server`` raises a bare :class:`OSError`, which leaves a
    caller string-matching ``errno`` to tell "the port is busy" from "I cannot
    bind that address".  Those need completely different advice, so the errno is
    carried here as a field.

    Attributes:
        errno: the OS error number, **or None**. ``EADDRINUSE`` almost always
            means the vendor's own server (or a previous, not-fully-closed
            instance of this one) still holds port 11113; ``EACCES`` means a
            privileged port or a sandbox; ``EADDRNOTAVAIL`` means the host
            address does not exist on this machine.

            ``None`` happens and is not a bug here: when ``getaddrinfo``
            resolves to several candidates and all of them fail, asyncio raises
            a summary ``OSError("could not bind on any address out of ...")``
            and **discards the per-address errno**, so there is nothing to
            recover. Treat ``errno is None`` as "the address could not be
            bound, reason not reported" and show :attr:`host` and :attr:`port`.
        host: the address we tried to bind.
        port: the port we tried to bind.
    """

    def __init__(self, host: str, port: int, cause: OSError) -> None:
        import errno as _errno

        self.host = host
        self.port = port
        self.errno = cause.errno
        self.__cause__ = cause
        name = _errno.errorcode.get(cause.errno, "OSError") if cause.errno else "OSError"
        hint = {
            _errno.EADDRINUSE: (
                "something already holds that port -- most likely the Visionect "
                "server itself, which you cannot run alongside this listener, or "
                "an earlier instance whose client connections were never closed "
                "(see VisionectServer.close)"
            ),
            _errno.EACCES: "no permission to bind that address or port",
            _errno.EADDRNOTAVAIL: "that address does not exist on this host",
        }.get(cause.errno or 0, str(cause))
        if cause.errno is None:
            hint = (
                f"{cause}. asyncio does not report which errno applied to each "
                "candidate address; check that the host address exists on this "
                "machine and that nothing else holds the port"
            )
        super().__init__(f"cannot listen on {host}:{port} ({name}): {hint}")


class CommandPacketsDisabled(VisionectError):
    """A ``packet.Type 2`` (command) send was attempted with them disabled.

    Packet type 2 has never been observed on the wire; see the warning at the
    top of :mod:`pyvisionect.packets.misc`.  ``ConnectionConfig(
    allow_command_packets=False)`` lets a cautious consumer guarantee that none
    are ever emitted.
    """


class MissingLz4(VisionectError):
    """An LZ4 block had to be produced or consumed but ``lz4`` is not installed.

    The device never compresses, so a pure *server* can run without ``lz4`` by
    setting ``ConnectionConfig(compressor=STORED_ONLY)``.
    """
