"""Loads a candidate's defense.py in the test process (a legacy API fixture; production runs generated code only in
isolated_hooks)."""
from pathlib import Path
import sys
from types import ModuleType
import uuid


def load_defense(adapter: Path | None):
    if adapter is None:
        return None
    # Compile source directly: no stale import cache or writes into the adapter.
    path = adapter / "defense.py"
    module_name = "_evosafe_candidate_" + uuid.uuid4().hex
    module = ModuleType(module_name)
    module.__file__ = str(path)
    # dataclasses with postponed annotations resolve their defining module at
    # class creation. A unique temporary module also avoids concurrent clashes.
    sys.modules[module_name] = module
    try:
        exec(compile(path.read_text(), str(path), "exec"), module.__dict__)
        defense = module.build()
    finally:
        sys.modules.pop(module_name, None)
    if getattr(defense, "hook_api", "dict-v1") not in {"dict-v1", "structured-v1"}:
        raise ValueError("unsupported defense hook_api")
    for name in ("system_prompt_transform", "on_pre_tool_call", "on_post_tool_call"):
        if not callable(getattr(defense, name, None)):
            raise ValueError(f"defense is missing {name}")
    return defense
