"""CL-Bench poker: one hand of an order's stream, played in CL-Bench's own loop by `host.AdapterHost` over the
adapter, inside clbench's environment (poker_runner.py, started by clbench_host.py).

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


class CLBenchPokerAgent(Agent):
    """`agent.name: ../utils/agent/agent.py:CLBenchPokerAgent`"""

    def run(self, task: dict, adapter: Path | None) -> RunOutput:
        if adapter is None:
            raise ValueError("this agent serves an adapter")
        result, log, wall = run_host("poker_runner.py", task, adapter, self.params)
        tokens = result.get("tokens") or {}
        return RunOutput(output=result["delta"], log=log, wall_s=wall,
                         tokens=(tokens.get("input_tokens", 0) + tokens.get("output_tokens", 0)) or None,
                         extra={"reward_bb": result["reward_bb"], "settled_without_host": result["settled_without_host"],
                                "settled_reward_bb": result["settled_reward_bb"], "host_tokens": tokens,
                                "mwm": {"unit": "hands", "number": result["hand_num"], "entry": result["entry"]}})
