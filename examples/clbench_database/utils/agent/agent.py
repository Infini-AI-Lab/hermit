"""CL-Bench database: one question of an order's stream, played in CL-Bench's own loop by `host.AdapterHost` over
the adapter, inside clbench's environment (database_runner.py, started by clbench_host.py).

Settings come from the environment the launcher prepares (MWM_TASK_WORKDIR, MWM_CLBENCH, MWM_HOST_MODEL);
`agent.params` may name `model`, `clbench` and `workdir` instead.
"""
from __future__ import annotations

from pathlib import Path
import sys

from hermit.agents import Agent
from hermit.task import RunOutput

sys.path.insert(0, str(Path(__file__).resolve().parent))
from clbench_host import run_host  # noqa: E402


class CLBenchDatabaseAgent(Agent):
    """`agent.name: ../utils/agent/agent.py:CLBenchDatabaseAgent`"""

    def run(self, task: dict, adapter: Path | None) -> RunOutput:
        if adapter is None:
            raise ValueError("this agent serves an adapter")
        result, log, wall = run_host("database_runner.py", task, adapter, self.params)
        tokens = result.get("tokens") or {}
        extra = {"success": result["success"], "verdict": result["verdict"],
                 "queries_used": result["queries_used"], "drifted": result["drifted"], "host_tokens": tokens,
                 "mwm": {"unit": "questions", "number": result["question_num"], "entry": result["entry"]}}
        if result.get("context_tool") is not None:
            # what the adapter's context tool rendered for the question (the Trainer keeps it as pipeline_extra;
            # the ReHarness Evolver revises exactly the reflections it names)
            extra["context_tool"] = result["context_tool"]
        return RunOutput(output=result["answer"], log=log, wall_s=wall,
                         tokens=(tokens.get("input_tokens", 0) + tokens.get("output_tokens", 0)) or None, extra=extra)
