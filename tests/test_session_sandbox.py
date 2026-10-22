"""A Session in sandboxed mode: the adapter's tools run in a sandboxed `hermit serve`, never in the program."""
import asyncio
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml

from hermit import Session
from hermit.adapter.remote import ToolError

EXAMPLE = Path(__file__).resolve().parents[1] / "docs" / "_examples" / "file_adapter"

SNOOP = '''

def snoop(path, _resources):
    import os, sys
    out = {}
    try:
        out["read"] = open(path).read()
    except Exception as e:
        out["read"] = "ERR " + type(e).__name__
    try:
        open(os.path.join(os.path.dirname(__file__), "planted.txt"), "w").write("x")
        out["write_adapter"] = "ok"
    except Exception as e:
        out["write_adapter"] = "ERR " + type(e).__name__
    out["deployment"] = (_resources.get("deployment") or {}).get("wm_call", "")
    ev = _resources.get("evidence")
    out["evidence"] = str(ev.root) if ev is not None else ""
    if ev is not None:
        (ev.root / "note.txt").write_text("seen")
    out["pid_parent"] = str(os.getppid() != 0)
    return {"text": json.dumps(out)}
'''


def _bwrap_works() -> bool:
    if not shutil.which("bwrap"):
        return False
    return subprocess.run(["bwrap", "--unshare-user", "--unshare-net", "--ro-bind", "/", "/", "true"],
                          capture_output=True).returncode == 0


def adapter_copy(tmp_path: Path) -> Path:
    root = tmp_path / "adapter"
    shutil.copytree(EXAMPLE, root)
    manifest = yaml.safe_load((root / "adapter.yaml").read_text())
    manifest["exports"]["tools"].append({
        "name": "snoop", "description": "Report what the tool can reach.", "entrypoint": "tools.py:snoop",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        "output_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}})
    (root / "adapter.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    text = (root / "tools.py").read_text()
    (root / "tools.py").write_text(("import json\n" if "import json" not in text else "") + text + SNOOP)
    return root


def test_other_resources_are_refused(tmp_path):
    with pytest.raises(ValueError, match="cannot cross into the sandbox"):
        Session(EXAMPLE, resources={"db": object()}, sandbox=True)


@pytest.mark.skipif(not _bwrap_works(), reason="needs a working bubblewrap (unprivileged user namespaces)")
def test_tools_run_in_the_sandboxed_server(tmp_path):
    root = adapter_copy(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("secret")
    before = {m for m in sys.modules if m.startswith("_adapter_")}
    with Session(root, sandbox=True, deployment={"wm_call": "call it"}, evidence=tmp_path / "ev",
                 audit_log=tmp_path / "calls.jsonl") as s:
        assert s.sandboxed and s.id == "file-example" and s.context().strip()
        assert [a["name"] for a in s.agents()] and s.has_tool("snoop")
        assert s.call("count_words", {"text": "a b c"}) == {"count": 3}

        async def inside_loop():
            return await s.acall("count_words", {"text": "x"}), s.call("count_words", {"text": "x y"})
        assert asyncio.run(inside_loop()) == ({"count": 1}, {"count": 2})
        seen = json.loads(Session.text(s.call("snoop", {"path": str(secret)})))
        with pytest.raises(ValueError, match="Unknown tool"):
            s.call("missing", {})
        with pytest.raises(ToolError):
            s.call("count_words", {"text": 1})                      # invalid arguments, answered by the server
        assert s.respond({"id": "c", "function": {"name": "count_words", "arguments": '{"text": "q"}'}})[
            "content"] == '{"count": 1}'
        with pytest.raises(AttributeError):
            s.resources
    assert seen["read"] == "ERR FileNotFoundError"                  # outside the grant: absent inside
    assert seen["write_adapter"].startswith("ERR") and not (root / "planted.txt").exists()
    assert seen["deployment"] == "call it"
    assert (tmp_path / "ev" / "note.txt").read_text() == "seen"     # the evidence store is writable
    assert {m for m in sys.modules if m.startswith("_adapter_")} == before   # never imported here
    records = [json.loads(l) for l in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert [r["status"] for r in records][:3] == ["ok", "ok", "ok"] and "error" in [r["status"] for r in records]


@pytest.mark.skipif(not _bwrap_works(), reason="needs a working bubblewrap (unprivileged user namespaces)")
def test_a_server_that_cannot_start_is_reported(tmp_path):
    root = adapter_copy(tmp_path)
    (root / "tools.py").write_text("raise SystemExit('broken tools')\n")
    with pytest.raises(RuntimeError, match="did not start"):
        Session(root, sandbox=True)


GARBLE = """

def garble():
    # Half a message into the server's own channel (the MCP server moved it off stdout, which is /dev/null).
    import os
    for fd in os.listdir('/proc/self/fd'):
        try:
            if int(fd) > 2 and os.readlink(f'/proc/self/fd/{fd}').startswith('pipe:'):
                os.write(int(fd), b'{"x": ')
        except OSError:
            pass
    return {}
"""


@pytest.mark.skipif(not _bwrap_works(), reason="needs a working bubblewrap (unprivileged user namespaces)")
def test_a_tool_that_breaks_its_channel_ends_in_an_error_not_a_hang(tmp_path):
    root = adapter_copy(tmp_path)
    manifest = yaml.safe_load((root / "adapter.yaml").read_text())
    manifest["exports"]["tools"].append({"name": "garble", "description": "x", "entrypoint": "tools.py:garble",
                                         "input_schema": {"type": "object"}, "output_schema": {"type": "object"}})
    (root / "adapter.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    (root / "tools.py").write_text((root / "tools.py").read_text() +
                                   GARBLE)
    with Session(root, sandbox=True) as s:
        s._remote.call_timeout = 5
        with pytest.raises(ToolError, match="no answer"):
            s.call("garble", {})
