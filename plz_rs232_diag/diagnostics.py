from __future__ import annotations

import grp
import os
import platform
import pwd
import socket
import stat
import struct
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

try:
    import fcntl
except ImportError:  # pragma: no cover - Linux is the supported platform.
    fcntl = None

try:
    import serial
except ImportError:  # Inventory commands remain usable without pyserial.
    serial = None


SUPPORTED_BAUD_RATES = (2400, 4800, 9600, 19200)
TRIM_BAUD_RATES = (8800, 9000, 9200, 9400, 9500, 9600, 9700, 9800, 10000, 10200, 10400)
FRAMINGS = {
    "8N1": (8, "N", 1),
    "8N2": (8, "N", 2),
    "8E1": (8, "E", 1),
    "8O1": (8, "O", 1),
    "7E1": (7, "E", 1),
    "7O1": (7, "O", 1),
}
TERMINATORS = {
    "LF": b"\n",
    "CR": b"\r",
    "CRLF": b"\r\n",
}

TIOCGICOUNT = 0x545D
ICOUNT_FIELDS = (
    "cts",
    "dsr",
    "rng",
    "dcd",
    "rx",
    "tx",
    "frame",
    "overrun",
    "parity",
    "brk",
    "buf_overrun",
)
UART_ERROR_FIELDS = ("frame", "overrun", "parity", "brk", "buf_overrun")


def host_metadata() -> dict[str, Any]:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "kernel": platform.release(),
        "uid": os.geteuid(),
        "user": _user_name(os.geteuid()),
    }


def _user_name(uid: int) -> str | None:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return None


def _group_name(gid: int) -> str | None:
    try:
        return grp.getgrgid(gid).gr_name
    except KeyError:
        return None


def _read_attr(path: Path, name: str) -> str | None:
    try:
        return (path / name).read_text(encoding="utf-8", errors="replace").strip()
    except (FileNotFoundError, PermissionError, OSError):
        return None


def discover_ports() -> list[str]:
    ports = set()
    for pattern in ("/dev/ttyUSB*", "/dev/ttyACM*"):
        ports.update(str(path) for path in Path("/dev").glob(Path(pattern).name))
    return sorted(ports)


def _usb_parent_for_tty(port: str) -> Path | None:
    tty_path = Path("/sys/class/tty") / Path(port).name
    try:
        resolved = tty_path.resolve(strict=True)
    except (FileNotFoundError, OSError):
        return None
    for candidate in (resolved, *resolved.parents):
        if (candidate / "idVendor").exists():
            return candidate
    return None


def _driver_for_tty(port: str) -> str | None:
    tty_path = Path("/sys/class/tty") / Path(port).name
    try:
        resolved = tty_path.resolve(strict=True)
    except (FileNotFoundError, OSError):
        return None
    for candidate in (resolved, *resolved.parents):
        driver = candidate / "driver"
        if driver.is_symlink():
            try:
                return driver.resolve(strict=True).name
            except (FileNotFoundError, OSError):
                return None
    return None


