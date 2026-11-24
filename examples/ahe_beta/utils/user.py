"""Codex in a task's Harbor environment, with a user who talks to it while it works: this example's acting agent.

    agent:
      name: ../utils/user.py:UserCodexAgent
      base: codex                              # Hermit's Codex agent (hermit.agent.acting.workspace): every setting
      model: {name: gpt-5.6-luna, login: chatgpt, effort: medium}   # of it applies as it is
      codex: {web_search: false, bin: /opt/hermit-codex/bin/codex, version: 0.170.0}
      params:
        user:
          simulate: scripted_user:simulate     # module:function (importable from `pythonpath`) or file.py:function
          pythonpath: []                       # folders put on sys.path first (an AHE-Beta checkout's backend/)
          params: {}                           # handed to simulate, under the task's own assignment
          assignments: null                    # a JSON file {row id: assignment} (its persona, round, ...), read on
                                               # this machine, so a learner reading the rows never sees it; else
          task_field: user                     # the row's field that holds it
          wait_s: 3600                         # how long a Stop waits for the user's next message
          exit_wait_s: 120                     # how long Codex has to exit once the user ends the session

Without `user` it is Hermit's Codex agent as it is. The Codex CLI is the environment's: a task's own class is not
asked what to install (hermit.isolate), so `bin` names one the environment mounts (`environment.harbor.environment.
mounts`; a static release of this machine, as ../codex/run.yaml does).

The function the user's side writes:

    def simulate(task: dict, agent: Conversation, workspace: Workspace, params: dict) -> dict

    task       {"id": the row's id, "name": the Harbor task's name, "instruction": its instruction.md}
    agent      the running Codex session: write / poll / send / events / interrupt / close (channel.Conversation)
    workspace  the task's environment, as the user reaches it: run / read / write / quiesce (Workspace)
    params     user.params, the row's assignment, `session_id`, `artifact_root` (a private folder of this machine) and
               `adapter_sha256` (the adapter the session runs, None without one)
    returns    {"score": a number in [0, 1], "rating", "outcome", "reason", "issues", "feedback", "usage", "files",
               ...}

The first `write` starts Codex with that message as its prompt: one `codex exec` for the whole conversation. Three
hooks (user_hook.py) carry the conversation through a bridge (channel_server.py), both installed as root outside the
workspace: PostToolUse inserts a message while the agent works, PreToolUse denies tool calls after an interrupt, Stop
hands the reply to the user and continues the session with the user's next message. The adapter's own hooks stay; its
Stop hooks run inside the user's Stop hook, first, since Codex runs the hooks of one event at the same time.

What the run keeps:

    log/user-transcript.jsonl   every message, reply and tool event the user could see, with times
    log/user-verdict.json       what a learner may read of the verdict (VISIBLE: score, rating, outcome, reason, ...)
    log/user-reward.txt         the score, for a task whose verifier copies it (AHE-Beta's exported tasks)
    <workspaces>/user/<run>/    everything else simulate left (its session record, the persona, its private checks):
                                never in the log, so never in a learner's material
    RunOutput.extra["user"]     the verdict a judge reads (judge.py)

A session whose user failed (simulate raised, or returned no valid score) is not the agent's result: `extra.infra`
(`user_failed`), which the run runs again in a fresh environment and, failing again, records unscored; so is one whose
Codex session was infra. A session the adapter failed before Codex started stays the agent's result.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import inspect
import json
import math
import os
from pathlib import Path, PurePosixPath
import shlex
import sys
import tempfile
import threading
import time
import uuid

from hermit.agent.acting.workspace import CliAgent
from hermit.common import RunOutput, load_callable
from hermit.environment.base import Environment, Unsupported

sys.path.insert(0, str(Path(__file__).resolve().parent))
from channel import Conversation  # noqa: E402

HERE = Path(__file__).resolve().parent
ROOT = "/run/ahe-user-channel"                       # the bridge's folder in the environment (root's)
SOCKET = ROOT + "/channel.sock"
HOOK = ROOT + "/user_hook.py"
SERVER = "/usr/local/libexec/ahe-user-channel.py"
SERVICES = "/run/ahe-services"                       # root's service markers, which the quiesce leaves running
PID_FILE = SERVICES + "/ahe-user-channel.pid"
EVENTS = ("PreToolUse", "PostToolUse", "Stop")
FRAMES = {
    "reply_frame": "The user says:\n{text}",
    "insert_frame": "The user sent this while you were working:\n{text}",
    "interrupt_frame": "The user interrupted you: {text}\nStop here. Do not call any more tools. Say in one or "
                       "two sentences where things stand, then wait for the user's next message.",
}
VISIBLE = ("score", "rating", "score_kind", "outcome", "reason", "issues", "feedback")   # what a learner may read
RESERVED = {"transcript.jsonl", "verdict.json", "reward.txt"}
MARK = "hermit-user-run:"


class UserFailed(RuntimeError):
    """The user's side failed: no verdict of the agent's session (not the agent's result)."""


def load(spec: dict, base: Path):
    """The simulate function `user.simulate` names, with `user.pythonpath` on sys.path first."""
    for folder in spec.get("pythonpath") or []:
        path = Path(os.path.expandvars(os.path.expanduser(str(folder))))
        path = path if path.is_absolute() else (Path(base) / path).resolve()
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    name = str(spec.get("simulate") or "")
    if ":" not in name:
        raise ValueError(f"user.simulate is module:function or file.py:function, not {name!r}")
    return load_callable(name, Path(base))


def fingerprint(adapter: Path | None) -> str | None:
    """The adapter's files, as one digest: which adapter a user's session ran (the simulator's provenance)."""
    if adapter is None:
        return None
    digest = hashlib.sha256()
    root = Path(adapter)
    for path in sorted(p for p in root.rglob("*") if p.is_file() and ".git" not in p.relative_to(root).parts):
        digest.update(path.relative_to(root).as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


class UserCodexAgent(CliAgent):
    """Hermit's Codex agent; with `params.user`, a session the user leads (module docstring)."""

    def __init__(self, params=None, base=None):
        super().__init__(params, base)
        self._lock = threading.Lock()
        self._assignments: tuple[Path, dict] | None = None

    def run(self, task: dict, adapter: Path | None, environment=None) -> RunOutput:
        spec = self.params.get("user")
        if not spec:
            return super().run(task, adapter, environment)
        if environment is None or environment.isolation != "remote":
            raise Unsupported("a session with a user runs in the task's own environment (environment.harbor): the "
                              "channel lives there")
        simulate = load(spec, self.base)                 # a wrong name fails before the session
        codex = CliAgent({k: v for k, v in self.params.items() if k not in ("user", "prompt")}, self.base)
        return _Session(codex, task, adapter, environment, spec, self.private_root(),
                        self.assignment(task, spec)).run(simulate)

    def assignment(self, task: dict, spec: dict) -> dict:
        """This task's assignment: its entry in `user.assignments`, else the row's `user.task_field`."""
        if spec.get("assignments"):
            path = self.path(os.path.expandvars(str(spec["assignments"])))
            with self._lock:
                if self._assignments is None or self._assignments[0] != path:
                    self._assignments = (path, json.loads(path.read_text(encoding="utf-8")))
            found = self._assignments[1].get(str(task.get("id")))
            if found is None:
                raise ValueError(f"{path} has no assignment for the task {task.get('id')!r}")
        else:
            found = task.get(str(spec.get("task_field") or "user")) or {}
        if not isinstance(found, dict):
            raise ValueError(f"the task {task.get('id')!r}'s user assignment must be a mapping")
        return found

    def private_root(self) -> Path:
        root = Path(os.path.expanduser(str(self.params.get("workspaces", ".logs/workspaces"))))
        return (root if root.is_absolute() else self.base / root).resolve() / "user"


class _Session:
    """One task's conversation: the bridge and the hooks in the environment, Codex in a thread of its own, the user's
    function in the caller's."""

    def __init__(self, codex: CliAgent, task: dict, adapter, environment, spec: dict, private_root: Path,
                 assignment: dict):
        self.codex, self.task, self.adapter, self.environment, self.spec = codex, task, adapter, environment, spec
        self.assignment = assignment
        self.wait_s = float(spec.get("wait_s") or 3600)
        self.exit_wait_s = float(spec.get("exit_wait_s") or 120)
        name = "".join(c if c.isalnum() or c in "-_." else "_" for c in str(task.get("id", "task")))[:80]
        self.session_id = f"{name}-{uuid.uuid4().hex[:12]}"
        self.private = private_root / self.session_id
        self.settings = {"channel": SOCKET, "wait_s": self.wait_s, "adapter_stop": [],
                         **{key: str((spec.get("frames") or {}).get(key) or text) for key, text in FRAMES.items()}}
        self.first: str | None = None
        self.thread: threading.Thread | None = None
        self.output: RunOutput | None = None
        self.codex_error: BaseException | None = None
        self.baseline: dict | None = None
        self.stopped = False
        self.stop_lock = threading.Lock()
        self.conversation: Conversation | None = None

    # --- the environment's side -----------------------------------------------------------------------------------
    def root(self, command: str, timeout: float = 60, stdin: str | None = None) -> str:
        """A command as root there (the bridge's own operations); its output, or RuntimeError."""
        done = self.environment.exec(["bash", "-c", command], cwd="/", user="root", timeout=timeout, stdin=stdin)
        if done.code != 0:
            raise RuntimeError(f"the user channel's command failed ({done.code}): {done.output[-500:]}")
        return done.output

    def install(self, text: str, target: str, mode: str) -> None:
        """A file of root's there, which the agent's user cannot change: uploaded (as the agent user's), then copied."""
        staged = f"/tmp/.ahe-user-{uuid.uuid4().hex}"
        with tempfile.TemporaryDirectory(prefix="ahe-user-") as tmp:
            local = Path(tmp) / "file"
            local.write_text(text, encoding="utf-8")
            self.environment.upload(local, staged)
        self.root(f"install -D -o root -g root -m {mode} {shlex.quote(staged)} {shlex.quote(target)} && "
                  f"rm -f {shlex.quote(staged)}")

    def start_channel(self) -> None:
        """The bridge, its hook and its settings, as root, outside the workspace; then the processes already running,
        which the session's end leaves alone (processes.py)."""
        if self.environment.exec(["sh", "-c", "command -v python3"], timeout=60).code != 0:
            raise UserFailed("the user channel needs python3 in the task's environment")
        self.install((HERE / "channel_server.py").read_text(), SERVER, "0755")
        self.install((HERE / "user_hook.py").read_text(), HOOK, "0555")
        self.install(json.dumps(self.settings, indent=1), ROOT + "/user_hook.json", "0444")
        q = shlex.quote
        self.root(f"mkdir -p {q(SERVICES)} && chmod 0755 {q(ROOT)} && chmod 0700 {q(SERVICES)} && "
                  f"rm -f {q(SOCKET)} {q(ROOT + '/STOP')} {q(PID_FILE)} && "
                  f"(setsid python3 -B {q(SERVER)} serve --root {q(ROOT)} --pid-file {q(PID_FILE)} "
                  f"> {q(ROOT + '/server.log')} 2>&1 < /dev/null &) && "
                  f"for i in $(seq 1 100); do test -S {q(SOCKET)} && exit 0; sleep 0.1; done; "
                  "echo 'the user channel did not start' >&2; cat " + q(ROOT + "/server.log") + " >&2; exit 1")
        script = (HERE / "processes.py").read_text() + "\nimport json\nprint(json.dumps(snapshot()))\n"
        self.baseline = json.loads(self.root("python3 -B -", stdin=script).strip().splitlines()[-1])
        self.install(json.dumps({"hooks": self.merged({})}, indent=2), self.hooks_path(), "0444")
        self.lock_hooks()

    def hooks_path(self) -> str:
        return str(PurePosixPath(str(self.environment.workspace)) / ".codex" / "hooks.json")

    def merged(self, adapter: dict) -> dict:
        """The adapter's hooks with the user's: its Stop hooks run by the user's Stop hook (`adapter_stop`), first."""
        stops = []
        for group in adapter.get("Stop") or []:
            if group.get("matcher") or any(h.get("type") != "command" for h in group.get("hooks") or []):
                raise ValueError("the adapter's Stop hooks run inside the user's Stop hook, one after another: they "
                                 "must be command hooks without a matcher")
            stops += list(group.get("hooks") or [])
        self.settings["adapter_stop"] = stops
        timeout = int(self.wait_s + sum(float(h.get("timeout") or 60) for h in stops) + 60)
        merged = {event: list(groups) for event, groups in adapter.items() if event != "Stop"}
        for event in EVENTS:
            merged.setdefault(event, []).append({"hooks": [{
                "type": "command", "command": f"python3 -B {shlex.quote(HOOK)} {event}",
                "timeout": timeout if event == "Stop" else 30}]})
        return merged

    def deployed(self, staging: Path) -> None:
        """The adapter's `.codex/` as the deployment staged it, before its upload: its hooks merged with the user's."""
        path = staging / "hooks.json"
        adapter = json.loads(path.read_text()).get("hooks") or {} if path.is_file() else {}
        path.write_text(json.dumps({"hooks": self.merged(adapter)}, indent=2) + "\n")
        self.install(json.dumps(self.settings, indent=1), ROOT + "/user_hook.json", "0444")

    def lock_hooks(self) -> None:
        """`.codex/` root's and read-only: the agent's commands cannot drop the user's hooks or add their own."""
        folder = shlex.quote(str(PurePosixPath(self.hooks_path()).parent))
        self.root(f"chown -R root:root {folder} && chmod -R a+rX,go-w {folder} && "
                  f"chmod 0444 {shlex.quote(self.hooks_path())}")

    # --- Codex -------------------------------------------------------------------------------------------------------
    def start(self, text: str) -> None:
        """The user's first message: Codex starts with it as its prompt."""
        self.first = text
        self.thread = threading.Thread(target=self._codex, name=f"codex-{self.session_id}", daemon=True)
        self.thread.start()

    def _codex(self) -> None:
        try:
            self.output = self.codex.run(self.task, self.adapter, _Hooked(self.environment, self))
        except BaseException as exc:                     # noqa: BLE001 - read by run()
            self.codex_error = exc

    def alive(self) -> bool:
        return self.thread is None or self.thread.is_alive()

    def stop(self) -> None:
        """The session over: the user's end sent, Codex given exit_wait_s to exit, then whatever the rollout started
        stopped (the bridge and the task's own services stay), so that what the user reviews is frozen."""
        with self.stop_lock:
            if self.stopped:
                return
            if self.conversation is not None:
                with contextlib.suppress(Exception):    # a channel that is gone: the processes are stopped anyway
                    self.conversation.end()
            if self.thread is not None:
                self.thread.join(self.exit_wait_s)
            if self.baseline is not None:
                script = (HERE / "processes.py").read_text() + f"\nquiesce({self.baseline!r})\n"
                self.root("python3 -B -", stdin=script, timeout=60)
            if self.thread is not None:
                self.thread.join(120)
                if self.thread.is_alive():
                    raise RuntimeError("Codex did not stop after its processes were ended")
            self.stopped = True

    def stop_channel(self) -> None:
        q = shlex.quote
        with contextlib.suppress(Exception):
            self.root(f"touch {q(ROOT + '/STOP')}; if [ -f {q(PID_FILE)} ]; then "
                      f"kill $(cut -d: -f1 {q(PID_FILE)}) 2>/dev/null || true; fi", timeout=30)

    # --- the session -------------------------------------------------------------------------------------------------
    def run(self, simulate) -> RunOutput:
        started = time.time()
        task = {"id": self.task.get("id"), "name": self.environment.task_dir.name,
                "instruction": self.environment.instruction}
        self.private.mkdir(parents=True, exist_ok=True)
        params = {**dict(self.spec.get("params") or {}), **self.assignment, "session_id": self.session_id,
                  "artifact_root": str(self.private), "adapter_sha256": fingerprint(self.adapter)}
        verdict, error = None, None
        try:
            self.start_channel()
            self.conversation = Conversation(None, self.start, self.alive, self.stop, transport=_Channel(self))
            verdict = simulate(task, self.conversation, Workspace(self), params)
            if not isinstance(verdict, dict):
                raise UserFailed(f"simulate returned {type(verdict).__name__}, not a dict")
            score = verdict.get("score")
            if score is None or isinstance(score, bool) or not isinstance(score, (int, float)) or \
                    not math.isfinite(score) or not 0 <= score <= 1:
                raise UserFailed(f"simulate's score must be a number in [0, 1], not {score!r}")
        except Exception as exc:                         # noqa: BLE001 - the user's failure: recorded below
            if "AgentPhaseOver" in f"{type(exc).__name__}: {exc}":
                self.stop_channel()                      # Harbor's agent timeout ended the phase: the agent's time
                raise                                    # ran out (the user's shares it), the run's to read
            error = exc
        finally:
            try:
                self.stop()
            except Exception as exc:                     # noqa: BLE001 - a session that would not stop: no verdict
                error = error or exc
            self.stop_channel()
        return self.result(verdict, error, started)

    def result(self, verdict: dict | None, error: BaseException | None, started: float) -> RunOutput:
        out = self.output
        if self.codex_error is not None and not isinstance(self.codex_error, Exception):
            raise self.codex_error                       # an interrupt, not a failure: never a record
        if self.codex_error is not None:                 # the environment's or this machine's: the run's to classify
            raise self.codex_error
        transcript = self.conversation.transcript if self.conversation is not None else []
        if out is None:                                  # Codex never started (the user failed first)
            log = self.private / "log"
            log.mkdir(parents=True, exist_ok=True)
            out = RunOutput(output=None, log=log, cost_usd=0.0, wall_s=0.0, tokens=0, api_speed={"model_calls": 0})
        log = Path(out.log)
        extra = dict(out.extra or {})
        kept = {k: v for k, v in (verdict or {}).items() if k not in ("files", "record")}
        for name, text in ((verdict or {}).get("files") or {}).items():   # the user's own records: private
            if Path(str(name)).name != name or name in RESERVED:
                error = error or UserFailed(f"simulate's file {name!r} is reserved or names a folder")
                continue
            (self.private / name).write_text(str(text), encoding="utf-8")
        if verdict and verdict.get("record") is not None:
            (self.private / "record.json").write_text(json.dumps(verdict["record"], indent=1, ensure_ascii=False,
                                                                 default=str), encoding="utf-8")
        visible = {k: kept[k] for k in VISIBLE if k in kept}
        if error is not None:
            visible["error"] = f"{type(error).__name__}: {error}"[-2000:]
        _write(log / "user-transcript.jsonl", "".join(json.dumps(e, ensure_ascii=False, default=str) + "\n"
                                                      for e in transcript))
        _write(log / "user-verdict.json", json.dumps(visible, indent=1, ensure_ascii=False, default=str) + "\n")
        (log / "user-reward.txt").unlink(missing_ok=True)
        said = [e for e in transcript if e.get("from") == "user"]
        extra["user"] = {**kept, "session_id": self.session_id, "private": str(self.private),
                         "messages": sum(1 for e in said if e.get("kind") == "message"),
                         "interrupts": sum(1 for e in said if e.get("kind") == "interrupt"),
                         "inserted": sum(1 for e in transcript if e.get("kind") == "delivered"
                                         and e.get("at") == "PostToolUse"),
                         "replies": sum(1 for e in transcript if e.get("kind") == "reply"),
                         **({"error": visible["error"]} if error is not None else {})}
        if extra.get("infra") or extra.get("adapter_failed"):
            out.extra = extra                            # the session's own word stands (infra, or the adapter's)
            return out
        if error is not None:                            # the user failed: not the agent's result
            extra.update(infra="user_failed", infra_final=False,
                         exception_message=f"the user's session failed: {visible['error']}"[-2000:])
            out.extra = extra
            return out
        _write(log / "user-reward.txt", f"{float(verdict['score']):g}\n")
        out.extra = extra
        if out.wall_s is not None:
            out.wall_s = round(time.time() - started, 1)
        return out


def _write(path: Path, text: str) -> None:
    """A record of this machine's in the log, never through a link the session left there."""
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags, 0o644), "w", encoding="utf-8") as stream:
        stream.write(text)


