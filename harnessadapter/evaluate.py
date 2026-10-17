"""`harnessadapter eval`: one adapter on the test split, with the same pipeline and judge as training."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import time

from harnessadapter.adapter.manifest import MANIFEST
from harnessadapter.config import Config
from harnessadapter.agents.base import load_agent
from harnessadapter.task import TaskData, load_callable
from harnessadapter.trainer.budget import Budget
from harnessadapter.trainer.rollout import run_tasks
from harnessadapter.tree import ROOT_TAG, Tree


def evaluate(config: Config, adapter: str, *, log=print) -> dict:
    d = config.data
    data = TaskData(config.path(d["data"]["tasks"]), config.path(d["data"]["train"]),
                    config.path(d["data"]["val"]), config.path(d["data"]["test"]))
    tests = data.rows("test")
    if not tests:
        raise SystemExit("eval needs data.test")
    pipeline = load_agent(d["agent"]["name"], d["agent"].get("params"), config.base).run
    judge = load_callable(f"{d['judge']['path']}:{d['judge']['name']}", config.base)
    run_name = config.run_name()
    run_dir = config.run_dir()
    from harnessadapter.adapter.manifest import read_manifest
    template = read_manifest(config.template_dir())["template"]
    tree_dir = config.tree_dir(template["id"])
    tmp = Path(tempfile.mkdtemp(prefix="ha-eval-"))
    if adapter == "none":
        target, label, commit = None, "none", None
    else:
        as_path = config.path(adapter)
        if as_path is not None and (as_path / MANIFEST).is_file():
            target, label, commit = as_path, as_path.name, None
        else:
            tree = Tree(tree_dir)
            ref = {"final": f"{run_name}/final", "root": ROOT_TAG}.get(adapter, adapter)
            commit = tree.resolve(ref)
            if not tree.is_valid(commit):
                raise SystemExit(f"{adapter} is an invalid (unrepaired) commit, not an adapter")
            target, label = tree.export(commit, tmp / "adapter"), adapter.replace("/", "_")
    repeats = int(d["eval"]["repeats"])
    out = run_dir / "eval" / label
    out.mkdir(parents=True, exist_ok=True)
    budget = Budget()
    t0 = time.time()
    per_task: dict[str, list[float]] = {t["id"]: [] for t in tests}
    errors = 0                       # runs the pipeline or judge failed on (scored 0)
    with open(out / "results.jsonl", "w") as f:
        for r in range(1, repeats + 1):
            log(f"eval {label}: repeat {r}/{repeats} on {len(tests)} test tasks")
            for rec in run_tasks(tests, target, pipeline, judge, concurrency=int(d["agent"]["concurrency"]),
                                 budget=budget, part="rollout"):
                per_task[rec["id"]].append(rec["judge"]["score"])
                errors += rec["status"] != "ok"
                f.write(json.dumps({"repeat": r, **{k: rec[k] for k in ("id", "status", "error", "output", "judge",
                                    "cost_usd", "wall_s", "api_speed", "tokens", "log")}}, default=str) + "\n")
    scores = [s for v in per_task.values() for s in v]
    spent = budget.snapshot()
    summary = {"adapter": adapter, "commit": commit, "tasks": len(tests), "repeats": repeats,
               "mean_score": sum(scores) / len(scores) if scores else 0.0,
               "solved_once": sum(1 for v in per_task.values() if any(s >= 1.0 for s in v)) / len(tests),
               "cost_usd": spent["usd"]["rollout"] if not spent["unknown_cost_calls"] else None,
               "unknown_cost_tasks": spent["unknown_cost_calls"], "errors": errors, "wall_s": round(time.time() - t0, 1)}
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    if adapter == "final":
        _update_card(Tree(tree_dir), run_name, run_dir, summary)
    return summary


def _update_card(tree: Tree, run_name: str, run_dir: Path, summary: dict) -> None:
    """Rewrite the final adapter's card with the evaluation results: a new commit on the run's branch, re-tagged."""
    checkout = run_dir / "checkout"
    if not checkout.exists():
        return
    card = checkout / "CARD.md"
    if not card.exists():
        return
    text = card.read_text()
    head, _, rest = text.partition("## Evaluation")
    tail = rest.partition("\nWhere it has not been tested")[2]
    table = ("\n\n| Adapter | Tasks | Repeats | Mean score | Solved at least once | Cost (USD) |\n|---|---|---|---|---|---|\n"
             f"| final | {summary['tasks']} | {summary['repeats']} | {summary['mean_score']:.3f} | "
             f"{summary['solved_once']:.3f} | "
             + ("unknown" if summary["cost_usd"] is None else f"{summary['cost_usd']:.4f}") + " |\n")
    card.write_text(head + "## Evaluation" + table + "\nWhere it has not been tested" + tail)
    if Tree.commit(checkout, f"card: eval results of {run_name}/final\n"):
        tree.tag(f"{run_name}/final", Tree.head(checkout), force=True)
