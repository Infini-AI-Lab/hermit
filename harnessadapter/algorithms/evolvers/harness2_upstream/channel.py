"""Upstream Harness²'s session channel (harness2.core.proposer.Proposer, `claude_cli`) on a framework-bound model.

Upstream starts `claude -p` with Vertex settings and no sandbox. Here the session talks to the bound backend's
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
import signal
import subprocess
import threading
import time
from typing import Any

from harness2.core.proposer import SYSTEM_ARG_MAX, Proposer, _DROP_ENV, _parse_json

from harnessadapter.evolver.agent import price_usage
from harnessadapter.evolver.base import Usage

QUIET = {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1", "DISABLE_TELEMETRY": "1",
         "DISABLE_ERROR_REPORTING": "1", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}


@dataclass
class FrameworkProposer(Proposer):
    params: dict = field(default_factory=dict)    # the Evolver's bound params: env, price, binding, model, tools
    sandbox: Any = None                           # harnessadapter.sandbox.Sandbox for the sessions, or None
    cancel: Any = None                            # threading.Event set by the Trainer to stop the step
    usages: list = field(default_factory=list)    # one Usage per session attempt, for the step's StepResult
    _usage_lock: threading.Lock = field(default_factory=threading.Lock)

    def env(self) -> dict[str, str]:
        own = {k: os.path.expandvars(str(v)) for k, v in (self.params.get("env") or {}).items()}
        return {**{k: v for k, v in os.environ.items() if k not in _DROP_ENV}, **QUIET, **own}

    def _claude_cli(self, cwd: Path, mission: str, system: str, wall: int) -> tuple[str, float, str]:
        home = Path(cwd) / ".claude-home"
        home.mkdir(parents=True, exist_ok=True)
        env = self.env()
        env["CLAUDE_CONFIG_DIR"] = str(home)
        cmd = [str(self.params.get("claude_bin") or "claude"), "--dangerously-skip-permissions", "-p",
               "--output-format", "json", "--effort",
               self.spec.effort, "--tools", str(self.params.get("tools") or "Read,Write,Edit,Glob,Grep,Bash"),
               "--no-session-persistence"]
        stdin = mission
        if system:
            if len(system) > SYSTEM_ARG_MAX:
                stdin = f"# SYSTEM INSTRUCTIONS\n\n{system}\n\n# END\n\n{mission}"
                cmd += ["--append-system-prompt", "Follow the SYSTEM INSTRUCTIONS block at the top of the message."]
            else:
                cmd += ["--append-system-prompt", system]
        if self.sandbox is not None:
            keep = [*(self.params.get("env") or {}), *QUIET, "CLAUDE_CONFIG_DIR"]
            cmd, env = self.sandbox.wrap(cmd, writable=[Path(cwd)], env=env, home=home, keep=keep)
        out_path, err_path = Path(cwd) / ".session.out", Path(cwd) / ".session.err"
        with open(out_path, "w") as out, open(err_path, "w") as err, \
                subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=out, stderr=err, text=True,
                                 start_new_session=True) as proc:
            try:
                proc.stdin.write(stdin)
                proc.stdin.close()
            except BrokenPipeError:
                pass
            started = time.time()
            while proc.poll() is None:
                stop = self.cancel is not None and self.cancel.is_set()
                if stop or time.time() - started > wall:
                    _kill(proc)
                    if stop:
                        raise RuntimeError("the session was stopped by the Trainer (budget or timeout)")
                    raise subprocess.TimeoutExpired(cmd, wall)
                time.sleep(1)
        payload = _parse_json(out_path.read_text(errors="replace"))
        text = str(payload.get("result") or payload.get("text") or "")
        problem = ""
        if payload.get("is_error") or payload.get("subtype") not in (None, "success"):
            problem = f"CLI error: {payload.get('subtype')} {str(text)[:200]}"
            text = ""
        elif proc.returncode != 0 and not text.strip():
            problem = f"rc={proc.returncode}: {err_path.read_text(errors='replace')[-300:]}"
        usage = self._usage(payload)
        return text, float(usage.cost_usd or 0.0) if usage else 0.0, problem

    def _usage(self, payload: dict) -> Usage | None:
        u = payload.get("usage") or {}
        if not u:
            return None
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


def _kill(proc: subprocess.Popen) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=10)
            return
        except subprocess.TimeoutExpired:
            continue
