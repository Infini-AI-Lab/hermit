"""Tests for OpenClaw host deployer."""
import json
from pathlib import Path
import sys
import pytest

from hermit.hosts.openclaw import prepare as openclaw_prepare

LEAN = Path(__file__).resolve().parents[1] / "hermit" / "adapter" / "templates" / "lean_collab"
PROMPT_ONLY = Path(__file__).resolve().parents[1] / "hermit" / "adapter" / "templates" / "prompt_only"


def test_openclaw_deploy_prompt_only(tmp_path):
    ws = tmp_path / "po_ws"
    result = openclaw_prepare(PROMPT_ONLY, ws, sys.executable)
    assert result["host"] == "openclaw"
    assert (ws / "AGENTS.md").is_file()
    assert (ws / "openclaw.run.json").is_file()
    assert (ws / ".hermit-deploy.json").is_file()
    assert "hermit:prompt-only:start" in (ws / "AGENTS.md").read_text()
    cfg = json.loads((ws / "openclaw.run.json").read_text())
    assert cfg["skills"]["workshop"]["autonomous"]["mode"] == "off"
    assert "skill_workshop" in cfg["tools"]["deny"]
    assert "mcp" not in cfg
    assert result["server_name"] is None

    # Redeploying is allowed
    openclaw_prepare(PROMPT_ONLY, ws, sys.executable)


def test_openclaw_deploy_with_skills_and_tools(tmp_path):
    ws = tmp_path / "lean_ws"
    result = openclaw_prepare(LEAN, ws, sys.executable)
    assert result["host"] == "openclaw"
    assert (ws / "AGENTS.md").is_file()
    assert (ws / "openclaw.run.json").is_file()
    assert (ws / ".hermit-deploy.json").is_file()
    assert (ws / "skills" / "good-map" / "SKILL.md").is_file()
    assert (ws / "hermit" / "lean-collab" / "ADAPTER.md").is_file()
    assert "good-map" in result["skills"]

    cfg = json.loads((ws / "openclaw.run.json").read_text())
    assert "lean_collab" in cfg["mcp"]["servers"]
    srv = cfg["mcp"]["servers"]["lean_collab"]
    assert srv["command"] == sys.executable
    assert srv["args"][:3] == ["-m", "hermit.runtime.mcp_server", "--adapter"]


def test_openclaw_refuses_roles(tmp_path):
    a = tmp_path / "adapter_roles"
    (a / "agents").mkdir(parents=True)
    (a / ".hermit").mkdir()
    (a / ".hermit" / "adapter.yaml").write_text("template: {id: role-example, version: '1'}\n")
    (a / "exports.yaml").write_text("{context: ADAPTER.md, agents: agents}\n")
    (a / "ADAPTER.md").write_text("instructions\n")
    (a / "agents" / "helper.md").write_text("---\nname: helper\ndescription: A helper agent.\n---\nHelp.\n")

    ws = tmp_path / "role_ws"
    with pytest.raises(ValueError, match="does not support subagent roles"):
        openclaw_prepare(a, ws, sys.executable)
    assert not ws.exists()
