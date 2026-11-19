"""Tasks as directories in Harbor's format (https://harborframework.com/docs/tasks), read without Harbor:

    <task>/
      task.toml          [metadata], [agent] timeout_sec, [verifier] timeout_sec / environment_mode / env,
                         [environment] resources and network (network_mode, allowed_hosts, allow_internet)
      instruction.md     what the agent is asked
      environment/       what the task needs of its environment (a Dockerfile for a container; a task Hermit runs in
                         its own sandbox may have none)
      tests/test.sh      the verifier: it writes /logs/verifier/reward.txt (a number) or reward.json (named numbers)

A multi-step task adds `[[steps]]` to task.toml and `steps/<name>/{instruction.md, tests/}`. A task's row is its
public half (the instruction, its timeouts, resources, network modes and metadata, and `harbor_task`, the
directory's name); where its tests and its directory are is the private half (hermit.task.task), which only the
verifier reads.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable
import tomllib

from hermit.task.task import Task

TASK_FILE = "task.toml"


def is_task(path: Path) -> bool:
    return path.is_dir() and (path / TASK_FILE).is_file()


def task_dirs(tasks_dir: Path) -> list[Path]:
    """The task directories under `tasks_dir`, by name."""
    tasks_dir = Path(tasks_dir)
    if is_task(tasks_dir):
        return [tasks_dir]
    return sorted(p for p in tasks_dir.iterdir() if is_task(p)) if tasks_dir.is_dir() else []


def task_config(task_dir: Path) -> dict:
    return tomllib.loads((Path(task_dir) / TASK_FILE).read_text())


def step_names(config: dict) -> list[str]:
    return [str(s["name"]) for s in (config.get("steps") or [])]


def _baseline(env) -> str:
    """An environment's network mode (Harbor's baseline): its network_mode, else the deprecated allow_internet (true:
    public, false: no-network), else public."""
    env = env if isinstance(env, dict) else {}
    if env.get("network_mode"):
        return str(env["network_mode"])
    if env.get("allow_internet") is not None and env.get("allowed_hosts") is None:
        return "public" if env["allow_internet"] else "no-network"
    return "public"


def network_modes(config: dict, step: str | None = None) -> dict[str, str]:
    """{"agent": mode, "verifier": mode} of a task (or of its step `step`), as Harbor resolves them
    (harbor/trial/network_policy.py): a phase's own `network_mode` ([agent], [verifier], a step's first), else the
    environment's baseline (the verifier's own environment's when it has one); modes public, no-network, allowlist."""
    step_cfg = next((s for s in config.get("steps") or [] if str(s.get("name")) == str(step)), None) if step else None
    step_cfg = step_cfg or {}

    def explicit(role: str):
        return ((step_cfg.get(role) or {}).get("network_mode")) or ((config.get(role) or {}).get("network_mode"))
    base = _baseline(config.get("environment"))
    verifier_env = ((step_cfg.get("verifier") or {}).get("environment")
                    or (config.get("verifier") or {}).get("environment"))
    return {"agent": str(explicit("agent") or base),
            "verifier": str(explicit("verifier") or (_baseline(verifier_env) if verifier_env else base))}


def allowed_hosts(config: dict, step: str | None = None) -> dict[str, list[str]]:
    """{"agent": hosts, "verifier": hosts}: the hosts each phase's allowlist names, resolved as network_modes resolves
    the modes (a phase's own policy, a step's first, else the baseline: the verifier's own environment's when it has
    one, else [environment]'s)."""
    step_cfg = next((s for s in config.get("steps") or [] if str(s.get("name")) == str(step)), None) if step else None
    step_cfg = step_cfg or {}

    def own(role: str):
        for section in (step_cfg.get(role) or {}, config.get(role) or {}):
            if section.get("network_mode"):
                return list(section.get("allowed_hosts") or [])
        return None
    base = list((config.get("environment") or {}).get("allowed_hosts") or [])
    verifier_env = ((step_cfg.get("verifier") or {}).get("environment")
                    or (config.get("verifier") or {}).get("environment"))
    verifier_base = list((verifier_env or {}).get("allowed_hosts") or []) if verifier_env else base
    agent, verifier = own("agent"), own("verifier")
    return {"agent": base if agent is None else agent, "verifier": verifier_base if verifier is None else verifier}


def _size_mb(value) -> int | None:
    """A size as Harbor reads the deprecated `memory`/`storage` fields ("2G", "512M"), in MB; None when unreadable."""
    text = str(value).strip().upper()
    try:
        if text.endswith("G"):
            return int(float(text[:-1]) * 1024)
        if text.endswith("M"):
            return int(float(text[:-1]))
        if text.endswith("K"):
            return int(float(text[:-1]) / 1024)
    except ValueError:
        return None
    return None


