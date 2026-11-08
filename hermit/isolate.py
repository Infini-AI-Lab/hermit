"""The task sandbox, the judge's sandboxes and the declared tools' sandbox (docs/developer/sandbox-design.md).

The acting agent runs in the task sandbox: a worker process (`worker.py`) inside bubblewrap that runs the agent on
one task and hands the result back, so nothing the agent or the adapter's code starts runs with the framework's
access. What the sandbox shows is the run config's `environment` section and nothing else:

- read-only: the framework's packages (the acting layer runs inside), `environment.packages` with their
  dependencies, `environment.read`, the adapter of this call, and a CLI agent's own installation;
- read-write: this call's work directory (the built-in agents' logs and workspaces go there), a fresh directory in
  place of the `.logs` beside the task's agent file, and `environment.write`;
- environment: what the framework sets, `environment.env`, and the key variable of the agent's model (as Harbor
  gives its agents their keys); a Claude login's token (CLAUDE_CODE_OAUTH_TOKEN) passes by name to each call's
  worker, never to the judge;
- network: `environment.network` (no-network | allowlist | public); the agent's model provider (a plan login's hosts)
  is always allowed, and a model server or meter on the loopback is forwarded.

The data and split files, the judge, the runs and trees directories may not lie in a declared path: the config is
refused, and the isolation check inside proves them absent. A task with `environment.image` runs its CLI agent in a
container of its own (the Base Agent's container isolation), started from a worker on the host that keeps the
adapter's code in the strict sandbox; a Harbor task set (`agent.name: harbor`) runs in Harbor's, from this process.

The judge runs where `judge.mode` says: `host` in this process on the downloaded answer, `shared` in the task
sandbox after the agent (a new exec over the same work directory, with `judge.tests` shown to it alone), `separate`
in a fresh sandbox holding only the paths `judge.collect` names. A template's declared tools run in a strict
sandbox (their code and the template read-only, the adapter they act on read-write, no network); a task's in one
built from `environment`, like its agent. A model only a CLI reaches (`llm: {cli: ...}`, `agent.params.model: {cli:
...}`) answers each request with one session in a sandbox of its own (`cli_sandbox`): its empty workspace, its
model's hosts, nothing of the run.
"""
from __future__ import annotations

import dataclasses
import fnmatch
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid

from hermit.agent.base import login as codex_login
from hermit.agent.base.cli import CLIS
from hermit.agent.base.endpoint import cli_endpoint, endpoint_spec
from hermit.redact import redact_name, redact_tree
from hermit.sandbox import (LIMITS, NETWORK_MODES, PACKAGE, AllowlistProxy, Bind, Sandbox, framework_env,
                            hidden_paths, model_access, network_mode, owner_readable, package_closure,
                            program_paths, sanitized_copy, set_hidden)
from hermit.task import RunOutput, load_callable, output_record

WORKER = PACKAGE / "worker.py"
CLI_HOMES = {c.backend: (c.user_folder, c.home_var, c.home_files[0]) for c in CLIS.values()}   # folder, var, sign-in
CHATGPT_HOSTS = list(codex_login.HOSTS["chatgpt"])      # a Codex ChatGPT login reaches its plan here (recorded)
JUDGE_MODES = ("host", "shared", "separate")
HOME_SECRETS = [Path.home() / ".ssh", Path.home() / ".bashrc"]   # proven absent inside a sandbox
ENVIRONMENT_LISTS = ("packages", "read", "write", "env", "allowed_hosts")
RECORDS_WALK = 50000                                     # directories a declared path is searched for run records


def _cli_kind(name, params: dict) -> str | None:
    from hermit.agent.model.backends import CLI_AGENTS
    kind = CLI_AGENTS.get(str(name))
    if kind is None and str(name) == "cli":
        kind = {"claude-code": "claude"}.get(params.get("backend", "codex"), params.get("backend", "codex"))
    if kind is None and params.get("base"):          # a task's own agent on a CLI's Base Agent (agent.base)
        kind = {"claude-code": "claude"}.get(str(params["base"]), str(params["base"]))
    return kind if kind in CLI_HOMES else None


def host_pipeline(config) -> str | None:
    """Why the acting pipeline runs on the host, or None when it runs in the task sandbox."""
    d = config.data
    if not d["sandbox"]["enabled"]:
        return "sandbox.enabled is false"
    if str(d["agent"]["name"]) == "harbor":
        return "a Harbor task set runs in Harbor's own environment"
    if d["environment"].get("runner") == "host":
        return "environment.runner: host (the task's own code isolates the adapter's code it runs, in containers)"
    if d["environment"].get("image"):
        return "environment.image: the agent runs in a container of its own"
    return None


