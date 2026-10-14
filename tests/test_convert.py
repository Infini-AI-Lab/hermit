"""Converting an adapter into a plugin in the native format of Claude Code, Codex, Pi and Hermes."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys

import pytest
import yaml

from harnessadapter.plugins import Unsupported, build

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "docs" / "_examples" / "file_adapter"        # a short context, a skill, a role and a tool
LEAN = ROOT / "harnessadapter" / "templates" / "lean_collab"  # a context of about 6,000 characters


def variant(tmp_path, *, roles=True, context=True):
    """The example adapter, optionally without its role or with an empty context."""
    a = tmp_path / "adapter"
    shutil.copytree(EXAMPLE, a)
    if not roles:
        manifest = yaml.safe_load((a / "adapter.yaml").read_text())
        del manifest["exports"]["agents"]
        manifest["structure"]["learnable"] = [item for item in manifest["structure"]["learnable"]
                                              if not item["path"].startswith("agents/")]
        (a / "adapter.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
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
    assert server["command"] == "harnessadapter"
    assert server["args"] == ["serve", "${CLAUDE_PLUGIN_ROOT}/adapter",
                              "--audit-log", "${CLAUDE_PLUGIN_DATA}/tool-calls.jsonl"]
    assert (plugin / "adapter" / "adapter.yaml").is_file()
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
    assert 'pi.registerMcpServer("file_example"' in extension and 'command: "harnessadapter"' in extension
    assert 'args: ["serve", join(root, "adapter")' in extension
    assert "import.meta.url" in extension


@pytest.mark.parametrize("target", ["pi", "codex"])
def test_targets_without_context_refuse_it(tmp_path, target):
    with pytest.raises(Unsupported, match="- context:"):
        build(target, variant(tmp_path, roles=False), tmp_path / target, sys.executable)


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
    if not (Path(sys.executable).parent / "harnessadapter").exists():
        pytest.skip("the harnessadapter console script is not installed next to this interpreter")
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
    """`harnessadapter serve DIR` is the line the plugins carry: an MCP server on stdio for one host session."""
    import subprocess
    audit = tmp_path / "calls.jsonl"
    proc = subprocess.Popen([sys.executable, "-m", "harnessadapter.cli", "serve", str(EXAMPLE), "--audit-log", str(audit)],
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
