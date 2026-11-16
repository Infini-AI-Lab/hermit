"""`hermit eval`: one adapter on the test split, with the same pipeline and judge as training.

What a summary reports: `mean_score` over the scored runs (a run that ended `infra`, not the agent's result, is left
out and counted in `unscored`); `solved_once`, the share of the test rows with a scored run that a scored run solved
(a score of at least 1); `errors`, the runs the pipeline or the judge failed (scored 0). With eval.repeats > 1 each
repeat hands the pipeline copies of its rows that name it (`repeat`). `eval.infra_retries` runs the infra rows again
(`eval.infra_wait_s` before each pass), unless one reports a halt (a lost login, a usage limit: a person restores
it); a step of a multi-step task is never run again alone (its task's stream runs its steps in turn), nor is a task the
acting agent never runs (Harbor's `unsupported_task`: it would be refused again), and a test split that lists one
ahead of an earlier step of its stream (one it lists after it or not at all) is refused before any run (_rows).
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
import time

from hermit.adapter.manifest import CARD, MANIFEST
from hermit.config import Config
from hermit.isolate import agent_pipeline, judge_for
from hermit.redact import redact_path, redact_value
from hermit.task import TaskData
from hermit.trainer.budget import Budget
from hermit.trainer.card import number
from hermit.trainer.rollout import REPEAT, own_row_keys, reports_halt, run_tasks
from hermit.tree import ROOT_TAG, Tree

RESULT_KEYS = ("id", "status", "error", "output", "judge", "cost_usd", "wall_s", "api_speed", "tokens", "log")


def evaluate(config: Config, adapter: str, *, log=print) -> dict:
    """`hermit eval --adapter <adapter>`: the adapter on data.test, eval.repeats times; the results in
    <run>/eval/<label>/. The final adapter's results also go into its card."""
    d = config.data
    path = config.path(d["data"]["test"])
    if path is None:
        raise SystemExit("eval needs data.test")
    tests = _rows(config, path)
    if not tests:
        raise SystemExit(f"eval needs data.test: {path} names no task")
    tmp = Path(tempfile.mkdtemp(prefix="ha-eval-"))
    try:
        target, label, commit = _resolve(config, adapter, tmp)
        out = config.run_dir() / "eval" / label
        summary = _run(config, target, tests, out, label=label, log=log)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    summary = {"adapter": adapter, "commit": commit, **summary}
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    if adapter == "final":
        _update_card(Tree(_tree_dir(config)), config.run_name(), config.run_dir(), summary)
    return summary


def _rows(config: Config, split: Path) -> list[dict]:
    """The rows of a split file (its ids checked against data.tasks), in the order they run: refused before any run
    when it lists a step of a multi-step task ahead of an earlier step of its stream (one it lists after it or not at
    all), which it would wait for (hermit.trainer.trainer.out_of_step)."""
    from hermit.trainer.trainer import out_of_step
    data = TaskData(config.path(config.data["data"]["tasks"]), None, None, split)
    problem = out_of_step(data.splits["test"], data.tasks, f"the split {split}")
    if problem:
        raise SystemExit(f"eval refuses to start: {problem}")
    return data.rows("test")


def _tree_dir(config: Config) -> Path:
    """The run's tree: trainer.tree_dir, else the template's under trainer.output_dir."""
    if config.data["trainer"]["tree_dir"]:
        return config.path(config.data["trainer"]["tree_dir"])
    if not config.data["template"]:
        raise SystemExit("an adapter of the run's tree (final, root, a tag, a commit) needs template (or "
                         "trainer.tree_dir, a tree that exists): the tree is the template's")
    from hermit.adapter.manifest import read_manifest
    return config.tree_dir(read_manifest(config.template_dir())["template"]["id"])


def _resolve(config: Config, adapter: str, tmp: Path) -> tuple[Path | None, str, str | None]:
    """(the adapter's directory, its label, its commit): none; a directory holding an adapter (relative to the
    config); final, root, a tag or a commit of the run's tree, exported into `tmp`. An acting agent that takes no
    adapter (one of Harbor's own, Config.harbor_builtin) is refused any but none."""
    if adapter == "none":
        return None, "none", None
    builtin = config.harbor_builtin()
    if builtin is not None:
        raise SystemExit(f"the acting agent {builtin} (agent.params.acting_agent) is one of Harbor's own agents, which "
                         f"takes no adapter: evaluate it with --adapter none")
    as_path = config.path(adapter)
    if as_path is not None and (as_path / MANIFEST).is_file():
        return as_path, as_path.name, None
    where = _tree_dir(config)
    if not (where / ".git").exists():
        raise SystemExit(f"{adapter} names an adapter of a training run's tree, and there is none at {where} (no "
                         f"hermit train ran): give none or an adapter directory")
    tree = Tree(where)
    ref = {"final": f"{config.run_name()}/final", "root": ROOT_TAG}.get(adapter, adapter)
    try:
        commit = tree.resolve(ref)
    except RuntimeError:
        raise SystemExit(f"{adapter} is no adapter of the run's tree at {where} (final: a finished hermit train of "
                         f"{config.run_name()}; root, a tag or a commit)") from None
    if not tree.is_valid(commit):
        raise SystemExit(f"{adapter} is an invalid (unrepaired) commit, not an adapter")
    return tree.export(commit, tmp / "adapter"), adapter.replace("/", "_"), commit