def validate(config) -> None:
    """The sandbox sections of a run config: types, modes, and the declared paths against the run's secrets."""
    d = config.data
    s, env, judge = d["sandbox"], d["environment"], d["judge"]
    if not isinstance(s.get("enabled", True), bool):
        raise ValueError("sandbox.enabled must be true or false")
    if s.get("enforce", "required") not in ("required", "best_effort"):
        raise ValueError("sandbox.enforce must be required or best_effort")
    for key in ENVIRONMENT_LISTS:
        if env.get(key) is not None and not isinstance(env[key], list):
            raise ValueError(f"environment.{key} must be a list")
    if env.get("runner", "sandbox") not in ("sandbox", "host"):
        raise ValueError(f"environment.runner must be sandbox or host, not {env.get('runner')!r}")
    # A model only a CLI reaches (the learner's llm, an acting pipeline's model) runs each session in a sandbox of its
    # own (cli_sandbox) that reaches its model's hosts: a reference's provider, or its CLI's own login.
    for where, spec in (("llm", d.get("llm")), ("agent.params.model", (d["agent"].get("params") or {}).get("model"))):
        if isinstance(spec, dict) and spec.get("cli") and (spec.get("isolation") or "sandbox") == "sandbox" and \
                s.get("enabled", True) and not spec.get("binding") and \
                codex_login.LOGINS.get(spec.get("login")) != {"claude-code": "claude"}.get(spec["cli"], spec["cli"]):
            raise ValueError(f"{where}.cli runs in a sandbox of its own that reaches only its model's hosts: give its "
                             "model as a `<backend>/<model>` reference or its CLI's login (`login: chatgpt` for codex, "
                             "`login: claude` for claude) (or isolation: container)")
    if env.get("network") not in NETWORK_MODES:
        raise ValueError(f"environment.network must be one of {list(NETWORK_MODES)}, not {env.get('network')!r}")
    if env.get("allowed_hosts") and env["network"] != "allowlist":
        raise ValueError("environment.allowed_hosts needs environment.network: allowlist")
    hide = [] if s.get("hide") is None else s["hide"]
    if not isinstance(hide, list) or not all(isinstance(n, str) for n in hide):
        raise ValueError("sandbox.hide must be a list of module or package names")
    hidden_paths(s.get("hide") or [])                 # each must be installed: what is not found cannot be covered
    evolver = s.get("evolver")
    if not isinstance(evolver, dict):
        raise ValueError(f"sandbox.evolver takes {{allowed_hosts, env, resources}}, not {evolver!r}")
    unknown = set(evolver) - {"allowed_hosts", "env", "resources"}
    if unknown:
        raise ValueError(f"sandbox.evolver: unknown keys {sorted(unknown)}")
    for where, res in (("environment.resources", env.get("resources")), ("sandbox.evolver.resources",
                                                                          evolver.get("resources"))):
        if res is not None and (not isinstance(res, dict) or set(res) - LIMITS):
            raise ValueError(f"{where} takes {sorted(LIMITS)}")
        for name, value in (res or {}).items():
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
                raise ValueError(f"{where}.{name} must be a positive whole number (or null: no limit), not {value!r}")
    for key in ("allowed_hosts", "env"):
        if evolver.get(key) is not None and not isinstance(evolver[key], list):
            raise ValueError(f"sandbox.evolver.{key} must be a list")
    # A Claude login's token passes by name to the sessions on the login only, never as a sandbox's variable (which the
    # judge, the task's tools and a learner's other commands get too): a pattern that would take it in is refused. The
    # name itself, written out, is a deliberate grant (a task whose own code runs Claude Code on the token).
    for where, names in (("environment.env", env.get("env")), ("sandbox.evolver.env", evolver.get("env"))):
        for name in names or ():
            if isinstance(name, str) and name != codex_login.TOKEN_ENV and any(c in name for c in "*?[") and \
                    fnmatch.fnmatchcase(codex_login.TOKEN_ENV, name):
                raise ValueError(f"{where}: {name!r} matches {codex_login.TOKEN_ENV}, a Claude login's token, which "
                                 "passes by name to the sessions on the login and to nothing else in the sandbox; "
                                 "name the variables you mean (or the token's own name, to grant it on purpose)")
    mode = judge.get("mode") or "host"
    if mode not in JUDGE_MODES:
        raise ValueError(f"judge.mode must be one of {list(JUDGE_MODES)}, not {mode!r}")
    for key in ("tests", "collect"):
        if judge.get(key) is not None and not isinstance(judge[key], list):
            raise ValueError(f"judge.{key} must be a list")
    if judge.get("tests") and mode == "host":
        raise ValueError("judge.tests are shown to a sandboxed judge: set judge.mode to shared or separate")
    if judge.get("collect") and mode != "separate":
        raise ValueError("judge.collect carries paths to a separate judge sandbox: set judge.mode: separate")
    params = d["agent"].get("params") or {}
    if params.get("isolation") == "container":
        raise ValueError("the acting agent's isolation: container moved to environment.image (and "
                         "environment.container for its network and proxy)")
    if "hide" in (d["evolver"].get("params") or {}):
        raise ValueError("evolver.params.hide was removed: the evolver sandbox is an allowlist and shows only what the "
                         "run and the method declare (docs/framework/configuration.md#sandboxes)")
    if (d["evolver"].get("params") or {}).get("isolation") not in (None, "sandbox"):
        raise ValueError("the learner runs in the evolver sandbox (sandbox.evolver); its isolation is not set (a "
                         "container is for the acting agent's environment.image and an llm: {cli: ...} endpoint)")
    reason = host_pipeline(config)
    kind = _cli_kind(d["agent"]["name"], params)
    login = params.get("login")
    if kind and login:            # wherever the pipeline runs: else every session would be refused, one task at a time
        own = codex_login.LOGINS.get(login)
        if own is None:
            raise ValueError(f"agent.params.login is {' or '.join(sorted(codex_login.LOGINS))} or absent, not "
                             f"{login!r}")
        if own != kind:
            raise ValueError(f"login: {login} is {'Codex' if own == 'codex' else 'Claude Code'}'s sign-in; the acting "
                             f"agent runs {'Codex' if kind == 'codex' else 'Claude Code'}, which takes login: "
                             f"{'chatgpt' if kind == 'codex' else 'claude'}")
        if not params.get("model") or params.get("binding"):
            raise ValueError(f"with login: {login}, agent.model is the CLI's own model name (agent.model.name), not "
                             "empty or a <backend>/<model> reference")
        if kind == "claude" and params.get("effort") is not None:
            from hermit.agent.base.cli import Claude
            if params["effort"] not in Claude.efforts:
                raise ValueError(f"agent.model.effort is one of {', '.join(Claude.efforts)} for Claude Code, not "
                                 f"{params['effort']!r}")
    if env.get("image"):
        if kind not in ("codex", "claude"):
            raise ValueError("environment.image runs a CLI acting agent (codex or claude) in a container: agent.base "
                             "(or agent.name) must be codex or claude")
        if env.get("packages") or env.get("read"):
            raise ValueError("environment.packages and environment.read build a bubblewrap sandbox; with "
                             "environment.image the image holds the agent's world")
        if env.get("resources"):
            raise ValueError("environment.resources are the bubblewrap sandbox's limits; a container "
                             "(environment.image) keeps its own (hermit/agent/base/container.py)")
    if not reason and kind and not params.get("binding") and codex_login.LOGINS.get(params.get("login")) != kind:
        raise ValueError("a CLI acting agent in the task sandbox signs in through a model reference (agent.params.model:"
                         " <backend>/<model>, its key variable passed in) or its CLI's login (login: chatgpt for "
                         "Codex, its copy passed in; login: claude for Claude Code, its token passed by name); the "
                         "CLI's own sign-in from your home is not copied into the sandbox")
    if reason and mode != "host" and s["enabled"]:          # with the sandbox off every judge runs in this process
        raise ValueError(f"judge.mode {mode} runs the judge in the task sandbox, but the pipeline runs on the host "
                         f"({reason}); use judge.mode: host")
    check_declared(config)


