"""The `codex`, `claude-code` and `pi` agents: one task run by the agent CLI in a workspace the adapter is deployed
into. They are the framework's one way to act with a coding-agent CLI; a task adds only what is its own.

    agent:
      name: codex                            # or claude-code; `cli` with params.backend is the same
      params:
        model: gpt-5.4                       # optional: the CLI's default otherwise. A `<backend>/<model>` reference
                                             # (hermit.agent.model.backends) binds the CLI to that backend: its provider
                                             # settings go into args (Codex) or env (Claude Code), its price is used
                                             # (Claude Code's own cost figure only for Anthropic's backend)
        login: chatgpt                       # Codex: the user's own ChatGPT login (`model` is then a Codex model
                                             # name; no key, no price); absent: the backend's key, or for a model
                                             # that is no reference the user's own Codex sign-in (on the host or in
                                             # a container; the task sandbox refuses it: give a reference or login)
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
        context_query: question              # the task field an adapter's context tool retrieves for
        # (a Codex agent in a Docker container of its own: the run config's environment.image and
        #  environment.container, not these params; hermit.agent.base.container)
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
The adapter is deployed into the workspace as `hermit deploy` deploys it (hermit.hosts): the whole adapter under
`hermit/<id>/`, each export connected to the CLI's own place, and the context naming the rest; an adapter whose
context is per task (a context tool) gets this task's context, and the session gets the deployment's tools without
approvals and its sub-agents. Then the task's files and mounts are added, the prompt is given to the CLI in
print mode, and the answer is read from the agent's last message. The log is what the run records: the host's own
event stream (`agent-codex.jsonl`, `agent-claude.jsonl`; in a run, a model key's value scrubbed), the prompt, the last message, the deployment
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

from hermit.agent.acting.agent import Agent
from hermit.agent.base import container
from hermit.agent.base.interface import BaseAgent, Limits, Mount, make
from hermit.task import RunOutput, load_callable

BACKENDS = ("codex", "claude", "pi")
DEPLOYS = ("host",)
ISOLATIONS = ("none", "container")
INPUT_FIELDS = ("question", "prompt", "instruction", "input", "query")
MEMORY = Path("memory") / "NOTES.md"     # an adapter's notes the agent keeps across tasks (continual-harness)
MEMORY_NOTE = ("`{path}` is your memory across questions: read it first, and record under a `## <topic>` "
               "heading anything worth keeping for later questions (where figures live, how a source is laid out, "
               "a pitfall).")


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


def deploy(adapter: Path, workspace: Path, backend: str, *, context: str | None = None,
           context_limit: int | None = None, approve_tools: bool = False, serve_tools: bool = True) -> dict:
    """`hermit deploy`'s deployment into a workspace; `context` is this task's, for an adapter whose context is per
    task, `context_limit` Codex's project_doc_max_bytes for the session (None: Codex's default), `approve_tools` the
    adapter's tools called without asking."""
    if backend == "codex":
        from hermit.hosts.codex import DOC_BYTES, prepare
        return prepare(adapter, workspace, sys.executable, context=context, context_limit=context_limit or DOC_BYTES,
                       approve_tools=approve_tools, serve_tools=serve_tools)
    if backend == "pi":
        from hermit.hosts.pi import prepare as prepare_pi
        return prepare_pi(adapter, workspace, sys.executable, context=context)
    from hermit.hosts.claude_code import prepare
    return prepare(adapter, workspace, sys.executable, context=context)


def _deploy(task: dict, adapter: Path | None, work: Path, backend: str, params: dict) -> tuple[dict, dict | None]:
    """The adapter in the workspace; (what was deployed, the context tool's record or None)."""
    if adapter is None:
        return {"mode": "host"}, None
    adapter = Path(adapter)
    from hermit.adapter.loader import agent_tools, load_manifest
    from hermit.adapter.manifest import PORTABLE
    manifest = load_manifest(adapter)
    # A container session cannot start the tools' server on this machine: the session's own tool server (the Base
    # Agent's, which a container reaches) serves them instead.
    harness_tools = params.get("isolation") == "container" and bool(agent_tools(manifest))
    # An adapter only hermit runs (not plugin-portable: a context tool's per-task context, say) has no installed
    # workspace, so `hermit deploy` refuses it; here the task is known, so its context is deployed.
    context, record = _context(task, adapter, params) if not manifest.get(PORTABLE, True) else (None, None)
    deployed = deploy(adapter, work, backend, context=context, context_limit=params.get("project_doc_max_bytes"),
                      approve_tools=True, serve_tools=not harness_tools)   # nobody is there to approve a call
    return {"mode": "host", **deployed, **({"harness_tools": manifest["id"]} if harness_tools else {})}, record


