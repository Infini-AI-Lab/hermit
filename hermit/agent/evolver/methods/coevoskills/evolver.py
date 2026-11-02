"""Thin controller seam: public training task -> original loop -> adapter files.

No actor class, prompt, tool loop, scoring function or upstream Evolver function
is modified. Fresh probes call the Trainer's existing run_agent tool.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import uuid

from hermit.agent.evolver.agent import _run
from hermit.agent.evolver.base import Evolver, StepResult, Usage
from hermit.agent.evolver.methods.coevoskills.upstream import real_tree, safe_name


def path(value, base):
    p = Path(os.path.expandvars(str(value))).expanduser()
    return p if p.is_absolute() else (base / p).resolve()


class CoEvoSkillsEvolver(Evolver):
    supports_resume = False

    def setup(self, ctx):
        if ctx.params.get("cli_backend", "codex") != "codex":
            raise ValueError("the fixed OfficeQA actor uses Codex")
        if not os.environ.get("HERMIT_COEVOSKILLS_RUNTIME"):
            raise RuntimeError("start with the common launcher and its model-only gateway")
        seed = path(ctx.params["seed"], ctx.base or Path.cwd())
        snapshot = ctx.workspace.state / "initial-adapter"
        if not snapshot.exists():
            shutil.copytree(seed, snapshot)
        real_tree(snapshot)

    def step(self, ctx):
        if not ctx.tool_url:
            raise RuntimeError("the fixed actor is reached through the Trainer's run_agent interface")
        records = sorted((ctx.workspace.learning_material / "current/tasks").glob("*.json"))
        if len(records) != 1:
            return StepResult("failed", "one training task per adapter update is required")
        record = json.loads(records[0].read_text())
        tid = safe_name(str(record["id"]))
        base = ctx.base or Path.cwd()
        out = ctx.workspace.output / ("fixed-" + uuid.uuid4().hex[:10])
        # Do not forward judge feedback, reference answers, or prior actor traces.
        request = {
            "task_id": tid, "task_path": str(path(ctx.params["task_packages"], base) / tid.lower()),
            "output": str(out), "tool_url": ctx.tool_url,
            "projection_root": str(ctx.workspace.scratch / "actor-probes"),
            "seed": str(ctx.workspace.state / "initial-adapter"),
            "params": {**ctx.params, "upstream_root": str(path(ctx.params["upstream_root"], base))},
        }
        request_path = ctx.workspace.scratch / "common-request.json"
        request_path.write_text(json.dumps(request, indent=2))
        command = [sys.executable, "-m", "hermit.agent.evolver.methods.coevoskills.worker", str(request_path)]
        code = _run(command, {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, ctx,
                    ctx.workspace.output / "fixed-loop.log", base)
        usage = []
        for p in out.glob("model-calls/*/usage.json"):
            usage.extend(Usage(**r) for r in json.loads(p.read_text()))
        result_file = out / "result.json"
        if code or (ctx.cancel is not None and ctx.cancel.is_set()) or not result_file.is_file():
            return StepResult("failed", f"{tid}: original evolution did not complete; adapter retained", usage=usage)
        result = json.loads(result_file.read_text())
        source = Path(result["skills"])
        real_tree(source)
        target = ctx.workspace.adapter / "skills" / tid
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".candidate-", dir=target.parent) as tmp:
            candidate = Path(tmp) / "skills"
            from hermit.agent.evolver.methods.coevoskills.packaging import project
            audit = project(source, candidate, tid)
            (ctx.workspace.output / 'portability.json').write_text(json.dumps(audit, indent=2))
            if target.exists():
                shutil.rmtree(target)
            candidate.rename(target)
        return StepResult("done", f"{tid}: installed executable Skills from the original loop",
                          usage=usage, extra={"coevoskills": {
                              "training_task": tid, "fixed_actor_probe_score": result["score"],
                              "actor": "codex + examples/officeqa/utils/task.py",
                              "upstream_algorithm_modified": False}})
