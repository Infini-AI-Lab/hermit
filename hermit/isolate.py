"""The task sandbox, the judge's sandboxes and the declared tools' sandbox (docs/developer/sandbox-design.md).

The acting agent runs in the task sandbox: a worker process (`worker.py`) inside bubblewrap that runs the agent on
one task and hands the result back, so nothing the agent or the adapter's code starts runs with the framework's
access. What the sandbox shows is the run config's `environment` section and nothing else:

- read-only: the framework's packages (the acting layer runs inside), `environment.packages` with their
  dependencies, `environment.read`, the adapter of this call, and a CLI agent's own installation;
- read-write: this call's work directory (the built-in agents' logs and workspaces go there), a fresh directory in
  place of the `.logs` beside the task's agent file, and `environment.write`;
- environment: what the framework sets, and `environment.env`;
- network: `environment.network` (no-network | allowlist | public); the run's model proxy is always reachable, and
  the agent's binding is routed through it, so the sandbox holds a secret of its own, never the provider's key.

The data and split files, the judge, the runs and trees directories may not lie in a declared path: the config is
refused, and the isolation check inside proves them absent. A task with `environment.image` runs its CLI agent in a
container of its own (the Base Agent's container isolation), started from a worker on the host that keeps the
adapter's code in the strict sandbox; a Harbor task set (`agent.name: harbor`) runs in Harbor's, from this process.

The judge runs where `judge.mode` says: `host` in this process on the downloaded answer, `shared` in the task
sandbox after the agent (a new exec over the same work directory, with `judge.tests` shown to it alone), `separate`
in a fresh sandbox holding only the paths `judge.collect` names. A template's declared tools run in a strict
sandbox (their code and the template read-only, the adapter they act on read-write, no network); a task's in one
built from `environment`, like its agent.
"""
from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
from urllib.parse import urlparse
import uuid

from hermit.agent.base import login as codex_login
from hermit.model_proxy import ModelProxy, rewrite
from hermit.sandbox import (LIMITS, NETWORK_MODES, PACKAGE, AllowlistProxy, Bind, Sandbox, framework_env,
                            network_mode, package_closure, program_paths, sanitized_copy)
from hermit.task import RunOutput, load_callable, output_record

WORKER = PACKAGE / "worker.py"
CLI_HOMES = {"codex": (".codex", "CODEX_HOME", "auth.json"), "claude": (".claude", "CLAUDE_CONFIG_DIR", ".credentials.json")}
CHATGPT_HOSTS = ["chatgpt.com", "auth.openai.com"]       # a Codex ChatGPT login reaches its plan here (recorded)
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
        if not isinstance(env.get(key) or [], list):
            raise ValueError(f"environment.{key} must be a list")
    if env.get("runner", "sandbox") not in ("sandbox", "host"):
        raise ValueError(f"environment.runner must be sandbox or host, not {env.get('runner')!r}")
    if env.get("runner") == "host" and (env.get("read") or env.get("packages") or env.get("write")):
        raise ValueError("environment.read, write and packages build the task sandbox; with environment.runner: "
                         "host the pipeline runs in the Trainer's process")
    if env.get("network") not in NETWORK_MODES:
        raise ValueError(f"environment.network must be one of {list(NETWORK_MODES)}, not {env.get('network')!r}")
    if env.get("allowed_hosts") and env["network"] != "allowlist":
        raise ValueError("environment.allowed_hosts needs environment.network: allowlist")
    evolver = s.get("evolver") or {}
    unknown = set(evolver) - {"allowed_hosts", "env", "resources"}
    if unknown:
        raise ValueError(f"sandbox.evolver: unknown keys {sorted(unknown)}")
    for where, res in (("environment.resources", env.get("resources")), ("sandbox.evolver.resources",
                                                                          evolver.get("resources"))):
        if res and (not isinstance(res, dict) or set(res) - LIMITS):
            raise ValueError(f"{where} takes {sorted(LIMITS)}")
    for key in ("allowed_hosts", "env"):
        if not isinstance(evolver.get(key) or [], list):
            raise ValueError(f"sandbox.evolver.{key} must be a list")
    mode = judge.get("mode") or "host"
    if mode not in JUDGE_MODES:
        raise ValueError(f"judge.mode must be one of {list(JUDGE_MODES)}, not {mode!r}")
    for key in ("tests", "collect"):
        if not isinstance(judge.get(key) or [], list):
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
    if (d["evolver"].get("params") or {}).get("isolation") == "container":
        raise ValueError("the learner runs in the evolver sandbox (sandbox.evolver); isolation: container is for "
                         "the acting agent's environment.image and an llm: {cli: ...} endpoint")
    reason = host_pipeline(config)
    if env.get("image"):
        if _cli_kind(d["agent"]["name"], params) != "codex" and params.get("base") != "codex":
            raise ValueError("environment.image runs a Codex acting agent in a container: agent.base (or agent.name) "
                             "must be codex")
        if env.get("packages") or env.get("read"):
            raise ValueError("environment.packages and environment.read build a bubblewrap sandbox; with "
                             "environment.image the image holds the agent's world")
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
    # What the learner reads is declared the same way (its sandbox shows these under /w/environment/<name>).
    declared += [("evolver_access.environment", v) for v in d["evolver_access"].get("environment") or []]
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