def _secrets(config) -> tuple[list[Path], Path | None]:
    """(what no declared path may overlap: the data and split files, the judge and its tests, this run's directory and
    the tree; the runs directory, which a declared path may lie inside but neither be nor hold)."""
    d = config.data
    out = [config.path(d["data"].get(k)) for k in ("tasks", "train", "val", "test")]
    judge = d.get("judge") or {}
    if judge.get("path") and str(judge["path"]).partition(":")[0].endswith(".py"):
        out.append(config.path(judge["path"]))
    out += [config.path(t) for t in judge.get("tests") or []]
    runs = None
    try:
        out.append(config.run_dir())
        runs = config.run_dir().parent
        from hermit.adapter.manifest import read_manifest
        out.append(config.tree_dir(read_manifest(config.template_dir())["template"]["id"]))
    except Exception:                                    # a config without a template still guards the rest
        pass
    keep = lambda p: p is not None and "$" not in str(p)
    return [p for p in out if keep(p)], runs if keep(runs) else None


def run_secrets(config) -> list[Path]:
    """What no sandbox but the judge's may show: the data and split files, the judge, its tests, this run's directory,
    the tree, and the runs directory (a launcher's task directory may lie inside it: the isolation check skips a
    directory that holds a bound path)."""
    hard, runs = _secrets(config)
    return [*hard, *([runs] if runs is not None else [])]


def config_paths(config, where: str, values) -> list[Path]:
    """Paths from the config; one naming an unset variable is an error, not a path that quietly binds nothing."""
    out = []
    for value in values or []:
        path = config.path(value)
        if path is not None and "$" in str(path):
            raise ValueError(f"{where}: {value!r} names an unset environment variable")
        if path is not None:
            out.append(path)
    return out


def _overlaps(a: Path, b: Path) -> bool:
    a, b = Path(os.path.realpath(a)), Path(os.path.realpath(b))
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def check_declared(config) -> None:
    """A declared path (the task sandbox's, or one the learner reads) may neither hold nor lie inside a run secret:
    the allowlist shows what is declared, so a secret inside it would be shown."""
    d = config.data
    hard, runs = _secrets(config)
    tests = [config.path(t) for t in d["judge"].get("tests") or []]
    declared = [(f"environment.{key}", v) for key in ("read", "write") for v in d["environment"].get(key) or []]
    # What the learner reads is declared the same way (its sandbox shows these under /w/environment/<name>), and so
    # is the task description it is shown at /w/task-info.
    declared += [("evolver_access.environment", v) for v in d["evolver_access"].get("environment") or []]
    declared += [("task_info", d["task_info"])] if d.get("task_info") else []
    for key, value in declared:
        path = config.path(value)
        if path is None or "$" in str(path):
            continue                                 # an unset variable: refused when the sandbox is built
        for secret in hard:
            if _overlaps(path, secret):
                what = "a judge test" if secret in tests else "the data, the judge, this run or the tree"
                raise ValueError(f"{key} {value!r} overlaps {secret} ({what}): it would be seen; move the "
                                 "secret out of that path or declare a narrower one")
        # The runs directory holds every run's records: a declared path may lie inside it (a launcher's task
        # directory beside the runs), never be it or hold it.
        if runs is not None and Path(os.path.realpath(runs)).is_relative_to(Path(os.path.realpath(path))):
            raise ValueError(f"{key} {value!r} holds the runs directory {runs}: every run's records would be "
                             "seen; declare a narrower path")
        # Nor another run's records (another output directory's), whose judge feedback can carry the answers.
        found = _run_records(path)
        if found:
            raise ValueError(f"{key} {value!r} holds a run's records ({found}): their feedback can carry the "
                             "answers; declare a narrower path")


def _run_records(root: Path, limit: int = RECORDS_WALK) -> Path | None:
    """The run directory (one holding a steps.jsonl) that `root` lies in, or the first one under it, walking at most
    `limit` directories (links not followed); past that, ValueError: the path is too large to search."""
    real = Path(os.path.realpath(root))
    for directory in [real, *real.parents]:
        if (directory / "steps.jsonl").is_file():
            return directory
    if not real.is_dir():
        return None
    for walked, (directory, _, files) in enumerate(os.walk(real)):
        if walked >= limit:
            raise ValueError(f"{root} holds more than {limit} directories, too many to search for run records; "
                             "declare a narrower path")
        if "steps.jsonl" in files:
            return Path(directory)
    return None


class Recheck:
    """Declared paths searched again for run records before a call: one that came to hold a run's records after the
    config was checked (another experiment writing there) refuses the call. At most once a minute; `own` (this run's
    directory) is not another's."""

    def __init__(self, paths, own: Path | None = None):
        self.paths, self.own = [Path(p) for p in paths], Path(os.path.realpath(own)) if own else None
        self._checked, self._lock = float("-inf"), threading.Lock()

    def __call__(self) -> None:
        now = time.monotonic()
        if now - self._checked < 60:
            return
        with self._lock:
            for path in self.paths:
                found = _run_records(path)
                if found and (self.own is None or Path(os.path.realpath(found)) != self.own):
                    raise RuntimeError(f"declared path {path} now holds a run's records ({found}): the call is "
                                       "refused; move them out or declare a narrower path")
            self._checked = now


