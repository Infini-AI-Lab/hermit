"""CL-Bench database pipeline: one question of an order's stream, played by the native metaworld host seat over the
adapter in clbench's own environment (host/database_runner.py; settings in host/launch.py)."""
from __future__ import annotations

from pathlib import Path
import sys

from harnessadapter.task import RunOutput

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))
from clbench_host import run_host  # noqa: E402


def run(task: dict, adapter: Path | None) -> RunOutput:
    if adapter is None:
        raise ValueError("this pipeline serves an adapter")
    result, log, wall = run_host("database_runner.py", task, adapter)
    tokens = result.get("tokens") or {}
    return RunOutput(output=result["answer"], log=log, wall_s=wall,
                     tokens=(tokens.get("input_tokens", 0) + tokens.get("output_tokens", 0)) or None,
                     extra={"success": result["success"], "verdict": result["verdict"],
                            "queries_used": result["queries_used"], "drifted": result["drifted"],
                            "host_tokens": tokens,
                            "mwm": {"unit": "questions", "number": result["question_num"], "entry": result["entry"]}})
