"""Converting an adapter into the installable form of Claude Code, Codex, Pi and Hermes."""
import json
from pathlib import Path
import sys

import pytest

from harnessadapter.plugins import Unsupported, build

LEAN = Path(__file__).resolve().parents[1] / "harnessadapter" / "templates" / "lean_collab"


def role_adapter(tmp_path):
    a = tmp_path / "adapter"
    (a / "agents").mkdir(parents=True)
    (a / "adapter.yaml").write_text(
        "template: {id: role-demo, version: '1'}\n"
        "exports: {context: ADAPTER.md, agents: agents}\n"
        "structure: {learnable: [ADAPTER.md, agents/*.md]}\n")
    (a / "ADAPTER.md").write_text("Check the units.\n")
    (a / "agents" / "reviewer.md").write_text(
        "---\nname: reviewer\ndescription: Check a result against the task requirements.\n---\n\nRead it.\n")
    return a


def test_claude_and_codex_share_one_artifact(tmp_path):
    out = tmp_path / "art"
    result = build("claude-code", LEAN, out, sys.executable)
    plugin = Path(result["plugin"])
    # One marketplace, two manifests: each host reads its own and finds its own MCP file.
    assert json.loads((out / ".claude-plugin" / "marketplace.json").read_text())["plugins"][0]["name"] == "lean-collab"
    claude = json.loads((plugin / ".claude-plugin" / "plugin.json").read_text())
    codex = json.loads((plugin / ".codex-plugin" / "plugin.json").read_text())
    assert claude["mcpServers"] == "./mcp-claude.json" and codex["mcpServers"] == "./mcp-codex.json"
    # The adapter travels inside the artifact.
    assert (plugin / "adapter" / "adapter.yaml").is_file()


def test_claude_entry_is_relocatable_and_codex_entry_is_not(tmp_path):
    result = build("claude-code", LEAN, tmp_path / "art", sys.executable)
    plugin = Path(result["plugin"])
    claude = json.loads((plugin / "mcp-claude.json").read_text())["mcpServers"]["lean_collab"]
    codex = json.loads((plugin / "mcp-codex.json").read_text())["mcpServers"]["lean_collab"]
    # Claude Code expands ${CLAUDE_PLUGIN_ROOT}; nothing in its entry names this machine.
    assert claude["command"] == "harnessadapter-mcp"
    assert all(not arg.startswith("/") for arg in claude["args"])
    assert "${CLAUDE_PLUGIN_ROOT}/adapter" in claude["args"]
    # Codex expands nothing, so its entry has to carry an absolute path. The result says so.
    assert str(plugin / "adapter") in codex["args"]
    assert result["relocatable"] == {"claude-code": True, "codex": False}


def test_context_becomes_a_skill(tmp_path):
    result = build("claude-code", LEAN, tmp_path / "art", sys.executable)
    skill = Path(result["plugin"]) / "skills" / "lean-collab-context" / "SKILL.md"
    assert skill.is_file() and skill.read_text().startswith("---\nname: \"lean-collab-context\"")
    assert result["context_as"] == "skill:lean-collab-context"
    assert result["skills"][0] == "lean-collab-context"


def test_roles_reach_claude_code_by_convention_not_by_manifest_key(tmp_path):
    a = role_adapter(tmp_path)
    result = build("claude-code", a, tmp_path / "art", sys.executable)
    plugin = Path(result["plugin"])
    assert (plugin / "agents" / "reviewer.md").is_file()
    # Claude Code discovers agents/ itself: the key fails validation as a string and loads nothing as a list.
    assert "agents" not in json.loads((plugin / ".claude-plugin" / "plugin.json").read_text())
    # A Codex plugin cannot carry them, and the result says which ones it leaves behind.
    assert result["omitted"] == {"codex": ["reviewer"]}


def test_codex_refuses_roles_but_claude_code_does_not(tmp_path):
    a = role_adapter(tmp_path)
    with pytest.raises(Unsupported, match="Codex plugin holds skills and MCP servers only"):
        build("codex", a, tmp_path / "x", sys.executable)
    build("claude-code", a, tmp_path / "y", sys.executable)
    # --allow-lossy is the way past it.
    assert build("codex", a, tmp_path / "z", sys.executable, allow_lossy=True)["roles"] == {"reviewer": "reviewer"}


def test_pi_package(tmp_path):
    out = tmp_path / "pi"
    result = build("pi", LEAN, out, sys.executable)
    assert json.loads((out / "package.json").read_text())["keywords"] == ["pi-package"]
    assert (out / "skills" / "lean-collab-context" / "SKILL.md").is_file()
    # Pi reads no MCP server from a package, so it is emitted separately and the result says so.
    assert result["mcp_installed_separately"] and (out / "mcp.json").is_file()
    assert "pi mcp add" in (out / "README.md").read_text()


def test_hermes_import_source(tmp_path):
    out = tmp_path / "h"
    result = build("hermes", LEAN, out, sys.executable)
    # The importer reads mcpServers from .claude.json NEXT TO the source directory.
    assert (out / ".claude" / "CLAUDE.md").is_file()
    assert "lean_collab" in json.loads((out / ".claude.json").read_text())["mcpServers"]
    assert (out / ".claude" / "skills" / "lean-collab-context" / "SKILL.md").is_file()
    # Hermes turns CLAUDE.md into memory entries, so here the context keeps its always-on character.
    assert result["context_as"] == "memory:CLAUDE.md"


@pytest.mark.parametrize("target", ["pi", "hermes"])
def test_targets_without_roles_refuse_them(tmp_path, target):
    a = role_adapter(tmp_path)
    with pytest.raises(Unsupported):
        build(target, a, tmp_path / target, sys.executable)


def test_refuses_a_foreign_nonempty_directory(tmp_path):
    out = tmp_path / "art"
    out.mkdir()
    (out / "someone-elses-file").write_text("x")
    with pytest.raises(ValueError, match="did not create"):
        build("claude-code", LEAN, out, sys.executable)
