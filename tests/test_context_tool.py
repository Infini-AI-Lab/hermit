"""The per-task adapter context: `context_tool` in `exports.yaml`, a declared tool the harness calls once per task with the
task's input, whose text is appended below ADAPTER.md (Session.context(query=..., task=...)). Tested on a minimal
adapter of its own, independent of any method's template."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from hermit import Session
from hermit.adapter.loader import agent_tools, load_manifest

ROOT = Path(__file__).resolve().parents[1]
META = """\
template: {id: ctxdemo, version: '1', contract: '2'}
structure:
  learnable: [{path: ADAPTER.md, format: {text: true}}]
  fixed: [interface.py, exports.yaml]
plugin-portable: false
"""
EXPORTS = """\
context: ADAPTER.md
tools:
  - name: lookup
    description: the harness's per-task context
    entrypoint: interface.py:lookup
    input_schema: {type: object, properties: {query: {type: string}, task: {type: object}}}
    output_schema: {type: object}
  - name: calc
    description: a tool for the agent
    entrypoint: interface.py:calc
    input_schema: {type: object, properties: {x: {type: number}}}
    output_schema: {type: object}
context_tool: lookup
"""
INTERFACE = '''\
def lookup(query="", task=None):
    task = task or {}
    if not query:
        return {"text": "", "retrieved": []}
    return {"text": f"[NOTES FOR {task.get('id', '?')}]\\n{query.upper()}", "retrieved": [task.get("id")]}


def calc(x=0):
    return {"twice": 2 * x}
'''


@pytest.fixture
def adapter(tmp_path):
    a = tmp_path / "adapter"
    a.mkdir()
    (a / ".hermit").mkdir()
    (a / ".hermit" / "adapter.yaml").write_text(META)
    (a / "exports.yaml").write_text(EXPORTS)
    (a / "ADAPTER.md").write_text("# Static instructions\n")
    (a / "interface.py").write_text(INTERFACE)
    return a


def test_the_context_tool_must_be_a_declared_tool(tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    (a / "ADAPTER.md").write_text("x\n")
    (a / ".hermit").mkdir()
    (a / ".hermit" / "adapter.yaml").write_text("template: {id: x, version: '1'}\nstructure: {}\n")
    (a / "exports.yaml").write_text("context_tool: nope\n")
    with pytest.raises(ValueError, match="context_tool"):
        load_manifest(a)


def test_the_context_is_static_without_a_query_and_per_task_with_one(adapter):
    manifest = load_manifest(adapter)
    assert manifest["context_tool"] == "lookup"
    assert [t["name"] for t in agent_tools(manifest)] == ["calc"]        # the hosts and plugins export this list
    session = Session(adapter)
    assert session.context_tool == "lookup"
    assert [t["name"] for t in session.tools] == ["calc"] and not session.has_tool("lookup") and session.has_tool("calc")
    assert [t["name"] for t in session.declared_tools] == ["lookup", "calc"]
    assert session.context() == "# Static instructions\n" and session.last_context is None
    text = session.context(query="find the total", task={"id": "t7"})
    assert text.startswith("# Static instructions\n") and text.rstrip().endswith("[NOTES FOR t7]\nFIND THE TOTAL")
    assert session.last_context == {"text": "[NOTES FOR t7]\nFIND THE TOTAL", "retrieved": ["t7"]}
    assert session.context(query="", task={"id": "t8"}) == "# Static instructions\n"    # an empty text adds nothing
    assert session.last_context == {"text": "", "retrieved": []}
    # the agent can call its own tool, never the harness's
    ok = session.respond({"id": "c1", "function": {"name": "calc", "arguments": '{"x": 4}'}})
    assert json.loads(ok["content"]) == {"twice": 8}
    refused = session.respond({"id": "c2", "function": {"name": "lookup", "arguments": '{"query": "x"}'}})
    assert refused["content"].startswith("Error: ValueError: Unknown tool")


def test_the_deploy_check_calls_the_tool_with_an_empty_query(adapter):
    out = subprocess.run([sys.executable, "-m", "hermit.adapter.render_context", "--adapter", str(adapter)],
                         capture_output=True, text=True, cwd=ROOT)
    assert out.returncode == 0, out.stderr
    rendered = json.loads(out.stdout)
    assert rendered["context"] == "# Static instructions\n" and rendered["context_tool"] == "lookup"
    (adapter / "interface.py").write_text("def lookup(query='', task=None):\n    return {'nope': 1}\n\ndef calc(x=0):\n    return {}\n")
    out = subprocess.run([sys.executable, "-m", "hermit.adapter.render_context", "--adapter", str(adapter)],
                         capture_output=True, text=True, cwd=ROOT)
    assert out.returncode != 0 and "must return an object with a `text` string" in out.stderr


def test_the_cli_agents_refuse_an_adapter_with_a_context_tool(adapter, tmp_path):
    """The CLI agents deploy ADAPTER.md once; a per-task context would never reach them, so they refuse the adapter."""
    from hermit.agents import cli
    with pytest.raises(ValueError, match="context_tool"):
        cli.run({"id": "t1", "prompt": "x"}, adapter, params={"backend": "codex"}, base=tmp_path)
    assert not list((tmp_path / ".logs" / "workspaces").glob("*/AGENTS.md"))         # nothing was deployed


# ---------- plugin-portable: whether a template's adapters can be deployed or converted ----------

def test_a_template_that_is_not_plugin_portable_is_refused_by_deploy_and_convert(adapter, tmp_path):
    from hermit.hosts import claude_code, codex, pi
    from hermit.plugins import TARGETS, Unsupported, build
    for target in TARGETS:
        with pytest.raises(Unsupported, match="plugin-portable: false"):
            build(target, adapter, tmp_path / f"plugin-{target}", sys.executable)
        assert not (tmp_path / f"plugin-{target}").exists()
    for host in (codex, claude_code, pi):
        workspace = tmp_path / f"ws-{host.__name__.rsplit('.', 1)[-1]}"
        with pytest.raises(ValueError, match="plugin-portable: false"):
            host.prepare(adapter, workspace, sys.executable)
        assert not workspace.exists()                                 # refused before anything is written
    run = subprocess.run([sys.executable, "-m", "hermit", "deploy", "--adapter", str(adapter), "--workspace",
                          str(tmp_path / "ws-cli")], capture_output=True, text=True, cwd=ROOT)
    assert run.returncode == 2 and "plugin-portable: false" in run.stderr and "Traceback" not in run.stderr


def test_a_context_tool_needs_a_template_that_is_not_plugin_portable(adapter):
    meta = adapter / ".hermit" / "adapter.yaml"
    meta.write_text(META.replace("plugin-portable: false\n", ""))
    with pytest.raises(ValueError, match="plugin-portable: false"):
        load_manifest(adapter)
    meta.write_text(META.replace("plugin-portable: false", "plugin-portable: no-thanks"))
    with pytest.raises(ValueError, match="plugin-portable must be true or false"):
        load_manifest(adapter)


def test_a_portable_template_deploys_as_before(tmp_path):
    from hermit.hosts import codex
    a = tmp_path / "plain"
    (a / ".hermit").mkdir(parents=True)
    (a / ".hermit" / "adapter.yaml").write_text("template: {id: plain, version: '1', contract: '2'}\n")
    (a / "ADAPTER.md").write_text("Be brief.\n")
    codex.prepare(a, tmp_path / "ws", sys.executable)
    assert "Be brief." in (tmp_path / "ws" / "AGENTS.md").read_text()
