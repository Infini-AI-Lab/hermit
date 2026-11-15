"""Deploying the Lean template into Codex, Claude Code, Pi and DeepSeek Harness workspaces, and an adapter into every
host's workspace through its command line for a session another process runs (a task's container)."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib

import pytest
import yaml

from hermit.hosts.claude_code import prepare as claude_prepare
from hermit.hosts.codex import prepare as codex_prepare
from hermit.hosts.dsh import prepare as dsh_prepare
from hermit.hosts.opencode import prepare as opencode_prepare
from hermit.hosts.pi import prepare as pi_prepare

LEAN = Path(__file__).resolve().parents[2] / "hermit" / "adapter" / "templates" / "lean_collab"
HARNESS2_CODEX = Path(__file__).resolve().parents[2] / "hermit" / "adapter" / "templates" / "harness2_codex"
FILE_ADAPTER = Path(__file__).resolve().parents[2] / "docs" / "_examples" / "file_adapter"
CODEX_TREE = {"AGENTS.md": "Do the work.\n- When a table must be read, read .agents/skills/tables/SKILL.md and follow "
                           "it.\n",
              ".agents/skills/tables/SKILL.md": "---\ndescription: reading a table of a bulletin\n---\nRun `python3 "
                                                ".agents/skills/tables/scripts/sum.py`.\n",
              ".agents/skills/tables/scripts/sum.py": "print(1 + 1)\n", "scripts/check.py": "print('ok')\n",
              "sub_agents/reader/AGENT.md": "---\ndescription: reads long files\n---\nSummarise what you read.\n",
              ".codex/hooks.json": json.dumps({"hooks": {"PostToolUse": [{"matcher": "Bash", "hooks": [
                  {"type": "command", "command": "python3 scripts/check.py"}]}]}})}


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


def test_dsh_gets_the_tools_and_skills_as_rows_of_the_workspaces_patch_layer(tmp_path):
    """dsh has no project file for MCP servers or skill folders: the tools are an `insert` row of the workspace's
    patch layer, which a session loads with --patch, and the layer names the workspace's .dsh/skills (dsh reads a
    project's skills at its root only: the nearest folder with a .git, the working directory included, else the
    working directory); roles stay files named in AGENTS.md; a context longer than dsh gives the model of a project's
    instructions is refused."""
    result = dsh_prepare(LEAN, tmp_path / "d", sys.executable)
    layer = json.loads((tmp_path / "d" / ".dsh" / "hermit.patch.yml").read_text())
    [server] = [r for r in layer if "insert" in r]
    assert {"id": "skill-filesystem", "config": {"customSkillDirs": [".dsh/skills"]}} in layer
    [row] = server["insert"]
    assert row["name"] == "@deepseek-ai/dsh-mcp-client" and row["config"]["serverName"] == "lean_collab"
    assert row["config"]["transport"] == "stdio" and row["config"]["args"][:3] == ["-m", "hermit.runtime.mcp_server",
                                                                                  "--adapter"]
    assert row["config"]["toolCallTimeoutMs"] == 120_000           # Codex's tool_timeout_sec, not dsh's 60 s
    # dsh reports a server that does not start (the dsh acting agent fails the session; dsh runs on)
    assert row["config"]["failOnStartupError"] is True
    assert result["patch"] == str(tmp_path / "d" / ".dsh" / "hermit.patch.yml")
    assert "hermit:lean-collab:start" in (tmp_path / "d" / "AGENTS.md").read_text()
    assert (tmp_path / "d" / ".dsh" / "skills" / "good-map" / "SKILL.md").exists()
    assert result["context_injected_via"] == "AGENTS.md" and result["roles"] == {}
    dsh_prepare(LEAN, tmp_path / "d", sys.executable)
    assert (tmp_path / "d" / "AGENTS.md").read_text().count("hermit:lean-collab:start") == 1
    with pytest.raises(ValueError, match="generated for another host"):
        pi_prepare(LEAN, tmp_path / "d", sys.executable)
    a = subagent_adapter(tmp_path)
    result = dsh_prepare(a, tmp_path / "s", sys.executable)
    assert result["placed"] == {"agents": "files"} and result["patch"] is None      # no tools: no server row
    assert json.loads((tmp_path / "s" / ".dsh" / "hermit.patch.yml").read_text()) == []
    assert "`reviewer`: Check a result against the task requirements." in (tmp_path / "s" / "AGENTS.md").read_text()
    big = subagent_adapter(tmp_path / "big")
    (big / "ADAPTER.md").write_text("Check the units.\n" * 4000)       # 68,000 bytes
    with pytest.raises(ValueError, match="past the 65536 bytes dsh gives the model of a project's instructions"):
        dsh_prepare(big, tmp_path / "b", sys.executable)
    assert not (tmp_path / "b" / "AGENTS.md").exists()


def test_dsh_refuses_an_agents_md_dsh_would_cut_and_a_server_name_it_would_refuse(tmp_path):
    """dsh's 65,536 bytes are the instructions as the model is given them, in dsh's frame (304 bytes around one
    AGENTS.md; a `</system-reminder>` in the text one byte longer): an AGENTS.md up to the difference is deployed, one
    byte more is refused (it was deployed, and dsh cut its end, telling only the model). An adapter id that is no MCP
    server name dsh takes is refused before anything is written."""
    import shutil
    from hermit.agent.base.cli import DSH_INSTRUCTION_BYTES, DSH_INSTRUCTION_FRAME
    limit = DSH_INSTRUCTION_BYTES - DSH_INSTRUCTION_FRAME
    edge = subagent_adapter(tmp_path / "edge")
    dsh_prepare(edge, tmp_path / "e0", sys.executable)
    rest = len((tmp_path / "e0" / "AGENTS.md").read_bytes()) - len("Check the units.\n")
    (edge / "ADAPTER.md").write_text("x" * (limit - rest - 1) + "\n")
    dsh_prepare(edge, tmp_path / "e1", sys.executable)
    assert len((tmp_path / "e1" / "AGENTS.md").read_bytes()) == limit
    for text in ("x" * (limit - rest) + "\n", "</system-reminder>" + "x" * (limit - rest - 19) + "\n"):
        (edge / "ADAPTER.md").write_text(text)
        with pytest.raises(ValueError, match=f"with the {DSH_INSTRUCTION_FRAME} bytes dsh puts around it"):
            dsh_prepare(edge, tmp_path / "e2", sys.executable)
    assert not (tmp_path / "e2" / "AGENTS.md").exists()
    long_id = tmp_path / "long"
    shutil.copytree(LEAN, long_id)
    manifest = long_id / ".hermit" / "adapter.yaml"
    manifest.write_text(manifest.read_text().replace("id: lean-collab", "id: lean-collab-" + "x" * 30))
    with pytest.raises(ValueError, match="dsh takes an MCP server name of 32 characters at most"):
        dsh_prepare(long_id, tmp_path / "l", sys.executable)
    assert not (tmp_path / "l").exists()


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


def test_pi_has_no_subagents_so_a_role_is_named_in_the_context(tmp_path):
    a = subagent_adapter(tmp_path)
    result = pi_prepare(a, tmp_path / "p", sys.executable)
    text = (tmp_path / "p" / "AGENTS.md").read_text()
    assert result["placed"] == {"agents": "files"} and result["roles"] == {}
    assert "`reviewer`: Check a result against the task requirements. (instructions: " \
           "`hermit/sub-example/agents/reviewer.md`)" in text
    assert (tmp_path / "p" / "hermit/sub-example/agents/reviewer.md").is_file()


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


def test_a_worktrees_git_pointer_is_not_deployed(tmp_path):
    a = tree_adapter(tmp_path)
    (a / ".git").write_text(f"gitdir: {tmp_path / 'tree' / 'worktrees' / 'adapter'}\n")
    result = codex_prepare(a, tmp_path / "w", sys.executable)
    assert not (tmp_path / "w" / "hermit" / "tree-example" / ".git").exists()
    assert "hermit/tree-example/.git" not in result["files"]
    assert ".git" not in (tmp_path / "w" / "AGENTS.md").read_text()


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


def test_pi_reads_no_hook_file_so_a_hook_is_named_in_the_context(tmp_path):
    a = tree_adapter(tmp_path)
    (a / "exports.yaml").write_text("context: ADAPTER.md\nhooks: .codex/hooks.json\n")
    result = pi_prepare(a, tmp_path / "p", sys.executable)
    text = (tmp_path / "p" / "AGENTS.md").read_text()
    assert result["placed"] == {"hooks": "files"} and result["hooks"] is None
    assert "On `PostToolUse` for `Bash`: run `cd hermit/tree-example && python3 scripts/stamp.py`" in text


def test_the_context_names_the_files_no_export_connects(tmp_path):
    a = tree_adapter(tmp_path)
    result = codex_prepare(a, tmp_path / "w", sys.executable)
    text = (tmp_path / "w" / "AGENTS.md").read_text()
    assert result["placed"] == {"skills": "native", "agents": "native", "hooks": "native"}
    assert "- `hermit/tree-example/notes.md`: undeclared material" in text
    assert "- `hermit/tree-example/scripts/stamp.py`\n" in text                  # code is no description
    assert "SKILL.md`" not in text and "AGENT.md`" not in text and "hooks.json`" not in text   # connected natively
    assert "`hermit/tree-example/ADAPTER.md`" not in text and "exports.yaml`" not in text      # the context itself


def test_a_context_codex_would_cut_is_refused(tmp_path):
    a = subagent_adapter(tmp_path)
    (a / "ADAPTER.md").write_text("x" * 40000)
    with pytest.raises(ValueError, match="project_doc_max_bytes"):
        codex_prepare(a, tmp_path / "w", sys.executable)
    codex_prepare(a, tmp_path / "v", sys.executable, context_limit=65536)
    codex_prepare(a, tmp_path / "u", sys.executable, context_limit=None)


def test_a_redeployment_replaces_the_copy(tmp_path):
    a = tree_adapter(tmp_path)
    codex_prepare(a, tmp_path / "w", sys.executable)
    copy = tmp_path / "w" / "hermit" / "tree-example"
    (a / "notes.md").unlink()
    (a / "scripts/new.py").write_text("print(1)\n")
    codex_prepare(a, tmp_path / "w", sys.executable)
    assert not (copy / "notes.md").exists() and (copy / "scripts/new.py").is_file()


@pytest.mark.parametrize("prepare,skills", [(codex_prepare, ".codex/skills"), (claude_prepare, ".claude/skills"),
                                            (pi_prepare, ".pi/skills"), (opencode_prepare, ".opencode/skills"),
                                            (dsh_prepare, ".dsh/skills")])
def test_a_redeployment_retires_the_skills_and_drops_the_skill_files_the_adapter_no_longer_has(tmp_path, prepare,
                                                                                               skills):
    """A skill the new version no longer exports leaves the host's discovery (its contents kept in .retired-skills/),
    a file a skill no longer has is gone, and a skill the user put there stays."""
    a, w = tree_adapter(tmp_path, hooks=False), tmp_path / "w"
    (a / ".agents/skills/old-trick").mkdir()
    (a / ".agents/skills/old-trick/SKILL.md").write_text("---\ndescription: An old trick.\n---\nDo it.\n")
    (a / ".agents/skills/unit-check/helper.py").write_text("print('help')\n")
    assert prepare(a, w, sys.executable)["skills"] == ["old-trick", "unit-check"]
    (w / skills / "mine").mkdir()
    (w / skills / "mine" / "SKILL.md").write_text("---\ndescription: The user's own.\n---\nKeep it.\n")
    (a / ".agents/skills/old-trick/SKILL.md").unlink()
    (a / ".agents/skills/old-trick").rmdir()
    (a / ".agents/skills/unit-check/helper.py").unlink()
    assert prepare(a, w, sys.executable)["skills"] == ["unit-check"]
    assert sorted(p.name for p in (w / skills).iterdir()) == ["mine", "unit-check"]
    assert sorted(p.name for p in (w / skills / "unit-check").iterdir()) == ["SKILL.md"]
    [retired] = (w / ".retired-skills").glob("*/old-trick/SKILL.md")
    assert retired.read_text().endswith("Do it.\n")
    (a / "exports.yaml").write_text("context: ADAPTER.md\n")            # no skills export at all
    assert prepare(a, w, sys.executable)["skills"] == []
    assert [p.name for p in (w / skills).iterdir()] == ["mine"]


def test_a_redeployment_never_replaces_a_users_skill_or_writes_through_a_linked_skills_folder(tmp_path):
    """A skill the user made that the adapter now exports too, and a skills folder linked elsewhere (a shared one),
    stop the redeployment before anything is written: replacing or retiring there would delete the user's files."""
    a, w = tree_adapter(tmp_path, hooks=False), tmp_path / "w"
    codex_prepare(a, w, sys.executable)
    (w / ".codex/skills/mine").mkdir()
    (w / ".codex/skills/mine/helper.py").write_text("the user's\n")
    (a / ".agents/skills/mine").mkdir()
    (a / ".agents/skills/mine/SKILL.md").write_text("---\ndescription: The adapter's.\n---\nDo it.\n")
    context = (w / "AGENTS.md").read_text()
    (a / "ADAPTER.md").write_text("Changed.\n")
    with pytest.raises(ValueError, match=r"\.codex/skills/mine exists and was not written by a deployment"):
        codex_prepare(a, w, sys.executable)
    assert (w / ".codex/skills/mine/helper.py").is_file() and (w / "AGENTS.md").read_text() == context
    (a / ".agents/skills/mine/SKILL.md").unlink()
    (a / ".agents/skills/mine").rmdir()
    shared = tmp_path / "shared"
    (w / ".codex/skills").rename(shared)
    (w / ".codex/skills").symlink_to(shared)
    (shared / "unit-check" / "notes.md").write_text("the team's\n")
    with pytest.raises(ValueError, match=r"\.codex/skills is a link"):
        codex_prepare(a, w, sys.executable)
    assert (shared / "unit-check" / "notes.md").is_file()


