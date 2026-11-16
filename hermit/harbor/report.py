"""An eval of a Harbor benchmark, read from its results: pass@1 over the trials that are the agent's result.

    python -m hermit.harbor.report <eval dir or results.jsonl>... [--json]

`hermit eval` scores a trial that is not the agent's result (infra: the host, the provider, a session that never
started, once the runner's own runs again are spent: agent.params.infra_retries) as a failed run: its judge
(hermit.harbor.judge) gives it no score, so it counts among the summary's `errors` and as 0 in its `mean_score`. A
benchmark's own figure leaves such trials out. This reads the eval's results.jsonl (`<run dir>/eval/<adapter>/`) and
gives, over all its repeats and for each repeat:

    trials        the rows of the eval
    scored        the trials the judge scored (status ok): the agent's results
    infra         the trials that are not the agent's result (the runner's verdict: output.status infra)
    failed        the other failed runs (the pipeline or the judge failed for another reason)
    pass_at_1     of the scored trials, the share that passed (the judge's `passed`, which hermit eval keeps in the
                  row's judge.extra: the reward reached the run's pass_reward); null when the judge says no trial
                  passes or fails (pass_reward: null, a score scale)
    mean_scored   the scored trials' mean score
    mean_all      every trial's mean score, the failed and the infra ones counted 0 (hermit eval's mean_score)

Nothing is run again here; a rerun of the infra trials is an eval run again.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

RESULTS = "results.jsonl"


def rows_of(path: Path) -> list[dict]:
    """The rows of an eval's results.jsonl (`path`, or the file in the eval directory `path`)."""
    path = Path(path)
    if path.is_dir():
        path = path / RESULTS
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            if isinstance(row, dict):
                rows.append(row)
    return rows


def kind(row: dict) -> str:
    """scored, infra or failed (the module's docstring)."""
    output = row.get("output") if isinstance(row.get("output"), dict) else {}
    if output.get("status") == "infra":
        return "infra"
    return "scored" if row.get("status") == "ok" else "failed"


def passed(row: dict):
    """Whether the judge said the trial passed (True, False; None: no pass reward), from the row's judge record as
    hermit eval writes it: the judge's other fields beside score and feedback are its `extra`
    (hermit.task.normalise_judge)."""
    judged = row.get("judge") if isinstance(row.get("judge"), dict) else {}
    extra = judged.get("extra") if isinstance(judged.get("extra"), dict) else {}
    return extra.get("passed")


def summary(rows: list[dict]) -> dict:
    """The figures of the module's docstring for `rows`."""
    kinds = [kind(r) for r in rows]
    scored = [r for r, k in zip(rows, kinds) if k == "scored"]
    scores = [float((r.get("judge") or {}).get("score") or 0.0) for r in scored]
    passes = [passed(r) for r in scored]
    judged = [p for p in passes if p is not None]
    return {"trials": len(rows), "scored": len(scored), "infra": kinds.count("infra"), "failed": kinds.count("failed"),
            "pass_at_1": sum(1 for p in judged if p) / len(judged) if judged else None,
            "mean_scored": sum(scores) / len(scores) if scores else None,
            "mean_all": sum(scores) / len(rows) if rows else None}


def report(path: Path) -> dict:
    """An eval's figures: over all its rows, and by repeat when it has more than one."""
    rows = rows_of(path)
    out = {"results": str(path), **summary(rows)}
    repeats = sorted({r.get("repeat") for r in rows if r.get("repeat") is not None})
    if len(repeats) > 1:
        out["by_repeat"] = {str(n): summary([r for r in rows if r.get("repeat") == n]) for n in repeats}
    return out


def _figure(value, percent: bool = False) -> str:
    if value is None:
        return "-"
    return f"{100 * value:.1f}%" if percent else f"{value:.4f}"


def line(figures: dict) -> str:
    return (f"{figures['trials']} trials: {figures['scored']} scored, {figures['infra']} infra, {figures['failed']} "
            f"failed; pass@1 {_figure(figures['pass_at_1'], True)} of the scored, mean score "
            f"{_figure(figures['mean_scored'])} of the scored, {_figure(figures['mean_all'])} with the infra and "
            "failed trials as 0")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("evals", nargs="+", type=Path, help="an eval's directory or its results.jsonl")
    parser.add_argument("--json", action="store_true", help="the figures as JSON, one object per eval")
    args = parser.parse_args(argv)
    for path in args.evals:
        try:
            figures = report(path)
        except (OSError, ValueError) as exc:
            print(f"{path}: {exc}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(figures))
            continue
        print(f"{path}: {line(figures)}")
        for n, each in (figures.get("by_repeat") or {}).items():
            print(f"  repeat {n}: {line(each)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
