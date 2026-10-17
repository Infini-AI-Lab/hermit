"""ServeLearnBench judge: the benchmark's reward, and as feedback the one line its methods are shown.

The verifier ran inside the pipeline (the reward is the episode's). A serving task's feedback is `Score: n/100.`
and nothing else: no correct answer, no category, no window, no explanation (docs/protocol.md of the benchmark).
"""
from __future__ import annotations

from servelearnbench.protocol import score_line


def score(task: dict, output) -> dict:
    extra = output.extra or {}
    reward = float(extra.get("reward") or 0.0)
    return {"score": reward, "feedback": score_line(reward), "wm_calls": extra.get("wm_calls")}
