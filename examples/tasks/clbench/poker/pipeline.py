"""CL-Bench poker pipeline: one hand the host plays in an order's stream, by the native metaworld poker seat over
the adapter in clbench's own environment (host/poker_runner.py; settings in host/launch.py)."""
from __future__ import annotations

from pathlib import Path
import sys

from harnessadapter.task import RunOutput

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))
from clbench_host import run_host  # noqa: E402


def run(task: dict, adapter: Path | None) -> RunOutput:
    if adapter is None:
        raise ValueError("this pipeline serves an adapter")
    result, log, wall = run_host("poker_runner.py", task, adapter)
    tokens = result.get("tokens") or {}
    return RunOutput(output=result["delta"], log=log, wall_s=wall,
                     tokens=(tokens.get("input_tokens", 0) + tokens.get("output_tokens", 0)) or None,
                     extra={"reward_bb": result["reward_bb"], "settled_without_host": result["settled_without_host"],
                            "settled_reward_bb": result["settled_reward_bb"], "host_tokens": tokens,
                            "mwm": {"unit": "hands", "number": result["hand_num"], "entry": result["entry"]}})
