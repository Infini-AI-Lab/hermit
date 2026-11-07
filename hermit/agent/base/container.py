"""One CLI session in a Docker container of its own: Codex or Claude Code, for the acting agent's `environment.image`
and a CLI model endpoint's `isolation: container` (`llm: {cli: ...}`, `agent.params.model: {cli: ...}`).

The container has: the image (the CLI, ripgrep and python3; docker/codex/ builds Codex's, docker/claude/ Claude
Code's); the workspace and the session's home read-write, and the task's mounts (read-only unless a mount says
otherwise), each at the path the CLI is given; a /tmp of its own; nothing else of the machine. It sits on an internal
Docker network with no route out; its only way out is the egress proxy (docker/codex/egress_proxy.py), which opens
tunnels to the allowed hosts (the model provider) and nothing else. The CLI's own sandbox (bubblewrap) runs inside and
holds the session's commands: Codex's workspace-write sandbox, or Claude Code's (its settings: cli.CLAUDE_SANDBOX). The
commands write only the workspace, /tmp and (Codex's) parts of its home, and have no network at all; bubblewrap needs
namespaces, which Docker allows only with SYS_ADMIN, seccomp and AppArmor off. Claude Code's sandbox also gives the
commands a process namespace with a /proc of their own, so they do not see the CLI's process; the kernel mounts that
/proc only where no part of the container's is masked (systempaths=unconfined).

A secret the CLI can read from a file descriptor (Claude Code's token: `by_fd`) is passed by name like the rest of `env`,
then moved by the container's shell into a pipe and out of the environment before the CLI starts: no process in the
container has it in its environment, and the pipe is empty once the CLI has read it. Claude Code's image keeps its CLI
execute-only for the session's user, so the kernel makes the running CLI undumpable: what runs beside it outside its
sandbox (a hook, a deployment's MCP server) cannot read its memory or its descriptors either. Before a session the
agent checks that the image has Claude Code and that its sandbox starts (_claude_ready).

The session's `container` setting names the image, the network and the proxy (for the acting agent the run
config's `environment.image` and `environment.container`, for a CLI model endpoint its own `container`, as
`llm.container`); each defaults to the environment's HERMIT_CODEX_IMAGE, HERMIT_CODEX_NETWORK and HERMIT_CODEX_PROXY
(HERMIT_CLAUDE_* for Claude Code), else to the names below.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import subprocess
import threading
import time

from hermit.agent.base.interface import Mount   # noqa: F401 (re-exported)

DEFAULTS = {"image": ("HERMIT_CODEX_IMAGE", "hermit-codex:0.162.0"),
            "network": ("HERMIT_CODEX_NETWORK", "hermit-sandbox"),
            "proxy": ("HERMIT_CODEX_PROXY", "http://hermit-egress:3128")}
CLAUDE_DEFAULTS = {"image": ("HERMIT_CLAUDE_IMAGE", "hermit-claude:2.1.318"),
                   "network": ("HERMIT_CLAUDE_NETWORK", "hermit-sandbox"),
                   "proxy": ("HERMIT_CLAUDE_PROXY", "http://hermit-egress-claude:3128")}
# Per CLI: its defaults, what builds its image, and what its own sandbox needs of the container besides SECURITY
CLIS = {"codex": (DEFAULTS, "docker/codex/Dockerfile", []),
        "claude": (CLAUDE_DEFAULTS, "docker/claude/Dockerfile", ["--security-opt", "systempaths=unconfined"])}
# Docker's default capabilities less three the sessions do not need, plus what bubblewrap needs to make namespaces
SECURITY = ["--cap-drop", "NET_RAW", "--cap-drop", "MKNOD", "--cap-drop", "AUDIT_WRITE", "--cap-add", "SYS_ADMIN",
            "--security-opt", "seccomp=unconfined", "--security-opt", "apparmor=unconfined"]
LIMITS = ["--tmpfs", "/tmp:size=4g", "--pids-limit", "2048", "--memory", "6g"]
MISSING = "hermit: mount missing:"          # what the container prints, and exits 75, when a mount did not arrive
CLAUDE_MIN = (2, 1, 318)                    # Claude Code whose sandbox settings were checked here (cli.CLAUDE_SANDBOX)
PROBE_OK = "hermit: sandbox ok"


def settings(params: dict, backend: str = "codex") -> dict[str, str]:
    defaults = CLIS[backend][0]
    given = params.get("container") or {}
    unknown = set(given) - set(defaults)
    if unknown:
        raise ValueError(f"container settings: unknown keys {sorted(unknown)}; known: {sorted(defaults)}")
    return {k: str(given.get(k) or os.environ.get(env) or default) for k, (env, default) in defaults.items()}


def check(conf: dict[str, str], backend: str = "codex") -> list[str]:
    """What a session needs that is missing: docker, the image, the network; for Claude Code, also the CLI in the image
    and its sandbox able to start there (_claude_ready)."""
    docker = shutil.which("docker")
    if docker is None:
        return ["docker not found"]
    inspect = lambda *a: subprocess.run([docker, *a], capture_output=True).returncode == 0
    problems = ([f"no image {conf['image']} ({CLIS[backend][1]})"] * (not inspect("image", "inspect", conf["image"]))
                + [f"no network {conf['network']} (docker network create --internal {conf['network']})"]
                * (not inspect("network", "inspect", conf["network"])))
    return problems or (_claude_ready(docker, conf) if backend == "claude" else [])


def _claude_ready(docker: str, conf: dict[str, str]) -> list[str]:
    """One throwaway container with a session's options runs the image's `claude --version` and bubblewrap as Claude
    Code's sandbox runs it (namespaces of its own, a fresh /proc). Claude Code refuses to start only when bubblewrap or
    socat is missing; a bubblewrap that is there but cannot start would fail every command of every session, and the
    sessions would still end as served."""
    script = ("claude --version && bwrap --unshare-all --die-with-parent --ro-bind / / --proc /proc --dev /dev true "
              f"&& echo {shlex.quote(PROBE_OK)}")
    cmd = [docker, "run", "--rm", "--name", f"hermit-check-{secrets.token_hex(3)}", "--label", "hermit-session=1",
           "--network", "none", "--read-only", *LIMITS, *SECURITY, *CLIS["claude"][2],
           "--user", f"{os.getuid()}:{os.getgid()}", conf["image"], "sh", "-c", script]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=180, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return [f"the image {conf['image']} did not run a check in 180 s"]
    said = (out.stdout + out.stderr).strip()[-300:]
    version = re.match(r"\s*(\d+)\.(\d+)\.(\d+)", out.stdout)
    if version is None:
        return [f"no Claude Code in the image {conf['image']} ({CLIS['claude'][1]}): {said}"]
    if tuple(map(int, version.groups())) < CLAUDE_MIN:
        return [f"the image's Claude Code is {'.'.join(version.groups())}; a container needs "
                f"{'.'.join(map(str, CLAUDE_MIN))} or later ({CLIS['claude'][1]})"]
    if PROBE_OK not in out.stdout:
        return [f"Claude Code's sandbox cannot start in the image's container: {said}"]
    return []


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


NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _by_fd(by_fd: dict[str, str]) -> str:
    """The container shell's lines that move each secret (a variable of `env`, by name) into a pipe on a descriptor of
    its own (3, 4, ...), name that descriptor in the variable the CLI reads it from, and take the secret out of the
    environment: the CLI and everything it starts are run without it. (A here-document is a pipe in the image's sh,
    dash, and the shell closes its writing end: the CLI reads the secret to the end once, and then the pipe is empty.)"""
    lines = []
    for fd, (secret, named) in enumerate(sorted(by_fd.items()), start=3):
        if not (NAME.fullmatch(secret) and NAME.fullmatch(named)):
            raise ValueError(f"not variable names: {secret!r}, {named!r}")
        lines.append(f"exec {fd}<<HERMIT_FD\n${{{secret}}}\nHERMIT_FD\nunset {secret}; {named}={fd}; export {named}\n")
    return "".join(lines)


def run(command: list[str], *, conf: dict[str, str], work: Path, home: Path, mounts: list[Mount], env: dict[str, str],
        timeout: float | None, stdout: Path, cancel=None, stdin: str | None = None, backend: str = "codex",
        by_fd: dict[str, str] | None = None) -> tuple[int, bool, bool]:
    """`command` in a fresh container, its stdout and stderr to `stdout`; returns (exit code, timed out, cancelled).
    `env` is passed by name (the values stay out of the command line); each variable `by_fd` names ({secret: the
    variable that gets its descriptor}) reaches the CLI as a descriptor instead (_by_fd). A session past `timeout`
    seconds, or once `cancel` (a threading.Event) is set, or whose caller is interrupted, is stopped and its container
    removed. The session runs as this user, not as the container's root: the CLI's bubblewrap sandbox makes mount points
    in the workspace from inside a user namespace, where root cannot write a directory this user owns, and the files the
    session leaves stay this user's. `stdin` (a long prompt) is written to the session's stdin by a thread of its own
    and then closed; without it the stdin is empty."""
    missing = set(by_fd or ()) - set(env)
    if missing:
        raise ValueError(f"by_fd names variables env does not pass: {sorted(missing)}")
    prologue = _by_fd(by_fd or {})
    docker = shutil.which("docker")
    if docker is None:
        raise RuntimeError("hermit: docker not found")
    work, home = Path(work).resolve(), Path(home).resolve()
    name = f"hermit-{work.parent.name[:40]}-{secrets.token_hex(3)}"
    check_mounts = "".join(f"test -e {shlex.quote(str(m.target))} || {{ echo {shlex.quote(MISSING)} "
                           f"{shlex.quote(str(m.target))} >&2; exit 75; }}; " for m in mounts)
    cmd = [docker, "run", "--rm", *(["-i"] if stdin is not None else []), "--name", name, "--label", "hermit-session=1", "--network", conf["network"],
           "--read-only", *LIMITS, *SECURITY, *CLIS[backend][2], "--user", f"{os.getuid()}:{os.getgid()}",
           *_bind(work, work, False), *_bind(home, home, False),
           *(a for m in mounts for a in _bind(m.source, m.target, m.readonly)),
           "-w", str(work), "-e", f"HOME={home}", "-e", f"HTTPS_PROXY={conf['proxy']}",
           *(a for k in env for a in ("-e", k)),
           conf["image"], "sh", "-c", check_mounts + prologue + 'exec "$@"', "sh", *command]
    deadline = time.time() + timeout if timeout else None
    with open(stdout, "w") as out:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL if stdin is None else subprocess.PIPE, stdout=out,
                                stderr=subprocess.STDOUT, env={**os.environ, **env}, text=True)
        if stdin is not None:
            def feed():
                try:
                    proc.stdin.write(stdin)
                except (BrokenPipeError, OSError, ValueError):
                    pass
                finally:                  # closed however the write went: the CLI reads its stdin to the end
                    try:
                        proc.stdin.close()
                    except (BrokenPipeError, OSError, ValueError):
                        pass
            threading.Thread(target=feed, daemon=True).start()
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
