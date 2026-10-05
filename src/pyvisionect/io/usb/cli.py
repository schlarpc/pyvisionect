'''``pyvisionect-usb`` -- the command line over the serial console.

Read-only by default, and the destructive subcommands dry-run by default too:
``provision`` prints the plan and exits unless you pass ``--execute``.  Reading
the plan is the only chance you get to notice that it is about to point a sign at
the wrong host, so it is the default rather than a flag.
'''

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys
from collections.abc import Sequence

from .commands import (
    ABSENT_FROM_7_4_4407,
    HIDDEN_IN_7_4_4407,
    LISTED_BY_HELP,
    COMMANDS,
    DOCUMENTED_COUNT,
    PROBED_ABSENT_7_4_4407,
    UNDOCUMENTED_IN_7_4_4407,
)
from .console import SerialConsole
from .device import Sign
from .discovery import find_ports, identify
from .provisioning import DEFAULT_SERVER_PORT, WifiSecurity, plan_bootstrap, plan_repoint

__all__ = ["main"]


def _as_json(obj: object) -> object:
    """Make a dataclass tree JSON-serialisable, including UUIDs and tuples."""
    import uuid as _uuid

    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {
            f.name: _as_json(getattr(obj, f.name)) for f in dataclasses.fields(obj)
        }
    if isinstance(obj, _uuid.UUID):
        return str(obj)
    if isinstance(obj, (list, tuple)):
        return [_as_json(v) for v in obj]
    if isinstance(obj, dict):
        return {str(k): _as_json(v) for k, v in obj.items()}
    return obj


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for ``pyvisionect-usb``."""
    parser = argparse.ArgumentParser(
        prog="pyvisionect-usb",
        description=(
            "Talk to a Visionect sign over its USB serial console. Read-only "
            "unless you say otherwise; the server address and WiFi credentials "
            "are read-only over the network, so this is the only way to set them."
        ),
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ports", help="list candidate serial ports")
    p.add_argument("--all", action="store_true", help="include unrecognised bridges")

    p = sub.add_parser("identify", help="confirm a port is a sign (2 read-only commands)")
    p.add_argument("port")

    p = sub.add_parser("dump", help="read every readable setting")
    p.add_argument("port")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("run", help="send one command and print the parsed reply")
    p.add_argument("port")
    p.add_argument("command", nargs="+")
    p.add_argument(
        "--allow-writes",
        action="store_true",
        help="permit a setter (still refuses the destructive and hard tiers)",
    )

    p = sub.add_parser(
        "commands", help="the firmware's command set, and the documented delta"
    )
    p.add_argument("--port", help="read `help` from a device instead of the built-in table")

    p = sub.add_parser(
        "provision", help="repoint a sign at your server (prints the plan; add --execute)"
    )
    p.add_argument("port")
    p.add_argument("--server", required=True, help="IP or DNS name (TCLV 18)")
    p.add_argument("--server-port", type=int, default=DEFAULT_SERVER_PORT)
    p.add_argument("--ssid", help="WiFi SSID (TCLV 65); omit to leave WiFi alone")
    p.add_argument("--psk", help="WiFi passphrase (TCLV 67)")
    p.add_argument("--security", default=WifiSecurity.WPA2, choices=WifiSecurity.ALL)
    p.add_argument("--band", type=int, default=0, help="0 dual, 1 2.4GHz, 2 5GHz")
    p.add_argument("--conn-type", type=int, help="TCLV 2; run `commands` first")
    p.add_argument("--no-reboot", action="store_true")
    p.add_argument(
        "--execute",
        action="store_true",
        help="actually send it. Without this, the plan is printed and nothing happens.",
    )

    p = sub.add_parser("encryption", help="what is known about the encryption setters")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING, format="%(message)s"
    )

    if args.cmd == "ports":
        candidates = find_ports(all_ports=args.all)
        if not candidates:
            print("no candidate ports. Try --all, and check you can read the node:")
            print("  ls -l /dev/ttyUSB*   # root:dialout 660 on Linux")
            return 1
        for candidate in candidates:
            mark = "*" if candidate.likely else " "
            print(
                f"{mark} {candidate.device}  {candidate.usb_id or '-'}  "
                f"{candidate.bridge or candidate.description or ''}"
            )
        print()
        print(
            "A '*' only means the USB id is a known bridge -- 0403:6001 is the "
            "stock FTDI id and is in thousands of unrelated products. Run "
            "`identify` to be sure."
        )
        return 0

    if args.cmd == "encryption":
        from . import encryption as E

        print(E.VERDICT)
        print()
        print(f"encryption_mode_set values (TCLV {E.TCLV_ENCRYPTION_MODE}):")
        for value, name in sorted(E.MODES.items()):
            print(f"  {value} = {name}")
        print()
        print(f"encryption_key_set (TCLV {E.TCLV_ENCRYPTION_KEY}) -- key format, untested:")
        for candidate in E.KEY_FORMAT_CANDIDATES:
            print(f"  {candidate.name} ({candidate.length} chars), e.g. {candidate.example}")
        print()
        print(E.TLS_ALTERNATIVE)
        return 0

    if args.cmd == "commands" and not args.port:
        print(
            f"{len(COMMANDS)} commands known to exist in firmware 7.4.4407, of "
            f"which {len(LISTED_BY_HELP)} are listed by help; "
            f"{DOCUMENTED_COUNT} documented by the vendor."
        )
        print(
            f"  {len(HIDDEN_IN_7_4_4407)} present but hidden from help "
            f"({', '.join(sorted(HIDDEN_IN_7_4_4407))})"
        )
        print(
            f"  {len(ABSENT_FROM_7_4_4407)} documented but not listed here "
            f"({len(PROBED_ABSENT_7_4_4407)} probed bare and genuinely absent; "
            f"the other {len(ABSENT_FROM_7_4_4407) - len(PROBED_ABSENT_7_4_4407)} "
            f"are unprobed -- help is not a complete index, so some may be hidden too)"
        )
        print(f"  {len(UNDOCUMENTED_IN_7_4_4407)} present but undocumented")
        print()
        for name, entry in sorted(COMMANDS.items()):
            flags = [entry.kind.value]
            if not entry.documented:
                flags.append("UNDOCUMENTED")
            if name in HIDDEN_IN_7_4_4407:
                flags.append("HIDDEN")
            if entry.asserts:
                flags.append(f"ASSERTS@{entry.asserts}")
            print(f"{entry.syntax:<58} [{','.join(flags)}]")
        return 0

    if args.cmd == "identify":
        result = identify(args.port)
        print(result.describe())
        return 0 if result.is_sign else 1

    if args.cmd == "provision":
        if args.ssid and not args.psk:
            parser.error("--psk is required with --ssid")
        try:
            plan = (
                plan_bootstrap(
                    args.ssid,
                    args.psk,
                    args.server,
                    args.server_port,
                    security=args.security,
                    band=args.band,
                    conn_type=args.conn_type,
                    reboot=not args.no_reboot,
                )
                if args.ssid
                else plan_repoint(
                    args.server, args.server_port, reboot=not args.no_reboot
                )
            )
        except ValueError as exc:
            # A plan this firmware cannot express -- a passphrase with a space
            # being the one that actually happens; a spaced SSID is fine and
            # expands to the three-setter route. A traceback here would bury
            # the explanation, which is the useful part.
            print(f"cannot build that plan: {exc}", file=sys.stderr)
            return 2
        print(plan.describe())
        if plan.unavailable:
            print()
            print(
                f"REFUSING: firmware 7.4.4407 does not have {list(plan.unavailable)}."
            )
            return 2
        if not args.execute:
            print()
            print("Dry run. Nothing was sent. Add --execute to do it for real.")
            return 0
        with SerialConsole(args.port) as console:
            console.sync()
            sign = Sign(console, allow_destructive=True)
            sign.refresh_commands()
            for step, result in zip(plan.steps, plan.execute(sign)):
                print(f"$ {step.command}\n{result.text}")
        return 0

    # the remaining subcommands all need an open console
    with SerialConsole(args.port) as console:
        console.sync()
        if args.cmd == "commands":
            sign = Sign(console)
            names = sign.refresh_commands()
            print(f"{len(names)} commands on this device")
            for name in sorted(names):
                known = COMMANDS.get(name)
                note = "" if known else "   [not in this library's table]"
                print(f"  {name}{note}")
            return 0

        if args.cmd == "dump":
            sign = Sign(console)
            info = sign.dump()
            if args.json:
                print(json.dumps(_as_json(info), indent=2, sort_keys=True))
            else:
                print(info.describe())
                print()
                for f in dataclasses.fields(info):
                    if f.name == "errors":
                        continue
                    value = getattr(info, f.name)
                    if value not in (None, (), {}):
                        print(f"{f.name}: {value}")
                if info.errors:
                    print()
                    print("could not read:")
                    for name, why in sorted(info.errors.items()):
                        print(f"  {name}: {why}")
            return 0

        if args.cmd == "run":
            sign = Sign(console, allow_writes=args.allow_writes)
            result = sign._run(" ".join(args.command))
            print(result.text)
            if result.rv is not None:
                print(f"(rv {result.rv})")
            for entry in result.logs:
                print(f"[async] {entry.text}", file=sys.stderr)
            return 0 if result.ok else 1

    return 0  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
