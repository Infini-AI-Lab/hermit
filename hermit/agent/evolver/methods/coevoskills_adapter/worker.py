"""One training question's upstream loop, in a process of its own (upstream's modules keep state in globals).

    python -m hermit.agent.evolver.methods.coevoskills_adapter.worker <request.json>

The request names the question's Harbor task package (the example's preparation writes it), the Trainer's tool
endpoint, the adapter without packages the oracle deploys a candidate into, the example's `deliver` hook and
`path_map`, and the loop's limits. The package is copied without its `solution`, its `allow_internet` set to false,
and run through Harbor's Trial with upstream's evolution agent (HarborTerminus2Evolution) unchanged: its roles' model
is the run's `llm` (model.py), its oracle the task's acting agent and judge (oracle.py), its containers
environment.py's. Upstream leaves its best snapshot in the task's skills directory and records that snapshot's final
verdict in the trial's evolution_run_log.json, also when the loop then ends in an error (a timeout, an agent error);
`result.json` records the Skills, checked against upstream's schema, that verdict and the loop's error, if any. The
package is copied under the request's `task_dir` name, unique to this worker: Harbor names the containers and the
image of the question after it, so the Evolver can remove exactly this worker's.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import signal
import sys

from hermit.agent.evolver.methods.coevoskills_adapter.upstream import checkout, real_tree

AGENT = "libs.terminus_agent.agents.terminus_2.harbor_terminus_2_evolution:HarborTerminus2Evolution"
ENVIRONMENT = "hermit.agent.evolver.methods.coevoskills_adapter.environment:FixtureDockerEnvironment"
ORACLE = "hermit.agent.evolver.methods.coevoskills_adapter.oracle:FixedActorOracle"


def writable(root: Path) -> None:
    """Files the containers wrote stay removable by the user."""
    for path in [root, *root.rglob("*")]:
        if not path.is_symlink():
            path.chmod(path.stat().st_mode | 0o600 | (0o100 if path.is_dir() else 0))


def final_verdict(trials: Path) -> dict:
    """Upstream's final verdict on the Skills it left (its best snapshot): `gt_oracle_result` of the trial's
    agent/evolution_run_log.json, as {passed, reward, verdict}; None values when the loop recorded none."""
    for log in sorted(Path(trials).glob("*/agent/evolution_run_log.json")):
        verdict = json.loads(log.read_text()).get("gt_oracle_result")
        if isinstance(verdict, dict):
            return {"passed": verdict.get("passed"), "reward": verdict.get("reward", verdict.get("pass_rate")),
                    "verdict": verdict}
    return {"passed": None, "reward": None, "verdict": None}


async def run(request: dict) -> None:
    import toml
    root = checkout(request["upstream_root"])
    os.environ["SKILLSBENCH_ROOT"] = str(root)        # upstream locates its prompts and meta-skills by this name
    out = Path(request["output"])
    out.mkdir(parents=True)
    source = Path(request["task_path"])
    real_tree(source)
    if not (source / "task.toml").is_file() or not (source / "environment").is_dir() or not (source / "tests").is_dir():
        raise ValueError(f"{source} is no Harbor task package (task.toml, environment/, tests/)")
    task = out / "task" / request.get("task_dir", source.name)
    shutil.copytree(source, task, ignore=shutil.ignore_patterns(".evolution", "__pycache__", "*.pyc", "solution"))
    manifest = task / "task.toml"
    config = toml.loads(manifest.read_text())
    config.setdefault("environment", {})["allow_internet"] = False
    manifest.write_text(toml.dumps(config))

    from hermit.agent.evolver.methods.coevoskills_adapter import environment, model, oracle
    from hermit.task import load_callable
    oracle.CONFIG.clear()
    oracle.CONFIG.update({k: request[k] for k in ("task_id", "tool_url", "projection_root", "seed", "path_map")})
    oracle.CONFIG["deliver"] = load_callable(request["deliver"], Path(request["base"]))
    os.environ["COEVOSKILLS_CUSTOM_AGENT_IMPORT_PATHS"] = json.dumps({"hermit-actor": ORACLE})
    model.install(request["tool_url"])
    environment.install()

    from harbor.models.trial.config import TrialConfig
    from harbor.trial.trial import Trial
    name = request["model_name"]
    trial = TrialConfig.model_validate({
        "task": {"path": str(task)}, "trials_dir": str(out / "evolution"),
        "trial_name": request["trial_name"],
        "agent": {"import_path": AGENT, "model_name": name, "override_timeout_sec": request["task_timeout_s"],
                  "kwargs": {"gt_oracle_agent": "hermit-actor", "gt_oracle_model": name,
                             "independent_verifier_model": name,
                             "max_host_interventions": request["max_iterations"],
                             "max_episodes": request["max_episodes"]}},
        "verifier": {"disable": True},
        "environment": {"delete": True, "import_path": ENVIRONMENT}})
    result = await Trial(trial).run()
    (out / "upstream-result.json").write_text(result.model_dump_json(indent=2))
    error = None
    if result.exception_info:                         # upstream still leaves its best snapshot before it raises
        error = f"{result.exception_info.exception_type}: {result.exception_info.exception_message[:300]}"
    skills = task / "environment" / "skills"
    from libs.terminus_agent.evolution.skill_schema import validate_skill_path
    manifests = list(skills.glob("evo-*/SKILL.md"))
    if not manifests or any(validate_skill_path(p) for p in manifests):
        raise RuntimeError("the upstream loop left no valid Skill" + (f" ({error})" if error else ""))
    real_tree(skills)
    (out / "result.json").write_text(json.dumps({"skills": str(skills), "loop_error": error,
                                                 **final_verdict(out / "evolution")}, indent=2, default=str))


def main() -> None:
    def terminate(*_):
        raise KeyboardInterrupt("stopped by the Evolver; Harbor removes the task's containers")
    signal.signal(signal.SIGTERM, terminate)
    if sys.platform == "linux":                       # the loop does not outlive the Evolver that started it
        import ctypes
        ctypes.CDLL(None).prctl(1, signal.SIGTERM, 0, 0, 0)       # PR_SET_PDEATHSIG
    request = json.loads(Path(sys.argv[1]).read_text())
    try:
        asyncio.run(run(request))
    finally:
        for path in (Path(request["output"]), Path(request["projection_root"])):
            if path.is_dir():
                writable(path)


if __name__ == "__main__":
    main()
