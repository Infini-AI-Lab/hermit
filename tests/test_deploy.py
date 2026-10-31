"""Deploying the Lean template into Codex, Claude Code and Pi workspaces."""
import json
from pathlib import Path
import sys
import tomllib

import pytest

from hermit.hosts.claude_code import prepare as claude_prepare
from hermit.hosts.codex import prepare as codex_prepare
from hermit.hosts.pi import prepare as pi_prepare

LEAN = Path(__file__).resolve().parents[1] / "hermit" / "algorithms" / "templates" / "lean_collab"


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


def tree_adapter(tmp_path, hooks=True):
    """An adapter in the Codex-tree layout of the harness2-codex template, with one skill, one subagent, a hook and a
    helper script."""
    a = tmp_path / "tree"
    for d in (".hermit", ".agents/skills/unit-check", "sub_agents/reviewer", "scripts", ".codex"):
        (a / d).mkdir(parents=True)
    (a / ".hermit" / "adapter.yaml").write_text("template: {id: tree-example, version: '1'}\n"
                                                "structure: {learnable: ['**']}\n")
    (a / "exports.yaml").write_text("context: ADAPTER.md\nskills: .agents/skills\nagents: sub_agents\n"
                                    + ("hooks: .codex/hooks.json\n" if hooks else ""))
    (a / "ADAPTER.md").write_text("Run `python3 scripts/stamp.py` before answering.\n")
    (a / ".agents/skills/unit-check/SKILL.md").write_text("---\ndescription: Check units.\n---\nList bare numbers.\n")
    (a / ".agents/skills/README.md").write_text("Skills go here.\n")
    (a / "sub_agents/reviewer/AGENT.md").write_text("---\ndescription: Review a draft.\n---\nCompare it with the task.\n")
    (a / "sub_agents/README.md").write_text("<!-- one directory per role -->\n")
    (a / "scripts/stamp.py").write_text("print('stamped')\n")
    (a / "scripts/stamp.py").chmod(0o755)
    (a / ".codex/hooks.json").write_text(json.dumps({"hooks": {"PostToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "python3 scripts/stamp.py"}]}]}}))
    (a / "notes.md").write_text("undeclared material\n")
    return a


def test_the_whole_adapter_is_copied_and_the_context_says_where(tmp_path):
    a = tree_adapter(tmp_path)
    result = codex_prepare(a, tmp_path / "w", sys.executable)
    copy = tmp_path / "w" / "hermit" / "tree-example"
    assert result["adapter_dir"] == str(copy)
    assert (copy / "notes.md").read_text() == "undeclared material\n" and (copy / "scripts/stamp.py").stat().st_mode & 0o111
    assert not (copy / ".hermit").exists()
    assert set(result["files"]) == {f"hermit/tree-example/{p.relative_to(a).as_posix()}" for p in a.rglob("*")
                                    if p.is_file() and ".hermit" not in p.parts}
    text = (tmp_path / "w" / "AGENTS.md").read_text()
    assert "This adapter's files are in `hermit/tree-example/`." in text and "scripts/stamp.py" in text


def test_declared_skills_roles_and_hooks_are_connected(tmp_path):
    a = tree_adapter(tmp_path)
    result = codex_prepare(a, tmp_path / "w", sys.executable)
    assert result["skills"] == ["unit-check"] and set(result["roles"]) == {"reviewer"}
    role = tomllib.loads((tmp_path / "w" / ".codex/agents/reviewer.toml").read_text())
    assert role["description"] == "Review a draft." and role["developer_instructions"].strip() == "Compare it with the task."
    hook = json.loads((tmp_path / "w" / ".codex/hooks.json").read_text())["hooks"]["PostToolUse"][0]["hooks"][0]
    assert hook["command"].endswith("\npython3 scripts/stamp.py")
    assert hook["command"].startswith(f"cd {tmp_path / 'w' / 'hermit' / 'tree-example'} || exit 1")
    result = claude_prepare(a, tmp_path / "c", sys.executable)
    settings = json.loads((tmp_path / "c" / ".claude/settings.json").read_text())
    assert settings["hooks"]["PostToolUse"][0]["hooks"][0]["command"].endswith("\npython3 scripts/stamp.py")
    assert (tmp_path / "c" / ".claude/agents/reviewer.md").is_file() and result["hooks"]


def test_a_redeployment_drops_hooks_no_longer_declared_and_keeps_other_settings(tmp_path):
    claude_prepare(tree_adapter(tmp_path), tmp_path / "c", sys.executable)
    settings = tmp_path / "c" / ".claude/settings.json"
    settings.write_text(json.dumps({**json.loads(settings.read_text()), "model": "kept"}))
    (tmp_path / "tree" / "exports.yaml").write_text("context: ADAPTER.md\nskills: .agents/skills\nagents: sub_agents\n")
    claude_prepare(tmp_path / "tree", tmp_path / "c", sys.executable)
    assert json.loads(settings.read_text()) == {"model": "kept"}
    codex_prepare(tree_adapter(tmp_path / "x"), tmp_path / "w", sys.executable)
    (tmp_path / "x" / "tree" / "exports.yaml").write_text("context: ADAPTER.md\n")
    codex_prepare(tmp_path / "x" / "tree", tmp_path / "w", sys.executable)
    assert not (tmp_path / "w" / ".codex/hooks.json").exists()


def test_pi_refuses_hooks(tmp_path):
    a = tree_adapter(tmp_path)
    (a / "exports.yaml").write_text("context: ADAPTER.md\nhooks: .codex/hooks.json\n")
    with pytest.raises(ValueError, match="hook"):
        pi_prepare(a, tmp_path / "p", sys.executable)
    assert not (tmp_path / "p").exists()


def test_a_redeployment_replaces_the_copy(tmp_path):
    a = tree_adapter(tmp_path)
    codex_prepare(a, tmp_path / "w", sys.executable)
    copy = tmp_path / "w" / "hermit" / "tree-example"
    (a / "notes.md").unlink()
    (a / "scripts/new.py").write_text("print(1)\n")
    codex_prepare(a, tmp_path / "w", sys.executable)
    assert not (copy / "notes.md").exists() and (copy / "scripts/new.py").is_file()


def test_a_copied_file_replaced_by_a_directory_stops_the_redeployment(tmp_path):
    a = tree_adapter(tmp_path)
    codex_prepare(a, tmp_path / "w", sys.executable)
    notes = tmp_path / "w" / "hermit" / "tree-example" / "notes.md"
    notes.unlink()
    notes.mkdir()
    with pytest.raises(ValueError, match="notes.md is no longer the file the last deployment copied"):
        codex_prepare(a, tmp_path / "w", sys.executable)
    assert notes.is_dir() and not any(notes.iterdir())     # refused before writing


def test_a_changed_copy_or_a_foreign_file_stops_the_redeployment(tmp_path):
    a = tree_adapter(tmp_path)
    codex_prepare(a, tmp_path / "w", sys.executable)
    copy = tmp_path / "w" / "hermit" / "tree-example"
    (copy / "notes.md").write_text("my edit\n")
    before = (tmp_path / "w" / ".hermit-deploy.json").read_text()
    with pytest.raises(ValueError, match="notes.md was changed since the last deployment"):
        codex_prepare(a, tmp_path / "w", sys.executable)
    assert (copy / "notes.md").read_text() == "my edit\n" and (tmp_path / "w" / ".hermit-deploy.json").read_text() == before
    (copy / "notes.md").unlink()
    (copy / "extra.md").write_text("mine\n")
    (a / "extra.md").write_text("the adapter's\n")
    with pytest.raises(ValueError, match="extra.md exists and was not written by a deployment"):
        codex_prepare(a, tmp_path / "w", sys.executable)


def test_a_link_out_of_the_adapter_is_refused(tmp_path):
    a = tree_adapter(tmp_path)
    (tmp_path / "secret.txt").write_text("outside\n")
    (a / "leak.txt").symlink_to(tmp_path / "secret.txt")
    with pytest.raises(ValueError, match="leak.txt leads outside the adapter"):
        codex_prepare(a, tmp_path / "w", sys.executable)


def test_a_hooks_file_must_parse_to_deploy(tmp_path):
    from hermit.adapter.loader import load_manifest
    a = tree_adapter(tmp_path)
    (a / ".codex/hooks.json").write_text(json.dumps({"PostToolUse": []}))
    assert load_manifest(a)["hooks"] == ".codex/hooks.json"        # loading (and training's checks) do not read it
    with pytest.raises(ValueError, match="top-level"):
        codex_prepare(a, tmp_path / "w", sys.executable)
    (a / ".codex/hooks.json").unlink()
    assert codex_prepare(a, tmp_path / "w", sys.executable)["hooks"] is None     # declared, not written yet


def test_a_tree_subagent_loads_as_its_harness_reads_it(tmp_path):
    """An AGENT.md the Codex-tree templates' own checks accept never fails the deploy check: frontmatter that is not
    valid YAML, or none at all, still loads, named by its directory."""
    from hermit.adapter.loader import Adapter
    a = tree_adapter(tmp_path)
    (a / "sub_agents/reviewer/AGENT.md").write_text("---\ndescription: Review: units, dates\n---\nCompare.\n")
    (a / "sub_agents/plain").mkdir()
    (a / "sub_agents/plain/AGENT.md").write_text("Just instructions.\n")
    roles = {r["name"]: r for r in Adapter(a).agent_definitions()}
    assert roles["reviewer"]["description"] == "Review: units, dates" and roles["reviewer"]["body"] == "Compare.\n"
    assert roles["plain"]["description"] == "plain" and roles["plain"]["body"] == "Just instructions.\n"


@pytest.mark.parametrize("template", ["continual_harness", "harness2_codex"])
def test_the_codex_tree_templates_declare_their_skills_and_roles(template):
    from hermit.adapter.loader import load_manifest
    root = Path(__file__).resolve().parents[1] / "hermit" / "algorithms" / "templates" / template
    manifest = load_manifest(root)
    assert manifest["skills"] == ".agents/skills" and manifest["agents"] == "sub_agents"
    assert manifest.get("hooks") == (".codex/hooks.json" if template == "harness2_codex" else None)


def test_the_deployment_record_cannot_reach_outside_the_copy(tmp_path):
    a = tree_adapter(tmp_path)
    codex_prepare(a, tmp_path / "w", sys.executable)
    marker = tmp_path / "w" / ".hermit-deploy.json"
    record = json.loads(marker.read_text())
    victim = tmp_path / "victim.txt"
    victim.write_text("keep\n")
    record["files"]["../victim.txt"] = "0" * 64
    marker.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="not a path under hermit/"):
        codex_prepare(a, tmp_path / "w", sys.executable)
    assert victim.read_text() == "keep\n"


def test_a_linked_copy_folder_is_refused(tmp_path):
    a = tree_adapter(tmp_path)
    codex_prepare(a, tmp_path / "w", sys.executable)
    elsewhere = tmp_path / "elsewhere"
    (tmp_path / "w" / "hermit").rename(elsewhere)
    (tmp_path / "w" / "hermit").symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(ValueError, match="a link is on its way"):
        codex_prepare(a, tmp_path / "w", sys.executable)


@pytest.mark.parametrize("hooks", [True, False])
def test_the_codex_cli_agent_trusts_the_deployed_hooks(tmp_path, monkeypatch, hooks):
    """`codex exec` skips a project's hooks it cannot ask the user to trust; the CLI agent trusts the ones the
    deployment wrote for the task, and only then."""
    import subprocess
    from hermit.agents import cli
    seen, real_run = {}, subprocess.run

    def fake_run(argv, **kwargs):
        if argv[0] != "codex":
            return real_run(argv, **kwargs)
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0)
    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    a = tree_adapter(tmp_path, hooks=hooks)
    cli._run({"id": "t1", "prompt": "hi"}, a, {"backend": "codex", "prompt": "{prompt}"}, tmp_path)
    assert ("--dangerously-bypass-hook-trust" in seen["argv"]) is hooks
