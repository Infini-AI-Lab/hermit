"""LIBERO's acting prompt, for a method that brings no acting prompt of its own (prompt-only, and the others that learn
only what the session is given as context): the task, the workspace, the simulator, the rules and what is scored,
and nothing of a method. LIBERO has no acting prompt; this one is written here. The workspace, the simulator
service, the record and the judge are the cap-harness engine's (`examples/cap_harness/utils/agent/task.py`), so a
method's number on LIBERO is measured the same way whatever its prompt.

    agent:
      params:
        prompt: ../utils/agent/task.py:prompt
        prepare: ../../cap_harness/utils/agent/task.py:prepare
        answer: ../../cap_harness/utils/agent/task.py:answer
        collect: ../../cap_harness/utils/agent/task.py:collect
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
ENGINE = HERE.parents[2] / "cap_harness" / "utils" / "agent"


def _engine():
    """The engine's task functions, loaded from their own folder (they import sim.py beside them)."""
    if str(ENGINE) not in sys.path:
        sys.path.insert(0, str(ENGINE))
    spec = importlib.util.spec_from_file_location("cap_harness_task", ENGINE / "task.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def prompt(task: dict, params: dict, base: Path) -> str:
    engine = _engine()
    s = engine.settings(task, params)
    steps = (f" An episode runs at most {int(s['max_steps'])} simulator steps." if s["max_steps"] else "")
    return (HERE / "prompt.md").read_text().format(
        suite=s["suite"], task_id=s["task_id"], task_dir=engine.task_dir(s), dev=s["dev"],
        unseen=s["unseen"] or "none", heldout=len(engine.sim_seeds(s["heldout"])), steps=steps,
        benchmark_doc=s["doc"])
