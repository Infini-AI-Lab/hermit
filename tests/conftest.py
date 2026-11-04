import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest


@pytest.fixture(autouse=True)
def shared_login(tmp_path_factory, monkeypatch):
    """The shared copy of a ChatGPT login (hermit.agent.base.login.ROOT) in a directory of the test's own, never the
    user's ~/.cache."""
    from hermit.agent.base import login
    root = tmp_path_factory.mktemp("codex-login")
    monkeypatch.setattr(login, "ROOT", root)
    monkeypatch.setattr(login, "PLAN", root / "plan")         # and its plan
    monkeypatch.setenv("HERMIT_HOMES", str(root / "homes"))   # and the CLI homes, never the user's ~/.cache
    return root


# Legacy API fixture loader; production runs generated code only in isolated_hooks.
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
