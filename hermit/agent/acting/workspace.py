"""The `codex`, `claude-code`, `pi`, `mini-swe-agent`, `opencode`, `dsh` and `hermes` agents: one task run by the agent
CLI in a workspace the adapter is deployed into. They are the framework's one way to act with a coding-agent CLI; a task
adds only what is its own.

    agent:
      name: codex                            # or claude-code, pi, mini-swe-agent, opencode, dsh or hermes
                                             # (`cli`: params.backend)
      params:
        model: gpt-5.4                       # optional: the CLI's default otherwise. A `<backend>/<model>` reference
                                             # (hermit.agent.model.backends) binds the CLI to that backend: its provider
                                             # settings go into args (Codex) or env (Claude Code), its price is used
                                             # (Claude Code's own cost figure only for Anthropic's backend)
        login: chatgpt                       # the user's own subscription (`model` is then the CLI's model name; no
                                             # key, no price): chatgpt for Codex, claude for Claude Code. Absent: the
                                             # backend's key, or for a model that is no reference the CLI's own
                                             # sign-in (on the host or in a container; the task sandbox refuses it:
                                             # give a reference or a login)
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
        # (the CLI, Codex or Claude Code, in a Docker container of its own: the run config's environment.image and
        #  environment.container, not these params; hermit.environment.docker)
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
        # mini-swe-agent only (hermit.agent.base.cli.MiniSweAgent): model_class (litellm or litellm_textbased),
        # config (its config file, beside the run config), step_limit (model calls per task, default 50)

For every task a new directory holds the workspace (`work/`), the CLI's home (`home/`, Codex) and the log (`log/`).
The adapter is deployed into the workspace as `hermit deploy` deploys it (hermit.hosts): the whole adapter under
`hermit/<id>/`, each export connected to the CLI's own place, and the context naming the rest; an adapter whose
context is per task (a context tool) gets this task's context, and the session gets the deployment's tools without
approvals and its sub-agents. Then the task's files and mounts are added, the prompt is given to the CLI in
print mode, and the answer is read from the agent's last message. The log is what the run records: the host's own
event stream (`agent-codex.jsonl`, `agent-claude.jsonl`; in a run, a model key's value scrubbed), the prompt, the last message, the deployment
and what `collect` adds; Evolvers read it (hermit.agent.base.trajectory). `RunOutput.extra` has the same keys for every task:
backend, model, effort, web_search, deploy, isolation, deployed, exit_code, timed_out, attempts, workspace, mounts (where
each task mount inside the workspace is: bound or linked), and context_tool, task and session_failed (a session that
gave no answer) when there are any. Usage is read from the event stream, as the AgentEvolver reads it.

In a task's own environment (`run(task, adapter, environment=...)`, a remote one: a task's Harbor environment,
hermit.environment.harbor) the workspace is the environment's: the adapter is deployed here into a staging folder as
`hermit deploy` lays it out for a session there (the hosts' deployment with the environment's `python3`, its tools
not served, its hooks run from `hermit/<id>/` relative to the session's directory: `remote_hooks`), the staging's
files uploaded into the environment's workspace (Hermit's context first in a context file the image already has,
then the image's own text), the `files` uploaded beside them, and the CLI runs there (its Base Agent's remote
isolation). The adapter's notes come back from there. The adapter's tools, `mounts`, `prepare` and `collect` are not
supported there (refused).
"""
from __future__ import annotations

import dataclasses
import inspect
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sys
import time
import uuid

from hermit.agent.acting.agent import ALL, Agent
from hermit.agent.base.interface import BaseAgent, LoginFailed, Unsupported, make
from hermit.environment import Container, Environment, Limits, Mount, Local, docker as container
from hermit.agent.base.login import LOGINS, NO_LOGIN
from hermit.agent.model.usage import Ledger
from hermit.common import RunOutput, load_callable

BACKENDS = ("codex", "claude", "pi", "mini-swe-agent", "opencode", "dsh", "hermes")
DEPLOYS = ("host",)
ISOLATIONS = ("none", "container")
INPUT_FIELDS = ("question", "prompt", "instruction", "input", "query")
REMOTE_CODEX_DOC_BYTES = 262144           # Codex's project_doc_max_bytes in a task's own environment (the runner's)
MODEL_REJECTED = "model_rejected"         # the provider refused the configuration (hermit.environment.harbor_errors)
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


