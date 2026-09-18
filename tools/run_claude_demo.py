#!/usr/bin/env python3
"""Run a bounded Claude Code integration trial using the existing login and model."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT / ".demo/claude")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--max-budget-usd", type=float, default=2.0)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    info = json.loads((workspace / ".ha-demo-generated.json").read_text())
    if info.get("host") != "claude-code":
        raise SystemExit("Prepare a Claude Code demo workspace first")
    output = workspace / "runs"
    output.mkdir(exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="claude-", dir=output))
    server = dict(info["server"])
    server["args"] = list(server["args"])
    server["args"][server["args"].index("--audit-log") + 1] = str(run / "tool-calls.jsonl")
    mcp_config = run / "mcp.json"
    mcp_name = info.get("server_name", "ha_demo")
    mcp_config.write_text(json.dumps({"mcpServers": {mcp_name: server}}, indent=2) + "\n")
    manifest = json.loads((Path(info["adapter"]) / "adapter.json").read_text())
    allowed = [f"Agent({name})" for name in info["roles"]]
    allowed += [f"mcp__{mcp_name}__{tool['name']}" for tool in manifest["tools"]]
    # Only the requested native role and the three read/calculation tools are preapproved.
    # Shell, editing, web, and other MCP servers are not part of this trial.
    command = ["claude", "-p", "--output-format", "stream-json", "--verbose",
               "--forward-subagent-text", "--strict-mcp-config", "--mcp-config", str(mcp_config),
               "--tools", "Agent", "--allowedTools",
               ",".join(allowed),
               "--permission-mode", "dontAsk", "--permission-prompts", "none",
               "--max-budget-usd", str(args.max_budget_usd),
               "--settings", '{"disableAllHooks":true,"autoMemoryEnabled":false}']
    # Load role files and CLAUDE.md natively from the prepared project; no --agents
    # or --append-system-prompt shortcut substitutes for those interfaces.
    env = dict(os.environ)
    env["ENABLE_TOOL_SEARCH"] = "false"
    (run / "invocation.json").write_text(json.dumps({"argv": command, "cwd": str(workspace),
                                                   "timeout": args.timeout, "tool_search": False}, indent=2) + "\n")
    print(f"Run directory: {run}", flush=True)
    with (run / "events.jsonl").open("w") as out, (run / "stderr.log").open("w") as err:
        process = subprocess.Popen(command, cwd=workspace, stdin=subprocess.PIPE,
                                   stdout=out, stderr=err, text=True, env=env, start_new_session=True)
        try:
            process.communicate((workspace / "PROMPT.md").read_text(), timeout=args.timeout)
            result = {"exit_code": process.returncode, "timed_out": False}
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            result = {"exit_code": process.returncode, "timed_out": True}
    events = [json.loads(line) for line in (run / "events.jsonl").read_text().splitlines() if line.strip()]
    final = next((e for e in reversed(events) if e.get("type") == "result"), {})
    (run / "answer.md").write_text(final.get("result", "") + "\n")
    result.update(run_directory=str(run), result_subtype=final.get("subtype"),
                  is_error=final.get("is_error"), total_cost_usd=final.get("total_cost_usd"))
    (run / "process-result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    if result["timed_out"] or result["exit_code"] != 0 or not final or final.get("is_error"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
