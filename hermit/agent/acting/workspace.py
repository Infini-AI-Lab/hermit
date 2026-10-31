"""The `codex` and `claude-code` agents: one task run by the agent CLI in a workspace the adapter is deployed into.
They are the framework's one way to act with a coding-agent CLI; a task adds only what is its own.

    agent:
      name: codex                            # or claude-code; `cli` with params.backend is the same
      params:
        model: gpt-5.4                       # optional: the CLI's default otherwise. A `<backend>/<model>` reference
                                             # (hermit.agent.model.backends) binds the CLI to that backend: its provider
                                             # settings go into args (Codex) or env (Claude Code), its price is used
                                             # (Claude Code's own cost figure only for Anthropic's backend)
        login: chatgpt                       # Codex: the user's own ChatGPT login (`model` is then a Codex model
                                             # name; no key, no price); absent: the backend's key, or for a model
                                             # that is no reference the user's own Codex sign-in
        effort: medium                       # Codex's model_reasoning_effort; absent: the CLI's default
        prompt: environment/prompt.md        # the task prompt; `{field}` placeholders take the task's fields; or
                                             # `file.py:function` (task) -> the prompt
        files: [environment/fixtures]        # optional: copied into every workspace before the run
        mounts:                              # optional: read-only data in the workspace
          - {source: data/corpus, at: docs}
        prepare: utils/task.py:prepare       # optional: (task, workspace[, params, base]) -> mounts, before the run
        answer: '<answer>(.*?)</answer>'     # optional: the last match in the last message is the answer; or
                                             # `file.py:function` (last message, task) -> the output; absent: the
                                             # last message is the output
        collect: utils/task.py:collect       # optional: (task, workspace, log) -> facts, after the run
        deploy: host                         # host: as `hermit deploy`; tree: a Codex tree; files: the adapter as
                                             # it is (hermit.agent.acting.deploy)
        reserved: [task.md]                  # deploy: files: root names the task writes, refused in an adapter
        context_query: question              # the task field an adapter's context tool retrieves for
        isolation: none                      # none: where the Trainer runs the agent; container: Codex in a
        container: {image: ..., network: ..., proxy: ...}   # Docker container of its own (hermit.agent.base.container)
        project_doc_max_bytes: null          # Codex's cap on AGENTS.md; null: Codex's default
        shell_env: core                      # the variables Codex's commands get: core (no keys, no PYTHONPATH) or all
        web_search: false                    # web search, set explicitly either way: Codex's hosted search (it runs
                                             # at the provider, past any proxy), Claude Code's WebSearch and WebFetch
        args: []                             # extra CLI arguments
        env: {}                              # extra environment variables for the CLI (a `$NAME` value is expanded)
        price: {input: 1.25, cached_input: 0.125, output: 10}   # USD per million tokens; without it cost is unknown
        timeout_s: 3600
        retries: 3                           # runs the provider could not serve are run again, then an error
        retry_wait_s: 120
        workspaces: .logs/workspaces         # one fresh directory per task, beside the config (`~` is expanded)

For every task a new directory holds the workspace (`work/`), the CLI's home (`home/`, Codex) and the log (`log/`).
The adapter is deployed into the workspace, the task's files and mounts are added, the prompt is given to the CLI in
print mode, and the answer is read from the agent's last message. The log is what the run records: the host's own
event stream unchanged (`agent-codex.jsonl`, `agent-claude.jsonl`), the prompt, the last message, the deployment
and what `collect` adds; Evolvers read it (hermit.agent.base.trajectory). `RunOutput.extra` has the same keys for every task:
backend, model, effort, web_search, deploy, isolation, deployed, exit_code, timed_out, attempts, workspace, and context_tool
and task when there are any. Usage is read from the event stream, as the AgentEvolver reads it.
"""
from __future__ import annotations

import inspect
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time
import uuid

from hermit.agent.acting import deploy as deployments
from hermit.agent.acting.agent import Agent
from hermit.agent.base import container
from hermit.agent.base.interface import BaseAgent, Limits, Mount, make
from hermit.task import RunOutput, load_callable