def test_a_skill_the_deployment_record_names_outside_the_skills_folder_is_refused(tmp_path):
    a, w = tree_adapter(tmp_path), tmp_path / "w"
    codex_prepare(a, w, sys.executable)
    record = json.loads((w / ".hermit-deploy.json").read_text())
    (w / ".codex/skills/old").mkdir()
    (w / ".hermit-deploy.json").write_text(json.dumps({**record, "skills": ["unit-check", "old", "../agents"]}))
    with pytest.raises(ValueError, match="Previous skill metadata"):
        codex_prepare(a, w, sys.executable)
    assert (w / ".codex/agents").is_dir() and (w / ".codex/skills/old").is_dir()     # nothing was moved


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
    root = Path(__file__).resolve().parents[2] / "hermit" / "adapter" / "templates" / template
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
    from hermit.agent.acting import workspace as cli
    from hermit.agent.base import cli as base_cli
    seen = {}

    def fake_run(argv, *, stream, **kwargs):
        seen["argv"] = argv
        stream.write_text("")
        return 0, False, False
    monkeypatch.setattr(base_cli, "run_process", fake_run)
    a = tree_adapter(tmp_path, hooks=hooks)
    cli._run({"id": "t1", "prompt": "hi"}, a, {"backend": "codex", "web_search": False, "prompt": "{prompt}"}, tmp_path)
    assert ("--dangerously-bypass-hook-trust" in seen["argv"]) is hooks