def _prompt(params: dict, base: Path, task: dict, environment=None) -> str:
    value = params.get("prompt")
    if not value and getattr(environment, "instruction", None) is not None:
        return environment.instruction        # a task's own environment: Harbor's instruction, as the runner gave it
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
           context_limit: int | None = None, approve_tools: bool = False, serve_tools: bool = True,
           python: str | None = None) -> dict:
    """`hermit deploy`'s deployment into a workspace; `context` is this task's, for an adapter whose context is per
    task, `context_limit` Codex's project_doc_max_bytes for the session (None: Codex's default), `approve_tools` the
    adapter's tools called without asking, `python` the interpreter the deployment's commands run with where the
    session runs (default: this one)."""
    python = python or sys.executable
    if backend == "codex":
        from hermit.hosts.codex import DOC_BYTES, prepare
        return prepare(adapter, workspace, python, context=context, context_limit=context_limit or DOC_BYTES,
                       approve_tools=approve_tools, serve_tools=serve_tools)
    if backend == "pi":
        from hermit.hosts.pi import prepare as prepare_pi
        return prepare_pi(adapter, workspace, python, context=context)
    if backend == "mini-swe-agent":
        from hermit.hosts.mini_swe_agent import prepare as prepare_mini
        return prepare_mini(adapter, workspace, python, context=context, serve_tools=serve_tools)
    if backend == "opencode":
        from hermit.hosts.opencode import prepare as prepare_opencode
        return prepare_opencode(adapter, workspace, python, context=context, serve_tools=serve_tools)
    if backend == "dsh":
        from hermit.hosts.dsh import prepare as prepare_dsh
        return prepare_dsh(adapter, workspace, python, context=context)
    if backend == "hermes":
        from hermit.hosts.hermes import prepare as prepare_hermes
        return prepare_hermes(adapter, workspace, python, context=context, serve_tools=serve_tools)
    from hermit.hosts.claude_code import prepare
    return prepare(adapter, workspace, python, context=context, serve_tools=serve_tools)


REMOTE_HOOK_HOSTS = {"codex": "hermit.hosts.codex", "claude": "hermit.hosts.claude_code"}
CONTEXT_FILES = {"codex": "AGENTS.md", "claude": "CLAUDE.md"}


def remote_hooks(adapter: Path, manifest: dict, deploy_dir: Path, backend: str) -> bool:
    """The deployed hooks rewritten for a session in a remote environment: the ones the host's deployment connects (its
    HOOK_TYPES), every command run from the adapter's copy by its workspace-relative path (Codex's
    `.codex/hooks.json`; the `hooks` of Claude Code's `.claude/settings.json`, from `$CLAUDE_PROJECT_DIR`, since Claude
    Code runs a hook in the shell's current directory), not from this machine's folder. True when the adapter has
    hooks the CLI runs."""
    from importlib import import_module
    import shlex
    from hermit.adapter.loader import hook_definitions
    from hermit.hosts.inputs import ADAPTER_DIR, hook_commands, native_hooks
    if backend not in REMOTE_HOOK_HOSTS:
        return False
    hooks = native_hooks(hook_definitions(adapter, manifest), import_module(REMOTE_HOOK_HOSTS[backend]).HOOK_TYPES)
    relative = hook_commands(hooks, Path(ADAPTER_DIR) / manifest["id"]) if hooks else None
    if backend == "claude":
        if relative is not None:
            folder = f"{ADAPTER_DIR}/{manifest['id']}"
            plain = f"cd {shlex.quote(folder)} || exit 1\n"
            for groups in relative["hooks"].values():
                for hook in (h for g in groups for h in g["hooks"]):
                    if hook.get("type") == "command" and hook["command"].startswith(plain):
                        hook["command"] = f'cd "${{CLAUDE_PROJECT_DIR:-.}}"/{shlex.quote(folder)} || exit 1\n' + \
                            hook["command"][len(plain):]
        path = deploy_dir / ".claude" / "settings.json"
        settings = json.loads(path.read_text()) if path.is_file() else {}
        if relative is None:
            if settings.pop("hooks", None) is not None:
                path.write_text(json.dumps(settings, indent=2) + "\n")
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({**settings, "hooks": relative["hooks"]}, indent=2) + "\n")
        return True
    path = deploy_dir / ".codex" / "hooks.json"
    if relative is None:
        path.unlink(missing_ok=True)
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(relative, indent=2) + "\n")
    return True


