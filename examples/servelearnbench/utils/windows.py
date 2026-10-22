#!/usr/bin/env python3
"""The benchmark's per-window evaluation of a training run, and its result file.

    python windows.py --config CFG --run RUN_NAME [--windows 0,1] [--workdir DIR] [overrides...]

ServeLearnBench scores a method on each window's held-out test tasks after that window's serving tasks, against the
method as it then stands. Test tasks never update a method, so the same scores come from replaying them later on
the adapter the run had at that point: the Trainer keeps one commit per step, and this script picks, for window w,
the last step whose tasks all lie in windows 0..w (the adapter before any later serving task was learned from),
runs `hermit eval` on that commit with the window's test split, and then assembles every row the pipeline
wrote (serving and test) into one file in the benchmark's format:

    <workdir>/results/<scenario>_<method>.jsonl        slb score <file>

A window whose last block is shared with the next window is evaluated on the commit before that block, so the few
serving tasks of that block go unlearned for the test (the benchmark's own runner updates at the window's end).
`data.step_size` that divides each window's serving count avoids this.

Windows can be evaluated while the run is still training: a commit never changes. `--windows` selects a subset;
a window whose step has not been trained yet is skipped and said so.

The adapter's tools read the run's evidence store (its `trace` follows an address to the raw record). A replay runs
after later serving tasks were learned from, so each window's test gets a snapshot, `<workdir>/evidence_w<w>/`,
holding only the records up to the last serving task of the chosen step: what the adapter could reach at the time.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys

HERE = Path(__file__).resolve().parent


def _steps(run_dir: Path) -> list[dict]:
    p = run_dir / "steps.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def commit_for_window(steps: list[dict], windows: dict, w: int, stream_pos: dict[str, int]) -> tuple[str | None, int]:
    """The commit after the last step whose every task is at or before window w's last serving task, and that step
    (0 = the bootstrap or root)."""
    end = windows["windows"][w]["serving_to"]
    chosen, number = None, 0
    for s in steps:
        if s.get("bootstrap"):
            chosen, number = s["commit"], 0
            continue
        positions = [stream_pos[t] for t in s["tasks"] if t in stream_pos]
        if positions and max(positions) <= end:
            chosen, number = s["commit"], s["step"]
        elif positions and min(positions) > end:
            break
    return chosen, number


def evidence_snapshot(workdir: Path, w: int, upto: int) -> Path:
    """`<workdir>/evidence_w<w>/`: the evidence store's README and every record whose stream position is at or
    before `upto`, in their block directories; rebuilt on every call."""
    src, dst = workdir / "evidence", workdir / f"evidence_w{w}"
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    if (src / "README.md").is_file():
        shutil.copy2(src / "README.md", dst / "README.md")
    n = 0
    for p in sorted(src.glob("block*/e*.json")):
        digits = p.stem[1:].split("_", 1)[0]
        if digits.isdigit() and int(digits) <= upto:
            (dst / p.parent.name).mkdir(exist_ok=True)
            shutil.copy2(p, dst / p.parent.name / p.name)
            n += 1
    print(f"W{w}: evidence snapshot of {n} records (stream positions <= {upto}) at {dst}")
    return dst


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, type=Path)
    ap.add_argument("--run", required=True, help="trainer.run_name of the training run")
    ap.add_argument("--windows", default="", help="comma-separated window indexes (default: all)")
    ap.add_argument("--workdir", type=Path, default=None, help="the run's task directory (default: $SLB_WORKDIR)")
    ap.add_argument("--method", default="mwm", help="the method name written into the result file")
    ap.add_argument("--smoke", action="store_true", help="a prefix run: test the final adapter on splits/smoke_test.txt")
    ap.add_argument("overrides", nargs="*", help="more key=value overrides for the eval config")
    a = ap.parse_args()
    import os
    workdir = a.workdir or Path(os.environ["SLB_WORKDIR"])
    os.environ["SLB_WORKDIR"] = str(workdir)
    os.environ.setdefault("SLB_METHOD", a.method)

    from hermit.config import Config
    from hermit.evaluate import evaluate
    from hermit.tree import ROOT_TAG

    windows = json.loads((HERE / "windows.json").read_text())
    tasks = {json.loads(l)["id"]: json.loads(l) for l in (HERE / "tasks.jsonl").read_text().splitlines() if l.strip()}
    stream_pos = {tid: int(r["timestamp"]) for tid, r in tasks.items() if r["phase"] == "serving"}
    base = Config.load(a.config, [f"trainer.run_name={a.run}", *a.overrides])
    run_dir = base.run_dir()
    steps = _steps(run_dir)
    if not steps and not (run_dir / "steps.jsonl").exists():
        raise SystemExit(f"no training run at {run_dir}")
    wanted = [int(x) for x in a.windows.split(",") if x.strip()] or [w["window"] for w in windows["windows"]]
    trained_to = max((stream_pos[t] for s in steps for t in s["tasks"] if t in stream_pos), default=0)
    report = []
    if a.smoke:
        # A run on a prefix of the stream: its final adapter on the smoke test split, nothing per window.
        split = HERE / "splits" / "smoke_test.txt"
        cfg = Config.load(a.config, [f"trainer.run_name={a.run}", f"data.test={split}", *a.overrides])
        print(f"smoke: eval of the final adapter on {split.name}")
        summary = evaluate(cfg, "final", log=print)
        report.append({"window": None, "commit": summary.get("commit"), "step": None, **summary})
        wanted = []
    for w in wanted:
        end = windows["windows"][w]["serving_to"]
        if trained_to < end:
            # The step holding this window's last serving task is not committed (or the run never reached it).
            print(f"W{w}: not trained (stream position {trained_to} of {end}); skipped")
            continue
        commit, number = commit_for_window(steps, windows, w, stream_pos)
        if commit is None:
            commit, number = ROOT_TAG, 0           # nothing learned before this window: the template itself
        split = HERE / "splits" / f"test_w{w}.txt"
        cfg = Config.load(a.config, [f"trainer.run_name={a.run}", f"data.test={split}", *a.overrides])
        upto = max((stream_pos[t] for s in steps if s["step"] <= number for t in s["tasks"] if t in stream_pos),
                   default=0)
        os.environ["SLB_EVIDENCE"] = str(evidence_snapshot(workdir, w, upto))
        print(f"W{w}: eval on commit {commit[:12]} (after step {number}) with {split.name}")
        summary = evaluate(cfg, commit, log=print)
        report.append({"window": w, "commit": commit, "step": number, **summary})
    (workdir / "windows_eval.json").write_text(json.dumps(report, indent=1))
    assemble(workdir, windows["scenario"], a.method)


def assemble(workdir: Path, scenario: str, method: str) -> None:
    """Every row the pipeline wrote, serving then test by window, as one benchmark result file."""
    from servelearnbench.scoring import setting_scores
    rows = []
    for p in sorted((workdir / "rows").glob("*.json")):
        rows.append(json.loads(p.read_text()))
    serving = sorted((r for r in rows if r["phase"] == "serving"), key=lambda r: r["timestamp"])
    tests = sorted((r for r in rows if r["phase"] == "test"), key=lambda r: (r["window"], r["task_id"]))
    model = next((r["model"] for r in rows), "?")
    out = workdir / "results"
    out.mkdir(exist_ok=True)
    path = out / f"{scenario}_{method}.jsonl"
    with open(path, "w") as f:
        f.write(json.dumps({"config": {"scenario": scenario, "method": method, "model": model,
                                       "source": "hermit examples/servelearnbench"}}) + "\n")
        for r in serving + tests:
            r = dict(r)
            r["method"] = method
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    scores = setting_scores(serving + tests)
    by_window: dict = {}
    for r in tests:
        by_window.setdefault(r["window"], []).append(r)
    print(f"\n{path}")
    print(f"serving rows {len(serving)}, test rows {len(tests)}")
    for w in sorted(by_window):
        s = setting_scores(by_window[w])
        h = "–" if s["hidden"] is None else f"{s['hidden']:.1f}"
        fs = "–" if s["fully_specified"] is None else f"{s['fully_specified']:.1f}"
        print(f"W{w}: H {h}  F {fs}  (n={s['n_test']})")
    h = "–" if scores["hidden"] is None else f"{scores['hidden']:.1f}"
    fs = "–" if scores["fully_specified"] is None else f"{scores['fully_specified']:.1f}"
    print(f"setting: H {h}  F {fs}  (n_test={scores['n_test']})")


if __name__ == "__main__":
    sys.exit(main())
