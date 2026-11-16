"""`hermit check`: basic checks of the settings before training. Passing does not guarantee success."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
import time

from hermit.adapter.manifest import MANIFEST, read_manifest
from hermit.agent.base import login as subscription
from hermit.agent.base.endpoint import cli_problems
from hermit.agent.base.interface import LoginFailed
from hermit.agent.model.backends import bindings_of, list_models, probe as probe_model, problems_of, registry, warnings_of
from hermit.config import Config
from hermit.sandbox import hidden_paths, set_hidden
from hermit.agent.evolver.loader import find, instantiate, load_class
from hermit.spec import check_spec
from hermit.agent.acting.agent import load_agent
from hermit.isolate import agent_pipeline, config_paths, host_pipeline, judge_for, validate as validate_sandboxes
from hermit.task import TaskData, load_callable
from hermit.trainer.rollout import run_one
from hermit.trainer.trainer import needs_errors, warnings_for

DISCLAIMER = ("check looks only at the basic items above. Passing it does not guarantee that the settings are free of "
              "problems or that the run will succeed.")


def run_check(config: Config, *, probe: bool = True, log=print) -> bool:
    d = config.data
    ok = True
    set_hidden(hidden_paths(d["sandbox"].get("hide") or []))    # the spec check's sandbox below covers it too

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
        if start is not None and (start / MANIFEST).is_file():
            enforce = d["sandbox"].get("enforce", "required") if d["sandbox"]["enabled"] else None
            result = check_spec(start, template_dir, enforce=enforce)
            problems = [f"{i.path or '-'}: check {i.check}: {i.problem}" for i in result.issues]
        report("template", problems, f"{read_manifest(template_dir)['template']}")
    except Exception as exc:
        report("template", [str(exc)])
    # Evolver.
    evolver_params = None
    try:
        spec = find(d["evolver"]["name"], config.base)
        cls = load_class(spec)
        _, evolver_params = instantiate(spec, d["evolver"]["params"], backends=registry(d.get("backends")),
                                        default_model=d.get("model"))
        # an Evolver's own settings (with a login, its problems are the login's, below)
        report("evolver", needs_errors(config, spec.needs, cls.supports_resume) +
               ([] if evolver_params.get("login") else subscription.evolver_problems(evolver_params)),
               f"{spec.name} ({spec.class_path})")
    except Exception as exc:
        report("evolver", [str(exc)])
    # Models: every model the run calls, the backend each is bound to, and whether that backend speaks the caller's
    # dialect and has its key. With the probe: GET /models, then one short request in the dialect.
    for who, binding, api in bindings_of(d, evolver_params):
        problems = problems_of(binding, api)
        note = f"{binding.ref} at {binding.base_url} ({api})"
        for w in warnings_of(binding, api):
            log(f"[warning] {who}: {w}")
        if not problems and probe:
            models = list_models(binding)
            if isinstance(models, list) and binding.model not in models:
                log(f"[warning] {who}: GET {binding.base_url}/models does not list {binding.model!r} (it lists "
                    f"{len(models)} models); the backend may still serve it")
            elif isinstance(models, str):
                log(f"[warning] {who}: GET {binding.base_url}/models: {models}")
            result = probe_model(binding, api)
            if not result["ok"]:
                problems.append(f"{api} request to {result['url']} failed: HTTP {result['status']} {result['detail']}")
            else:
                note += f", {api} request answered"
        report(f"model: {who}", problems, note)
    # Subscriptions: whether each login's sign-in and CLI are there (no model call), and the spec of a model reached
    # through a CLI, which `train` would refuse only when it builds the endpoint.
    for who, login, program, contained, root in subscription.logins_of(d, evolver_params):
        report(f"login: {who}", subscription.problems_of(login, program, contained, root) +
               (subscription.evolver_problems(evolver_params) if who == "evolver" else []), login)
    params = d["agent"].get("params") or {}
    for who, spec in (("llm", d.get("llm")), ("agent's model", params.get("model"))):
        if isinstance(spec, dict) and spec.get("cli"):
            report(f"model through a CLI: {who}", cli_problems(spec),
                   f"{spec['cli']}, isolation {spec.get('isolation') or 'sandbox'}")
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
        # A sandboxed agent is loaded only inside its sandbox (the probe below); importing it here would run the
        # task's code in this process.
        sandboxed = host_pipeline(config) is None
        pipeline = True if sandboxed else load_agent(d["agent"]["name"], d["agent"].get("params"), config.base).run
        # The judge is imported here when it runs here: a host judge, or any judge when the pipeline runs on the
        # host (the sandbox off); a sandboxed judge is loaded only inside its sandbox (the probe below).
        judge = load_callable(f"{d['judge']['path']}:{d['judge']['name']}", config.base) \
            if (d["judge"].get("mode") or "host") == "host" or not sandboxed else True
    except Exception as exc:
        problems.append(f"agent/judge: {exc}")
        pipeline = judge = None
    report("task", problems)
    # Sandboxes: the declared paths must exist (an unset variable leaves a path that names nothing) and hold no
    # secret; the selected backend must be installed.
    sandbox_problems = []
    try:
        validate_sandboxes(config)
        env = d["environment"]
        for key in ("read", "write"):
            for path in config_paths(config, f"environment.{key}", env.get(key)):
                if key != "write" and not path.exists():
                    sandbox_problems.append(f"environment.{key} path does not exist: {path}")
        for path in config_paths(config, "judge.tests", d["judge"].get("tests")):
            if not path.exists():
                sandbox_problems.append(f"judge.tests path does not exist: {path}")
        for path in config_paths(config, "evolver_access.environment", d["evolver_access"]["environment"]):
            if not path.exists():
                sandbox_problems.append(f"evolver_access.environment path does not exist: {path}")
    except ValueError as exc:
        sandbox_problems.append(str(exc))
    if d["sandbox"]["enabled"] and d["sandbox"].get("enforce", "required") == "required" and not shutil.which("bwrap"):
        sandbox_problems.append("sandbox.enforce is required but bubblewrap (bwrap) is not installed")
    if d["environment"].get("image") and d["sandbox"]["enabled"] and not shutil.which("docker"):
        sandbox_problems.append("environment.image needs Docker, which is not installed")
    where = host_pipeline(config) or "the task sandbox (bubblewrap)"
    report("sandbox", sandbox_problems,
           f"enabled={d['sandbox']['enabled']}, enforce={d['sandbox'].get('enforce', 'required')}, agent: {where}, "
           f"network={d['environment'].get('network')}, judge={d['judge'].get('mode') or 'host'}")
    # Budgets.
    b = d["trainer"]["budget"]
    report("budgets", [] if any(b.get(k) for k in ("usd", "tokens", "hours")) else ["no hard budget is set"],
           f"hard {b}, {d['trainer']['epochs']} epoch(s)")
    # Probe: one training task through the agent and the judge, without an adapter.
    if probe and data is not None and pipeline is not None and data.splits["train"]:
        task = data.tasks[data.splits["train"][0]]
        t0 = time.time()
        # The probe runs the agent as a rollout would: in the task sandbox, not in this process.
        probe_pipeline, close = None, (lambda: None)
        try:
            probe_pipeline, close = agent_pipeline(config, "check", records=None)
            judge = judge_for(config, probe_pipeline)
        except Exception as exc:
            close()                  # what was started before the error
            probe_pipeline, close = None, (lambda: None)
            record = {"status": "error", "error": f"the check sandbox: {exc}", "judge": {"score": 0.0},
                      "api_speed": None, "cost_usd": None}
        if probe_pipeline is not None:
            try:
                record = run_one(task, None, probe_pipeline, judge)
            except LoginFailed as exc:
                record = {"status": "error", "error": str(exc), "judge": {"score": 0.0}, "api_speed": None,
                          "cost_usd": None}
            finally:
                close()
        problems = [record["error"]] if record["status"] != "ok" else []
        report("agent probe", problems, f"task {task['id']}: score {record['judge']['score']}, "
               f"{time.time() - t0:.0f} s, api speed {json.dumps(record.get('api_speed'))}, cost {record['cost_usd']}")
    for w in warnings_for(config, val_given=True):
        log(f"[warning] {w}")
    log(DISCLAIMER)
    return ok
