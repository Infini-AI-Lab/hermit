"""What every layer shares and none owns: what an acting agent returns (RunOutput), read by the run and the judge, and
how a `file.py:function` or `module:function` spec is loaded. It imports nothing of Hermit, so the agents
(hermit.agent), the tasks (hermit.task) and the environments (hermit.environment) can all use it without depending
on each other."""
from __future__ import annotations

from dataclasses import dataclass, field
import importlib
import importlib.util
from pathlib import Path
import sys
from typing import Any, Callable


@dataclass
class RunOutput:
    output: Any                          # what the judge scores
    log: Path | None = None              # the host's original log (the trajectory), a file or a directory
    cost_usd: float | None = None        # None = unknown
    wall_s: float = 0.0
    api_speed: dict | None = None        # for example output tokens per second
    messages: list | None = None         # optional normalised message list of the trajectory
    tokens: int | None = None            # input + output tokens, if known
    extra: dict = field(default_factory=dict)


def load_callable(spec: str, base: Path) -> Callable:
    """'file.py:function' relative to `base`, or 'package.module:function'."""
    module_part, _, name = spec.partition(":")
    if not name:
        raise ValueError(f"Expected 'file.py:function' or 'module:function', got {spec!r}")
    if module_part.endswith(".py"):
        path = (Path(base) / module_part).resolve()
        mod_name = f"_task_{path.stem}_{abs(hash(path))}"
        mod_spec = importlib.util.spec_from_file_location(mod_name, path)
        module = importlib.util.module_from_spec(mod_spec)
        sys.modules[mod_name] = module
        if str(path.parent) not in sys.path:
            sys.path.insert(0, str(path.parent))
        mod_spec.loader.exec_module(module)
    else:
        module = importlib.import_module(module_part)
    fn = getattr(module, name, None)
    if not callable(fn):
        raise ValueError(f"{spec} is not callable")
    return fn
