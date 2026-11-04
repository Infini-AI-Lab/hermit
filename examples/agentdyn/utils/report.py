"""Report grouped security metrics from Hermit eval results.jsonl."""
import argparse
from collections import Counter
import json
from pathlib import Path

from hermit.agent.evolver.methods.evosafe.contract import aggregate, completed, summary


def summarize(rows, expected_ids, repeats=1, *, tasks=None):
    if repeats < 1 or not expected_ids or len(set(expected_ids)) != len(expected_ids):
        raise ValueError("provide unique expected IDs and positive repeats")
    if tasks is not None:
        index = {t["id"]: t for t in tasks}
        if len(index) != len(tasks) or not set(expected_ids) <= index.keys():
            raise ValueError("task file must have unique IDs covering the expected split")
        enriched = []
        for row in rows:
            if row["id"] not in index:
                raise ValueError("result ID missing from task file")
            task = index[row["id"]]
            kind = task.get("kind") or ("injected" if task.get("injection_task_id") is not None else "clean")
            metrics = ((row.get("judge") or {}).get("extra") or {}).get("metrics") or {}
            if metrics.get("kind", kind) != kind:
                raise ValueError("result kind contradicts task file")
            enriched.append({**row, "input": {"kind": kind}})
        rows = enriched
    elif any("input" not in row for row in rows):
        raise ValueError("tasks are required for eval records without input")
    # A run without a valid verdict is an error as much as a failed one.
    valid = [r for r in rows if completed(r, (r.get("input") or {}).get("kind"))]
    errors = len(rows) - len(valid)
    expected = Counter((task, repeat) for task in expected_ids for repeat in range(1, repeats + 1))
    observed = Counter((r["id"], r.get("repeat", 1)) for r in rows)
    coverage_ok = observed == expected
    report = {"episodes": len(rows), "errors": errors, "error_rate": errors / len(rows) if rows else None,
              "expected_episodes": sum(expected.values()), "coverage_ok": coverage_ok,
              "complete": coverage_ok and errors == 0,
              "unknown_cost_episodes": sum(r.get("cost_usd") is None for r in rows)}
    try:
        report["completed_episode_metrics"] = aggregate(valid)
    except ValueError as exc:
        report["metrics_error"] = str(exc)
    # Every run counted, failed ones included (never credited); objective only when all completed.
    try:
        report["defense_metrics"] = summary(rows)
        # summary sees only observed rows; the expected split/repeats define completeness.
        if not report["complete"]:
            report["defense_metrics"]["objective"] = None
    except ValueError as exc:
        report["defense_metrics_error"] = str(exc)
    # Partial-run metrics are diagnostic only, never a candidate selection score.
    report["selection_objective"] = report.get("completed_episode_metrics", {}).get("objective") if report["complete"] else None
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path)
    parser.add_argument("--tasks", type=Path, required=True, help="task JSONL for ID-to-kind mapping")
    parser.add_argument("--split", type=Path, required=True, help="expected test IDs, one per line")
    parser.add_argument("--repeats", type=int, default=1)
    args = parser.parse_args()
    ids = [s.strip() for s in args.split.read_text().splitlines() if s.strip() and not s.startswith("#")]
    print(json.dumps(summarize([json.loads(s) for s in args.results.read_text().splitlines() if s.strip()], ids, args.repeats, tasks=[json.loads(s) for s in args.tasks.read_text().splitlines() if s.strip()]), indent=2))