def resources(config: dict) -> dict:
    """The task's resources ([environment]): cpus, memory_mb, storage_mb, gpus (None where it sets none)."""
    env = config.get("environment") or {}
    memory = env.get("memory_mb") if env.get("memory_mb") is not None else \
        (_size_mb(env["memory"]) if env.get("memory") is not None else None)
    storage = env.get("storage_mb") if env.get("storage_mb") is not None else \
        (_size_mb(env["storage"]) if env.get("storage") is not None else None)
    return {"cpus": env.get("cpus"), "memory_mb": memory, "storage_mb": storage, "gpus": env.get("gpus")}


def _instruction(path: Path) -> str:
    """The instruction as the file holds it (its line endings kept: a `\r\n` in a question is part of it), trimmed."""
    try:
        with open(path, encoding="utf-8", newline="") as f:
            return f.read().strip()
    except OSError:
        return ""


def rows_of(task_dir: Path, *, rollouts: int = 1) -> list[dict]:
    """The Hermit rows of one task: by rollout, and by step for a multi-step task."""
    task_dir = Path(task_dir)
    config = task_config(task_dir)
    env, agent = (config.get("environment") or {}), (config.get("agent") or {})
    verifier = config.get("verifier") or {}
    base = {"harbor_task": task_dir.name, "agent_timeout_sec": agent.get("timeout_sec"),
            "verifier_timeout_sec": verifier.get("timeout_sec"), "build_timeout_sec": env.get("build_timeout_sec"),
            "docker_image": env.get("docker_image"), "metadata": config.get("metadata") or {}, **resources(config),
            "network": network_modes(config)}
    steps = step_names(config)
    out = []
    for k in range(1, rollouts + 1):
        suffix = f"#{k}" if rollouts > 1 else ""
        if not steps:
            out.append({"id": f"{task_dir.name}{suffix}", **base, "rollout": k,
                        "instruction": _instruction(task_dir / "instruction.md")})
            continue
        for i, step in enumerate(steps, start=1):
            step_cfg = next(s for s in config["steps"] if str(s["name"]) == step)
            out.append({"id": f"{task_dir.name}:{step}{suffix}", **base, "rollout": k, "step": step, "step_index": i,
                        "steps": len(steps), "instruction": _instruction(task_dir / "steps" / step / "instruction.md"),
                        "network": network_modes(config, step),
                        "agent_timeout_sec": ((step_cfg.get("agent") or {}).get("timeout_sec")
                                              or base["agent_timeout_sec"]),
                        "verifier_timeout_sec": ((step_cfg.get("verifier") or {}).get("timeout_sec")
                                                 or base["verifier_timeout_sec"])})
    return out


VERIFIER_KEYS = ("environment_mode", "timeout_sec", "env")    # what the judge reads of [verifier]
VERIFIER_TIMEOUT_SEC = 600.0                                     # Harbor's default [verifier] timeout_sec


def tasks_of(tasks_dir: Path, *, rollouts: int = 1, view: Callable[[dict], dict] | None = None) -> dict[str, Task]:
    """Every task under `tasks_dir` (one directory is a set of one), by id: each row's public half (or `view(row)`:
    the row the task set's agents and learners read, `data.view`), and its private half naming its directory, its
    tests (a step's own tests where it has them, else the task's), its verifier's settings, and `needs`: what it asks
    of its environment (network modes, allowlist hosts, memory), checked before each call."""
    out: dict[str, Task] = {}
    for task_dir in task_dirs(tasks_dir):
        for row in rows_of(task_dir, rollouts=rollouts):
            if row["id"] in out:
                raise ValueError(f"two tasks under {tasks_dir} have the id {row['id']!r}")
            step = row.get("step")
            tests = task_dir / "steps" / step / "tests" if step and (task_dir / "steps" / step / "tests").is_dir() \
                else task_dir / "tests"
            config = task_config(task_dir)
            step_cfg = next((s for s in config.get("steps") or [] if str(s.get("name")) == str(step)), {}) \
                if step else {}
            verifier = {**(config.get("verifier") or {}), **(step_cfg.get("verifier") or {})}   # a step's own first
            verifier.setdefault("timeout_sec", VERIFIER_TIMEOUT_SEC)
            if verifier.get("environment") and not verifier.get("environment_mode"):
                verifier["environment_mode"] = "separate"   # an environment of its own implies it (Harbor)
            verifier["env"] = {**((config.get("verifier") or {}).get("env") or {}),
                               **((step_cfg.get("verifier") or {}).get("env") or {})}
            private = {"task_dir": str(task_dir.resolve()), "tests": str(tests.resolve()) if tests.is_dir() else None,
                       "verifier": {k: v for k, v in verifier.items() if k in VERIFIER_KEYS}}
            private["needs"] = {"network": row["network"], "allowed_hosts": allowed_hosts(config, step),
                                "memory_mb": row.get("memory_mb")}
            out[row["id"]] = Task(id=row["id"], public=view(row) if view else row, private=private)
    if not out:
        raise ValueError(f"no task directories (with {TASK_FILE}) under {tasks_dir}")
    return out
