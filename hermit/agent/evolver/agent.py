"""AgentEvolver: a built-in agent (Codex or Claude Code) run on the workspace with a prompt; CommandEvolver: any program.

AgentEvolver params:
  backend: codex | claude
  model: the model passed to the agent (optional: the agent's default); with `login: chatgpt`, Codex's model name
         under the user's ChatGPT sign-in (the shared copy of it, hermit.agent.base.login, outside the run's
         directory). A `<backend>/<model>` reference (see
         hermit.agent.model.backends) binds it to that backend: the provider settings go into `args` (Codex) or `env`
         (Claude Code) and `price` is taken from the backend
  env: environment variables set for the agent; a `$NAME` value is expanded from the environment at launch (Claude
       Code's ANTHROPIC_BASE_URL and token come from the binding this way, so the run's records hold no key)
  prompt: start prompt file, relative to the Evolver's directory (placeholders: {message} {epoch} {step} {adapter}
          {learning_material} {task_info} {history} {state} {scratch} {output})
  resume_prompt: prompt file for a resumed session (optional; default: the start prompt)
  args: extra command-line arguments for the agent (for example codex `-c key=value` pairs)
  home: copy these files of the user's agent home into the run's agent home (default: auth.json / .credentials.json)
  price: USD per million tokens {input, cached_input, output}; without it an agent's cost is unknown (counted as such).
         Claude Code's own cost figure (Anthropic's prices) counts only for a model on Anthropic's backend
  tools: the only Trainer tools the agent may use (for example [check_spec]); null: all of them. With a list, the
         task message names no endpoint URL either
  hide: paths the agent must not read although the sandbox shows their parents, besides the framework's examples,
        tests, docs and .git; "module:<name>" hides an installed package. null: nothing is hidden
  web_search: the CLI's web search, true or false
This is Layer 3 over the `codex` or `claude` Base Agent (docs/framework/agent-layers.md): it fills the prompt, picks
the Trainer's tools and the paths to hide, and resumes the last session; the Base Agent runs the CLI in the run's
sandbox, with the tools as one MCP server (`evolver_tools`).
"""
from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess

import time

from hermit.agent.base.interface import Limits, Mount, make
from hermit.agent.base.records import parse_log, price_usage   # noqa: F401 (re-exported)
from hermit.agent.evolver.base import Context, Evolver, StepContext, StepResult, Usage


def _fill(text: str, ctx: StepContext) -> str:
    ws = ctx.workspace
    values = {"message": ctx.message, "epoch": getattr(ctx.progress, "epoch", ""), "step": getattr(ctx.progress, "step", ""),
              "adapter": ws.adapter, "learning_material": ws.learning_material, "task_info": ws.task_info,
              "history": ws.history, "state": ws.state, "scratch": ws.scratch, "output": ws.output}
    for key, value in values.items():
        text = text.replace("{" + key + "}", str(value))
    return text


