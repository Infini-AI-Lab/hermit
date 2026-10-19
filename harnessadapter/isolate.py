"""What the framework runs besides the learner, each in a sandbox of its own role (#11): the agent that does a task
(rollout in training, eval on the test split, check's probe) and the tools a template or task declares.

The Trainer, `eval` and `check` used to call the agent in their own process, so the agent, and everything it
started, ran with the framework's access: the task's answers, the run records, the network. Now every call runs in
a worker process (`worker.py`) inside the role's sandbox, and only the worker can hand its result back.

The agent's sandbox (rollout, eval, check) is a preset for task code, which reaches far more than a learner does:
its own helpers beside the config, the Python packages of its benchmark, the variables its launcher exported, the
endpoints those name. Its point is the run's secrets, not a minimal world:

- read-only: the task's project (the deepest directory holding the config, the data, the task info, the judge and
  the agent), the whole Python environment, the adapter of this call, `sandbox.read` and `profiles.<role>.read`;
- read-write: the agent's own directory (agents keep logs beside their code), this call's work directory (the
  built-in agents' logs and workspaces go there) and `profiles.<role>.write`;
- covered, read-only, wherever they fall in the above: the run's secrets (the task's data and split files, the judge,
  the runs and trees directories, the agent's log directory) and `profiles.<role>.mask` (a judge's answers kept
  beside the task, say);
- environment: inherited (`sandbox.env` is the learner's), unless `profiles.<role>.env` lists the variables;
- network: open, unless `profiles.<role>.network: isolated`, which gives the role its own allowlist proxy for
  `sandbox.allow`, `profiles.<role>.allow` and the agent's model endpoint, and forwards a loopback model;
- `profiles.<role>.enabled: false` runs that role as before, in the framework's process.

`eval` and `check` take `profiles.rollout` unless they have a profile of their own. The `tool` role (a template's or
task's declared tools) keeps the strict defaults of the learner: `sandbox.network`, `sandbox.env` and the framework's
own packages. `sandbox.profiles.<role>.inherit: false` drops the top-level `read`, `allow` and `packages`.
"""
from __future__ import annotations

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

from harnessadapter.sandbox import FRAMEWORK, PACKAGE, AllowlistProxy, Sandbox, network_mode
from harnessadapter.task import RunOutput

ROLES = ("learner", "rollout", "eval", "check", "tool")
AGENT_ROLES = ("rollout", "eval", "check")
WORKER = PACKAGE / "worker.py"
PROFILE_KEYS = {"enabled", "inherit", "read", "write", "allow", "packages", "mask", "network", "env"}
CLI_HOMES = {"codex": (".codex", "CODEX_HOME", "auth.json"), "claude": (".claude", "CLAUDE_CONFIG_DIR", ".credentials.json")}


def profile(sandbox: dict, role: str) -> dict:
    """`sandbox.profiles.<role>` with its defaults, merged with the top-level lists unless `inherit: false`."""
    profiles = sandbox.get("profiles") or {}
    raw = profiles.get(role)
    if raw is None and role in ("eval", "check"):
        raw = profiles.get("rollout")                    # the same agent, so the same needs
    raw = dict(raw or {})
    unknown = set(raw) - PROFILE_KEYS
    if unknown:
        raise ValueError(f"sandbox.profiles.{role}: unknown keys {sorted(unknown)}")
    agent = role in AGENT_ROLES
    network = raw.get("network", "open" if agent else sandbox.get("network", "isolated"))
    network_mode(network)
    env = raw["env"] if "env" in raw else (None if agent else sandbox.get("env"))
    if env is not None and not isinstance(env, list):
        raise ValueError(f"sandbox.profiles.{role}.env must be a list of variable names or null")
    inherit = raw.get("inherit", True)
    top = {k: list(sandbox.get(k) or []) for k in ("read", "allow", "packages")} if inherit else \
        {"read": [], "allow": [], "packages": []}
    return {"enabled": bool(sandbox.get("enabled", True)) and bool(raw.get("enabled", True)),
            "read": top["read"] + list(raw.get("read") or []), "write": list(raw.get("write") or []),
            "allow": top["allow"] + list(raw.get("allow") or []),
            "packages": (["*"] if agent else []) + top["packages"] + list(raw.get("packages") or []),
            "mask": list(raw.get("mask") or []), "network": network, "env": env}