def _deploy(task: dict, adapter: Path | None, work: Path, backend: str, params: dict, *,
            remote: bool = False) -> tuple[dict, dict | None]:
    """The adapter in the workspace (for a remote environment: in the staging folder `work`, laid out for a session
    there); (what was deployed, the context tool's record or None)."""
    if adapter is None:
        return {"mode": "host"}, None
    adapter = Path(adapter)
    from hermit.adapter.loader import agent_tools, load_manifest
    from hermit.adapter.manifest import PORTABLE
    manifest = load_manifest(adapter)
    if remote:
        if backend not in REMOTE_HOOK_HOSTS:
            raise Unsupported(f"agent {backend} does not run in a remote environment (codex and claude do)")
        if agent_tools(manifest):
            raise Unsupported(f"the adapter {manifest['id']} has tools, and a session in a remote environment (a "
                              "task's Harbor environment) cannot reach the framework's tool server yet")
        context, record = _context(task, adapter, params) if not manifest.get(PORTABLE, True) else (None, None)
        deployed = deploy(adapter, work, backend, context=context, context_limit=params.get("project_doc_max_bytes"),
                          approve_tools=True, serve_tools=False, python="python3")
        hooks = remote_hooks(adapter, manifest, work, backend)
        return {"mode": "host", **deployed, "hooks": hooks}, record
    # A container session cannot start the tools' server on this machine, mini-swe-agent reaches tools only
    # through its Base Agent, OpenCode waits 2 s at most for a starting server (STARTUP_GRACE_MS in 2.2; the
    # adapter's takes longer, so its first request would go without them), and Hermes reads MCP servers from its home
    # only, no project file: the session's own tool server (the Base Agent's, running before the session starts)
    # serves them instead.
    harness_tools = (params.get("isolation") == "container" or
                     backend in ("mini-swe-agent", "opencode", "hermes")) and \
        bool(agent_tools(manifest))
    # An adapter only hermit runs (not plugin-portable: a context tool's per-task context, say) has no installed
    # workspace, so `hermit deploy` refuses it; here the task is known, so its context is deployed.
    context, record = _context(task, adapter, params) if not manifest.get(PORTABLE, True) else (None, None)
    deployed = deploy(adapter, work, backend, context=context, context_limit=params.get("project_doc_max_bytes"),
                      approve_tools=True, serve_tools=not harness_tools)   # nobody is there to approve a call
    return {"mode": "host", **deployed, **({"harness_tools": manifest["id"]} if harness_tools else {})}, record


def _session_args(backend: str, deployed: dict, work: Path) -> list[str]:
    """What the session needs to use what the deployment connected: the workspace's own Codex configuration (its
    tools, roles and hooks) trusted, as a person trusts a deployed project, and its sub-agents enabled; Pi's project
    configuration (.pi/mcp.json, .pi/skills) trusted for the session (--approve), without which Pi skips it; dsh's
    patch layer of the adapter's tools (.dsh/hermit.patch.yml), which dsh loads only when it is named (--patch).
    OpenCode loads its project's opencode.json and .opencode/skills with no trust step; Hermes is given the
    deployment's skills folder by the Base Agent (`skills_dir`) and its tools by the session's tool server."""
    if backend == "pi":
        return ["--approve"]
    if backend in ("opencode", "hermes"):
        return []
    if backend == "dsh":
        return ["--patch", str(deployed["patch"])] if deployed.get("patch") else []
    if backend == "codex":
        return ["-c", f'projects={{"{work}" = {{ trust_level = "trusted" }} }}',
                *(["--enable", "multi_agent"] if deployed.get("roles") else [])]
    if backend == "mini-swe-agent":                 # it reads the deployment's config from the workspace itself
        return []
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
    supports = ALL      # a deployment connects each export where the CLI has a place, and names the rest in the context
    def run(self, task: dict, adapter: Path | None, environment=None) -> RunOutput:
        return _run(task, adapter, self.params, self.base, environment)

    def installs(self, adapter: Path | None) -> list[dict]:
        return installs(self.params, adapter)

    def usage_of(self, logs: Path) -> list:
        """The usage a session's stream left in `logs` (a trial's agent logs: the stream the CLI wrote there as it ran),
        read and priced as the session's own would have been: what a session Harbor's timeout cut off had already
        reported (the runner parsed the retained stream after its trial too)."""
        backend = _check(self.params.get("backend", "codex"), self.params)
        agent = base_agent(backend, self.params)
        stream = Path(logs) / f"agent-{agent.backend}.jsonl"
        if not stream.is_file() or stream.is_symlink():
            return []
        from hermit.agent.base.records import price_usage
        s = agent.settings
        _, usage = agent.parse(stream, (s.get("binding") or {}).get("ref") or s.get("model") or agent.backend)
        price_usage(usage, s)
        return list(usage)


