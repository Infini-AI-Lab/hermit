#!/usr/bin/env python3
"""Run the learner with the native MWM mount names, in its own network namespace.

    python mounts.py --ro SRC:/evidence ... --bind SRC:/world-model [--hide PATH] [--chdir DIR]
                     [--netns --forward PORT=SOCKET ... --probe FILE] -- codex exec ...

Mounts. The MWM contract, agenda and skills name the learner's read-only areas by their native mount points
(/evidence, /environment, /skills, /patterns) and the world model as /world-model. The framework's sandbox binds
every path at its own location, so this launcher runs inside it and starts a nested bubblewrap that re-binds the
visible root unchanged and adds those names. It grants nothing the outer sandbox does not already allow: every
source must be visible to this process. `--hide PATH` covers a path the outer sandbox shows but the native learner
never saw (the Trainer's other workspace areas, the framework's examples) with an empty directory.

Network. With --netns the nested sandbox gets its own network namespace: loopback only, no route out (natively the
learner ran under the metaworld netlock: the model API and the venue's channels, nothing else). Each `--forward
PORT=SOCKET` re-creates one host loopback port inside it (127.0.0.1:PORT, relayed over a Unix socket the Evolver
serves): the learner's model meter and the Trainer's tool endpoint. `--probe FILE` measures the isolation before the
command starts (an outside TCP connection must fail, each forwarded port must answer) and writes the result to FILE;
the command does not start unless the probe passes.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import threading

OUTSIDE = (("1.1.1.1", 443), ("8.8.8.8", 53), ("api.fireworks.ai", 443))


def _pipe(a: socket.socket, b: socket.socket) -> None:
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


def _listen_forward(port: int, sock_path: str) -> None:
    """Inside the namespace: 127.0.0.1:port -> the Unix socket."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", port))
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
            threading.Thread(target=_pipe, args=(client, upstream), daemon=True).start()
            threading.Thread(target=_pipe, args=(upstream, client), daemon=True).start()
    threading.Thread(target=serve, daemon=True).start()


def _probe(ports: list[int]) -> dict:
    outside = {}
    for host, port in OUTSIDE:
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
    passed = all(v != "reachable" for v in outside.values()) and all(v == "open" for v in forwarded.values())
    return {"passed": passed, "outside": outside, "forwarded": forwarded}


def inner(argv: list[str]) -> None:
    """Inside the nested sandbox: start the forwards, probe, then run the command and pass its exit code on."""
    split = argv.index("--")
    parser = argparse.ArgumentParser()
    parser.add_argument("--forward", action="append", default=[])
    parser.add_argument("--probe")
    args = parser.parse_args(argv[:split])
    ports = []
    for item in args.forward:
        port, _, sock_path = item.partition("=")
        _listen_forward(int(port), sock_path)
        ports.append(int(port))
    if args.probe:
        result = _probe(ports)
        with open(args.probe, "w") as f:
            json.dump(result, f)
        if not result["passed"]:
            print(json.dumps({"type": "isolation.failed", "probe": result}), flush=True)
            raise SystemExit(97)
    raise SystemExit(subprocess.call(argv[split + 1:]))


def main(argv=None) -> None:
    if argv is None:
        argv = sys.argv[1:]
    if argv[:1] == ["--inner"]:
        return inner(argv[1:])
    if "--" not in argv:
        raise SystemExit("usage: mounts.py [--ro SRC:DEST] [--bind SRC:DEST] [--hide PATH] [--chdir DIR] "
                         "[--netns --forward PORT=SOCKET --probe FILE] -- COMMAND ...")
    split = argv.index("--")
    parser = argparse.ArgumentParser()
    parser.add_argument("--ro", action="append", default=[])
    parser.add_argument("--bind", action="append", default=[])
    parser.add_argument("--hide", action="append", default=[])
    parser.add_argument("--chdir")
    parser.add_argument("--netns", action="store_true")
    parser.add_argument("--forward", action="append", default=[])
    parser.add_argument("--probe")
    args = parser.parse_args(argv[:split])
    command = argv[split + 1:]
    added = {}
    for flag, items in (("--ro-bind", args.ro), ("--bind", args.bind)):
        for item in items:
            source, _, dest = item.rpartition(":")
            if not source or not dest.startswith("/") or dest.count("/") != 1:
                raise SystemExit(f"a mount is SRC:/name (one top-level name): {item!r}")
            if not os.path.exists(source):
                raise SystemExit(f"mount source does not exist: {source}")
            added[dest] = (flag, os.path.realpath(source))
    bwrap = ["bwrap", "--die-with-parent", "--unshare-pid", "--proc", "/proc", "--dev-bind", "/dev", "/dev"]
    if args.netns:
        bwrap.append("--unshare-net")
    for name in sorted(os.listdir("/")):
        path = "/" + name
        if path in ("/proc", "/dev") or path in added:
            continue
        if os.path.islink(path):
            bwrap += ["--symlink", os.readlink(path), path]
        elif os.path.exists(path):
            bwrap += ["--bind", path, path]
    for path in args.hide:
        if os.path.isdir(path):
            bwrap += ["--tmpfs", os.path.realpath(path)]
    for dest, (flag, source) in sorted(added.items()):
        bwrap += [flag, source, dest]
    # The forward sockets and the probe file live in one directory, bound in after the hides (it may sit under one).
    for item in args.forward:
        sock_dir = os.path.dirname(item.partition("=")[2])
        bwrap += ["--bind", sock_dir, sock_dir]
        break
    if args.probe:
        probe_dir = os.path.dirname(os.path.realpath(args.probe))
        bwrap += ["--bind", probe_dir, probe_dir]
    if args.chdir:
        bwrap += ["--chdir", args.chdir]
    if args.netns or args.forward or args.probe:
        wrapper = [sys.executable, os.path.realpath(__file__), "--inner",
                   *[a for f in args.forward for a in ("--forward", f)],
                   *(["--probe", args.probe] if args.probe else []), "--"]
        command = wrapper + command
    os.execvp("bwrap", bwrap + ["--", *command])


if __name__ == "__main__":
    main()