def validate(sandbox: dict) -> None:
    for role in (sandbox.get("profiles") or {}):
        if role not in ROLES:
            raise ValueError(f"sandbox.profiles: unknown role {role!r}; the roles are {', '.join(ROLES)}")
        profile(sandbox, role)


def run_secrets(config) -> list[Path]:
    """What a task's agent must not read although it may sit inside a directory it is shown: the data and split
    files, the judge, where runs and trees are kept, and the agent's log directory (earlier calls' work)."""
    d = config.data
    out = [config.path(d["data"].get(k)) for k in ("tasks", "train", "val", "test")]
    judge = d.get("judge") or {}
    if judge.get("path"):
        out.append(config.path(judge["path"]))
    try:
        out.append(config.run_dir().parent)
        from harnessadapter.adapter.manifest import read_manifest
        out.append(config.tree_dir(read_manifest(config.template_dir())["template"]["id"]))
    except Exception:                                    # a config without a template still masks the rest
        pass
    params = (d.get("agent") or {}).get("params") or {}
    out.append(config.path(params.get("logs", ".logs")))
    return [p for p in out if p is not None]


def config_paths(config, role: str, key: str, values) -> list[Path]:
    """Paths from the config; one naming an unset variable is an error, not a path that quietly binds nothing."""
    out = []
    for value in values:
        path = config.path(value)
        if path is not None and "$" in str(path):
            raise ValueError(f"sandbox.profiles.{role}.{key}: {value!r} names an unset environment variable")
        if path is not None:
            out.append(path)
    return out


def project_root(config) -> list[Path]:
    """The task's project: the deepest directory holding the config and every path it names for the task (data,
    task info, judge, agent). An example keeps its config in a method's directory and its task in ../utils. When
    that directory would be the filesystem's root or the home, each path's own directory is given instead."""
    d = config.data
    names = [*(d["data"].get(k) for k in ("tasks", "train", "val", "test")), d.get("task_info"),
             (d.get("judge") or {}).get("path")]
    agent = str((d.get("agent") or {}).get("name") or "").rpartition(":")[0]
    if agent.endswith(".py"):
        names.append(agent)
    paths = [Path(config.base).resolve()]
    for name in names:
        p = config.path(name)
        if p is not None and p.exists():
            paths.append(p.resolve())
    common = Path(os.path.commonpath([str(p) for p in paths]))
    if common == Path("/") or common == Path.home().resolve() or len(common.parts) < 3:
        return sorted({p if p.is_dir() else p.parent for p in paths})
    return [common]


SKIP_WALK = {".git", ".venv", "node_modules", "__pycache__"}


def project_records(roots: list[Path], config, keep=()) -> list[Path]:
    """Every run-record directory in the task's project, besides this run's: the `runs` (or this config's output
    directory's name) and `.logs` of the other methods beside it, whose judge feedback can carry the answers.
    `keep` are directories the agent writes itself (its own logs)."""
    names = {"runs", ".logs", Path(str(config.data["trainer"].get("output_dir") or "runs")).name}
    keep = {Path(k).resolve() for k in keep}
    out = []
    for root in roots:
        for directory, children, _ in os.walk(root):
            descend = []
            for child in children:
                path = Path(directory) / child
                if child in names and path.resolve() not in keep:
                    out.append(path)
                elif child not in SKIP_WALK and not (path.is_symlink()):
                    descend.append(child)
            children[:] = descend
    return out


def _agent_logs(name: str, base: Path) -> list[Path]:
    """The `.logs` directory beside a task's own agent file (file.py:Class), the one place there it may write: real
    agents keep their logs there (examples/officeqa_paper). The code itself stays read-only, or an agent could
    rewrite what every later call, and the host, runs. A module agent and a built-in one get nothing."""
    target = str(name).rpartition(":")[0]
    if not target.endswith(".py"):
        return []
    logs = (Path(base) / target).resolve().parent / ".logs"
    logs.mkdir(exist_ok=True)
    return [logs]


def _model_access(data: dict) -> tuple[list[str], list[str], list[int]]:
    """The agent's model endpoints (allowed through the proxy), its key variables, and loopback ports to forward."""
    from harnessadapter.backends import bindings_of
    allow, keys, ports = [], [], []
    for who, binding, _ in bindings_of(data):
        if not who.startswith("agent"):
            continue
        for url in (binding.base_url, binding.spec().get("messages_url")):
            if not url:
                continue
            parsed = urlparse(url)
            if parsed.hostname in ("127.0.0.1", "localhost"):
                ports.append(parsed.port or (443 if parsed.scheme == "https" else 80))
            else:
                allow.append(url)
        if binding.api_key_env:
            keys.append(binding.api_key_env)
    return list(dict.fromkeys(allow)), list(dict.fromkeys(keys)), sorted(set(ports))