def installs(params: dict, adapter: Path | None) -> list[dict]:
    """What a CLI agent needs in a task's own environment before its session (Agent.installs): its CLI, unless `bin`
    names one there (by the environment's installer of the CLI's name, unless it is on the session's PATH there and no
    `version` is pinned, as the Harbor runner installed it), and python3 when the adapter has hooks the CLI runs."""
    from hermit.agent.base.cli import REMOTE_INSTALL, REMOTE_PATH
    backend = str(params.get("backend") or "codex")
    backend = "claude" if backend == "claude-code" else backend          # as _check names it
    out = []
    if params.get("bin") and backend in REMOTE_INSTALL:      # a mounted binary: used when it serves, else installed
        out.append({"agent": REMOTE_INSTALL[backend], "version": params.get("version"), "bin": str(params["bin"]),
                    "program": backend})
    elif backend in REMOTE_INSTALL:
        out.append({"agent": REMOTE_INSTALL[backend], "version": params.get("version"),
                    "probe": "bash -c " + shlex.quote(REMOTE_PATH + f"command -v {backend}")})
    if adapter is not None and backend in REMOTE_HOOK_HOSTS:
        from importlib import import_module
        from hermit.adapter.loader import hook_definitions, load_manifest
        from hermit.hosts.inputs import native_hooks
        try:
            manifest = load_manifest(Path(adapter))
            hooks = native_hooks(hook_definitions(Path(adapter), manifest),
                                 import_module(REMOTE_HOOK_HOSTS[backend]).HOOK_TYPES)
        except Exception:                                # noqa: BLE001 - the deployment says why (adapter_failed)
            hooks = None
        if hooks:
            out.append({"packages": ["python3"]})
    return out


def run(task: dict, adapter: Path | None, *, params: dict, base: Path, environment=None) -> RunOutput:
    """The function form; CliAgent is the class (`agent.name: codex` or `claude-code`)."""
    return _run(task, adapter, params, base, environment)


def _infra_run(cause: str, message: str, usages: Ledger, log: Path, started: float, *, final: bool) -> RunOutput:
    """A run in a task's own environment that is not the agent's result (`extra.infra`, which the run reads:
    hermit.trainer.rollout), with what its sessions spent: `final`, never run again (the provider allowance spent, the
    configuration refused)."""
    ledger = usages                   # a session ran: none reported is not known (the runner's usage None), never 0
    return RunOutput(output=None, log=log, cost_usd=ledger.cost(None), wall_s=round(time.time() - started, 1),
                     tokens=ledger.tokens(None), api_speed={"model_calls": ledger.calls()},
                     extra={"infra": cause, "infra_final": final, "exception_message": message[-2000:]})


def _copy_files(params: dict, base: Path, work: Path) -> None:
    """The config's `files`, copied into the workspace before the run."""
    for item in params.get("files") or []:
        source = Path(base) / item
        target = work / source.name
        shutil.copytree(source, target) if source.is_dir() else shutil.copy2(source, target)


class AdapterContextTooLong(ValueError):
    """The adapter's context with the image's own over the CLI's cap: the adapter's result (context_over_limit)."""


def _adapter_failed(cause: str, message: str, usages: list, log: Path, started: float) -> RunOutput:
    """The adapter failed before any session (its deployment, its per-task context, its context over the CLI's cap):
    the agent's result, scored 0 with its message (`extra.adapter_failed`, which the run reads)."""
    (log / "adapter_failed.txt").write_text(message, encoding="utf-8")
    return RunOutput(output=None, log=log, cost_usd=0.0, wall_s=round(time.time() - started, 1), tokens=0,
                     api_speed={"model_calls": 0}, extra={"adapter_failed": cause, "exception_message": message[-2000:],
                                                         "session_failed": message[-600:]})


