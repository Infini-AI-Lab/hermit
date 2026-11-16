"""The adapter, deployed for the task container.

`stage` prepares the acting CLI's workspace from the adapter exactly as `hermit deploy` would, by the host of that
CLI (`HOSTS`: hermit.hosts.codex, claude_code, pi, opencode, dsh, mini_swe_agent): the context block in the CLI's
context file, the native roles, skills and hooks where the CLI looks for them, the whole adapter copied to
`hermit/<adapter id>/`: the host's own `prepare`, called as Hermit's acting agents call it
(hermit.agent.acting.workspace.deploy), in a child process of this module (`python -m hermit.harbor.staging`) that
renders the adapter's exports in the strict sandbox (HA_ADAPTER_SANDBOX=1: the adapter read-only, no network, none of
this process's keys). What differs from a host deployment, because the workspace goes into a container whose paths
are not this machine's:

- the hooks (Codex's `.codex/hooks.json`, Claude Code's in `.claude/settings.json`) are rewritten with every command
  run from `hermit/<adapter id>/` relative to the session's directory (the task's workdir, where the CLI runs; for
  Claude Code, which runs a hook in the shell's current directory, under `$CLAUDE_PROJECT_DIR`), not from the absolute
  host folder;
- no host starts the adapter's tools (`prepare(serve_tools=False)`): a trial's tools are served from this machine
  (hermit.harbor.tools) and named to the CLI by the launch plan (hermit.harbor.launch), in its home (Claude Code's on
  its command line, `--mcp-config`; mini-swe-agent's shell calls them as the `hermit-tool` command);
- an adapter whose context is per task (a `context_tool`, or one that is not plugin-portable) gets this task's
  context, computed beforehand in the adapter sandbox (hermit.harbor.context) and handed to the host
  (`prepare(context=...)`), as the acting agents' workspace hands it; the context tool's result is kept in the record
  (`context_tool`). Codex's deployment checks the context against the CLI's limit on its AGENTS.md
  (`context_limit`: the session's project_doc_max_bytes).

The answer-only agent (`host` answer) has no workspace: its whole deployment is the adapter's context for the task, the
system text of its one model call (`context.md`).

The staging directory holds `deploy/` (the workspace), `staging.json` (what was deployed) and the context's files.
Without an adapter nothing is staged and the CLI runs without one.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

STAGING_INFO = "staging.json"
CONTEXT_FILE = "context.md"
HOSTS = {"codex": "hermit.hosts.codex", "claude": "hermit.hosts.claude_code", "pi": "hermit.hosts.pi",
         "opencode": "hermit.hosts.opencode", "dsh": "hermit.hosts.dsh",
         "mini-swe-agent": "hermit.hosts.mini_swe_agent"}
HOOK_HOSTS = ("codex", "claude")                         # the CLIs whose hooks a deployment connects natively


def prepare_workspace(adapter: Path, deploy: Path, python: str, enforce: str | None = None, host: str = "codex", *,
                      context_file: Path | None = None, context_limit: int | None = None) -> dict:
    """The host's `prepare` (`HOSTS`) in a child process under the strict adapter sandbox (`main`, run by `python`),
    its tools not served; its JSON report. `context_limit` is Codex's alone."""
    from hermit.adapter.session import ENV_ENFORCE, ENV_SANDBOX
    from hermit.sandbox import framework_env
    env = framework_env({ENV_SANDBOX: "1", ENV_ENFORCE: enforce or os.environ.get(ENV_ENFORCE, "required")})
    flags = [*(["--context-file", str(context_file)] if context_file is not None else []),
             *(["--context-limit", str(int(context_limit))] if host == "codex" and context_limit else [])]
    run = subprocess.run([python, "-m", "hermit.harbor.staging", "--host", host, "--adapter", str(adapter),
                          "--workspace", str(deploy), "--python", python, *flags], capture_output=True, text=True,
                         env=env)
    if run.returncode != 0:
        raise RuntimeError(f"deploying the adapter failed: {run.stderr.strip()[-1500:]}")
    return json.loads(run.stdout)


