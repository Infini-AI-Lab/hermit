#!/usr/bin/env python3
"""Write one ServeLearnBench scenario as a framework task: tasks.jsonl, the splits, windows.json, and the
learner's read-only copy of the reference documents.

    python prepare.py --scenario retail_l3 [--block 12] [--smoke 12]

The serving stream becomes `splits/serving.txt` in stream order; the held-out test tasks of window w become
`splits/test_w<w>.txt`. A task row holds what the acting agent receives and nothing the evaluator keeps:
{id, scenario, phase, timestamp, block, instruction}. `timestamp` is the stream position ServeLearnBench stamps on
the instruction (a window's test tasks carry the position of that window's last serving task); `block` is the MWM
block a serving task belongs to (position // block + 1), which names its evidence directory, so the run must use
`data.step_size` = --block and `data.shuffle: false`. Window indexes and the evaluator's fields (ground truth,
category, family) are not in the rows: windows.json maps them on the host side, and nothing a learner reads says
where a window begins.

`--smoke N` also writes `splits/smoke_serving.txt` (the first N serving tasks) and `splits/smoke_test.txt` (the
first 4 test tasks of window 0), for a short end-to-end check.

The reference documents are exported section by section through the agent's own `read_docs` tool into
task-info/mwm/environment/docs/, so the learner's /environment holds exactly what a host can retrieve.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _read_docs_tool(bundle):
    for tool in bundle["tools"]:
        try:
            if tool.get_info()["function"]["name"] == "read_docs":
                return tool
        except Exception:                                      # noqa: BLE001
            continue
    return None


def export_docs(bundle, dest: Path) -> int:
    """One file per section, the text the tool returns, nothing more; returns the number of sections."""
    tool = _read_docs_tool(bundle)
    if tool is None:
        return 0
    data = bundle["load_data"]()
    n = 0
    for doc in ("policy", "workflow"):
        toc = tool.invoke(data=data, doc=doc)
        slugs = [line[2:].split(" — ", 1)[0].strip() for line in str(toc).splitlines() if line.startswith("- ")]
        if not slugs:
            continue
        d = dest / doc
        d.mkdir(parents=True, exist_ok=True)
        for slug in slugs:
            body = tool.invoke(data=data, doc=doc, section=slug)
            if isinstance(body, str) and body.startswith("Error:"):
                continue
            (d / f"{slug}.md").write_text(body if isinstance(body, str) else json.dumps(body))
            n += 1
    return n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="retail_l3")
    ap.add_argument("--block", type=int, default=12, help="serving episodes per MWM block (= data.step_size)")
    ap.add_argument("--smoke", type=int, default=12, help="serving tasks in the smoke split (0: none)")
    a = ap.parse_args()

    from servelearnbench import protocol
    from servelearnbench.scenarios import get_bundle, n_windows

    bundle = get_bundle(a.scenario, fresh=True)
    serving = list(bundle["serving"])
    protocol.stamp_serving(serving)
    splits = HERE / "splits"
    splits.mkdir(exist_ok=True)
    rows, windows = [], []
    for t in serving:
        pos = t.z["stream_pos"]
        rows.append({"id": t.task_id, "scenario": a.scenario, "phase": "serving", "timestamp": pos,
                     "block": (pos - 1) // a.block + 1, "instruction": t.instruction})
    (splits / "serving.txt").write_text("".join(t.task_id + "\n" for t in serving))
    for w in range(n_windows(bundle)):
        tests = bundle["test_set"](w)
        ts = protocol.stamp_test(tests, bundle["serving"], w)
        first = min(t.z["stream_pos"] for t in serving if t.z["window"] == w)
        windows.append({"window": w, "serving_from": first, "serving_to": ts,
                        "serving": [t.task_id for t in serving if t.z["window"] == w],
                        "test": [t.task_id for t in tests]})
        for t in tests:
            rows.append({"id": t.task_id, "scenario": a.scenario, "phase": "test", "timestamp": ts,
                         "instruction": t.instruction})
        (splits / f"test_w{w}.txt").write_text("".join(t.task_id + "\n" for t in tests))
    ids = [r["id"] for r in rows]
    if len(set(ids)) != len(ids):
        raise SystemExit("task ids are not unique across serving and test")
    (HERE / "tasks.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    (HERE / "windows.json").write_text(json.dumps({"scenario": a.scenario, "block": a.block, "windows": windows},
                                                  indent=1))
    if a.smoke:
        (splits / "smoke_serving.txt").write_text("".join(t.task_id + "\n" for t in serving[:a.smoke]))
        (splits / "smoke_test.txt").write_text("".join(i + "\n" for i in windows[0]["test"][:4]))
    docs = HERE / "task-info" / "mwm" / "environment" / "docs"
    n = export_docs(bundle, docs)
    print(f"{a.scenario}: {len(serving)} serving tasks in {len(windows)} windows, "
          f"{sum(len(w['test']) for w in windows)} test tasks, block {a.block}, {n} document sections exported")


if __name__ == "__main__":
    main()
