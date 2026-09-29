"""Export per-question scores and campaign totals for every OfficeQA harness-adapter run.

Reads the local experiment roots (not in git) and writes answer-free tables next to
this file. Only 0/1 scores, completion statuses, token/cost totals and counts are
written; no predicted or reference answer values ever leave the roots.

    python export_results.py /path/to/project-dir
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "officeqa" / "v09-mechanism-adapter" / "plumbing"))
try:
    from data import ISSUE_47  # the 18 ids listed in databricks/officeqa issue #47
except Exception:  # noqa: BLE001 - the table still works without the flag
    ISSUE_47 = ()

# (column, label, root, grade file, state arm key or None)
ARMS = [
    ("v03_baseline", "v3 baseline (no adapter)", "officeqa-codex70-v3-work", "baseline-grades.json", "baseline"),
    ("v03_mwm", "v3 MWM adapter", "officeqa-codex70-v3-work", "adapted-grades.json", "adapted"),
    ("v04_baseline", "v4 baseline", "officeqa-codex50-gold-v4-work", "baseline-grades.json", "baseline"),
    ("v04_mwm_gold", "v4 MWM + gold feedback", "officeqa-codex50-gold-v4-work", "adapted-grades.json", "adapted"),
    ("v05_baseline", "v5 baseline", "officeqa-codex50-full-mwm-v5-work", "baseline-grades.json", "baseline"),
    ("v05_full_mwm", "v5 full MWM", "officeqa-codex50-full-mwm-v5-work", "adapted-grades.json", "adapted"),
    ("v06f_mwm_baseline", "v6f baseline (MWM pair)", "officeqa-codex50-full-mwm-v6f-work", "baseline-grades.json", "baseline"),
    ("v06f_mwm", "v6f full MWM + postmortems", "officeqa-codex50-full-mwm-v6f-work", "adapted-grades.json", "adapted"),
    ("v06f_po_baseline", "v6f baseline (prompt-only pair)", "officeqa-codex50-full-prompt-only-v6f-work", "baseline-grades.json", "baseline"),
    ("v06f_prompt_only", "v6f prompt-only + postmortems", "officeqa-codex50-full-prompt-only-v6f-work", "adapted-grades.json", "adapted"),
    ("v07_baseline", "v7 baseline", "officeqa-codex50-cards-v7-work", "baseline-grades.json", "baseline"),
    ("v07_cards", "v7 cards", "officeqa-codex50-cards-v7-work", "adapted-grades.json", "adapted"),
    ("v08_none", "v8 none (133)", "officeqa-pro133-none-v8-work", "baseline-grades.json", "baseline"),
    ("v08_prompt", "v8 prompt-only (133)", "officeqa-pro133-prompt-v8-work", "adapted-grades.json", "adapted"),
    ("v08_cards", "v8 cards (133)", "officeqa-pro133-cards-v8-work", "adapted-grades.json", "adapted"),
    ("v09p1_a_text_static", "v9 pilot 1 a: text-static T=0", "v9-p1-a", "text-static-grades.json", None),
    ("v09p1_b_mech_static", "v9 pilot 1 b: mech-static T=0", "v9-p1-b", "mech-static-grades.json", None),
    ("v09p1_c_text_static_t1", "v9 pilot 1 c: text-static T=1.0 top_p 0.95", "v9-p1-c", "text-static-grades.json", None),
]
FORBIDDEN = {"answer", "answers", "reference", "expected", "ground_truth", "gold", "prediction", "predicted"}


def load(path: Path):
    return json.loads(path.read_text())


def statuses(root: Path, arm_key: str | None) -> dict[str, str]:
    if arm_key is not None:
        state = load(root / "state.json")
        return {r["task_id"]: r.get("status", "") for r in state["arms"][arm_key]["results"]}
    out = {}
    for summary in sorted((root / "runs" / "episodes").glob("*/summary.json")):
        s = load(summary)
        out[s["task_id"]] = s.get("status", "")
    return out


def cost(root: Path) -> dict | None:
    p = root / "cost-estimate.json"
    if not p.exists():
        return None
    t = load(p).get("formal_total", {})
    return {k: t.get(k) for k in ("logical_calls", "known_usage", "known_cost_lower_bound_usd", "usage_complete")}


def main(base: Path) -> None:
    opus = load(base / "opus-officeqa-133" / "evaluation" / "grades.json")
    order133 = [(r["ordinal"], r["task_id"]) for r in opus["per_question"]]
    # The v9 pilots ran the same 50 questions as v4-v7 (ordinals 1-50 here) in a shuffled order.
    pos50 = {}
    for summary in (base / "v9-p1-a" / "runs" / "episodes").glob("*/summary.json"):
        s = load(summary)
        pos50[s["task_id"]] = s["ordinal"]

    strict: dict[str, dict] = {t: {} for _, t in order133}
    onepct: dict[str, dict] = {t: {} for _, t in order133}
    status: dict[str, dict] = {t: {} for _, t in order133}
    campaigns = []
    for col, label, rootname, gradefile, arm_key in ARMS:
        root = base / rootname
        grades = load(root / "private-evaluation" / gradefile)
        rows = grades["results"]
        for r in rows:
            strict.setdefault(r["task_id"], {})[col] = int(r["strict_score"] >= 1)
            onepct.setdefault(r["task_id"], {})[col] = int(r["one_percent_score"] >= 1)
        for t, s in statuses(root, arm_key).items():
            status.setdefault(t, {})[col] = s
        campaigns.append({
            "column": col, "label": label, "root": rootname, "n": len(rows),
            "strict_correct": sum(int(r["strict_score"] >= 1) for r in rows),
            "one_percent_correct": sum(int(r["one_percent_score"] >= 1) for r in rows),
            "cost_lower_bound": cost(root),
        })
    for arm in ("opus_final", "opus_solver"):
        for r in opus["per_question"]:
            strict[r["task_id"]]["opus_" + arm.split("_")[1]] = int(r[arm])
        campaigns.append({"column": "opus_" + arm.split("_")[1], "label": f"Opus 5.5 reference ({arm})",
                          "root": "opus-officeqa-133", "n": 133, "strict_correct": opus["summary"][arm]["strict"],
                          "one_percent_correct": opus["summary"][arm]["one_percent"], "cost_lower_bound": None})

    cols = [c for c, *_ in ARMS] + ["opus_final", "opus_solver"]
    for name, table, columns in (("per_question_strict.csv", strict, cols),
                                 ("per_question_one_percent.csv", onepct, [c for c, *_ in ARMS]),
                                 ("per_question_status.csv", status, [c for c, *_ in ARMS])):
        with open(HERE / name, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["ordinal_133", "v9_pilot_position", "task_id", "issue_47"] + columns)
            for ordinal, t in order133:
                w.writerow([ordinal, pos50.get(t, ""), t, int(t in ISSUE_47)] + [table[t].get(c, "") for c in columns])
    out = {"note": "Answer-free export: 0/1 scores (official reward.py, strict = 0% tolerance), statuses and totals only.",
           "campaigns": campaigns}
    text = json.dumps(out, indent=1)
    assert not any(f'"{k}"' in text for k in FORBIDDEN), "refusing to write an answer-like field"
    (HERE / "campaigns.json").write_text(text + "\n")
    for c in campaigns:
        print(f'{c["column"]:28s} {c["strict_correct"]:>4}/{c["n"]:<4} 1%={c["one_percent_correct"]}')


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
