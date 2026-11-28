"""Stop processes created during one rollout before the terminal review snapshot.

The environment's existing services and PID 1 remain alive. Process identity includes
start time so that a recycled PID does not inherit an exclusion from the baseline.
"""

import os
import signal
import time
from pathlib import Path


def processes():
    rows = {}
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            fields = path.joinpath("stat").read_text().rsplit(")", 1)[1].split()
            if fields[0] != "Z":         # (parent, start time): the start time a number, as a marker's is read
                rows[int(path.name)] = (int(fields[1]), int(fields[19]))
        except (OSError, IndexError, ValueError):
            continue
    return rows


def snapshot():
    return {str(pid): start for pid, (_, start) in processes().items()}


def exclusions(rows, _ports=(), service_dir=Path("/run/ahe-services")):
    """Return processes that belong to the host or explicitly registered services.

    Declared ports are diagnostic metadata.  A task process can bind one of
    those ports, so port ownership is never sufficient to exempt a process
    from terminal cleanup.
    """
    excluded = {1}
    parent = os.getpid()
    while parent in rows and parent not in excluded:
        excluded.add(parent)
        parent = rows[parent][0]
    # Services are recipe-owned.  The marker directory is created by the
    # runtime as root, and the marker records PID plus its proc start time.
    services = set()
    try:
        directory = service_dir.stat()
        trusted_service_dir = directory.st_uid == 0 and not directory.st_mode & 0o022
    except OSError:
        trusted_service_dir = False
    if not trusted_service_dir:
        return excluded
    for marker in service_dir.glob("*.pid"):
        try:
            metadata = marker.stat()
            if metadata.st_uid != 0 or metadata.st_mode & 0o022:
                continue
            raw = marker.read_text().strip().split(":", 1)
            pid = int(raw[0])
            if pid not in rows:
                continue
            if len(raw) == 2 and int(raw[1]) != rows[pid][1]:
                continue
            services.add(pid)
        except (ValueError, OSError):
            continue
    while True:
        expanded = services | {
            pid for pid, (ppid, _) in rows.items() if ppid in services
        }
        if expanded == services:
            break
        services = expanded
    return excluded | services


def quiesce(baseline, ports=()):
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