BACKENDS = ("codex", "claude")
DEPLOYS = ("host", "tree", "files")
ISOLATIONS = ("none", "container")
INPUT_FIELDS = ("question", "prompt", "instruction", "input", "query")
MEMORY_NOTE = ("`memory/NOTES.md` is your memory across questions: read it first, and record under a `## <topic>` "
               "heading anything worth keeping for later questions (where figures live, how a source is laid out, "
               "a pitfall).")                                     # a deployed tree with a memory (deploy: tree)


def _format(template: str, task: dict) -> str:
    class Fields(dict):
        def __missing__(self, key):
            return "{" + key + "}"
    return template.format_map(Fields({k: v for k, v in task.items() if not isinstance(v, (dict, list))}))


def _task_function(value, base: Path):
    """A `file.py:function` value as the function, else None."""
    return load_callable(value, Path(base)) if isinstance(value, str) and ".py:" in value else None


def _call(fn, *args, params: dict, base: Path):
    """A task function with its positional arguments, and `params` and `base` when it names them."""
    wanted = inspect.signature(fn).parameters
    return fn(*args, **{k: v for k, v in (("params", params), ("base", Path(base))) if k in wanted})


def _prompt(params: dict, base: Path, task: dict) -> str:
    value = params.get("prompt")
    if not value:
        for field in ("prompt", "question", "instruction", "input"):
            if isinstance(task.get(field), str):
                return task[field]
        raise ValueError("agent.params.prompt is required when the task has no prompt field")
    fn = _task_function(value, base)
    if fn is not None:
        return str(_call(fn, task, params=params, base=base))
    path = Path(base) / value
    return _format(path.read_text().rstrip("\n") if value.endswith((".md", ".txt")) and path.is_file() else value,
                   task)


def _query(task: dict, params: dict) -> str:
    field = params.get("context_query")
    if field:
        return str(task.get(field) or "")
    for key in INPUT_FIELDS:
        if isinstance(task.get(key), str) and task[key].strip():
            return task[key]
    return json.dumps({k: v for k, v in task.items() if k not in ("id", "task_id")}, ensure_ascii=False, default=str)


def _context(task: dict, adapter: Path, params: dict) -> tuple[str, dict | None]:
    """The adapter's context for this task: ADAPTER.md and what its context tool, if any, renders."""
    from hermit.adapter.session import Session
    session = Session(adapter)
    try:
        return session.context(query=_query(task, params), task=task), session.last_context
    finally:
        session.close()


def deploy(adapter: Path, workspace: Path, backend: str) -> dict:
    """`hermit deploy`'s deployment into a workspace."""
    if backend == "codex":
        from hermit.hosts.codex import prepare
    else:
        from hermit.hosts.claude_code import prepare
    return prepare(adapter, workspace, sys.executable)


def _deploy(task: dict, adapter: Path | None, work: Path, backend: str, params: dict) -> tuple[dict, dict | None]:
    """The adapter in the workspace by `deploy`; (what was deployed, the context tool's record or None)."""
    mode = params.get("deploy") or "host"
    if adapter is None:
        return {"mode": mode}, None
    adapter = Path(adapter)
    from hermit.adapter.loader import agent_tools, load_manifest
    manifest = load_manifest(adapter)
    if mode == "host":
        if params.get("isolation") == "container" and agent_tools(manifest):
            raise ValueError("the adapter's tools are served by an MCP server on this machine, which a container "
                             "session cannot start: use isolation: none")
        # An adapter with a context tool is not portable, so `hermit deploy` refuses it: its per-task context is for
        # deploy: tree or files.
        return {"mode": mode, **deploy(adapter, work, backend)}, None
    if backend != "codex":
        raise ValueError(f"deploy: {mode} deploys a Codex workspace; agent {backend} takes deploy: host")
    if mode == "tree":
        context, record = (None, None) if (adapter / "AGENTS.md").is_file() else _context(task, adapter, params)
        return {"mode": mode, **deployments.tree(adapter, work, context)}, record
    context, record = _context(task, adapter, params)
    return {"mode": mode, **deployments.files(adapter, work, context, tuple(params.get("reserved") or ()))}, record


