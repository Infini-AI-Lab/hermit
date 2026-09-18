#!/usr/bin/env python3
"""Run one bounded Codex integration trial in a generated demo workspace.

Uses existing Codex login. Does not change global config or copy credentials.
This command DOES invoke the user's configured Codex model.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT / ".demo/codex")
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    info = json.loads((workspace / ".ha-demo-generated.json").read_text())
    output = workspace / "runs"
    output.mkdir(exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="codex-", dir=output))
    # Preserve model choice, but omit unrelated global MCP servers and plugins.
    config_path = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "config.toml"
    user = tomllib.loads(config_path.read_text()) if config_path.exists() else {}
    if user.get("model_provider", "openai") != "openai":
        raise SystemExit("This demo runner expects the existing OpenAI/Codex login; use project config with your own provider instead.")
    command = ["codex", "--ask-for-approval", "never", "exec", "--ignore-user-config",
               "--skip-git-repo-check", "--sandbox", "workspace-write", "--json", "--cd", str(workspace),
               "--output-last-message", str(run / "answer.md")]

    def setting(key, value):
        command.extend(["-c", f"{key}={json.dumps(value, ensure_ascii=False)}"])

    for key in ["model", "model_reasoning_effort"]:
        if key in user:
            setting(key, user[key])
    setting(f'projects.{json.dumps(str(workspace))}.trust_level', "trusted")
    setting("agents.enabled", True)
    setting("agents.max_concurrent_threads_per_session", 1)
    for key, value in info["server"].items():
        if key == "args":
            value = list(value)
            value[value.index("--audit-log") + 1] = str(run / "tool-calls.jsonl")
        setting("mcp_servers." + info.get("server_name", "ha_demo") + "." + key, value)
    for name, path in info["roles"].items():
        role = tomllib.loads(Path(path).read_text())
        setting(f"agents.{name}.description", role["description"])
        setting(f"agents.{name}.config_file", path)
    command.append("-")
    (run / "invocation.json").write_text(json.dumps({"argv": command, "timeout": args.timeout},
                                                   ensure_ascii=False, indent=2) + "\n")
    if args.dry_run:
        print(json.dumps({"run": str(run), "argv": command}, ensure_ascii=False, indent=2))
        return
    print(f"Run directory: {run}", flush=True)
    prompt = (workspace / "PROMPT.md").read_text()
    with (run / "events.jsonl").open("w") as out, (run / "stderr.log").open("w") as err:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=out, stderr=err,
                                   text=True, start_new_session=True)
        try:
            process.communicate(prompt, timeout=args.timeout)
            result = {"exit_code": process.returncode, "timed_out": False}
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            result = {"exit_code": process.returncode, "timed_out": True}
    result["run_directory"] = str(run)
    (run / "process-result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    if result["timed_out"] or result["exit_code"] != 0:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
