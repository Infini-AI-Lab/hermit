"""Converting an adapter into a plugin in the native format of Claude Code, Codex, Pi and Hermes."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml

from hermit.plugins import Unsupported, build

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "docs" / "_examples" / "file_adapter"        # a short context, a skill, a role and a tool
LEAN = ROOT / "hermit" / "adapter" / "templates" / "lean_collab"  # a context of about 6,000 characters


def variant(tmp_path, *, roles=True, context=True):
    """The example adapter, optionally without its role or with an empty context."""
    a = tmp_path / "adapter"
    shutil.copytree(EXAMPLE, a)
    if not roles:
        exports = yaml.safe_load((a / "exports.yaml").read_text())
        del exports["agents"]
        (a / "exports.yaml").write_text(yaml.safe_dump(exports, sort_keys=False))
        meta = yaml.safe_load((a / ".hermit" / "adapter.yaml").read_text())
        meta["structure"]["learnable"] = [item for item in meta["structure"]["learnable"]
                                          if not str(item if isinstance(item, str) else item["path"]).startswith("agents/")]
        (a / ".hermit" / "adapter.yaml").write_text(yaml.safe_dump(meta, sort_keys=False))
        shutil.rmtree(a / "agents")
    if not context:
        (a / "ADAPTER.md").write_text("")
    return a


def test_claude_code_carries_every_part(tmp_path):
    out = tmp_path / "cc"
    result = build("claude-code", EXAMPLE, out, sys.executable)
    plugin = out / "plugins" / "file-example"
    assert json.loads((out / ".claude-plugin" / "marketplace.json").read_text())["plugins"][0]["source"] == \
        "./plugins/file-example"
    manifest = json.loads((plugin / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["$schema"].startswith("https://anthropic.com/") and "agents" not in manifest
    # Context: an output style that Claude Code applies, and appends to the system prompt, while the plugin is on.
    style = (plugin / "output-styles" / "file-example.md").read_text()
    assert "force-for-plugin: true" in style and style.rstrip().endswith("Check the result before returning it.")
    assert (plugin / "agents" / "reviewer.md").is_file() and (plugin / "skills" / "verify-output" / "SKILL.md").is_file()
    server = json.loads((plugin / ".mcp.json").read_text())["mcpServers"]["file_example"]
    assert server["command"] == "hermit"
    assert server["args"] == ["serve", "${CLAUDE_PLUGIN_ROOT}/adapter",
                              "--audit-log", "${CLAUDE_PLUGIN_DATA}/tool-calls.jsonl"]
    assert (plugin / "adapter" / ".hermit" / "adapter.yaml").is_file()
    assert result["roles"] == {"reviewer": "reviewer"} and result["omitted"] == []


def test_claude_code_without_context_has_no_output_style(tmp_path):
    result = build("claude-code", variant(tmp_path, context=False), tmp_path / "cc", sys.executable)
    assert result["context"] is None and not (Path(result["plugin"]) / "output-styles").exists()


def test_codex_native_layout(tmp_path):
    out = tmp_path / "codex"
    build("codex", variant(tmp_path, roles=False, context=False), out, sys.executable)
    plugin = out / "plugins" / "file-example"
    marketplace = json.loads((out / ".agents" / "plugins" / "marketplace.json").read_text())
    assert marketplace["plugins"][0]["source"] == {"source": "local", "path": "./plugins/file-example"}
    # The primary format: plugin.json at the root, not the .codex-plugin/ fallback, which expands no variables.
    assert json.loads((plugin / "plugin.json").read_text())["$schema"].endswith("/1.0.0/plugin.schema.json")
    assert not (plugin / ".codex-plugin").exists()
    mcp = json.loads((plugin / "mcp.json").read_text())
    assert mcp["mcpServers"]["file_example"]["args"] == ["serve", "${PLUGIN_ROOT}/adapter",
                                                         "--audit-log", "${PLUGIN_DATA}/tool-calls.jsonl"]
    assert (plugin / "skills" / "verify-output" / "SKILL.md").is_file()


def test_codex_refuses_context_and_roles_together(tmp_path):
    with pytest.raises(Unsupported) as error:
        build("codex", EXAMPLE, tmp_path / "x", sys.executable)
    assert "- context:" in str(error.value) and "- roles:" in str(error.value)
    assert not (tmp_path / "x").exists()   # nothing is written before the refusal
    result = build("codex", EXAMPLE, tmp_path / "y", sys.executable, allow_lossy=True)
    assert result["omitted"] == ["context", "roles"]
    plugin = tmp_path / "y" / "plugins" / "file-example"
    assert not (plugin / "agents").exists() and not (plugin / "AGENTS.md").exists()


def test_pi_package(tmp_path):
    out = tmp_path / "pi"
    build("pi", variant(tmp_path, roles=False, context=False), out, sys.executable)
    assert json.loads((out / "package.json").read_text())["keywords"] == ["pi-package"]
    assert (out / "skills" / "verify-output" / "SKILL.md").is_file()
    # Pi reads no MCP server from package files; the extension registers it when Pi loads the package.
    extension = (out / "extensions" / "file-example.ts").read_text()
    assert 'pi.registerMcpServer("file_example"' in extension and 'command: "hermit"' in extension
    assert 'args: ["serve", join(root, "adapter")' in extension
    assert "import.meta.url" in extension


def test_pi_refuses_a_context(tmp_path):
    """Pi carries no context (Codex's refusal: test_codex_refuses_context_and_roles_together)."""
    with pytest.raises(Unsupported, match="- context:"):
        build("pi", variant(tmp_path, roles=False), tmp_path / "pi", sys.executable)


def load_hermes_module(out):
    spec = importlib.util.spec_from_file_location(f"hermes_plugin_{id(out)}", out / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StubContext:
    def __init__(self):
        self.sections, self.skills, self.tools = {}, {}, {}

    def register_system_prompt_section(self, id, content):
        self.sections[id] = content

    def register_skill(self, name, path):
        self.skills[name] = Path(path)

    def register_tool(self, *, name, toolset, schema, handler):
        self.tools[name] = (toolset, schema, handler)


def test_hermes_plugin_registers_context_skills_and_tools(tmp_path):
    out = tmp_path / "hermes"
    result = build("hermes", variant(tmp_path, roles=False), out, sys.executable)
    assert "provides_tools: [file_example_count_words]" in (out / "plugin.yaml").read_text()
    # The plugin's module is the shipped file, copied unchanged; only the data next to it is per adapter.
    assert (out / "__init__.py").read_bytes() == (ROOT / "hermit" / "plugins" / "hermes" /
                                                  "plugin_init.py").read_bytes()
    ctx = StubContext()
    load_hermes_module(out).register(ctx)
    assert ctx.sections["file-example"].startswith("Review the task requirements")
    assert ctx.skills["verify-output"] == out / "skills" / "verify-output" / "SKILL.md"
    toolset, schema, _ = ctx.tools["file_example_count_words"]
    assert toolset == "file_example" and schema["parameters"]["required"] == ["text"]
    assert result["context_chars"] == len("Review the task requirements before acting. Check the result before "
                                          "returning it.")


def test_hermes_refuses_a_context_it_would_drop_and_roles(tmp_path):
    # Hermes drops a section over 4,000 characters with only a log warning; the Lean context is longer.
    with pytest.raises(Unsupported, match="4,000 characters"):
        build("hermes", LEAN, tmp_path / "lean", sys.executable)
    with pytest.raises(Unsupported, match="- roles:"):
        build("hermes", EXAMPLE, tmp_path / "example", sys.executable)


def test_hermes_bridge_runs_the_tool(tmp_path, monkeypatch):
    if not (Path(sys.executable).parent / "hermit").exists():
        pytest.skip("the hermit console script is not installed next to this interpreter")
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""))
    out = tmp_path / "hermes"
    build("hermes", variant(tmp_path, roles=False), out, sys.executable)
    ctx = StubContext()
    load_hermes_module(out).register(ctx)
    handler = ctx.tools["file_example_count_words"][2]
    assert json.loads(handler({"text": "one two three"})) == {"count": 3}
    assert json.loads(handler({"text": "four five"})) == {"count": 2}   # the same server, reused


