"""What the framework runs besides the learner, each in a sandbox of its own role (#11): the agent that does a task
(rollout in training, eval on the test split, check's probe) and the tools a template or task declares.

The Trainer, `eval` and `check` used to call the agent in their own process, so the agent, and everything it
started, ran with the framework's access: the task's answers, the run records, the network. Now every call runs in
a worker process (`worker.py`) inside the role's sandbox, and only the worker can hand its result back.

A role's sandbox, from `sandbox` and `sandbox.profiles.<role>` in the run config:

- read-only: the config's directory, the adapter of this call, `sandbox.read` and `profiles.<role>.read`;
- read-write: the agent's own directory (agents keep logs beside their code), this call's work directory (the
  built-in agents' logs and workspaces go there) and `profiles.<role>.write`;
- covered, read-only, wherever they fall in the above: the run's secrets (the task's data and split files, the judge,
  the runs and trees directories, the agent's log directory);
- network: the role's own allowlist proxy for `sandbox.allow`, `profiles.<role>.allow` and the agent's model
  endpoint (its binding); a model served on the loopback is forwarded;
- `profiles.<role>.enabled: false` runs that role as before, in the framework's process.

`sandbox.profiles.<role>.inherit: false` drops the top-level `read`, `allow` and `packages` for that role.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.parse import urlparse
import uuid

from harnessadapter.sandbox import FRAMEWORK, PACKAGE, AllowlistProxy, Sandbox, network_mode
from harnessadapter.task import RunOutput

ROLES = ("learner", "rollout", "eval", "check", "tool")
WORKER = PACKAGE / "worker.py"
PROFILE_KEYS = {"enabled", "inherit", "read", "write", "allow", "packages"}
CLI_HOMES = {"codex": (".codex", "CODEX_HOME", "auth.json"), "claude": (".claude", "CLAUDE_CONFIG_DIR", ".credentials.json")}


def profile(sandbox: dict, role: str) -> dict:
    """`sandbox.profiles.<role>` with its defaults, merged with the top-level lists unless `inherit: false`."""
    raw = dict((sandbox.get("profiles") or {}).get(role) or {})
    unknown = set(raw) - PROFILE_KEYS
    if unknown:
        raise ValueError(f"sandbox.profiles.{role}: unknown keys {sorted(unknown)}")
    inherit = raw.get("inherit", True)
    top = {k: list(sandbox.get(k) or []) for k in ("read", "allow", "packages")} if inherit else \
        {"read": [], "allow": [], "packages": []}
    return {"enabled": bool(sandbox.get("enabled", True)) and bool(raw.get("enabled", True)),
            "read": top["read"] + list(raw.get("read") or []), "write": list(raw.get("write") or []),
            "allow": top["allow"] + list(raw.get("allow") or []),
            "packages": top["packages"] + list(raw.get("packages") or [])}


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


def _agent_code(name: str, base: Path) -> list[Path]:
    """The directory of a task's own agent (file.py:Class or module:Class); a built-in agent is in the package."""
    target = str(name).rpartition(":")[0]
    if not target:
        return []
    if target.endswith(".py"):
        return [(Path(base) / target).resolve().parent]
    import importlib.util
    spec = importlib.util.find_spec(target)
    return [Path(spec.origin).resolve().parent] if spec and spec.origin else []


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
        network, wants_proxy = network_mode(s["network"])
        allow = [*prof["allow"], *allow]
        env_allow = s.get("env")
        if env_allow is not None:
            env_allow = [*env_allow, *[k for k in env_keys if k not in env_allow]]
        self.proxy = AllowlistProxy(allow, log=records / f"network-{role}.jsonl" if records else None) \
            if (self.enabled and network == "isolated" and wants_proxy and allow) else None
        self.sandbox = Sandbox(enabled=self.enabled, network=s["network"], allow=allow,
                               read=[config.path(p) for p in prof["read"]], readable=list(readable),
                               writable=[*writable, *[config.path(p) for p in prof["write"]]],
                               proxy_url=self.proxy.url if self.proxy else None, env_allow=env_allow,
                               enforce=s.get("enforce", "required"), packages=prof["packages"], record_dir=records,
                               probe_absent=list(probe_absent), masked=list(masked), role=role,
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
                keep=(), forward=()) -> dict:
    """Run worker.py in the sandbox with `job`; return its payload, or raise with what it left behind."""
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
    with os.fdopen(read_end, "r") as result:
        data = result.read()
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
        # The agent's own directory is writable: agents keep logs beside their code (examples/officeqa_paper,
        # servelearnbench), and those logs become learning material. The masks go over it read-only, so writing
        # there cannot uncover the data, the judge or the runs.
        self.box = RoleSandbox(config, role, records=records, readable=[self.base],
                               writable=_agent_code(self.name, self.base),
                               allow=allow, env_keys=self.keys, masked=run_secrets(config),
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
        payload = _run_worker(self.box.sandbox, job, work, home=home, readable=[adapter] if adapter else [],
                              env=env, keep=keep, forward=self.forward)
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
        payload = _run_worker(self.box.sandbox, job, work, home=home,
                              readable=[p for p in (Path(base), root) if p is not None],
                              writable=[adapter] if adapter is not None else [])
        shutil.rmtree(work, ignore_errors=True)
        return payload["result"]

    def close(self) -> None:
        self.box.close()