def _agent_logs(name: str, base: Path) -> list[Path]:
    """The `.logs` directory beside a task's own agent file (file.py:Class), where real agents keep their logs
    (examples/officeqa). Each call gets a fresh directory bound there, so no call reads an earlier one's."""
    target = str(name).rpartition(":")[0]
    if not target.endswith(".py"):
        return []
    return [(Path(base) / target).resolve().parent / ".logs"]


def _route_models(params: dict, proxy: ModelProxy) -> tuple[dict, dict, list[int]]:
    """The agent's params as the sandbox sees them: its binding (a CLI agent's) or its model spec (a chat agent's)
    routed through the model proxy, and with them the provider settings bound from it (a Codex agent's `args`, a
    Claude Code agent's `env`); the environment the routes need (key variable -> secret); loopback ports a local
    model server listens on."""
    params = dict(params)
    env, ports = {}, []
    for key in ("binding", "model"):
        spec = params.get(key)
        if not isinstance(spec, dict) or not spec.get("base_url"):
            continue
        mapping, key_env, secret = proxy.origins(spec)
        params[key] = {k: rewrite(v, mapping) if k in ("base_url", "messages_url") and v else v
                       for k, v in spec.items()}
        for url in (params[key].get("base_url"), params[key].get("messages_url")):
            parsed = urlparse(url) if url else None
            if parsed and parsed.hostname in ("127.0.0.1", "localhost") and parsed.port != proxy.port:
                ports.append(parsed.port or (443 if parsed.scheme == "https" else 80))
        if isinstance(params.get("args"), list):
            params["args"] = [rewrite(str(a), mapping) for a in params["args"]]
        if isinstance(params.get("env"), dict):
            params["env"] = {k: rewrite(str(v), mapping) for k, v in params["env"].items()}
        if key_env and secret:
            env[key_env] = secret
    return params, env, sorted(set(ports))


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
    try:
        with open(log, "wb") as out:
            proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=out, stderr=subprocess.STDOUT, env=env,
                                    cwd=str(work), pass_fds=(write_end,))
    except BaseException:
        os.close(read_end)
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
        raise TimeoutError(f"the sandboxed {job['kind']} ran longer than {timeout / 60:.1f} min and was stopped; "
                           f"log {log}")
    data = received[0] if received else ""
    try:
        code = proc.wait(timeout=30)                     # it answered; a process kept alive after that is stopped
    except subprocess.TimeoutExpired:
        proc.kill()
        code = proc.wait()
    if not data:
        tail = log.read_text(errors="replace")[-1500:] if log.exists() else ""
        reason = "the sandbox's isolation check failed" if code == 97 else f"it exited with code {code}"
        raise RuntimeError(f"the sandboxed {job['kind']} returned nothing: {reason}; log {log}:\n{tail}")
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
    for b in binds:
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

    def __init__(self, config, role: str, *, records: Path | None, work_root: Path | None = None):
        d = config.data
        agent, envc = d["agent"], d["environment"]
        self.config, self.role = config, role
        self.name, self.base = agent["name"], Path(config.base)
        self.proxy = ModelProxy(log=records / f"models-{role}.jsonl" if records else None)
        self.params, self.model_env, local_ports = _route_models(dict(agent.get("params") or {}), self.proxy)
        self.forward = [*local_ports, self.proxy.port]
        self.work_root = Path(work_root) if work_root else config.path(self.params.get("logs", ".logs")) / role
        self.cli = _cli_kind(self.name, self.params)
        self.timeout = float(agent["timeout_min"]) * 60 if agent.get("timeout_min") else None
        self.enforce = d["sandbox"].get("enforce", "required")
        self.logs = _agent_logs(self.name, self.base)
        for log_dir in self.logs:                        # the mount point of each call's own log directory
            log_dir.mkdir(exist_ok=True)
        self.others: list[Sandbox] = []                  # the judge's sandboxes, closed with this one
        missing = [p for p in envc.get("packages") or [] if p != "*" and not package_closure([p])]
        if missing:
            raise ValueError(f"environment.packages names distributions that are not installed: {missing}")
        readable = config_paths(config, "environment.read", envc.get("read"))
        if self.cli:                                     # the CLI and the node it may run on, as installed
            readable += program_paths(self.cli)
        # A Codex agent on a ChatGPT login waits for the login's plan with every session on the machine: the plan's
        # directory (its PAUSE, MAX_USED, SLOTS and report) is the one outside, shared into every call.
        self.chatgpt = self.cli == "codex" and self.params.get("login") == "chatgpt"
        self.plan = codex_login.plan_dir(self.params.get("plan_dir")) if self.chatgpt else None
        if self.plan is not None:
            self.plan.mkdir(parents=True, exist_ok=True)
        network, allow = envc.get("network") or "no-network", list(envc.get("allowed_hosts") or [])
        if self.chatgpt:                                 # the recorded exception: the plan login reaches its hosts
            network, allow = "allowlist", [*allow, *[h for h in CHATGPT_HOSTS if h not in allow]]
        isolated, wants_proxy = network_mode(network)
        self.allow_proxy = AllowlistProxy(allow, log=records / f"network-{role}.jsonl" if records else None) \
            if (isolated == "isolated" and wants_proxy and allow) else None
        self.sandbox = Sandbox(enabled=True, network=network, allow=allow, readable=readable,
                               writable=[*config_paths(config, "environment.write", envc.get("write")),
                                         *([self.plan] if self.plan is not None else [])],
                               proxy_url=self.allow_proxy.url if self.allow_proxy else None,
                               env_allow=list(envc.get("env") or []), enforce=self.enforce,
                               packages=list(envc.get("packages") or []), framework=True, record_dir=records,
                               probe_absent=[*HOME_SECRETS, *run_secrets(config)],
                               limits=dict(envc.get("resources") or {}), role=role)

    def __call__(self, task: dict, adapter: Path | None) -> RunOutput:
        work = self.work_root / f"{_safe(task.get('id', 'task'))}-{uuid.uuid4().hex[:8]}"
        home = work / "home"
        home.mkdir(parents=True)
        # Logs and workspaces go to this call's own directory.
        params = {**self.params, "logs": str(work / "logs"), "workspaces": str(work / "workspaces")}
        from hermit.adapter.session import ENV_ENFORCE
        env = framework_env({ENV_ENFORCE: self.enforce, **self.model_env})
        keep = [ENV_ENFORCE, *self.model_env]
        if self.chatgpt:     # inside, the agent keeps this call's copy in its workspaces; the shared one is out here
            params["login_root"] = str(work / "workspaces")
            params["plan_dir"] = str(self.plan)                     # the machine's plan, not this call's copy
        if self.cli:         # a CLI agent's home; only a ChatGPT login is copied in (the recorded exception)
            folder, var, _ = CLI_HOMES[self.cli]
            agent_home = home / folder
            agent_home.mkdir()
            if self.chatgpt:
                codex_login.prepare(codex_login.ROOT, agent_home)      # the shared copy, refreshed by earlier calls
            env[var] = str(agent_home)
            keep.append(var)
        binds = [Bind(work / "agent-logs" / str(i), str(log_dir), True) for i, log_dir in enumerate(self.logs)]
        job = {"kind": "agent", "name": self.name, "params": params, "base": str(self.base), "task": task,
               "adapter": str(adapter) if adapter else None}
        try:
            payload = _run_worker(self.sandbox, job, work, home=home, readable=[adapter] if adapter else [],
                                  env=env, keep=keep, forward=self.forward, timeout=self.timeout, binds=binds)
        finally:
            if self.chatgpt:
                # Every copy the call left: its run copy, and a session's own when the worker was stopped before it
                # could hand that back (a timeout kills it). The latest refresh goes back to the shared copy; then
                # none of them is kept.
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
        log = _owned_log(rec.get("log"), [work, *self.sandbox.writable], binds)
        extra = dict(rec.get("extra") or {})
        if rec.get("log") and log is None:
            extra["sandbox_warning"] = f"log {rec['log']!r} lies outside what the call could write; dropped"
        extra["sandbox_work"] = str(work)
        return RunOutput(output=rec.get("output"), log=log,
                         cost_usd=rec.get("cost_usd"), wall_s=rec.get("wall_s") or 0.0, api_speed=rec.get("api_speed"),
                         messages=rec.get("messages"), tokens=rec.get("tokens"), extra=extra)

    def close(self) -> None:
        for box in [self.sandbox, *self.others]:
            box.close()
        self.proxy.close()
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
        self.enforce = d["sandbox"].get("enforce", "required")
        self.box = Sandbox(enabled=False, role=role)     # the worker is on the host; the container is the boundary

    def __call__(self, task: dict, adapter: Path | None) -> RunOutput:
        from hermit.adapter.session import ENV_ENFORCE
        work = self.work_root / f"{_safe(task.get('id', 'task'))}-{uuid.uuid4().hex[:8]}"
        work.mkdir(parents=True)
        job = {"kind": "agent", "name": self.name, "params": self.params, "base": str(self.base), "task": task,
               "adapter": str(adapter) if adapter else None}
        payload = _run_worker(self.box, job, work, home=work, env=framework_env({ENV_ENFORCE: self.enforce}),
                              timeout=self.timeout)
        rec = payload["output"]
        return RunOutput(output=rec.get("output"), log=Path(rec["log"]) if rec.get("log") else None,
                         cost_usd=rec.get("cost_usd"), wall_s=rec.get("wall_s") or 0.0, api_speed=rec.get("api_speed"),
                         messages=rec.get("messages"), tokens=rec.get("tokens"), extra=dict(rec.get("extra") or {}))


