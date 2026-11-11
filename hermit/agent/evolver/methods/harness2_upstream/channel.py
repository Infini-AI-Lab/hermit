"""Upstream Harness²'s session channel (harness2.core.proposer.Proposer, `claude_cli`) on a framework-bound model.

Upstream starts `claude -p` with Vertex settings and no sandbox. Here the session is one call of the `claude` Base
Agent (Layer 2): it talks to the bound backend's
Messages API (the binding's Claude Code environment: base URL, key, model for every alias), gets a fresh config
directory, sees only its own copy of the evidence (the run's sandbox: system files, the Claude install, the session
directory; the network only to its model: the provider's host, or the forwarded endpoint of a model on the loopback),
and its cost is priced from its token usage at the backend's prices. Everything else - the stdin mission, the JSON reply, retries, harvesting the edited
`context/harness`, the audit and its cache - is upstream's `Proposer.call`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
from typing import Any

from harness2.core.proposer import SYSTEM_ARG_MAX, Proposer, _DROP_ENV, _parse_json

from hermit.agent.base.cli import PI_PARAMS
from hermit.agent.base.interface import LoginFailed, make
from hermit.agent.base.records import price_usage
from hermit.agent.evolver.base import Usage

QUIET = {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1", "DISABLE_TELEMETRY": "1",
         "DISABLE_ERROR_REPORTING": "1", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}


def _read_own(path: Path) -> str:
    """A file this process had the session write into its working directory (its stream, its stderr), read only as
    the regular file it is: the session could put a link or a pipe there, and this runs outside its sandbox."""
    import stat as _stat
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return ""
    if not _stat.S_ISREG(os.fstat(fd).st_mode):          # before fdopen, which refuses a directory
        os.close(fd)
        return ""
    with os.fdopen(fd, "rb") as f:
        return f.read().decode("utf-8", errors="replace")


@dataclass
class FrameworkProposer(Proposer):
    params: dict = field(default_factory=dict)    # the Evolver's bound params: env, price, binding, model, tools
    sandbox: Any = None                           # hermit.sandbox.Sandbox for the sessions, or None
    cancel: Any = None                            # threading.Event set by the Trainer to stop the step
    usages: list = field(default_factory=list)    # one Usage per session attempt, for the step's StepResult
    served: int = 0                               # sessions the provider served (a failed one's usage is in usages)
    _usage_lock: threading.Lock = field(default_factory=threading.Lock)

    def env(self) -> dict[str, str]:
        own = {k: os.path.expandvars(str(v)) for k, v in (self.params.get("env") or {}).items()}
        return {**{k: v for k, v in os.environ.items() if k not in _DROP_ENV}, **QUIET, **own}

    def _claude_cli(self, cwd: Path, mission: str, system: str, wall: int) -> tuple[str, float, str]:
        """One upstream session as one call of a CLI Base Agent (docs/framework/agent-layers.md): `claude` by default,
        upstream's command (stdin mission, its system prompt, one JSON result, its tools, no session kept); `codex`
        with `params.backend: codex` (a model only Codex reaches: a ChatGPT login), the mission on stdin, the system
        text as its developer instructions, its own sandbox off inside the run's; `pi` with `params.backend: pi`, on
        the bound model. Each has its own config directory, runs in the run's sandbox with the session directory
        writable, and is stopped at `wall` or by the Trainer."""
        if self.params.get("backend") == "codex":
            return self._codex_cli(cwd, mission, system, wall)
        if self.params.get("backend") == "pi":
            return self._pi_cli(cwd, mission, system, wall)
        home = Path(cwd) / ".claude-home"
        stdin = mission
        if system and len(system) > SYSTEM_ARG_MAX:
            stdin = f"# SYSTEM INSTRUCTIONS\n\n{system}\n\n# END\n\n{mission}"
            system = "Follow the SYSTEM INSTRUCTIONS block at the top of the message."
        own = {k: str(v) for k, v in (self.params.get("env") or {}).items()}   # the Base Agent expands $NAME
        err = Path(cwd) / ".session.err"
        # upstream reads one JSON result object; on a login the whole event stream (its last line is that object): the
        # Base Agent reads from it what paid for the session, the plan's windows and a refusal's reset
        settings = {"web_search": False, "bin": self.params.get("claude_bin") or "claude", "stdin": True,
                    "output_format": "stream-json" if self.params.get("login") else "json",
                    "effort": self.spec.effort, "persist_session": False,
                    "allowed_tools": str(self.params.get("tools") or "Read,Write,Edit,Glob,Grep,Bash").split(","),
                    "append_system_prompt": system or None, "home": home, "home_files": [],
                    "log": Path(cwd) / ".session.out", "stderr_log": err, "env": {**QUIET, **own},
                    "env_drop": sorted(_DROP_ENV),
                    "isolation": "sandbox" if self.sandbox is not None else "none", "sandbox": self.sandbox,
                    "writable": [Path(cwd)], "timeout_s": wall, "cancel": self.cancel}
        if self.params.get("login"):      # the user's subscription: its model by name, its plan's waits (Base Agent)
            keys = ("login", "model", "plan_dir", "plan_max_wait_s", "plan_rerun", "usage_credits")
            settings.update({k: self.params[k] for k in keys      # plan_max_wait_s: null is no limit
                             if self.params.get(k) is not None or (k == "plan_max_wait_s" and k in self.params)})
        result = make("claude", settings).run(stdin, workspace=Path(cwd))
        if result.extra.get("login_failed"):
            raise LoginFailed(f"a Harness² session: {result.extra.get('failure')}")
        if result.extra["cancelled"]:
            raise RuntimeError("the session was stopped by the Trainer (budget or timeout)")
        if result.timed_out:
            raise subprocess.TimeoutExpired(["claude"], wall)
        if result.log is None:              # never started: the plan held it past plan_max_wait_s, or no slot came
            return "", 0.0, f"not run: {result.extra.get('failure') or 'the session did not start'}"
        payload = _parse_json(_read_own(Path(result.log)))
        if not result.extra.get("served"):  # a startup error's text is no reply (upstream would take plain text)
            usage = None if result.usage else self._usage(payload)        # what it spent before it failed is kept
            cost = self._spent(result.usage) if result.usage else float(usage.cost_usd or 0.0) if usage else 0.0
            return "", cost, f"rc={result.exit_code}: {(result.extra.get('failure') or _read_own(err))[-300:]}"
        with self._usage_lock:
            self.served += 1
        # A served session's reply as the Base Agent read it, as the Codex route takes it: the stream's final message
        # whole, where the CLI's `result` keeps only the last part of a message it continued past its output limit.
        text = result.reply
        problem = ""
        if payload.get("is_error") or payload.get("subtype") not in (None, "success"):
            problem = f"CLI error: {payload.get('subtype')} {str(text)[:200]}"
            text = ""
        elif result.exit_code != 0 and not text.strip():
            problem = f"rc={result.exit_code}: {_read_own(err)[-300:]}"
        if self.params.get("login") and result.usage:     # the stream's every result, sub-agents' included
            return text, self._spent(result.usage), problem
        usage = self._usage(payload)
        return text, float(usage.cost_usd or 0.0) if usage else 0.0, problem

    def _codex_cli(self, cwd: Path, mission: str, system: str, wall: int) -> tuple[str, float, str]:
        """The session on Codex: the `codex` Base Agent, which on a ChatGPT login also waits for the plan and runs a
        refused session again (hermit.agent.base.login). (Ported from examples/officeqa/harness2_stream/
        codex_proposer.py, PR #54.)"""
        from hermit.agent.base.cli import EFFORTS, codex_instructions
        own = {k: str(v) for k, v in (self.params.get("env") or {}).items()}
        err = Path(cwd) / ".session.err"
        extra, mission = codex_instructions(system, mission)
        settings = {"web_search": False, "bin": self.params.get("claude_bin") or "codex", "stdin": True,
                    "effort": EFFORTS.get(self.spec.effort, self.spec.effort),     # Claude Code's max is Codex's xhigh
                    "model": self.params.get("model") or self.spec.model,
                    "login": self.params.get("login"), "login_root": self.params.get("login_root"),
                    "plan_dir": self.params.get("plan_dir"), "plan_max_wait_s": self.params.get("plan_max_wait_s"),
                    "plan_rerun": self.params.get("plan_rerun"), "usage_credits": self.params.get("usage_credits"),
                    "binding": self.params.get("binding"), "price": self.params.get("price"),
                    "args": [*(self.params.get("args") or []), *extra],
                    "codex_sandbox": "danger-full-access",      # the run's sandbox is the boundary, as for Claude Code
                    "home": Path(cwd) / ".codex-home", "home_files": [], "log": Path(cwd) / ".session.out",
                    "stderr_log": err, "env": {**QUIET, **own}, "env_drop": sorted(_DROP_ENV),
                    "isolation": "sandbox" if self.sandbox is not None else "none", "sandbox": self.sandbox,
                    "writable": [Path(cwd)], "timeout_s": wall, "cancel": self.cancel}
        settings = {k: v for k, v in settings.items() if v is not None or k == "plan_max_wait_s" and k in self.params}
        result = make("codex", settings).run(mission, workspace=Path(cwd))     # plan_max_wait_s: null is no limit
        return self._reply("codex", result, err, wall)

    def _pi_cli(self, cwd: Path, mission: str, system: str, wall: int) -> tuple[str, float, str]:
        """The session on Pi: the `pi` Base Agent on the bound model (its models.json provider), the mission on stdin,
        upstream's system text appended to Pi's own, Pi's own tools (read, bash, edit, write) inside the run's sandbox,
        no session kept."""
        own = {k: str(v) for k, v in (self.params.get("env") or {}).items()}
        err = Path(cwd) / ".session.err"
        key = (self.params.get("binding") or {}).get("api_key_env")          # upstream drops Anthropic's for Claude
        settings = {"web_search": False, "bin": self.params.get("claude_bin") or "pi", "stdin": True,
                    "effort": self.spec.effort,                               # Claude Code's levels are Pi's too
                    "model": self.params.get("model") or self.spec.model, "binding": self.params.get("binding"),
                    **{k: self.params.get(k) for k in PI_PARAMS},              # the bound model, images, command limit
                    "price": self.params.get("price"), "args": self.params.get("args"),
                    "append_system_prompt": system or None, "persist_session": False,
                    "home": Path(cwd) / ".pi-home", "home_files": [], "log": Path(cwd) / ".session.out",
                    "stderr_log": err, "env": {**QUIET, **own}, "env_drop": sorted(set(_DROP_ENV) - {key}),
                    "isolation": "sandbox" if self.sandbox is not None else "none", "sandbox": self.sandbox,
                    "writable": [Path(cwd)], "timeout_s": wall, "cancel": self.cancel}
        result = make("pi", {k: v for k, v in settings.items() if v is not None}).run(mission, workspace=Path(cwd))
        return self._reply("pi", result, err, wall)

    def _reply(self, program: str, result, err: Path, wall: int) -> tuple[str, float, str]:
        """(reply, cost, problem) of a Codex or Pi session. Its usage is kept for the step, a failed, stopped or timed
        out session's too; a reply cut at the output limit is no reply."""
        with self._usage_lock:
            self.usages.extend(result.usage)
        if result.extra["cancelled"]:
            raise RuntimeError("the session was stopped by the Trainer (budget or timeout)")
        if result.timed_out:
            raise subprocess.TimeoutExpired([program], wall)
        cost = sum(float(u.cost_usd or 0.0) for u in result.usage)
        if not result.extra["served"] or not result.reply.strip() or result.finish_reason == "length":
            tail = (_read_own(err) or result.extra.get("failure") or result.extra.get("output_tail", ""))[-300:]
            return "", cost, f"rc={result.exit_code}: {tail}"
        with self._usage_lock:
            self.served += 1
        return result.reply, cost, ""

    def _harvest(self, tmp: Path, sub: str, label: str, cache_key: str = "") -> Path | None:
        """Upstream's harvest, without following links: the session wrote this tree, and the harvest runs outside its
        sandbox. A linked directory on the way gives nothing back (upstream retries the stage); links inside are
        copied as links and dropped."""
        path = Path(tmp)
        for part in Path(sub).parts:
            path = path / part
            if path.is_symlink():
                return None
        if not path.is_dir():
            return None
        dest = Path(self.audit_dir or tempfile.mkdtemp(prefix="harness2-harvest-"))
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", label) + (f".{cache_key}" if cache_key else "")
        dest = dest / f"{safe}.workspace"
        shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(path, dest, symlinks=True)
        for link in [q for q in dest.rglob("*") if q.is_symlink()]:
            link.unlink()
        return dest

    def _spent(self, usage: list) -> float:
        """The Base Agent's usage of one session, priced as the backend bills it and kept; its cost."""
        price_usage(usage, self.params)              # the backend's price, not Claude Code's own
        with self._usage_lock:
            self.usages.extend(usage)
        return sum(float(u.cost_usd or 0.0) for u in usage)

    def _usage(self, payload: dict) -> Usage | None:
        u = payload.get("usage") or {}
        if not any(int(u.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                                                 "cache_creation_input_tokens")):
            return None                          # no model call reached the endpoint
        cached = int(u.get("cache_read_input_tokens") or 0)
        usage = Usage(model=str(self.params.get("model") or self.spec.model),
                      input_tokens=int(u.get("input_tokens") or 0) + cached
                      + int(u.get("cache_creation_input_tokens") or 0),
                      cached_input_tokens=cached, output_tokens=int(u.get("output_tokens") or 0),
                      calls=int(payload.get("num_turns") or 1))
        price_usage([usage], self.params)        # the backend's price, not Claude Code's own
        with self._usage_lock:
            self.usages.append(usage)
        return usage
