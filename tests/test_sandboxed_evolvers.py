"""CommandEvolver and AgentEvolver in the bubblewrap sandbox: tools reachable, task data hidden."""
import json
import os
from pathlib import Path
import shutil
import sys

import pytest

from hermit.config import Config
from hermit.trainer.trainer import Trainer
from test_training import make_task

pytestmark = pytest.mark.skipif(not shutil.which("bwrap"), reason="needs bubblewrap")

SCRIPT = r'''
import json, os, sys, urllib.request
from pathlib import Path
ctx = json.load(open(os.environ["HA_CONTEXT"]))
url = os.environ["HA_TOOL_URL"]
def call(name, **args):
    req = urllib.request.Request(f"{url}/call/{name}", data=json.dumps(args).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req).read())
adapter = Path(ctx["workspace"]["adapter"])
(adapter / "ADAPTER.md").write_text("Always add the numbers.\n")
hidden = all(not Path(p).exists() for p in sys.argv[1:])   # the task's files and the home directory
check = call("check_spec")["result"]
Path(ctx["workspace"]["output"], "step_result.json").write_text(json.dumps({
    "status": "done", "summary": f"hidden={hidden} passed={check['passed']}",
    "usage": [{"model": "fake", "input_tokens": 10, "output_tokens": 5, "calls": 1, "cost_usd": 0.001}]}))
'''


def test_command_evolver_in_sandbox(tmp_path):
    path = make_task(tmp_path, sandbox={"enabled": True, "network": "restricted", "allow": []})
    (tmp_path / "cmd").mkdir()
    script = tmp_path / "cmd" / "evolve.py"      # the Evolver's own directory stays readable in the sandbox
    script.write_text(SCRIPT)
    (tmp_path / "cmd" / "evolver.yaml").write_text(
        "class: hermit.evolver.agent:CommandEvolver\n"
        f"params: {{command: [{json.dumps(sys.executable)}, {json.dumps(str(script))}, "
        f"{json.dumps(str(tmp_path / 'test.txt'))}, {json.dumps(str(Path.home() / '.bashrc'))}]}}\n")
    cfg = Config.load(path, ["evolver.name=cmd", "trainer.epochs=1"])
    summary = Trainer(cfg, log=lambda *_: None).train()
    steps = [json.loads(l) for l in (tmp_path / "runs/toy-run/steps.jsonl").read_text().splitlines()]
    assert steps[0]["evolver"]["summary"] == "hidden=True passed=True", steps[0]["evolver"]
    assert steps[1]["rollout"]["mean_score"] == 1.0
    assert summary["spending"]["usd"]["evolver"] == pytest.approx(0.002)


FAKE_CODEX = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
root = Path(args[args.index("-C") + 1])
resume = "resume" in args
(root / "adapter" / "ADAPTER.md").write_text("Always add the numbers.\n")
(root / "output" / "report.md").write_text(("resumed" if resume else "fresh") + ": learned to add\n")
assert os.environ["CODEX_HOME"].endswith("agent_home")
print(json.dumps({"type": "thread.started", "thread_id": "sess-123"}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 7}}))
'''


def test_agent_evolver_with_a_fake_codex(tmp_path, monkeypatch):
    # The fake codex lives in the Evolver's own directory: the sandbox hides the task directory and /tmp, so a fake
    # placed there would not be found and the real codex on PATH would run instead.
    path = make_task(tmp_path, sandbox={"enabled": True, "network": "open"})
    evolver = tmp_path / "fakecodex"
    evolver.mkdir()
    (evolver / "codex").write_text(FAKE_CODEX)
    (evolver / "codex").chmod(0o755)
    (evolver / "START.md").write_text("start {message}")
    (evolver / "RESUME.md").write_text("resume {message}")
    (evolver / "evolver.yaml").write_text("class: hermit.evolver.agent:AgentEvolver\n"
                                          "params: {backend: codex, prompt: START.md, resume_prompt: RESUME.md, home: [],\n"
                                          "         price: {input: 1.0, cached_input: 0.5, output: 10.0}}\n")
    monkeypatch.setenv("PATH", f"{evolver}{os.pathsep}{os.environ['PATH']}")
    assert shutil.which("codex") == str(evolver / "codex")
    cfg = Config.load(path, ["evolver.name=fakecodex", "evolver.resume=true"])
    Trainer(cfg, log=lambda *_: None).train()
    steps = [json.loads(l) for l in (tmp_path / "runs/toy-run/steps.jsonl").read_text().splitlines()]
    assert steps[0]["evolver"]["summary"].startswith("fresh") and steps[0]["session"] == "sess-123"
    assert steps[1]["evolver"]["summary"].startswith("resumed")
    assert steps[0]["evolver"]["usage"][0]["output_tokens"] == 7
    # (100 - 40) × 1.0 + 40 × 0.5 + 7 × 10.0 = 150 per million tokens
    assert steps[0]["evolver"]["usage"][0]["cost_usd"] == pytest.approx(150e-6)


def test_an_agent_gets_an_empty_stdin(tmp_path):
    # codex exec appends its stdin to the prompt; a launcher may leave a command file on the Trainer's stdin.
    from types import SimpleNamespace
    from hermit.evolver.agent import _run
    log = tmp_path / "agent.log"
    probe = "import os; print(os.path.samestat(os.fstat(0), os.stat(os.devnull)))"
    assert _run([sys.executable, "-c", probe], dict(os.environ), SimpleNamespace(cancel=None), log, tmp_path) == 0
    assert log.read_text().strip() == "True"
