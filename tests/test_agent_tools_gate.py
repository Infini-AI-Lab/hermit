"""AgentEvolver `tools` and `hide`: the session's tool server serves only the allowed tools, the hiding sandbox
covers paths, and a Codex run sees no Trainer endpoint."""
import json
import os

import pytest

from hermit.config import Config
from hermit.sandbox import FRAMEWORK, HidingSandbox, hidden_paths
from hermit.trainer.trainer import Trainer
from test_training import make_task


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
    path = make_task(tmp_path, sandbox={"enabled": False, "network": "open"})
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


@pytest.mark.skipif(not __import__("shutil").which("bwrap"), reason="needs bubblewrap")
def test_hidden_paths_are_covered_under_every_name_and_the_runs_own_places_stay(tmp_path):
    """A hidden directory is covered under the name it was given and its real one (a workspace reached through a
    link shows it under both); the places the session is given to work in, when they lie below a hidden directory
    (a run kept under the framework's examples/), stay visible, and what is hidden below them stays covered."""
    import subprocess
    from hermit.sandbox import Sandbox
    examples = tmp_path / "examples"                      # stands for the framework's examples/: task data, a run
    (examples / "data").mkdir(parents=True)
    (examples / "data" / "answers.txt").write_text("42\n")
    run = examples / "task" / "runs" / "r"
    for area in ("state", "adapter", "material"):
        (run / area).mkdir(parents=True)
    (run / "adapter" / "ADAPTER.md").write_text("mine\n")
    (run / "material" / "secret.txt").write_text("hidden from this call\n")
    real = tmp_path / "real"
    (real / "records").mkdir(parents=True)
    (real / "records" / "log.txt").write_text("hidden\n")
    (tmp_path / "alias").symlink_to(real)
    alias = tmp_path / "alias"
    box = Sandbox(network="open", writable=[run / "state", run / "adapter", alias],
                  readable=[run / "material"])
    hiding = HidingSandbox(box, [examples, run / "material", alias / "records"])

    def shows(path) -> str:
        args, env = hiding.wrap(["sh", "-c", f"cat {path} 2>/dev/null || echo ABSENT"], env=dict(os.environ))
        return subprocess.run(args, env=env, capture_output=True, text=True, timeout=60).stdout.strip()
    assert shows(examples / "data" / "answers.txt") == "ABSENT"              # the hidden tree's own files
    assert shows(run / "adapter" / "ADAPTER.md") == "mine"                   # the run's places below it stay
    assert shows(run / "material" / "secret.txt") == "ABSENT"               # hidden below them: covered again
    assert shows(alias / "records" / "log.txt") == "ABSENT"                  # under the link's name
    assert shows(real / "records" / "log.txt") == "ABSENT"                   # and under the real one
