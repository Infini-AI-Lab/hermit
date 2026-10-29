"""Deploying adapters into Codex, Claude Code, Pi and OpenCode workspaces."""
import json
from pathlib import Path
import sys
import tomllib

import pytest
import yaml

from hermit.hosts.claude_code import prepare as claude_prepare
from hermit.hosts.codex import prepare as codex_prepare
from hermit.hosts.pi import prepare as pi_prepare

LEAN = Path(__file__).resolve().parents[1] / "hermit" / "algorithms" / "templates" / "lean_collab"
EXAMPLE = Path(__file__).resolve().parents[1] / "docs" / "_examples" / "file_adapter"


def test_codex(tmp_path):
    result = codex_prepare(LEAN, tmp_path / "w", sys.executable)
    config = tomllib.loads((tmp_path / "w" / ".codex" / "config.toml").read_text())
    assert "lean_collab" in config["mcp_servers"]
    assert "hermit:lean-collab:start" in (tmp_path / "w" / "AGENTS.md").read_text()
    assert set(result["skills"]) == {"good-leaf", "good-map", "kernel-check", "lean-workflow", "solving-together"}
    # Redeploying into the same workspace is allowed; a foreign nonempty workspace is not.
    codex_prepare(LEAN, tmp_path / "w", sys.executable)


def test_claude(tmp_path):
    result = claude_prepare(LEAN, tmp_path / "c", sys.executable)
    assert (tmp_path / "c" / ".mcp.json").exists() and (tmp_path / "c" / ".claude" / "skills" / "good-map" / "SKILL.md").exists()
    assert result["context_injected_via"] == "CLAUDE.md"


def test_pi(tmp_path):
    result = pi_prepare(LEAN, tmp_path / "p", sys.executable)
    mcp = json.loads((tmp_path / "p" / ".pi" / "mcp.json").read_text())
    assert mcp["mcpServers"]["lean_collab"]["args"][:3] == ["-m", "hermit.runtime.mcp_server", "--adapter"]
    assert "hermit:lean-collab:start" in (tmp_path / "p" / "AGENTS.md").read_text()
    assert (tmp_path / "p" / ".pi" / "skills" / "good-map" / "SKILL.md").exists()
    assert result["context_injected_via"] == "AGENTS.md" and result["roles"] == {}
    pi_prepare(LEAN, tmp_path / "p", sys.executable)
    assert (tmp_path / "p" / "AGENTS.md").read_text().count("hermit:lean-collab:start") == 1
    with pytest.raises(ValueError, match="generated for another host"):
        codex_prepare(LEAN, tmp_path / "p", sys.executable)


def subagent_adapter(tmp_path):
    a = tmp_path / "adapter"
    (a / "agents").mkdir(parents=True)
    (a / ".hermit").mkdir()
    (a / ".hermit" / "adapter.yaml").write_text(
        "template: {id: sub-example, version: '1'}\n"
        "structure: {learnable: [exports.yaml, ADAPTER.md, agents/*.md]}\n")
    (a / "exports.yaml").write_text("{context: ADAPTER.md, agents: agents}\n")
    (a / "ADAPTER.md").write_text("Check the units.\n")
    (a / "agents" / "reviewer.md").write_text(
        "---\nname: reviewer\ndescription: Check a result against the task requirements.\n---\n\nRead the task, then the result.\n")
    return a


def test_pi_refuses_roles(tmp_path):
    a = subagent_adapter(tmp_path)
    with pytest.raises(ValueError, match="reviewer"):
        pi_prepare(a, tmp_path / "p", sys.executable)
    assert not (tmp_path / "p").exists()


def test_one_file_subagent(tmp_path):
    a = subagent_adapter(tmp_path)
    codex_prepare(a, tmp_path / "c", sys.executable)
    role = tomllib.loads((tmp_path / "c" / ".codex" / "agents" / "reviewer.toml").read_text())
    assert role["description"].startswith("Check a result") and role["developer_instructions"].strip() == "Read the task, then the result."
    claude_prepare(a, tmp_path / "d", sys.executable)
    text = (tmp_path / "d" / ".claude" / "agents" / "reviewer.md").read_text()
    assert 'name: "reviewer"' in text and text.rstrip().endswith("Read the task, then the result.") and text.count("---") == 2


def test_subagent_without_frontmatter_fails_the_check(tmp_path):
    from hermit.spec import check_spec
    a = subagent_adapter(tmp_path)
    root = tmp_path / "root"
    import shutil
    shutil.copytree(a, root)
    (a / "agents" / "reviewer.md").write_text("no frontmatter\n")
    result = check_spec(a, root)
    assert not result.passed and any(i.check == 5 and i.path == "agents/reviewer.md" for i in result.issues)


@pytest.fixture
def opencode_prepare():
    from hermit.hosts.opencode import prepare
    return prepare


def test_opencode(tmp_path, opencode_prepare):
    workspace = tmp_path / "workspace with spaces"
    result = opencode_prepare(EXAMPLE, workspace, sys.executable)
    config = json.loads((workspace / "opencode.json").read_text())
    server = config["mcp"]["file_example"]
    assert config["$schema"] == "https://opencode.ai/config.json"
    assert server["type"] == "local" and server["enabled"] is True
    assert server["command"] == [sys.executable, "-m", "hermit.runtime.mcp_server", "--adapter", str(EXAMPLE),
                                  "--audit-log", str(workspace / "tool-calls.jsonl")]
    assert Path(server["environment"]["PYTHONPATH"]) == EXAMPLE.parents[2]
    assert "hermit:file-example:start" in (workspace / "AGENTS.md").read_text()
    assert result["skills"] == ["verify-output"]
    assert ((workspace / ".opencode/skills/verify-output/SKILL.md").read_bytes()
            == (EXAMPLE / "skills/verify-output/SKILL.md").read_bytes())
    assert result["host"] == "opencode" and result["context_injected_via"] == "AGENTS.md"
    assert result["server"] == server
    assert json.loads((workspace / ".hermit-deploy.json").read_text()) == result


