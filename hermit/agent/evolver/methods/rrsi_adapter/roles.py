"""RRSI's one-call search roles (the analyst, its digesters, the critic), each call one `codex` Base Agent session.

Upstream's analyst, digester and critic (rrsi.analyst, rrsi.digester, rrsi.critic) call `generate(prompt, system,
json_only, ...)`, Claude Opus 4.8 on Vertex AI (rrsi.llm). Here `generate` is rebound in those three modules, for the
duration of a call, to `Roles.generate`: one call of the `codex` Base Agent (`make("codex", settings).run`,
docs/framework/agent-layers.md), the way the Evolver's proposer is one, with the role's system text as the session's
developer instructions and the prompt on stdin. The session runs under the run's sandbox with the workspace's records
hidden (`Limits.hidden`), Codex's own sandbox read-only, in an empty directory, so a role answers from the text it is
given, as upstream's do; a digester's session runs in the round's rendered traces instead, the directory upstream's
digester reads with its read_file/grep/bash actions. The reply is the session's last message; with `json_only`,
upstream's own `extract_json` cuts the JSON out of it, as upstream's `generate` does.

The roles are not calls of the run's model (`ctx.agent`): through the Trainer's chat endpoint a session for a model
only Codex serves runs in an empty workspace of its own, and a digester there, told its trace is in its working
directory, finds none and answers so instead of with an action. Everything upstream does with the reply (parsing
actions, the digesters' tool loop, the critic's three tries) is upstream's code, unchanged. Each call keeps its
prompt, system text, events, reply and meta under `records` (the step's output, kept with the run's records), its home
and working directory under `log_dir`; its usage is the Base Agent's. A reply counts only from a session that ended
(exit 0, no timeout); a call whose session timed out twice fails. A session the Trainer stopped, or that the model's
plan held back or refused, ends the call (`Stop`); one the provider could not serve is tried again after a longer
pause, and a call whose every session the provider could not serve ends the same way (`Stop`, not counted toward
max_failures, as a proposer's).
"""
from __future__ import annotations

from contextlib import contextmanager
import importlib
import itertools
import json
import os
from pathlib import Path
import subprocess
import threading
import time

from hermit.agent.base import login as codex_login
from hermit.agent.base.cli import codex_instructions
from hermit.agent.base.interface import Limits, Result, make
from hermit.agent.evolver.base import Usage

ROLE_MODULES = ("rrsi.analyst", "rrsi.digester", "rrsi.critic")


class Stop(BaseException):
    """The call must end now: the Trainer stopped it (its budget, its clock), the model's plan held a session back,
    or the provider kept failing it. A BaseException, so that upstream's `except Exception` handlers around a role call never
    swallow it."""


@contextmanager
def bound(generate, digester_subprocess=None):
    """`generate` in the role modules replaced by `generate` while the block runs, and, when given, the
    `subprocess` module the digester runs its read-only `bash` action with (upstream runs it on the host)."""
    modules = [importlib.import_module(m) for m in ROLE_MODULES]
    saved = [(m, m.generate) for m in modules]
    digester = importlib.import_module("rrsi.digester")
    saved_subprocess = digester.subprocess
    try:
        for m in modules:
            m.generate = generate
        if digester_subprocess is not None:
            digester.subprocess = digester_subprocess
        yield
    finally:
        for m, g in saved:
            m.generate = g
        digester.subprocess = saved_subprocess


