"""`harnessadapter check`: basic checks of the settings before training. Passing does not guarantee success."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import time

from harnessadapter.adapter.manifest import read_manifest
from harnessadapter.config import Config
from harnessadapter.evolver.loader import find, load_class
from harnessadapter.spec import check_spec
from harnessadapter.task import TaskData, load_callable
from harnessadapter.trainer.rollout import run_one
from harnessadapter.trainer.trainer import needs_errors, warnings_for

DISCLAIMER = ("check looks only at the basic items above. Passing it does not guarantee that the settings are free of "
              "problems or that the run will succeed.")


def run_check(config: Config, *, probe: bool = True, log=print) -> bool:
    d = config.data
    ok = True

    def report(item: str, problems: list[str], note: str = ""):
        nonlocal ok
        ok = ok and not problems
        log(f"[{'ok' if not problems else 'PROBLEM'}] {item}" + (f" — {note}" if note and not problems else ""))
        for p in problems:
            log(f"    - {p}")

    # Template and starting adapter.
    try:
        template_dir = config.template_dir()
        start = config.path(d["start_from"]) if d.get("start_from") else template_dir
        problems = []
        if start is not None and (start / "adapter.yaml").is_file():
            result = check_spec(start, template_dir)
            problems = [f"{i.path or '-'}: check {i.check}: {i.problem}" for i in result.issues]
        report("template", problems, f"{read_manifest(template_dir)['template']}")
    except Exception as exc:
        report("template", [str(exc)])
    # Evolver.
    try:
        spec = find(d["evolver"]["name"], config.base)
        cls = load_class(spec)
        report("evolver", needs_errors(config, spec.needs, cls.supports_resume), f"{spec.name} ({spec.class_path})")
    except Exception as exc:
        report("evolver", [str(exc)])
    # Task.
    problems = []
    info = config.path(d["task_info"])
    if info is None or not (info / "README.md").is_file():
        problems.append("task_info/README.md is missing")
    data = None
    try:
        data = TaskData(config.path(d["data"]["tasks"]), config.path(d["data"]["train"]),
                        config.path(d["data"]["val"]), config.path(d["data"]["test"]))
        problems += data.overlaps()
        if not data.splits["train"]:
            problems.append("the training split is empty")
    except Exception as exc:
        problems.append(f"data: {exc}")
    try:
        pipeline = load_callable(d["pipeline"]["run"], config.base)
        judge = load_callable(f"{d['judge']['path']}:{d['judge']['name']}", config.base)
    except Exception as exc:
        problems.append(f"pipeline/judge: {exc}")
        pipeline = judge = None
    report("task", problems)
    # Sandbox: paths the task makes readable must exist (an unset variable leaves a path that names nothing).
    missing = [str(config.path(h)) for h in d["sandbox"]["read"] or [] if not config.path(h).exists()]
    report("sandbox", [f"sandbox.read path does not exist: {m}" for m in missing],
           f"enabled={d['sandbox']['enabled']}, network={d['sandbox']['network']}, env={d['sandbox']['env']}")
    # Budgets.
    b = d["trainer"]["budget"]
    report("budgets", [] if any(b.get(k) for k in ("usd", "tokens", "hours")) else ["no hard budget is set"],
           f"hard {b}, {d['trainer']['epochs']} epoch(s)")
    # Probe: one training task through the pipeline and the judge, without an adapter.
    if probe and data is not None and pipeline is not None and data.splits["train"]:
        task = data.tasks[data.splits["train"][0]]
        t0 = time.time()
        record = run_one(task, None, pipeline, judge)
        problems = [record["error"]] if record["status"] != "ok" else []
        report("pipeline probe", problems, f"task {task['id']}: score {record['judge']['score']}, "
               f"{time.time() - t0:.0f} s, api speed {json.dumps(record.get('api_speed'))}, cost {record['cost_usd']}")
    for w in warnings_for(config, val_given=True):
        log(f"[warning] {w}")
    log(DISCLAIMER)
    return ok
