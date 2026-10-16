"""AgentEvolver `tools` and `hide`: the tool gate serves only the allowed tools, the hiding sandbox covers paths,
and a Codex run sees the gate, not the endpoint."""
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import threading
import urllib.request

import pytest

from harnessadapter.config import Config
from harnessadapter.evolver.gate import ToolGate
from harnessadapter.sandbox import FRAMEWORK, HidingSandbox, hidden_paths
from harnessadapter.trainer.trainer import Trainer
from test_training import make_task


def fake_endpoint():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            body = json.dumps([{"name": "check_spec"}, {"name": "run_agent"}, {"name": "llm"}]).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            body = json.dumps({"result": {"called": self.path}}).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


def test_tool_gate_passes_only_allowed_tools():
    upstream, url = fake_endpoint()
    gate = ToolGate(url, ["check_spec"])
    try:
        tools = json.loads(urllib.request.urlopen(gate.url + "/tools").read())
        assert [t["name"] for t in tools] == ["check_spec"]
        ok = urllib.request.urlopen(urllib.request.Request(gate.url + "/call/check_spec", data=b"{}",
                                                           headers={"Content-Type": "application/json"})).read()
        assert json.loads(ok)["result"]["called"] == "/call/check_spec"
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(urllib.request.Request(gate.url + "/call/run_agent", data=b"{}"))
        assert err.value.code == 404
    finally:
        gate.close(); upstream.shutdown()


def test_hidden_paths_and_hiding_sandbox(tmp_path):
    paths = hidden_paths(["module:yaml", str(tmp_path)])
    assert FRAMEWORK / "examples" in paths and FRAMEWORK / ".git" in paths and tmp_path in paths
    assert any(p.name == "yaml" for p in paths)
    with pytest.raises(RuntimeError, match="no installed package"):
        hidden_paths(["module:no_such_package_xyz"])

    class Inner:
        def wrap(self, command, **kw):
            return ["bwrap", "--ro-bind", "/", "/", "--", *command], {"X": "1"}
    secret = tmp_path / "secret.txt"
    secret.write_text("s")
    keep = tmp_path / "task-info"
    keep.mkdir()
    args, env = HidingSandbox(Inner(), [tmp_path, secret], keep=[keep]).wrap(["true"])
    cut = args.index("--")
    assert args[4:cut] == ["--tmpfs", str(tmp_path), "--ro-bind", "/dev/null", str(secret), "--ro-bind", str(keep), str(keep)]
    assert env == {"X": "1"} and args[-1] == "true"
    assert HidingSandbox(Inner(), []).wrap(["true"])[0][-1] == "true"


FAKE_CODEX = r'''#!/usr/bin/env python3
"""A codex stand-in: lists the tools its MCP server was pointed at and reports them through report.md."""
import json, re, sys, urllib.request
args = sys.argv[1:]
url = None
for a in args:
    if a.startswith("mcp_servers.evolver_tools.args="):
        tool_args = json.loads(a.split("=", 1)[1])
        url = tool_args[tool_args.index("--url") + 1]
prompt = args[-1]
output = re.search(r"output=(\S+)", prompt).group(1)
tools = [t["name"] for t in json.loads(urllib.request.urlopen(url + "/tools").read())]
gated = "127.0.0.1" in url and "Tools: check_spec (template)" in prompt
open(output + "/report.md", "w").write(f"tools={','.join(tools)} gated={gated}\n")
print(json.dumps({"type": "thread.started", "thread_id": "sess-1"}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 2}}))
'''


def test_agent_evolver_gates_the_tools(tmp_path, monkeypatch):
    path = make_task(tmp_path, sandbox={"enabled": False, "network": "open"})
    evolver = tmp_path / "gated"
    evolver.mkdir()
    (evolver / "codex").write_text(FAKE_CODEX)
    (evolver / "codex").chmod(0o755)
    (evolver / "START.md").write_text("output={output}\n{message}")
    (evolver / "evolver.yaml").write_text("class: harnessadapter.evolver.agent:AgentEvolver\n"
                                          "params: {backend: codex, prompt: START.md, home: [], tools: [check_spec]}\n")
    monkeypatch.setenv("PATH", f"{evolver}{os.pathsep}{os.environ['PATH']}")
    cfg = Config.load(path, ["evolver.name=gated", "trainer.epochs=1", "data.step_size=6"])
    Trainer(cfg, log=lambda *_: None).train()
    steps = [json.loads(l) for l in (tmp_path / "runs/toy-run/steps.jsonl").read_text().splitlines()]
    assert steps[0]["evolver"]["summary"] == "tools=check_spec gated=True"