def test_an_unattended_session_calls_the_adapters_tools_without_asking(tmp_path):
    from hermit.agent.acting import workspace as cli
    a = Path(__file__).resolve().parents[2] / "docs" / "_examples" / "file_adapter"
    user = codex_prepare(a, tmp_path / "u", sys.executable)
    assert "default_tools_approval_mode" not in (tmp_path / "u" / ".codex" / "config.toml").read_text()
    deployed, _ = cli._deploy({"id": "t"}, a, tmp_path / "w", "codex", {})
    config = tomllib.loads((tmp_path / "w" / ".codex" / "config.toml").read_text())
    assert config["mcp_servers"][user["server_name"]]["default_tools_approval_mode"] == "approve"
    args = cli._session_args("codex", deployed, tmp_path / "w")
    assert any("trust_level" in arg and str(tmp_path / "w") in arg for arg in args)    # its own config is read
    assert not any(arg.startswith("mcp_servers.") for arg in args)   # a partial server would not load


def test_internal_files_stay_out_and_may_not_hide_an_export(tmp_path):
    from hermit.adapter.loader import load_manifest
    a = tree_adapter(tmp_path)
    (a / "exports.yaml").write_text("context: ADAPTER.md\nskills: .agents/skills\ninternal: [./notes.md]\n")
    result = codex_prepare(a, tmp_path / "w", sys.executable)
    assert "hermit/tree-example/notes.md" not in result["files"] and "notes.md" not in (tmp_path / "w" / "AGENTS.md").read_text()
    for hidden in (".agents/skills/", ".agents", "./.agents/skills/unit-check/"):
        (a / "exports.yaml").write_text(f"context: ADAPTER.md\nskills: .agents/skills\ninternal: ['{hidden}']\n")
        with pytest.raises(ValueError, match="internal may not cover what skills"):
            load_manifest(a)


