"""The judge of a benchmark run through Harbor: the verifier's reward, as the container wrote it.

    judge:
      path: hermit.harbor.judge        # name: score (the default)

The score is the trial's reward (`/logs/verifier/reward.txt` or `reward.json`'s `reward`, carried in the runner's
output), 0 for a trial that never reached a reward (a timeout, an infra failure, an agent error). The feedback names
the status and, for a failed trial, the tail of the verifier's output, which is what a learner can act on. A
benchmark whose tests write other reward keys keeps them in `output["rewards"]`; this judge reads `reward`.
"""
from __future__ import annotations

from pathlib import Path

TAIL = 1500                                              # characters of the verifier's stdout in the feedback


def score(task: dict, output) -> dict:
    out = output.output if isinstance(output.output, dict) else {}
    status = out.get("status") or "infra"
    reward = out.get("reward")
    if status == "ok" and reward is not None:
        feedback = "passed" if float(reward) >= 1.0 else f"reward {float(reward):g}"
        if float(reward) < 1.0:
            feedback += _verifier_tail(output.log)
        return {"score": float(reward), "feedback": feedback, "status": status, "rewards": out.get("rewards")}
    cause = out.get("failure_cause") or status
    detail = out.get("exception_type") or ""
    feedback = f"{status} ({cause}{': ' + detail if detail else ''})"
    if status == "timeout":
        feedback += "; the agent ran out of its time budget" + (f"; the verifier scored {reward:g}" if reward is not None
                                                                   else "")
    feedback += _verifier_tail(output.log)
    return {"score": 0.0, "feedback": feedback, "status": status, "failure_cause": cause,
            "verifier_reward_raw": reward}


def _verifier_tail(log) -> str:
    """The end of the verifier's stdout from the curated log directory, when there is one."""
    if not log:
        return ""
    path = Path(log) / "verifier" / "test-stdout.txt"
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    if not text:
        return ""
    return "\nverifier output (tail):\n" + (text[-TAIL:] if len(text) > TAIL else text)
