#!/usr/bin/env python3
"""Inside a sandbox with its own network namespace: re-create the forwarded ports, check the isolation, then run.

    python -m harnessadapter.sandbox_relay [--forward PORT=SOCKET ...] [--deny PATH ...] [--report SOCKET] -- COMMAND

The namespace has loopback only. Each `--forward PORT=SOCKET` listens on 127.0.0.1:PORT and relays every connection
to a Unix socket the host side serves (the host bridges it to its own 127.0.0.1:PORT): the run's tool endpoint, the
allowlist proxy, a model meter. Before the command starts the relay checks the isolation: an outside TCP connection
must fail, every forwarded port must answer, every `--deny` path must not exist. The result goes to the host over
`--report` (a socket, not a file the command could later rewrite); the command does not start unless it passed.

Standard library only: it runs under whatever Python the sandbox shows.
"""
from __future__ import annotations

import argparse
import errno
import json
import os
import socket
import stat
import subprocess
import sys
import threading

OUTSIDE = (("1.1.1.1", 443), ("8.8.8.8", 53))
FAILED = 97          # exit code when the isolation check fails


def pipe(a: socket.socket, b: socket.socket) -> None:
    """Copy a -> b until a closes, then shut both down."""
    try:
        while True:
            data = a.recv(65536)
            if not data:
                break
            b.sendall(data)
    except OSError:
        pass
    finally:
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def listen_forward(port: int, sock_path: str) -> bool:
    """127.0.0.1:port -> the Unix socket at sock_path, for as long as this process lives. A port something inside
    already listens on (a nested launcher forwarding the same port, as the mwm Evolver's does) is left to it; the
    probe still checks that it answers."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind(("127.0.0.1", port))
    except OSError as exc:
        server.close()
        if exc.errno == errno.EADDRINUSE:
            return False
        raise
    server.listen(64)

    def serve():
        while True:
            client, _ = server.accept()
            upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                upstream.connect(sock_path)
            except OSError:
                client.close()
                continue
            threading.Thread(target=pipe, args=(client, upstream), daemon=True).start()
            threading.Thread(target=pipe, args=(upstream, client), daemon=True).start()
    threading.Thread(target=serve, daemon=True).start()
    return True


def probe(ports: list[int], deny: list[str] = (), keep: list[str] = (), open_network: bool = False) -> dict:
    """The isolation as seen from inside: outside TCP blocked, forwarded ports open, denied paths absent or covered.
    `keep` are the paths this command mounted on top of a covered directory: they are all it may hold. On an open
    network only the paths are checked."""
    outside = {}
    for host, port in ([] if open_network else OUTSIDE):
        try:
            socket.create_connection((host, port), timeout=3).close()
            outside[f"{host}:{port}"] = "reachable"
        except OSError as exc:
            outside[f"{host}:{port}"] = f"blocked ({type(exc).__name__})"
    forwarded = {}
    for port in ports:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=3).close()
            forwarded[str(port)] = "open"
        except OSError as exc:
            forwarded[str(port)] = f"closed ({type(exc).__name__})"
    denied = {path: _hidden(path, keep) for path in deny}
    passed = (all(v != "reachable" for v in outside.values()) and all(v == "open" for v in forwarded.values())
              and all(v in ("absent", "masked") for v in denied.values()))
    return {"passed": passed, "outside": outside, "forwarded": forwarded, "denied": denied}


def _hidden(path: str, keep: list[str] = ()) -> str:
    """absent (not there), masked (a covered file reads as empty, a covered directory holds nothing but the paths
    this command mounted on top of it), or visible."""
    if not os.path.lexists(path):
        return "absent"
    try:
        if os.path.isdir(path):
            def kept(entry):
                full = os.path.join(path, entry)
                return any(k == full or k.startswith(full + "/") for k in keep)
            return "masked" if all(kept(e) for e in os.listdir(path)) else "visible"
        st = os.stat(path)
        return "masked" if stat.S_ISCHR(st.st_mode) or (stat.S_ISREG(st.st_mode) and st.st_size == 0) else "visible"
    except OSError:
        return "absent"


def _report(sock_path: str, result: dict) -> None:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(sock_path)
            s.sendall(json.dumps(result).encode())
    except OSError:
        pass                  # the host stops listening after one report; a missing report is itself recorded


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if "--" not in argv:
        raise SystemExit("usage: sandbox_relay [--forward PORT=SOCKET] [--deny PATH] [--report SOCKET] -- COMMAND")
    split = argv.index("--")
    parser = argparse.ArgumentParser()
    parser.add_argument("--forward", action="append", default=[])
    parser.add_argument("--deny", action="append", default=[])
    parser.add_argument("--keep", action="append", default=[])
    parser.add_argument("--report")
    parser.add_argument("--open-network", action="store_true")
    args = parser.parse_args(argv[:split])
    ports = []
    for item in args.forward:
        port, _, sock_path = item.partition("=")
        listen_forward(int(port), sock_path)
        ports.append(int(port))
    result = probe(ports, args.deny, args.keep, open_network=args.open_network)
    if args.report:
        _report(args.report, result)
    if not result["passed"]:
        print(json.dumps({"type": "sandbox.isolation_failed", "probe": result}), file=sys.stderr, flush=True)
        raise SystemExit(FAILED)
    command = argv[split + 1:]
    # The command keeps the descriptors it was started with (a worker's result pipe); the relay's own sockets are
    # created non-inheritable, so nothing of the relay leaks into it.
    proc = subprocess.Popen(command, close_fds=False)
    try:
        raise SystemExit(proc.wait())
    except KeyboardInterrupt:
        proc.terminate()
        raise SystemExit(proc.wait())


if __name__ == "__main__":
    main()