class RoleSandbox:
    """A role's Sandbox and allowlist proxy, built from the run config."""

    def __init__(self, config, role: str, *, records: Path | None, readable=(), writable=(), allow=(),
                 env_keys=(), masked=(), probe_absent=()):
        d = config.data
        s = d["sandbox"]
        prof = profile(s, role)
        self.role, self.enabled = role, prof["enabled"]
        network, wants_proxy = network_mode(prof["network"])
        allow = [*prof["allow"], *allow]
        env_allow = prof["env"]
        if env_allow is not None:
            env_allow = [*env_allow, *[k for k in env_keys if k not in env_allow]]
        self.proxy = AllowlistProxy(allow, log=records / f"network-{role}.jsonl" if records else None) \
            if (self.enabled and network == "isolated" and wants_proxy and allow) else None
        self.sandbox = Sandbox(enabled=self.enabled, network=prof["network"], allow=allow,
                               read=config_paths(config, role, "read", prof["read"]), readable=list(readable),
                               writable=[*writable, *config_paths(config, role, "write", prof["write"])],
                               proxy_url=self.proxy.url if self.proxy else None, env_allow=env_allow,
                               enforce=s.get("enforce", "required"), packages=prof["packages"], record_dir=records,
                               probe_absent=list(probe_absent),
                               masked=[*masked, *config_paths(config, role, "mask", prof["mask"])], role=role,
                               audit=s.get("audit") if records else None)

    def close(self) -> None:
        self.sandbox.close()
        if self.proxy:
            self.proxy.close()


def _framework_env(extra: dict | None = None) -> dict:
    env = {**os.environ, **(extra or {})}
    env["PYTHONPATH"] = str(FRAMEWORK) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


def _run_worker(box: Sandbox, job: dict, work: Path, *, home: Path, readable=(), writable=(), env=None,
                keep=(), forward=(), timeout: float | None = None) -> dict:
    """Run worker.py in the sandbox with `job`; return its payload, or raise with what it left behind. After
    `timeout` seconds the sandbox is stopped (everything in it goes with it: its own process namespace)."""
    read_end, write_end = os.pipe()
    env = dict(env or _framework_env())
    env["HA_RESULT_FD"] = str(write_end)
    args, env = box.wrap([sys.executable, str(WORKER)], readable=list(readable), writable=[work, *writable],
                         env=env, home=home, keep=["PYTHONPATH", "HA_RESULT_FD", *keep], forward=list(forward))
    log = work / "worker.log"
    try:
        with open(log, "wb") as out:
            proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=out, stderr=subprocess.STDOUT, env=env,
                                    cwd=str(work), pass_fds=(write_end,))
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
    code = proc.wait()
    if not data:
        tail = log.read_text(errors="replace")[-1500:] if log.exists() else ""
        reason = "the sandbox's isolation check failed" if code == 97 else f"it exited with code {code}"
        raise RuntimeError(f"the sandboxed {job['kind']} returned nothing: {reason}; log {log}:\n{tail}")
    payload = json.loads(data)
    if not payload.get("ok"):
        raise RuntimeError(f"{payload.get('error')}\n{payload.get('traceback', '')}".strip())
    return payload


def _safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(text))[:60] or "task"


