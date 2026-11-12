"""AlgoTune's judge: the Harbor judge's verdict (hermit.harbor.judge), with a trial scored 1 - 1/speedup.

The verifier's reward is the trial's speedup: the reference solver's time over the agent's when every output is valid
and the solver is faster, else 1.0 (AlgoTune's floor); 0 when the verifier wrote none (it crashed before the timing
ended), which AlgoTune counts as no speedup too. AlgoTune scores a set of tasks by the harmonic mean H of their
speedups. The score of a trial here is q = 1 - 1/speedup (0 for no speedup), so that the mean m of the scores, the
figure every mean of Hermit's takes (the Trainer's, the Evolvers', `hermit eval`'s), is 1 - 1/H: H = 1 / (1 - m), and a
method that raises the mean raises AlgoTune's own score. The speedup is kept beside it (`speedup`).

    judge:
      path: ../utils/judge.py

    python examples/algotune/utils/judge.py <eval dir>...   # each eval's harmonic mean, from its results.jsonl
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from hermit.harbor import judge as harbor


def speedup_score(speedup: float | None) -> float:
    """1 - 1/speedup for a speedup above 1, else 0 (no speedup, or no reward)."""
    return 1.0 - 1.0 / speedup if speedup is not None and speedup > 1.0 else 0.0


def score(task: dict, output) -> dict:
    record = harbor.score(task, output)
    out = output.output if isinstance(output.output, dict) else {}
    raw = out.get("reward")
    speedup = float(raw) if raw is not None and record.get("status") != "infra" else None
    record["speedup"] = speedup
    record["score"] = speedup_score(speedup)
    if speedup is not None:
        record["feedback"] = f"speedup {speedup:g}x (score {record['score']:.3f}): " + record["feedback"]
    return record


def harmonic_mean(results: Path) -> dict:
    """AlgoTune's score of an eval's results.jsonl: the harmonic mean of the scored trials' speedups (no speedup
    counted as 1x), from their scores; infra trials are left out and counted."""
    scores, infra = [], 0
    for line in Path(results).read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("status") == "infra":
            infra += 1
            continue
        judged = row.get("judge") or {}
        scores.append(float(judged.get("score") or 0.0))
    mean = sum(scores) / len(scores) if scores else None
    return {"trials": len(scores), "infra": infra, "mean_score": mean,
            "harmonic_mean_speedup": None if mean is None else 1.0 / (1.0 - mean),
            "sped_up": sum(1 for s in scores if s > 0)}


if __name__ == "__main__":
    for arg in sys.argv[1:] or ["."]:
        path = Path(arg)
        path = path / "results.jsonl" if path.is_dir() else path
        print(f"{path}: {json.dumps(harmonic_mean(path))}")
