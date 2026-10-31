"""A learner call as one `codex exec` session under the user's Codex login (a ChatGPT account), for Evolvers whose
`learner: codex` replaces the run's `llm` tool (ACE).

The prompt goes on stdin; the reply is Codex's last message. A Codex session can run commands, and Codex's
read-only mode limits writes, not reads, so each session is confined to two new directories of its own, removed after
it: an empty working directory and a Codex home holding only a copy of the login. It sees no file of the machine and
no earlier session's history. Where it runs:

- `learner_container: {image, network, proxy}`: in a fresh container of that image (it must have `codex` on PATH),
  on that Docker network, reaching its provider through that proxy, with only the two directories mounted;
- otherwise in the run's sandbox, which confines it with bubblewrap. A sandbox that would run it unconfined
  (`enforce: best_effort` without bubblewrap, as on macOS) refuses instead: set `learner_container`.

The run keeps one copy of the login (`<run>/workspace/agent_home/auth.json`), taken from the user's when it is missing
or the user's is newer (a later `codex login`); each session starts from it, and a token the session refreshed is
written back to it for the next. The Evolver's finalize removes it."""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import time
from pathlib import Path

from hermit.evolver.agent import parse_log
from hermit.evolver.base import StepContext, Usage

# In a container, Codex's home lives in the container's memory, seeded from the session's home: what Codex writes there
# (its plugin catalog, its databases, tens of MB a session) never reaches the machine's disk, where Docker Desktop's
# file sharing keeps a deleted file's space held. Its session record and a refreshed login come back.
CODEX_HOME_IN = "/codex-home"
SEEDED = ('seed="$1"; shift; cp -a "$seed"/. /codex-home/ || exit 75; "$@"; rc=$?; '
          'mkdir -p "$seed/sessions" && cp -a /codex-home/sessions/. "$seed/sessions/" 2>/dev/null; '
          'test -f /codex-home/auth.json && cp -p /codex-home/auth.json "$seed/auth.json"; exit $rc')

# Docker's default capabilities less three a session does not need, plus what Codex's own sandbox (bubblewrap)
# needs to make namespaces inside the container
SECURITY = ["--cap-drop", "NET_RAW", "--cap-drop", "MKNOD", "--cap-drop", "AUDIT_WRITE", "--cap-add", "SYS_ADMIN",
            "--security-opt", "seccomp=unconfined", "--security-opt", "apparmor=unconfined"]


# Codex's own reports of a provider it could not reach (after its own retries): the session is run again
UNAVAILABLE = re.compile(r"unexpected status (?:429|5\d\d)|exceeded retry limit|stream disconnected|"
                         r"error sending request|too many requests|service unavailable|bad gateway|"
                         r"routing discovery failed", re.IGNORECASE)


def last_refresh(path: Path) -> str:
    try:
        return str(json.loads(path.read_text()).get("last_refresh") or "")
    except (OSError, ValueError):
        return ""


def home_of(ctx: StepContext) -> Path:
    """Where the run keeps its copy of the login for the learner."""
    return ctx.workspace.state.parent / "agent_home"


def unconfined(ctx: StepContext) -> str | None:
    """Why a learner session would run unconfined here, or None."""
    if (ctx.params.get("learner_container") or {}).get("image"):
        return None
    box = ctx.sandbox
    if box is not None and getattr(box, "enabled", False) and not shutil.which("bwrap"):
        return ("learner: codex runs commands, and this sandbox cannot confine them (no bubblewrap): set "
                "evolver.params.learner_container {image, network, proxy} to run each session in a container")
    return None


def ask(ctx: StepContext, prompt: str, stem: Path) -> tuple[str, list[Usage]]:
    """One session: `stem`.codex.jsonl is its event stream, `stem`.last.txt its reply. Raises on a failed session."""
    p, ws = ctx.params, ctx.workspace
    reason = unconfined(ctx)
    if reason:
        raise RuntimeError(reason)
    home = home_of(ctx)
    home.mkdir(parents=True, exist_ok=True)
    source = Path(os.path.expanduser(str(p.get("learner_auth") or "~/.codex/auth.json")))
    mine = home / "auth.json"
    if not source.is_file() and not mine.is_file():
        raise FileNotFoundError(f"no Codex login at {source}: run `codex login` (or set learner_auth)")
    if source.is_file() and (not mine.is_file() or last_refresh(source) > last_refresh(mine)):
        shutil.copy2(source, mine)
        os.chmod(mine, 0o600)
    # A provider the session could not reach (an outage of the network): run again, `learner_unavailable_attempts`
    # times in all, `learner_unavailable_wait_s` apart.
    attempts = max(1, int(p.get("learner_unavailable_attempts") or 1))
    log = Path(f"{stem}.codex.jsonl")
    for attempt in range(attempts):
        # New directories for every session, removed after it: nothing of an earlier session to read, and no path
        # reused (Docker Desktop's file sharing can keep a removed directory's handle for a re-created one).
        session = ws.scratch / "codex_learner" / secrets.token_hex(6)
        work, session_home = session / "work", session / "home"
        work.mkdir(parents=True)
        session_home.mkdir()
        shutil.copy2(mine, session_home / "auth.json")
        os.chmod(session_home / "auth.json", 0o600)
        try:
            return _session(ctx, prompt, stem, session_home, work)
        except RuntimeError:
            unavailable = log.is_file() and UNAVAILABLE.search(log.read_text(errors="replace"))
            if attempt == attempts - 1 or not unavailable:
                raise
        finally:
            _keep_refreshed(session_home / "auth.json", mine)
            shutil.rmtree(session, ignore_errors=True)
        wait = float(p.get("learner_unavailable_wait_s") or 120)
        if ctx.cancel is not None and ctx.cancel.wait(wait):
            raise RuntimeError("cancelled while waiting for the provider")
        if ctx.cancel is None:
            time.sleep(wait)
    raise AssertionError("unreachable")