class _Channel:
    """The bridge as the Conversation reaches it (channel.ConversationTransport): each operation one command as root
    in the environment; a read waits there for new events up to `wait` seconds (one command, not a busy loop)."""

    def __init__(self, session: _Session):
        self.session = session

    def send(self, record: dict) -> None:
        self.session.root(f"python3 -B {shlex.quote(SERVER)} enqueue --root {shlex.quote(ROOT)}", timeout=60,
                          stdin=json.dumps(record, ensure_ascii=False, default=str))

    def read(self, since: int, wait: float = 0.0) -> tuple[int, list[dict]]:
        got = self.session.root(f"python3 -B {shlex.quote(SERVER)} drain --root {shlex.quote(ROOT)} "
                                f"--value {int(since)} --wait {max(0.0, float(wait)):.1f}", timeout=float(wait) + 60)
        value = json.loads(got.strip().splitlines()[-1])
        return int(value["next"]), list(value["events"])

    def close(self) -> None:
        self.session.stop_channel()


class Workspace:
    """The task's environment, as the user reaches it: commands run as the task's agent user (the user is a person at
    the same machine), from the workspace unless `cwd` says otherwise."""

    def __init__(self, session: _Session):
        self.session = session
        self.environment = session.environment
        self.closed = False

    def _run(self, command: str, timeout: float, cwd: str | None, user: str | None) -> dict:
        if self.closed:
            raise RuntimeError("the workspace was closed")
        script = ('o=$(mktemp) && e=$(mktemp) || exit 125; bash -c "$1" >"$o" 2>"$e"; c=$?; '
                  f'printf "\\n{MARK}\\n"; base64 "$o" | tr -d "\\n"; printf "\\n"; base64 "$e" | tr -d "\\n"; '
                  'printf "\\n"; rm -f "$o" "$e"; exit $c')
        done = self.environment.exec(["bash", "-c", script, "bash", command], cwd=cwd, timeout=float(timeout),
                                     user=user)
        text = done.output
        if MARK in text:
            lines = text.rsplit(MARK, 1)[1].strip("\n").split("\n")
            decode = lambda s: base64.b64decode(s).decode("utf-8", errors="replace") if s else ""   # noqa: E731
            return {"code": done.code, "stdout": decode(lines[0] if lines else ""),
                    "stderr": decode(lines[1] if len(lines) > 1 else ""), "timed_out": done.timed_out}
        return {"code": done.code, "stdout": "", "stderr": text, "timed_out": done.timed_out}

    def run(self, command: str, timeout: float = 60.0, cwd: str | None = None) -> dict:
        """{code, stdout, stderr, timed_out} of `command` (a shell line) there."""
        return self._run(command, timeout, cwd, None)

    def read(self, path: str) -> str:
        out = self.run(f"cat -- {shlex.quote(path)}")
        if out["code"] != 0:
            raise FileNotFoundError(f"{path}: {out['stderr'].strip()}")
        return out["stdout"]

    def write(self, path: str, text: str) -> None:
        if self.closed:
            raise RuntimeError("the workspace was closed")
        with tempfile.TemporaryDirectory(prefix="ahe-user-") as tmp:
            local = Path(tmp) / (PurePosixPath(path).name or "file")
            local.write_text(text, encoding="utf-8")
            self.environment.upload(local, path)

    def quiesce(self) -> None:
        """The agent stopped, and what it started (the session's end: _Session.stop)."""
        self.session.stop()

    def close(self) -> None:
        self.closed = True

    # A task's own service (AHE-Beta's recipes): in the task's main container, as root (the recipe's controller); a
    # companion container's is not reachable through a Hermit environment yet.
    def service_identity(self, name: str) -> str:
        return f"{self.environment.name}:{name}"

    def service_run(self, command: str, *, service: str | None, timeout: float, cwd: str | None) -> dict:
        if service is not None:
            raise Unsupported(f"the companion service {service!r} is not reachable from a Hermit environment yet")
        return self._run(command, timeout, cwd, "root")

    def service_read(self, path: str, *, service: str | None, max_bytes: int) -> bytes:
        if service is not None:
            raise Unsupported(f"the companion service {service!r} is not reachable from a Hermit environment yet")
        q = shlex.quote(path)
        size = self.service_run(f"test -f {q} && test ! -L {q} && stat -c %s {q}", service=None, timeout=30, cwd="/")
        if size["code"] != 0:
            raise FileNotFoundError(path)
        if int(size["stdout"].strip()) > max_bytes:
            raise OverflowError(path)
        with tempfile.TemporaryDirectory(prefix="ahe-service-") as tmp:
            target = Path(tmp) / "artifact"
            self.environment.download(path, target)
            if target.is_symlink() or not target.is_file() or target.stat().st_size > max_bytes:
                raise ValueError(f"{path}: not a regular file within {max_bytes} bytes")
            return target.read_bytes()


