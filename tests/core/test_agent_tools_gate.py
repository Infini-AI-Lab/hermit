"""AgentEvolver `tools` (and the removed `hide`): the session's tool server serves only the allowed tools and a Codex run sees no
Trainer endpoint (with the sandbox off; the sandboxed fake Codex in test_sandboxed_evolvers.py checks it under
bubblewrap); `hide` is refused, the evolver sandbox showing only its layout."""
import json
import os
from pathlib import Path

import pytest

from hermit.config import Config
from hermit.trainer.trainer import Trainer
from toy_task import make_task


FAKE_CODEX = r'''#!/usr/bin/env python3
"""A codex stand-in: lists the tools its MCP server was pointed at and reports them through report.md."""
import json, re, sys, urllib.request
args = sys.argv[1:]
import os, tomllib
server = tomllib.load(open(os.environ["CODEX_HOME"] + "/config.toml", "rb"))["mcp_servers"]["evolver_tools"]
url = server["url"][:-len("/mcp")]                  # the tool server's JSON entrance beside its MCP one
prompt = args[-1]
output = re.search(r"output=(\S+)", prompt).group(1)
tools = [t["name"] for t in json.loads(urllib.request.urlopen(url + "/tools").read())]
gated = (server["default_tools_approval_mode"] == "approve" and "Tools: check_spec (template)" in prompt
         and "http://" not in prompt)
open(output + "/report.md", "w").write(f"tools={','.join(tools)} gated={gated}\n")
print(json.dumps({"type": "thread.started", "thread_id": "sess-1"}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 2}}))
'''


def test_agent_evolver_gates_the_tools(tmp_path, monkeypatch):
    """Without the sandboxed run's bubblewrap too (test_sandboxed_evolvers.py checks it there)."""
    path = make_task(tmp_path, sandbox={"enabled": False})
    evolver = tmp_path / "gated"
    evolver.mkdir()
    (evolver / "codex").write_text(FAKE_CODEX)
    (evolver / "codex").chmod(0o755)
    (evolver / "START.md").write_text("output={output}\n{message}")
    (evolver / "evolver.yaml").write_text("class: hermit.agent.evolver.agent:AgentEvolver\n"
                                          "params: {backend: codex, web_search: false, prompt: START.md, home: [], tools: [check_spec]}\n")
    monkeypatch.setenv("PATH", f"{evolver}{os.pathsep}{os.environ['PATH']}")
    cfg = Config.load(path, ["evolver.name=gated", "trainer.epochs=1", "data.step_size=6"])
    Trainer(cfg, log=lambda *_: None).train()
    steps = [json.loads(l) for l in (tmp_path / "runs/toy-run/steps.jsonl").read_text().splitlines()]
    assert steps[0]["evolver"]["summary"] == "tools=check_spec gated=True"


def test_hide_is_refused_now_that_the_sandbox_shows_only_its_layout():
    from types import SimpleNamespace
    from hermit.agent.evolver.agent import AgentEvolver
    ctx = SimpleNamespace(params={"backend": "codex", "web_search": False, "prompt": "x", "hide": []})
    result = AgentEvolver().step(ctx)
    assert result.status == "failed" and "hide was removed" in result.summary