def _upload_staging(environment: Environment, work: Path, backend: str, deployed: dict, params: dict,
                    image: dict | None = None) -> None:
    """The staging folder's files into a remote environment's workspace. A context file the image already has there
    is kept after Hermit's text (Hermit's first), as the Harbor runner merged them; a file that is there but cannot be
    read fails the call (the environment's fault: never the adapter's text alone in its place), and for Codex the
    merged text must fit its cap on AGENTS.md (project_doc_max_bytes), as the runner checked it."""
    name = CONTEXT_FILES.get(backend)
    mine = work / name if name else None
    if mine is not None and mine.is_file() and deployed.get("adapter_id"):
        image = {} if image is None else image          # the image's own file, read once: a run again finds the merge
        if "text" not in image:
            image["text"] = None
            if environment.exec(["test", "-e", name], timeout=60).code == 0:
                # read there, a link followed there (AGENTS.md -> CLAUDE.md), as the runner read it in the container
                got = environment.exec(["sh", "-c", f'printf "{IMAGE_MARK}"; cat -- {name}'], timeout=120)
                if got.code != 0 or IMAGE_MARK not in got.output:
                    del image["text"]
                    raise RuntimeError(f"{IMAGE_CONTEXT_UNREAD} ({name}): {got.output[-300:]}")
                image["text"] = got.output.split(IMAGE_MARK, 1)[1]
        if image["text"] is not None:
            mine.write_text(mine.read_text(encoding="utf-8") + "\n\n" + image["text"], encoding="utf-8")
            # the merge replaces a link there, never writes through it to the file it names
            environment.exec(["sh", "-c", f"if [ -L {name} ]; then rm -f -- {name}; fi"], timeout=60)
        if backend == "codex":
            from hermit.hosts.codex import DOC_BYTES
            limit = int(params.get("project_doc_max_bytes") or DOC_BYTES)
            size = len(mine.read_bytes())
            if size > limit:
                raise AdapterContextTooLong(f"the adapter's context with the image's own {name} is {size} bytes, over "
                                            f"Codex's limit of {limit} (project_doc_max_bytes): the session would not "
                                            "read all of it")
    for entry in sorted(work.iterdir()):
        environment.upload(entry, entry.name)


IMAGE_CONTEXT_UNREAD = "the image's own context file could not be read"
IMAGE_MARK = "hermit-image-context:"        # what precedes the file's text in the reading command's output


def _ensure_python3(environment: Environment) -> bool:
    """Whether python3 is where the session runs, for the adapter's hooks (rendered for `python3`): installed at the
    environment's setup when the agent asked for it (installs: an adapter with hooks), as the Harbor runner ensured it
    before a session with hooks. Missing: a warning, and the session runs with hooks that may fail open."""
    if environment.exec(["sh", "-c", "command -v python3"], timeout=60).code == 0:
        return True
    print("WARNING: python3 for the adapter's hooks is not in the environment", file=sys.stderr, flush=True)
    return False


def _collect_hooks(environment: Environment, log: Path) -> None:
    """The adapter's hook audit and state (`.hermit-hooks/` in the workspace: hook-calls.jsonl, hook-state/) into the
    log, where the Harbor runner's curated log kept them and the AHE Debugger and learner read them."""
    if environment.exec(["test", "-d", ".hermit-hooks"], timeout=60).code != 0:
        return
    got = log.parent / "hook-logs"
    environment.download(".hermit-hooks", got)
    for entry in sorted(got.iterdir()):
        target = log / entry.name
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        elif os.path.lexists(target):
            target.unlink()
        shutil.move(str(entry), str(target))
    shutil.rmtree(got, ignore_errors=True)


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
    # They sign in through a model reference (dsh: or its own provider), and run on this machine
    if backend in NO_LOGIN:
        if params.get("login") is not None:
            raise ValueError(f"agent {backend} takes no login: its model is a <backend>/<model> reference"
                             + (", or a model of dsh's own provider" if backend == "dsh" else ""))
        if params.get("isolation") == "container":
            raise ValueError(f"isolation: container runs Codex or Claude Code; agent {backend} takes isolation: none")
        if backend == "dsh" and params.get("permission_mode") is not None:
            from hermit.agent.base.cli import DSH_PERMISSION_MODES
            if params["permission_mode"] not in DSH_PERMISSION_MODES:
                raise ValueError(f"agent.dsh.permission_mode is one of {', '.join(DSH_PERMISSION_MODES)} (dsh's own "
                                 f"sandbox), not {params['permission_mode']!r}")
        return backend
    login, label = params.get("login"), {"codex": "Codex", "claude": "Claude Code"}[backend]
    if login is not None and login not in LOGINS:
        raise ValueError(f"agent.params.login is {' or '.join(sorted(LOGINS))} or absent, not {login!r}")
    if login and LOGINS[login] != backend:
        raise ValueError(f"login: {login} is {'Codex' if login == 'chatgpt' else 'Claude Code'}'s sign-in; agent "
                         f"{backend} takes login: {'chatgpt' if backend == 'codex' else 'claude'}")
    if login and (not params.get("model") or params.get("binding")):
        raise ValueError(f"with login: {login}, agent.params.model is a {label} model name, not empty or a "
                         "<backend>/<model> reference")
    if params.get("isolation") == "container":
        container.settings(params, backend)         # refuses unknown keys
    if backend == "claude" and params.get("effort") is not None:
        from hermit.agent.base.cli import Claude
        if params["effort"] not in Claude.efforts:       # the CLI would warn and go on at its default
            raise ValueError(f"agent.params.effort is one of {', '.join(Claude.efforts)} for Claude Code, not "
                             f"{params['effort']!r}")
    return backend


