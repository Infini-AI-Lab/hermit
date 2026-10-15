"""Deploying the Lean template into Codex and Claude Code workspaces."""
from pathlib import Path
import sys
import tomllib

from harnessadapter.hosts.claude_code import prepare as claude_prepare
from harnessadapter.hosts.codex import prepare as codex_prepare

LEAN = Path(__file__).resolve().parents[1] / "harnessadapter" / "algorithms" / "templates" / "lean_collab"


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


def subagent_adapter(tmp_path):
    a = tmp_path / "adapter"
    (a / "agents").mkdir(parents=True)
    (a / "adapter.yaml").write_text(
        "template: {id: sub-example, version: '1'}\n"
        "exports: {context: ADAPTER.md, agents: agents}\n"
        "structure: {learnable: [ADAPTER.md, agents/*.md]}\n")
    (a / "ADAPTER.md").write_text("Check the units.\n")
    (a / "agents" / "reviewer.md").write_text(
        "---\nname: reviewer\ndescription: Check a result against the task requirements.\n---\n\nRead the task, then the result.\n")
    return a


def test_one_file_subagent(tmp_path):
    a = subagent_adapter(tmp_path)
    codex_prepare(a, tmp_path / "c", sys.executable)
    role = tomllib.loads((tmp_path / "c" / ".codex" / "agents" / "reviewer.toml").read_text())
    assert role["description"].startswith("Check a result") and role["developer_instructions"].strip() == "Read the task, then the result."
    claude_prepare(a, tmp_path / "d", sys.executable)
    text = (tmp_path / "d" / ".claude" / "agents" / "reviewer.md").read_text()
    assert 'name: "reviewer"' in text and text.rstrip().endswith("Read the task, then the result.") and text.count("---") == 2


def test_subagent_without_frontmatter_fails_the_check(tmp_path):
    from harnessadapter.spec import check_spec
    a = subagent_adapter(tmp_path)
    root = tmp_path / "root"
    import shutil
    shutil.copytree(a, root)
    (a / "agents" / "reviewer.md").write_text("no frontmatter\n")
    result = check_spec(a, root)
    assert not result.passed and any(i.check == 5 and i.path == "agents/reviewer.md" for i in result.issues)