def test_refuses_a_foreign_nonempty_directory(tmp_path):
    out = tmp_path / "cc"
    out.mkdir()
    (out / "someone-elses-file").write_text("x")
    with pytest.raises(ValueError, match="did not create"):
        build("claude-code", EXAMPLE, out, sys.executable)


def test_serve_runs_the_tool_server(tmp_path):
    """`hermit serve DIR` is the line the plugins carry: an MCP server on stdio for one host session."""
    import subprocess
    audit = tmp_path / "calls.jsonl"
    proc = subprocess.Popen([sys.executable, "-m", "hermit.cli", "serve", str(EXAMPLE), "--audit-log", str(audit)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)

    def request(id, method, params):
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": id, "method": method, "params": params}) + "\n")
        proc.stdin.flush()
        while True:
            message = json.loads(proc.stdout.readline())
            if message.get("id") == id:
                return message["result"]

    try:
        request(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                  "clientInfo": {"name": "test", "version": "0"}})
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        proc.stdin.flush()
        assert [t["name"] for t in request(2, "tools/list", {})["tools"]] == ["count_words"]
        result = request(3, "tools/call", {"name": "count_words", "arguments": {"text": "a b c d"}})
        assert result["structuredContent"] == {"count": 4}
    finally:
        proc.kill()
    assert json.loads(audit.read_text().splitlines()[-1])["result"] == {"count": 4}
    assert "made by hermit" not in proc.communicate()[1]       # no plugin's record beside it: no version warning