def _session_args(backend: str, deployed: dict, work: Path) -> list[str]:
    """What the session needs to use what the deployment connected: the workspace's own Codex configuration (its
    tools, roles and hooks) trusted, as a person trusts a deployed project, and its sub-agents enabled; Pi's project
    configuration (.pi/mcp.json, .pi/skills) trusted for the session (--approve), without which Pi skips it."""
    if backend == "pi":
        return ["--approve"]
    if backend == "codex":
        return ["-c", f'projects={{"{work}" = {{ trust_level = "trusted" }} }}',
                *(["--enable", "multi_agent"] if deployed.get("roles") else [])]
    return ["--mcp-config", str(work / ".mcp.json")] if deployed.get("server_name") else []


def _memory(work: Path, deployed: dict) -> Path | None:
    """The adapter's memory file in the workspace, when it has one."""
    if not deployed.get("adapter_dir"):
        return None
    path = Path(deployed["adapter_dir"]) / MEMORY
    return path if path.is_file() else None


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


def _clean(work: Path, mount_points=()) -> None:
    """The workspace kept for inspection: no links out of it, no mount points left empty. A link is never followed,
    so nothing it leads to (a task's data) is touched. A mount point that cannot be read or removed yet (Docker
    Desktop may still hold one a moment after its container exits) is left in place instead of failing the run; any
    other entry that cannot be tidied still fails it."""
    root = work.resolve()
    held = {os.path.abspath(p) for p in mount_points}
    for directory, dirs, files in os.walk(work, topdown=False):          # followlinks=False: links are entries
        for name in dirs + files:
            path = Path(directory) / name
            try:
                if path.is_symlink():
                    if not path.resolve().is_relative_to(root):
                        path.unlink()
                elif path.is_dir() and not any(path.iterdir()):
                    path.rmdir()
            except OSError:
                if os.path.abspath(path) not in held:      # only a mount point the container runtime has not
                    raise                                   # released yet is left as it is


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


def _agent_name(backend: str) -> str:
    """The acting agent's name for a backend (claude: claude-code)."""
    return {"claude": "claude-code"}.get(backend, backend)


def _check(backend: str, params: dict) -> str:
    """The backend; refuses what does not combine."""
    backend = params.get("backend", backend)
    backend = "claude" if backend == "claude-code" else backend
    if backend not in BACKENDS:
        raise ValueError(f"agent.params.backend must be one of {BACKENDS}")
    if params.get("reserved") is not None:
        raise ValueError("agent.params.reserved is gone: the adapter is deployed under hermit/<id>/, apart from the "
                         "task's files; remove the line")
    if params.get("deploy") in ("tree", "files"):
        raise ValueError(f"agent.params.deploy: {params['deploy']} is gone: every adapter is deployed as `hermit "
                         "deploy` deploys it (its exports connected, its other files named in the context); remove "
                         "the line")
    for key, allowed in (("deploy", DEPLOYS), ("isolation", ISOLATIONS)):
        if params.get(key) is not None and params[key] not in allowed:
            raise ValueError(f"agent.params.{key} must be one of {allowed}, not {params[key]!r}")
    if not isinstance(params.get("web_search"), bool):
        raise ValueError("agent.params.web_search must be set, true or false: Codex's hosted search runs at the "
                         "provider, past any sandbox or proxy, so it is never left to a default")
    if params.get("login") not in (None, "chatgpt"):
        raise ValueError(f"agent.params.login is chatgpt or absent, not {params['login']!r}")
    if params.get("login") and backend != "codex":
        raise ValueError(f"login: chatgpt is Codex's sign-in; agent {_agent_name(backend)} takes its own")
    if params.get("login") and (not params.get("model") or params.get("binding")):
        raise ValueError("with login: chatgpt, agent.params.model is a Codex model name, not empty or a "
                         "<backend>/<model> reference")
    if params.get("isolation") == "container":
        if backend != "codex":
            raise ValueError(f"isolation: container runs Codex; agent {_agent_name(backend)} takes isolation: none")
        container.settings(params)                  # refuses unknown keys
    return backend


def base_agent(backend: str, params: dict) -> BaseAgent:
    """The Base Agent (Layer 2) these params describe: the CLI and how it is run, nothing of the task."""
    settings = {k: params[k] for k in ("model", "binding", "login", "plan_dir", "plan_max_wait_s", "effort",
                                       "isolation", "container", "codex_sandbox", "shell_env", "project_doc_max_bytes",
                                       "args", "env", "price", "provider")
                if k in params and (params[k] is not None or k == "plan_max_wait_s")}
    if params.get("claude_permission_mode"):
        settings["permission_mode"] = params["claude_permission_mode"]
    return make(backend, {**settings, "web_search": params["web_search"]})


