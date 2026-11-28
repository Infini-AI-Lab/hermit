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
          review_s: 600                        # what Codex leaves the user for its last turn and review (Codex's
                                               # own clock ends this much before the task's deadline)

Without `user` it is Hermit's Codex agent as it is. The Codex CLI is the environment's: a task's own class is not
asked what to install (hermit.isolate), so `bin` names one the environment mounts (`environment.harbor.environment.
mounts`; a static release of this machine, as ../smoke/run.yaml does). The environment needs python3 (3.8 or later)
for the bridge and the hooks.

The function the user's side writes:

    def simulate(task: dict, agent: Conversation, workspace: Workspace, params: dict) -> dict

    task       {"id": the row's id, "name": the Harbor task's name, "instruction": its instruction.md}
    agent      the running Codex session: write / poll / send / events / interrupt / close (channel.Conversation)
    workspace  the task's environment, as the user reaches it: run / read / write / quiesce (Workspace)
    params     user.params, the row's assignment, `session_id`, `artifact_root` (a private folder of this machine),
               `adapter_sha256` (the adapter the session runs, None without one) and `plan_admitted` (the run holds
               a login's plan admission for the task's environment: a user model on that login may share it)
    returns    {"score": a number in [0, 1], "rating", "outcome", "reason", "issues", "feedback", "usage", "files",
               "record", ...}: VISIBLE's keys are what a learner reads, so a simulator puts nothing private in them
               (ahe_user.py maps AHE-Beta's verdict so); `files` ({name: text}) and `record` stay private. An
               exception it raises may carry `usage` too (what it spent before failing) and `final` (no run again
               mends it)

The first `write` starts Codex with that message as its prompt: one `codex exec` for the whole conversation. Three
hooks (user_hook.py) carry the conversation through a bridge (channel_server.py), both installed as root outside the
workspace: PostToolUse inserts a message while the agent works, PreToolUse denies tool calls after an interrupt and
once the user has ended the session, Stop hands the reply to the user and continues the session with the user's next
message. The adapter's own hooks stay; its Stop hooks run inside the user's Stop hook, first, since Codex runs the
hooks of one event at the same time. What the run places in the workspace (the hooks, the adapter's deployment) is
listed in the task repository's local exclude file while the session runs, and taken back, file by file, when the
agent has stopped (what a placed file overlaid restored as it was, a folder the run made removed once empty), so that
the agent's change as the user reviews it holds none of it.

What the run keeps:

    log/user-transcript.jsonl   every message, reply and tool event the user could see, with times
    log/user-verdict.json       what a learner may read of the verdict (VISIBLE: score, rating, outcome, reason, ...)
    log/user-reward.txt         the score, for a task whose verifier copies it (AHE-Beta's exported tasks)
    <workspaces>/user/<run>/    what simulate wrote under artifact_root (AHE-Beta's native session): its own records,
                                which AHE-Beta's own Evolver hands its controller whole (../delivery/)
    <workspaces>/user/<run>.hermit/   the verdict's `files` (files/) and `record` (record.json), and the session's log
                                when Codex never ended one: never in the log, so never in a learner's material
    RunOutput.extra["user"]     the visible verdict, the user's usage and the counts, which a judge reads (judge.py)
    RunOutput.tokens, cost_usd  the rollout's: the actor's and, when the verdict's `usage` reports them (`tokens`,
                                `cost_usd`, None for a model without a price), the user model's; the actor's own in
                                RunOutput.extra["actor"]

A session whose user failed (simulate raised, or returned no valid score) is not the agent's result: `extra.infra`
(`user_failed`), which the run runs again in a fresh environment and, failing again, records as a failed run; final
(not run again) when the environment cannot run the channel, the task needs what a Hermit environment does not
reach (a companion container's service) or the user's own failure says so (an exception whose `final` is true: its
configuration, say). So is a Codex session that was infra. A session that ran out of its time before
the user's verdict (Harbor ended the agent's phase, the task's deadline had passed when the user failed, or Codex's
clock ended the session the user then could not continue) is the agent's result: score 0, as AHE-Beta rates a session
that delivered nothing; a failure of the user's after the session was stopped in time (its review, its capture) stays
the user's. A session the adapter failed before Codex started is the agent's result too. A failure to stop the session
after the user's verdict leaves the verdict as it is (`extra.user_cleanup_error`).
"""
from __future__ import annotations

import base64
import contextlib
import contextvars
import dataclasses
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
from hermit.agent.base.interface import login_failure
from hermit.common import RunOutput, load_callable
from hermit.environment.base import Environment, Unsupported

sys.path.insert(0, str(Path(__file__).resolve().parent))
from channel import Conversation, ConversationClosed  # noqa: E402

HERE = Path(__file__).resolve().parent
ROOT = "/run/ahe-user-channel"                       # the bridge's folder in the environment (root's)
SOCKET = ROOT + "/channel.sock"
HOOK = ROOT + "/user_hook.py"
SERVER = "/usr/local/libexec/ahe-user-channel.py"
SERVICES = "/run/ahe-services"                       # root's service markers, which the quiesce leaves running
DEPLOYED = ROOT + "/deployed"                        # what the run's placements overlaid in the workspace, as it was
CONTEXT = "AGENTS.md"                                # the context file Codex's deployment writes (a task's own merged)
PID_FILE = SERVICES + "/ahe-user-channel.pid"
EVENTS = ("PreToolUse", "PostToolUse", "Stop")
FRAMES = {
    "reply_frame": "The user says:\n{text}",
    "insert_frame": "The user sent this while you were working:\n{text}",
    "interrupt_frame": "The user interrupted you: {text}\nStop here. Do not call any more tools. Say in one or "
                       "two sentences where things stand, then wait for the user's next message.",
    "end_frame": "The user has ended the session. Do not call any more tools.",
}
VISIBLE = ("score", "rating", "score_kind", "outcome", "reason", "issues", "feedback")   # what a learner may read
MARK = "hermit-user-run:"
REVIEW_S = 600.0
PYTHON = "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)"
TIMED_OUT = {"score": 0.0, "rating": 1, "outcome": "timed_out",
             "reason": "the session ran out of its time before the user's verdict"}
# What the run placed in the workspace, in the task repository's local exclude file (git's info/exclude): $1 the
# workspace, then paths relative to it; a path the repository tracks is left as it is.
# Each line the run adds to the task repository's exclude file sits between MARKS, which undeploy takes back.
MARKS = ("# hermit-user-channel begin", "# hermit-user-channel end")
EXCLUDE = ('cd "$1" 2>/dev/null || exit 0; shift; g() { git -c safe.directory="*" "$@"; }; '
           'g rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0; p=$(g rev-parse --show-prefix) || exit 0; '
           'x=$(g rev-parse --git-path info/exclude) && mkdir -p "$(dirname "$x")" || exit 0; '
           'for f in "$@"; do g ls-files --error-unmatch -- "$f" >/dev/null 2>&1 && continue; '
           'grep -qxF -- "/$p$f" "$x" 2>/dev/null && continue; '
           'printf "%s\\n/%s%s\\n%s\\n" "' + MARKS[0] + '" "$p" "$f" "' + MARKS[1] + '" >> "$x"; done')
UNEXCLUDE = ('x=$(git -c safe.directory="*" rev-parse --git-path info/exclude 2>/dev/null) && [ -f "$x" ] && '
             "sed -i.ahe-bak '/^" + MARKS[0] + "$/,/^" + MARKS[1] + "$/d' \"$x\" && rm -f \"$x.ahe-bak\" || true")


class UserFailed(RuntimeError):
    """The user's side failed: no verdict of the agent's session (not the agent's result)."""


class ChannelUnavailable(UserFailed):
    """The task's environment cannot run the channel (no python3 3.8 or later): no run again does better (final)."""


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
    """Which adapter a user's session ran (the simulator's provenance): binding.py's digest (the files its template
    covers, the card aside, their executable bits), the one a delivery binding freezes (../delivery/)."""
    if adapter is None:
        return None
    from binding import fingerprint as digest
    try:
        return digest(adapter)
    except ValueError:                                   # a link in it: no binding can be this adapter's
        return None


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
        params = {k: v for k, v in self.params.items() if k not in ("user", "prompt")}
        budget = environment.limits.timeout_s            # the task's agent budget (Harbor's, less its margin)
        if budget:                                       # Codex's clock ends before the user's last turn and review
            own = max(60.0, float(budget) - float(spec.get("review_s", REVIEW_S)))
            params["timeout_s"] = min(own, float(params.get("timeout_s") or own))   # (start: from the deadline)
        codex = CliAgent(params, self.base)
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
        self.review_s = float(spec.get("review_s", REVIEW_S))
        name = "".join(c if c.isalnum() or c in "-_." else "_" for c in str(task.get("id", "task")))[:80]
        self.session_id = f"{name}-{uuid.uuid4().hex[:12]}"
        self.private = private_root / self.session_id     # the simulator's own (artifact_root)
        self.records = private_root / f"{self.session_id}.hermit"   # the run's records of it, beside
        self.settings = {"channel": SOCKET, "wait_s": self.wait_s, "adapter_stop": [],
                         **{key: str((spec.get("frames") or {}).get(key) or text) for key, text in FRAMES.items()}}
        self.first: str | None = None
        self.thread: threading.Thread | None = None
        self.output: RunOutput | None = None
        self.codex_error: BaseException | None = None
        self.start_error: BaseException | None = None   # Codex could not start: the user waits for no reply
        self.baseline: dict | None = None
        self.stopped = False
        self.stop_lock = threading.Lock()
        self.conversation: Conversation | None = None
        self.phase_over = False                          # Harbor ended the agent's phase: the environment takes no
        self.unsupported: str | None = None              # more commands; what the task needs that is not reachable
        self.failed_at: float | None = None              # when the user's side failed, and whether its time had run
        self.out_of_time_at_failure = False              # out then (not after the cleanup that followed)
        self.codex_folder: str | None = None             # a task's own .codex/: its owner and mode, put back
        self.stop_started_at: float | None = None        # when the session was first stopped (the agent's end)
        self.placed: dict[str, bool] = {}                # each file kept aside: was it there before the run?
        self.touched: set[str] = set()                   # the files the run placed (undeploy takes them back)
        self.made: set[str] = set()                      # the folders the run made for them
        self.cleanup_error: str | None = None            # a failure to stop the session after the user's verdict

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
        probe = self.environment.exec(["python3", "-c", PYTHON], timeout=60)
        if probe.code in (1, 126, 127) and not probe.timed_out:   # an older python3, or none: no run does better
            raise ChannelUnavailable("the user channel needs python3 3.8 or later in the task's environment")
        if probe.code != 0:                              # the environment's own failure (a timeout, ...): run again
            raise RuntimeError(f"the user channel's python3 check failed ({probe.code}): {probe.output[-300:]}")
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
        self.baseline = json.loads(self.root("python3 -B -", stdin=script).strip().split("\n")[-1])
        self.preserve([".codex/hooks.json"])
        self.preserve([CONTEXT], placing=self.adapter is not None)   # before the deployment stages it (it may unlink
        folder = shlex.quote(str(PurePosixPath(self.hooks_path()).parent))   # a link there and then fail)
        self.codex_folder = self.root(f"if [ -d {folder} ]; then stat -c '%u:%g %a' {folder}; fi").strip() or None
        self.install(json.dumps({"hooks": self.merged({})}, indent=2), self.hooks_path(), "0444")
        self.exclude([".codex/hooks.json"])
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
        """The adapter's `.codex/` as the deployment staged it, before its upload: its hooks merged with the user's. A
        hooks file that does not parse, or a Stop hook the user's cannot run, is the adapter's (ValueError: the
        adapter failed); the channel's own commands failing there is the environment's (EnvironmentUnavailable)."""
        path = staging / "hooks.json"
        adapter = json.loads(path.read_text()).get("hooks") or {} if path.is_file() else {}
        path.write_text(json.dumps({"hooks": self.merged(adapter)}, indent=2) + "\n")
        self.environmental(lambda: self.install(json.dumps(self.settings, indent=1), ROOT + "/user_hook.json", "0444"))

    @staticmethod
    def environmental(operation) -> None:
        """One of the channel's commands while the deployment uploads: its failure the environment's."""
        from hermit.environment.harbor import EnvironmentUnavailable
        try:
            operation()
        except EnvironmentUnavailable:
            raise
        except Exception as exc:                         # noqa: BLE001 - the environment's, never the adapter's
            raise EnvironmentUnavailable(f"the user channel failed in the task's environment: {exc}") from exc

    def lock_hooks(self) -> None:
        """`.codex/` and its hooks root's and read-only: the agent's commands cannot drop the user's hooks or add
        their own. The folder's other files keep their owners and modes (a task's own among them)."""
        folder, hooks = shlex.quote(str(PurePosixPath(self.hooks_path()).parent)), shlex.quote(self.hooks_path())
        self.root(f"chown root:root {folder} {hooks} && chmod 0755 {folder} && chmod 0444 {hooks}")

    def exclude(self, names) -> None:
        """What the run places in the workspace (the channel's hooks, the adapter's deployment), out of the task's
        repository: listed in its local exclude file, so that the agent's change as the user reviews it (AHE-Beta's
        prediction patch: `git add -A` of the tree into an index of its own) holds none of it. A path the repository
        tracks stays as it is; without a repository or git there is nothing to do."""
        names = [str(n) for n in names if str(n)]
        if names:
            with contextlib.suppress(Exception):         # best effort: the session runs either way
                self.environment.exec(["bash", "-c", EXCLUDE, "bash", str(self.environment.workspace), *names],
                                      cwd="/", user="root", timeout=60)

    def preserve(self, paths, *, placing: bool = True, folders=()) -> None:
        """Before the run first places these files (paths in the workspace): what is there kept aside as root (a link
        as a link), and the folders on their way (and `folders`, a staged tree's own) the run will make, for
        undeploy() to put back (`placing`: the run places them now; else only kept aside, in case it does). A failure,
        or a link on a path's way (root would follow it out of the workspace), raises: nothing is placed over a file
        that could not be kept."""
        new = []
        for path in [*paths, *folders]:
            parts = PurePosixPath(path).parts
            if not parts or PurePosixPath(path).is_absolute() or any(part in (".", "..") for part in parts):
                raise ValueError(f"the run would place {path!r}, which is not a path inside the workspace")
        for path in paths:
            if path not in self.placed and path not in new:
                new.append(path)
        q = shlex.quote
        folders = sorted(({str(f) for p in [*new, *folders] for f in PurePosixPath(p).parents if str(f) != "."}
                          | {str(f) for f in folders}) - self.made, key=lambda f: len(PurePosixPath(f).parts))
        lines = ["set -e", f"cd {q(str(self.environment.workspace))}", *_unlinked([*new, *folders])]
        for i, path in enumerate(new):
            kept = f"{DEPLOYED}/{path}"
            aside = f"mkdir -p {q(str(PurePosixPath(kept).parent))}; rm -rf {q(kept)}; cp -a {q(path)} {q(kept)}"
            lines.append(f"if [ -e {q(path)} ] || [ -L {q(path)} ]; then {aside}; echo p {i}; else echo a {i}; fi")
        lines += [f"if [ ! -d {q(folder)} ]; then echo m {j}; fi" for j, folder in enumerate(folders)]
        said = [line.split() for line in self.root("\n".join(lines), timeout=120).splitlines()]
        found = {(w[0], int(w[1])) for w in said if len(w) == 2 and w[0] in "pam" and w[1].isdigit()}
        for i, path in enumerate(new):
            if ("p", i) not in found and ("a", i) not in found:
                raise RuntimeError(f"the workspace's {path!r} could not be kept aside before the run placed its own")
            self.placed[path] = ("p", i) in found
        self.made |= {folder for j, folder in enumerate(folders) if ("m", j) in found}
        if placing:                                      # only once what it overlays is kept
            self.touched.update(paths)

    def undeploy(self) -> None:
        """What the run placed in the workspace taken back once the agent has stopped, before the user's review and
        the task's capture (a finalizer may archive the whole workspace): each placed file removed and what it overlaid
        put back as it was (a task's own AGENTS.md the deployment prepended to, a link as a link), each folder the run
        made removed once empty (one the agent put files in stays). A failure raises: the workspace is not the agent's
        as it was, so no review may rate it."""
        if not self.touched and not self.made:
            return
        q = shlex.quote
        lines = ["set -e", f"cd {q(str(self.environment.workspace))}",   # each folder the run made a folder still,
                 *_unlinked([*self.touched, *(f"{folder}/__pycache__" for folder in self.made)])]   # not a link
        for path in sorted(self.touched, key=lambda p: -len(PurePosixPath(p).parts)):
            lines.append(f"rm -rf {q(path)}")
            if self.placed.get(path):
                kept = f"{DEPLOYED}/{path}"
                lines.append(f"mkdir -p {q(str(PurePosixPath(path).parent))}; cp -a {q(kept)} {q(path)}")
        lines += [f"rm -rf {q(folder)}/__pycache__; rmdir {q(folder)} 2>/dev/null || true"   # a deployed helper's
                  for folder in sorted(self.made, key=lambda f: -len(PurePosixPath(f).parts))]   # bytecode with it
        workspace = PurePosixPath(str(self.environment.workspace))
        folder = str(PurePosixPath(self.hooks_path()).parent.relative_to(workspace))
        if self.codex_folder and ".codex" not in self.made:   # a task's own .codex/, as it was
            owner, mode = self.codex_folder.split()
            lines.append(f"if [ -d {q(folder)} ] && [ ! -L {q(folder)} ]; then chown {q(owner)} {q(folder)}; "
                         f"chmod {q(mode)} {q(folder)}; fi")
        lines.append(UNEXCLUDE)
        try:
            self.root("\n".join(lines), timeout=120)
        except Exception as exc:
            raise RuntimeError(f"the workspace could not be put back as it was before the run's placements: {exc}") \
                from exc
        self.touched.clear()
        self.made.clear()

    # --- Codex -------------------------------------------------------------------------------------------------------
    def start(self, text: str) -> None:
        """The user's first message: Codex starts with it as its prompt, its clock ending `review_s` before the task's
        deadline (what the channel's setup and the user's first turn took comes off Codex's time, not the review's)."""
        self.first = text
        deadline = getattr(self.environment, "deadline_at", None)
        if deadline is not None:                         # the whole call's window (Hermit's deadline_s): the deployment
            left = float(deadline) - time.time() - self.review_s    # and the sign-in come off it too
            if left <= 0:
                self.start_error = UserFailed(f"the task's time ran out before Codex could start (review_s "
                                              f"{self.review_s:g} s is all that is left)")
                raise self.start_error
            given, whole = self.codex.params.get("timeout_s"), self.codex.params.get("deadline_s")
            self.codex.params = {**self.codex.params, "deadline_s": min(left, float(whole)) if whole else left,
                                 "timeout_s": min(left, float(given)) if given else left}
        context = contextvars.copy_context()             # the run's (the adapter's confinement: hermit.isolate)
        self.thread = threading.Thread(target=context.run, args=(self._codex,), name=f"codex-{self.session_id}",
                                       daemon=True)
        self.thread.start()

    def _codex(self) -> None:
        try:
            self.output = self.codex.run(self.task, self.adapter, _Hooked(self.environment, self))
        except BaseException as exc:                     # noqa: BLE001 - read by run()
            self.codex_error = exc

    def alive(self) -> bool:
        return self.start_error is None and (self.thread is None or self.thread.is_alive())

    def stop(self) -> None:
        """The session over: the user's end sent, Codex given exit_wait_s to exit, then whatever the rollout started
        stopped (the bridge and the task's own services stay), so that what the user reviews is frozen."""
        with self.stop_lock:
            if self.stop_started_at is None:             # the agent's session ends here (result: was it in time?)
                self.stop_started_at = time.time()
            if self.stopped:
                return
            if self.phase_over:                          # the phase's end stopped every command (harbor_driver)
                if self.thread is not None:
                    self.thread.join(self.exit_wait_s)
                self.stopped = True
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
            self.undeploy()
            if self.conversation is not None:
                with contextlib.suppress(Exception):    # what the agent reported in its last moments: the transcript's
                    self.conversation.drain()
            self.stopped = True

    def stop_channel(self) -> None:
        q = shlex.quote
        if self.phase_over:
            return
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
                  "artifact_root": str(self.private), "adapter_sha256": fingerprint(self.adapter),
                  "plan_admitted": bool(getattr(self.environment, "plan_admitted", False))}
        verdict, error = None, None
        try:
            self.start_channel()
            self.conversation = Conversation(None, self.start, self.alive, self.stop, transport=_Channel(self))
            verdict = _valid(simulate(task, self.conversation, Workspace(self), params))
        except Exception as exc:                         # noqa: BLE001 - the user's failure, or the time's: below
            self.failed_at = time.time()                 # as it happened, not after the cleanup below
            if _phase_over(exc):                         # Harbor's agent timeout ended the phase: the agent's time
                self.phase_over = True                   # ran out (the user's shares it)
            deadline = getattr(self.environment, "deadline_at", None)   # out of time: the agent's session ended
            ended = min(t for t in (self.stop_started_at, self.failed_at) if t is not None)   # past the deadline, or
            self.out_of_time_at_failure = (deadline is not None and ended >= float(deadline)) or \
                (self.phase_over and self.stop_started_at is None)   # Harbor ended the phase while it ran
            error = exc
        finally:
            try:
                self.stop()
            except Exception as exc:                     # noqa: BLE001 - a session that would not stop
                if error is None:                        # after the user's verdict: it stands, the failure noted
                    self.cleanup_error = f"{type(exc).__name__}: {exc}"[-2000:]
            self.stop_channel()
        return self.result(verdict, error, started)

    def result(self, verdict: dict | None, error: BaseException | None, started: float) -> RunOutput:
        out = self.output
        failed_usage = getattr(error, "usage", None)    # what a failed user had spent (its `usage`)
        if error is not None and login_failure(error):   # the user model's sign-in is gone, whatever else happened
            raise error                                  # (the actor's own failure too): the rollout ends the run
        if self.codex_error is not None and _phase_over(self.codex_error):
            self.phase_over = True                       # the phase's end reached Codex's session first
        elif self.codex_error is not None:               # an interrupt, the environment's or this machine's failure:
            _spent(self.codex_error, (verdict or {}).get("usage") or failed_usage)   # the run's to classify, never a
            raise self.codex_error                                                    # record of the user's
        transcript = self.conversation.transcript if self.conversation is not None else []
        if out is None:                                  # Codex never ended its session here (never started, or the
            log = self.records / "log"                   # phase ended under it): its usage is not known
            log.mkdir(parents=True, exist_ok=True)
            ran = self.thread is not None
            out = RunOutput(output=None, log=log, cost_usd=None if ran else 0.0, wall_s=0.0, tokens=None if ran else 0,
                            api_speed={"model_calls": None if ran else 0})
        if out.log is None:                              # the session's staging failed before it had a log
            out.log = self.records / "log"
            out.log.mkdir(parents=True, exist_ok=True)
        log = Path(out.log)
        extra = dict(out.extra or {})
        gone = _closed(error) or bool(getattr(self.conversation, "gone", False))   # the user found Codex gone
        out_of_time = self.out_of_time_at_failure or (bool(extra.get("timed_out")) and gone)
        if error is not None and out_of_time and not extra.get("infra") and not extra.get("adapter_failed") and \
                not self.unsupported and not isinstance(error, ChannelUnavailable) and \
                not getattr(error, "final", False):      # a session no run mends (unbound, misconfigured) stays so
            extra["user_error"] = f"{type(error).__name__}: {error}"[-2000:]   # the time ran out before a verdict:
            verdict, error = dict(TIMED_OUT), None       # the agent's result, as AHE-Beta rates no delivery
            extra["timed_out"] = True
        kept = {k: v for k, v in (verdict or {}).items() if k not in ("files", "record")}
        files = self.records / "files"                   # the user's own records: private (named as files: _valid)
        for name, text in ((verdict or {}).get("files") or {}).items():
            files.mkdir(parents=True, exist_ok=True)
            (files / name).write_text(str(text), encoding="utf-8")
        if verdict and verdict.get("record") is not None:
            self.records.mkdir(parents=True, exist_ok=True)
            (self.records / "record.json").write_text(json.dumps(verdict["record"], indent=1, ensure_ascii=False,
                                                                 default=str), encoding="utf-8")
        visible = {k: kept[k] for k in VISIBLE if k in kept}
        if error is not None:
            visible["error"] = f"{type(error).__name__}: {error}"[-2000:]
        _write(log / "user-transcript.jsonl", "".join(json.dumps(e, ensure_ascii=False, default=str) + "\n"
                                                      for e in transcript))
        _write(log / "user-verdict.json", json.dumps(visible, indent=1, ensure_ascii=False, default=str) + "\n")
        (log / "user-reward.txt").unlink(missing_ok=True)
        said = [e for e in transcript if e.get("from") == "user"]
        # what the run records of the verdict (the learning material holds it): the visible part, the user's usage, the
        # counts; the private folder by its path, for a trusted reader on this machine (AHE-Beta's own Evolver)
        extra["user"] = {**visible, **({"usage": kept["usage"]} if isinstance(kept.get("usage"), dict) else {}),
                         "session_id": self.session_id, "private": str(self.private),
                         "messages": sum(1 for e in said if e.get("kind") == "message"),
                         "interrupts": sum(1 for e in said if e.get("kind") == "interrupt"),
                         "inserted": sum(1 for e in transcript if e.get("kind") == "delivered"
                                         and e.get("at") == "PostToolUse"),
                         "replies": sum(1 for e in transcript if e.get("kind") == "reply")}
        if self.cleanup_error:
            extra["user_cleanup_error"] = self.cleanup_error
        usage = kept.get("usage") if isinstance(kept.get("usage"), dict) else {}
        spent = usage if usage else failed_usage if isinstance(failed_usage, dict) else {}
        if isinstance(spent.get("tokens"), int) or "cost_usd" in spent:   # the user model's spend is the rollout's
            extra["actor"] = {"tokens": out.tokens, "cost_usd": out.cost_usd}   # (the paper's Tokens: the actor's)
            out.tokens, out.cost_usd, unknown = _with_user(out.tokens, out.cost_usd, spent)
            if unknown:
                extra["user_cost_unknown"] = True        # a model without a price, or calls it could not count
        if extra.get("infra") or extra.get("adapter_failed"):
            out.extra = extra                            # the session's own word stands (infra, or the adapter's)
            return out
        if error is not None:                            # the user failed: not the agent's result
            why = f"; the task needs {self.unsupported}" if self.unsupported else ""
            extra.update(infra="user_failed", infra_final=isinstance(error, ChannelUnavailable) or bool(why) or
                         bool(getattr(error, "final", False)),
                         exception_message=f"the user's session failed: {visible['error']}{why}"[-2000:])
            out.extra = extra
            return out
        _write(log / "user-reward.txt", f"{float(verdict['score']):g}\n")
        out.extra = extra
        if out.wall_s is not None:
            out.wall_s = round(time.time() - started, 1)
        return out


def _phase_over(exc: BaseException) -> bool:
    """Whether a failure is Harbor's end of the agent's phase (harbor_driver's AgentPhaseOver), however wrapped."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if getattr(exc, "kind", None) == "AgentPhaseOver" or "AgentPhaseOver" in f"{type(exc).__name__}: {exc}":
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def _with_user(tokens, cost_usd, usage: dict) -> tuple:
    """The rollout's tokens and cost with the user model's spend added (a verdict's `usage`, or a failed user's), and
    whether the user's cost is unknown: the tokens summed (those reported); the cost as Hermit prices a run
    (Usage.cost), unknown when a part of it is: the actor's, or the user's (no price, or calls it could not count)."""
    said, cost = usage.get("tokens"), usage.get("cost_usd")
    priced = isinstance(cost, (int, float)) and not isinstance(cost, bool) and not usage.get("uncounted_calls")
    return ((tokens or 0) + said if isinstance(said, int) else tokens,
            cost_usd + float(cost) if priced and cost_usd is not None else None, not priced)


def _spent(error: BaseException, usage) -> None:
    """The user model's spend (a verdict's `usage`, or a failed user's) added to what `error`, a failed run, says it
    spent (the Trainer's `spent`), as on a run that ends (_with_user): a run that said nothing has an unknown cost."""
    if not isinstance(usage, dict) or not (isinstance(usage.get("tokens"), int) or "cost_usd" in usage):
        return
    spent = getattr(error, "spent", None) or RunOutput(output=None, cost_usd=None, tokens=None)
    if not dataclasses.is_dataclass(spent):              # not the Trainer's RunOutput: the failure goes on as it is
        return
    tokens, cost, _ = _with_user(spent.tokens, spent.cost_usd, usage)
    error.spent = dataclasses.replace(spent, tokens=tokens, cost_usd=cost)


def _unlinked(paths) -> list[str]:
    """Shell lines (in the workspace, under `set -e`) that stop when a folder on one of these paths' way is a link: a
    command run as root on the path would follow it out of the workspace."""
    ways = sorted({str(f) for p in paths for f in PurePosixPath(p).parents if str(f) != "."})
    return [f"if [ -L {shlex.quote(way)} ]; then printf '%s\\n' {shlex.quote('a link on the way: ' + way)} >&2; "
            "exit 3; fi" for way in ways]


def _closed(exc: BaseException | None) -> bool:
    """Whether the user failed because Codex's session was gone (channel.ConversationClosed, however wrapped)."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, ConversationClosed):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def _valid(verdict) -> dict:
    """simulate's result, refused (UserFailed: not the agent's result) unless it is a mapping with a score in [0, 1]
    and its files named as files."""
    if not isinstance(verdict, dict):
        raise UserFailed(f"simulate returned {type(verdict).__name__}, not a dict")

    def refused(why):                                    # what the result says it spent still counts (the rollout's)
        exc = UserFailed(why)
        exc.usage = verdict["usage"] if isinstance(verdict.get("usage"), dict) else None
        return exc
    score = verdict.get("score")
    if score is None or isinstance(score, bool) or not isinstance(score, (int, float)) or \
            not math.isfinite(score) or not 0 <= score <= 1:
        raise refused(f"simulate's score must be a number in [0, 1], not {score!r}")
    files = verdict.get("files")
    if files is not None and (not isinstance(files, dict) or any(
            not isinstance(name, str) or not name or Path(name).name != name or name in (".", "..") for name in files)):
        raise refused("simulate's files must be {name: text}, each name a file's")
    return verdict


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
        value = json.loads(got.strip().split("\n")[-1])   # "\n" only: a reply may hold U+2028, which splitlines cuts
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
        if MARK in text:                                 # the marker's line ends; then stdout's line and stderr's, an
            lines = text.rsplit(MARK, 1)[1].split("\n")[1:]   # empty stream an empty line

            def decode(line: str) -> str:
                return base64.b64decode(line.strip()).decode("utf-8", errors="replace") if line.strip() else ""
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

    def _companion(self, service: str) -> None:
        self.session.unsupported = f"the companion container {service!r}, which a Hermit environment does not reach"
        raise Unsupported(f"the companion service {service!r} is not reachable from a Hermit environment yet")

    def service_run(self, command: str, *, service: str | None, timeout: float, cwd: str | None) -> dict:
        if service is not None:
            self._companion(service)
        return self._run(command, timeout, cwd, "root")

    def service_read(self, path: str, *, service: str | None, max_bytes: int) -> bytes:
        if service is not None:
            self._companion(service)
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
        name, source = PurePosixPath(str(target)).as_posix(), Path(source)
        codex = name == ".codex" and source.is_dir()
        if codex:
            self._session.deployed(source)               # the deployment's .codex/: its hooks with the user's
        files, folders = [], []
        if "/" not in name and name not in ("", ".", ".."):   # one of the deployment's entries: each file it places,
            tree = sorted(source.rglob("*")) if source.is_dir() else []                      # kept aside first, and
            files = [f"{name}/{p.relative_to(source).as_posix()}" for p in tree if p.is_file() or p.is_symlink()] \
                if source.is_dir() else [name]                                               # its folders (an empty
            folders = [name, *(f"{name}/{p.relative_to(source).as_posix()}" for p in tree    # one too) made here
                               if p.is_dir() and not p.is_symlink())] if source.is_dir() else []
            self._session.environmental(lambda: self._session.preserve(files, folders=folders))
        uploaded = self._inner.upload(source, target)
        if codex:
            self._session.environmental(self._session.lock_hooks)
        self._session.exclude(files)                     # out of the repository's changes while the session runs
        return uploaded

    def download(self, source, target):
        return self._inner.download(source, target)

    def secret(self, paths):
        return self._inner.secret(paths)

    def install(self, cli, version=None):
        return self._inner.install(cli, version)

    def ensure(self, packages):
        return self._inner.ensure(packages)
