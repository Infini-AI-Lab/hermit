"""The `codex` and `claude-code` agents: one task run by the agent CLI in a workspace the adapter is deployed into.

    agent:
      name: codex                            # or claude-code; `cli` with params.backend is the same
      params:
        model: gpt-5.4                       # optional: the CLI's default otherwise. A `<backend>/<model>` reference
                                             # (hermit.backends) binds the CLI to that backend: its provider
                                             # settings go into args (Codex) or env (Claude Code), its price is used
                                             # (Claude Code's own cost figure only for Anthropic's backend)
        prompt: environment/prompt.md        # the task prompt; `{field}` placeholders take the task's fields
        files: [environment/fixtures]        # optional: copied into every workspace before the run
        args: []                             # extra CLI arguments
        env: {}                              # extra environment variables for the CLI (a `$NAME` value is expanded)
        price: {input: 1.25, cached_input: 0.125, output: 10}   # USD per million tokens; without it cost is unknown
        timeout_s: 3600
        workspaces: .logs/workspaces         # one fresh directory per task, beside the config

For every task a new workspace is created, the adapter is deployed into it exactly as `hermit deploy`
would (AGENTS.md or CLAUDE.md, the MCP server, skills, roles), the prompt is given to the CLI in print mode, and
the agent's last message is the output. The workspace is the log: it keeps the agent's JSON event stream, the
deployment and whatever the agent wrote. Usage is read from the event stream, as the AgentEvolver reads it.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

from hermit.agents.base import Agent
from hermit.evolver.agent import parse_log, price_usage
from hermit.task import RunOutput

BACKENDS = ("codex", "claude")


def _format(template: str, task: dict) -> str:
    class Fields(dict):
        def __missing__(self, key):
            return "{" + key + "}"
    return template.format_map(Fields({k: v for k, v in task.items() if not isinstance(v, (dict, list))}))


def _prompt(params: dict, base: Path, task: dict) -> str:
    value = params.get("prompt")
    if not value:
        for field in ("prompt", "question", "instruction", "input"):
            if isinstance(task.get(field), str):
                return task[field]
        raise ValueError("agent.params.prompt is required when the task has no prompt field")
    path = Path(base) / value
    return _format(path.read_text() if value.endswith((".md", ".txt")) and path.is_file() else value, task)


def deploy(adapter: Path, workspace: Path, backend: str) -> dict:
    if backend == "codex":
        from hermit.hosts.codex import prepare
    else:
        from hermit.hosts.claude_code import prepare
    return prepare(adapter, workspace, sys.executable)


def command(backend: str, prompt: str, workspace: Path, params: dict, last_message: Path,
            hooks: bool = False) -> list[str]:
    model = params.get("model")
    args = list(params.get("args") or [])
    if backend == "codex":
        # `codex exec` cannot ask the user to trust a project's hooks, so it skips them; the deployment's own hooks
        # (the adapter's, in a workspace made for this task) are trusted here, as `claude -p` runs a project's hooks.
        return ["codex", "exec", "--json", "--skip-git-repo-check", "-C", str(workspace),
                "-s", params.get("codex_sandbox", "workspace-write"), "-o", str(last_message),
                *(["--dangerously-bypass-hook-trust"] if hooks else []),
                *(["-m", model] if model else []), *args, prompt]
    return ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose",
            "--permission-mode", params.get("claude_permission_mode", "bypassPermissions"),
            *(["--model", model] if model else []), *args]


def last_message(backend: str, log: Path, last: Path) -> str:
    if backend == "codex":
        return last.read_text() if last.is_file() else ""
    text = ""
    for line in log.read_text(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "result" and isinstance(event.get("result"), str):
            text = event["result"]
    return text


class CliAgent(Agent):
    def run(self, task: dict, adapter: Path | None) -> RunOutput:
        return _run(task, adapter, self.params, self.base)


def run(task: dict, adapter: Path | None, *, params: dict, base: Path) -> RunOutput:
    """The function form; CliAgent is the class (`agent.name: codex` or `claude-code`)."""
    return _run(task, adapter, params, base)


def _run(task: dict, adapter: Path | None, params: dict, base: Path) -> RunOutput:
    backend = params.get("backend", "codex")
    if backend == "claude-code":
        backend = "claude"
    if backend not in BACKENDS:
        raise ValueError(f"agent.params.backend must be one of {BACKENDS}")
    root = Path(base) / params.get("workspaces", ".logs/workspaces")
    workspace = root / f"{task.get('id', 'task')}-{uuid.uuid4().hex[:8]}"
    workspace.mkdir(parents=True)
    deployed = {}
    if adapter is not None:
        from hermit.adapter.loader import load_manifest
        tool = load_manifest(Path(adapter)).get("context_tool")
        if tool:
            raise ValueError(f"agent {backend}: the CLI agents deploy the adapter as `hermit deploy` does, and this "
                             f"adapter declares a context_tool ({tool}) whose per-task context would never reach the "
                             "agent; use the react "
                             "agent or an Agent that calls Session.context(query=..., task=...) and records "
                             "session.last_context in RunOutput.extra['context_tool']")
        deployed = deploy(Path(adapter), workspace, backend)
    for item in params.get("files") or []:
        source = Path(base) / item
        target = workspace / source.name
        shutil.copytree(source, target) if source.is_dir() else shutil.copy2(source, target)
    prompt = _prompt(params, base, task)
    log, last = workspace / f"agent-{backend}.jsonl", workspace / ".last-message.txt"
    env = {**os.environ, **{k: os.path.expandvars(str(v)) for k, v in (params.get("env") or {}).items()}}
    started = time.time()
    with open(log, "w") as out:
        try:
            # An empty stdin: the CLI reads a non-terminal stdin to its end as more prompt, and an inherited one
            # (a shell's socket, an open pipe) may never end.
            argv = command(backend, prompt, workspace, params, last, hooks=bool(deployed.get("hooks")))
            proc = subprocess.run(argv, cwd=str(workspace), env=env,
                                  stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                  timeout=float(params.get("timeout_s", 3600)))
            code = proc.returncode
        except subprocess.TimeoutExpired:
            code = -1
    wall = round(time.time() - started, 1)
    _, usage = parse_log(log, backend, params.get("model") or backend)
    price_usage(usage, params)
    cost = tokens = None
    if usage:
        cost, tokens = usage[0].cost_usd, usage[0].input_tokens + usage[0].output_tokens
    return RunOutput(output=last_message(backend, log, last), log=workspace, cost_usd=cost, wall_s=wall, tokens=tokens,
                     api_speed={"model_calls": usage[0].calls} if usage else None,
                     extra={"backend": backend, "exit_code": code, "timed_out": code == -1, "workspace": str(workspace)})