def _run(config: Config, target: Path | None, tests: list[dict], out: Path, *, label: str, log) -> dict:
    """The tests through the eval pipeline and judge, eval.repeats times, the infra runs again as eval.infra_retries
    says: the summary's counts, with `halted` when a run reported a halt. results.jsonl keeps each row's latest run
    (an infra run that ran again is replaced by its rerun); the summary's cost counts every run, the replaced ones
    too, and `infra_rerun` how many ran again."""
    d = config.data
    repeats = int(d["eval"]["repeats"])
    retries, wait = int(d["eval"].get("infra_retries") or 0), float(d["eval"].get("infra_wait_s") or 0)
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.jsonl").write_text("")        # an earlier eval's results are not this one's, should this one fail,
    (out / "summary.json").unlink(missing_ok=True)       # nor is its summary
    named = repeats > 1 and _harbor_rows(config)             # each repeat's rows name it (_repeat)
    added = (REPEAT,) if named else ()
    problems = own_row_keys(tests, added, "the test split") if named else []
    if problems:
        raise SystemExit("eval refuses to start: " + "; ".join(problems))
    pipeline, close = agent_pipeline(config, "eval", records=out / "sandbox", deploys_adapters=target is not None)
    try:
        judge = judge_for(config, pipeline)
    except BaseException:
        close()                      # the pipeline's sandbox and proxies, before the error leaves
        raise
    budget = Budget()
    t0 = time.time()
    by_id = {t["id"]: t for t in tests}
    # (repeat, task id, which of the split's rows of that id) -> the latest run's record: a split that names a task
    # twice keeps a row for each, as on main
    records: dict[tuple[int, str, int], dict] = {}
    rerun = 0
    concurrency = int(d["agent"]["concurrency"])
    try:
        for r in range(1, repeats + 1):
            log(f"eval {label}: repeat {r}/{repeats} on {len(tests)} test tasks")
            seen: dict[str, int] = {}
            for rec in run_tasks(_repeat(tests, r, named), target, pipeline, judge, concurrency=concurrency,
                                 budget=budget, part="rollout", added=added):
                records[(r, rec["id"], seen.setdefault(rec["id"], 0))] = rec
                seen[rec["id"]] += 1
            _write(out, records)
        # A step of a multi-step task runs in its task's stream, after the steps before it: alone its turn could not
        # come, and it would end infra again at once. Its infra run is kept as it is.
        steps = {key for key, rec in records.items() if rec["status"] == "infra" and _one_step_of_many(by_id[key[1]])}
        if retries and steps:
            log(f"eval {label}: {len(steps)} infra run(s) are steps of multi-step tasks, not run again: a step runs "
                "only in its task's stream, after the steps before it")
        # A task the acting agent never runs (Harbor runs it for none of Hermit's agents) is refused again on a rerun.
        never = {key for key, rec in records.items() if rec["status"] == "infra" and _unrunnable(rec)} - steps
        if retries and never:
            log(f"eval {label}: {len(never)} infra run(s) are of tasks Harbor runs for none of Hermit's agents "
                "(unsupported_task), not run again: each would be refused the same way")
        for attempt in range(1, retries + 1):
            infra = [key for key, rec in records.items() if rec["status"] == "infra"]
            again = [key for key in infra if key not in steps and key not in never]
            if not again:
                break
            halted = next((records[key] for key in infra if reports_halt(records[key])), None)
            if halted is not None:
                log(f"eval {label}: {len(infra)} infra run(s) left, not run again: {halted.get('error')} (a halt: "
                    "restore the login or wait for the window, then run hermit eval again)")
                break
            log(f"eval {label}: {len(again)} infra run(s); running them again in {wait:g} s "
                f"(retry {attempt}/{retries})")
            time.sleep(wait)
            for r in sorted({key[0] for key in again}):
                slots: dict[str, list] = {}                 # each rerun of an id replaces one of its infra runs
                for key in (k for k in again if k[0] == r):
                    slots.setdefault(key[1], []).append(key)
                for rec in run_tasks(_repeat([by_id[key[1]] for key in again if key[0] == r], r, named), target,
                                     pipeline, judge, concurrency=concurrency, budget=budget, part="rollout",
                                     added=added):
                    records[slots[rec["id"]].pop(0)] = rec
            rerun += len(again)
            _write(out, records)
    finally:
        close()
        from hermit.isolate import config_paths
        for place in config_paths(config, "environment.write", d["environment"].get("write")):
            redact_path(Path(place).resolve())       # what the agents wrote there: no model key kept
    summary = _counts(tests, list(records.values()), repeats, budget) | {"wall_s": round(time.time() - t0, 1)}
    if retries:
        summary["infra_rerun"] = rerun
    halted = next((rec for rec in records.values() if rec["status"] == "infra" and reports_halt(rec)), None)
    if halted is not None:          # a lost login, a usage limit: the runs after it returned at once, unrun
        summary["halted"] = str(halted.get("error") or "a halt")
    return summary