def _agent_logs(name: str, base: Path) -> list[Path]:
    """The `.logs` directory beside a task's own agent file (file.py:Class), where real agents keep their logs
    (examples/officeqa). Each call gets a fresh directory bound there, so no call reads an earlier one's."""
    target = str(name).rpartition(":")[0]
    if not target.endswith(".py"):
        return []
    return [(Path(base) / target).resolve().parent / ".logs"]


def _after_scrub(log: Path | None, roots) -> Path | None:
    """A returned log as the scrub left it: a name that held a key value was renamed (redact_name), under one of
    `roots`."""
    if log is None or os.path.lexists(log):
        return log
    for root in roots:
        if Path(log).is_relative_to(root):
            return Path(root).joinpath(*(redact_name(part) for part in Path(log).relative_to(root).parts))
    return log


def _log_binds(work: Path, logs: list[Path], own: list[Path], existing: bool = False, shown=()) -> list[Bind]:
    """Each `.logs` beside the agent's file bound to this call's fresh directory (`existing`: only those it made);
    and a directory of the call's own that lies in one of them (an agent file beside the config: the default
    .logs/<role>/<call>) bound again after it at its own path, so the fresh `.logs` does not hide it: `own`
    writable, `shown` (the agent's log copy a judge reads) read-only."""
    binds = [Bind(work / "agent-logs" / str(i), str(log_dir), True) for i, log_dir in enumerate(logs)
             if not existing or (work / "agent-logs" / str(i)).is_dir()]
    covered = [Path(b.target).resolve() for b in binds]
    inside = lambda p: any(Path(p).resolve().is_relative_to(c) for c in covered)
    return binds + [Bind(p, str(p), True) for p in own if inside(p)] + \
        [Bind(p, str(p), False) for p in shown if os.path.exists(p) and inside(p)]


def _run_worker(box: Sandbox, job: dict, work: Path, *, home: Path, readable=(), writable=(), env=None,
                keep=(), forward=(), timeout: float | None = None, binds=()) -> dict:
    """Run worker.py in the sandbox with `job`; return its payload, or raise with what it left behind. After
    `timeout` seconds the sandbox is stopped (everything in it goes with it: its own process namespace)."""
    read_end, write_end = os.pipe()
    env = dict(env or framework_env())
    env["HA_RESULT_FD"] = str(write_end)
    try:
        args, env = box.wrap([sys.executable, str(WORKER)], readable=list(readable), writable=[work, *writable],
                             env=env, home=home, keep=["PYTHONPATH", "HA_RESULT_FD", *keep], forward=list(forward),
                             binds=list(binds))
    except BaseException:
        os.close(read_end)
        os.close(write_end)
        raise
    log = work / "worker.log"
    # The log is read back from this descriptor, never by its name: the call can write its directory and could put a
    # link or a pipe there.
    try:
        out = open(log, "w+b")
    except BaseException:
        os.close(read_end)
        os.close(write_end)
        box.abandon(args)                                # never started: no isolation report comes
        raise
    try:
        proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=out, stderr=subprocess.STDOUT, env=env,
                                cwd=str(work), pass_fds=(write_end,))
    except BaseException:
        os.close(read_end)
        out.close()
        box.abandon(args)
        raise
    finally:
        os.close(write_end)
    try:
        proc.stdin.write(json.dumps(job, default=str).encode())
        proc.stdin.close()
    except BrokenPipeError:
        pass                                             # it ended before reading: the exit code tells why
    # Read on a thread: a result larger than the pipe's buffer must drain while the worker writes it.
    received: list[str] = []

    def read():
        with os.fdopen(read_end, "r") as result:
            received.append(result.read())
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    reader.join(timeout)
    if reader.is_alive():
        proc.kill()
        proc.wait()
        reader.join(10)
        out.close()
        box.abandon(args)                                # stopped before (or without) its isolation report
        raise TimeoutError(f"the sandboxed {job['kind']} ran longer than {timeout / 60:.1f} min and was stopped; "
                           f"log {log}")
    data = received[0] if received else ""
    try:
        code = proc.wait(timeout=30)                     # it answered; a process kept alive after that is stopped
    except subprocess.TimeoutExpired:
        proc.kill()
        code = proc.wait()
    box.abandon(args)                                    # ended: a report that never came is not waited for
    if not data:
        out.seek(max(0, os.fstat(out.fileno()).st_size - 1500))
        tail = out.read().decode(errors="replace")
        out.close()
        reason = "the sandbox's isolation check failed" if code == 97 else f"it exited with code {code}"
        raise RuntimeError(f"the sandboxed {job['kind']} returned nothing: {reason}; log {log}:\n{tail}")
    out.close()
    payload = json.loads(data)
    if not payload.get("ok"):
        raise RuntimeError(f"{payload.get('error')}\n{payload.get('traceback', '')}".strip())
    return payload


def _owned_log(value, roots, binds=()) -> Path | None:
    """The log a call reported, as a path this process may read: under a place the call could write, never a link.
    A path under a bind's name inside is mapped back to its source."""
    if not value:
        return None
    path = Path(value)
    for b in sorted(binds, key=lambda b: -len(str(b.target))):    # the innermost name inside first
        target = Path(b.target)
        if path == target or path.is_relative_to(target):
            path = Path(b.source) / path.relative_to(target)
            break
    if path.is_symlink():
        return None
    real = Path(os.path.realpath(path))
    return path if any(real.is_relative_to(Path(os.path.realpath(r))) for r in roots) else None


