"""AgentEvolver: a built-in agent (Codex or Claude Code) run on the workspace with a prompt; CommandEvolver: any program.

AgentEvolver params:
  backend: codex | claude
  model: the model name passed to the agent (optional: the agent's default)
  prompt: start prompt file, relative to the Evolver's directory (placeholders: {message} {epoch} {step} {adapter}
          {learning_material} {task_info} {history} {state} {scratch} {output})
  resume_prompt: prompt file for a resumed session (optional; default: the start prompt)
  args: extra command-line arguments for the agent (for example codex `-c key=value` pairs)
  home: copy these files of the user's agent home into the run's agent home (default: auth.json / .credentials.json)
  price: USD per million tokens {input, cached_input, output}; without it an agent's cost is unknown (counted as such)
The Trainer's tools reach the agent as one MCP server (`evolver_tools`) that forwards to the tool endpoint.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

from harnessadapter.evolver.base import Context, Evolver, StepContext, StepResult, Usage


def _fill(text: str, ctx: StepContext) -> str:
    ws = ctx.workspace
    values = {"message": ctx.message, "epoch": getattr(ctx.progress, "epoch", ""), "step": getattr(ctx.progress, "step", ""),
              "adapter": ws.adapter, "learning_material": ws.learning_material, "task_info": ws.task_info,
              "history": ws.history, "state": ws.state, "scratch": ws.scratch, "output": ws.output}
    for key, value in values.items():
        text = text.replace("{" + key + "}", str(value))
    return text


def _run(command: list[str], env: dict, ctx: Context, log: Path, cwd: Path) -> int:
    """Run a process group, streaming stdout to `log`; kill it when the Trainer cancels the call."""
    with open(log, "w") as out:
        proc = subprocess.Popen(command, stdout=out, stderr=subprocess.STDOUT, env=env, cwd=str(cwd),
                                start_new_session=True)
        while proc.poll() is None:
            if ctx.cancel is not None and ctx.cancel.is_set():
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(15)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                break
            time.sleep(1)
        return proc.wait()


class AgentEvolver(Evolver):
    supports_resume = True

    def _home(self, ctx: Context, backend: str) -> Path:
        home = ctx.workspace.state.parent / "agent_home"
        home.mkdir(parents=True, exist_ok=True)
        source = Path.home() / (".codex" if backend == "codex" else ".claude")
        for name in ctx.params.get("home", ["auth.json", ".credentials.json"]):
            if (source / name).is_file() and not (home / name).exists():
                shutil.copy2(source / name, home / name)
                os.chmod(home / name, 0o600)
        return home

    def finalize(self, ctx: Context):
        """Remove the copied login files from the run's agent home; the rest of the home is kept for inspection."""
        home = ctx.workspace.state.parent / "agent_home"
        for name in ctx.params.get("home", ["auth.json", ".credentials.json"]):
            if (home / name).is_file():
                (home / name).unlink()
        return super().finalize(ctx)

    def step(self, ctx: StepContext) -> StepResult:
        params = ctx.params
        backend = params.get("backend", "codex")
        directory = Path(getattr(self, "directory", "."))
        prompt_file = params.get("resume_prompt") if ctx.session and params.get("resume_prompt") else params["prompt"]
        prompt = _fill((directory / prompt_file).read_text(), ctx)
        home = self._home(ctx, backend)
        log = ctx.workspace.output / f"agent-{backend}.jsonl"
        tools = [sys.executable, "-m", "harnessadapter.tools_mcp", "--url", ctx.tool_url or ""]
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2]) + os.pathsep + env.get("PYTHONPATH", "")
        root = ctx.workspace.adapter.parent
        if backend == "codex":
            env["CODEX_HOME"] = str(home)
            command = ["codex", "exec", "--json", "--skip-git-repo-check", "--ignore-user-config",
                       "-s", "danger-full-access", "-C", str(root),
                       "-c", f"mcp_servers.evolver_tools.command={json.dumps(tools[0])}",
                       "-c", f"mcp_servers.evolver_tools.args={json.dumps(tools[1:])}",
                       "-c", "mcp_servers.evolver_tools.tool_timeout_sec=1800",
                       *(["-m", params["model"]] if params.get("model") else []), *params.get("args", [])]
            command += (["resume", ctx.session, prompt] if ctx.session else [prompt])
        elif backend == "claude":
            env["CLAUDE_CONFIG_DIR"] = str(home)
            mcp = {"mcpServers": {"evolver_tools": {"type": "stdio", "command": tools[0], "args": tools[1:]}}}
            command = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose",
                       "--mcp-config", json.dumps(mcp), "--permission-mode", "bypassPermissions",
                       *(["--model", params["model"]] if params.get("model") else []),
                       *(["--resume", ctx.session] if ctx.session else []), *params.get("args", [])]
        else:
            return StepResult("failed", f"unknown backend {backend!r}")
        if ctx.sandbox is not None:
            command, env = ctx.sandbox.wrap(command, writable=[home], env=env, home=home)
        code = _run(command, env, ctx, log, root)
        session, usage = parse_log(log, backend, params.get("model") or backend)
        price = params.get("price")       # USD per million tokens: {input, cached_input, output}
        for u in usage:
            if price and u.cost_usd is None:
                u.cost_usd = ((u.input_tokens - u.cached_input_tokens) * price.get("input", 0)
                              + u.cached_input_tokens * price.get("cached_input", price.get("input", 0))
                              + u.output_tokens * price.get("output", 0)) / 1e6
        if ctx.cancel is not None and ctx.cancel.is_set():
            return StepResult("failed", "stopped by the Trainer", session=session, usage=usage)
        report = ctx.workspace.output / "report.md"
        summary = report.read_text().strip().splitlines()[0][:300] if report.exists() and report.read_text().strip() \
            else f"{backend} session finished"
        status = "done" if code == 0 else "failed"
        return StepResult(status, summary if code == 0 else f"{backend} exited with {code}", session=session,
                          usage=usage)