def _write(out: Path, records: dict) -> None:
    """results.jsonl: one line per row of each repeat, its latest run, repeat by repeat in the split's order, no model
    key kept."""
    rows = [redact_value({"repeat": r, **{k: rec.get(k) for k in RESULT_KEYS}}) for (r, *_), rec in records.items()]
    with open(out / "results.jsonl", "w") as f:
        for row in rows:
            f.write(json.dumps(row, default=str) + "\n")


def _harbor_rows(config: Config) -> bool:
    """Whether the eval's pipeline is a Harbor task set's (`agent.name: harbor`), whose rows name their repeat: its
    trial cache keeps its trials by row, and each multi-step task's row of a repeat is a stream of its own."""
    return str(config.data["agent"].get("name")) == "harbor"


def _repeat(rows: list[dict], r: int, named: bool) -> list[dict]:
    """The rows repeat `r` hands the pipeline: where they name it (`named`: a Harbor task set's, with eval.repeats > 1),
    copies that do (`repeat`), so that its trial cache runs each repeat anew and each repeat of a multi-step task is a
    stream of its own; else the rows themselves, as on main. No record's `input` keeps it (run_tasks' `added`)."""
    return [{**row, REPEAT: r} for row in rows] if named else rows


def _one_step_of_many(row: dict) -> bool:
    """Whether a row is a step of a multi-step task (hermit.harbor.dataset's step rows: a `harbor_task` with `steps`
    > 1, as the Trainer's _step_of_many reads them; another task's own `steps` field is no such thing)."""
    return bool(row.get("harbor_task")) and isinstance(row.get("steps"), int) and row["steps"] > 1


def _unrunnable(record: dict) -> bool:
    """Whether an infra run is of a task its acting agent never runs (hermit.harbor: a task Harbor runs for none of
    Hermit's agents, `unsupported_task`, never started), which a rerun would only repeat."""
    return (record.get("pipeline_extra") or {}).get("failure_cause") == "unsupported_task"


def _counts(tests: list[dict], records: list[dict], repeats: int, budget: Budget) -> dict:
    got = [r for r in records if r["status"] != "infra"]
    scores = [r["judge"]["score"] for r in got]
    by_id: dict[str, list[float]] = {}
    for r in got:
        by_id.setdefault(r["id"], []).append(r["judge"]["score"])
    spent = budget.snapshot()
    return {"tasks": len(tests), "repeats": repeats,
            "mean_score": sum(scores) / len(scores) if scores else None,
            "solved_once": sum(1 for v in by_id.values() if any(s >= 1.0 for s in v)) / len(by_id) if by_id else None,
            "scored": len(got), "unscored": len(records) - len(got),
            "cost_usd": spent["usd"]["rollout"] if not spent["unknown_cost_calls"] else None,
            "unknown_cost_tasks": spent["unknown_cost_calls"],
            "errors": sum(1 for r in records if r["status"] in ("error", "judge_error"))}


def _update_card(tree: Tree, run_name: str, run_dir: Path, summary: dict) -> None:
    """Rewrite the final adapter's card with the evaluation results: a new commit on the run's branch, re-tagged."""
    checkout = run_dir / "checkout"
    if not checkout.exists():
        return
    card = checkout / CARD
    if not card.exists():
        return
    text = card.read_text()
    head, _, rest = text.partition("## Evaluation")
    tail = rest.partition("\nWhere it has not been tested")[2]
    table = ("\n\n| Adapter | Tasks | Repeats | Mean score | Solved at least once | Cost (USD) |\n|---|---|---|---|---|---|\n"
             f"| final | {summary['tasks']} | {summary['repeats']} | {number(summary['mean_score'])} | "
             f"{number(summary['solved_once'])} | "
             + ("unknown" if summary["cost_usd"] is None else f"{summary['cost_usd']:.4f}") + " |\n")
    if summary.get("unscored"):
        table += f"\n{summary['unscored']} run(s) not scored (infra), left out of the scores.\n"
    card.write_text(head + "## Evaluation" + table + "\nWhere it has not been tested" + tail)
    if Tree.commit(checkout, f"card: eval results of {run_name}/final\n"):
        tree.tag(f"{run_name}/final", Tree.head(checkout), force=True)