def environment(work: Path, params: dict, limits: Limits) -> Environment:
    """Where the session works: the task's workspace on this machine, or in a container of its own
    (`isolation: container`, its settings `container`)."""
    if params.get("isolation") == "container":
        return Container(work, limits, settings=params.get("container"))
    return Local(work, limits)


def base_agent(backend: str, params: dict) -> BaseAgent:
    """The Base Agent (Layer 2) these params describe: the CLI and how it is run, nothing of the task."""
    from hermit.agent.base.cli import HERMES_PARAMS, PI_PARAMS
    settings = {k: params[k] for k in ("model", "binding", "login", "plan_dir", "plan_max_wait_s", "usage_credits",
                                       "effort", "codex_sandbox", "shell_env", "bin", "version", "remote_home",
                                       "project_doc_max_bytes", "args", "env", "price", *PI_PARAMS,
                                       *(p for p in HERMES_PARAMS if p != "skills_dir"),
                                       "model_class", "config", "step_limit")
                if k in params and (params[k] is not None or k == "plan_max_wait_s")}
    if params.get("claude_permission_mode"):
        settings["permission_mode"] = params["claude_permission_mode"]
    if backend == "dsh" and params.get("permission_mode"):    # dsh's own sandbox (agent.dsh.permission_mode)
        settings["permission_mode"] = params["permission_mode"]
    return make(backend, {**settings, "web_search": params["web_search"]})


