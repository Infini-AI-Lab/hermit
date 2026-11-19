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

import json
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


def _step(config: dict, step: str | None) -> dict:
    return (next((s for s in config.get("steps") or [] if str(s.get("name")) == str(step)), None) or {}) if step \
        else {}


def verifier_environment(config: dict, step: str | None = None) -> tuple[str, dict | None]:
    """(mode, the verifier's own environment or None) as Harbor resolves them (harbor/models/task/verifier_mode.py):
    a level's explicit environment_mode, else `separate` when it defines an environment; the step's level before the
    task's; `shared` when neither says. Separate: the step's environment, else the task verifier's, else [environment]
    again; shared: none (the verifier runs in the agent's environment)."""
    def level(verifier: dict):
        if verifier.get("environment_mode"):
            return str(verifier["environment_mode"])
        return "separate" if verifier.get("environment") is not None else None
    step_v, task_v = (_step(config, step).get("verifier") or {}), (config.get("verifier") or {})
    mode = (level(step_v) if step else None) or level(task_v) or "shared"
    if mode != "separate":
        return mode, None
    for env in (step_v.get("environment") if step else None, task_v.get("environment")):
        if env is not None:
            return mode, env
    return mode, dict(config.get("environment") or {})


def network_modes(config: dict, step: str | None = None) -> dict[str, str]:
    """{"agent": mode, "verifier": mode} of a task (or of its step `step`), as Harbor resolves them
    (harbor/trial/network_policy.py): a phase's own `network_mode` ([agent], [verifier], a step's first), else the
    environment's baseline (the verifier's own environment's when it has one); modes public, no-network, allowlist."""
    step_cfg = next((s for s in config.get("steps") or [] if str(s.get("name")) == str(step)), None) if step else None
    step_cfg = step_cfg or {}

    def explicit(role: str):
        return ((step_cfg.get(role) or {}).get("network_mode")) or ((config.get(role) or {}).get("network_mode"))
    base = _baseline(config.get("environment"))
    verifier_env = verifier_environment(config, step)[1]
    return {"agent": str(explicit("agent") or base),
            "verifier": str(explicit("verifier") or (_baseline(verifier_env) if verifier_env is not None else base))}


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
    verifier_env = verifier_environment(config, step)[1]
    verifier_base = list(verifier_env.get("allowed_hosts") or []) if verifier_env is not None else base
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


def toml_value(value) -> str:
    """A value as TOML writes it, read back by tomllib as the same value (key order kept): a string (JSON's escapes
    are TOML's; DEL escaped too), a boolean, an integer, a float, a list, a mapping (an inline table). TOML has no
    null: None is refused."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return "nan" if value != value else ("inf" if value > 0 else "-inf") if value in (float("inf"), float("-inf")) \
            else repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007F")
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{toml_key(k)} = {toml_value(v)}" for k, v in value.items()) + "}"
    raise ValueError(f"TOML cannot hold {value!r}")


def toml_key(key) -> str:
    """A key as TOML writes it: bare when it can be, else quoted."""
    key = str(key)
    return key if key and all(c.isascii() and (c.isalnum() or c in "-_") for c in key) else toml_value(key)


def write_task(task_dir: Path, instruction: str, *, metadata: dict | None = None, **sections: dict) -> Path:
    """One task directory in Harbor's format: instruction.md (the instruction, as rows_of reads it back: trimmed, so
    it must be trimmed already) and task.toml (`version`, [metadata], then each of `sections` as a table:
    environment={"network_mode": "no-network"}, agent=..., verifier=...). tasks_of reads it back with every value as
    given, in its order; a value TOML cannot hold (None) is refused."""
    if instruction != instruction.strip():
        raise ValueError(f"{task_dir}: the instruction is read back trimmed; trim it first")
    lines = ['version = "1.0"']
    for name, table in {"metadata": metadata or {}, **sections}.items():
        if not table:
            continue
        lines += ["", f"[{name}]"]
        for key, value in table.items():
            try:
                lines.append(f"{toml_key(key)} = {toml_value(value)}")
            except ValueError as exc:
                raise ValueError(f"{task_dir}: [{name}] {key}: {exc}") from None
    task_dir = Path(task_dir)
    task_dir.mkdir(parents=True, exist_ok=True)
    with open(task_dir / "instruction.md", "w", encoding="utf-8", newline="") as f:     # its line endings kept
        f.write(instruction + "\n")
    (task_dir / TASK_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return task_dir


VERIFIER_KEYS = ("environment_mode", "timeout_sec", "env")    # what the judge reads of [verifier]
VERIFIER_TIMEOUT_SEC = 600.0                                     # Harbor's default [verifier] timeout_sec


def tasks_of(tasks_dir: Path, *, rollouts: int = 1, view: Callable[[dict], dict] | None = None) -> dict[str, Task]:
    """Every task under `tasks_dir` (one directory is a set of one), by id: each row's public half (or `view(row)`:
    the row the task set's agents and learners read, `data.view`), and its private half naming its directory, its
    tests (the task's, and a step's own laid over them), its verifier's settings, and `needs`: what it asks of its
    environment (network modes, allowlist hosts, memory, its agent's timeout), applied to each call."""
    out: dict[str, Task] = {}
    for task_dir in task_dirs(tasks_dir):
        for row in rows_of(task_dir, rollouts=rollouts):
            if row["id"] in out:
                raise ValueError(f"two tasks under {tasks_dir} have the id {row['id']!r}")
            step = row.get("step")
            # The tests as Harbor shows them: the task's, a step's own laid over them (its test.sh first)
            tests, step_tests = task_dir / "tests", (task_dir / "steps" / step / "tests") if step else None
            config = task_config(task_dir)
            step_cfg = _step(config, step)
            verifier = {**(config.get("verifier") or {}), **(step_cfg.get("verifier") or {})}   # a step's own first
            verifier.setdefault("timeout_sec", VERIFIER_TIMEOUT_SEC)
            verifier["environment_mode"] = verifier_environment(config, step)[0]
            verifier["env"] = {**((config.get("verifier") or {}).get("env") or {}),
                               **((step_cfg.get("verifier") or {}).get("env") or {})}
            private = {"task_dir": str(task_dir.resolve()), "tests": str(tests.resolve()) if tests.is_dir() else None,
                       "step_tests": str(step_tests.resolve()) if step_tests and step_tests.is_dir() else None,
                       "verifier": {k: v for k, v in verifier.items() if k in VERIFIER_KEYS}}
            if private["tests"] is None and private["step_tests"]:
                private["tests"], private["step_tests"] = private["step_tests"], None
            verifier_env = verifier_environment(config, step)[1]
            private["needs"] = {"network": row["network"], "allowed_hosts": allowed_hosts(config, step),
                                "memory_mb": row.get("memory_mb"), "agent_timeout_sec": row.get("agent_timeout_sec"),
                                "verifier_memory_mb": resources({"environment": verifier_env})["memory_mb"]
                                if verifier_env is not None else None,
                                "steps": row.get("steps")}
            out[row["id"]] = Task(id=row["id"], public=view(row) if view else row, private=private)
    if not out:
        raise ValueError(f"no task directories (with {TASK_FILE}) under {tasks_dir}")
    return out
