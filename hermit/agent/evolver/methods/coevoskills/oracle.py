"""Documented upstream custom-oracle seam; delegates to the main’s unchanged Codex acting agent.

This class contains no inference or agent loop. It supplies the original
CoEvoSkills loop's fresh-execution callback through the Trainer's run_agent tool.
The private judge result is never sent to Generator or Verifier.
"""
from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
import shutil
import urllib.request
import uuid

from harbor.agents.base import BaseAgent

CONFIG: dict = {}


def post(name, payload):
    request = urllib.request.Request(CONFIG["tool_url"] + "/call/" + name,
                                    data=json.dumps(payload).encode(),
                                    headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=4000) as response:
        reply = json.loads(response.read())
    if reply.get("error"):
        raise RuntimeError(reply["error"])
    return reply["result"]


def projection(skills: Path) -> Path:
    from hermit.agent.evolver.methods.coevoskills.upstream import real_tree
    real_tree(skills)
    target = Path(CONFIG["projection_root"]) / uuid.uuid4().hex
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(CONFIG["seed"], target, ignore=shutil.ignore_patterns("skills", "__pycache__", "*.pyc"))
    destination = target / "skills" / CONFIG["task_id"]
    from hermit.agent.evolver.methods.coevoskills.packaging import project
    audit = project(skills, destination, CONFIG["task_id"])
    (target.parent / (target.name + '-portability.json')).write_text(json.dumps(audit, indent=2))
    from hermit.agent.evolver.methods.coevoskills.worker import writable_artifacts
    writable_artifacts(target)
    return target


def probe(skills: Path):
    adapter = projection(skills)
    check = post("check_spec", {"path": str(adapter)})
    if not check.get("passed"):
        raise RuntimeError("generated Skill does not satisfy the fixed adapter contract: " + json.dumps(check.get("issues", [])))
    result = post("run_agent", {"task_ids": [CONFIG["task_id"]], "adapter": str(adapter)})["results"][0]
    if result["status"] != "ok":
        raise RuntimeError("the common fixed actor could not complete its execution")
    if result.get("log"):
        from hermit.agent.evolver.methods.coevoskills.integrity import verify_deployment
        try:
            verify_deployment(adapter, Path(result['log']))
        except (OSError, ValueError) as exc:
            raise RuntimeError('deployed Skill integrity failed; original judge result is not rewritten') from exc
    # Only the prediction is a public output artifact. Never export judge feedback.
    return result


class FixedActorOracle(BaseAgent):
    @staticmethod
    def name():
        return "hermit-officeqa"

    def version(self):
        return "fixed-actor-interface-1"

    async def setup(self, environment):
        pass

    async def run(self, instruction, environment, context):
        # Download precisely this candidate's Skill, not its generator workspace.
        local = Path(CONFIG["projection_root"]) / ("download-" + uuid.uuid4().hex)
        local.mkdir(parents=True)
        await environment.download_dir("/app/environment/skills", str(local))
        from hermit.agent.evolver.methods.coevoskills.worker import writable_artifacts
        writable_artifacts(local)
        manifests = list(local.glob("evo-*/SKILL.md"))
        if not manifests:
            # Some environment implementations preserve the source directory name.
            manifests = list(local.glob("skills/evo-*/SKILL.md"))
        if not manifests:
            raise RuntimeError("the oracle received no evolved Skill")
        root = manifests[0].parent.parent
        result = await asyncio.to_thread(probe, root)
        answer = str((result.get("output") or {}).get("answer") or "")
        encoded = base64.b64encode(("<answer>" + answer + "</answer>").encode()).decode()
        await environment.exec(command=(
            "printf '%s' '" + encoded + "' | base64 -d > /root/answer.txt"), timeout_sec=15)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        if result.get("log"):
            source = Path(result["log"]) / "agent-codex.jsonl"
            if source.is_file():
                shutil.copy2(source, self.logs_dir / "codex.txt")