def test_internal_may_not_cover_a_file_an_export_copies(tmp_path):
    a = tree_adapter(tmp_path)
    (a / ".agents/skills/unit-check/private.txt").write_text("secret\n")
    (a / "exports.yaml").write_text("context: ADAPTER.md\nskills: .agents/skills\ninternal: ['.agents/**/private.txt']\n")
    with pytest.raises(ValueError, match="internal may not cover what skills"):
        codex_prepare(a, tmp_path / "w", sys.executable)
    assert not (tmp_path / "w" / ".codex" / "skills" / "unit-check" / "private.txt").exists()


def test_a_hook_type_the_host_does_not_run_is_done_from_the_context_or_refused(tmp_path):
    a = tree_adapter(tmp_path)
    (a / ".codex/hooks.json").write_text(json.dumps({"hooks": {
        "Stop": [{"hooks": [{"type": "prompt", "prompt": "Check the answer has units."}]}],
        "PostToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "python3 scripts/stamp.py"}]}]}}))
    result = codex_prepare(a, tmp_path / "c", sys.executable)                 # Codex runs command hooks only
    hooks = json.loads((tmp_path / "c" / ".codex/hooks.json").read_text())["hooks"]
    text = (tmp_path / "c" / "AGENTS.md").read_text()
    assert set(hooks) == {"PostToolUse"} and result["placed"]["hooks"] == "native and files"
    assert "On `Stop`: check this yourself before you go on: Check the answer has units." in text
    assert "hermit/tree-example/.codex/hooks.json" in text
    result = claude_prepare(a, tmp_path / "d", sys.executable)                # Claude Code runs both
    assert set(json.loads((tmp_path / "d" / ".claude/settings.json").read_text())["hooks"]) == {"Stop", "PostToolUse"}
    assert result["placed"]["hooks"] == "native"
    (a / "exports.yaml").write_text("context: ADAPTER.md\nhooks: .codex/hooks.json\n")
    result = pi_prepare(a, tmp_path / "p", sys.executable)                    # Pi runs none
    text = (tmp_path / "p" / "AGENTS.md").read_text()
    assert result["placed"] == {"hooks": "files"} and "Check the answer has units." in text and "stamp.py" in text
    (a / ".codex/hooks.json").write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "http", "url": "x"}]}]}}))
    with pytest.raises(ValueError, match="'http' hook of Stop"):
        pi_prepare(a, tmp_path / "q", sys.executable)


