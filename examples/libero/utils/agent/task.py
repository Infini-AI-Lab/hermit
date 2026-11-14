"""LIBERO's acting prompt for a method that brings no acting prompt of its own (prompt-only, and the others that
learn only what the session is given as context): the task, the workspace, the simulator, the rules and what is
scored, and nothing of a method. The workspace, the simulator service, the record and the judge are the engine's
(`examples/aspire_robosuite/utils/agent/task.py`: `prepare`, `answer`, `collect`), as for ASPIRE's own prompts, so a
method's number on LIBERO is measured the same way whatever its prompt.

    agent:
      params:
        prompt: ../utils/agent/task.py:prompt
        prepare: ../../aspire_robosuite/utils/agent/task.py:prepare
        answer: ../../aspire_robosuite/utils/agent/task.py:answer
        collect: ../../aspire_robosuite/utils/agent/task.py:collect
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
ENGINE = HERE.parents[2] / "aspire_robosuite" / "utils" / "agent"


def _engine():
    """The engine's task functions, loaded from their own folder (they import sim.py beside them)."""
    if str(ENGINE) not in sys.path:
        sys.path.insert(0, str(ENGINE))
    spec = importlib.util.spec_from_file_location("aspire_robosuite_task", ENGINE / "task.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def prompt(task: dict, params: dict, base: Path) -> str:
    engine = _engine()
    s = engine.settings(task, params)
    if s["role"] == "verifier":
        raise ValueError(f"{task['id']}: a verifier row belongs to a method that verifies (ASPIRE's); "
                         "this prompt acts on a task")
    task_dir = engine.task_dir(s)
    heldout = len(engine.sim_seeds(s["heldout"]))
    return (HERE / "prompt.md").read_text().format(
        suite=s["suite"], task_id=s["task_id"], task_dir=task_dir, dev=s["dev"], unseen=s["unseen"] or "none",
        heldout=heldout, max_steps=s["max_steps"] or 1000, benchmark_doc=s["doc"])
