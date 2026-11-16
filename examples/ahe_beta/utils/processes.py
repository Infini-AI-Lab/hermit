"""Stop processes created during one rollout before the terminal review snapshot.

The environment's existing services and PID 1 remain alive. Process identity includes
start time so that a recycled PID does not inherit an exclusion from the baseline.
"""

import os
from pathlib import Path
import signal
import time


def processes():
    rows = {}
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            fields = path.joinpath("stat").read_text().rsplit(")", 1)[1].split()
            if fields[0] != "Z":
                rows[int(path.name)] = (int(fields[1]), fields[19])
        except (OSError, IndexError, ValueError):
            continue
    return rows


def snapshot():
    return {str(pid): start for pid, (_, start) in processes().items()}


def exclusions(rows, ports):
    excluded = {1}
    parent = os.getpid()
    while parent in rows and parent not in excluded:
        excluded.add(parent)
        parent = rows[parent][0]
    # Services are recipe-owned, and a hot replacement may own a declared handoff port.
    services = set()
    for marker in Path("/run/ahe-services").glob("*.pid"):
        try:
            services.add(int(marker.read_text().strip()))
        except (ValueError, OSError):
            continue
    inodes = set()
    for name in ("tcp", "tcp6"):
        table = Path("/proc/net") / name
        if not table.exists():
            continue
        for line in table.read_text().splitlines()[1:]:
            fields = line.split()
            if fields[3] == "0A" and int(fields[1].rsplit(":", 1)[1], 16) in ports:
                inodes.add(fields[9])
    for pid in rows:
        try:
            for fd in Path("/proc", str(pid), "fd").iterdir():
                try:
                    if os.readlink(fd) in {f"socket:[{i}]" for i in inodes}:
                        services.add(pid)
                except FileNotFoundError:
                    pass
        except FileNotFoundError:
            pass
    while True:
        expanded = services | {
            pid for pid, (ppid, _) in rows.items() if ppid in services
        }
        if expanded == services:
            break
        services = expanded
    return excluded | services


def quiesce(baseline, ports):
    for sig in (signal.SIGTERM, signal.SIGKILL):
        rows = processes()
        excluded = exclusions(rows, ports)
        victims = [
            pid
            for pid, (_, start) in rows.items()
            if pid not in excluded and baseline.get(str(pid)) != start
        ]
        for pid in victims:
            try:
                if processes().get(pid) == rows[pid]:
                    os.kill(pid, sig)
            except ProcessLookupError:
                pass
        time.sleep(0.2)
    rows = processes()
    excluded = exclusions(rows, ports)
    remaining = [
        pid
        for pid, (_, start) in rows.items()
        if pid not in excluded and baseline.get(str(pid)) != start
    ]
    if remaining:
        raise RuntimeError(f"rollout processes still active: {remaining}")
