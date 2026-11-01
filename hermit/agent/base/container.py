"""One CLI session in a Docker container of its own (`agent.params.isolation: container`).

The container has: the image (a CLI, ripgrep and python3; docker/codex/Dockerfile builds Codex's); the workspace and
the session's home read-write, and the task's mounts (read-only unless a mount says otherwise), each at the path the
CLI is given; a /tmp of its own; nothing else of the machine. It sits on an internal Docker network with no route out;
its only way out is the egress proxy (docker/codex/egress_proxy.py), which opens tunnels to the allowed hosts (the
model provider) and nothing else. Codex's own workspace-write sandbox (bubblewrap) runs inside, so the session's
commands write only the workspace, /tmp and parts of its home, and have no network at all; bubblewrap needs
namespaces, which Docker allows only with SYS_ADMIN, seccomp and AppArmor off.

`agent.params.container` names the image, the network and the proxy; each defaults to the environment's
HERMIT_CODEX_IMAGE, HERMIT_CODEX_NETWORK and HERMIT_CODEX_PROXY, else to the names below.
"""
from __future__ import annotations

import os
from pathlib import Path
import secrets
import shlex
import shutil
import subprocess
import time

from hermit.agent.base.interface import Mount   # noqa: F401 (re-exported)

DEFAULTS = {"image": ("HERMIT_CODEX_IMAGE", "hermit-codex:0.162.0"),
            "network": ("HERMIT_CODEX_NETWORK", "hermit-sandbox"),
            "proxy": ("HERMIT_CODEX_PROXY", "http://hermit-egress:3128")}
# Docker's default capabilities less three the sessions do not need, plus what bubblewrap needs to make namespaces
SECURITY = ["--cap-drop", "NET_RAW", "--cap-drop", "MKNOD", "--cap-drop", "AUDIT_WRITE", "--cap-add", "SYS_ADMIN",
            "--security-opt", "seccomp=unconfined", "--security-opt", "apparmor=unconfined"]
LIMITS = ["--tmpfs", "/tmp:size=4g", "--pids-limit", "2048", "--memory", "6g"]
MISSING = "hermit: mount missing:"          # what the container prints, and exits 75, when a mount did not arrive


def settings(params: dict) -> dict[str, str]:
    given = params.get("container") or {}
    unknown = set(given) - set(DEFAULTS)
    if unknown:
        raise ValueError(f"agent.params.container: unknown keys {sorted(unknown)}; known: {sorted(DEFAULTS)}")
    return {k: str(given.get(k) or os.environ.get(env) or default) for k, (env, default) in DEFAULTS.items()}


def check(conf: dict[str, str]) -> list[str]:
    """What a session needs that is missing: docker, the image, the network."""
    docker = shutil.which("docker")
    if docker is None:
        return ["docker not found"]
    inspect = lambda *a: subprocess.run([docker, *a], capture_output=True).returncode == 0
    return ([f"no image {conf['image']} (docker/codex/Dockerfile)"] * (not inspect("image", "inspect", conf["image"]))
            + [f"no network {conf['network']} (docker network create --internal {conf['network']})"]
            * (not inspect("network", "inspect", conf["network"])))


def gateway(conf: dict[str, str]) -> str:
    """The container network's gateway: the address of this machine a session in the container reaches (the
    framework's tool server listens there)."""
    out = subprocess.run([shutil.which("docker") or "docker", "network", "inspect", conf["network"], "-f",
                          "{{range .IPAM.Config}}{{.Gateway}} {{end}}"], capture_output=True, text=True)
    found = out.stdout.split()
    if out.returncode or not found:
        raise RuntimeError(f"no gateway for docker network {conf['network']}: {out.stderr.strip()[:300]}")
    return found[0]


def _bind(source: Path, target: Path, readonly: bool) -> list[str]:
    return ["--mount", f"type=bind,src={Path(source).resolve()},dst={target}" + (",readonly" if readonly else "")]


def run(command: list[str], *, conf: dict[str, str], work: Path, home: Path, mounts: list[Mount], env: dict[str, str],
        timeout: float | None, stdout: Path, cancel=None) -> tuple[int, bool, bool]:
    """`command` in a fresh container, its stdout and stderr to `stdout`; returns (exit code, timed out, cancelled).
    `env` is passed by name (the values stay out of the command line). A session past `timeout` seconds, or once
    `cancel` (a threading.Event) is set, or whose caller is interrupted, is stopped and its container removed. The
    session runs as this user, not as the container's root: Codex's bubblewrap sandbox makes mount points in the
    workspace from inside a user namespace, where root cannot write a directory this user owns, and the files the
    session leaves stay this user's."""
    docker = shutil.which("docker")
    if docker is None:
        raise RuntimeError("hermit: docker not found")
    work, home = Path(work).resolve(), Path(home).resolve()
    name = f"hermit-{work.parent.name[:40]}-{secrets.token_hex(3)}"
    check_mounts = "".join(f"test -e {shlex.quote(str(m.target))} || {{ echo {shlex.quote(MISSING)} "
                           f"{shlex.quote(str(m.target))} >&2; exit 75; }}; " for m in mounts)
    cmd = [docker, "run", "--rm", "--name", name, "--label", "hermit-session=1", "--network", conf["network"],
           "--read-only", *LIMITS, *SECURITY, "--user", f"{os.getuid()}:{os.getgid()}",
           *_bind(work, work, False), *_bind(home, home, False),
           *(a for m in mounts for a in _bind(m.source, m.target, m.readonly)),
           "-w", str(work), "-e", f"HOME={home}", "-e", f"HTTPS_PROXY={conf['proxy']}",
           *(a for k in env for a in ("-e", k)),
           conf["image"], "sh", "-c", check_mounts + 'exec "$@"', "sh", *command]
    deadline = time.time() + timeout if timeout else None
    with open(stdout, "w") as out:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                env={**os.environ, **env})
        timed_out = cancelled = False
        try:
            while proc.poll() is None:
                timed_out = deadline is not None and time.time() > deadline
                cancelled = cancel is not None and cancel.is_set()
                if timed_out or cancelled:
                    break
                time.sleep(0.2)
        finally:
            if proc.poll() is None:              # timed out, cancelled, or the caller was interrupted
                subprocess.run([docker, "rm", "-f", name], stdin=subprocess.DEVNULL, capture_output=True)
                try:
                    proc.wait(30)
                except subprocess.TimeoutExpired:
                    proc.kill()
        code = proc.wait()
    return (-1 if timed_out else code), timed_out, cancelled