class SandboxedSubprocess:
    """What the digester sees as `subprocess` while bound: its `run(cmd, shell=True, cwd=...)` runs the command
    under the run's sandbox with the workspace's records covered, as every other command a learner starts does.
    Upstream's allowlist of read-only commands (rrsi.digester._bash_ok) still applies before it is called.

    The digester runs its shell in the round's rendered traces, which upstream writes under the run's state, and the
    sandbox covers the state: a working directory under a covered path is replaced by `visible`, a copy of those
    traces outside the state, so relative paths name the same files."""

    TimeoutExpired = subprocess.TimeoutExpired
    CalledProcessError = subprocess.CalledProcessError

    def __init__(self, sandbox, hide: list[Path], visible: Path | None = None):
        self.sandbox, self.hide, self.visible = sandbox, list(hide), visible

    def _cwd(self, cwd):
        if cwd is None or self.visible is None:
            return cwd
        c = Path(cwd).resolve()
        covered = any(c == h.resolve() or h.resolve() in c.parents for h in self.hide)
        return str(self.visible) if covered else cwd

    def run(self, cmd, shell: bool = False, cwd=None, **kw):
        args = ["bash", "-c", cmd] if shell else list(cmd)
        env = kw.pop("env", None)
        cwd = self._cwd(cwd)
        if self.sandbox is not None:
            from hermit.sandbox import HidingSandbox
            args, env = HidingSandbox(self.sandbox, self.hide).wrap(args, env=dict(env or os.environ))
            if self.hide and getattr(self.sandbox, "enabled", False) and \
                    not any(os.path.basename(str(a)) == "bwrap" for a in args):
                # the run's sandbox ran unconfined (no bubblewrap, enforce: best_effort): nothing would be covered,
                # so the command is refused, as the Base Agent refuses a session it cannot hide paths from
                raise RuntimeError("the digester's shell needs bubblewrap to hide the run's records")
        return subprocess.run(args, cwd=cwd, env=env, **kw)


