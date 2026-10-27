from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .diagnostics import (
    FRAMINGS,
    SUPPORTED_BAUD_RATES,
    TERMINATORS,
    TRIM_BAUD_RATES,
    break_isolation,
    diagnostic_bundle,
    discover_ports,
    host_metadata,
    inventory,
    kernel_serial_events,
    port_owners,
    probe_ports,
    scan_ports,
)


def _add_serial_options(parser: argparse.ArgumentParser, *, repeats: int = 1) -> None:
    parser.add_argument("ports", nargs="*", help="default: detected /dev/ttyUSB* and /dev/ttyACM*")
    parser.add_argument("--repeats", type=int, default=repeats)
    parser.add_argument("--timeout", type=float, default=0.7, help="read timeout in seconds")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="plz-rs232-diag",
        description="Read-only KIKUSUI PLZ-U RS232 diagnostics",
    )
    parser.add_argument("--output", type=Path, help="write JSON report to this path")
    parser.add_argument("--compact", action="store_true", help="emit compact JSON")
    parser.add_argument("--strict", action="store_true", help="return 2 unless every probe is reachable")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inventory_parser = subparsers.add_parser("inventory", help="list serial ports and USB topology")
    inventory_parser.add_argument("ports", nargs="*")

    probe_parser = subparsers.add_parser("probe", help="query *IDN? using 9600/8N1/LF")
    _add_serial_options(probe_parser, repeats=5)
    probe_parser.add_argument("--baud", type=int, default=9600)
    probe_parser.add_argument(
        "--flow-control",
        choices=("xonxoff", "none"),
        default="xonxoff",
    )
    probe_parser.add_argument("--send-xon", action="store_true")

    scan_baud = subparsers.add_parser("scan-baud", help="scan official PLZ-U baud rates")
    _add_serial_options(scan_baud)

    scan_framing = subparsers.add_parser(
        "scan-framing", help="scan framing and terminator combinations at 9600 bps"
    )
    _add_serial_options(scan_framing)

    scan_trim = subparsers.add_parser("scan-trim", help="scan baud rates around 9600 bps")
    _add_serial_options(scan_trim, repeats=2)

    counters = subparsers.add_parser("counters", help="probe and capture UART counter deltas")
    _add_serial_options(counters, repeats=5)

    break_check = subparsers.add_parser(
        "break-check", help="separate open-only UART errors from reply-time errors"
    )
    break_check.add_argument("ports", nargs="*")
    break_check.add_argument("--opens", type=int, default=5)
    break_check.add_argument("--queries", type=int, default=5)
    break_check.add_argument("--timeout", type=float, default=0.45)

    subparsers.add_parser("owners", help="list processes holding serial ports")

    kernel_log = subparsers.add_parser("kernel-log", help="show recent USB/serial kernel events")
    kernel_log.add_argument("--since", default="30 minutes ago")
    kernel_log.add_argument("--limit", type=int, default=200)

    all_parser = subparsers.add_parser("all", help="collect the normal diagnostic bundle")
    _add_serial_options(all_parser, repeats=5)
    return parser


def _selected_ports(requested: Sequence[str]) -> list[str]:
    return list(requested) if requested else discover_ports()


def _envelope(command: str, data: Any) -> dict[str, Any]:
    return {"tool": "kikusui-plz-rs232-diag", "host": host_metadata(), "command": command, "data": data}


def _emit(payload: dict[str, Any], output: Path | None, compact: bool) -> None:
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        indent=None if compact else 2,
        separators=(",", ":") if compact else None,
    )
    if output is None:
        print(serialized)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(serialized + "\n", encoding="utf-8")
    print(str(output))


def _validate_positive(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    for name in ("repeats", "opens", "queries", "limit"):
        value = getattr(args, name, None)
        if value is not None and value < 1:
            parser.error(f"--{name.replace('_', '-')} must be at least 1")
    timeout = getattr(args, "timeout", None)
    if timeout is not None and timeout <= 0:
        parser.error("--timeout must be greater than 0")


def _strict_failed(data: Any) -> bool:
    probes = data.get("probes", data) if isinstance(data, dict) else data
    if not isinstance(probes, list):
        return False
    return not probes or any(row.get("status") != "reachable" for row in probes)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _validate_positive(parser, args)
    command = args.command

    try:
        if command == "inventory":
            data = inventory(_selected_ports(args.ports))
        elif command == "probe":
            ports = _selected_ports(args.ports)
            data = probe_ports(
                ports,
                baudrate=args.baud,
                repeats=args.repeats,
                timeout=args.timeout,
                xonxoff=args.flow_control == "xonxoff",
                send_xon=args.send_xon,
            )
        elif command == "scan-baud":
            data = scan_ports(
                _selected_ports(args.ports),
                baud_rates=SUPPORTED_BAUD_RATES,
                framings=("8N1",),
                terminators=("LF",),
                repeats=args.repeats,
                timeout=args.timeout,
            )
        elif command == "scan-framing":
            data = scan_ports(
                _selected_ports(args.ports),
                baud_rates=(9600,),
                framings=FRAMINGS,
                terminators=TERMINATORS,
                repeats=args.repeats,
                timeout=args.timeout,
            )
        elif command == "scan-trim":
            data = scan_ports(
                _selected_ports(args.ports),
                baud_rates=TRIM_BAUD_RATES,
                framings=("8N1",),
                terminators=("LF",),
                repeats=args.repeats,
                timeout=args.timeout,
            )
        elif command == "counters":
            data = probe_ports(
                _selected_ports(args.ports),
                repeats=args.repeats,
                timeout=args.timeout,
            )
        elif command == "break-check":
            data = [
                break_isolation(
                    port,
                    opens=args.opens,
                    queries=args.queries,
                    timeout=args.timeout,
                )
                for port in _selected_ports(args.ports)
            ]
        elif command == "owners":
            data = port_owners()
        elif command == "kernel-log":
            data = kernel_serial_events(since=args.since, limit=args.limit)
        elif command == "all":
            ports = _selected_ports(args.ports)
            data = diagnostic_bundle(ports, repeats=args.repeats, timeout=args.timeout)
        else:  # pragma: no cover - argparse enforces the command choices.
            parser.error(f"unknown command: {command}")
            return 1
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    _emit(_envelope(command, data), args.output, args.compact)
    if args.strict and command in ("probe", "all") and _strict_failed(data):
        return 2
    return 0