def _safe(text: str) -> str:
    """A filename-safe slug of a task id (shared with the learning material)."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(text))[:60] or "task"


class SandboxedAgent:
    """The run's agent as a pipeline, every call in a worker inside the task sandbox."""

    def __init__(self, config, role: str, *, records: Path | None, work_root: Path | None = None, model_endpoint=None):
        d = config.data
        agent, envc = d["agent"], d["environment"]
        self.config, self.role = config, role
        self.name, self.base = agent["name"], Path(config.base)
        # What can refuse the config is checked before anything is started (a proxy left running otherwise).
        missing = [p for p in envc.get("packages") or [] if p != "*" and not package_closure([p])]
        if missing:
            raise ValueError(f"environment.packages names distributions that are not installed: {missing}")
        readable = config_paths(config, "environment.read", envc.get("read"))
        writable = config_paths(config, "environment.write", envc.get("write"))
        self.params = dict(agent.get("params") or {})
        if model_endpoint is not None:       # a model only a CLI reaches: its endpoint is out here, with the sign-in;
            self.params["model"] = endpoint_spec(model_endpoint, self.params["model"])   # the call gets a URL
        # The agent reaches its model as Harbor's do: its key variable passes, its provider's host is allowed, a
        # server on the loopback (a meter, a CLI model's endpoint) is forwarded.
        model_keys, model_hosts, self.forward = model_access([self.params.get("binding"), self.params.get("model")])
        self.work_root = Path(work_root) if work_root else config.path(self.params.get("logs", ".logs")) / role
        self.cli = _cli_kind(self.name, self.params)
        self.timeout = float(agent["timeout_min"]) * 60 if agent.get("timeout_min") else None
        self.enforce = d["sandbox"].get("enforce", "required")
        self.logs = _agent_logs(self.name, self.base)
        for log_dir in self.logs:                        # the mount point of each call's own log directory
            log_dir.mkdir(exist_ok=True)
        self.others: list[Sandbox] = []                  # the judge's sandboxes, closed with this one
        if self.cli:                                     # the CLI and the node it may run on, as installed
            readable += program_paths(CLIS[self.cli].program)
        # A CLI agent on a login waits for the login's plan with every session on the machine: the plan's directory
        # (its PAUSE, MAX_USED, SLOTS and report) is the one outside, shared into every call; the worker inside runs
        # the Base Agent's wait, slot and holds there. A ChatGPT login's file is copied in; a Claude login signs in
        # with its token, passed by name.
        login = self.params.get("login")
        self.chatgpt = self.cli == "codex" and login == "chatgpt"
        self.claude_login = self.cli == "claude" and login == "claude"
        # Under its real name: the sandbox shows a linked path under both names, and the session's cover (the Base
        # Agent's _bound) hides the one it is given.
        self.plan = codex_login.plan_dir(self.params.get("plan_dir"), login).resolve() \
            if self.cli and codex_login.LOGINS.get(login) == self.cli else None
        if self.plan is not None:
            self.plan.mkdir(parents=True, exist_ok=True)
        network, allow = envc.get("network") or "no-network", list(envc.get("allowed_hosts") or [])
        wanted = [*model_hosts, *(codex_login.hosts(login) if self.plan is not None else [])]   # and a plan login's
        if network != "public" and wanted:
            network, allow = "allowlist", [*allow, *[h for h in wanted if h not in allow]]
        isolated, wants_proxy = network_mode(network)
        self.allow_proxy = AllowlistProxy(allow, log=records / f"network-{role}.jsonl" if records else None) \
            if (isolated == "isolated" and wants_proxy and allow) else None
        self.sandbox = Sandbox(enabled=True, network=network, allow=allow, readable=readable,
                               writable=[*writable, *([self.plan] if self.plan is not None else [])],
                               proxy_url=self.allow_proxy.url if self.allow_proxy else None,
                               env_allow=[*(envc.get("env") or []), *model_keys], enforce=self.enforce,
                               packages=list(envc.get("packages") or []), framework=True, record_dir=records,
                               probe_absent=[*HOME_SECRETS, *run_secrets(config)],
                               limits=dict(envc.get("resources") or {}), role=role)
        # What the config declares (not the CLI's installation), searched again for run records before calls.
        self._recheck = Recheck([*config_paths(config, "environment.read", envc.get("read")),
                                 *config_paths(config, "environment.write", envc.get("write"))])

    def __call__(self, task: dict, adapter: Path | None) -> RunOutput:
        self._recheck()
        work = self.work_root / f"{_safe(task.get('id', 'task'))}-{uuid.uuid4().hex[:8]}"
        home = work / "home"
        home.mkdir(parents=True)
        # Logs and workspaces go to this call's own directory.
        params = {**self.params, "logs": str(work / "logs"), "workspaces": str(work / "workspaces")}
        from hermit.adapter.session import ENV_ENFORCE
        env = framework_env({ENV_ENFORCE: self.enforce})
        # A Claude login's token passes by name for this call's worker only (keep), never through the sandbox's
        # env_allow: a shared judge is this sandbox again, and it gets no keep.
        keep = [ENV_ENFORCE, *([codex_login.TOKEN_ENV] if self.claude_login else [])]
        if self.chatgpt:     # inside, the agent keeps this call's copy in its workspaces; the shared one is out here
            params["login_root"] = str(work / "workspaces")
        if self.plan is not None:
            params["plan_dir"] = str(self.plan)                     # the machine's plan, not this call's copy
        # A CLI agent's home: only a ChatGPT login is copied in (the recorded exception); a Claude login signs in with
        # its token, so no file is.
        if self.cli:
            folder, var, _ = CLI_HOMES[self.cli]
            agent_home = home / folder
            agent_home.mkdir()
            if self.chatgpt:
                codex_login.prepare(codex_login.ROOT, agent_home)      # the shared copy, refreshed by earlier calls
            env[var] = str(agent_home)
            keep.append(var)
        binds = _log_binds(work, self.logs, [work])
        job = {"kind": "agent", "name": self.name, "params": params, "base": str(self.base), "task": task,
               "adapter": str(adapter) if adapter else None}
        clean = work.parent / f"{work.name}.log"
        try:
            try:
                payload = _run_worker(self.sandbox, job, work, home=home, readable=[adapter] if adapter else [],
                                      env=env, keep=keep, forward=self.forward, timeout=self.timeout, binds=binds)
            finally:
                if self.chatgpt:
                    # Every copy the call left: its run copy, and a session's own when the worker was stopped
                    # before it could hand that back (a timeout kills it). The latest refresh goes back to the
                    # shared copy; then none of them is kept.
                    codex_login.remove(home / CLI_HOMES["codex"][0] / CLI_HOMES["codex"][2], within=work)
                    left = [work / "workspaces" / "login" / codex_login.FILE,
                            *work.glob(f"workspaces/*/home/{codex_login.FILE}")]
                    for path in left:
                        codex_login.take_back(codex_login.ROOT, path, within=work)
                    for path in left:
                        codex_login.remove(path, within=work)
                    shutil.rmtree(work / "workspaces" / "login", ignore_errors=True)
            rec = payload["output"]
            # The log is copied into the learner's material outside any sandbox: it must lie where this call could
            # write (its work directory, its logs, the declared write paths), never a file it merely names.
            confined = self.sandbox.confines()            # best_effort without bubblewrap: no bind was applied
            log = _owned_log(rec.get("log"), [work, *self.sandbox.writable, *([] if confined else self.logs)],
                             binds if confined else [])
            extra = dict(rec.get("extra") or {})
            if rec.get("log") and log is None:
                extra["sandbox_warning"] = f"log {rec['log']!r} lies outside what the call could write; dropped"
            if log is not None:
                # Host code reads the log next (the learning material, a method's trajectory reader): a sanitized
                # copy beside the call's directory, where the call could not write, without links out or special
                # files (one the call left unreadable made readable first).
                if os.path.isfile(log) and not os.path.islink(log) and not os.access(log, os.R_OK):
                    os.chmod(log, os.stat(log).st_mode | 0o400)
                sanitized_copy(log.parent, clean, [log.name])
                log = clean / log.name if os.path.lexists(clean / log.name) else None
            extra["sandbox_work"] = str(work)
            result = RunOutput(output=rec.get("output"), log=log, cost_usd=rec.get("cost_usd"),
                               wall_s=rec.get("wall_s") or 0.0, api_speed=rec.get("api_speed"),
                               messages=rec.get("messages"), tokens=rec.get("tokens"), extra=extra)
        finally:                                         # what the run keeps of the call, a failed one too:
            redact_tree(work)                            # no model key in it
            redact_tree(clean)
        result.log = _after_scrub(result.log, [clean, work])
        return result

    def close(self) -> None:
        for box in [self.sandbox, *self.others]:
            box.close()
        if self.allow_proxy:
            self.allow_proxy.close()


