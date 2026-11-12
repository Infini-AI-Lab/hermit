"""CommandEvolver and AgentEvolver in the bubblewrap sandbox: tools reachable (and only the allowed ones), task data
hidden, rollouts and eval sandboxed too; and, with no sandbox, the learning material's README as each CLI Evolver's
prompt names it."""
import json
import os
from pathlib import Path
import shutil
import sys

import pytest

from hermit.config import Config
from hermit.trainer.trainer import Trainer
from toy_task import make_sandboxed_task

ROOT = Path(__file__).resolve().parents[2]
NEEDS_BWRAP = pytest.mark.skipif(not shutil.which("bwrap"), reason="needs bubblewrap")

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
assert str(adapter) == "/w/adapter", adapter                # the layout's names, in the context it is given
check = call("check_spec")["result"]
Path(ctx["workspace"]["output"], "step_result.json").write_text(json.dumps({
    "status": "done", "summary": f"hidden={hidden} passed={check['passed']}",
    "usage": [{"model": "fake", "input_tokens": 10, "output_tokens": 5, "calls": 1, "cost_usd": 0.001}]}))
'''


@NEEDS_BWRAP
def test_command_evolver_in_sandbox(tmp_path):
    """The learner (its workspace under /w, the paths it is given spelled so) and every rollout and eval call (the
    task sandbox, shown only agent/) each run in a sandbox of their own, every call's isolation checked from
    inside."""
    from hermit.evaluate import evaluate
    path = make_sandboxed_task(tmp_path)
    (tmp_path / "cmd").mkdir()
    script = tmp_path / "cmd" / "evolve.py"      # the Evolver's own directory stays readable in the sandbox
    script.write_text(SCRIPT)
    (tmp_path / "cmd" / "evolver.yaml").write_text(
        "class: hermit.agent.evolver.agent:CommandEvolver\n"
        f"params: {{command: [{json.dumps(sys.executable)}, {json.dumps(str(script))}, "
        f"{json.dumps(str(tmp_path / 'test.txt'))}, {json.dumps(str(Path.home() / '.bashrc'))}]}}\n")
    cfg = Config.load(path, ["evolver.name=cmd", "trainer.epochs=1"])
    summary = Trainer(cfg, log=lambda *_: None).train()
    steps = [json.loads(l) for l in (tmp_path / "runs/toy-run/steps.jsonl").read_text().splitlines()]
    assert steps[0]["evolver"]["summary"] == "hidden=True passed=True", steps[0]["evolver"]
    assert steps[0]["rollout"]["mean_score"] == 0.0 and steps[1]["rollout"]["mean_score"] == 1.0
    assert summary["spending"]["usd"]["evolver"] == pytest.approx(0.002)
    records = [json.loads(p.read_text()) for p in (tmp_path / "runs/toy-run/sandbox").glob("rollout-*.json")]
    assert len(records) >= 6 and all(r["probe"]["passed"] for r in records)   # one per call, each checked
    result = evaluate(cfg, "final", log=lambda *_: None)
    assert result["mean_score"] == 1.0 and result["errors"] == 0
    assert list((tmp_path / "runs/toy-run/eval/final/sandbox").glob("eval-*.json"))


FAKE_CODEX = r'''#!/usr/bin/env python3
import json, os, sys, tomllib, urllib.request
from pathlib import Path
args = sys.argv[1:]
config = (Path(os.environ["CODEX_HOME"]) / "config.toml").read_text()
assert 'web_search = "disabled"' in config          # web search is set explicitly, off unless params.web_search
root = Path(args[args.index("-C") + 1])
resume = "resume" in args
# The tools its MCP server was pointed at (the server's JSON entrance beside its MCP one), and whether the prompt
# names only the allowed tool and no endpoint URL, with the tools approved without asking.
server = tomllib.loads(config)["mcp_servers"]["evolver_tools"]
tools = [t["name"] for t in json.loads(urllib.request.urlopen(server["url"][:-len("/mcp")] + "/tools").read())]
prompt = args[-1]
gated = (server["default_tools_approval_mode"] == "approve" and "Tools: check_spec (template)" in prompt
         and "http://" not in prompt)
(root / "adapter" / "ADAPTER.md").write_text("Always add the numbers.\n")
(root / "output" / "report.md").write_text(("resumed" if resume else "fresh")
                                           + f": learned to add tools={','.join(tools)} gated={gated}\n")
