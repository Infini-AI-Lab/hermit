"""Harness²'s editor sessions as Codex sessions (`codex exec`) instead of Claude Code (`claude -p`), for a run whose
editors' model is reachable only through Codex: a ChatGPT login (a Codex plan, ../utils/agent/codex_login.py).

    evolver:
      params:
        model: codexplan/gpt-5.6-luna            # a backend at ChatGPT's Codex endpoint (codex_login.py)
        proposer: codex_proposer.py:CodexProposer
        claude_bin: /path/to/pin/bin/codex       # the Codex binary, a copy alone in its package directory: the
                                                 # Evolver shows the sessions' sandbox the program it names
    sandbox: {allow: [chatgpt.com]}              # the sessions reach the model through the run's allowlist proxy

Everything but the session program is upstream's channel as the framework runs it (channel.FrameworkProposer): the
stdin mission, the system text, the session directory with its copy of the evidence, the run's sandbox for the
editors (the system, the program's installation and the session directory; the network only through the allowlist
proxy), the wall clock, retries and the harvest of the edited `context/harness`. The session: `codex exec --json` in
the session directory at the reasoning effort upstream asks for, Codex's own sandbox off inside the run's (as Claude
Code's permissions are skipped there), the system text as Codex's developer instructions, and a fresh CODEX_HOME in
the session directory with codex_login's settings and a copy of the login, removed when the session ends. The reply
is the session's final message. Its usage is recorded unpriced (a plan has no per-token price). A session waits while
the runs are paused, and one refused for the plan's limit is run again.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
import tomllib

from hermit.algorithms.evolvers.harness2_upstream.channel import FrameworkProposer, _kill
from hermit.evolver.base import Usage

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "utils" / "agent"))
import codex_login  # noqa: E402


@dataclass
class CodexProposer(FrameworkProposer):

    def _claude_cli(self, cwd: Path, mission: str, system: str, wall: int) -> tuple[str, float, str]:
        say = lambda m: print(f"harness2 codex editor: {m}", file=sys.stderr, flush=True)
        while True:
            codex_login.wait_for_plan(log=say)
            text, problem, refused = self._codex(Path(cwd), mission, system, wall)
            if not refused:
                return text, 0.0, problem
            codex_login.hold_for_limit(log=say)          # refused for the plan's limit: run the session again

    def _codex(self, cwd: Path, mission: str, system: str, wall: int) -> tuple[str, str, bool]:
        home = cwd / ".codex-home"
        shutil.rmtree(home, ignore_errors=True)
        home.mkdir(parents=True)
        lines = [*codex_login.config_lines(), "features.shell_snapshot = false"]
        if system:
            lines.append(f"developer_instructions = {json.dumps(system, ensure_ascii=False)}")
        lines += ["", "[shell_environment_policy]", 'inherit = "core"', 'exclude = ["*KEY*", "*TOKEN*", "*SECRET*"]']
        text = "\n".join(lines) + "\n"
        tomllib.loads(text)
        (home / "config.toml").write_text(text, encoding="utf-8")
        codex_login.install_auth(home)
        last = cwd / ".session.last"
        cmd = [str(self.params.get("claude_bin") or "codex"), "exec", "--json", "--skip-git-repo-check",
               "--dangerously-bypass-approvals-and-sandbox", "-C", str(cwd),
               "-m", str(self.params.get("model") or self.spec.model),
               "-c", f'model_reasoning_effort="{self.spec.effort}"', "-o", str(last), "-"]
        env = {**self.env(), "CODEX_HOME": str(home), "HOME": str(home)}
        if self.sandbox is not None:
            cmd, env = self.sandbox.wrap(cmd, writable=[cwd], env=env, home=home, keep=["CODEX_HOME"])
        out_path, err_path = cwd / ".session.out", cwd / ".session.err"
        try:
            with open(out_path, "w") as out, open(err_path, "w") as err, \
                    subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=out, stderr=err, text=True,
                                     start_new_session=True) as proc:
                try:
                    proc.stdin.write(mission)
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
        finally:
            codex_login.note_usage(home)
            codex_login.remove_auth(home)
        events = []
        for line in out_path.read_text(errors="replace").splitlines():
            if line.startswith("{"):
                try:
                    events.append(json.loads(line))
                except ValueError:
                    pass
        refusals = " ".join(str(e.get("message") or (e.get("error") or {}).get("message") or "") for e in events
                            if e.get("type") in ("error", "turn.failed"))
        served = [e for e in events if e.get("type") == "turn.completed"]
        if not served and codex_login.limit_hit(refusals):
            return "", "the plan's usage limit", True
        reply = last.read_text(errors="replace") if last.is_file() else ""
        if not reply.strip():
            texts = [(e.get("item") or {}).get("text") or "" for e in events if e.get("type") == "item.completed"
                     and (e.get("item") or {}).get("type") == "agent_message"]
            reply = texts[-1] if texts else ""
        problem = ""
        if not served:
            problem = f"rc={proc.returncode}: {(refusals or err_path.read_text(errors='replace'))[-300:]}"
            reply = ""
        if served:
            u = served[-1].get("usage") or {}
            usage = Usage(model=str(self.params.get("model") or self.spec.model),
                          input_tokens=int(u.get("input_tokens") or 0),
                          cached_input_tokens=int(u.get("cached_input_tokens") or 0),
                          output_tokens=int(u.get("output_tokens") or 0), calls=len(served), cost_usd=None)
            with self._usage_lock:
                self.usages.append(usage)
        return reply, problem, False