def test_a_container_session_gets_the_adapters_tools_from_its_own_tool_server(tmp_path):
    from hermit.agent.acting import workspace as cli
    a = Path(__file__).resolve().parents[2] / "docs" / "_examples" / "file_adapter"
    deployed, _ = cli._deploy({"id": "t"}, a, tmp_path / "w", "codex", {"isolation": "container"})
    assert deployed["harness_tools"] == "file-example" and deployed["server_name"] is None
    assert "mcp_servers" not in tomllib.loads((tmp_path / "w" / ".codex" / "config.toml").read_text())
    # Claude Code's as well: a stdio server of the deployment would take the tool server's name and fail in the image
    deployed, _ = cli._deploy({"id": "t"}, a, tmp_path / "c", "claude", {"isolation": "container"})
    assert deployed["harness_tools"] == "file-example" and deployed["server_name"] is None
    assert json.loads((tmp_path / "c" / ".mcp.json").read_text()) == {"mcpServers": {}}
    assert cli._session_args("claude", deployed, tmp_path / "c") == []         # no second --mcp-config


def test_an_adapter_only_hermit_runs_is_deployed_by_the_acting_agent(tmp_path):
    from hermit.agent.acting import workspace as cli
    a = subagent_adapter(tmp_path)
    (a / ".hermit" / "adapter.yaml").write_text(
        "template: {id: sub-example, version: '1'}\nplugin-portable: false\n"
        "structure: {learnable: [exports.yaml, ADAPTER.md, agents/*.md]}\n")
    with pytest.raises(ValueError, match="plugin-portable: false"):
        codex_prepare(a, tmp_path / "u", sys.executable)                  # no installed workspace
    deployed, _ = cli._deploy({"id": "t", "question": "q"}, a, tmp_path / "w", "codex", {})
    assert "Check the units." in (tmp_path / "w" / "AGENTS.md").read_text() and deployed["roles"]