assert os.environ["CODEX_HOME"].endswith("agent_home")
print(json.dumps({"type": "thread.started", "thread_id": "sess-123"}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 7}}))
'''


@NEEDS_BWRAP
def test_agent_evolver_with_a_fake_codex(tmp_path, monkeypatch):
    """`tools: [check_spec]`: the session's tool server serves only the allowed tools, and the prompt names them and
    no Trainer endpoint."""
    # The fake codex lives in the Evolver's own directory: the sandbox hides the task directory and /tmp, so a fake
    # placed there would not be found and the real codex on PATH would run instead.
    path = make_sandboxed_task(tmp_path)
    evolver = tmp_path / "fakecodex"
    evolver.mkdir()
    (evolver / "codex").write_text(FAKE_CODEX)
    (evolver / "codex").chmod(0o755)
    (evolver / "START.md").write_text("start {message}")
    (evolver / "RESUME.md").write_text("resume {message}")
    (evolver / "evolver.yaml").write_text("class: hermit.agent.evolver.agent:AgentEvolver\n"
                                          "params: {backend: codex, web_search: false, prompt: START.md,\n"
                                          "         resume_prompt: RESUME.md, home: [], tools: [check_spec],\n"
                                          "         price: {input: 1.0, cached_input: 0.5, output: 10.0}}\n")
    monkeypatch.setenv("PATH", f"{evolver}{os.pathsep}{os.environ['PATH']}")
    assert shutil.which("codex") == str(evolver / "codex")
    cfg = Config.load(path, ["evolver.name=fakecodex", "evolver.resume=true", "trainer.epochs=1"])
    Trainer(cfg, log=lambda *_: None).train()
    steps = [json.loads(l) for l in (tmp_path / "runs/toy-run/steps.jsonl").read_text().splitlines()]
    assert steps[0]["evolver"]["summary"].startswith("fresh") and steps[0]["session"] == "sess-123"
    assert steps[1]["evolver"]["summary"].startswith("resumed")
    assert all(s["evolver"]["summary"].endswith("tools=check_spec gated=True") for s in steps[:2]), steps[0]["evolver"]
    assert steps[0]["evolver"]["usage"][0]["output_tokens"] == 7
    # (100 - 40) × 1.0 + 40 × 0.5 + 7 × 10.0 = 150 per million tokens
    assert steps[0]["evolver"]["usage"][0]["cost_usd"] == pytest.approx(150e-6)


def test_each_cli_evolvers_start_md_names_the_materials_readme_where_the_trainer_writes_it(tmp_path):
    """The README that says what the learning material holds is the one hermit.trainer.material writes at the
    material's root (current/, a link to the step's folder, holds none): each CLI Evolver's prompt (START.md), filled
    in as the Evolver fills it, names that file, never a README in current/."""
    import yaml
    from hermit.agent.evolver.agent import _fill
    from hermit.agent.evolver.base import Access, Progress, StepContext, Workspace
    from hermit.trainer.material import Material, readme
    material = Material(tmp_path / "learning_material")
    material.write(1, 1, "abc1234", False, [], None)
    assert (material.root / "README.md").read_text() == readme()
    assert not (material.root / "current" / "README.md").exists()
    ws = Workspace(**{name: tmp_path / name for name in ("adapter", "task_info", "history", "state", "scratch",
                                                         "output")}, learning_material=material.root)
    ctx = StepContext(workspace=ws, access=Access(), tools={}, params={}, progress=Progress(1, 1, False))
    methods = ROOT / "hermit" / "agent" / "evolver" / "methods"
    manifests = {path.parent.name: yaml.safe_load(path.read_text()) for path in methods.glob("*/evolver.yaml")}
    prompts = {name: manifest["params"]["prompt"] for name, manifest in manifests.items()
               if manifest.get("class") == "hermit.agent.evolver.agent:AgentEvolver"}
    assert {"codex", "claude", "pi", "opencode", "dsh"} <= set(prompts)
    for name, prompt in sorted(prompts.items()):
        text = " ".join(_fill((methods / name / prompt).read_text(), ctx).split())
        assert f"({material.root}/current/; {material.root}/README.md says what it holds)" in text, name
        assert "its README.md" not in text and "current/README.md" not in text, name
