"""The loop's fresh-execution oracle: upstream's custom-oracle seam (`COEVOSKILLS_CUSTOM_AGENT_IMPORT_PATHS`), answered
by the task's own acting agent through the Trainer's `run_agent` tool.

Upstream runs a fresh agent with the candidate Skill in a fresh container and then the task's hidden tests there;
only pass or fail returns to the loop (`_run_gt_oracle_check`). Here the oracle agent is the run's acting agent: the
candidate is made the skills of a copy of the adapter without packages (packaging.py), checked by `check_spec`, run on
the question by `run_agent`, and the acting agent's output is written into the oracle's container where the task's
hidden test reads it, as the example's `deliver` hook names the files. No model call is made here, and neither the
judge's score nor its feedback goes to the loop: the hidden test decides, as upstream's does. A candidate the acting
agent changed during its run fails the check (integrity.py): upstream keeps the Skill immutable during the run.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import shutil
import uuid

from harbor.agents.base import BaseAgent

from hermit.agent.evolver.methods.coevoskills_adapter.client import call_tool

CONFIG: dict = {}                    # task_id, tool_url, projection_root, seed, path_map, deliver: set by the worker


def projection(skills: Path) -> Path:
    """A copy of the adapter without packages, with the candidate's packages as its skills."""
    from hermit.agent.evolver.methods.coevoskills_adapter.packaging import project
    from hermit.agent.evolver.methods.coevoskills_adapter.upstream import real_tree
    from hermit.agent.evolver.methods.coevoskills_adapter.worker import writable
    real_tree(skills)
    target = Path(CONFIG["projection_root"]) / uuid.uuid4().hex
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(CONFIG["seed"], target, ignore=shutil.ignore_patterns("skills", "__pycache__", "*.pyc"))
    audit = project(skills, target / "skills", CONFIG["task_id"], CONFIG.get("path_map"))
    (target.parent / (target.name + "-portability.json")).write_text(json.dumps(audit, indent=2))
    writable(target)
    return target


def probe(skills: Path) -> dict:
    """The acting agent's run of the question with the candidate: the run's record (output, judge, log)."""
    adapter = projection(skills)
    check = call_tool(CONFIG["tool_url"], "check_spec", {"path": str(adapter)})
    if not check.get("passed"):
        raise RuntimeError("the candidate does not satisfy the template: " + json.dumps(check.get("issues", [])))
    result = call_tool(CONFIG["tool_url"], "run_agent",
                       {"task_ids": [CONFIG["task_id"]], "adapter": str(adapter)})["results"][0]
    if result["status"] != "ok":
        raise RuntimeError(f"the acting agent's run failed: {result.get('output')}")
    if not result.get("log"):
        raise RuntimeError("the acting agent's run left no log: the candidate's deployment cannot be checked")
    from hermit.agent.evolver.methods.coevoskills_adapter.integrity import verify_deployment
    try:
        verify_deployment(adapter, Path(result["log"]))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"the candidate was changed during the run, so the run tests another program: {exc}") \
            from exc
    return result


class FixedActorOracle(BaseAgent):
    @staticmethod
    def name():
        return "hermit-actor"

    def version(self):
        return "1"

    async def setup(self, environment):
        pass

    async def run(self, instruction, environment, context):
        from hermit.agent.evolver.methods.coevoskills_adapter.worker import writable
        local = Path(CONFIG["projection_root"]) / ("download-" + uuid.uuid4().hex)
        local.mkdir(parents=True)
        await environment.download_dir("/app/environment/skills", str(local))   # the candidate, not the workspace
        writable(local)
        manifests = list(local.glob("evo-*/SKILL.md")) or list(local.glob("skills/evo-*/SKILL.md"))
        if not manifests:
            raise RuntimeError("the oracle received no evolved Skill")
        result = await asyncio.to_thread(probe, manifests[0].parent.parent)
        # A hand-over that fails is an error of the check, never a wrong answer: Harbor's exec does not raise on a
        # failed command, so its return code is checked; the file goes in by Harbor's own upload.
        for path, text in CONFIG["deliver"](result.get("output")).items():
            if not str(path).startswith("/") or "'" in str(path):
                raise ValueError(f"deliver: an absolute container path without quotes, not {path!r}")
            made = await environment.exec(command=f"mkdir -p \"$(dirname '{path}')\"", timeout_sec=15)
            if made.return_code != 0:
                raise RuntimeError(f"deliver: the oracle's container refused the folder of {path}")
            staged = local / ("deliver-" + uuid.uuid4().hex)
            staged.write_text(str(text), encoding="utf-8")
            await environment.upload_file(staged, str(path))
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        if result.get("log") and (Path(result["log"]) / "agent-codex.jsonl").is_file():
            shutil.copy2(Path(result["log"]) / "agent-codex.jsonl", self.logs_dir / "codex.txt")