def test_opencode_roles_and_redeployment(tmp_path, opencode_prepare):
    adapter = subagent_adapter(tmp_path)
    role = adapter / "agents/reviewer.md"
    role.write_text(role.read_text().replace("name: reviewer", "name: unit_reviewer"))
    (adapter / "ADAPTER.md").write_text("Ask `unit_reviewer` to check the units.\n")
    workspace = tmp_path / "w"
    result = opencode_prepare(adapter, workspace, sys.executable)
    native = workspace / ".opencode/agents/unit-reviewer.md"
    text = native.read_text()
    frontmatter = yaml.safe_load(text.split("---", 2)[1])
    assert frontmatter["mode"] == "subagent" and frontmatter["description"].startswith("Check a result")
    assert text.rstrip().endswith("Read the task, then the result.")
    assert result["roles"] == {"unit-reviewer": str(native)}
    assert result["role_name_map"] == {"unit_reviewer": "unit-reviewer"}
    context = workspace / "AGENTS.md"
    assert "`unit-reviewer`" in context.read_text() and "`unit_reviewer`" not in context.read_text()
    context.write_text("Keep my project notes.\n\n" + context.read_text())
    (adapter / "ADAPTER.md").write_text("Updated instructions.\n")
    role.unlink()
    result = opencode_prepare(adapter, workspace, sys.executable)
    assert context.read_text().startswith("Keep my project notes.")
    assert context.read_text().count("hermit:sub-example:start") == 1
    assert "Updated instructions." in context.read_text() and "Ask `unit-reviewer`" not in context.read_text()
    assert not native.exists() and result["roles"] == {}
    assert [p.read_text() for p in workspace.glob(".retired-roles/*/unit-reviewer.md")] == [text]
    assert json.loads((workspace / "opencode.json").read_text())["mcp"] == {}
    assert result["server"] is None and result["audit_log"] is None and result["skills_dir"] is None


@pytest.mark.parametrize("placement", ["same", "child", "parent", "foreign", "other-host"])
def test_opencode_workspace_protection(tmp_path, opencode_prepare, placement):
    adapter = subagent_adapter(tmp_path)
    workspace = {"same": adapter, "child": adapter / "nested", "parent": tmp_path}.get(placement, tmp_path / "w")
    if placement in ("foreign", "other-host"):
        workspace.mkdir()
        (workspace / "keep.txt").write_text("do not overwrite")
        if placement == "other-host":
            (workspace / ".hermit-deploy.json").write_text('{"host": "codex"}')
    with pytest.raises(ValueError):
        opencode_prepare(adapter, workspace, sys.executable)
    assert not (workspace / "opencode.json").exists()
    if placement in ("foreign", "other-host"):
        assert (workspace / "keep.txt").read_text() == "do not overwrite"


def test_opencode_role_name_collision(tmp_path, opencode_prepare):
    adapter = subagent_adapter(tmp_path)
    role = adapter / "agents/reviewer.md"
    role.write_text(role.read_text().replace("name: reviewer", "name: unit_reviewer"))
    (adapter / "agents/other.md").write_text(role.read_text().replace("name: unit_reviewer", "name: unit-reviewer"))
    with pytest.raises(ValueError, match="collide"):
        opencode_prepare(adapter, tmp_path / "w", sys.executable)
    assert not (tmp_path / "w/opencode.json").exists()


def test_opencode_sandbox_environment(tmp_path, monkeypatch, opencode_prepare):
    import hermit.hosts.opencode as host
    from hermit.adapter.session import ENV_ENFORCE, ENV_PYTHON, ENV_SANDBOX
    monkeypatch.setattr(host, "render_exports", lambda *args: {"context": "test", "agents": []})
    monkeypatch.setenv(ENV_SANDBOX, "1")
    monkeypatch.setenv(ENV_ENFORCE, "required")
    result = opencode_prepare(EXAMPLE, tmp_path / "w", sys.executable, learning_material=tmp_path / "material")
    env = result["server"]["environment"]
    assert env[ENV_SANDBOX] == "1" and env[ENV_ENFORCE] == "required" and env[ENV_PYTHON] == sys.executable
    assert result["server"]["command"][-2:] == ["--learning-material", str(tmp_path / "material")]


def test_opencode_cli(tmp_path, capsys):
    from hermit.cli import main
    adapter = subagent_adapter(tmp_path)
    assert main(["deploy", "--adapter", str(adapter), "--workspace", str(tmp_path / "w"), "--host", "opencode"]) == 0
    assert json.loads(capsys.readouterr().out)["host"] == "opencode"


def test_opencode_mcp_command_runs_tool(tmp_path, opencode_prepare):
    import asyncio
    from mcp.client import Client
    from mcp.client.stdio import StdioServerParameters
    workspace = tmp_path / "workspace with spaces"
    opencode_prepare(EXAMPLE, workspace, sys.executable)
    server = json.loads((workspace / "opencode.json").read_text())["mcp"]["file_example"]

    async def call():
        async with asyncio.timeout(30):
            async with Client(StdioServerParameters(command=server["command"][0], args=server["command"][1:],
                                                    env=server["environment"], cwd=workspace)) as client:
                assert [tool.name for tool in (await client.list_tools()).tools] == ["count_words"]
                result = await client.call_tool("count_words", {"text": "one two three"})
                assert not result.is_error and result.structured_content == {"count": 3}

    asyncio.run(call())
    assert json.loads((workspace / "tool-calls.jsonl").read_text().splitlines()[-1])["result"] == {"count": 3}