class SandboxedAgent:
    """The run's agent as a pipeline, every call in a worker inside the role's sandbox."""

    def __init__(self, config, role: str, *, records: Path | None, work_root: Path | None = None):
        d = config.data
        agent = d["agent"]
        self.name, self.params, self.base = agent["name"], dict(agent.get("params") or {}), Path(config.base)
        allow, self.keys, self.forward = _model_access(d)
        self.work_root = Path(work_root) if work_root else config.path(self.params.get("logs", ".logs")) / role
        self.cli = self._cli_kind()
        self.timeout = float(agent["timeout_min"]) * 60 if agent.get("timeout_min") else None
        # Only the logs beside the agent's code are writable (examples/officeqa_paper keeps them there); the code,
        # the config and the task's helpers are read-only. Every run record in the project is covered, this run's
        # and the other methods' beside it.
        project, logs = project_root(config), _agent_logs(self.name, self.base)
        self.box = RoleSandbox(config, role, records=records, readable=project, writable=logs,
                               allow=allow, env_keys=self.keys,
                               masked=[m for m in [*run_secrets(config), *project_records(project, config, keep=logs)]
                                       if m.resolve() not in {p.resolve() for p in logs}],
                               probe_absent=[Path.home() / ".ssh", Path.home() / ".bashrc"])

    def _cli_kind(self) -> str | None:
        from harnessadapter.backends import CLI_AGENTS
        kind = CLI_AGENTS.get(str(self.name))
        if kind is None and str(self.name) == "cli":
            kind = {"claude-code": "claude"}.get(self.params.get("backend", "codex"), self.params.get("backend", "codex"))
        return kind if kind in CLI_HOMES else None

    def __call__(self, task: dict, adapter: Path | None) -> RunOutput:
        work = self.work_root / f"{_safe(task.get('id', 'task'))}-{uuid.uuid4().hex[:8]}"
        home = work / "home"
        home.mkdir(parents=True)
        # Logs and workspaces go to this call's own directory: the agent's log directory is covered (earlier calls).
        params = {**self.params, "logs": str(work / "logs"), "workspaces": str(work / "workspaces")}
        env, keep = _framework_env(), list(self.keys)
        if self.cli:                                     # a CLI agent signs in with a copy of the user's login only
            folder, var, login = CLI_HOMES[self.cli]
            agent_home = home / folder
            agent_home.mkdir()
            source = Path.home() / folder / login
            if source.is_file():
                shutil.copy2(source, agent_home / login)
                os.chmod(agent_home / login, 0o600)
            env[var] = str(agent_home)
            keep.append(var)
        job = {"kind": "agent", "name": self.name, "params": params, "base": str(self.base), "task": task,
               "adapter": str(adapter) if adapter else None}
        try:
            payload = _run_worker(self.box.sandbox, job, work, home=home, readable=[adapter] if adapter else [],
                                  env=env, keep=keep, forward=self.forward, timeout=self.timeout)
        finally:
            if self.cli:                                 # the login copy does not outlive the call
                (home / CLI_HOMES[self.cli][0] / CLI_HOMES[self.cli][2]).unlink(missing_ok=True)
        rec = payload["output"]
        return RunOutput(output=rec.get("output"), log=Path(rec["log"]) if rec.get("log") else None,
                         cost_usd=rec.get("cost_usd"), wall_s=rec.get("wall_s") or 0.0, api_speed=rec.get("api_speed"),
                         messages=rec.get("messages"), tokens=rec.get("tokens"), extra=rec.get("extra") or {})

    def close(self) -> None:
        self.box.close()


def agent_pipeline(config, role: str, *, records: Path | None, work_root: Path | None = None):
    """(pipeline, close): the agent in the role's sandbox, or in this process when that role's sandbox is off."""
    if not profile(config.data["sandbox"], role)["enabled"]:
        from harnessadapter.agents.base import load_agent
        d = config.data
        return load_agent(d["agent"]["name"], d["agent"].get("params"), config.base).run, (lambda: None)
    agent = SandboxedAgent(config, role, records=records, work_root=work_root)
    return agent, agent.close


class ToolRunner:
    """Runs a declared tool (a template's or a task's) in the `tool` role's sandbox: its code and the template's
    files read-only, the adapter it acts on read-write, nothing else."""

    def __init__(self, config, *, records: Path | None, work_root: Path):
        self.work_root = Path(work_root)
        self.box = RoleSandbox(config, "tool", records=records, masked=run_secrets(config),
                               probe_absent=[Path.home() / ".ssh", Path.home() / ".bashrc"])
        self.enabled = self.box.enabled

    def __call__(self, entrypoint: str, base: Path, arguments: dict, bound: dict) -> object:
        work = self.work_root / f"tool-{uuid.uuid4().hex[:8]}"
        home = work / "home"
        home.mkdir(parents=True)
        adapter, root = bound.get("_adapter"), bound.get("_root")
        job = {"kind": "tool", "entrypoint": entrypoint, "base": str(base), "arguments": arguments,
               "bound": {k: str(v) for k, v in bound.items()}}
        try:
            payload = _run_worker(self.box.sandbox, job, work, home=home,
                                  readable=[p for p in (Path(base), root) if p is not None],
                                  writable=[adapter] if adapter is not None else [])
        finally:
            shutil.rmtree(work, ignore_errors=True)
        return payload["result"]

    def close(self) -> None:
        self.box.close()