class Roles:
    """`generate` for upstream's one-call roles, each call one session of the `codex` Base Agent with `settings`."""

    def __init__(self, ctx, settings: dict, log_dir: Path, hide: list[Path] = (), records: Path | None = None):
        self.ctx, self.settings, self.log_dir = ctx, dict(settings), Path(log_dir)
        self.records = Path(records) if records is not None else self.log_dir
        self.hide = [Path(p) for p in hide]
        self.usage: list[Usage] = []
        self.calls = 0
        self.traces: Path | None = None          # the round's rendered traces, the digesters' working directory
        self._count = itertools.count(1)
        self._lock = threading.Lock()
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.records.mkdir(parents=True, exist_ok=True)

    def sandboxed_subprocess(self) -> SandboxedSubprocess:
        return SandboxedSubprocess(self.ctx.sandbox, self.hide, visible=self.traces)

    def _cancelled(self) -> bool:
        return self.ctx.cancel is not None and self.ctx.cancel.is_set()

    def _wait(self, seconds: float) -> bool:
        """Wait up to `seconds`; True as soon as the Trainer stops the call."""
        if self.ctx.cancel is None:
            time.sleep(seconds)
            return False
        return self.ctx.cancel.wait(seconds)

    def generate(self, prompt: str, system: str | None = None, max_retries: int = 6, json_only: bool = False,
                 model: str | None = None, max_tokens: int = 20_000, cache_prefix: str | None = None) -> str:
        """Upstream's `generate` contract: the reply text (JSON cut out with `json_only`); after `max_retries`
        sessions without a reply, RuntimeError, as upstream raises. `model` and `max_tokens` are the session's
        settings' (upstream's roles share one model). A session without a reply is tried again after upstream's
        pause, four times as long when the provider could not serve it."""
        from rrsi.llm import _JSON_SUFFIX, extract_json
        system_text = (system or "") + (_JSON_SUFFIX if json_only else "")
        text = f"{cache_prefix}\n\n{prompt}" if cache_prefix else prompt
        last, timeouts, provider = "", 0, True
        for attempt in range(max(1, int(max_retries))):
            if self._cancelled():
                raise Stop("stopped by the Trainer")
            result = self._session(system_text, text)
            extra, tail = result.extra, str(result.extra.get("output_tail") or "")
            if extra.get("cancelled") or self._cancelled():
                raise Stop("stopped by the Trainer")
            # the Base Agent says whether the plan refused it (hermit >= #69); before, its output's tail is read
            refused = extra["refused"] if "refused" in extra else codex_login.refused(tail)
            if (not extra.get("started") and extra.get("unavailable")) or (not extra.get("served") and refused):
                raise Stop(f"the model's plan did not serve a role's session: {tail[-200:]}")
            if result.reply.strip() and result.exit_code == 0 and not result.timed_out:
                return extract_json(result.reply) if json_only else result.reply
            last = tail[-300:] or ("timed out" if result.timed_out else f"exit code {result.exit_code}")
            provider = provider and bool(extra.get("unavailable")) and not extra.get("served")
            timeouts += bool(result.timed_out)
            if timeouts >= 2:
                # upstream retries a failed API call; a session that ran out of its clock twice (role_timeout_s
                # each) is no transient error: the call fails, and so does the step (counted toward max_failures)
                raise RuntimeError(f"generate: two of this call's Codex sessions ran out of role_timeout_s: {last}")
            if self._wait(min(2 ** attempt, 30) * (4 if extra.get("unavailable") else 1)):
                raise Stop("stopped by the Trainer")
        if provider:
            # every session failed on the provider (429, 5xx, overloaded), as a proposer session that keeps failing
            # on it does: not the step's doing, so it is not counted toward max_failures
            raise Stop(f"the model's provider kept failing a role's sessions: {last}")
        raise RuntimeError(f"generate failed after {max_retries} Codex sessions: {last}")

    def _session(self, system: str, prompt: str) -> Result:
        with self._lock:
            n = next(self._count)
            self.calls += 1
        call = self.log_dir / f"{n:05d}"
        work = call / "work"
        work.mkdir(parents=True, exist_ok=True)
        rec = self.records / f"{n:05d}"
        rec.mkdir(parents=True, exist_ok=True)
        (rec / "prompt.txt").write_text(prompt)
        (rec / "system.txt").write_text(system)
        # Upstream's digester reads "the rendered trajectory files in your working directory" through its JSON
        # actions; a Codex session reads its working directory with its own tools as well, so a digester's session
        # runs in the round's rendered traces (Codex's read-only sandbox), and every other role's in an empty one.
        cwd = self.traces if self.traces is not None and "trajectory digester" in system else work
        extra, text = codex_instructions(system, prompt)
        settings = {**self.settings, "home": call / "home", "log": rec / "events.jsonl", "stdin": True,
                    "args": [*(self.settings.get("args") or []), *extra], "cancel": self.ctx.cancel}
        # nor the other calls' homes and records-in-progress, nor the round's traces unless it is a digester
        others = [p for p in sorted(self.log_dir.iterdir()) if p.resolve() not in (call.resolve(), Path(cwd).resolve())]
        hidden = [*self.hide, *others] if getattr(self.ctx.sandbox, "enabled", False) else []
        started = time.time()
        result = make("codex", settings).run(text, workspace=cwd, limits=Limits(hidden=hidden))
        with self._lock:
            self.usage += result.usage
        (rec / "reply.txt").write_text(result.reply)
        meta = {"exit": result.exit_code, "timed_out": result.timed_out, "wall_s": round(time.time() - started, 1),
                "chars": len(result.reply), **{k: result.extra.get(k) for k in ("served", "unavailable", "started",
                                                                                  "cancelled")}}
        (rec / "meta.json").write_text(json.dumps(meta, indent=1))
        return result

    def total(self) -> list[Usage]:
        """The usage of every session so far, one record per model."""
        out: dict[str, Usage] = {}
        for x in self.usage:
            u = out.setdefault(x.model, Usage(model=x.model, cost_usd=0.0))
            u.input_tokens += x.input_tokens
            u.cached_input_tokens += x.cached_input_tokens
            u.output_tokens += x.output_tokens
            u.calls += x.calls
            u.cost_usd = None if u.cost_usd is None or x.cost_usd is None else u.cost_usd + x.cost_usd
        return list(out.values())
