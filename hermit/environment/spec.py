"""What an environment must give: the paths it shows read-only and read-write, the variables that pass, the network,
the Python distributions it shows, its resources and GPUs, or a container image. A run config's `environment`
section is one; a task directory's task.toml asks for some of it ([environment] network_mode, memory_mb), which the
run's must cover: a task is never run with less than it asks, nor given more than the run declares.

    spec = EnvironmentSpec.from_section(config.data["environment"], resolve)   # resolve(where, values) -> [Path]
    spec.problems_for(task_row)                                                # [] when the run's covers the task
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

NETWORK_ORDER = {"no-network": 0, "allowlist": 1, "public": 2}     # how much each network mode lets through


@dataclass(frozen=True)
class EnvironmentSpec:
    read: tuple[Path, ...] = ()
    write: tuple[Path, ...] = ()
    env: tuple[str, ...] = ()
    network: str = "no-network"
    allowed_hosts: tuple[str, ...] = ()
    packages: tuple[str, ...] = ()
    resources: Mapping[str, Any] = field(default_factory=dict)
    gpu: Any = None
    image: str | None = None
    container: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_section(cls, section: Mapping[str, Any], resolve: Callable[[str, Any], list[Path]]) -> "EnvironmentSpec":
        """A run config's `environment` section; `resolve(where, values)` turns its paths into paths (the config's
        own resolution, which refuses an unset variable)."""
        s = dict(section or {})
        return cls(read=tuple(resolve("environment.read", s.get("read"))),
                   write=tuple(resolve("environment.write", s.get("write"))),
                   env=tuple(s.get("env") or ()), network=s.get("network") or "no-network",
                   allowed_hosts=tuple(s.get("allowed_hosts") or ()), packages=tuple(s.get("packages") or ()),
                   resources=dict(s.get("resources") or {}), gpu=s.get("gpu"), image=s.get("image"),
                   container=dict(s.get("container") or {}))

    def problems_for(self, task: Mapping[str, Any], *, verifier: bool = False) -> list[str]:
        """What a task asks of its environment (a directory task's row: `network`, `allowed_hosts`, `memory_mb`) that
        this one does not give: a wider network than the run declares (for its verifier too when `verifier`: a task's
        own verifier runs in a sandbox like the agent's), hosts its allowlist lacks, more memory than its limit. A row
        that asks nothing (a row of tasks.jsonl) has none."""
        out = []
        modes = task.get("network") if isinstance(task.get("network"), Mapping) else {}
        for phase in ("agent", "verifier") if verifier else ("agent",):
            asked = modes.get(phase)
            if asked in NETWORK_ORDER and NETWORK_ORDER[asked] > NETWORK_ORDER.get(self.network, 0):
                out.append(f"the task's {phase} needs network {asked}; the run's environment.network is "
                           f"{self.network}")
            elif asked == "allowlist":
                hosts = task.get("allowed_hosts") or {}
                hosts = list(hosts.get(phase) or []) if isinstance(hosts, Mapping) else list(hosts)
                wild = [h for h in hosts if "*" in h]
                missing = [h for h in hosts if h not in self.allowed_hosts and h not in wild]
                if wild and self.network == "allowlist":     # its proxy matches a host as written
                    out.append(f"the task's {phase} allows {wild}: wildcard hosts are not supported; name the hosts")
                if missing and self.network == "allowlist":
                    out.append(f"the task's {phase} needs to reach {missing}, which environment.allowed_hosts lacks")
        if task.get("steps"):                            # each step would start where the last one left nothing
            out.append(f"the task has {task['steps']} steps, which run in one environment one after another: a "
                       "multi-step task is not supported here yet")
        limit = self.resources.get("memory_mb")
        if verifier and limit and task.get("verifier_memory_mb") and int(task["verifier_memory_mb"]) > int(limit):
            out.append(f"the task's verifier needs {task['verifier_memory_mb']} MB of memory; "
                       f"environment.resources.memory_mb is {limit}")
        if limit and task.get("memory_mb") and int(task["memory_mb"]) > int(limit):
            out.append(f"the task needs {task['memory_mb']} MB of memory; environment.resources.memory_mb is {limit}")
        return out
