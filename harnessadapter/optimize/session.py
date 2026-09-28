"""One optimizer session, the optimizer counterpart of evolution.session.

    Adapter + Environment + Optimizer Evolver + settings + Training State (optional)
        -> candidate Adapter + updated Training State + result record

Session layout (under output/):

    optimize-<evolver>-XXXX/
      source/            snapshot of the adapter the session started from
      candidate/         the selected adapter (a git repository, like every
                         evolved candidate); deploy it like any other adapter
      training_state/    the plugin's optimizer state (GEPA's gepa_state.bin,
                         SkillOpt's run directory); pass it back to resume
      work/              materialized candidates and rollout records
      control/           settings, environment description, optimizer-model
                         ledger, result.json; report.md at the top

Nothing is promoted automatically and the source adapter is never modified.
The selection split is the environment's val split; test is never touched
here (evaluate_adapters does that, for every method alike).
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import tempfile
import threading
import time
from typing import Callable

from harnessadapter.adapter import versioning
from harnessadapter.optimize.environment import Environment, Rollout, check_rollouts, disjoint_splits
from harnessadapter.optimize.plugin import OptimizeContext, load_optimizer
from harnessadapter.optimize.surface import TextSurface, candidate_hash, copy_adapter


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _save(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


class GuardedEnvironment(Environment):
    """What a plugin actually holds: the session's environment restricted to
    its train and val tasks. Every requested task is resolved by id against
    those registered tasks (a caller cannot relabel a test task as train), and
    every call is counted and logged."""

    def __init__(self, inner: Environment, log_path: Path):
        self._inner = inner
        self.name = inner.name
        self.log_path = log_path
        self.rollouts = 0
        self.calls = 0
        self._lock = threading.Lock()
        self._visible = {t.id: t for split in ("train", "val") for t in inner.tasks(split)}

    def tasks(self, split: str):
        if split not in ("train", "val"):
            raise PermissionError("Optimizers see only the train and val splits")
        return self._inner.tasks(split)

    def evaluate(self, adapter, tasks, *, label):
        requested = list(tasks)
        unknown = [t.id for t in requested if t.id not in self._visible]
        if unknown:
            raise PermissionError(f"Optimizers evaluate only train and val tasks; refused {unknown[:5]}")
        registered = [self._visible[t.id] for t in requested]
        started = time.monotonic()
        rollouts = check_rollouts(registered, self._inner.evaluate(adapter, registered, label=label))
        with self._lock, self.log_path.open("a") as out:
            self.calls += 1
            self.rollouts += len(rollouts)
            out.write(json.dumps({"utc": _utc(), "label": label, "adapter": Path(adapter).name if adapter else None,
                                  "tasks": [t.id for t in registered], "splits": sorted({t.split for t in registered}),
                                  "scores": [r.score for r in rollouts], "statuses": [r.status for r in rollouts],
                                  "infra": sum(1 for r in rollouts if r.infra),
                                  "seconds": round(time.monotonic() - started, 1)}) + "\n")
        return rollouts

    def describe(self):
        return self._inner.describe()


def optimize(source: Path, environment: Environment, evolver: str, *, lm: Callable, output: Path,
             settings: dict | None = None, components: list[str] | None = None,
             training_state: Path | None = None, log: Callable[[str], None] = print) -> dict:
    definition = load_optimizer(evolver)
    merged = definition.merged(settings)
    source = Path(source).resolve()
    sizes = disjoint_splits(environment)
    if not sizes.get("train") or not sizes.get("val"):
        raise ValueError("An optimizer needs nonempty train and val splits")
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    session = Path(tempfile.mkdtemp(prefix=f"optimize-{definition.name}-", dir=output))
    for name in ("source", "training_state", "work", "control"):
        (session / name).mkdir(exist_ok=True)
    copy_adapter(source, session / "source")
    surface = TextSurface(session / "source", components)
    if training_state is not None:
        # Resume: the plugin continues from its own state format.
        shutil.copytree(Path(training_state), session / "training_state", dirs_exist_ok=True)
    if getattr(lm, "ledger", False) is None:
        lm.ledger = session / "control/optimizer-calls"
    guarded = GuardedEnvironment(environment, session / "control/evaluations.jsonl")
    record = {"session": str(session), "source": str(source), "evolver": definition.name,
              "evolver_definition": str(definition.root), "settings": merged,
              "components": surface.components, "environment": environment.describe(),
              "splits": sizes, "optimizer_model": getattr(lm, "describe", lambda: {})(),
              "resumed_from": str(training_state) if training_state else None,
              "seed_hash": candidate_hash(surface.read()), "started_utc": _utc(), "status": "running"}
    _save(session / "control/result.json", record)
    context = OptimizeContext(surface=surface, environment=guarded, lm=lm, state_dir=session / "training_state",
                              work_dir=session / "work", settings=merged, log=log)
    started = time.monotonic()
    try:
        outcome = definition.plugin.run(context)
    except BaseException as exc:
        record.update(status="failed", error=f"{type(exc).__name__}: {exc}", finished_utc=_utc(),
                      rollouts=guarded.rollouts, optimizer_usage=dict(getattr(lm, "usage", {})))
        _save(session / "control/result.json", record)
        raise
    selected = surface.check(outcome.candidate)
    candidate = session / "candidate"
    candidate.mkdir()
    # From the snapshot the candidates were evaluated against; only the
    # revision history comes from the live source.
    copy_adapter(session / "source", candidate)
    versioning.copy_history(source, candidate)
    versioning.ensure_repo(candidate)
    for name, text in selected.items():
        (candidate / name).write_text(text)
    commit = versioning.commit_all(candidate, f"optimize: {definition.name} session {session.name}")
    changed = [n for n in surface.components if selected[n] != surface.read()[n]]
    record.update(status="candidate_ready", finished_utc=_utc(), seconds=round(time.monotonic() - started, 1),
                  candidate=str(candidate), candidate_hash=candidate_hash(selected), candidate_commit=commit,
                  changed_components=changed, rollouts=guarded.rollouts, evaluation_calls=guarded.calls,
                  infra_lost_rollouts=_infra_count(session / "control/evaluations.jsonl"),
                  optimizer_usage=_usage(lm, outcome.summary), plugin=outcome.summary)
    _save(session / "control/result.json", record)
    (session / "report.md").write_text(render_report(record, selected))
    return record


def _infra_count(log: Path) -> int:
    if not log.is_file():
        return 0
    return sum(json.loads(line).get("infra", 0) for line in log.read_text().splitlines() if line.strip())


def _usage(lm, summary: dict) -> dict:
    """Optimizer-model usage: the session's ledger, or the plugin's own
    accounting when it drives its own client (SkillOpt's token tracker)."""
    usage = dict(getattr(lm, "usage", {}))
    own = (summary.get("token_summary") or {}).get("_total")
    if own and not usage.get("calls"):
        usage.update(calls=own.get("calls", 0), prompt_tokens=own.get("prompt_tokens", 0),
                     completion_tokens=own.get("completion_tokens", 0), source="plugin")
    return usage


def render_report(record: dict, selected: dict[str, str]) -> str:
    plugin = record.get("plugin", {})
    lines = [f"# {record['evolver']} optimizer session", "",
             f"- Source: `{record['source']}`",
             f"- Environment: {json.dumps(record['environment'], ensure_ascii=False)}",
             f"- Splits: {record['splits']} (test is never evaluated during optimization)",
             f"- Rollouts used: {record['rollouts']} in {record['evaluation_calls']} evaluation calls "
             f"({record.get('infra_lost_rollouts', 0)} lost to infrastructure, scored 0)",
             f"- Optimizer model: {record['optimizer_model']}; usage {record['optimizer_usage']}",
             f"- Changed components: {record['changed_components'] or 'none (the seed was kept)'}",
             f"- Seed val score: {plugin.get('seed_val_score')}; selected val score: {plugin.get('selected_val_score')}",
             "", "`candidate_ready` means the search finished and produced a loadable adapter;",
             "only a held-out evaluation says whether it is better.", ""]
    for name, text in selected.items():
        lines += [f"## {name}", "", "```markdown", text.rstrip(), "```", ""]
    return "\n".join(lines)


def evaluate_adapters(environment: Environment, adapters: dict[str, Path | None], split: str, *,
                      output: Path, repeats: int = 1) -> dict:
    """Score several adapters on the same split (normally test) with the
    same environment: the comparison every optimizer is judged by. Episodes
    lost to the infrastructure are left out of the mean and counted."""
    tasks = environment.tasks(split)
    results = {}
    for name, adapter in adapters.items():
        runs = []
        for repeat in range(repeats):
            rollouts: list[Rollout] = check_rollouts(tasks, environment.evaluate(
                adapter, tasks, label=f"{split}/{name}/r{repeat + 1}"))
            valid = [r for r in rollouts if not r.infra]
            runs.append({"mean": sum(r.score for r in valid) / len(valid) if valid else None,
                         "scored": len(valid), "infra_lost": [r.task_id for r in rollouts if r.infra],
                         "scores": {r.task_id: r.score for r in rollouts},
                         "statuses": {r.task_id: r.status for r in rollouts}})
        means = [r["mean"] for r in runs if r["mean"] is not None]
        results[name] = {"adapter": str(adapter) if adapter else None, "runs": runs,
                         "mean": sum(means) / len(means) if means else None}
        _save(Path(output) / f"{split}-scores.json", {"split": split, "tasks": [t.id for t in tasks],
                                                        "environment": environment.describe(), "results": results})
    return results
