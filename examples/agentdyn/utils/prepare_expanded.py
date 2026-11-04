"""Freeze a multi-suite replay protocol from one existing AgentDyn log collection.

No attack generator, trajectory scores, or model outcomes participate in selection.
The native audit initializes local simulated environments; it does not call models.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import random

from bridge import AGENTDYN_REVISION, case_fields, validate_case

SUITES = ("shopping", "github", "dailylife")
# Source-level judge discrepancies, fixed before any model-backed expanded run.
EXCLUSIONS = {
    ("shopping", "user_task_4"): "prompt/ground_truth request one item; utility requires quantity two",
    ("shopping", "user_task_19"): "prompt saves invoice under /user; utility requires /user/invoices",
    ("github", "user_task_9"): "utility uses post_environment for both pre and post references",
    ("github", "user_task_12"): "prompt requests all collaborators; utility accepts any one via OR",
    ("dailylife", "user_task_18"): "prompt says today (2024-05-19); utility requires 2024-05-20",
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def read_collection(root):
    """Whitelist task fields; exclude all old messages, verdicts, and error records."""
    root = Path(root)
    cases, sources = {}, defaultdict(list)
    for suite in SUITES:
        for path in sorted((root / suite).rglob("*.json")):
            row = json.loads(path.read_text())
            if (not isinstance(row, dict) or row.get("suite_name") != suite
                    or not row.get("user_task_id") or not row.get("injection_task_id")
                    or not row.get("injections")):
                continue
            case = {"suite": suite, **{key: row[key] for key in
                    ("user_task_id", "injection_task_id", "injections")}}
            validate_case(case)
            key = digest(case)
            cases[key] = case
            sources[key].append({"path": str(path.relative_to(root)),
                                 "file_sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    if {c["suite"] for c in cases.values()} != set(SUITES):
        raise ValueError("source must contain existing injected cases for all three suites")
    return cases, sources


def audit_native(cases):
    """Validate IDs/vectors and reject already-successful or malformed initial states."""
    from agentdojo.task_suite import get_suite
    suites = {name: get_suite("v1.2.2", name) for name in SUITES}
    inventory = {name: {"user_tasks": len(s.user_tasks), "injection_tasks": len(s.injection_tasks),
                       "vectors": len(s.get_injection_vector_defaults())} for name, s in suites.items()}
    audit = {}
    for key, case in sorted(cases.items()):
        suite = suites[case["suite"]]
        try:
            user = suite.get_user_task_by_id(case["user_task_id"])
            attack = suite.get_injection_task_by_id(case["injection_task_id"])
            if set(case["injections"]) - set(suite.get_injection_vector_defaults()):
                raise ValueError("unknown injection vector")
            environment = user.init_environment(suite.load_and_inject_default_environment(case["injections"]))
            clean = user.init_environment(suite.load_and_inject_default_environment({}))
            # Follow the native dispatch: some goals implement only *_from_traces.
            utility = suite._check_task_result(user, [], environment.model_copy(deep=True), environment, [])
            clean_utility = suite._check_task_result(user, [], clean.model_copy(deep=True), clean, [])
            security = suite._check_task_result(attack, [], environment.model_copy(deep=True), environment, [])
            valid = type(utility) is bool and type(clean_utility) is bool and type(security) is bool
            audit[key] = {"eligible": valid and not utility and not clean_utility and not security,
                          "initial_injected_utility": utility, "initial_clean_utility": clean_utility,
                          "initial_attack_success": security}
        except Exception as exc:
            audit[key] = {"eligible": False, "error_type": type(exc).__name__}
    return inventory, audit


def select(cases, audit, seed=20261101, groups_per_split=2, goals_per_group=2):
    if groups_per_split < 1 or goals_per_group < 1:
        raise ValueError("positive groups_per_split and goals_per_group required")
    groups = defaultdict(lambda: defaultdict(dict))
    for key, case in sorted(cases.items()):
        pair = (case["suite"], case["user_task_id"])
        if pair in EXCLUSIONS or not audit[key]["eligible"]:
            continue
        # Deterministically pick one recorded payload per user-task/goal, independent of verdicts.
        groups[case["suite"]][case["user_task_id"]].setdefault(case["injection_task_id"], key)
    rows, assignments, chosen = [], [], []
    splits = {name: [] for name in ("train", "val", "test")}
    for suite in SUITES:
        available = sorted(t for t, goals in groups[suite].items() if len(goals) >= goals_per_group)
        random.Random(f"{seed}:{suite}").shuffle(available)
        needed = 3 * groups_per_split
        if len(available) < needed:
            raise ValueError(f"{suite}: need {needed} eligible user groups, found {len(available)}")
        for index, task in enumerate(available[:needed]):
            split = tuple(splits)[index // groups_per_split]
            assignments.append({"suite": suite, "user_task_id": task, "split": split})
            group_rows = [{"id": f"{suite}-{task}-clean", "suite": suite, "user_task_id": task}]
            goals = sorted(groups[suite][task])
            # Rotation spreads existing goal families across successive user groups.
            start = index * goals_per_group % len(goals)
            for offset in range(goals_per_group):
                goal = goals[(start + offset) % len(goals)]
                key = groups[suite][task][goal]
                group_rows.append({"id": f"{suite}-{task}-{goal}-{key[:8]}", **cases[key]})
                chosen.append(key)
            rows.extend(group_rows)
            splits[split].extend(r["id"] for r in group_rows)
    # Interleave suites in each split so cheap cascade prefixes are not single-domain.
    by_id = {r["id"]: r for r in rows}
    for split, ids in splits.items():
        buckets = {s: [i for i in ids if by_id[i]["suite"] == s] for s in SUITES}
        splits[split] = [buckets[s][index] for index in range(len(next(iter(buckets.values())))) for s in SUITES]
    return rows, splits, assignments, chosen


def export_full(output, cases, audit, assignments, seed):
    """Export every eligible frozen case, preserving acceptance split ownership."""
    output = Path(output)
    owners = {(a["suite"], a["user_task_id"]): a["split"] for a in assignments}
    eligible = {k: c for k, c in cases.items() if audit[k]["eligible"]
                and (c["suite"], c["user_task_id"]) not in EXCLUSIONS}
    groups = sorted({(c["suite"], c["user_task_id"]) for c in eligible.values()})
    for suite in SUITES:
        remaining = [g for g in groups if g[0] == suite and g not in owners]
        random.Random(f"{seed}:{suite}:full").shuffle(remaining)
        for index, group in enumerate(remaining):
            owners[group] = ("train", "val", "test")[index % 3]
    rows = []
    for s, t in groups:
        rows.append({"id": f"{s}-{t}-clean", "suite": s, "user_task_id": t})
        rows.extend({"id": f"{s}-{t}-{c['injection_task_id']}-{k[:8]}", **c}
                    for k, c in sorted(eligible.items()) if (c["suite"], c["user_task_id"]) == (s, t))
    output.mkdir(parents=True, exist_ok=False)
    (output / "tasks.jsonl").write_text("".join(json.dumps(case_fields(r), sort_keys=True) + "\n" for r in rows))
    counts = {}
    for split in ("train", "val", "test"):
        buckets = {s: [r["id"] for r in rows if r["suite"] == s
                      and owners[(s, r["user_task_id"]) ] == split] for s in SUITES}
        ids = [buckets[s][index] for index in range(max(map(len, buckets.values())))
               for s in SUITES if index < len(buckets[s])]
        (output / f"{split}.txt").write_text("\n".join(ids) + "\n")
        counts[split] = len(ids)
    manifest = {"protocol": "full eligible frozen inventory; not a completed live evaluation",
                "reference_revision": AGENTDYN_REVISION, "seed": seed, "counts": counts,
                "groups": [{"suite": s, "user_task_id": t, "split": owners[(s, t)]} for s, t in groups],
                "tasks_sha256": hashlib.sha256((output / "tasks.jsonl").read_bytes()).hexdigest()}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def prepare(source, output, seed=20261101, groups_per_split=2, goals_per_group=2):
    output = Path(output)
    if output.exists():
        raise FileExistsError(output)
    cases, sources = read_collection(source)
    inventory, audit = audit_native(cases)
    rows, splits, assignments, chosen = select(cases, audit, seed, groups_per_split, goals_per_group)
    manifest = {
        "protocol": "AgentDyn multi-suite frozen replay acceptance v1; not official benchmark score",
        "reference_revision": AGENTDYN_REVISION, "benchmark_version": "v1.2.2", "seed": seed,
        "groups_per_suite_per_split": groups_per_split, "goals_per_group": goals_per_group,
        "source_collection": str(Path(source).resolve()), "selection_uses_model_outcomes": False,
        "native_inventory": inventory, "source_case_counts": dict(Counter(c["suite"] for c in cases.values())),
        "groups": assignments, "counts": {s: len(ids) for s, ids in splits.items()},
        "exclusions": [{"suite": s, "user_task_id": t, "reason": reason} for (s, t), reason in EXCLUSIONS.items()],
        "selected_sources": {k: sources[k] for k in chosen},
        "audit_scope": "native ID/vector/environment initialization and no-op verdicts; not semantic pairing certification",
    }
    output.mkdir(parents=True)
    (output / "tasks.jsonl").write_text("".join(json.dumps(case_fields(r), sort_keys=True) + "\n" for r in rows))
    for split, ids in splits.items():
        (output / f"{split}.txt").write_text("\n".join(ids) + "\n")
    (output / "source-audit.json").write_text(json.dumps({"inventory": inventory, "cases": audit}, indent=2))
    manifest["tasks_sha256"] = hashlib.sha256((output / "tasks.jsonl").read_bytes()).hexdigest()
    manifest["split_sha256"] = {s: hashlib.sha256((output / f"{s}.txt").read_bytes()).hexdigest() for s in splits}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2))
    export_full(output / "full", cases, audit, assignments, seed)
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="one existing native runs/model directory containing all suites")
    parser.add_argument("output", type=Path, help="new output directory; never overwritten")
    parser.add_argument("--seed", type=int, default=20261101)
    parser.add_argument("--groups-per-split", type=int, default=2)
    parser.add_argument("--goals-per-group", type=int, default=2)
    args = parser.parse_args()
    result = prepare(args.source, args.output, args.seed, args.groups_per_split, args.goals_per_group)
    print(json.dumps({k: result[k] for k in ("counts", "tasks_sha256", "native_inventory")}, indent=2))
