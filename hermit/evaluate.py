"""`hermit eval`: one adapter on the test split, with the same pipeline and judge as training; and a stream's
checkpoint tests (`run.checkpoints`, `hermit eval --checkpoints`), which take the same path.

What a summary reports: `mean_score` over the scored runs (a run that ended `infra`, not the agent's result, is left
out and counted in `unscored`); `solved_once`, the share of the test rows (with a scored run) that a scored run
solved; `pass_at_k`, the same by task: a Harbor task's rows (`<task>#1..k`, named by `output.harbor_task`) and the
repeats are its k attempts, a step of a multi-step task (`<task>:<step>[#k]`, its row naming the `step`) is a task
of its own whose attempts are its rollouts, and any other row is a task of its own; `errors`, the runs the pipeline
or the judge failed (scored 0). "Solved" is the judge's `passed` when it is true or false; a judge that returns
`passed: None` (a benchmark whose reward is a scale with no pass) leaves every row unknown, a row it gave no verdict
(an error) included, and neither share is computed; else (no `passed`, or one that is no bool: ASPIRE's count of the
seeds passed) a score of at least 1. With eval.repeats > 1 each repeat hands the pipeline copies of its rows that
name it (`repeat`). `eval.infra_retries` runs the infra rows again (`eval.infra_wait_s` before each pass), unless one
reports a halt (a lost login, a usage limit: a person restores it); a step of a multi-step task is never run again
alone (its task's stream runs its steps in turn), and a split that lists one ahead of an earlier step of its stream
is refused before any run (_rows).
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import time

from hermit.adapter.manifest import CARD, MANIFEST
from hermit.config import Config
from hermit.isolate import agent_pipeline, judge_for
from hermit.redact import redact_path, redact_value
from hermit.task import TaskData, load_callable
from hermit.trainer.budget import Budget
from hermit.trainer.card import number
from hermit.trainer.rollout import run_tasks
from hermit.tree import ROOT_TAG, Tree

RESULT_KEYS = ("id", "status", "error", "output", "judge", "cost_usd", "wall_s", "api_speed", "tokens", "log")


def evaluate(config: Config, adapter: str, *, log=print, split: str | Path | None = None) -> dict:
    """`hermit eval --adapter <adapter> [--split <file>]`: the adapter on data.test, or on `split` (a split file),
    eval.repeats times; the results in <run>/eval/<label>/ (`<label>-<the split file's name>` for another split). The
    final adapter on data.test also goes into its card and the finished run's summary.json (`test`)."""
    d = config.data
    # eval.repeats as the config's loading reads it for hermit eval ("3" and 2.0 are 3 and 2): a training config the
    # Python API loaded (no command) was not checked for it there
    given = (d.get("eval") or {}).get("repeats")
    try:
        repeats = int(given)
    except (TypeError, ValueError):
        repeats = 0
    if repeats < 1:
        raise ValueError(f"eval.repeats must be a whole number of at least 1, not {given!r}")
    path = Path(split).expanduser().resolve() if split else config.path(d["data"]["test"])
    if path is None:
        raise SystemExit("eval needs data.test (or --split <file>)")
    tests = _rows(config, path)
    if not tests:
        raise SystemExit(f"eval needs data.test: {path} names no task")
    tmp = Path(tempfile.mkdtemp(prefix="ha-eval-"))
    try:
        target, label, commit = _resolve(config, adapter, tmp)
        if split:
            label += f"-{_safe(path.stem)}"
        out = config.run_dir() / "eval" / label
        summary, _ = _run(config, target, tests, out, label=label, log=log)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    summary = {"adapter": adapter, "commit": commit, **summary, **({"split": str(path)} if split else {})}
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    if adapter == "final" and not split:
        _update_card(Tree(_tree_dir(config)), config.run_name(), config.run_dir(), summary)
        _record(config.run_dir(), "test", summary)
    return summary


