"""Original upstream loop with CLI transport and its documented fixed-actor oracle hook."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import uuid

import toml

from hermit.agent.evolver.methods.coevoskills.upstream import checkout, real_tree


def writable_artifacts(root):
    """Host artifacts are mutable bookkeeping, not the frozen acting Skill sources."""
    for path in [root, *root.rglob("*")]:
        if path.is_symlink():
            continue
        mode = path.stat().st_mode
        path.chmod(mode | 0o600 | (0o100 if path.is_dir() else 0))


async def run(request):
    params = request["params"]
    root = checkout(params["upstream_root"])
    from hermit.agent.evolver.methods.coevoskills.strict_runtime import configure
    configure(params)
    # Upstream's historical variable name locates CoEvoSkills prompts/meta-skills.
    # It points only to the pinned source checkout, not a SkillsBench integration.
    os.environ["SKILLSBENCH_ROOT"] = str(root)
    out = Path(request["output"])
    out.mkdir(parents=True)
    source = Path(request["task_path"])
    real_tree(source)
    task = out / "task" / source.name
    shutil.copytree(source, task, ignore=shutil.ignore_patterns(".evolution", "__pycache__", "*.pyc", "solution"))
    manifest = task / "task.toml"
    config = toml.loads(manifest.read_text())
    if config.get("metadata", {}).get("benchmark") != "officeqa":
        raise ValueError("only prepared OfficeQA fixtures are supported")
    config["environment"]["allow_internet"] = False
    manifest.write_text(toml.dumps(config))

    from hermit.agent.evolver.methods.coevoskills import oracle
    oracle.CONFIG.clear()
    oracle.CONFIG.update({k: request[k] for k in ("task_id", "tool_url", "projection_root", "seed")})
    os.environ["COEVOSKILLS_CUSTOM_AGENT_IMPORT_PATHS"] = json.dumps({
        "hermit-officeqa":
        "hermit.agent.evolver.methods.coevoskills.oracle:FixedActorOracle"})
    from hermit.agent.evolver.methods.coevoskills.transport import install
    install({**params, "transport_logs": str(out / "model-calls")})

    from harbor.models.trial.config import TrialConfig
    from harbor.trial.trial import Trial
    cfg = TrialConfig.model_validate({
        "task": {"path": str(task)}, "trials_dir": str(out / "evolution"),
        "trial_name": "common-" + uuid.uuid4().hex[:12],
        "agent": {
            "import_path": "libs.terminus_agent.agents.terminus_2.harbor_terminus_2_evolution:HarborTerminus2Evolution",
            "model_name": params["model_name"],
            "override_timeout_sec": params["task_timeout_s"],
            "kwargs": {
                "gt_oracle_agent": "hermit-officeqa",
                "gt_oracle_model": params["model_name"],
                "independent_verifier_model": params["model_name"],
                "max_host_interventions": params["max_iterations"],
                "max_episodes": params["max_episodes"],
            }},
        "verifier": {"disable": True}, "environment": {"delete": True,
            "import_path": "hermit.agent.evolver.methods.coevoskills.strict_runtime:ModelOnlyDockerEnvironment"},
    })
    result = await Trial(cfg).run()
    (out / "upstream-result.json").write_text(result.model_dump_json(indent=2))
    if result.exception_info:
        raise RuntimeError("the unchanged upstream evolution loop failed")
    skills = task / "environment/skills"
    from libs.terminus_agent.evolution.skill_schema import validate_skill_path
    manifests = list(skills.glob("evo-*/SKILL.md"))
    if not manifests or any(validate_skill_path(p) for p in manifests):
        raise RuntimeError("upstream exported no valid executable Skill")
    real_tree(skills)
    final = await asyncio.to_thread(oracle.probe, skills)
    (out / "result.json").write_text(json.dumps({
        "skills": str(skills), "score": final["judge"]["score"],
        "actor": "main Codex acting agent",
        "upstream_algorithm_modified": False,
        "generation_container_network": "none",
    }, indent=2))
    writable_artifacts(out)


def main():
    def terminate(*_):
        raise KeyboardInterrupt("controller stopped; clean up task environment")
    signal.signal(signal.SIGTERM, terminate)
    if sys.platform == "linux":
        import ctypes
        parent = os.getppid()
        if ctypes.CDLL(None).prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
            raise RuntimeError("cannot arrange parent-death cleanup")
        if os.getppid() != parent:
            terminate()
    request = json.loads(Path(sys.argv[1]).read_text())
    try:
        asyncio.run(run(request))
    finally:
        for path in (Path(request["output"]), Path(request["projection_root"])):
            if path.is_dir():
                writable_artifacts(path)


if __name__ == "__main__":
    main()
