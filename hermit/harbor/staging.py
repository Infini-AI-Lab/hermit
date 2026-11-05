"""The adapter, deployed for the task container.

`stage` prepares a Codex workspace from the adapter exactly as `hermit deploy` would (`hermit.hosts.codex.prepare`:
the AGENTS.md context block, `.codex/config.toml` with the native roles, `.codex/agents/*.toml`, `.codex/skills/`,
the whole adapter copied to `hermit/<adapter id>/`), in a child process that renders the adapter's exports in the
strict sandbox (HA_ADAPTER_SANDBOX=1: the adapter read-only, no network, none of this process's keys). Two things
differ from a host deployment, because the workspace goes into a container whose paths are not this machine's:

- `.codex/hooks.json` is rewritten with every command run from `hermit/<adapter id>/` relative to the session's
  directory (the task's workdir, where Codex runs), not from the absolute host folder;
- the MCP server of the adapter's tools is not started in the container (no Hermit there); the container-side agent
  drops `mcp_servers` from the config it uploads and says so in its log. An adapter whose context comes from a
  `context_tool` is refused, as the built-in CLI agents refuse it: that per-task context would never reach the agent.

The staging directory holds `deploy/` (the workspace) and `staging.json` (what was deployed). Without an adapter
nothing is staged and Harbor's agent runs as installed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

STAGING_INFO = "staging.json"


def prepare_workspace(adapter: Path, deploy: Path, python: str, enforce: str | None = None) -> dict:
    """`hermit.hosts.codex.prepare` in a child process under the strict adapter sandbox; its JSON report."""
    from hermit.adapter.session import ENV_ENFORCE, ENV_SANDBOX
    from hermit.sandbox import framework_env
    env = framework_env({ENV_SANDBOX: "1", ENV_ENFORCE: enforce or os.environ.get(ENV_ENFORCE, "required")})
    run = subprocess.run([python, "-m", "hermit.hosts.codex", "--adapter", str(adapter), "--workspace", str(deploy),
                          "--python", python], capture_output=True, text=True, env=env)
    if run.returncode != 0:
        raise RuntimeError(f"deploying the adapter failed: {run.stderr.strip()[-1500:]}")
    return json.loads(run.stdout)


def container_hooks(adapter: Path, manifest: dict, deploy: Path) -> bool:
    """Rewrite the deployed hooks file for the container: every command runs from the adapter's copy by its
    workspace-relative path. True when the adapter has hooks."""
    from hermit.adapter.loader import hook_definitions
    from hermit.hosts.inputs import ADAPTER_DIR, hook_commands
    hooks = hook_definitions(adapter, manifest)
    path = deploy / ".codex" / "hooks.json"
    if not hooks:
        path.unlink(missing_ok=True)
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(hook_commands(hooks, Path(ADAPTER_DIR) / manifest["id"]), indent=2) + "\n")
    return True


def stage(adapter: Path | None, staging: Path, python: str, enforce: str | None = None) -> dict | None:
    """The adapter deployed for the container under `staging/deploy/`; what was deployed, or None without an
    adapter."""
    if adapter is None:
        return None
    from hermit.adapter.loader import agent_tools, load_manifest
    adapter = Path(adapter).resolve()
    manifest = load_manifest(adapter)
    tool = manifest.get("context_tool")
    if tool:
        raise ValueError(f"the harbor agent deploys the adapter as `hermit deploy` does, and this adapter declares a "
                         f"context_tool ({tool}) whose per-task context would never reach the agent in the container")
    deploy = staging / "deploy"
    info = prepare_workspace(adapter, deploy, python, enforce)
    hooks = container_hooks(adapter, manifest, deploy)
    out = {"adapter_id": info.get("adapter_id"), "version": info.get("version"), "roles": sorted(info.get("roles") or {}),
           "skills": list(info.get("skills") or []), "hooks": hooks, "adapter_dir": "hermit/" + str(manifest["id"]),
           "tools_not_served": sorted(agent_tools(manifest)) if agent_tools(manifest) else [], "deploy": str(deploy)}
    (staging / STAGING_INFO).write_text(json.dumps(out, indent=1) + "\n")
    return out