def test_an_exported_roles_instructions_may_not_be_internal(tmp_path):
    from hermit.spec import check_spec
    a = subagent_adapter(tmp_path)
    (a / "private").mkdir()
    (a / "private" / "role.md").write_text("secret steps\n")
    (a / "roles.py").write_text("def export():\n    return [{'name': 'reviewer', 'description': 'Review.', "
                                "'instructions': 'private/role.md'}]\n")
    (a / "exports.yaml").write_text("context: ADAPTER.md\nagents: {entrypoint: roles.py:export}\ninternal: [private/]\n")
    with pytest.raises(Exception, match="instructions private/role.md are internal"):
        codex_prepare(a, tmp_path / "w", sys.executable)
    assert not (tmp_path / "w" / ".codex" / "agents" / "reviewer.toml").exists()
    (a / "public").mkdir()
    (a / "roles.py").write_text("def export():\n    return [{'name': 'reviewer', 'description': 'Review.', "
                                "'instructions': 'public/../private/role.md'}]\n")       # the same file, spelled around
    with pytest.raises(Exception, match="are internal"):
        codex_prepare(a, tmp_path / "w2", sys.executable)
    root = tmp_path / "root"
    import shutil
    shutil.copytree(a, root)
    result = check_spec(a, root)
    assert not result.passed and any(i.check == 5 for i in result.issues)          # refused when submitted too