def _run(task: dict, adapter: Path | None, params: dict, base: Path, given: Environment | None = None) -> RunOutput:
    backend = _check(params.get("backend", "codex"), params)
    remote = given is not None and given.isolation == "remote"
    if given is not None and not remote:
        raise Unsupported(f"agent {backend} makes its own workspace; the environment a task gives it is a remote one "
                          f"(a task's Harbor environment), not {given!r}")
    if remote:
        unsupported = [k for k in ("mounts", "prepare", "collect") if params.get(k)]
        lim = given.limits
        if lim.readable or lim.hidden or lim.network is not None:
            raise Unsupported(f"agent {backend} in a remote environment takes no readable mounts, hidden paths or "
                              "network limit of Hermit's (the task's own say what its environment reaches)")
        if unsupported or params.get("isolation") == "container":
            raise Unsupported(f"agent {backend} in a remote environment (a task's Harbor environment) takes none of "
                              f"{', '.join(unsupported) or 'isolation: container'}: the environment is the task's own")
    if remote and backend == "codex" and not params.get("project_doc_max_bytes"):
        # the Harbor runner's cap on AGENTS.md for Codex in a task's container (its CLI table's: 256 KiB), for the
        # deployment's check and the session's own setting alike
        params = {**params, "project_doc_max_bytes": REMOTE_CODEX_DOC_BYTES}
    if params.get("config"):                        # mini-swe-agent's config file, beside the run config
        params = {**params, "config": str(Path(base) / os.path.expanduser(str(params["config"])))}
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
    # In a task's own environment the session's clock is the task's agent budget unless the agent's own `timeout_s`
    # says less (the Harbor runner gave each task its own: Terminal-Bench-Science's 8 hours)
    own_budget = given.limits.timeout_s if remote else None
    default_s = float(own_budget) if own_budget else 3600.0
    timeout_s = float(params.get("timeout_s") or default_s)
    # In a task's own environment a provider that could not serve is run again by the run, in a fresh environment
    # (the Harbor runner's busy_retries, one allowance over the task's runs: environment.harbor.busy_retries), never
    # here in the environment the session already changed
    retries = 1 if remote else max(1, int(params.get("retries", 3)))
    usages = Ledger()                   # every attempt's usage: a run again spends too
    image_context: dict = {}            # a task environment's own context file, read before the first deployment
    started = time.time()
    # `deadline_s`: the whole call's time (attempts and waits), set by a caller that is stopped at it (a container
    # session must end, and remove its container, before its worker is killed)
    from hermit.agent.base.deadline import Deadline
    budget = float(params["deadline_s"]) if params.get("deadline_s") else None
    deadline = Deadline(started + budget if budget is not None else None)   # the run's one deadline
    if deadline.at is not None:         # and the session's own waits (a login's plan) come off it too
        agent = agent.configured(deadline=deadline.at)
    for attempt in range(1, retries + 1):
        for d in (work, home, log):
            shutil.rmtree(d, ignore_errors=True)
            d.mkdir(parents=True)
        if remote:
            # the adapter's own failure (its deployment, its per-task context, a context over the CLI's cap) is the
            # agent's result, scored 0 with its message, as the runner graded adapter_failed and context_over_limit;
            # the environment's failing under it stays infra
            row = task if any(isinstance(task.get(f), str) for f in INPUT_FIELDS) or params.get("context_query") \
                else {**task, "instruction": given.instruction} if given.instruction is not None else task
            try:
                deployed, context_tool = _deploy(row, adapter, work, backend, params, remote=True)
                _copy_files(params, base, work)
                _upload_staging(given, work, backend, deployed, params, image_context)
            except AdapterContextTooLong as exc:
                return _adapter_failed("context_over_limit", str(exc), usages, log, started)
            except (OSError, Unsupported):
                raise
            except Exception as exc:
                said = type(exc).__name__ + str(exc)
                if IMAGE_CONTEXT_UNREAD in said or "EnvironmentUnavailable" in said or "AgentPhaseOver" in said:
                    raise                                # the environment's, or Harbor ended the phase: not the adapter
                from hermit.adapter.session import host_fault
                host_fault(exc)                          # this machine's, whatever the adapter (the runner's staging)
                return _adapter_failed("adapter_failed", f"{type(exc).__name__}: {exc}", usages, log, started)
        else:
            deployed, context_tool = _deploy(task, adapter, work, backend, params, remote=False)
        where = Path(str(given.workspace)) if remote else work       # the workspace as the session sees it
        session = agent.configured(args=[*(agent.settings.get("args") or []), *_session_args(backend, deployed, where)],
                                   **({"tools_name": deployed["harness_tools"].replace("-", "_")}
                                      if deployed.get("harness_tools") else {}),
                                   # OpenCode loads only the skills it is allowed: the deployment's
                                   **({"skills": list(deployed.get("skills") or [])} if backend == "opencode" else {}),
                                   # Hermes reads skills from the folders its config names: the deployment's
                                   **({"skills_dir": deployed["skills_dir"]}
                                      if backend == "hermes" and deployed.get("skills_dir") else {}))
        memory = _memory(work, deployed)
        if not remote:
            _copy_files(params, base, work)
        if remote and deployed.get("hooks"):
            _ensure_python3(given)
        mounts = [] if remote else _mounts(task, work, params, base)
        prompt = _prompt(params, base, task, given if remote else None) + (
            "\n" + MEMORY_NOTE.format(path=memory.relative_to(work).as_posix()) if memory else "")
        (log / "prompt.txt").write_text(prompt, encoding="utf-8")
        (log / "deployment.json").write_text(json.dumps(deployed, indent=1, default=str))
        from hermit.adapter.session import Session
        served = Session(adapter) if deployed.get("harness_tools") else None
        if budget is not None:          # what is left once the deployment, the files and the server are ready
            timeout_s = min(float(params.get("timeout_s") or default_s), deadline.remaining())
            if timeout_s <= 0:
                if served is not None:
                    served.close()
                raise RuntimeError(f"agent {backend} could not be served within deadline_s={budget:.0f}")
        if remote:
            own = given.limits.timeout_s
            place = given.with_limits(dataclasses.replace(given.limits, timeout_s=min(timeout_s, float(own))
                                                          if own else timeout_s))   # its other limits kept
        else:
            place = environment(work, params, Limits(readable=mounts, timeout_s=timeout_s))
        try:
            result = session.run(prompt, environment=place, tools=served.as_tools() if served else ())
        finally:
            if served is not None:
                served.close()
        usages.add(result.usage)
        if remote and not result.usage and result.extra.get("started"):
            # a session that reached its CLI's events but reported no usage (cut off before turn.completed) may have
            # been billed: its cost is not known (the runner's usage None), never 0
            usages.unreported(0, str(params.get("model") or backend), sessions=1)
        if result.extra.get("login_failed"):          # before a timeout: a session that then hung is no attempt
            raise LoginFailed(f"agent {backend}: {result.extra.get('failure')}")
        if remote:
            # what the Harbor runner read of a session beyond its own result (codex_stream.session_verdict): the
            # account's bill refused (nothing later can be served: the run ends), the configuration refused (never
            # run again); a refusal of what the input made the request, and the model's own, are the agent's result
            from hermit.agent.base.codex_stream import session_verdict
            spent = sum(u.input_tokens + u.output_tokens for u in (result.usage or []))
            said = session_verdict(result.extra, spent)
            if said == "usage_limit":
                raise LoginFailed(f"agent {backend}: usage_limit: the provider does not bill the account (its quota "
                                  f"or credit spent, or a payment it needs): {result.extra.get('failure')}")
            if said == MODEL_REJECTED:
                return _infra_run(MODEL_REJECTED, f"agent {backend}: the provider refused the run's configuration: "
                                  f"{result.extra.get('failure')}", usages, log, started, final=True)
            if result.extra.get("refused") and not result.extra["served"] and not result.timed_out:
                if not LOGINS.get(params.get("login")):
                    # no plan of the run's to wait for (the user's own sign-in): nothing later can be served
                    raise LoginFailed(f"agent {backend}: usage_limit: the account refused the session for its usage "
                                      f"limit and the run holds no plan to wait for: {result.extra.get('failure')}")
                # the login's plan refused it (its window): run again once the plan allows, never counted
                out = _infra_run("usage_limit", f"agent {backend}: the plan refused the session: "
                                 f"{result.extra.get('failure')}", usages, log, started, final=False)
                if result.extra.get("hold_unwritten"):   # no hold to wait for: the run pauses (the runner's 5 min)
                    out.extra["hold_unwritten"] = True
                return out
        if result.extra["served"] or result.timed_out:
            break
        if not result.extra["unavailable"]:
            if result.exit_code and not result.extra["started"]:
                # The CLI exited before it wrote one event: the run never started, which is no wrong answer.
                said = f"agent {backend} exited {result.exit_code} before starting a session: " \
                    f"{result.extra['output_tail']}"
                if remote:
                    return _infra_run("session_failed", said, usages, log, started, final=False)
                raise RuntimeError(said)
            break
        if remote:                      # the run runs it again in a fresh environment (busy_retries)
            return _infra_run("model_provider_failed", f"agent {backend}: the provider could not serve the session: "
                              f"{result.extra['output_tail']}", usages, log, started, final=False)
        if attempt == retries:
            raise RuntimeError(f"agent {backend} could not be served after {retries} attempts: "
                               f"{result.extra['output_tail']}")
        wait = float(params.get("retry_wait_s", 120))
        time.sleep(wait if deadline.at is None else max(0.0, min(wait, deadline.remaining() - 1)))
    code, timed_out, last, usage = result.exit_code, result.timed_out, result.reply, usages
    (log / "last_message.txt").write_text(last, encoding="utf-8")
    if memory is not None and remote:                                  # the notes the session left, from there
        try:
            given.download(memory.parent.relative_to(work).as_posix(), log / "memory")
        except Exception:                                              # noqa: BLE001 - none left
            pass
    elif memory is not None and memory.parent.is_dir():                # the notes the session left
        shutil.copytree(memory.parent, log / "memory", dirs_exist_ok=True)
    hooks_missing = None
    if remote and deployed.get("hooks"):
        try:                                         # an audit that cannot be read leaves the session as it ended
            _collect_hooks(given, log)
        except Exception as exc:                     # noqa: BLE001 - recorded, the verifier decides the rest
            hooks_missing = f"{type(exc).__name__}: {exc}"[-500:]
    # A session the CLI itself reports as failed has no answer to read or to judge (trainer.rollout: an error record).
    failed = result.extra.get("failed")
    output = None if failed else _output(last, task, params, base, code, timed_out, timeout_s)
    facts = None
    fn = None if remote else _task_function(params.get("collect"), base)
    if fn is not None:
        facts = _call(fn, task, work, log, params=params, base=base)
    _clean(work, [m.target for m in mounts])
    # every model of the session (Hermes's auxiliary calls are a usage of their own); none reported: not known
    cost, tokens = usage.cost(None), usage.tokens(None)
    extra = {"backend": backend, "model": params.get("model"), "effort": params.get("effort"),
             "web_search": params["web_search"],
             "deploy": deployed.get("mode"), "isolation": "remote" if remote else params.get("isolation") or "none",
             "deployed": {k: v for k, v in deployed.items() if k not in ("mode", "files")},
             "exit_code": code, "timed_out": timed_out, "attempts": attempt,
             "workspace": str(given.workspace) if remote else str(work),
             "mounts": result.extra.get("mounts") or {}}       # a task's data in the workspace: bound or linked
    if failed:
        extra["session_failed"] = str(result.extra.get("failure") or f"the CLI exited {code} without a result")
    if hooks_missing:
        extra["hooks_audit_missing"] = hooks_missing
    if context_tool is not None:
        extra["context_tool"] = context_tool
    if facts:
        extra["task"] = facts
    return RunOutput(output=output, log=log, cost_usd=cost, wall_s=round(time.time() - started, 1), tokens=tokens,
                     api_speed={"model_calls": usage.calls()} if not usage.empty else None, extra=extra)