def parse_log(log: Path, backend: str, model: str) -> tuple[str | None, list[Usage]]:
    """Session handle and usage from the agent's JSON event stream."""
    session, usage = None, Usage(model=model)
    if not log.exists():
        return None, []
    for line in log.read_text(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if backend == "codex":
            if event.get("type") == "thread.started":
                session = event.get("thread_id") or session
            if event.get("type") == "turn.completed" and event.get("usage"):
                u = event["usage"]
                usage.input_tokens += int(u.get("input_tokens", 0))
                usage.cached_input_tokens += int(u.get("cached_input_tokens", 0))
                usage.output_tokens += int(u.get("output_tokens", 0))
                usage.calls += 1
        else:
            session = event.get("session_id") or session
            if event.get("type") == "result":
                u = event.get("usage") or {}
                usage.input_tokens += int(u.get("input_tokens", 0)) + int(u.get("cache_read_input_tokens", 0))
                usage.cached_input_tokens += int(u.get("cache_read_input_tokens", 0))
                usage.output_tokens += int(u.get("output_tokens", 0))
                usage.calls += int(event.get("num_turns", 1))
                if event.get("total_cost_usd") is not None:
                    usage.cost_usd = float(event["total_cost_usd"])
    return session, ([usage] if usage.calls else [])


class CommandEvolver(Evolver):
    """Any external program. params: command (list), env (dict).

    The program gets HA_CONTEXT (a JSON file with the workspace paths, accesses, progress, message, params) and
    HA_TOOL_URL (the tool endpoint: GET /tools, POST /call/<name>). It changes the adapter directory and may write
    output/step_result.json: {status, summary, rationale, session, usage: [Usage fields...]}.
    """
    supports_resume = True

    def step(self, ctx: StepContext) -> StepResult:
        info = {"workspace": {k: str(v) for k, v in ctx.workspace.__dict__.items()},
                "access": {"validation": str(ctx.access.validation) if ctx.access.validation else None,
                           "environment": [str(p) for p in ctx.access.environment]},
                "progress": ctx.progress.__dict__ if ctx.progress else None, "session": ctx.session,
                "message": ctx.message, "params": {k: v for k, v in ctx.params.items() if k not in ("command", "env")}}
        path = ctx.workspace.scratch / "context.json"
        path.write_text(json.dumps(info, indent=1))
        env = {**os.environ, **{k: str(v) for k, v in (ctx.params.get("env") or {}).items()},
               "HA_CONTEXT": str(path), "HA_TOOL_URL": ctx.tool_url or ""}
        command = ctx.params["command"]
        command = command.split() if isinstance(command, str) else list(command)
        if ctx.sandbox is not None:
            command, env = ctx.sandbox.wrap(command, env=env)
        code = _run(command, env, ctx, ctx.workspace.output / "command.log", Path(getattr(self, "directory", ".")))
        result_file = ctx.workspace.output / "step_result.json"
        if result_file.exists():
            data = json.loads(result_file.read_text())
            usage = [Usage(**u) for u in data.get("usage", [])]
            return StepResult(data.get("status", "done" if code == 0 else "failed"), data.get("summary", ""),
                              data.get("rationale", ""), data.get("session"), usage, data.get("extra", {}))
        return StepResult("done" if code == 0 else "failed", f"command exited with {code}")