def test_codex_agent_deploys_a_codex_tree_and_writes_the_home(tmp_path):
    """The Codex agent deploys a Codex-native tree (the harness2-codex template with a skill, a hook, a script and a
    sub-agent) into Codex's own places and writes its home's settings and roles."""
    from hermit.agent.acting import workspace as acting
    from hermit.agent.base import cli
    import shutil
    adapter = tmp_path / "adapter"                  # as the Harness² layout writes the tree (its to_adapter)
    shutil.copytree(HARNESS2_CODEX, adapter, ignore=shutil.ignore_patterns("__pycache__"))
    for rel, text in CODEX_TREE.items():
        (adapter / rel).parent.mkdir(parents=True, exist_ok=True)
        (adapter / rel).write_text(text)
    (adapter / "ADAPTER.md").write_text(CODEX_TREE["AGENTS.md"])
    work, home = tmp_path / "work", tmp_path / "home"
    home.mkdir()
    deployed, _ = acting._deploy({"id": "t1"}, adapter, work, "codex", {})
    assert CODEX_TREE["AGENTS.md"].splitlines()[0] in (work / "AGENTS.md").read_text()
    assert (work / ".codex" / "skills" / "tables" / "scripts" / "sum.py").is_file()       # Codex's own places
    assert (work / ".codex" / "hooks.json").is_file() and (work / ".codex" / "agents" / "reader.toml").is_file()
    copy = work / "hermit" / "harness2-codex"
    assert (copy / "scripts" / "check.py").is_file() and (copy / "sub_agents" / "reader" / "AGENT.md").is_file()
    assert deployed["skills"] == ["tables"] and set(deployed["roles"]) == {"reader"} and deployed["hooks"]
    assert deployed["placed"] == {"skills": "native", "agents": "native", "hooks": "native"}
    assert "--enable" in acting._session_args("codex", deployed, work)               # the roles are spawned
    role = tomllib.loads((work / ".codex" / "agents" / "reader.toml").read_text())
    assert role["developer_instructions"].strip() == "Summarise what you read."
    roles = [{"name": "reader", "description": "reads long files", "instructions": "Summarise what you read."}]
    cli.write_home(home, {"project_doc_max_bytes": 65536}, roles)
    config = tomllib.loads((home / "config.toml").read_text())
    assert config["features"] == {"shell_snapshot": False, "multi_agent": True} and config["approval_policy"] == "never"
    assert config["project_doc_max_bytes"] == 65536 and "forced_login_method" not in config
    assert config["web_search"] == "disabled"
    assert config["shell_environment_policy"]["inherit"] == "core"
    assert config["agents"]["reader"] == {"description": "reads long files", "config_file": "agents/reader.toml"}
    role = tomllib.loads((home / "agents" / "reader.toml").read_text())
    assert role["developer_instructions"] == "Summarise what you read."
    assert acting._deploy({}, None, tmp_path / "work2", "codex", {}) == ({"mode": "host"}, None)   # no adapter
    (tmp_path / "home2").mkdir()
    cli.write_home(tmp_path / "home2", {"login": "chatgpt"}, [])
    config = tomllib.loads((tmp_path / "home2" / "config.toml").read_text())
    assert "multi_agent" not in config["features"] and config["forced_login_method"] == "chatgpt"
    assert config["web_search"] == "disabled"                 # the hosted search would reach past the proxy


# Each host's command line (what hermit.harbor.staging runs), the file its context goes into, and whether the
# workspace names a server for the adapter's tools.
HOST_COMMANDS = {
    "codex": ("hermit.hosts.codex", "AGENTS.md",
              lambda w: "mcp_servers" in tomllib.loads((w / ".codex" / "config.toml").read_text())),
    "claude-code": ("hermit.hosts.claude_code", "CLAUDE.md",
                    lambda w: json.loads((w / ".mcp.json").read_text())["mcpServers"] != {}),
    "pi": ("hermit.hosts.pi", "AGENTS.md",
           lambda w: json.loads((w / ".pi" / "mcp.json").read_text())["mcpServers"] != {}),
    "opencode": ("hermit.hosts.opencode", "AGENTS.md",
                 lambda w: json.loads((w / "opencode.json").read_text())["mcp"]["servers"] != {}),
    "dsh": ("hermit.hosts.dsh", "AGENTS.md",
            lambda w: any("insert" in row for row in json.loads((w / ".dsh" / "hermit.patch.yml").read_text()))),
    "mini-swe-agent": ("hermit.hosts.mini_swe_agent", "mini-swe-agent.yaml",
                       lambda w: "environment_class" in (yaml.safe_load((w / "mini-swe-agent.yaml").read_text())
                                                         .get("environment") or {}))}
NO_MINI = importlib.util.find_spec("minisweagent") is None


@pytest.mark.parametrize("host", [pytest.param(h, marks=pytest.mark.skipif(
    h == "mini-swe-agent" and NO_MINI, reason="needs the mini-swe-agent extra")) for h in HOST_COMMANDS])
