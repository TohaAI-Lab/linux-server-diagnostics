#!/usr/bin/env python3
"""Read-only Linux diagnostics. Python 3.9+, standard library only."""
import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

VERSION = "0.1.0"


def read_text(path):
    return Path(path).read_text(encoding="utf-8", errors="replace")


def cpu_sample():
    fields = read_text("/proc/stat").splitlines()[0].split()
    values = [int(v) for v in fields[1:9]]
    return sum(values), sum(values[3:5])


def cpu_info():
    total1, idle1 = cpu_sample()
    time.sleep(0.25)
    total2, idle2 = cpu_sample()
    delta = total2 - total1
    busy = 100 * (1 - (idle2 - idle1) / delta) if delta > 0 else None
    return {"logical_cpus": os.cpu_count(), "usage_percent": round(max(0, min(100, busy)), 1) if busy is not None else None,
            "load_average": list(os.getloadavg()), "sample_seconds": 0.25}


def memory_info():
    data = {}
    for line in read_text("/proc/meminfo").splitlines():
        key, _, value = line.partition(":")
        parts = value.split()
        if len(parts) == 2 and parts[1] == "kB":
            data[key] = int(parts[0]) * 1024
    total = data["MemTotal"]
    available = data.get("MemAvailable")
    swap_total = data["SwapTotal"]
    return {"ram_total_bytes": total, "ram_available_bytes": available,
            "ram_used_percent": round(100 * (total - available) / total, 1) if available is not None and total else None,
            "swap_total_bytes": swap_total, "swap_used_bytes": swap_total - data["SwapFree"]}


def disk_info():
    total, used, free = shutil.disk_usage("/")
    return {"path": "/", "total_bytes": total, "used_bytes": used, "available_bytes": free,
            "used_percent": round(100 * used / (used + free), 1) if used + free else None}


def process_info(limit):
    rows = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = dict(line.split(":", 1) for line in read_text(entry / "status").splitlines() if ":" in line)
            rss = fields.get("VmRSS", "").split()
            if not rss:
                continue
            rows.append({"pid": int(entry.name), "name": fields["Name"].strip(), "rss_bytes": int(rss[0]) * 1024})
        except (OSError, ValueError, KeyError):
            continue
    return sorted(rows, key=lambda row: (-row["rss_bytes"], row["pid"]))[:limit]


def network_info():
    rows = []
    for line in read_text("/proc/net/dev").splitlines()[2:]:
        name, sep, stats = line.partition(":")
        if not sep:
            continue
        values = stats.split()
        rows.append({"interface": name.strip(), "rx_bytes": int(values[0]), "tx_bytes": int(values[8])})
    return rows


def service_info():
    systemctl = shutil.which("systemctl")
    if not systemctl or not Path("/run/systemd/system").is_dir():
        return {"available": False, "reason": "systemd is not available in this environment"}
    result = subprocess.run([systemctl, "list-units", "--type=service", "--all", "--plain", "--no-legend", "--no-pager"],
                            capture_output=True, text=True, timeout=8, check=True,
                            env={**os.environ, "LC_ALL": "C", "SYSTEMD_COLORS": "0"})
    rows = []
    for line in result.stdout.splitlines():
        parts = line.split(None, 4)
        if len(parts) >= 4:
            rows.append(dict(zip(("unit", "load", "active", "sub"), parts[:4])))
    return {"available": True, "units": rows}


def collect(limit=10, include_services=False):
    report = {"version": VERSION, "timestamp_utc": datetime.now(timezone.utc).isoformat(),
              "system": {"os": platform.system(), "kernel": platform.release(), "architecture": platform.machine()}}
    sections = {"cpu": cpu_info, "memory": memory_info, "disk": disk_info,
                "top_processes_by_rss": lambda: process_info(limit), "network_counters": network_info}
    if include_services:
        sections["systemd"] = service_info
    for name, getter in sections.items():
        try:
            report[name] = getter()
        except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError) as exc:
            report[name] = {"error": type(exc).__name__}
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top", type=int, default=10, help="Number of processes to show (0–100)")
    parser.add_argument("--services", action="store_true", help="Include loaded systemd services")
    parser.add_argument("--compact", action="store_true", help="Output compact JSON")
    parser.add_argument("--version", action="version", version=VERSION)
    args = parser.parse_args(argv)
    if not 0 <= args.top <= 100:
        parser.error("--top must be between 0 and 100")
    if platform.system() != "Linux":
        parser.exit(1, "This tool requires Linux.\n")
    report = collect(args.top, args.services)
    print(json.dumps(report, indent=None if args.compact else 2, ensure_ascii=True))
    return 1 if any(isinstance(v, dict) and "error" in v for v in report.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