def agent_pipeline(config, role: str, *, records: Path | None, work_root: Path | None = None):
    """(pipeline, close): the agent in the task sandbox; in its own container from a worker on the host
    (`environment.image`); or in this process when the pipeline runs on the host (`host_pipeline`: the sandbox off,
    a Harbor task set, `environment.runner: host`)."""
    d = config.data
    if d["environment"].get("image") and d["sandbox"]["enabled"]:
        params = dict(d["agent"].get("params") or {})
        params["isolation"] = "container"
        params["container"] = {**(d["environment"].get("container") or {}), "image": d["environment"]["image"]}
        agent = ContainerAgent(config, role, params, work_root=work_root)
        return agent, (lambda: None)
    if host_pipeline(config):
        from hermit.agent.acting.agent import load_agent
        params = dict(d["agent"].get("params") or {})
        return load_agent(d["agent"]["name"], params, config.base).run, (lambda: None)
    agent = SandboxedAgent(config, role, records=records, work_root=work_root)
    return agent, agent.close


class SandboxedJudge:
    """The judge in the task sandbox (`shared`: a new exec over the agent's work directory) or in a fresh sandbox
    holding only the collected paths (`separate`). The judge's file and `judge.tests` are shown to it alone."""

    def __init__(self, config, agent: SandboxedAgent):
        d = config.data
        self.agent, self.mode = agent, d["judge"].get("mode") or "host"
        self.spec, self.base = f"{d['judge']['path']}:{d['judge']['name']}", Path(config.base)
        module = str(d["judge"]["path"]).partition(":")[0]
        self.readable = [*([config.path(module).parent] if module.endswith(".py") else []),
                         *config_paths(config, "judge.tests", d["judge"].get("tests"))]
        self.collect = list(d["judge"].get("collect") or [])
        # The judge's own sandbox: the task sandbox's settings, and its probe checks only the home's secrets (the
        # judge, its tests and the data are its to read). A separate judge gets none of what the agent could write
        # (environment.write): only what is collected.
        self.box = dataclasses.replace(agent.sandbox, probe_absent=list(HOME_SECRETS), role="judge",
                                       **({"writable": []} if self.mode == "separate" else {}))
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
        binds = [Bind(work / "agent-logs" / str(i), str(log_dir), True) for i, log_dir in enumerate(self.agent.logs)
                 if (work / "agent-logs" / str(i)).is_dir()]
        writable = [work]
        if self.mode == "separate":
            fresh = place / "collected"
            skipped = sanitized_copy(work, fresh, self.collect)
            if skipped:
                (place / "skipped.txt").write_text("\n".join(skipped))
            # The collected paths at the agent's own path, so the answer's references still resolve; nothing else
            # of what the agent left is there.
            binds, writable = [Bind(fresh, str(work), True)], []
        payload = _run_worker(self.box, job, place, home=home, readable=self.readable, writable=writable,
                              env=framework_env(self.agent.model_env), keep=list(self.agent.model_env),
                              forward=self.agent.forward, timeout=self.agent.timeout, binds=binds)
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
        if self.enabled and network_mode(task_network) == ("isolated", True) and allow:
            self.allow_proxy = AllowlistProxy(allow, log=records / "network-task-tool.jsonl" if records else None)
        on_host = d["environment"].get("runner") == "host"
        self.task_box = Sandbox(enabled=self.enabled and not on_host, network=task_network, allow=allow,
                                readable=config_paths(config, "environment.read", envc.get("read")),
                                writable=config_paths(config, "environment.write", envc.get("write")),
                                proxy_url=self.allow_proxy.url if self.allow_proxy else None,
                                env_allow=["PYTHONPATH", *(envc.get("env") or [])], enforce=enforce,
                                packages=list(envc.get("packages") or []), record_dir=records, probe_absent=probe,
                                limits=dict(envc.get("resources") or {}), role="task-tool")
        self.timeout = float(d["trainer"].get("call_timeout_min") or 60) * 60

    def __call__(self, entrypoint: str, base: Path, arguments: dict, bound: dict, kind: str = "template") -> object:
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
            shutil.rmtree(work, ignore_errors=True)
        return payload["result"]

    def close(self) -> None:
        self.box.close()
        self.task_box.close()
        if self.allow_proxy:
            self.allow_proxy.close()