def _mounts(task: dict, work: Path, params: dict, base: Path) -> list[Mount]:
    """The config's mounts and the `prepare` task function's, each at its target (a relative one is in the workspace)."""
    specs = list(params.get("mounts") or [])
    fn = _task_function(params.get("prepare"), base)
    if fn is not None:
        specs += list(_call(fn, task, work, params=params, base=base) or [])
    mounts = []
    for spec in specs:
        source = Path(os.path.expanduser(str(spec["source"])))
        source = source if source.is_absolute() else (Path(base) / source)
        target = Path(spec.get("at") or source)
        mounts.append(Mount(source.resolve(), target if target.is_absolute() else work / target,
                                      bool(spec.get("readonly", True))))
    return mounts


def _clean(work: Path) -> None:
    """The workspace kept for inspection: no links out of it, no mount points left empty. A link is never followed,
    so nothing it leads to (a task's data) is touched."""
    root = work.resolve()
    for directory, dirs, files in os.walk(work, topdown=False):          # followlinks=False: links are entries
        for name in dirs + files:
            path = Path(directory) / name
            if path.is_symlink():
                if not path.resolve().is_relative_to(root):
                    path.unlink()
            elif path.is_dir() and not any(path.iterdir()):
                path.rmdir()


def _output(last: str, task: dict, params: dict, base: Path, code: int, timed_out: bool, timeout_s: float):
    """What the judge scores: the `answer` task function's or pattern's output, with `fail_reason` when it found no answer;
    without `answer`, the last message."""
    spec = params.get("answer")
    if not spec:
        return last
    fn = _task_function(spec, base)
    if fn is not None:
        out = _call(fn, last, task, params=params, base=base)
    else:
        found = re.findall(str(spec), last, re.IGNORECASE | re.DOTALL)
        out = {"answer": str(found[-1]).strip() if found else ""}
    if isinstance(out, dict) and "fail_reason" not in out:
        if out.get("answer"):
            out["fail_reason"] = ""
        elif timed_out:
            out["fail_reason"] = f"session timed out after {int(timeout_s)} s without an answer"
        elif code:
            out["fail_reason"] = f"the CLI exited {code} without an answer"
        else:
            out["fail_reason"] = "no answer in the final message"
    return out


class CliAgent(Agent):
    def run(self, task: dict, adapter: Path | None) -> RunOutput:
        return _run(task, adapter, self.params, self.base)


def run(task: dict, adapter: Path | None, *, params: dict, base: Path) -> RunOutput:
    """The function form; CliAgent is the class (`agent.name: codex` or `claude-code`)."""
    return _run(task, adapter, params, base)


def _check(backend: str, params: dict) -> str:
    """The backend; refuses what does not combine."""
    backend = params.get("backend", backend)
    backend = "claude" if backend == "claude-code" else backend
    if backend not in BACKENDS:
        raise ValueError(f"agent.params.backend must be one of {BACKENDS}")
    for key, allowed in (("deploy", DEPLOYS), ("isolation", ISOLATIONS)):
        if params.get(key) is not None and params[key] not in allowed:
            raise ValueError(f"agent.params.{key} must be one of {allowed}, not {params[key]!r}")
    if not isinstance(params.get("web_search"), bool):
        raise ValueError("agent.params.web_search must be set, true or false: Codex's hosted search runs at the "
                         "provider, past any sandbox or proxy, so it is never left to a default")
    if params.get("login") not in (None, "chatgpt"):
        raise ValueError(f"agent.params.login is chatgpt or absent, not {params['login']!r}")
    if params.get("login") and backend != "codex":
        raise ValueError("login: chatgpt is Codex's sign-in; agent claude-code takes its own")
    if params.get("login") and (not params.get("model") or params.get("binding")):
        raise ValueError("with login: chatgpt, agent.params.model is a Codex model name, not empty or a "
                         "<backend>/<model> reference")
    if params.get("isolation") == "container":
        if backend != "codex":
            raise ValueError("isolation: container runs Codex; agent claude-code takes isolation: none")
        container.settings(params)                  # refuses unknown keys
    return backend


def base_agent(backend: str, params: dict) -> BaseAgent:
    """The Base Agent (Layer 2) these params describe: the CLI and how it is run, nothing of the task."""
    settings = {k: params[k] for k in ("model", "binding", "login", "effort", "isolation", "container",
                                       "codex_sandbox", "shell_env", "project_doc_max_bytes", "args", "env", "price")
                if params.get(k) is not None}
    if params.get("claude_permission_mode"):
        settings["permission_mode"] = params["claude_permission_mode"]
    return make(backend, {**settings, "web_search": params["web_search"]})