class ContainerAgent:
    """The agent of a task with `environment.image`: its CLI runs in a container of its own, started from a worker
    process on the host, one per call. The worker puts the adapter's code the agent renders or serves (its context,
    its tools) in the strict sandbox (HA_ADAPTER_SANDBOX), so none of it runs in this process or with its access."""

    def __init__(self, config, role: str, params: dict, *, work_root: Path | None = None):
        d = config.data
        self.name, self.base, self.params = d["agent"]["name"], Path(config.base), params
        self.work_root = Path(work_root) if work_root else config.path(params.get("logs", ".logs")) / role
        self.timeout = float(d["agent"]["timeout_min"]) * 60 if d["agent"].get("timeout_min") else None
        if self.timeout:
            # The container's own session ends first and removes the container; the worker, stopped at the outer
            # deadline, could not.
            inner = max(self.timeout - 60, self.timeout * 0.9)
            self.params = {**params, "timeout_s": min(float(params.get("timeout_s") or inner), inner),
                           "deadline_s": inner}               # all attempts and waits together, too
        # A login's plan as this process names it, as the task sandbox passes it in (a worker would name its own).
        login = self.params.get("login")
        if codex_login.LOGINS.get(login):
            self.params = {**self.params, "plan_dir": str(codex_login.plan_dir(self.params.get("plan_dir"), login))}
        self.enforce = d["sandbox"].get("enforce", "required")
        self.box = Sandbox(enabled=False, role=role)     # the worker is on the host; the container is the boundary

    def __call__(self, task: dict, adapter: Path | None) -> RunOutput:
        from hermit.adapter.session import ENV_ENFORCE
        work = self.work_root / f"{_safe(task.get('id', 'task'))}-{uuid.uuid4().hex[:8]}"
        work.mkdir(parents=True)
        # The agent's logs go to this call's own directory; its workspaces stay where the task puts them
        # (agent.params.workspaces: short, outside any checkout; Codex 0.164's command runner failed now and then
        # under a long one), and this call's session there, named here, is scrubbed with the call's directory
        # whether the call ended or failed.
        root = Path(os.path.expanduser(str(self.params.get("workspaces", ".logs/workspaces"))))
        root = (root if root.is_absolute() else self.base / root).resolve()
        session = root / work.name                       # <workspaces>/<task>-<id>: work, home, log
        params = {**self.params, "logs": str(work / "logs"), "workspaces": str(root), "session_name": work.name}
        job = {"kind": "agent", "name": self.name, "params": params, "base": str(self.base), "task": task,
               "adapter": str(adapter) if adapter else None}
        try:
            payload = _run_worker(self.box, job, work, home=work, env=framework_env({ENV_ENFORCE: self.enforce}),
                                  timeout=self.timeout)
        finally:
            redact_tree(work)                            # what the run keeps of the call: no model key in it
            if session.is_dir():
                redact_tree(session)
        rec = payload["output"]
        log = _after_scrub(Path(rec["log"]) if rec.get("log") else None, [work, session])
        return RunOutput(output=rec.get("output"), log=log,
                         cost_usd=rec.get("cost_usd"), wall_s=rec.get("wall_s") or 0.0, api_speed=rec.get("api_speed"),
                         messages=rec.get("messages"), tokens=rec.get("tokens"), extra=dict(rec.get("extra") or {}))