def _run(command: list[str], env: dict, ctx: Context, log: Path, cwd: Path) -> int:
    """Run a process group, streaming stdout to `log`; kill it when the Trainer cancels the call. Its stdin is
    empty: a coding-agent CLI given a prompt still reads a non-terminal stdin to its end as more prompt, so one
    inherited from a shell or a service (a socket, a pipe that never closes) would hang the session."""
    with open(log, "w") as out:
        proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, env=env,
                                cwd=str(cwd), start_new_session=True)
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

    def step(self, ctx: StepContext) -> StepResult:
        params = ctx.params
        backend = params.get("backend", "codex")
        if backend not in ("codex", "claude"):
            return StepResult("failed", f"unknown backend {backend!r}")
        if not isinstance(params.get("web_search"), bool):
            return StepResult("failed", "evolver.params.web_search must be set, true or false")
        directory = Path(getattr(self, "directory", "."))
        allowed = params.get("tools")
        tools = [t for name, t in ctx.tools.items() if allowed is None or name in allowed]
        message = ctx.message
        if allowed is not None and ctx.tool_url:
            # The Trainer's message names every tool and the endpoint's URL; the agent gets the allowed tools over
            # MCP and no URL, so a shell cannot reach the endpoint past the filter.
            message = message.replace(ctx.tool_url, "(not given: use the evolver_tools MCP server)")
            message = re.sub(r"^Tools: .*$", "Tools: " + ", ".join(f"{n} (template)" for n in allowed), message,
                             flags=re.M)
        ctx = dataclasses.replace(ctx, message=message)
        prompt_file = params.get("resume_prompt") if ctx.session and params.get("resume_prompt") else params["prompt"]
        prompt = _fill((directory / prompt_file).read_text(), ctx)
        limits = Limits()
        # Hidden paths are asked for only where the run's sandbox can cover them (as before, an unconfined run, by
        # sandbox.enabled: false or best_effort without bubblewrap, hides nothing).
        if params.get("hide") is not None and getattr(ctx.sandbox, "enabled", False) and shutil.which("bwrap"):
            from hermit.sandbox import hidden_paths
            limits = Limits(hidden=hidden_paths(params["hide"]),
                            readable=[Mount(ctx.workspace.task_info, ctx.workspace.task_info)])
        result = make(backend, self.settings(ctx, backend)).run(prompt, workspace=ctx.workspace.adapter.parent,
                                                                tools=tools, limits=limits, session=ctx.session)
        if ctx.cancel is not None and ctx.cancel.is_set():
            return StepResult("failed", "stopped by the Trainer", session=result.session, usage=result.usage)
        report = ctx.workspace.output / "report.md"
        summary = report.read_text().strip().splitlines()[0][:300] if report.exists() and report.read_text().strip() \
            else f"{backend} session finished"
        code = result.exit_code
        return StepResult("done" if code == 0 else "failed", summary if code == 0 else f"{backend} exited with {code}",
                          session=result.session, usage=result.usage)

    def settings(self, ctx: Context, backend: str) -> dict:
        """The Base Agent's settings: the learner's CLI in the run's sandbox (whose own isolation is the boundary,
        so the CLI's sandbox is off), with every variable for its commands, a home kept across calls for resume, no
        clock (the Trainer stops the call), the framework importable."""
        params = ctx.params
        env = {**(params.get("env") or {}),
               "PYTHONPATH": str(Path(__file__).resolve().parents[3]) + os.pathsep + os.environ.get("PYTHONPATH", "")}
        return {**{k: params[k] for k in ("model", "binding", "login", "args", "price", "effort")
                   if params.get(k) is not None},
                "web_search": params["web_search"], "env": env,
                "isolation": "sandbox" if ctx.sandbox is not None else "none", "sandbox": ctx.sandbox,
                "codex_sandbox": "danger-full-access", "shell_env": "all",
                "home": ctx.workspace.state.parent / "agent_home", "home_files": params.get("home"),
                "log": ctx.workspace.output / f"agent-{backend}.jsonl", "timeout_s": None, "cancel": ctx.cancel,
                "tools_name": "evolver_tools"}


class CommandEvolver(Evolver):
    """Any external program. params: command (list), env (dict).

    The program gets HA_CONTEXT (a JSON file with the workspace paths, accesses, progress, message, params) and
    HA_TOOL_URL (the tool endpoint: GET /tools, POST /call/<name>). It changes the adapter directory and may write
    output/step_result.json: {status, summary, rationale, session, usage: [Usage fields...], next_tasks}
    (next_tasks is taken from a subclass that sets chooses_tasks = True).
    """
    supports_resume = True

    def step(self, ctx: StepContext) -> StepResult:
        info = {"workspace": {k: str(v) for k, v in ctx.workspace.__dict__.items()},
                "access": {"validation": str(ctx.access.validation) if ctx.access.validation else None,
                           "environment": [str(p) for p in ctx.access.environment]},
                "progress": ({k: v for k, v in ctx.progress.__dict__.items()
                              if k != "remaining" or self.chooses_tasks} if ctx.progress else None),
                "session": ctx.session,
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
                              data.get("rationale", ""), data.get("session"), usage, data.get("extra", {}),
                              data.get("next_tasks"))
        return StepResult("done" if code == 0 else "failed", f"command exited with {code}")
