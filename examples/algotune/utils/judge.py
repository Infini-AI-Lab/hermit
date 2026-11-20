"""AlgoTune's judge: the verdict of Harbor's verifier in the task's environment (hermit.task.verify.environment), with
a trial scored 1 - 1/speedup.

The verifier's reward is the trial's speedup: the reference solver's time over the agent's when every output is valid
and the solver is faster, else 1.0 (AlgoTune's floor); 0 when the verifier wrote none (it crashed before the timing
ended), which AlgoTune counts as no speedup too. AlgoTune scores a set of tasks by the harmonic mean H of their
speedups. The score of a trial here is q = 1 - 1/speedup (0 for no speedup), so that the mean m of the scores, the
figure every mean of Hermit's takes (the Trainer's, the Evolvers', `hermit eval`'s), is 1 - 1/H: H = 1 / (1 - m), and a
method that raises the mean raises AlgoTune's own score. The speedup is kept beside it (`speedup`). A trial that is
not the agent's result (its environment failed) gets no score: the environment judge refuses it (`Unscored`), so the
run records a failed run, scored 0 in `hermit eval`'s mean.

    judge:
      path: ../utils/judge.py

    python examples/algotune/utils/judge.py <eval dir>...   # each eval's harmonic mean, over its scored trials
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from hermit.harbor import report
from hermit.task.verify import environment


def speedup_score(speedup: float | None) -> float:
    """1 - 1/speedup for a speedup above 1, else 0 (no speedup, or no reward)."""
    return 1.0 - 1.0 / speedup if speedup is not None and speedup > 1.0 else 0.0


def score(task: dict, output) -> dict:
    verdict = environment(task, output)                 # an infra trial: Unscored, raised before any reward is read
    raw = (output.extra or {})["verified"].get("reward")
    speedup = float(raw) if raw is not None else None
    record = {"score": verdict.score, "feedback": verdict.feedback, **verdict.extra}
    record["speedup"] = speedup
    record["score"] = speedup_score(speedup)
    if speedup is not None:
        record["feedback"] = f"speedup {speedup:g}x (score {record['score']:.3f}): " + record["feedback"]
    return record


def harmonic_mean(results: Path) -> dict:
    """AlgoTune's score of an eval's results.jsonl: the harmonic mean of the speedups of the trials that are the agent's
    result (no speedup counted as 1x), from their scores, the others counted apart as `python -m hermit.harbor.report`
    counts them (`infra`; `failed`, a run the pipeline or the judge failed for another reason); `finished` false, and
    `unfinished` why, for an eval that did not finish (report.unfinished): its rows are those of the repeats it
    finished, none with one repeat."""
    rows = report.rows_of(Path(results))
    figures = report.summary(rows)
    why = report.unfinished(Path(results), rows)
    scores = [float((row.get("judge") or {}).get("score") or 0.0) for row in rows if report.kind(row) == "scored"]
    mean = figures["mean_scored"]
    return {"trials": figures["trials"], "scored": figures["scored"], "infra": figures["infra"],
            "failed": figures["failed"], "mean_score": mean,
            "harmonic_mean_speedup": None if mean is None else 1.0 / (1.0 - mean),
            "sped_up": sum(1 for s in scores if s > 0), "finished": why is None, **({"unfinished": why} if why else {})}


if __name__ == "__main__":
    for arg in sys.argv[1:] or ["."]:
        path = Path(arg)
        path = path / "results.jsonl" if path.is_dir() else path
        print(f"{path}: {json.dumps(harmonic_mean(path))}")