def test_every_hosts_command_line_deploys_a_tasks_context_and_leaves_the_tools_to_its_caller(tmp_path, host):
    """A workspace for a session another process runs (hermit.harbor.staging: the task's container) is deployed
    through the host's command line: --context-file is the task's context (UTF-8 text the harness computed), the
    only way an adapter only Hermit runs (plugin-portable: false) deploys; --no-serve-tools leaves the adapter's tools
    to the caller, who serves them itself, so the workspace names no server for them, which would otherwise be the
    caller's server's name."""
    from hermit.sandbox import framework_env
    adapter = tmp_path / "adapter"
    shutil.copytree(FILE_ADAPTER, adapter)
    manifest = adapter / ".hermit" / "adapter.yaml"
    manifest.write_text(manifest.read_text() + "plugin-portable: false\n")
    (tmp_path / "context.md").write_text("Count the words of the task's three paragraphs: ünï.\n", encoding="utf-8")
    module, context_file, serves = HOST_COMMANDS[host]

    def deploy(workspace, *flags):
        return subprocess.run([sys.executable, "-m", module, "--adapter", str(adapter), "--workspace",
                               str(tmp_path / workspace), "--python", sys.executable, *flags],
                              capture_output=True, text=True, env=framework_env(), cwd=tmp_path)
    refused = deploy("w0")
    assert refused.returncode != 0 and "plugin-portable: false" in refused.stderr
    run = deploy("w", "--context-file", str(tmp_path / "context.md"), "--no-serve-tools")
    assert run.returncode == 0, run.stderr[-2000:]
    work = tmp_path / "w"
    text = (work / context_file).read_text(encoding="utf-8")
    assert "Count the words of the task's three paragraphs: ünï." in text and "Review the task requirements" not in text
    assert not serves(work) and json.loads(run.stdout)["adapter_id"] == "file-example"
    with_tools = deploy("w2", "--context-file", str(tmp_path / "context.md"))      # the host's own server, else
    assert with_tools.returncode == 0, with_tools.stderr[-2000:]
    assert serves(tmp_path / "w2")


def test_pi_and_dsh_name_no_server_for_the_tools_their_caller_serves(tmp_path):
    """Pi's and dsh's deployments, called with serve_tools false as the other hosts' are: no server in .pi/mcp.json, no
    server row in dsh's patch layer (its skills row stays), and then no MCP server name for dsh to refuse (the dsh Base
    Agent checks the name of its caller's server itself)."""
    result = pi_prepare(LEAN, tmp_path / "p", sys.executable, serve_tools=False)
    assert json.loads((tmp_path / "p" / ".pi" / "mcp.json").read_text()) == {"mcpServers": {}}
    assert result["server"] is None and result["server_name"] is None and result["audit_log"] is None
    assert (tmp_path / "p" / ".pi" / "skills" / "good-map" / "SKILL.md").is_file()
    result = dsh_prepare(LEAN, tmp_path / "d", sys.executable, serve_tools=False)
    layer = json.loads((tmp_path / "d" / ".dsh" / "hermit.patch.yml").read_text())
    assert layer == [{"id": "skill-filesystem", "config": {"customSkillDirs": [".dsh/skills"]}}]
    assert result["server"] is None and result["server_name"] is None
    long_id = tmp_path / "long"
    shutil.copytree(LEAN, long_id)
    manifest = long_id / ".hermit" / "adapter.yaml"
    manifest.write_text(manifest.read_text().replace("id: lean-collab", "id: lean-collab-" + "x" * 30))
    assert dsh_prepare(long_id, tmp_path / "l", sys.executable, serve_tools=False)["server"] is None


def test_a_pi_workspace_deployed_inside_a_run_sandboxes_its_tool_servers_session(tmp_path, monkeypatch):
    """Deployed inside a run (an acting agent's worker sets HA_ADAPTER_SANDBOX), the tool server Pi's workspace names
    puts the adapter's Session in a sandbox of its own, as Codex's, Claude Code's and dsh's do: the variables travel
    in the server's own env, since a host may start it with a cleared environment."""
    from hermit.adapter.session import ENV_ENFORCE, ENV_PYTHON, ENV_SANDBOX
    monkeypatch.setenv(ENV_SANDBOX, "1")
    monkeypatch.setenv(ENV_ENFORCE, "best_effort")
    pi_prepare(FILE_ADAPTER, tmp_path / "p", sys.executable)
    env = json.loads((tmp_path / "p" / ".pi" / "mcp.json").read_text())["mcpServers"]["file_example"]["env"]
    assert env[ENV_SANDBOX] == "1" and env[ENV_ENFORCE] == "best_effort" and env[ENV_PYTHON] == sys.executable
