"""Deploying the Lean template into Codex and Claude Code workspaces."""
from pathlib import Path
import sys
import tomllib

from harnessadapter.hosts.claude_code import prepare as claude_prepare
from harnessadapter.hosts.codex import prepare as codex_prepare

LEAN = Path(__file__).resolve().parents[1] / "harnessadapter" / "templates" / "lean_collab"


def test_codex(tmp_path):
    result = codex_prepare(LEAN, tmp_path / "w", sys.executable)
    config = tomllib.loads((tmp_path / "w" / ".codex" / "config.toml").read_text())
    assert "lean_collab" in config["mcp_servers"]
    assert "harnessadapter:lean-collab:start" in (tmp_path / "w" / "AGENTS.md").read_text()
    assert set(result["skills"]) == {"good-leaf", "good-map", "kernel-check", "lean-workflow", "solving-together"}
    # Redeploying into the same workspace is allowed; a foreign nonempty workspace is not.
    codex_prepare(LEAN, tmp_path / "w", sys.executable)


def test_claude(tmp_path):
    result = claude_prepare(LEAN, tmp_path / "c", sys.executable)
    assert (tmp_path / "c" / ".mcp.json").exists() and (tmp_path / "c" / ".claude" / "skills" / "good-map" / "SKILL.md").exists()
    assert result["context_injected_via"] == "CLAUDE.md"
