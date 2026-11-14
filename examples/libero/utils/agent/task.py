"""A method-neutral acting prompt for LIBERO, for a method that brings no acting prompt of its own (prompt-only, and
the others that learn only what the session is given as context): the task, the workspace, the simulator, the rules
and what is scored, and nothing of a method. LIBERO has no acting prompt; this one is written here. The workspace,
the simulator service, the record and the judge are the engine's (`examples/aspire_robosuite/utils/agent/task.py`),
as for ASPIRE's own prompts, so a method's number on LIBERO is measured the same way whatever its prompt; `prepare`
leaves out of the workspace what is ASPIRE's method rather than the task's.

    agent:
      params:
        prompt: ../utils/agent/task.py:prompt
        prepare: ../utils/agent/task.py:prepare
        answer: ../../aspire_robosuite/utils/agent/task.py:answer
        collect: ../../aspire_robosuite/utils/agent/task.py:collect
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
ENGINE = HERE.parents[2] / "aspire_robosuite" / "utils" / "agent"
# What the engine's prepare writes that is ASPIRE's method, not the task's: the evolutionary search's procedure (a
# program it leaves is the one the engine scores) and ASPIRE's assignment block (its coordinator's terms)
ASPIRE_ONLY = ("docs/evosearch-iteration.md", "task.md")


def _engine():
    """The engine's task functions, loaded from their own folder (they import sim.py beside them)."""
    if str(ENGINE) not in sys.path:
        sys.path.insert(0, str(ENGINE))
    spec = importlib.util.spec_from_file_location("aspire_robosuite_task", ENGINE / "task.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _settings(engine, task: dict, params: dict) -> dict:
    s = engine.settings(task, params)
    if s["role"] == "verifier":
        raise ValueError(f"{task['id']}: a verifier row belongs to a method that verifies (ASPIRE's); "
                         "this prompt acts on a task")
    return s


def prepare(task: dict, workspace: Path, params: dict, base: Path) -> list[dict]:
    """The engine's workspace (bin/sim registered with the service, the docs, the scene-snapshot program) without
    ASPIRE_ONLY."""
    engine = _engine()
    _settings(engine, task, params)
    mounts = engine.prepare(task, workspace, params, base)
    for name in ASPIRE_ONLY:
        (Path(workspace) / name).unlink(missing_ok=True)
    return mounts


def prompt(task: dict, params: dict, base: Path) -> str:
    engine = _engine()
    s = _settings(engine, task, params)
    steps = (f" An episode runs at most {int(s['max_steps'])} simulator steps." if s["max_steps"] else "")
    return (HERE / "prompt.md").read_text().format(
        suite=s["suite"], task_id=s["task_id"], task_dir=engine.task_dir(s), dev=s["dev"],
        unseen=s["unseen"] or "none", heldout=len(engine.sim_seeds(s["heldout"])), steps=steps,
        benchmark_doc=s["doc"])