def cli_sandbox(spec: dict, *, records: Path | None = None, enabled: bool = True, enforce: str = "required",
                probe_absent=(), role: str = "llm") -> tuple[Sandbox | None, AllowlistProxy | None]:
    """(the sandbox every session of a model only a CLI reaches runs in, its allowlist proxy or None): the run's `llm:
    {cli: ...}` (role llm, the Trainer's) and an acting pipeline's `agent.params.model: {cli: ...}` (role agent-model,
    agent_pipeline's). Each session sees its empty workspace and home (granted per call) and nothing of the run, holds
    its model's key variable, and reaches only its model: its provider's hosts or its login's, through a proxy of its
    own, and a model server or meter on the loopback. A Claude login's token is not among the sandbox's variables: the
    Base Agent passes it by name with each session. The login's plan stays outside (the Base Agent waits for it and
    reports to it from this process); the isolation check proves it absent, and the user's own Claude Code sign-in.
    Its callers give it the run's `enabled`, `enforce` and secrets to probe (`run_secrets`, HOME_SECRETS). (None, None)
    with `isolation: container`: the container is the boundary."""
    if (spec.get("isolation") or "sandbox") != "sandbox":
        return None, None
    if enabled and enforce == "required" and not shutil.which("bwrap"):      # refused before anything is started
        raise ValueError(f"a model through {spec.get('cli')} runs each session in a bubblewrap sandbox of its own "
                         "(isolation: sandbox, the default), and bubblewrap (bwrap) is not installed: install it, give "
                         "the model isolation: container, or set sandbox.enforce: best_effort (unconfined, recorded)")
    keys, hosts, ports = model_access([spec.get("binding")])
    login = spec.get("login")
    allow = [*hosts, *(h for h in codex_login.hosts(login) if h not in hosts)]
    absent = list(probe_absent)
    if login == "claude":                # the user's own sign-in, and the plan every session on the machine obeys
        absent += [Path.home() / ".claude" / ".credentials.json", codex_login.plan_dir(spec.get("plan_dir"), "claude")]
    records = Path(records) if records else None
    proxy = AllowlistProxy(allow, log=records / f"network-{role}.jsonl" if records else None) if allow else None
    try:
        box = Sandbox(enabled=enabled, network="allowlist" if allow else "no-network", allow=allow,
                      proxy_url=proxy.url if proxy else None, env_allow=keys, forward=ports, enforce=enforce,
                      framework=False, record_dir=records, role=role, probe_absent=absent)
    except BaseException:
        if proxy is not None:
            proxy.close()
        raise
    return box, proxy


def agent_pipeline(config, role: str, *, records: Path | None, work_root: Path | None = None):
    """(pipeline, close): the agent in the task sandbox; in its own container from a worker on the host
    (`environment.image`); or in this process when the pipeline runs on the host (`host_pipeline`: the sandbox off,
    a Harbor task set, `environment.runner: host`). An agent whose model only a CLI reaches (`agent.params.model:
    {cli: ...}`) gets it as a model endpoint started here, each request one session in a sandbox of its own built as
    the Trainer builds the llm's (cli_sandbox, role agent-model); the pipeline sees a URL and an ordinary model spec,
    which is never written to the config."""
    d = config.data
    set_hidden(hidden_paths(d["sandbox"].get("hide") or []))    # eval and check too: what only the judge may read
    params = dict(d["agent"].get("params") or {})
    endpoint = None
    if isinstance(params.get("model"), dict) and params["model"].get("cli"):
        box, proxy = cli_sandbox(params["model"], records=records, enabled=d["sandbox"]["enabled"],
                                 enforce=d["sandbox"].get("enforce", "required"),
                                 probe_absent=[*run_secrets(config), *HOME_SECRETS], role="agent-model")
        made = [thing for thing in (box, proxy) if thing is not None]
        try:
            endpoint = cli_endpoint(params["model"], Path(records) / "agent_sessions" if records else None,
                                    sandbox=box)
        except BaseException:
            for thing in made:
                thing.close()
            raise
        endpoint.closing += made                         # closed with the endpoint, after it
    stop = endpoint.close if endpoint is not None else (lambda: None)
    try:
        if d["environment"].get("image") and d["sandbox"]["enabled"]:
            params["isolation"] = "container"
            params["container"] = {**(d["environment"].get("container") or {}), "image": d["environment"]["image"]}
            return ContainerAgent(config, role, params, work_root=work_root), stop
        if host_pipeline(config):
            from hermit.agent.acting.agent import load_agent
            if endpoint is not None:
                params["model"] = endpoint_spec(endpoint, params["model"])
            return load_agent(d["agent"]["name"], params, config.base).run, stop
        agent = SandboxedAgent(config, role, records=records, work_root=work_root, model_endpoint=endpoint)
    except BaseException:
        stop()                                           # the endpoint started for a pipeline that never came
        raise

    def close() -> None:
        agent.close()
        stop()
    return agent, close


