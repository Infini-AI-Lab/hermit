"""The bubblewrap backend: the sandbox a task's agent (and its judge, and a task's declared tools) runs in, built from
an EnvironmentSpec (docs/developer/sandbox-design.md). An allowlist: it shows the spec's read paths read-only, its
write paths read-write, passes its variables, and reaches the network its mode allows, through an allowlist proxy
where hosts are allowed. A caller adds what is its own (the agent's model hosts and key variable, a login's plan, a
CLI's installation) through the `extra_*` arguments; nothing of an agent or a task is known here.

    box, proxy = task_sandbox(spec, enforce="required", records=..., role="rollout", probe_absent=[...])
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

from hermit.environment.spec import EnvironmentSpec
from hermit.sandbox import AllowlistProxy, Sandbox, gpu_list, network_mode


def task_sandbox(spec: EnvironmentSpec, *, enforce: str, records: Path | None, role: str,
                 probe_absent: Iterable[Path] = (), extra_readable: Iterable[Path] = (),
                 extra_writable: Iterable[Path] = (), extra_env: Iterable[str] = (), extra_hosts: Iterable[str] = (),
                 framework: bool = True, proxy_log: str | None = None) -> tuple[Sandbox, AllowlistProxy | None]:
    """(the sandbox, its allowlist proxy or None). Hosts in `extra_hosts` are reached under any network mode short of
    public (the mode becomes an allowlist that holds them); the proxy logs to records/network-<proxy_log or role>.jsonl.
    The caller closes both."""
    network, allow = spec.network or "no-network", list(spec.allowed_hosts)
    wanted = list(extra_hosts)
    if network != "public" and wanted:
        network, allow = "allowlist", [*allow, *[h for h in wanted if h not in allow]]
    isolated, wants_proxy = network_mode(network)
    proxy = AllowlistProxy(allow, log=records / f"network-{proxy_log or role}.jsonl" if records else None) \
        if (isolated == "isolated" and wants_proxy and allow) else None
    try:
        box = Sandbox(enabled=True, network=network, allow=allow, readable=[*spec.read, *extra_readable],
                      writable=[*spec.write, *extra_writable], proxy_url=proxy.url if proxy else None,
                      env_allow=[*spec.env, *extra_env], enforce=enforce, packages=list(spec.packages),
                      framework=framework, record_dir=records, probe_absent=list(probe_absent),
                      limits=dict(spec.resources), gpu=gpu_list(spec.gpu), role=role)
    except BaseException:
        if proxy is not None:
            proxy.close()
        raise
    return box, proxy