def _run(task: dict, adapter: Path | None, params: dict, base: Path) -> RunOutput:
    backend = _check(params.get("backend", "codex"), params)
    agent = base_agent(backend, params)
    root = Path(os.path.expanduser(str(params.get("workspaces", ".logs/workspaces"))))
    # Resolved: a container binds each directory at its real path, and the CLI is given these paths.
    root = (root if root.is_absolute() else Path(base) / root).resolve()
    # `session_name`: this call's directory, named by a caller that scrubs it afterwards (a container call, whose worker
    # may fail before it says where the directory is)
    run_dir = root / (params.get("session_name")
                      or f"{re.sub(r'[^A-Za-z0-9_.-]', '_', str(task.get('id', 'task')))[:80]}-{uuid.uuid4().hex[:8]}")
    work, home, log = run_dir / "work", run_dir / "home", run_dir / "log"
    agent = agent.configured(home=home, log=log / f"agent-{backend}.jsonl",
                             **({"login_root": params["login_root"]} if params.get("login_root") else {}))
    timeout_s = float(params.get("timeout_s", 3600))
    retries = max(1, int(params.get("retries", 3)))
    started = time.time()
    # `deadline_s`: the whole call's time (attempts and waits), set by a caller that is stopped at it (a container
    # session must end, and remove its container, before its worker is killed)
    budget = float(params["deadline_s"]) if params.get("deadline_s") else None
    if budget is not None:              # and the session's own waits (a login's plan) come off it too
        agent = agent.configured(deadline=started + budget)
    for attempt in range(1, retries + 1):
        for d in (work, home, log):
            shutil.rmtree(d, ignore_errors=True)
            d.mkdir(parents=True)
        deployed, context_tool = _deploy(task, adapter, work, backend, params)
        session = agent.configured(args=[*(agent.settings.get("args") or []), *_session_args(backend, deployed, work)],
                                   **({"tools_name": deployed["harness_tools"].replace("-", "_")}
                                      if deployed.get("harness_tools") else {}))
        memory = _memory(work, deployed)
        for item in params.get("files") or []:
            source = Path(base) / item
            target = work / source.name
            shutil.copytree(source, target) if source.is_dir() else shutil.copy2(source, target)
        mounts = _mounts(task, work, params, base)
        prompt = _prompt(params, base, task) + (
            "\n" + MEMORY_NOTE.format(path=memory.relative_to(work).as_posix()) if memory else "")
        (log / "prompt.txt").write_text(prompt, encoding="utf-8")
        (log / "deployment.json").write_text(json.dumps(deployed, indent=1, default=str))
        from hermit.adapter.session import Session
        served = Session(adapter) if deployed.get("harness_tools") else None
        if budget is not None:          # what is left once the deployment, the files and the server are ready
            timeout_s = min(float(params.get("timeout_s", 3600)), budget - (time.time() - started))
            if timeout_s <= 0:
                if served is not None:
                    served.close()
                raise RuntimeError(f"agent {backend} could not be served within deadline_s={budget:.0f}")
        try:
            result = session.run(prompt, workspace=work, limits=Limits(readable=mounts, timeout_s=timeout_s),
                                 tools=served.as_tools() if served else ())
        finally:
            if served is not None:
                served.close()
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
        wait = float(params.get("retry_wait_s", 120))
        time.sleep(wait if budget is None else max(0.0, min(wait, budget - (time.time() - started) - 1)))
    code, timed_out, last, usage = result.exit_code, result.timed_out, result.reply, result.usage
    (log / "last_message.txt").write_text(last, encoding="utf-8")
    if memory is not None and memory.parent.is_dir():                  # the notes the session left
        shutil.copytree(memory.parent, log / "memory", dirs_exist_ok=True)
    output = _output(last, task, params, base, code, timed_out, timeout_s)
    facts = None
    fn = _task_function(params.get("collect"), base)
    if fn is not None:
        facts = _call(fn, task, work, log, params=params, base=base)
    _clean(work, [m.target for m in mounts])
    cost = tokens = None
    if usage:
        cost, tokens = usage[0].cost_usd, usage[0].input_tokens + usage[0].output_tokens
    extra = {"backend": backend, "model": params.get("model"), "effort": params.get("effort"),
             "web_search": params["web_search"],
             "deploy": deployed.get("mode"), "isolation": params.get("isolation") or "none",
             "deployed": {k: v for k, v in deployed.items() if k not in ("mode", "files")},
             "exit_code": code, "timed_out": timed_out, "attempts": attempt, "workspace": str(work)}
    if context_tool is not None:
        extra["context_tool"] = context_tool
    if facts:
        extra["task"] = facts
    return RunOutput(output=output, log=log, cost_usd=cost, wall_s=round(time.time() - started, 1), tokens=tokens,
                     api_speed={"model_calls": usage[0].calls} if usage else None, extra=extra)