def test_reconversion_drops_the_parts_the_adapter_lost(tmp_path):
    """Converting again into the same output replaces the generated parts: a skill, a role or the context the
    adapter no longer has does not survive from the earlier plugin."""
    out = tmp_path / "cc"
    adapter = variant(tmp_path)
    build("claude-code", adapter, out, sys.executable)
    plugin = out / "plugins" / "file-example"
    assert (plugin / "skills" / "verify-output").is_dir() and (plugin / "agents" / "reviewer.md").is_file()
    shutil.rmtree(adapter)
    adapter = variant(tmp_path, roles=False, context=False)
    shutil.rmtree(adapter / "skills" / "verify-output")
    result = build("claude-code", adapter, out, sys.executable)
    assert result["skills"] == [] and result["roles"] == {} and result["context"] is None
    assert not (plugin / "skills" / "verify-output").exists()
    assert not (plugin / "agents").exists() and not (plugin / "output-styles").exists()
    assert (plugin / "adapter" / ".hermit" / "adapter.yaml").is_file() and (plugin / ".mcp.json").is_file()


@pytest.mark.parametrize("target", ["claude-code", "codex", "pi", "hermes"])
def test_plugin_records_the_framework_version(tmp_path, target):
    from hermit import __version__
    result = build(target, variant(tmp_path, roles=False, context=False), tmp_path / target, sys.executable)
    root = Path(result.get("plugin") or result["output"])
    record = json.loads((root / "hermit.json").read_text())
    assert record["plugin"] == "file-example" and record["version"] == "1.0"
    assert record["framework"] == __version__ == result["framework"]
    if target == "hermes":
        assert record["toolset"] == "file_example" and record["tools"][0]["hermes_name"] == "file_example_count_words"


def test_serve_warns_when_the_framework_version_differs(tmp_path):
    import subprocess
    result = build("claude-code", variant(tmp_path, roles=False, context=False), tmp_path / "cc", sys.executable)
    plugin = Path(result["plugin"])
    record = json.loads((plugin / "hermit.json").read_text())
    (plugin / "hermit.json").write_text(json.dumps({**record, "framework": "0.0.0"}))
    proc = subprocess.run([sys.executable, "-m", "hermit.cli", "serve", str(plugin / "adapter")],
                          input="", capture_output=True, text=True, timeout=60)
    assert "made by hermit 0.0.0" in proc.stderr       # and none without the record: test_serve_runs_the_tool_server


@pytest.mark.parametrize("target,marker", [("pi", "package.json"), ("hermes", "plugin.yaml")])
def test_refuses_a_foreign_package_with_the_hosts_own_marker(tmp_path, target, marker):
    """A Pi package or a Hermes plugin this framework did not make has that marker too; only the record
    `convert` writes makes a directory ours, so a foreign one is never cleared."""
    out = tmp_path / target
    out.mkdir()
    (out / marker).write_text("{}")
    (out / "skills").mkdir()
    with pytest.raises(ValueError, match="did not create"):
        build(target, variant(tmp_path, roles=False, context=False), out, sys.executable)
    assert (out / "skills").is_dir()


