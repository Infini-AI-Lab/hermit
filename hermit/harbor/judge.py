"""The judge of a benchmark run through Harbor: the verifier's reward, as the container wrote it.

    judge:
      path: hermit.harbor.judge        # name: score (the default)

The score is the trial's reward (`/logs/verifier/reward.txt` or `reward.json`'s `reward_key`, carried in the runner's
output) whenever the verifier gave one, whatever the trial's status: a trial that ran out of the agent's time or
whose agent exited with an error still has its work scored, as Harbor's own job mean scores it. A trial without a
reward scores 0, and so does one that is not the agent's result (infra: the Trainer does not judge one the runner
marks `unscored`). A trial "passed" when its reward reaches the benchmark's pass reward (`agent.params.pass_reward`,
1.0 unless the benchmark sets another; null for a benchmark whose reward is a scale with no pass, where none is said
to pass); `passed` says so (None without a pass reward), which `hermit eval` counts as solved. The feedback names the
status (and, for a context over the CLI's limit, by how much: the session never started) and, for a trial that did
not pass, the tail of the verifier's output (as curated: `verifier_redact` applied), which is what a learner can act
on. The verifier's other reward keys are kept in `rewards`, for every status.
"""
from __future__ import annotations

from pathlib import Path

TAIL = 1500                                              # characters of the verifier's stdout in the feedback
PASS_REWARD = 1.0


def pass_reward(output) -> float | None:
    """The run's pass reward, as the runner gave it (`extra["judge"]["pass_reward"]`), else PASS_REWARD."""
    extra = getattr(output, "extra", None)
    judge = extra.get("judge") if isinstance(extra, dict) else None
    if isinstance(judge, dict) and "pass_reward" in judge:
        return None if judge["pass_reward"] is None else float(judge["pass_reward"])
    return PASS_REWARD


def score(task: dict, output) -> dict:
    out = output.output if isinstance(output.output, dict) else {}
    status = out.get("status") or "infra"
    raw = out.get("reward")
    reward = float(raw) if raw is not None and status != "infra" else None
    bar = pass_reward(output)
    passed = reward is not None and bar is not None and reward >= bar
    cause = out.get("failure_cause") or status
    if status == "ok":
        feedback = "passed" if passed else f"reward {reward:g}" if reward is not None else "no reward"
    else:
        detail = out.get("exception_type") or ""
        feedback = f"{status} ({cause}{': ' + detail if detail else ''})"
        if cause == "solution_timed_out":
            feedback += "; the verifier ran out of its time on the agent's solution (too slow to finish its tests)"
        elif cause == "context_over_limit":            # the session never started: harborside's message says why
            extra = getattr(output, "extra", None) or {}
            feedback += "; " + str(extra.get("exception_message") or "").removeprefix("hermit: ")[:600]
        elif status == "timeout":
            feedback += "; the agent ran out of its time budget"
        if reward is not None:
            feedback += "; its work passed the verifier" if passed else f"; the verifier scored its work {reward:g}"
    if not passed:
        feedback += _verifier_tail(output.log)
    record = {"score": reward if reward is not None else 0.0, "feedback": feedback, "status": status,
              "passed": passed if bar is not None else None, "rewards": out.get("rewards")}
    if status != "ok":
        record.update(failure_cause=cause, verifier_reward_raw=raw)
    return record


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
