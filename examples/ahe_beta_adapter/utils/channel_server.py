#!/usr/bin/env python3
"""Root-owned bridge for the simulated-user channel.

The Actor container still runs the Codex hooks, but the mailbox and its evidence
live in a root-owned directory.  Hook requests are accepted only from a hook
process whose ancestry contains the running Codex process.  The host-side
controller uses the command-line operations to enqueue user messages and drain
the append-only event stream.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import socket
import sys
import time
from pathlib import Path

DEFAULT_ROOT = Path("/run/ahe-user-channel")
SERVICE_DIR = Path("/run/ahe-services")


def _paths(root: Path) -> dict[str, Path]:
    return {
        "socket": root / "channel.sock",
        "stop": root / "STOP",
        "queue": root / "to-agent",
        "taken": root / "taken",
        "events": root / "events.jsonl",
        "index": root / "events.idx",    # line n: where event n+1 starts in events.jsonl
        "seq": root / "seq",
    }


def _proc_cmdline(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
        return raw.decode(errors="replace").strip()
    except OSError:
        return ""


def _proc_parent(pid: int) -> int | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return int(fields[1])
    except (OSError, IndexError, ValueError):
        return None


def _hook_peer(pid: int) -> bool:
    """Require the peer to be a deployed hook below a live Codex process."""
    seen: set[int] = set()
    saw_hook = False
    current = pid
    for _ in range(32):
        if current in seen or current <= 1:
            break
        seen.add(current)
        command = _proc_cmdline(current)
        if "user_hook.py" in command:
            saw_hook = True
        if saw_hook and re.search(r"(?:^|[ /])codex(?:[ /._-]|$)", command):
            return True
        parent = _proc_parent(current)
        if parent is None:
            break
        current = parent
    return False


def _peer_pid(conn: socket.socket) -> int | None:
    try:
        data = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        return int.from_bytes(data[0:4], "little")
    except (AttributeError, OSError):
        return None


def _read_json_line(conn: socket.socket) -> dict:
    data = bytearray()
    while len(data) < 256 * 1024:
        chunk = conn.recv(4096)
        if not chunk:
            break
        data.extend(chunk)
        if b"\n" in chunk:
            break
    try:
        value = json.loads(bytes(data).splitlines()[0])
    except (IndexError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _reply(conn: socket.socket, value: dict) -> None:
    conn.sendall(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode() + b"\n")


def _last_seq(paths: dict[str, Path]) -> int:
    try:
        return int(paths["seq"].read_text())
    except (OSError, ValueError):
        return 0


def _next_seq(paths: dict[str, Path]) -> int:
    try:
        value = int(paths["seq"].read_text())
    except (OSError, ValueError):
        value = 0
    paths["seq"].write_text(str(value + 1))
    return value + 1


def _safe_record(record: dict) -> dict:
    # The bridge carries only bounded, already-structured hook events.
    encoded = json.dumps(record, ensure_ascii=False, default=str)
    if len(encoded) > 256 * 1024:
        encoded = encoded[:256 * 1024]
    try:
        value = json.loads(encoded)
    except ValueError:
        value = {"kind": "invalid"}
    return value if isinstance(value, dict) else {"kind": "invalid"}


def _enqueue(root: Path, record: dict) -> None:
    paths = _paths(root)
    paths["queue"].mkdir(parents=True, exist_ok=True)
    name = f"{time.time():017.6f}-{record.get('kind', 'message')}.json"
    temporary = paths["queue"] / ("." + name)
    temporary.write_text(json.dumps(_safe_record(record), ensure_ascii=False))
    os.chmod(temporary, 0o600)
    os.replace(temporary, paths["queue"] / name)


def _peek(root: Path, kind: str) -> list[dict]:
    paths = _paths(root)
    out: list[dict] = []
    for path in sorted(paths["queue"].glob(f"*-{kind}.json")) if paths["queue"].is_dir() else []:
        try:
            value = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(value, dict):
            out.append({"_file": path.name, **value})
    return out


def _take(root: Path, kind: str, names: list[str] | None = None) -> list[dict]:
    paths = _paths(root)
    if kind == "message" and paths["queue"].is_dir() and (
            any(paths["queue"].glob("*-end.json")) or any(paths["queue"].glob("*-interrupt.json"))):
        return []                        # after the user's end, and while an interrupt waits: no message handed over
    paths["taken"].mkdir(parents=True, exist_ok=True)
    out: list[dict] = []
    allowed = set(names or ())
    candidates = sorted(paths["queue"].glob(f"*-{kind}.json")) if paths["queue"].is_dir() else []
    for path in candidates:
        if allowed and path.name not in allowed:
            continue
        target = paths["taken"] / path.name
        try:
            os.replace(path, target)
            out.append(json.loads(target.read_text()))
        except (OSError, ValueError):
            continue
    return out


def _report(root: Path, event: dict, mirror: Path | None = None) -> None:
    paths = _paths(root)
    sequence = _next_seq(paths)
    record = {"seq": sequence, **_safe_record(event)}
    offset = paths["events"].stat().st_size if paths["events"].exists() else 0
    with paths["index"].open("a", encoding="utf-8") as stream:   # before the event: an offset may point early,
        stream.write(f"{offset}\n")                               # never past it
    os.chmod(paths["index"], 0o600)
    with paths["events"].open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    os.chmod(paths["events"], 0o600)
    if mirror is not None:
        mirror.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(mirror, flags, 0o644)
        with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        os.chmod(mirror, 0o644)


def _drain(root: Path, since: int) -> dict:
    """The events after sequence `since`, read from where the next one starts (events.idx), not from the top."""
    paths = _paths(root)
    events: list[dict] = []
    offset = 0
    if since > 0:
        try:
            offsets = paths["index"].read_text(encoding="utf-8").split()
            offset = int(offsets[since]) if since < len(offsets) else -1
        except (OSError, ValueError, IndexError):
            offset = 0                                   # no index: the whole stream, filtered by sequence
    if offset < 0:
        return {"next": since, "events": []}
    try:
        with paths["events"].open("rb") as stream:
            stream.seek(offset)
            lines = stream.read().decode("utf-8", errors="replace").split("\n")   # not splitlines: U+2028 in a reply
    except OSError:
        lines = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and int(event.get("seq", 0)) > since:
            events.append(event)
    next_seq = max([since, *(int(event.get("seq", since)) for event in events)])
    return {"next": next_seq, "events": events}


def _handle(root: Path, conn: socket.socket, mirror: Path | None) -> None:
    peer = _peer_pid(conn)
    if peer is None or not _hook_peer(peer):
        _reply(conn, {"ok": False, "error": "untrusted hook peer"})
        return
    request = _read_json_line(conn)
    op = request.get("op")
    if op == "report":
        _report(root, request.get("event") or {}, mirror)
        _reply(conn, {"ok": True})
    elif op == "peek":
        _reply(conn, {"ok": True, "messages": _peek(root, str(request.get("kind") or ""))})
    elif op == "take":
        _reply(
            conn,
            {
                "ok": True,
                "messages": _take(
                    root,
                    str(request.get("kind") or ""),
                    [str(name) for name in request.get("names") or []],
                ),
            },
        )
    else:
        _reply(conn, {"ok": False, "error": "unknown operation"})


def serve(root: Path, pid_file: Path, mirror: Path | None = None) -> None:
    paths = _paths(root)
    root.mkdir(parents=True, exist_ok=True)
    # The hook must traverse to the socket, while queue and evidence files
    # remain protected by their own 0700/0600 modes.
    os.chmod(root, 0o711)
    paths["queue"].mkdir(exist_ok=True)
    paths["taken"].mkdir(exist_ok=True)
    for path in (paths["queue"], paths["taken"]):
        os.chmod(path, 0o700)
        for item in path.iterdir():
            if item.is_file():
                item.unlink(missing_ok=True)
    paths["events"].unlink(missing_ok=True)
    paths["index"].unlink(missing_ok=True)
    paths["seq"].unlink(missing_ok=True)
    if mirror is not None:
        try:
            mirror.unlink()
        except FileNotFoundError:
            pass
    paths["socket"].unlink(missing_ok=True)
    paths["stop"].unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(paths["socket"]))
    os.chmod(paths["socket"], 0o666)
    server.listen(8)
    server.settimeout(0.5)
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(f"{os.getpid()}:{_proc_start_time(os.getpid())}\n")
    os.chmod(pid_file, 0o600)
    try:
        while not paths["stop"].exists():
            try:
                conn, _ = server.accept()
            except (TimeoutError, socket.timeout):     # distinct classes before Python 3.10
                continue
            with conn:
                try:
                    conn.settimeout(10)
                    _handle(root, conn, mirror)
                except OSError:                          # one hook's broken connection ends its request, not the
                    continue                             # bridge
    finally:
        server.close()
        paths["socket"].unlink(missing_ok=True)
        pid_file.unlink(missing_ok=True)


def _proc_start_time(pid: int) -> int:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return int(fields[19])
    except (OSError, IndexError, ValueError):
        return 0


def host_command(root: Path, operation: str, value: str | None = None, wait: float = 0.0) -> None:
    """The host's side, as root: `enqueue` a record (base64 JSON in `value`, else JSON on stdin), `drain` the events
    after sequence `value` (waiting up to `wait` seconds for one, so that the host polls with one command, not a
    loop), `stop` the bridge."""
    paths = _paths(root)
    if operation == "enqueue":
        record = json.loads(base64.b64decode(value).decode()) if value else json.loads(sys.stdin.read() or "null")
        if not isinstance(record, dict):
            raise SystemExit("enqueue needs a JSON record")
        _enqueue(root, record)
    elif operation == "drain":
        since, deadline = int(value or 0), time.monotonic() + max(0.0, wait)
        found = _drain(root, since) if _last_seq(paths) > since else {"next": since, "events": []}
        while not found["events"] and time.monotonic() < deadline and not paths["stop"].exists():
            time.sleep(0.2)
            if _last_seq(paths) > since:                 # the stream is read again only when it has grown
                found = _drain(root, since)
        print(json.dumps(found, ensure_ascii=False))
    elif operation == "stop":
        paths["stop"].write_text("stop\n")
        print(json.dumps({"ok": True}))
    else:
        raise SystemExit(f"unknown host operation: {operation}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("serve", "enqueue", "drain", "stop"))
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--pid-file", type=Path, default=SERVICE_DIR / "ahe-user-channel.pid")
    parser.add_argument("--mirror", type=Path)
    parser.add_argument("--value")
    parser.add_argument("--wait", type=float, default=0.0)
    args = parser.parse_args()
    if args.operation == "serve":
        serve(args.root, args.pid_file, args.mirror)
    else:
        host_command(args.root, args.operation, args.value, args.wait)


if __name__ == "__main__":
    main()
