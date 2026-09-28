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

import dataclasses
from datetime import datetime, timezone
import hashlib
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


INFRA_RERUNS = 2      # extra rounds for episodes the infrastructure lost
INFRA_PAUSE = 60      # seconds before each round


def rerun_infra(environment: Environment, adapter, tasks: list, rollouts: list, label: str, *,
                rounds: int = INFRA_RERUNS, pause: float = INFRA_PAUSE) -> tuple[list, int]:
    """Re-run the tasks whose episodes the infrastructure lost, up to `rounds`
    more times, so an outage does not decide a score. Returns the rollouts
    and the number of extra episodes run."""
    rollouts = list(rollouts)
    extra = 0
    for round_number in range(1, rounds + 1):
        lost = [i for i, r in enumerate(rollouts) if r.infra]
        if not lost:
            break
        time.sleep(pause)
        again = check_rollouts([tasks[i] for i in lost], environment.evaluate(
            adapter, [tasks[i] for i in lost], label=f"{label}/infra-rerun-{round_number}"))
        extra += len(again)
        for i, rollout in zip(lost, again):
            rollouts[i] = rollout
    return rollouts, extra


def _public_describe(description: dict) -> dict:
    """The environment description a plugin sees: no filesystem locations."""
    return {k: v for k, v in description.items() if not (isinstance(v, str) and v.startswith("/"))}


class GuardedEnvironment(Environment):
    """What a plugin actually holds: the session's environment restricted to
    its train and val tasks. Every requested task is resolved by id against
    those registered tasks, episodes lost to the infrastructure are re-run,
    rollouts are returned without record locations, and every call is
    counted and logged.

    This keeps a well-behaved plugin from using held-out data by accident.
    It is not a sandbox: an optimizer plugin is trusted in-process code."""

    def __init__(self, inner: Environment, log_path: Path, *, infra_reruns: int = INFRA_RERUNS,
                 infra_pause: float = INFRA_PAUSE):
        self._inner = inner
        self.name = inner.name
        self.log_path = log_path
        self.rollouts = 0
        self.calls = 0
        self.infra_reruns = infra_reruns
        self.infra_pause = infra_pause
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
        rollouts, extra = rerun_infra(self._inner, adapter, registered, rollouts, label,
                                      rounds=self.infra_reruns, pause=self.infra_pause)
        with self._lock, self.log_path.open("a") as out:
            self.calls += 1
            self.rollouts += len(rollouts) + extra
            out.write(json.dumps({"utc": _utc(), "label": label, "adapter": Path(adapter).name if adapter else None,
                                  "tasks": [t.id for t in registered], "splits": sorted({t.split for t in registered}),
                                  "scores": [r.score for r in rollouts], "statuses": [r.status for r in rollouts],
                                  "infra": sum(1 for r in rollouts if r.infra), "infra_reruns": extra,
                                  "seconds": round(time.monotonic() - started, 1)}) + "\n")
        return [dataclasses.replace(r, record=None) for r in rollouts]

    def describe(self):
        return _public_describe(self._inner.describe())


def split_fingerprint(environment: Environment) -> dict:
    """Which task ids an optimizer trained and selected on. Stored with the
    Training State, and checked when a session resumes from it."""
    ids = {split: [t.id for t in environment.tasks(split)] for split in ("train", "val", "test")}
    return {"ids": {k: v for k, v in ids.items() if k != "test"},
            "test_sha256": hashlib.sha256(json.dumps(sorted(ids["test"])).encode()).hexdigest(),
            "test_count": len(ids["test"])}


def check_resume_splits(state: Path, current: dict) -> None:
    path = state / "harnessadapter-splits.json"
    if not path.is_file():
        raise ValueError("This Training State records no split assignment; refusing to resume from it")
    stored = json.loads(path.read_text())
    if stored["ids"] != current["ids"]:
        raise ValueError("The train/val assignment differs from the one this Training State was built on")
    if stored["test_sha256"] != current["test_sha256"]:
        raise ValueError("The test split differs from the one this Training State was built on")


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
    # The history the candidate will carry is the source's at this moment.
    versioning.copy_history(source, session / "source")
    surface = TextSurface(session / "source", components)
    fingerprint = split_fingerprint(environment)
    if training_state is not None:
        # Resume: the plugin continues from its own state format, built on
        # the same split assignment.
        check_resume_splits(Path(training_state), fingerprint)
        shutil.copytree(Path(training_state), session / "training_state", dirs_exist_ok=True)
    else:
        _save(session / "training_state/harnessadapter-splits.json", fingerprint)
    if getattr(lm, "ledger", False) is None:
        lm.ledger = session / "control/optimizer-calls"
    guarded = GuardedEnvironment(environment, session / "control/evaluations.jsonl")
    record = {"session": str(session), "source": str(source), "evolver": definition.name,
              "evolver_definition": str(definition.root), "settings": merged,
              "components": surface.components, "environment": environment.describe(),
              "splits": sizes, "split_test_sha256": fingerprint["test_sha256"],
              "optimizer_model": getattr(lm, "describe", lambda: {})(),
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
    versioning.copy_history(session / "source", candidate)
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
                      output: Path, repeats: int = 1, infra_reruns: int = INFRA_RERUNS,
                      infra_pause: float = INFRA_PAUSE) -> dict:
    """Score several adapters on the same split (normally test) with the
    same environment: the comparison every optimizer is judged by. Episodes
    lost to the infrastructure are re-run; any still lost are left out of the
    adapter's mean and listed. paired_mean is over the tasks every adapter
    and repeat scored, the like-for-like comparison."""
    tasks = environment.tasks(split)
    results = {}

    def save():
        common = [t.id for t in tasks if all(t.id not in run["infra_lost"] for r in results.values() for run in r["runs"])]
        for r in results.values():
            per_run = [sum(run["scores"][t] for t in common) / len(common) for run in r["runs"]] if common else []
            r["paired_mean"] = sum(per_run) / len(per_run) if per_run else None
        _save(Path(output) / f"{split}-scores.json", {"split": split, "tasks": [t.id for t in tasks],
                                                        "paired_tasks": len(common),
                                                        "environment": environment.describe(), "results": results})

    for name, adapter in adapters.items():
        runs = []
        for repeat in range(repeats):
            label = f"{split}/{name}/r{repeat + 1}"
            rollouts = check_rollouts(tasks, environment.evaluate(adapter, tasks, label=label))
            rollouts, _ = rerun_infra(environment, adapter, tasks, rollouts, label, rounds=infra_reruns, pause=infra_pause)
            valid = [r for r in rollouts if not r.infra]
            runs.append({"mean": sum(r.score for r in valid) / len(valid) if valid else None,
                         "scored": len(valid), "infra_lost": [r.task_id for r in rollouts if r.infra],
                         "scores": {r.task_id: r.score for r in rollouts},
                         "statuses": {r.task_id: r.status for r in rollouts}})
        means = [r["mean"] for r in runs if r["mean"] is not None]
        results[name] = {"adapter": str(adapter) if adapter else None, "runs": runs,
                         "mean": sum(means) / len(means) if means else None}
        save()
    return results
