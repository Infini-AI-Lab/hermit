"""Upstream Harness²'s session channel (harness2.core.proposer.Proposer, `claude_cli`) on a framework-bound model.

Upstream starts `claude -p` with Vertex settings and no sandbox. Here the session is one call of the `claude` Base
Agent (Layer 2): it talks to the bound backend's
Messages API (the binding's Claude Code environment: base URL, key, model for every alias), gets a fresh config
directory, sees only its own copy of the evidence (the run's sandbox: system files, the Claude install, the session
directory; the network only through the forwarded model endpoint), and its cost is priced from its token usage at the
backend's prices. Everything else - the stdin mission, the JSON reply, retries, harvesting the edited
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

from hermit.agent.base.interface import make
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
    _usage_lock: threading.Lock = field(default_factory=threading.Lock)

    def env(self) -> dict[str, str]:
        own = {k: os.path.expandvars(str(v)) for k, v in (self.params.get("env") or {}).items()}
        return {**{k: v for k, v in os.environ.items() if k not in _DROP_ENV}, **QUIET, **own}

    def _claude_cli(self, cwd: Path, mission: str, system: str, wall: int) -> tuple[str, float, str]:
        """One upstream session as one call of a CLI Base Agent (docs/framework/agent-layers.md): `claude` by default,
        upstream's command (stdin mission, its system prompt, one JSON result, its tools, no session kept); `codex`
        with `params.backend: codex` (a model only Codex reaches: a ChatGPT login), the mission on stdin, the system
        text as its developer instructions, its own sandbox off inside the run's. Either has its own config directory,
        runs in the run's sandbox with the session directory writable, and is stopped at `wall` or by the Trainer."""
        if self.params.get("backend") == "codex":
            return self._codex_cli(cwd, mission, system, wall)
        home = Path(cwd) / ".claude-home"
        stdin = mission
        if system and len(system) > SYSTEM_ARG_MAX:
            stdin = f"# SYSTEM INSTRUCTIONS\n\n{system}\n\n# END\n\n{mission}"
            system = "Follow the SYSTEM INSTRUCTIONS block at the top of the message."
        own = {k: str(v) for k, v in (self.params.get("env") or {}).items()}   # the Base Agent expands $NAME
        err = Path(cwd) / ".session.err"
        settings = {"web_search": False, "bin": self.params.get("claude_bin") or "claude", "stdin": True,
                    "output_format": "json", "effort": self.spec.effort, "persist_session": False,
                    "allowed_tools": str(self.params.get("tools") or "Read,Write,Edit,Glob,Grep,Bash").split(","),
                    "append_system_prompt": system or None, "home": home, "home_files": [],
                    "log": Path(cwd) / ".session.out", "stderr_log": err, "env": {**QUIET, **own},
                    "env_drop": sorted(_DROP_ENV),
                    "isolation": "sandbox" if self.sandbox is not None else "none", "sandbox": self.sandbox,
                    "writable": [Path(cwd)], "timeout_s": wall, "cancel": self.cancel}
        result = make("claude", settings).run(stdin, workspace=Path(cwd))
        if result.extra["cancelled"]:
            raise RuntimeError("the session was stopped by the Trainer (budget or timeout)")
        if result.timed_out:
            raise subprocess.TimeoutExpired(["claude"], wall)
        payload = _parse_json(_read_own(Path(result.log)))
        text = str(payload.get("result") or payload.get("text") or "")
        problem = ""
        if payload.get("is_error") or payload.get("subtype") not in (None, "success"):
            problem = f"CLI error: {payload.get('subtype')} {str(text)[:200]}"
            text = ""
        elif result.exit_code != 0 and not text.strip():
            problem = f"rc={result.exit_code}: {_read_own(err)[-300:]}"
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
                    "binding": self.params.get("binding"), "price": self.params.get("price"),
                    "args": [*(self.params.get("args") or []), *extra],
                    "codex_sandbox": "danger-full-access",      # the run's sandbox is the boundary, as for Claude Code
                    "home": Path(cwd) / ".codex-home", "home_files": [], "log": Path(cwd) / ".session.out",
                    "stderr_log": err, "env": {**QUIET, **own}, "env_drop": sorted(_DROP_ENV),
                    "isolation": "sandbox" if self.sandbox is not None else "none", "sandbox": self.sandbox,
                    "writable": [Path(cwd)], "timeout_s": wall, "cancel": self.cancel}
        result = make("codex", {k: v for k, v in settings.items() if v is not None}).run(mission, workspace=Path(cwd))
        if result.extra["cancelled"]:
            raise RuntimeError("the session was stopped by the Trainer (budget or timeout)")
        if result.timed_out:
            raise subprocess.TimeoutExpired(["codex"], wall)
        if not result.extra["served"] or not result.reply.strip():
            tail = (_read_own(err) or result.extra.get("output_tail", ""))[-300:]
            return "", 0.0, f"rc={result.exit_code}: {tail}"
        cost = 0.0
        for usage in result.usage:
            with self._usage_lock:
                self.usages.append(usage)
            cost += float(usage.cost_usd or 0.0)
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