@pytest.mark.parametrize("target", ["claude-code", "codex", "pi", "hermes"])
def test_hooks_are_refused_by_every_target(tmp_path, target):
    """No plugin carries hooks yet: converting an adapter that has them fails, or with --allow-lossy reports them."""
    a = variant(tmp_path, roles=False, context=False)
    (a / "hooks.json").write_text(json.dumps({"hooks": {"PostToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "true"}]}]}}))
    (a / "exports.yaml").write_text((a / "exports.yaml").read_text() + "hooks: hooks.json\n")
    with pytest.raises(Unsupported, match="- hooks: no plugin carries an adapter's hooks yet \\(PostToolUse\\)"):
        build(target, a, tmp_path / "x", sys.executable)
    assert not (tmp_path / "x").exists()
    assert build(target, a, tmp_path / "y", sys.executable, allow_lossy=True)["omitted"] == ["hooks"]


# ---- the adapter's files: named to the session as a deployment names them ------------------------------------------
def _session_start(plugin: Path, cwd: Path) -> str:
    """What the plugin's SessionStart hooks print, run as Claude Code runs them: `sh -c` with CLAUDE_PLUGIN_ROOT set."""
    hooks = json.loads((plugin / "hooks" / "hooks.json").read_text())["hooks"]["SessionStart"]
    return "".join(subprocess.run(["sh", "-c", h["command"]], cwd=cwd, capture_output=True, text=True, check=True,
                                  env={**os.environ, "CLAUDE_PLUGIN_ROOT": str(plugin)}).stdout
                   for group in hooks for h in group["hooks"])


def test_claude_code_names_the_adapters_folder_and_files_at_session_start(tmp_path):
    from hermit.hosts.codex import prepare
    installed = tmp_path / "plugins dir"                          # a path with a space stays one word
    result = build("claude-code", EXAMPLE, installed, sys.executable)
    plugin = installed / "plugins" / "file-example"
    assert result["folder_hook"] == "hooks/hooks.json" and result["unnamed_files"] == []
    text = _session_start(plugin, tmp_path)
    assert text.startswith(f"The files of the `file-example` adapter are in `{plugin}/adapter/`.")
    # the same list a deployment's context gives, with paths in the adapter's folder
    prepare(EXAMPLE, tmp_path / "w", sys.executable)
    deployed = (tmp_path / "w" / "AGENTS.md").read_text()
    listed = [line.replace("hermit/file-example/", "") for line in deployed.splitlines()
              if line.startswith("- `hermit/file-example/")]
    assert listed == ["- `README.md`: File interface example",
                      "- `tools.py`: A fixed tool used to demonstrate the adapter's deployment interface."]
    assert [line for line in text.splitlines() if line.startswith("- `")] == listed
    assert all((plugin / "adapter" / line[3:line.index("`", 3)]).is_file() for line in listed)


def test_claude_code_names_no_folder_when_nothing_of_the_adapter_lives_there(tmp_path):
    a = variant(tmp_path, roles=False)
    shutil.rmtree(a / "skills")
    (a / "README.md").unlink()
    (a / "exports.yaml").write_text("context: ADAPTER.md\n")
    (a / "tools.py").unlink()
    result = build("claude-code", a, tmp_path / "cc", sys.executable)
    assert result["folder_hook"] is None and not (Path(result["plugin"]) / "hooks").exists()


def test_a_reconversion_drops_the_list_of_files_the_adapter_no_longer_has(tmp_path):
    a = variant(tmp_path)
    build("claude-code", a, tmp_path / "cc", sys.executable)
    plugin = tmp_path / "cc" / "plugins" / "file-example"
    assert (plugin / "hermit-files.md").is_file()
    (a / "README.md").unlink()
    (a / "exports.yaml").write_text("context: ADAPTER.md\nagents: agents\nskills: skills\n")
    (a / "tools.py").unlink()
    build("claude-code", a, tmp_path / "cc", sys.executable)
    assert not (plugin / "hermit-files.md").exists()             # the folder is still named: skills and a role
    text = _session_start(plugin, tmp_path)
    assert "Other files" not in text and "adapter/`." in text
    shutil.rmtree(a / "skills")
    shutil.rmtree(a / "agents")
    (a / "exports.yaml").write_text("context: ADAPTER.md\n")
    result = build("claude-code", a, tmp_path / "cc", sys.executable)
    assert result["folder_hook"] is None and not (plugin / "hooks").exists()   # nothing left for a hook to name
    assert "none is named to the session" in (plugin / "README.md").read_text()


def test_hermes_names_the_adapters_folder_and_files_at_the_end_of_its_section(tmp_path):
    out = tmp_path / "installed here"
    result = build("hermes", variant(tmp_path, roles=False), out, sys.executable)
    assert result["files_named"] is True and result["unnamed_files"] == []
    ctx = StubContext()
    load_hermes_module(out).register(ctx)
    section = ctx.sections["file-example"]
    assert section.startswith("Review the task requirements")
    assert f"The files of the `file-example` adapter are in `{out.resolve()}/adapter/`." in section
    assert "- `README.md`: File interface example" in section and "- `tools.py`:" in section
    assert len(section) <= 4_000


def test_hermes_reports_the_files_when_naming_them_would_pass_its_section_limit(tmp_path):
    a = variant(tmp_path, roles=False)
    (a / "ADAPTER.md").write_text("Check the result. " * 210)              # about 3,800 characters: within 4,000
    result = build("hermes", a, tmp_path / "h", sys.executable)
    assert result["files_named"] is False and result["unnamed_files"] == ["README.md", "tools.py"]
    ctx = StubContext()
    load_hermes_module(tmp_path / "h").register(ctx)
    assert "adapter/`" not in ctx.sections["file-example"]


@pytest.mark.parametrize("target", ["codex", "pi"])
def test_a_target_that_cannot_name_its_folder_reports_the_files_the_session_is_not_told_about(tmp_path, target):
    result = build(target, variant(tmp_path, roles=False, context=False), tmp_path / target, sys.executable)
    assert result["unnamed_files"] == ["README.md", "tools.py"]
    readme = (Path(result.get("plugin") or result["output"]) / "README.md").read_text()
    assert "- Files the session is not told about: README.md, tools.py (under `adapter/`" in readme