class SandboxedJudge:
    """The judge in the task sandbox (`shared`: a new exec over the agent's work directory) or in a fresh sandbox
    holding only the collected paths (`separate`). The judge's file and `judge.tests` are shown to it alone."""

    def __init__(self, config, agent: SandboxedAgent):
        d = config.data
        self.agent, self.mode = agent, d["judge"].get("mode") or "host"
        self.spec, self.base = f"{d['judge']['path']}:{d['judge']['name']}", Path(config.base)
        module = str(d["judge"]["path"]).partition(":")[0]
        self.readable = [*([config.path(module)] if module.endswith(".py") else []),     # the file, not its dir
                         *config_paths(config, "judge.tests", d["judge"].get("tests"))]
        self.collect = list(d["judge"].get("collect") or [])
        # The judge's own sandbox: the task sandbox's settings, and its probe checks only the home's secrets (the
        # judge, its tests and the data are its to read, and so is what sandbox.hide covers for everyone else). A
        # separate judge gets none of what the agent could write (environment.write): only what is collected. Nor
        # does any judge get a login's plan, which steers every session on the machine: its probe proves it absent.
        plan = [agent.plan] if agent.plan is not None else []
        own = [w for w in agent.sandbox.writable if Path(w) not in plan]
        self.box = dataclasses.replace(agent.sandbox, probe_absent=[*HOME_SECRETS, *plan], role="judge", hide=[],
                                       writable=[] if self.mode == "separate" else own)
        agent.others.append(self.box)

    def __call__(self, task: dict, out: RunOutput) -> dict:
        work = out.extra.get("sandbox_work")
        if not work:
            raise RuntimeError("the sandboxed judge needs the agent's work directory (the call ran on the host)")
        work = Path(work)
        place = work.parent / f"{work.name}-judge-{uuid.uuid4().hex[:6]}"
        home = place / "home"
        home.mkdir(parents=True)
        record = output_record(out)
        record["extra"] = {k: v for k, v in (record.get("extra") or {}).items() if k != "sandbox_work"}
        job = {"kind": "judge", "spec": self.spec, "base": str(self.base), "task": task, "output": record}
        # Shared: the agent's work as the agent saw it, its per-call `.logs` at the names the agent wrote them under.
        binds = _log_binds(work, self.agent.logs, [work, place], existing=True,
                           shown=[work.parent / f"{work.name}.log"])      # the agent's log copy, read by the judge
        writable = [work]
        if self.mode == "separate":
            fresh = place / "collected"
            skipped = sanitized_copy(work, fresh, self.collect)
            if skipped:
                (place / "skipped.txt").write_text("\n".join(skipped))
            # The collected paths at the agent's own path, so the answer's references still resolve; nothing else
            # of what the agent left is there.
            binds, writable = [Bind(fresh, str(work), True)], []
        # The log the agent returned is a sanitized copy beside its work directory: the judge reads it there.
        log_copy = [out.log.parent] if out.log and Path(out.log).parent.name == f"{work.name}.log" else []
        try:
            payload = _run_worker(self.box, job, place, home=home, readable=[*self.readable, *log_copy],
                                  writable=writable, env=framework_env(),
                                  forward=self.agent.forward, timeout=self.agent.timeout, binds=binds)
        finally:
            for kept in (place, *writable):              # the judge holds the task's keys too: none kept
                redact_tree(kept)
        return payload["result"]


def judge_for(config, pipeline):
    """The judge as `run_tasks` calls it: in this process (`host`, or any mode with the sandbox off), or sandboxed
    beside the task sandbox."""
    d = config.data
    mode = d["judge"].get("mode") or "host"
    if mode == "host" or not isinstance(pipeline, SandboxedAgent):
        return load_callable(f"{d['judge']['path']}:{d['judge']['name']}", config.base)
    return SandboxedJudge(config, pipeline)


class ToolRunner:
    """Runs a declared tool in a sandbox: a template's tool (the learner's adapter is what it acts on) in a strict
    one, its code and the template's files read-only, the adapter read-write, no network, no environment but
    PYTHONPATH; a task's tool (task code, like its agent) in one built from `environment` (its reads, writes,
    packages, variables and network) besides its own code and the adapter."""

    def __init__(self, config, *, records: Path | None, work_root: Path):
        d = config.data
        envc = d["environment"]
        self.work_root = Path(work_root)
        self.enabled = bool(d["sandbox"]["enabled"])
        enforce = d["sandbox"].get("enforce", "required")
        probe = [*HOME_SECRETS, *run_secrets(config)]
        self.box = Sandbox(enabled=self.enabled, network="no-network", env_allow=["PYTHONPATH"], enforce=enforce,
                           record_dir=records, probe_absent=probe, role="tool")
        self.allow_proxy = None
        task_network, allow = envc.get("network") or "no-network", list(envc.get("allowed_hosts") or [])
        # The grants first: one that cannot be resolved refuses the run before a proxy is left running.
        readable = config_paths(config, "environment.read", envc.get("read"))
        writable = config_paths(config, "environment.write", envc.get("write"))
        if self.enabled and network_mode(task_network) == ("isolated", True) and allow:
            self.allow_proxy = AllowlistProxy(allow, log=records / "network-task-tool.jsonl" if records else None)
        # With environment.runner: host only the pipeline runs here; a task's declared tool keeps its sandbox (its own
        # file, the adapter, environment's network and variables).
        self.task_box = Sandbox(enabled=self.enabled, network=task_network, allow=allow,
                                readable=readable, writable=writable,
                                proxy_url=self.allow_proxy.url if self.allow_proxy else None,
                                env_allow=["PYTHONPATH", *(envc.get("env") or [])], enforce=enforce,
                                packages=list(envc.get("packages") or []), record_dir=records, probe_absent=probe,
                                limits=dict(envc.get("resources") or {}), role="task-tool")
        self.timeout = float(d["trainer"].get("call_timeout_min") or 60) * 60
        self._recheck = Recheck([*readable, *writable])          # a task tool's grants, searched again

    def __call__(self, entrypoint: str, base: Path, arguments: dict, bound: dict, kind: str = "template") -> object:
        if kind == "task":
            self._recheck()
        work = self.work_root / f"tool-{uuid.uuid4().hex[:8]}"
        home = work / "home"
        home.mkdir(parents=True)
        adapter, root = bound.get("_adapter"), bound.get("_root")
        module = str(entrypoint).partition(":")[0]
        # The tool's own code: a template's whole directory (the template's files), a task's file alone (it often
        # sits beside the run's data and records; what else it needs is declared in environment.read).
        own = None
        if module.endswith(".py"):
            own = (Path(base) / module).resolve()
            own = own if kind == "task" else own.parent
        job = {"kind": "tool", "entrypoint": entrypoint, "base": str(base), "arguments": arguments,
               "bound": {k: str(v) for k, v in bound.items()}}
        box = self.task_box if kind == "task" else self.box
        try:
            payload = _run_worker(box, job, work, home=home,
                                  readable=[p for p in (own, root) if p is not None],
                                  writable=[adapter] if adapter is not None else [], timeout=self.timeout,
                                  env=framework_env())
        finally:
            redact_tree(work)                            # what removal below might leave: no model key in it
            owner_readable(work)                         # a directory the tool left read-only: removable again
            shutil.rmtree(work, ignore_errors=True)
        return payload["result"]

    def close(self) -> None:
        self.box.close()
        self.task_box.close()
        if self.allow_proxy:
            self.allow_proxy.close()
