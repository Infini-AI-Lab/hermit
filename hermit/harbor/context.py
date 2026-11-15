"""An adapter's context for one Harbor task, computed on this machine before the trial: what a host deploys in place
of the context it would render itself, for an adapter whose context is per task (a `context_tool`, or any adapter
that is not plugin-portable), and the whole system text of the answer-only agent.

    python -m hermit.harbor.context --adapter <dir> --task <row.json> --out <result.json> [--query-field <field>]

It runs in a child process with HA_ADAPTER_SANDBOX=1, so its Session runs the adapter's tools in the strict adapter
sandbox (no network but the loopback endpoints the environment names, none of this process's keys) and the adapter's
code is never imported into the runner's process: `Session(adapter).context(query=..., task=row)`, as the acting
agents' workspace computes it (hermit.agent.acting.workspace._context; the query is the row's `context_query` field,
else its instruction). The result file holds the text and the context tool's whole result (`last_context`, which the
rollout records as `extra["context_tool"]`: ReHarness revises what it retrieved).
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

TIMEOUT_S = 900.0                                        # a context tool that renders a library, or retrieves


def compute(adapter: Path, task: dict, out_dir: Path, *, python: str | None = None, enforce: str | None = None,
            query_field: str | None = None, timeout_s: float = TIMEOUT_S) -> tuple[str, dict | None]:
    """(the adapter's context for `task`, the context tool's record or None), computed in a child process under the
    strict adapter sandbox; the row and the result are kept in `out_dir` (context-task.json, context.json)."""
    from hermit.adapter.session import ENV_ENFORCE, ENV_SANDBOX
    from hermit.sandbox import framework_env
    out_dir.mkdir(parents=True, exist_ok=True)
    row, result = out_dir / "context-task.json", out_dir / "context.json"
    row.write_text(json.dumps(task, ensure_ascii=False, default=str), encoding="utf-8")
    result.unlink(missing_ok=True)
    env = framework_env({ENV_SANDBOX: "1", ENV_ENFORCE: enforce or os.environ.get(ENV_ENFORCE, "required")})
    command = [python or sys.executable, "-m", "hermit.harbor.context", "--adapter", str(Path(adapter).resolve()),
               "--task", str(row), "--out", str(result), *(["--query-field", query_field] if query_field else [])]
    try:
        run = subprocess.run(command, capture_output=True, text=True, env=env, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"the adapter's per-task context took longer than {timeout_s:.0f} s") from None
    if run.returncode != 0 or not result.is_file():
        raise RuntimeError(f"the adapter's per-task context failed: {(run.stderr or run.stdout).strip()[-1500:]}")
    data = json.loads(result.read_text(encoding="utf-8"))
    return str(data.get("text") or ""), data.get("last_context")


def main(argv=None) -> int:
    from hermit.adapter.session import Session
    from hermit.agent.acting.workspace import _query
    parser = argparse.ArgumentParser(description="An adapter's context for one task, in the adapter sandbox.")
    parser.add_argument("--adapter", type=Path, required=True)
    parser.add_argument("--task", type=Path, required=True, help="the task's row (JSON)")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--query-field", help="the row's field the context tool retrieves for (context_query)")
    args = parser.parse_args(argv)
    task = json.loads(args.task.read_text(encoding="utf-8"))
    session = Session(args.adapter)
    try:
        text = session.context(query=_query(task, {"context_query": args.query_field}), task=task)
        record = session.last_context
    finally:
        session.close()
    args.out.write_text(json.dumps({"text": text, "last_context": record}, ensure_ascii=False, default=str),
                        encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