def _run(task: dict, adapter: Path | None, params: dict, base: Path) -> RunOutput:
    backend = _check(params.get("backend", "codex"), params)
    agent = base_agent(backend, params)
    root = Path(os.path.expanduser(str(params.get("workspaces", ".logs/workspaces"))))
    # Resolved: a container binds each directory at its real path, and the CLI is given these paths.
    root = (root if root.is_absolute() else Path(base) / root).resolve()
    run_dir = root / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', str(task.get('id', 'task')))[:80]}-{uuid.uuid4().hex[:8]}"
    work, home, log = run_dir / "work", run_dir / "home", run_dir / "log"
    agent = agent.configured(home=home, log=log / f"agent-{backend}.jsonl",
                             **({"login_root": params["login_root"]} if params.get("login_root") else {}))
    timeout_s = float(params.get("timeout_s", 3600))
    retries = max(1, int(params.get("retries", 3)))
    started = time.time()
    for attempt in range(1, retries + 1):
        for d in (work, home, log):
            shutil.rmtree(d, ignore_errors=True)
            d.mkdir(parents=True)
        deployed, context_tool = _deploy(task, adapter, work, backend, params)
        for item in params.get("files") or []:
            source = Path(base) / item
            target = work / source.name
            shutil.copytree(source, target) if source.is_dir() else shutil.copy2(source, target)
        mounts = _mounts(task, work, params, base)
        prompt = _prompt(params, base, task) + ("\n" + MEMORY_NOTE if deployed.get("memory") else "")
        (log / "prompt.txt").write_text(prompt, encoding="utf-8")
        (log / "deployment.json").write_text(json.dumps(
            {**deployed, "sub_agents": [r["name"] for r in deployed.get("sub_agents") or []]}, indent=1, default=str))
        result = agent.run(prompt, workspace=work, limits=Limits(readable=mounts, timeout_s=timeout_s),
                           roles=deployed.get("sub_agents") or [])
        if result.extra["served"] or result.timed_out:
            break
        if not result.extra["unavailable"]:
            if result.exit_code and not result.extra["started"]:
                # The CLI exited before it wrote one event: the run never started, which is no wrong answer.
                raise RuntimeError(f"agent {backend} exited {result.exit_code} before starting a session: "
                                   f"{result.extra['output_tail']}")
            break
        if attempt == retries:
            raise RuntimeError(f"agent {backend} could not be served after {retries} attempts: "
                               f"{result.extra['output_tail']}")
        time.sleep(float(params.get("retry_wait_s", 120)))
    code, timed_out, last, usage = result.exit_code, result.timed_out, result.reply, result.usage
    (log / "last_message.txt").write_text(last, encoding="utf-8")
    if deployed.get("memory") and (work / "memory").is_dir():          # the notes the session left
        shutil.copytree(work / "memory", log / "memory", dirs_exist_ok=True)
    output = _output(last, task, params, base, code, timed_out, timeout_s)
    facts = None
    fn = _task_function(params.get("collect"), base)
    if fn is not None:
        facts = _call(fn, task, work, log, params=params, base=base)
    _clean(work)
    cost = tokens = None
    if usage:
        cost, tokens = usage[0].cost_usd, usage[0].input_tokens + usage[0].output_tokens
    extra = {"backend": backend, "model": params.get("model"), "effort": params.get("effort"),
             "web_search": params["web_search"],
             "deploy": deployed.get("mode"), "isolation": params.get("isolation") or "none",
             "deployed": {k: v for k, v in deployed.items() if k != "mode"} | (
                 {"sub_agents": [r["name"] for r in deployed["sub_agents"]]} if deployed.get("sub_agents") else {}),
             "exit_code": code, "timed_out": timed_out, "attempts": attempt, "workspace": str(work)}
    if context_tool is not None:
        extra["context_tool"] = context_tool
    if facts:
        extra["task"] = facts
    return RunOutput(output=output, log=log, cost_usd=cost, wall_s=round(time.time() - started, 1), tokens=tokens,
                     api_speed={"model_calls": usage[0].calls} if usage else None, extra=extra)