class _Hooked(Environment):
    """The task's environment as the Codex session sees it: the user's first message as its instruction (the prompt),
    and the adapter's `.codex/` merged with the user's hooks when the deployment uploads it. Everything else is the
    task's environment's."""
    isolation = "remote"

    def __init__(self, inner, session: _Session):
        object.__setattr__(self, "_inner", inner)        # before anything reads an attribute (__getattr__)
        object.__setattr__(self, "_session", session)
        super().__init__(inner.workspace, inner.limits)
        self.instruction = session.first

    def __getattr__(self, name):                         # network_public, bin_served, deadline_at, started, ...
        if name in ("_inner", "_session"):
            raise AttributeError(name)
        return getattr(self._inner, name)

    def with_limits(self, limits):
        other = _Hooked(self._inner.with_limits(limits), self._session)
        other.instruction = self.instruction
        return other

    def path(self, relative="", *, follow=True):
        return self._inner.path(relative, follow=follow)

    def exec(self, command, *, cwd=None, env=None, timeout=None, stdin=None, user=None, by_fd=None, stream=None,
             secret_env=()):
        kwargs = {"cwd": cwd, "env": env, "timeout": timeout, "stdin": stdin, "user": user, "by_fd": by_fd}
        accepts = inspect.signature(self._inner.exec).parameters
        kwargs.update({k: v for k, v in (("stream", stream), ("secret_env", secret_env)) if k in accepts and v})
        return self._inner.exec(command, **kwargs)

    def upload(self, source, target):
        if PurePosixPath(str(target)).as_posix() == ".codex" and Path(source).is_dir():
            self._session.deployed(Path(source))         # the deployment's .codex/: its hooks with the user's
            placed = self._inner.upload(source, target)
            self._session.lock_hooks()
            return placed
        return self._inner.upload(source, target)

    def download(self, source, target):
        return self._inner.download(source, target)

    def secret(self, paths):
        return self._inner.secret(paths)

    def install(self, cli, version=None):
        return self._inner.install(cli, version)

    def ensure(self, packages):
        return self._inner.ensure(packages)