def checkpoint_tests(config: Config, *, names: list[str] | None = None, log=print) -> dict:
    """The run's checkpoint tests (run.checkpoints) that are due and have no results yet: for each checkpoint the
    run reached, the commit after the step whose tasks end with its `after` row (`end`: the final adapter, once the
    run finished; a run that finished with no step recorded reached none, and the log says so), tested on its split
    for each of its adapters (checkpoint: that commit; final; root), with run.state's environment around the tests
    and run.score's scores of each. Results in <run>/checkpoints/<name>/<adapter>/ (label
    `checkpoint-<name>-<adapter>`); one whose summary.json exists is kept, not run again. `names`: only these
    checkpoints (the Trainer's, right after the step that reaches them). Writes <run>/checkpoints.json (and, for a
    finished run, summary.json's `checkpoint_tests`)."""
    from hermit.trainer.trainer import straddle
    run_dir, run_name = config.run_dir(), config.run_name()
    finished = (run_dir / "summary.json").is_file()
    recorded = (run_dir / "steps.jsonl").is_file()
    if not recorded and not finished:
        raise SystemExit(f"no run at {run_dir} (no steps.jsonl): its checkpoint tests follow hermit train")
    if not recorded:                      # it finished before its first step was recorded (a budget during step 1)
        log(f"checkpoint tests of {run_name}: the run recorded no step, so no checkpoint has a commit to test")
    # "\n" alone ends a step's record: its strings (a learner's summary) may hold U+0085, U+2028 or U+2029 unescaped
    # (hermit.trainer.trainer._completed_steps)
    steps = [json.loads(line) for line in (run_dir / "steps.jsonl").read_text().split("\n") if line.strip()] \
        if recorded else []
    steps = [s for s in steps if not s.get("bootstrap")]
    run = config.data.get("run") or {}
    state = load_callable(run["state"], config.base) if run.get("state") else None
    scorer = load_callable(run["score"], config.base) if run.get("score") else None
    entries: list[dict] = []
    for c in config.checkpoints():
        entry = {"name": c["name"], "after": c["after"], "split": str(c["split"]) if c["split"] else None}
        where = {"reached": False}
        problem = straddle(c, steps) if c["after"] != "end" else None
        if c["after"] == "end":
            if finished and recorded:
                where = {"reached": True, "step": steps[-1]["step"] if steps else 0,
                         "commit": Tree(_tree_dir(config)).resolve(f"{run_name}/final"),
                         "record": steps[-1] if steps else None}
        elif problem:
            where = {"reached": False, "error": problem}
        else:
            hit = next((s for s in steps if c["after"] in s["tasks"]), None)
            if hit is not None:
                where = {"reached": True, "step": hit["step"], "commit": hit["commit"], "record": hit}
        entry |= {k: v for k, v in where.items() if k != "record"}
        place = run_dir / "checkpoints" / c["name"]
        due = [a for a in c["adapters"] if where["reached"] and not (place / a / "summary.json").is_file()
               and (a != "final" or finished) and (names is None or c["name"] in names)]
        env = _state(state, run_dir, where) if due and state is not None else {}
        results = {}
        with _environ(env):
            for a in c["adapters"]:
                done = place / a / "summary.json"
                if done.is_file():
                    results[a] = json.loads(done.read_text())
                elif a in due:
                    results[a] = _checkpoint_test(config, c, a, where["commit"], place / a, scorer, log)
                else:
                    results[a] = {"pending": "the run has not reached it" if not where["reached"] else
                                  "after training" if a == "final" and not finished else "not run yet"}
        entry["adapters"] = results
        entries.append(entry)
        (run_dir / "checkpoints.json").write_text(json.dumps({"run": run_name, "checkpoints": entries}, indent=1,
                                                             default=str))
    overview = {e["name"]: {a: r.get("mean_score") for a, r in e["adapters"].items() if "pending" not in r}
                for e in entries}
    if finished:
        _record(run_dir, "checkpoint_tests", overview)
    return {"checkpoints": entries, "overview": overview}


def _checkpoint_test(config: Config, c: dict, adapter: str, commit: str, out: Path, scorer, log) -> dict:
    """One adapter of a checkpoint on its split: its summary (with run.score's scores), written last, which marks it
    done."""
    label = f"checkpoint-{c['name']}-{adapter}"
    ref = {"checkpoint": commit, "final": "final", "root": "root"}[adapter]
    tests = _rows(config, c["split"])
    tmp = Path(tempfile.mkdtemp(prefix="ha-eval-"))
    try:
        target, _, resolved = _resolve(config, ref, tmp)
        summary, rows = _run(config, target, tests, out, label=label, log=log)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    summary = {"adapter": adapter, "label": label, "commit": resolved, "split": str(c["split"]), **summary}
    if scorer is not None:
        scores = scorer(rows, c["name"])
        if not isinstance(scores, dict):
            raise ValueError(f"run.score returned {type(scores).__name__}, not a mapping of the benchmark's scores")
        summary["score"] = redact_value(scores)
    (out / "summary.json").write_text(json.dumps(summary, indent=1, default=str))
    return summary


def _state(state, run_dir: Path, where: dict) -> dict:
    """run.state's environment for a checkpoint's tests: {name: value} (a value None unsets it)."""
    env = state(run_dir, where.get("record"), where["commit"])
    if env is None:
        return {}
    if not isinstance(env, dict) or not all(isinstance(k, str) for k in env):
        raise ValueError(f"run.state returned {type(env).__name__}, not a mapping of environment variables")
    return env