def _session(ctx: StepContext, prompt: str, stem: Path, home: Path, work: Path) -> tuple[str, list[Usage]]:
    p = ctx.params
    last, log = Path(f"{stem}.last.txt"), Path(f"{stem}.codex.jsonl")
    reply = work / ".last_message.txt"
    args = ["exec", "--json", "--skip-git-repo-check", "--ignore-user-config", "-s", "read-only",
            "-C", str(work.resolve()), "-o", str(reply.resolve())]
    if p.get("learner_model"):
        args += ["-m", str(p["learner_model"])]
    if p.get("learner_effort"):
        args += ["-c", f'model_reasoning_effort="{p["learner_effort"]}"']
    args.append("-")
    container = p.get("learner_container") or {}
    name = None
    if container.get("image"):
        docker = shutil.which("docker")
        if docker is None:
            raise RuntimeError("learner_container needs docker on PATH")
        name = f"hermit-learner-{secrets.token_hex(4)}"
        h, w = home.resolve(), work.resolve()
        command = [docker, "run", "--rm", "-i", "--name", name, "--network", str(container.get("network") or "none"),
                   "--read-only", "--tmpfs", "/tmp:size=1g", "--pids-limit", "512", "--memory", "2g", *SECURITY,
                   "--user", f"{os.getuid()}:{os.getgid()}",
                   "--mount", f"type=bind,src={h},dst={h}", "--mount", f"type=bind,src={w},dst={w}", "-w", str(w),
                   "--tmpfs", f"{CODEX_HOME_IN}:size=512m,mode=0700,uid={os.getuid()},gid={os.getgid()}",
                   "-e", f"HOME={CODEX_HOME_IN}", "-e", f"CODEX_HOME={CODEX_HOME_IN}",
                   *(["-e", f"HTTPS_PROXY={container['proxy']}"] if container.get("proxy") else []),
                   str(container["image"]), "sh", "-c", SEEDED, "sh", str(h), "codex", *args]
        env = dict(os.environ)
    else:
        command = ["codex", *args]
        env = dict(os.environ)
        env["CODEX_HOME"] = str(home)
        if ctx.sandbox is not None:
            command, env = ctx.sandbox.wrap(command, writable=[home, work], env=env, home=home, keep=["CODEX_HOME"])
    timeout = float(p.get("learner_timeout_s") or 900)
    with open(log, "w") as out:
        proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=out, stderr=subprocess.STDOUT, env=env,
                                cwd=str(work), start_new_session=True)
        try:
            proc.stdin.write(prompt.encode("utf-8"))
            proc.stdin.close()
        except BrokenPipeError:
            pass
        started = time.monotonic()
        while proc.poll() is None:
            if (ctx.cancel is not None and ctx.cancel.is_set()) or time.monotonic() - started > timeout:
                if name:
                    subprocess.run([shutil.which("docker") or "docker", "rm", "-f", name],
                                   stdin=subprocess.DEVNULL, capture_output=True, check=False)
                _kill(proc, signal.SIGTERM)
                try:
                    proc.wait(15)
                except subprocess.TimeoutExpired:
                    _kill(proc, signal.SIGKILL)
                break
            time.sleep(0.5)
        code = proc.wait()
    if reply.is_file():
        shutil.move(str(reply), last)
    _, usage = parse_log(log, "codex", str(p.get("learner_model") or "codex"))
    if code != 0 or not last.is_file():
        tail = log.read_text(errors="replace")[-600:] if log.is_file() else ""
        raise RuntimeError(f"codex exec exited with {code}" + ("" if last.is_file() else " and wrote no reply")
                           + f": {tail}")
    return last.read_text(encoding="utf-8", errors="replace"), usage


def _kill(proc: subprocess.Popen, sig: int) -> None:
    """Signal the session's process group; a group that has already exited (macOS answers EPERM for zombies) needs
    nothing."""
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _keep_refreshed(session_auth: Path, master: Path) -> None:
    """A token the session refreshed (a later last_refresh) becomes the run's copy."""
    if session_auth.is_file() and last_refresh(session_auth) > last_refresh(master):
        tmp = master.with_suffix(".tmp")
        shutil.copy2(session_auth, tmp)
        os.chmod(tmp, 0o600)
        os.replace(tmp, master)
