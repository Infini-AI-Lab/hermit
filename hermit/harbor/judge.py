"""The judge of a benchmark run through Harbor: the verifier's reward, as the container wrote it.

    judge:
      path: hermit.harbor.judge        # name: score (the default)

The score is the trial's reward (`/logs/verifier/reward.txt` or `reward.json`'s `reward_key`, carried in the runner's
output) whenever the verifier gave one, whatever the trial's status: a trial that ran out of the agent's time or
whose agent exited with an error still has its work scored, as Harbor's own job mean scores it. A trial without a
reward scores 0. A trial that is not the agent's result (infra: the host, the provider, a session that never started;
the runner marks it `unscored` after its own runs again, agent.params.infra_retries) gets no score: `score` raises
Unscored, so that the run records a failed run (hermit.trainer.rollout: status judge_error, its usage and log kept),
which `hermit eval` counts among its errors and as 0 in its mean, as EvoSafe's judge does with a run it cannot score;
`python -m hermit.harbor.report <eval dir>` gives the pass@1 over the trials that are the agent's result beside it.
A trial "passed" when its reward reaches the benchmark's pass reward (`agent.params.pass_reward`,
1.0 unless the benchmark sets another; null for a benchmark whose reward is a scale with no pass, where none is said
to pass); `passed` says so (None without a pass reward). The feedback names the
status (and, for a context over the CLI's limit, by how much: the session never started) and, for a trial that did
not pass, the tail of the verifier's output (as curated: `verifier_redact` applied), which is what a learner can act
on. The verifier's other reward keys are kept in `rewards`, for every status.
"""
from __future__ import annotations

from pathlib import Path

TAIL = 1500                                              # characters of the verifier's stdout in the feedback
PASS_REWARD = 1.0


class Unscored(RuntimeError):
    """A trial that is not the agent's result (infra): no score is the agent's."""


def pass_reward(output) -> float | None:
    """The run's pass reward, as the runner gave it (`extra["judge"]["pass_reward"]`), else PASS_REWARD."""
    extra = getattr(output, "extra", None)
    judge = extra.get("judge") if isinstance(extra, dict) else None
    if isinstance(judge, dict) and "pass_reward" in judge:
        return None if judge["pass_reward"] is None else float(judge["pass_reward"])
    return PASS_REWARD


def unscored(output) -> str | None:
    """Why a rollout is not the agent's result (the runner's `unscored` cause, or an infra status), or None."""
    out = output.output if isinstance(output.output, dict) else {}
    extra = getattr(output, "extra", None)
    why = extra.get("unscored") if isinstance(extra, dict) else None
    if why or (out.get("status") or "infra") == "infra":
        return str(why or out.get("failure_cause") or "infra")
    return None


def score(task: dict, output) -> dict:
    why = unscored(output)
    if why is not None:
        extra = getattr(output, "extra", None)
        said = str((extra.get("exception_message") if isinstance(extra, dict) else None) or "")[:600]
        raise Unscored(f"the trial is not the agent's result ({why}{': ' + said if said else ''}): it has no score")
    out = output.output if isinstance(output.output, dict) else {}
    status = out.get("status") or "infra"
    raw = out.get("reward")
    reward = float(raw) if raw is not None else None
    bar = pass_reward(output)
    passed = reward is not None and bar is not None and reward >= bar
    cause = out.get("failure_cause") or status
    if status == "ok":
        feedback = "passed" if passed else f"reward {reward:g}" if reward is not None else "no reward"
    else:
        detail = out.get("exception_type") or ""
        feedback = f"{status} ({cause}{': ' + detail if detail else ''})"
        if cause == "solution_timed_out":
            feedback += ("; the verifier ran out of its time on the agent's solution (too slow to finish its tests, "
                         "or a process it left that hung them)")
        elif cause in ("context_over_limit", "adapter_failed"):   # no session ran: the message says why
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
