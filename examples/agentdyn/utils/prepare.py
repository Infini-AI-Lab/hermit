"""Normalize fixed replay cases; split by user task, never by injection variant."""
import argparse
import hashlib
import json
from pathlib import Path
import random

from bridge import AGENTDYN_REVISION, case_fields, validate_case


def read_logs(directory, suite):
    """Import only already-recorded injections, never call an attack generator."""
    cases = []
    for path in sorted(Path(directory).rglob("*.json")):
        record = json.loads(path.read_text())
        if not isinstance(record, dict) or record.get("suite_name") != suite:
            continue
        if not record.get("user_task_id") or not record.get("injection_task_id") or not record.get("injections"):
            continue
        cases.append({"suite": suite, "user_task_id": record["user_task_id"],
                      "injection_task_id": record["injection_task_id"], "injections": record["injections"]})
    if not cases:
        raise ValueError("no fixed injected cases found in native trace logs")
    return cases


def prepare(cases, output, seed=0):
    rows = []
    seen = set()
    groups = set()
    for case in cases:
        validate_case(case)
        # Explicitly discard old trajectories/verdicts and other answer material.
        # A clean case is the same row whether its injection fields are absent, null or empty.
        row = {key: case[key] for key in ("suite", "user_task_id", "injection_task_id", "injections") if case.get(key)}
        identity = json.dumps(row, sort_keys=True)
        if identity in seen:
            continue
        seen.add(identity)
        groups.add((row["suite"], row["user_task_id"]))
        row["id"] = "case-" + hashlib.sha256(identity.encode()).hexdigest()[:20]
        rows.append(row)
    # Pair every user task with a clean execution, including those supplied only
    # as injected cases. The native suite supplies the normal environment.
    for suite, task in sorted(groups):
        if not any(r["suite"] == suite and r["user_task_id"] == task and not r.get("injection_task_id") for r in rows):
            rows.append({"id": f"{suite}-{task}-clean", "suite": suite, "user_task_id": task})
    ordered = sorted(groups)
    if len(ordered) < 5:
        raise ValueError("need at least five user-task groups for train/validation/test")
    random.Random(seed).shuffle(ordered)
    n = max(1, len(ordered) // 5)
    assignment = {g: "val" if i < n else "test" if i < 2*n else "train" for i, g in enumerate(ordered)}
    splits = {name: [r for r in rows if assignment[(r["suite"], r["user_task_id"])] == name]
              for name in ("train", "val", "test")}
    for name, members in splits.items():
        if not any(r.get("injection_task_id") for r in members):
            raise ValueError(f"{name} has no injected cases; provide coverage for more user tasks")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    (output / "tasks.jsonl").write_text("".join(json.dumps(case_fields(r)) + "\n" for r in rows))
    for name, members in splits.items():
        (output / f"{name}.txt").write_text("".join(r["id"] + "\n" for r in members))
    manifest = {"seed": seed, "reference_revision": AGENTDYN_REVISION,
                "groups": [{"suite": s, "user_task_id": u, "split": assignment[(s,u)]} for s,u in ordered],
                "counts": {k: len(v) for k,v in splits.items()},
                "tasks_sha256": hashlib.sha256((output / "tasks.jsonl").read_bytes()).hexdigest()}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cases", type=Path, help="JSONL of existing frozen replay cases, or native log directory with --from-logs")
    parser.add_argument("output", type=Path, help="new output directory")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--from-logs", action="store_true")
    parser.add_argument("--suite", choices=["shopping", "github", "dailylife"], default="shopping")
    args = parser.parse_args()
    cases = read_logs(args.cases, args.suite) if args.from_logs else [
        json.loads(line) for line in args.cases.read_text().splitlines() if line.strip()]
    prepare(cases, args.output, args.seed)