@contextmanager
def _environ(overrides: dict):
    """The process's environment with `overrides` while the block runs (the eval's pipeline passes the ones
    environment.env names), restored after."""
    saved = {k: os.environ.get(k) for k in overrides}
    try:
        for k, v in overrides.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = str(v)
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _rows(config: Config, split: Path) -> list[dict]:
    """The rows of a split file (its ids checked against data.tasks), in the order they run: refused before any run
    when it lists a step of a multi-step task ahead of an earlier step of its stream, which it would wait for
    (hermit.trainer.trainer.out_of_step)."""
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
        if adapter != "root":
            raise SystemExit(f"{adapter} names an adapter of a training run's tree, and there is none at {where} (no "
                             f"hermit train ran): give root (the template's adapter), none, or an adapter directory")
        if not config.data["template"]:
            raise SystemExit(f"root on a tree that does not exist yet ({where}, trainer.tree_dir) needs template, "
                             "whose adapter begins the tree as hermit train begins it: give template, or a "
                             "trainer.tree_dir that holds a tree")
        Tree.open_or_create(where, config.template_dir())    # the template's adapter, as hermit train begins its tree
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


def _run(config: Config, target: Path | None, tests: list[dict], out: Path, *, label: str, log) -> tuple[dict, list]:
    """The tests through the eval pipeline and judge, eval.repeats times, the infra runs again as eval.infra_retries
    says; (the summary's counts, the results as results.jsonl keeps them)."""
    d = config.data
    repeats = int(d["eval"]["repeats"])
    retries, wait = int(d["eval"].get("infra_retries") or 0), float(d["eval"].get("infra_wait_s") or 0)
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.jsonl").write_text("")        # an earlier eval's results are not this one's, should this one fail
    pipeline, close = agent_pipeline(config, "eval", records=out / "sandbox", deploys_adapters=target is not None)
    try:
        judge = judge_for(config, pipeline)
    except BaseException:
        close()                      # the pipeline's sandbox and proxies, before the error leaves
        raise
    budget = Budget()
    t0 = time.time()
    by_id = {t["id"]: t for t in tests}
    records: dict[tuple[int, str], dict] = {}       # (repeat, task id) -> the latest run's record
    rerun = 0
    concurrency = int(d["agent"]["concurrency"])
    try:
        for r in range(1, repeats + 1):
            log(f"eval {label}: repeat {r}/{repeats} on {len(tests)} test tasks")
            for rec in run_tasks(_repeat(tests, r, repeats), target, pipeline, judge, concurrency=concurrency,
                                 budget=budget, part="rollout"):
                records[(r, rec["id"])] = rec
            rows = _write(out, records)
        # A step of a multi-step task runs in its task's stream, after the steps before it: alone it would start the
        # stream again and wait for a turn that never comes. Its infra run is kept as it is.
        steps = {key for key, rec in records.items() if rec["status"] == "infra" and _one_step_of_many(by_id[key[1]])}
        if retries and steps:
            log(f"eval {label}: {len(steps)} infra run(s) are steps of multi-step tasks, not run again: a step runs "
                "only in its task's stream, after the steps before it")
        for attempt in range(1, retries + 1):
            infra = [key for key, rec in records.items() if rec["status"] == "infra"]
            again = [key for key in infra if key not in steps]
            if not again:
                break
            halted = next((records[key] for key in infra if _halted(records[key])), None)
            if halted is not None:
                log(f"eval {label}: {len(infra)} infra run(s) left, not run again: {halted.get('error')} (a halt: "
                    "restore the login or wait for the window, then run hermit eval again)")
                break
            log(f"eval {label}: {len(again)} infra run(s); running them again in {wait:g} s "
                f"(retry {attempt}/{retries})")
            time.sleep(wait)
            for r in sorted({key[0] for key in again}):
                for rec in run_tasks(_repeat([by_id[i] for rr, i in again if rr == r], r, repeats), target, pipeline,
                                     judge, concurrency=concurrency, budget=budget, part="rollout"):
                    records[(r, rec["id"])] = rec
            rerun += len(again)
            rows = _write(out, records)
    finally:
        close()
        from hermit.isolate import config_paths
        for place in config_paths(config, "environment.write", d["environment"].get("write")):
            redact_path(Path(place).resolve())       # what the agents wrote there: no model key kept
    summary = _counts(tests, list(records.values()), repeats, budget) | {"wall_s": round(time.time() - t0, 1)}
    if retries:
        summary["infra_rerun"] = rerun
    return summary, rows


def _write(out: Path, records: dict) -> list[dict]:
    """results.jsonl: one line per run, repeat by repeat in the split's order, no model key kept."""
    rows = [redact_value({"repeat": r, **{k: rec.get(k) for k in RESULT_KEYS}}) for (r, _), rec in records.items()]
    with open(out / "results.jsonl", "w") as f:
        for row in rows:
            f.write(json.dumps(row, default=str) + "\n")
    return rows