def deploy_here(host: str, adapter: Path, workspace: Path, python: str, *, context: str | None = None,
                context_limit: int | None = None) -> dict:
    """The host's `prepare` in this process (prepare_workspace's child): the adapter's tools not served (the runner
    serves them, hermit.harbor.tools), the task's context when it is per task, and for Codex the session's limit on
    its AGENTS.md (None: the host's default)."""
    from importlib import import_module
    module = import_module(HOSTS[host])
    extra = {"context_limit": context_limit} if host == "codex" and context_limit else {}
    return module.prepare(Path(adapter), Path(workspace), python, context=context, serve_tools=False, **extra)


def main(argv=None) -> int:
    """`python -m hermit.harbor.staging --host <cli> --adapter <dir> --workspace <dir> --python <interpreter>
    [--context-file <file>] [--context-limit <bytes>]`: deploy_here, its report as JSON on stdout."""
    parser = argparse.ArgumentParser(description="One Harbor trial's adapter deployment (hermit.harbor.staging).")
    parser.add_argument("--host", required=True, choices=sorted(HOSTS))
    parser.add_argument("--adapter", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--context-file", type=Path)
    parser.add_argument("--context-limit", type=int)
    args = parser.parse_args(argv)
    context = args.context_file.read_text(encoding="utf-8") if args.context_file is not None else None
    # the interpreter's own path: resolve() would follow a venv's link out of it, as the hosts' own CLIs keep it
    report = deploy_here(args.host, args.adapter, args.workspace, str(Path(args.python).absolute()), context=context,
                         context_limit=args.context_limit)
    print(json.dumps(report, ensure_ascii=False, default=str))
    return 0


def container_hooks(adapter: Path, manifest: dict, deploy: Path, host: str = "codex") -> bool:
    """Rewrite the deployed hooks for the container: the ones the host's deployment connects (the hook types it runs,
    its HOOK_TYPES: Codex's command hooks, Claude Code's command and prompt hooks; the deployment puts a hook of
    another type in the context), every command run from the adapter's copy by its workspace-relative path (Codex's
    `.codex/hooks.json`; the `hooks` of Claude Code's `.claude/settings.json`, the file's other settings kept). True
    when the adapter has hooks the CLI runs (Codex's and Claude Code's: the other CLIs have none, and their deployment
    names the hooks' files in the context)."""
    from importlib import import_module
    from hermit.adapter.loader import hook_definitions
    from hermit.hosts.inputs import ADAPTER_DIR, hook_commands, native_hooks
    if host not in HOOK_HOSTS:
        return False
    hooks = native_hooks(hook_definitions(adapter, manifest), import_module(HOSTS[host]).HOOK_TYPES)
    relative = hook_commands(hooks, Path(ADAPTER_DIR) / manifest["id"]) if hooks else None
    if host == "claude":
        # Claude Code runs a hook in the shell's current directory, which follows the agent's `cd`: start from the
        # copy under the project's directory, which Claude Code names in $CLAUDE_PROJECT_DIR for its hooks
        if relative is not None:
            relative = _from_project(relative, f"{ADAPTER_DIR}/{manifest['id']}")
        path = deploy / ".claude" / "settings.json"
        settings = json.loads(path.read_text()) if path.is_file() else {}
        if relative is None:
            if settings.pop("hooks", None) is not None:
                path.write_text(json.dumps(settings, indent=2) + "\n")
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({**settings, "hooks": relative["hooks"]}, indent=2) + "\n")
        return True
    path = deploy / ".codex" / "hooks.json"
    if relative is None:
        path.unlink(missing_ok=True)
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(relative, indent=2) + "\n")
    return True


