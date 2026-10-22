"""Learning material: one directory per step in one standard format, index.jsonl, a `current` link."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil

from hermit.isolate import _safe

README = """# Learning material

One directory per training step (`step_0001/`, ...); `current` points at the latest; `index.jsonl` has one line per
step: step, epoch, number of tasks, mean score, rollout cost, and whether the adapter used for these rollouts differed
from the previous step's.

Each step directory:

- `summary.json` — step, epoch, the adapter commit the rollouts used, task ids, mean score, cost, time.
- `tasks/<task id>.json` — for each task: `input` (the task as given to the pipeline), `output` (what the agent
  produced), `judge` (everything the judge returned: score, feedback, anything else), `cost_usd` (null = unknown),
  `wall_s`, `api_speed`, `tokens`, `status` (ok or error), `error`, `trajectory` (where the full trajectory is) and
  `messages` (a normalised message list, when the pipeline provides one).
- `trajectories/<task id>/` — the host's original log, copied unchanged.
- `check_feedback.json` — present when the adapter the previous step handed back failed check_spec: each repaired
  file, where it was restored from, and why.
"""


class Material:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        if not (self.root / "README.md").exists():
            (self.root / "README.md").write_text(README)

    def step_dir(self, step: int) -> Path:
        return self.root / f"step_{step:04d}"

    def write(self, step: int, epoch: int, commit: str, changed: bool, results: list[dict],
              check_feedback: list[dict] | None) -> Path:
        d = self.step_dir(step)
        if d.exists():
            shutil.rmtree(d)
        (d / "tasks").mkdir(parents=True)
        costs = [r["cost_usd"] for r in results if r.get("cost_usd") is not None]
        for r in results:
            record = dict(r)
            log = r.get("log")
            # A link is copied as a link, never followed: the log is the agent's, and what it links to need not be.
            if log and Path(log).exists() and not Path(log).is_symlink():
                target = d / "trajectories" / _safe(r["id"])
                if Path(log).is_dir():
                    shutil.copytree(log, target, symlinks=True)
                else:
                    target.mkdir(parents=True)
                    shutil.copy2(log, target / Path(log).name, follow_symlinks=False)
                record["trajectory"] = os.path.relpath(target, d)
            else:
                record["trajectory"] = None
            record.pop("log", None)
            (d / "tasks" / f"{_safe(r['id'])}.json").write_text(json.dumps(record, ensure_ascii=False, indent=1,
                                                                           default=str))
        scores = [r["judge"]["score"] for r in results if r.get("judge")]
        summary = {"step": step, "epoch": epoch, "adapter_commit": commit, "adapter_changed": changed,
                   "tasks": [r["id"] for r in results], "mean_score": sum(scores) / len(scores) if scores else None,
                   "cost_usd": sum(costs) if costs else None,
                   "unknown_cost_tasks": sum(1 for r in results if r.get("cost_usd") is None),
                   "wall_s": max((r.get("wall_s") or 0) for r in results) if results else 0}
        (d / "summary.json").write_text(json.dumps(summary, indent=1))
        if check_feedback:
            (d / "check_feedback.json").write_text(json.dumps(check_feedback, indent=1))
        with open(self.root / "index.jsonl", "a") as index:
            index.write(json.dumps({k: summary[k] for k in ("step", "epoch", "mean_score", "cost_usd", "adapter_changed")}
                                   | {"tasks": len(results)}) + "\n")
        current = self.root / "current"
        if current.is_symlink() or current.exists():
            current.unlink()
        current.symlink_to(d.name)
        return d

    def truncate(self, last_step: int) -> None:
        """Drop material of steps after `last_step` (a discarded step on resume)."""
        for d in self.root.glob("step_*"):
            if int(d.name.split("_")[1]) > last_step:
                shutil.rmtree(d)
        index = self.root / "index.jsonl"
        if index.exists():
            keep = [l for l in index.read_text().splitlines() if l and json.loads(l)["step"] <= last_step]
            index.write_text("".join(l + "\n" for l in keep))
        current = self.root / "current"
        if current.is_symlink():
            current.unlink()
        if last_step > 0 and self.step_dir(last_step).exists():
            current.symlink_to(self.step_dir(last_step).name)