def _repeat(rows: list[dict], r: int, repeats: int) -> list[dict]:
    """The rows repeat `r` hands the pipeline: with eval.repeats > 1, copies that name it (`repeat`), so that a
    pipeline keeping its trials by row (Harbor's trial cache) runs each repeat anew; else the rows themselves."""
    return [{**row, "repeat": r} for row in rows] if repeats > 1 else rows


def _one_step_of_many(row: dict) -> bool:
    """Whether a row is a step of a multi-step task (hermit.harbor.dataset's step rows: a `harbor_task` with `steps`
    > 1, as the Trainer's _step_of_many reads them; another task's own `steps` field is no such thing)."""
    return bool(row.get("harbor_task")) and isinstance(row.get("steps"), int) and row["steps"] > 1


def _halted(record: dict) -> bool:
    """Whether an infra run reports a halt (hermit.harbor: a lost login, a usage limit; every rollout returns at once
    until a person restores it), which a rerun would only repeat."""
    extra = record.get("pipeline_extra") or {}
    return bool(extra.get("halt")) or "halted" in (extra.get("failure_cause"), record.get("error"))


def _solved(record: dict, scale: bool = False) -> bool | None:
    """Whether a run solved its task: the judge's `passed` when it is true or false; None (unknown) when it is None,
    or the run's judge reports a scale (`scale`: it returned `passed: None`, a benchmark with no pass) and gave this
    run no `passed` (an error, a failed judge); else a score of at least 1 (no `passed`, or one that is no bool:
    ASPIRE's judge returns the number of seeds passed)."""
    judge = record.get("judge") or {}
    extra = judge.get("extra") or {}
    passed = extra.get("passed")
    if isinstance(passed, bool):
        return passed
    if passed is None and ("passed" in extra or scale):
        return None
    return float(judge.get("score") or 0.0) >= 1.0


def _share_solved(attempts: dict[str, list]) -> float | None:
    """The share of the tasks (or rows) with a pass or fail whose attempts include a pass; None when none has one."""
    known = [[s for s in v if s is not None] for v in attempts.values()]
    known = [v for v in known if v]
    return sum(1 for v in known if any(v)) / len(known) if known else None


def _task_of(record: dict) -> str:
    """The task a run is an attempt at: its output's `harbor_task`, else its row's, else the row; for a step of a
    multi-step task (its output or its row names the `step`), that task's step, `<task>:<step>`, whose attempts are
    its rollouts."""
    output = record.get("output") if isinstance(record.get("output"), dict) else {}
    harbor = output if output.get("harbor_task") else {}         # a Harbor trial's output names its task and step
    row = record.get("input") or {}
    task = str(harbor.get("harbor_task") or row.get("harbor_task") or record["id"])
    step = harbor.get("step") or row.get("step")
    return f"{task}:{step}" if step else task


def _counts(tests: list[dict], records: list[dict], repeats: int, budget: Budget) -> dict:
    got = [r for r in records if r["status"] != "infra"]
    scores = [r["judge"]["score"] for r in got]
    # A judge that says its benchmark has no pass (`passed: None`) says so of every row, those it gave no verdict too.
    scale = any(((r.get("judge") or {}).get("extra") or {}).get("passed", False) is None for r in got)
    by_id: dict[str, list] = {}
    for r in got:
        by_id.setdefault(r["id"], []).append(_solved(r, scale))
    attempts: dict[str, int] = {}
    by_task: dict[str, list] = {}
    for r in records:
        attempts[_task_of(r)] = attempts.get(_task_of(r), 0) + 1
        if r["status"] != "infra":
            by_task.setdefault(_task_of(r), []).append(_solved(r, scale))
    ks = sorted(set(attempts.values()))
    spent = budget.snapshot()
    return {"tasks": len(tests), "repeats": repeats,
            "mean_score": sum(scores) / len(scores) if scores else None,
            "solved_once": _share_solved(by_id),
            "k": ks[-1] if ks else 0, **({"k_min": ks[0]} if len(ks) > 1 else {}),
            "pass_at_k": _share_solved(by_task),
            "scored": len(got), "unscored": len(records) - len(got),
            "cost_usd": spent["usd"]["rollout"] if not spent["unknown_cost_calls"] else None,
            "unknown_cost_tasks": spent["unknown_cost_calls"],
            "errors": sum(1 for r in records if r["status"] in ("error", "judge_error"))}


def _safe(text: str) -> str:
    return re.sub(r"[^\w.-]", "_", str(text))


def _record(run_dir: Path, key: str, value) -> None:
    """A finished run's summary.json gets `key` (its test, its checkpoint tests)."""
    path = run_dir / "summary.json"
    if not path.is_file():
        return
    summary = json.loads(path.read_text())
    summary[key] = value
    path.write_text(json.dumps(summary, indent=1, default=str))


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