def _from_project(hooks: dict, folder: str) -> dict:
    """The hooks with each command's `cd <folder>` made `cd "${CLAUDE_PROJECT_DIR:-.}"/<folder>`."""
    import shlex
    out = json.loads(json.dumps(hooks))
    plain = f"cd {shlex.quote(folder)} || exit 1\n"
    for groups in out["hooks"].values():
        for hook in (h for g in groups for h in g["hooks"]):
            if hook.get("type") == "command" and hook["command"].startswith(plain):
                hook["command"] = f'cd "${{CLAUDE_PROJECT_DIR:-.}}"/{shlex.quote(folder)} || exit 1\n' + \
                    hook["command"][len(plain):]
    return out


def per_task(manifest: dict) -> bool:
    """Whether the adapter's context is per task: a context tool, or an adapter only Hermit runs (not plugin-portable),
    which no installed workspace holds (`hermit deploy` refuses it)."""
    from hermit.adapter.manifest import PORTABLE
    return bool(manifest.get("context_tool")) or not manifest.get(PORTABLE, True)


def stage(adapter: Path | None, staging: Path, python: str, enforce: str | None = None,
          host: str = "codex", *, task: dict | None = None, context_query: str | None = None,
          context_limit: int | None = None) -> dict | None:
    """The adapter deployed for the container under `staging/deploy/`, for the acting CLI `host` (a HOSTS key, or
    answer: the context alone); what was deployed, or None without an adapter. `task` is the row: an adapter whose
    context is per task gets the task's (`context_query`: the row's field it retrieves for, else its instruction).
    `context_limit` is Codex's cap on its AGENTS.md (project_doc_max_bytes)."""
    if adapter is None:
        return None
    from hermit.adapter.loader import agent_tools, load_manifest
    from hermit.harbor.context import compute
    if host not in HOSTS and host != "answer":
        raise ValueError(f"no host deploys an adapter for {host!r}: one of {', '.join(HOSTS)} or answer")
    adapter = Path(adapter).resolve()
    manifest = load_manifest(adapter)
    tools = sorted(t["name"] for t in agent_tools(manifest))
    out: dict = {"host": host, "adapter_id": manifest["id"], "version": manifest.get("version"),
                 "adapter_dir": "hermit/" + str(manifest["id"]), "tools": tools}
    if host == "answer":
        # one model call, no workspace: the context is the whole deployment (ADAPTER.md, and the context tool's text)
        text, record = compute(adapter, dict(task or {}), staging, python=python, enforce=enforce,
                               query_field=context_query)
        (staging / CONTEXT_FILE).write_text(text, encoding="utf-8")
        out.update(context_file=str(staging / CONTEXT_FILE), context_tool=record, roles=[], skills=[], hooks=False,
                   tools=[], tools_unused=tools, deploy=None)
        (staging / STAGING_INFO).write_text(json.dumps(out, indent=1, default=str) + "\n")
        return out
    context_file, record = None, None
    if per_task(manifest):
        text, record = compute(adapter, dict(task or {}), staging, python=python, enforce=enforce,
                               query_field=context_query)
        context_file = staging / CONTEXT_FILE
        context_file.write_text(text, encoding="utf-8")
    deploy = staging / "deploy"
    info = prepare_workspace(adapter, deploy, python, enforce, host, context_file=context_file,
                             context_limit=context_limit)
    hooks = container_hooks(adapter, manifest, deploy, host)
    out.update(adapter_id=info.get("adapter_id") or manifest["id"], version=info.get("version") or out["version"],
               roles=sorted(info.get("roles") or {}), skills=list(info.get("skills") or []), hooks=hooks,
               patch=info.get("patch"), server_name=info.get("server_name"),
               context_file=str(context_file) if context_file else None, context_tool=record, deploy=str(deploy))
    (staging / STAGING_INFO).write_text(json.dumps(out, indent=1, default=str) + "\n")
    return out


if __name__ == "__main__":
    sys.exit(main())
