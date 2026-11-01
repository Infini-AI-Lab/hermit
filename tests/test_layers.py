"""The agent layers depend downward only (docs/framework/agent-layers.md): Layer 1 (hermit.agent.model) imports no
other layer, Layer 2 (hermit.agent.base) imports Layer 1 only, and neither imports Layer 3 (hermit.agent.acting,
hermit.agent.evolver) or the framework around them."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "hermit" / "agent"
ABOVE = {"model": ("hermit.agent.base", "hermit.agent.acting", "hermit.agent.evolver", "hermit.trainer",
                   "hermit.adapter", "hermit.tools", "hermit.task"),
         "base": ("hermit.agent.acting", "hermit.agent.evolver", "hermit.trainer", "hermit.adapter", "hermit.tools",
                  "hermit.task")}


def imports(path: Path) -> set[str]:
    out = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.add(node.module)
    return out


def test_each_layer_imports_only_the_layers_below():
    found = []
    for layer, banned in ABOVE.items():
        for path in sorted((ROOT / layer).rglob("*.py")):
            found += [f"{path.relative_to(ROOT)} imports {name}" for name in imports(path)
                      if any(name == b or name.startswith(b + ".") for b in banned)]
    assert found == []


def _layered(agent: dict, evolver: dict | None = None) -> dict:
    from hermit.config import layered
    data = {"agent": {"name": None, "params": {}, **agent}, "evolver": {"name": "m", "params": {}, **(evolver or {})},
            "llm": None}
    layered(data)
    return data


def test_the_per_layer_config_reads_into_the_flat_one():
    import pytest
    flat = _layered({"base": "codex", "params": {"deploy": "tree"}, "model": {"ref": "fw/m", "effort": "high"},
                     "codex": {"web_search": False}})["agent"]
    assert flat == {"name": "codex", "params": {"deploy": "tree", "effort": "high", "model": "fw/m", "web_search": False}}
    # react and direct send the effort with every request
    assert _layered({"base": "react", "model": {"ref": "fw/m", "effort": "high"}})["agent"]["params"]["model"] == \
        {"model": "fw/m", "extra_body": {"reasoning_effort": "high"}}
    assert _layered({"base": "codex"}, {"base": "direct", "model": {"ref": "fw/m", "effort": "low"}})["llm"] == \
        {"model": "fw/m", "extra_body": {"reasoning_effort": "low"}}
    assert _layered({"name": "cli", "base": "claude"})["agent"]["params"] == {"backend": "claude"}
    for agent, match in [({"name": "codex", "base": "claude"}, "different agents"),
                         ({"base": "codex", "params": {"model": "a/b"}, "model": "c/d"}, "set twice"),
                         ({"base": "react", "model": {"name": "x", "login": "chatgpt"}}, "CLI's"),
                         ({"model": "fw/m"}, "need agent.base")]:
        with pytest.raises(ValueError, match=match):
            _layered(agent)