def _stable_links(port: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {"by_id": [], "by_path": []}
    try:
        target = Path(port).resolve(strict=True)
    except (FileNotFoundError, OSError):
        return result

    for key, directory in (
        ("by_id", Path("/dev/serial/by-id")),
        ("by_path", Path("/dev/serial/by-path")),
    ):
        if not directory.is_dir():
            continue
        for link in directory.iterdir():
            try:
                if link.resolve(strict=True) == target:
                    result[key].append(str(link))
            except (FileNotFoundError, OSError):
                continue
        result[key].sort()
    return result


def port_inventory(port: str) -> dict[str, Any]:
    path = Path(port)
    row: dict[str, Any] = {"port": port, "exists": path.exists()}
    try:
        info = path.stat()
        row.update(
            {
                "mode": stat.filemode(info.st_mode),
                "uid": info.st_uid,
                "user": _user_name(info.st_uid),
                "gid": info.st_gid,
                "group": _group_name(info.st_gid),
                "readable": os.access(path, os.R_OK),
                "writable": os.access(path, os.W_OK),
            }
        )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        row["stat_error"] = f"{type(exc).__name__}: {exc}"

    row["stable_links"] = _stable_links(port)
    row["driver"] = _driver_for_tty(port)
    usb = _usb_parent_for_tty(port)
    if usb is not None:
        row["usb"] = usb_device_record(usb)
    else:
        row["usb"] = None
    return row


def usb_device_record(path: Path) -> dict[str, Any]:
    return {
        "node": path.name,
        "sysfs": str(path),
        "vid": _read_attr(path, "idVendor"),
        "pid": _read_attr(path, "idProduct"),
        "manufacturer": _read_attr(path, "manufacturer"),
        "product": _read_attr(path, "product"),
        "serial": _read_attr(path, "serial"),
        "busnum": _read_attr(path, "busnum"),
        "devnum": _read_attr(path, "devnum"),
        "speed_mbps": _read_attr(path, "speed"),
        "configuration": _read_attr(path, "bConfigurationValue"),
        "authorized": _read_attr(path, "authorized"),
    }


def all_usb_devices() -> list[dict[str, Any]]:
    root = Path("/sys/bus/usb/devices")
    if not root.is_dir():
        return []
    rows = []
    for path in sorted(root.iterdir()):
        if (path / "idVendor").exists():
            rows.append(usb_device_record(path.resolve()))
    return rows


def _usb_identity(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        row.get("vid"),
        row.get("pid"),
        row.get("serial"),
        row.get("product"),
    )


def duplicate_usb_exposures(devices: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for row in devices:
        groups.setdefault(_usb_identity(row), []).append(row)

    result = []
    for identity, rows in groups.items():
        configured = [row for row in rows if row.get("configuration") not in (None, "", "0")]
        unconfigured = [row for row in rows if row.get("configuration") in ("", "0")]
        if configured and unconfigured:
            result.append(
                {
                    "identity": {
                        "vid": identity[0],
                        "pid": identity[1],
                        "serial": identity[2],
                        "product": identity[3],
                    },
                    "configured_nodes": [row["node"] for row in configured],
                    "unconfigured_nodes": [row["node"] for row in unconfigured],
                    "note": "same USB identity appears as configured and unconfigured devices",
                }
            )
    return result


def inventory(ports: Sequence[str] | None = None) -> dict[str, Any]:
    selected = list(ports) if ports else discover_ports()
    port_rows = [port_inventory(port) for port in selected]
    active_identities = {
        _usb_identity(row["usb"])
        for row in port_rows
        if row.get("usb") is not None
    }
    devices = [
        row for row in all_usb_devices() if _usb_identity(row) in active_identities
    ]
    return {
        "ports": port_rows,
        "matching_usb_devices": devices,
        "duplicate_exposures": duplicate_usb_exposures(devices),
    }


def _require_serial() -> Any:
    if serial is None:
        raise RuntimeError(
            "pyserial is not installed; install python3-serial or run 'python3 -m pip install pyserial'"
        )
    return serial


def open_serial(
    port: str,
    *,
    baudrate: int = 9600,
    framing: str = "8N1",
    timeout: float = 0.7,
    xonxoff: bool = True,
) -> Any:
    serial_module = _require_serial()
    if framing not in FRAMINGS:
        raise ValueError(f"unsupported framing: {framing}")
    bytesize, parity, stopbits = FRAMINGS[framing]
    kwargs = {
        "port": port,
        "baudrate": baudrate,
        "bytesize": bytesize,
        "parity": parity,
        "stopbits": stopbits,
        "timeout": timeout,
        "write_timeout": 1.0,
        "xonxoff": xonxoff,
        "rtscts": False,
        "dsrdtr": False,
    }
    try:
        return serial_module.Serial(exclusive=True, **kwargs)
    except TypeError:
        return serial_module.Serial(**kwargs)


def read_response(handle: Any, max_bytes: int = 256) -> bytes:
    data = handle.read(max_bytes)
    while len(data) < max_bytes:
        waiting = int(getattr(handle, "in_waiting", 0) or 0)
        if waiting <= 0:
            break
        data += handle.read(min(waiting, max_bytes - len(data)))
    return data


def exchange_idn(
    handle: Any,
    *,
    terminator: bytes = b"\n",
    send_xon: bool = False,
    max_bytes: int = 256,
) -> bytes:
    handle.reset_input_buffer()
    if send_xon:
        handle.write(b"\x11")
    handle.write(b"*IDN?" + terminator)
    handle.flush()
    return read_response(handle, max_bytes=max_bytes)


def response_record(data: bytes) -> dict[str, Any]:
    printable = sum(1 for value in data if 32 <= value < 127 or value in (10, 13))
    return {
        "length": len(data),
        "hex": data.hex(),
        "ascii": data.decode("ascii", errors="backslashreplace"),
        "printable_bytes": printable,
        "printable_ratio": round(printable / len(data), 4) if data else 0.0,
        "valid_kikusui_idn": data.startswith(b"KIKUSUI,") and b"\x00" not in data,
    }


def uart_counts(handle: Any) -> dict[str, int]:
    if fcntl is None:
        raise OSError("fcntl is unavailable")
    buffer = bytearray(80)
    fcntl.ioctl(handle.fileno(), TIOCGICOUNT, buffer, True)
    values = struct.unpack("20i", buffer)
    return dict(zip(ICOUNT_FIELDS, values[: len(ICOUNT_FIELDS)]))


def count_delta(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    return {name: after[name] - before[name] for name in ICOUNT_FIELDS}


def classify_probe(
    responses: Sequence[dict[str, Any]], counter_info: dict[str, Any] | None
) -> str:
    if any(row.get("valid_kikusui_idn") for row in responses):
        return "reachable"
    if any(row.get("length", 0) > 0 for row in responses):
        delta = (counter_info or {}).get("delta") or {}
        if any(delta.get(name, 0) > 0 for name in UART_ERROR_FIELDS):
            return "reply-with-uart-errors"
        return "unexpected-reply"
    delta = (counter_info or {}).get("delta") or {}
    if delta.get("tx", 0) > 0 and delta.get("rx", 0) == 0:
        return "transmitted-no-rx"
    return "no-response"


def probe_port(
    port: str,
    *,
    baudrate: int = 9600,
    framing: str = "8N1",
    terminator: bytes = b"\n",
    terminator_name: str = "LF",
    repeats: int = 5,
    timeout: float = 0.7,
    xonxoff: bool = True,
    send_xon: bool = False,
    collect_counts: bool = True,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "port": port,
        "settings": {
            "baudrate": baudrate,
            "framing": framing,
            "terminator": terminator_name,
            "xonxoff": xonxoff,
            "send_xon": send_xon,
            "timeout_seconds": timeout,
        },
    }
    try:
        with open_serial(
            port,
            baudrate=baudrate,
            framing=framing,
            timeout=timeout,
            xonxoff=xonxoff,
        ) as handle:
            counter_info: dict[str, Any] = {"supported": False}
            before = None
            if collect_counts:
                try:
                    before = uart_counts(handle)
                    counter_info = {"supported": True, "before": before}
                except OSError as exc:
                    counter_info = {
                        "supported": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    }

            responses = []
            for _ in range(repeats):
                responses.append(
                    response_record(
                        exchange_idn(
                            handle,
                            terminator=terminator,
                            send_xon=send_xon,
                        )
                    )
                )

            if before is not None:
                after = uart_counts(handle)
                counter_info["after"] = after
                counter_info["delta"] = count_delta(before, after)

            result["responses"] = responses
            result["uart_counters"] = counter_info
            result["status"] = classify_probe(responses, counter_info)
    except Exception as exc:
        result.update(
            {
                "status": "open-or-io-error",
                "error": f"{type(exc).__name__}: {exc}",
                "responses": [],
            }
        )
    return result


def probe_ports(
    ports: Sequence[str],
    *,
    baudrate: int = 9600,
    repeats: int = 5,
    timeout: float = 0.7,
    xonxoff: bool = True,
    send_xon: bool = False,
) -> list[dict[str, Any]]:
    return [
        probe_port(
            port,
            baudrate=baudrate,
            repeats=repeats,
            timeout=timeout,
            xonxoff=xonxoff,
            send_xon=send_xon,
        )
        for port in ports
    ]


def scan_ports(
    ports: Sequence[str],
    *,
    baud_rates: Iterable[int],
    framings: Iterable[str],
    terminators: Iterable[str],
    repeats: int = 1,
    timeout: float = 0.7,
) -> list[dict[str, Any]]:
    rows = []
    for port in ports:
        for baudrate in baud_rates:
            for framing in framings:
                for terminator_name in terminators:
                    rows.append(
                        probe_port(
                            port,
                            baudrate=baudrate,
                            framing=framing,
                            terminator=TERMINATORS[terminator_name],
                            terminator_name=terminator_name,
                            repeats=repeats,
                            timeout=timeout,
                            collect_counts=False,
                        )
                    )
    return rows


def break_isolation(
    port: str,
    *,
    opens: int = 5,
    queries: int = 5,
    timeout: float = 0.45,
) -> dict[str, Any]:
    result: dict[str, Any] = {"port": port, "opens": opens, "queries": queries}
    try:
        with open_serial(port, timeout=timeout) as handle:
            baseline = uart_counts(handle)
        for _ in range(opens):
            with open_serial(port, timeout=timeout):
                time.sleep(0.25)
        with open_serial(port, timeout=timeout) as handle:
            after_open_only = uart_counts(handle)

        with open_serial(port, timeout=timeout) as handle:
            before_queries = uart_counts(handle)
            trials = []
            for _ in range(queries):
                data = exchange_idn(handle)
                trials.append(
                    {
                        "response": response_record(data),
                        "counters": uart_counts(handle),
                    }
                )
            after_queries = uart_counts(handle)

        result.update(
            {
                "supported": True,
                "baseline": baseline,
                "after_open_only": after_open_only,
                "open_only_delta": count_delta(baseline, after_open_only),
                "before_queries": before_queries,
                "after_queries": after_queries,
                "query_delta": count_delta(before_queries, after_queries),
                "trials": trials,
            }
        )
    except Exception as exc:
        result.update(
            {
                "supported": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
    return result


def port_owners() -> list[dict[str, Any]]:
    rows = []
    proc = Path("/proc")
    if not proc.is_dir():
        return rows
    for process in proc.iterdir():
        if not process.name.isdigit():
            continue
        fd_dir = process / "fd"
        try:
            descriptors = list(fd_dir.iterdir())
        except (FileNotFoundError, PermissionError, OSError):
            continue
        for descriptor in descriptors:
            try:
                target = os.readlink(descriptor)
            except (FileNotFoundError, PermissionError, OSError):
                continue
            if "ttyUSB" not in target and "ttyACM" not in target:
                continue
            try:
                command = (
                    (process / "cmdline")
                    .read_bytes()
                    .replace(b"\0", b" ")
                    .decode(errors="replace")
                    .strip()
                )
            except (FileNotFoundError, PermissionError, OSError):
                command = ""
            rows.append(
                {
                    "pid": int(process.name),
                    "fd": descriptor.name,
                    "target": target,
                    "command": command,
                }
            )
    return sorted(rows, key=lambda row: (row["target"], row["pid"], row["fd"]))


def kernel_serial_events(since: str = "30 minutes ago", limit: int = 200) -> dict[str, Any]:
    command = ["journalctl", "-k", "--since", since, "--no-pager", "-o", "short-iso"]
    try:
        completed = subprocess.run(
            command,
            check=False,
            text=True,
            capture_output=True,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        return {"available": False, "error": f"{type(exc).__name__}: {exc}", "events": []}

    needles = ("usb", "ttyusb", "ttyacm", "ch341", "ftdi", "overrun", "disconnect", "error")
    events = [
        line
        for line in completed.stdout.splitlines()
        if any(needle in line.lower() for needle in needles)
    ]
    return {
        "available": completed.returncode == 0,
        "returncode": completed.returncode,
        "stderr": completed.stderr.strip(),
        "since": since,
        "events": events[-limit:],
    }


def diagnostic_bundle(
    ports: Sequence[str],
    *,
    repeats: int = 5,
    timeout: float = 0.7,
) -> dict[str, Any]:
    return {
        "inventory": inventory(ports),
        "owners": port_owners(),
        "probes": probe_ports(ports, repeats=repeats, timeout=timeout),
        "kernel_events": kernel_serial_events(),
    }
