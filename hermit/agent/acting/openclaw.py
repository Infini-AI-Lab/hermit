"""The `openclaw` acting agent: one task run by `openclaw agent exec` in an isolated workspace.

    agent:
      name: openclaw
      params:
        model: openai/gpt-6-astra            # primary model for openclaw agent exec
        fallback: [anthropic/claude-sonnet-4-6] # optional fallback models
        prompt: environment/prompt.md        # prompt template or file ({field} placeholders)
        user: "{question}"                   # user message template
        files: [environment/fixtures]        # optional files copied into workspace
        args: []                             # extra CLI arguments
        env: {}                              # environment variables
        config: {}                           # optional base config dict or path
        code_mode: auto                      # direct | auto | code
        local_model_lean: false              # use reduced tool surface
        price: {input: 1.0, cached_input: 0.5, output: 2.0} # USD per million tokens
        timeout_s: 600                       # process deadline
        grace_s: 15                          # SIGTERM grace period before SIGKILL
        allow_self_learning: false           # default false: skills.workshop.autonomous.mode="off"
        workspaces: .logs/workspaces         # parent directory for per-task workspaces
        keep_workspaces: false
        keep_state: false

Each task runs in its own directory (<workspaces>/<task_id>-<uuid>/):
  ws/                  # --cwd (contains AGENTS.md, skills/, .hermit-deploy.json)
  state/               # --state-dir (retained SQLite store)
  openclaw.run.json    # --config
  artifacts/           # exported trajectory, envelope, stderr, run-meta.json
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import uuid

from hermit.agent.acting.agent import Agent
from hermit.hosts.openclaw import prepare as openclaw_prepare
from hermit.task import RunOutput

OWNED_FLAGS = {
    "--state-dir", "--cwd", "--config", "--isolated", "--auth-env-only",
    "--message-file", "--json", "-m", "--message"
}


def _format(template: str, task: dict) -> str:
    class Fields(dict):
        def __missing__(self, key):
            return "{" + key + "}"
    return template.format_map(Fields({k: v for k, v in task.items() if not isinstance(v, (dict, list))}))


def _prompt(params: dict, base: Path, task: dict) -> str:
    value = params.get("prompt")
    if not value:
        for field in ("prompt", "question", "instruction", "input"):
            if isinstance(task.get(field), str):
                return task[field]
        if params.get("user"):
            return _format(str(params["user"]), task)
        raise ValueError("agent.params.prompt is required when the task has no prompt field")
    path = Path(base) / value
    return _format(path.read_text(encoding="utf-8") if value.endswith((".md", ".txt")) and path.is_file() else value, task)


def _deep_merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def build_run_config(params: dict, ws_dir: Path, adapter: Path | None, python_bin: str) -> dict:
    """Build isolated per-run config for openclaw agent exec."""
    # Base config from file or dict if provided
    base_cfg = {}
    if params.get("config"):
        if isinstance(params["config"], dict):
            base_cfg = copy.deepcopy(params["config"])
        elif isinstance(params["config"], (str, Path)):
            cfg_p = Path(params["config"]).resolve()
            if cfg_p.is_file():
                base_cfg = json.loads(cfg_p.read_text(encoding="utf-8"))

    # Required enforcement: disable self-learning unless explicitly permitted
    required_deny = ["skill_workshop"]
    existing_deny = base_cfg.get("tools", {}).get("deny", [])
    union_deny = list(dict.fromkeys(existing_deny + required_deny))

    enforced = {
        "tools": {
            "deny": union_deny if not params.get("allow_self_learning") else existing_deny
        }
    }
    if not params.get("allow_self_learning"):
        enforced["skills"] = {"workshop": {"autonomous": {"mode": "off"}}}

    # Merge base config with enforced config
    merged = _deep_merge(base_cfg, enforced)

    # If adapter has deployed openclaw.run.json in ws_dir, merge its MCP config
    ws_run_json = ws_dir / "openclaw.run.json"
    if ws_run_json.is_file():
        try:
            ws_cfg = json.loads(ws_run_json.read_text(encoding="utf-8"))
            if "mcp" in ws_cfg:
                merged = _deep_merge(merged, {"mcp": ws_cfg["mcp"]})
        except Exception:
            pass

    return merged


def export_trajectory_from_state(state_dir: Path, session_id: str, ws_dir: Path, artifacts_dir: Path, bin_name: str = "openclaw") -> tuple[Path | None, bool, str | None]:
    """Derive session_key from state DB and call openclaw sessions export-trajectory."""
    sqlite_path = state_dir / "agents" / "main" / "agent" / "openclaw-agent.sqlite"
    if not sqlite_path.is_file():
        return None, True, None

    session_key = None
    try:
        conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
        cur = conn.cursor()
        # Find session_key in session_nodes where current_session_id == session_id
        rows = cur.execute("SELECT session_key FROM session_nodes WHERE current_session_id = ?", (session_id,)).fetchall()
        if rows and rows[0][0]:
            session_key = rows[0][0]
        else:
            # Fallback pattern
            session_key = f"agent:main:explicit:{session_id}"
        conn.close()
    except Exception:
        session_key = f"agent:main:explicit:{session_id}"

    bundle_output_name = f"trajectory-{session_id[:8]}"
    cmd = [
        bin_name, "sessions", "export-trajectory",
        "--session-key", session_key,
        "--store", str(sqlite_path),
        "--agent", "main",
        "--workspace", str(ws_dir),
        "--output", bundle_output_name,
        "--json"
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        export_target = ws_dir / ".openclaw" / "trajectory-exports" / bundle_output_name
        if export_target.is_dir():
            dest = artifacts_dir / "trajectory"
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(export_target, dest, symlinks=True)
            return dest, False, session_key
    except Exception:
        pass
    return None, True, session_key


class OpenClawAgent(Agent):
    def run(self, task: dict, adapter: Path | None) -> RunOutput:
        return _run(task, adapter, self.params, self.base)


def run(task: dict, adapter: Path | None, *, params: dict, base: Path) -> RunOutput:
    """The function form (`agent.name: openclaw`); OpenClawAgent is the class."""
    return _run(task, adapter, params, base)


def _run(task: dict, adapter: Path | None, params: dict, base: Path) -> RunOutput:
    bin_name = str(params.get("bin") or "openclaw")
    task_id = str(task.get("id", "task"))
    run_uuid = uuid.uuid4().hex[:8]

    root = Path(base) / params.get("workspaces", ".logs/workspaces")
    run_dir = root / f"{task_id}-{run_uuid}"
    ws_dir = run_dir / "ws"
    state_dir = run_dir / "state"
    artifacts_dir = run_dir / "artifacts"

    ws_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    # 1. Deploy adapter into ws if adapter given
    if adapter is not None:
        from hermit.adapter.loader import load_manifest
        tool = load_manifest(Path(adapter)).get("context_tool")
        if tool:
            raise ValueError(
                f"agent openclaw: OpenClawAgent deploys the adapter as `hermit deploy --host openclaw` does, "
                f"and this adapter declares a context_tool ({tool}) whose per-task context would never reach the agent"
            )
        openclaw_prepare(Path(adapter), ws_dir, sys.executable)

    # 2. Copy extra files if configured
    for item in params.get("files") or []:
        source = Path(base) / item
        target = ws_dir / source.name
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True)
        elif source.is_file():
            shutil.copy2(source, target)

    # 3. Build per-run config
    run_config = build_run_config(params, ws_dir, adapter, sys.executable)
    config_path = run_dir / "openclaw.run.json"
    config_bytes = json.dumps(run_config, indent=2, sort_keys=True).encode("utf-8")
    config_path.write_bytes(config_bytes)
    config_hash = hashlib.sha256(config_bytes).hexdigest()

    # 4. Prepare message file
    prompt_text = _prompt(params, base, task)
    msg_file = artifacts_dir / "task.md"
    msg_file.write_text(prompt_text, encoding="utf-8")

    # 5. Build command line
    cmd = [
        bin_name, "agent", "exec",
        "--cwd", str(ws_dir),
        "--config", str(config_path),
        "--state-dir", str(state_dir),
        "--message-file", str(msg_file),
        "--json"
    ]
    if params.get("model"):
        model_val = params["model"]
        if isinstance(model_val, dict):
            model_val = model_val.get("ref") or model_val.get("model")
        if model_val:
            cmd.extend(["--model", str(model_val)])
    for fb in params.get("fallback") or []:
        cmd.extend(["--fallback", str(fb)])
    if params.get("code_mode"):
        cmd.extend(["--code-mode", str(params["code_mode"])])
    if params.get("local_model_lean"):
        cmd.append("--local-model-lean")

    # User extra args (filtering out owned flags)
    extra_args = list(params.get("args") or [])
    skip_next = False
    for a in extra_args:
        if skip_next:
            skip_next = False
            continue
        if a in OWNED_FLAGS:
            skip_next = True
            continue
        if any(a.startswith(f"{f}=") for f in OWNED_FLAGS):
            continue
        cmd.append(str(a))

    # Environment
    env = dict(os.environ)
    for k in list(env.keys()):
        if k.startswith("OPENCLAW_"):
            env.pop(k, None)
    for k, v in (params.get("env") or {}).items():
        env[k] = os.path.expandvars(str(v))

    timeout_s = float(params.get("timeout_s", 600))
    grace_s = float(params.get("grace_s", 15))

    envelope_path = artifacts_dir / "envelope.json"
    stderr_path = artifacts_dir / "stderr.txt"

    started = time.time()
    code = 0
    stdout_data = ""
    stderr_data = ""
    timed_out = False

    try:
        proc = subprocess.Popen(
            cmd, cwd=str(ws_dir), env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True
        )
        try:
            stdout_data, stderr_data = proc.communicate(timeout=timeout_s)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                stdout_data, stderr_data = proc.communicate(timeout=grace_s)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                stdout_data, stderr_data = proc.communicate()
            code = -1
    except FileNotFoundError as exc:
        raise RuntimeError(f"OpenClaw binary not found: {bin_name}") from exc
    except Exception as exc:
        code = 1
        stderr_data = f"Failed to execute {cmd}: {exc}"

    wall_s = round(time.time() - started, 2)
    stderr_path.write_text(stderr_data, encoding="utf-8", errors="replace")

    # Parse envelope
    envelope = {}
    json_valid = False
    if stdout_data.strip():
        # Find JSON block if wrapped
        raw = stdout_data.strip()
        idx_brace = raw.find("{")
        if idx_brace >= 0:
            try:
                envelope = json.loads(raw[idx_brace:])
                json_valid = True
                envelope_path.write_text(json.dumps(envelope, indent=2, sort_keys=True), encoding="utf-8")
            except Exception:
                envelope_path.write_text(raw, encoding="utf-8")
        else:
            envelope_path.write_text(raw, encoding="utf-8")
    else:
        envelope_path.write_text("{}", encoding="utf-8")

    session_id = envelope.get("sessionId") or ""
    status = envelope.get("status") or ("timeout" if timed_out else ("ok" if code == 0 else "error"))
    final_output = envelope.get("final") or ""

    # Answer tag extraction if configured
    tag = params.get("answer_tag")
    if tag and final_output:
        match = re.search(rf"<{tag}>\s*(.*?)\s*</{tag}>", final_output, re.DOTALL | re.IGNORECASE)
        if match:
            final_output = match.group(1).strip()

    # Cost and tokens
    usage = envelope.get("usage") or {}
    cost_usd = envelope.get("costUsd")
    if cost_usd is None and isinstance(usage.get("cost"), dict):
        cost_usd = usage["cost"].get("total")
    price = params.get("price")
    total_tokens = usage.get("total")
    in_tokens = usage.get("input") or usage.get("prompt_tokens") or 0
    out_tokens = usage.get("output") or usage.get("completion_tokens") or 0
    if total_tokens is None and (in_tokens or out_tokens):
        total_tokens = in_tokens + out_tokens

    if (cost_usd is None or cost_usd == 0) and price and (in_tokens or out_tokens):
        p_in = price.get("input", 0.0)
        p_out = price.get("output", 0.0)
        cost_usd = round((in_tokens * p_in + out_tokens * p_out) / 1e6, 6)

    # 6. Trajectory export
    trajectory_dir = None
    log_degraded = False
    session_key = None
    if session_id and not timed_out:
        trajectory_dir, log_degraded, session_key = export_trajectory_from_state(state_dir, session_id, ws_dir, artifacts_dir, bin_name)
    else:
        log_degraded = True

    chosen_log = trajectory_dir if (trajectory_dir and trajectory_dir.is_dir()) else artifacts_dir

    # 7. Write run-meta.json
    run_meta = {
        "agent": "openclaw",
        "task_id": task_id,
        "session_id": session_id,
        "config_sha256": config_hash,
        "adapter": str(adapter) if adapter else None,
        "model": envelope.get("model") or params.get("model"),
        "provider": envelope.get("provider"),
        "exit_code": code,
        "timed_out": timed_out,
        "wall_s": wall_s,
        "argv": cmd
    }
    (artifacts_dir / "run-meta.json").write_text(json.dumps(run_meta, indent=2, sort_keys=True), encoding="utf-8")

    # Clean up state if not keep_state
    if not params.get("keep_state") and state_dir.exists():
        shutil.rmtree(state_dir, ignore_errors=True)

    # Clean up workspace if not keep_workspaces
    if not params.get("keep_workspaces") and ws_dir.exists() and False:
        pass  # keep artifacts and logs intact for Trainer collection

    extra = {
        "status": status,
        "exit_code": code,
        "sessionId": session_id,
        "session_id": session_id,
        "sessionKey": session_key,
        "session_key": session_key,
        "model": envelope.get("model"),
        "provider": envelope.get("provider"),
        "assistantTurns": envelope.get("assistantTurns"),
        "toolSummary": envelope.get("toolSummary"),
        "error": envelope.get("error"),
        "config_sha256": config_hash,
        "adapter_ref": str(adapter) if adapter else None,
        "log_degraded": log_degraded,
        "workspace": str(ws_dir),
        "artifacts": str(artifacts_dir)
    }

    return RunOutput(
        output=final_output,
        log=chosen_log,
        cost_usd=cost_usd,
        wall_s=wall_s,
        tokens=total_tokens,
        api_speed={"assistant_turns": envelope.get("assistantTurns")} if envelope.get("assistantTurns") else None,
        extra=extra
    )
