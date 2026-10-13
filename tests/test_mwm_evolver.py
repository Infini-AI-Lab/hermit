"""The mwm Evolver end to end on the Trainer, offline: a stand-in `codex` plays the learner.

The stand-in runs where the real one would (the framework sandbox plus the native mount names), records what it can
see, and edits the world model the way a session would. No model is called.
"""
import json
import os
from pathlib import Path
import shutil
import textwrap

import pytest
import yaml

from harnessadapter.config import Config
from harnessadapter.trainer.trainer import Trainer


@pytest.fixture
def tmp_path():
    """A directory outside /tmp: the template check runs under bubblewrap with an empty tmpfs at /tmp."""
    import tempfile
    base = Path.home() / ".cache" / "harnessadapter-tests"
    base.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(dir=base))
    yield path
    if not os.environ.get("KEEP_TEST_DIRS"):
        shutil.rmtree(path, ignore_errors=True)

FAKE_CODEX = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
cwd = Path(args[args.index("-C") + 1])
prompt = args[-1]
seen = {name: os.path.exists(name) for name in
        ("/evidence/README.md", "/environment/README.md", "/skills", "/patterns", "/world-model/CONTRACT.md")}
seen["evidence_files"] = sorted(os.listdir("/evidence"))
seen["cwd"] = os.getcwd()
seen["git_log"] = os.popen("git -C . log --oneline 2>&1 | wc -l").read().strip()
import urllib.request
gate = os.environ.get("MW_TOOLSRV", "")
def _call(name, body):
    req = urllib.request.Request(f"{gate}/call/{name}", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception as exc:
        return {"refused": type(exc).__name__}
seen["gate_tools"] = [t["name"] for t in json.loads(urllib.request.urlopen(gate + "/tools", timeout=10).read())]
seen["echo"] = _call("echo", {"text": "hi"})
seen["run_agent"] = _call("run_agent", {"task_ids": ["q1"]})
seen["cwd"] = os.getcwd()
seen["relative_evidence"] = os.path.exists("../evidence/README.md")
seen["world_model_ro"] = not os.access("/world-model/memory", os.W_OK)
ws = Path(os.environ["FAKE_WORKSPACE"])
areas = [ws / n for n in os.listdir(ws) if n not in ("adapter", "agent_home")] + [ws.parent / "learning_material"]
import harnessadapter
repo = Path(harnessadapter.__file__).resolve().parents[1]
areas += [repo / "examples", repo / "harnessadapter" / "evolvers" / "mwm" / "materials"]
seen["home_entries"] = sorted(os.listdir(os.environ["CODEX_HOME"]))
(Path(os.environ["CODEX_HOME"]) / "leftover.txt").write_text("from a previous session")
areas.append(cwd.parent.parent / "learning_material")
seen["hidden_listing"] = {str(p): sorted(os.listdir(p)) for p in areas if p.is_dir()}
n = len(list((cwd / "memory").glob("note*.md")))
kind = "bootstrap" if prompt.split("\n\n", 1)[1].startswith("BOOTSTRAP SESSION") else "review"
(cwd / "memory" / f"note{n}.md").write_text(
    f"last_confirmed_t: {n}\nscope: always\nderived_from: none (reasoning only)\nbrief: {kind} note {n}\n\nA fact.\n")
(cwd / "meta" / "seen.json").write_text(json.dumps(seen))
if os.environ.get("FAKE_EDIT_CORE") and kind == "review":
    (cwd / "interface.py").write_text((cwd / "interface.py").read_text() + "\n# edited\n")
if os.environ.get("FAKE_BAD_TOOL") and kind == "review":
    tool = cwd / "tools" / "broken"
    tool.mkdir(parents=True, exist_ok=True)
    (tool / "TOOL.md").write_text("broken: always fails its test\n")
    (tool / "run.py").write_text("SCHEMA = {'name': 'broken', 'description': 'x', 'parameters': {}}\n"
                                 "def run(**kwargs):\n    return 1\n")
    (tool / "test.py").write_text("raise SystemExit('fails on purpose')\n")
    import subprocess
    run = subprocess.run(["captest", "--wrepo", ".", "--name", "broken"], capture_output=True, text=True)
    (cwd / "meta" / "captest.txt").write_text(run.stdout + run.stderr)
print(json.dumps({"type": "thread.started", "thread_id": f"t-{kind}-{n}"}))
print(json.dumps({"type": "item.completed", "item": {"type": "command_execution"}}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 40,
                                                       "output_tokens": 10}}))
'''

PIPELINE = textwrap.dedent('''
    import os
    from pathlib import Path
    from harnessadapter.task import RunOutput

    def run(task, adapter):
        evidence = Path(os.environ["MWM_TEST_EVIDENCE"])
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / "README.md").write_text("# records\\n")
        block = evidence / f"block{task['block']:03d}"
        block.mkdir(exist_ok=True)
        (block / f"q{task['n']:03d}.json").write_text("[]")
        entry = f"## Question {task['n']}\\nverdict: CORRECT\\nraw trajectory: /evidence/block{task['block']:03d}/q{task['n']:03d}.json"
        (evidence.parent / f"host_git_{task['n']}").write_text(str((Path(adapter) / ".git").exists()))
        return RunOutput(output="ok", extra={"mwm": {"unit": "questions", "number": task["n"], "entry": entry}})
''')


def make(tmp: Path, monkeypatch, steps: int = 2) -> Path:
    bin_dir = tmp / "fakebin"
    bin_dir.mkdir()
    (bin_dir / "codex").write_text(FAKE_CODEX)
    (bin_dir / "codex").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    evidence = tmp / "taskdir" / "evidence"
    evidence.mkdir(parents=True)
    (evidence / "README.md").write_text("# records\n")
    monkeypatch.setenv("MWM_TEST_EVIDENCE", str(evidence))
    monkeypatch.setenv("FAKE_WORKSPACE", str((tmp / "runs" / "mwm-test" / "workspace").resolve()))
    rows = [{"id": f"q{i}", "n": i, "block": i} for i in range(1, steps + 1)]
    (tmp / "tasks.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (tmp / "train.txt").write_text("".join(f"q{i}\n" for i in range(1, steps + 1)))
    (tmp / "pipeline.py").write_text(PIPELINE)
    (tmp / "judge.py").write_text("def score(task, output):\n    return 1.0\n")
    (tmp / "echo.py").write_text("def echo(text):\n    return {'text': text}\n")
    deployment = Path(__file__).resolve().parents[1] / "examples/tasks/clbench/database/task-info/deployment"
    (tmp / "task-info").mkdir()
    shutil.copytree(deployment, tmp / "task-info" / "deployment")
    config = {"data": {"tasks": "tasks.jsonl", "train": "train.txt", "step_size": 1, "shuffle": False},
              "task_info": "task-info", "pipeline": {"run": "pipeline.py:run", "concurrency": 1},
              "judge": {"path": "judge.py"}, "template": "mwm",
              "evolver": {"name": "mwm", "params": {"evidence": str(evidence), "retry_wait_s": 0,
                                                    "session_timeout_s": 120}},
              "evolver_access": {"environment": [str(evidence)],
                                 "tools": [{"name": "echo", "description": "Echo a text.", "entrypoint": "echo.py:echo",
                                            "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}},
                                            "output_schema": {"type": "object"}}]},
              "sandbox": {"enabled": bool(shutil.which("bwrap")), "network": "none"},
              "trainer": {"run_name": "mwm-test", "output_dir": "runs", "epochs": 1, "call_timeout_min": 10}}
    path = tmp / "run.yaml"
    path.write_text(yaml.safe_dump(config))
    return path


def test_bootstrap_review_mounts_and_records(tmp_path, monkeypatch):
    cfg = Config.load(make(tmp_path, monkeypatch), [])
    Trainer(cfg, log=lambda *_: None).train()
    run = tmp_path / "runs" / "mwm-test"
    seen_git = {"host": (tmp_path / "taskdir" / "host_git_1").read_text() == "True",
                "learner": int(json.loads((run / "checkout" / "meta" / "seen.json").read_text())["git_log"]) >= 2}
    steps = [json.loads(l) for l in (run / "steps.jsonl").read_text().splitlines()]
    assert [s["step"] for s in steps] == [0, 1, 2] and steps[0]["bootstrap"] and steps[0]["tasks"] == []
    assert [s["evolver"]["status"] for s in steps] == ["done", "done", "done"]
    first = json.loads((run / "step_0000" / "output" / "sessions.json").read_text()) + \
        json.loads((run / "step_0001" / "output" / "sessions.json").read_text())
    assert [s["label"] for s in first] == ["bootstrap", "review_0001"]
    assert all(s["status"] == "adopted" for s in first)
    # the first block's hosts were served by the bootstrapped adapter (step 0's commit)
    assert steps[1]["parent"] == steps[0]["commit"] != steps[0]["parent"]
    assert seen_git["learner"] and not seen_git["host"]
    if shutil.which("bwrap"):
        for s in first:
            assert s["isolation"]["passed"], s["isolation"]
            assert all(v.startswith("blocked") for v in s["isolation"]["outside"].values())
    final = run / "checkout"
    notes = sorted(p.name for p in (final / "memory").glob("note*.md"))
    assert notes == ["note0.md", "note1.md", "note2.md"]           # bootstrap + two reviews
    seen = json.loads((final / "meta" / "seen.json").read_text())
    if shutil.which("bwrap"):
        assert all(seen[k] for k in ("/evidence/README.md", "/environment/README.md", "/skills", "/patterns",
                                     "/world-model/CONTRACT.md")), seen
        assert "block001" in seen["evidence_files"]                 # a review sees the block's records
        assert seen["cwd"] == "/w" and seen["relative_evidence"] and seen["world_model_ro"]
        assert len(seen["hidden_listing"]) >= 7 and all(v == [] for v in seen["hidden_listing"].values()), seen
    assert "leftover.txt" not in seen["home_entries"]              # a throwaway CODEX_HOME per session
    assert seen["gate_tools"] == ["echo"]                           # only the venue's channels pass the gate
    assert seen["echo"] == {"result": {"text": "hi"}}
    assert seen["run_agent"] == {"refused": "HTTPError"}
    times = json.loads((final / ".mwm" / "edit_times.json").read_text())
    assert {"memory/note0.md", "memory/note1.md", "memory/note2.md"} <= set(times)
    assert "review note 2" in (final / "ADAPTER.md").read_text()   # the catalog, newest edit first
    prompt = (run / "step_0002" / "output" / "review_0002.prompt.md").read_text()
    assert "REVIEW SESSION #2." in prompt and "This block covers questions 2-2; the current tick is 2." in prompt
    boot = (run / "step_0000" / "output" / "bootstrap.prompt.md").read_text()
    assert "BOOTSTRAP SESSION (before any episodes)" in boot


def test_bootstrap_sees_no_records(tmp_path, monkeypatch):
    if not shutil.which("bwrap"):
        pytest.skip("bwrap not installed")
    cfg = Config.load(make(tmp_path, monkeypatch, steps=1), [])
    trainer = Trainer(cfg, log=lambda *_: None)
    seen_after_boot = {}
    original = trainer.evolver._one_session

    def spy(ctx, **kw):
        record = original(ctx, **kw)
        if kw["label"] == "bootstrap":
            seen_after_boot.update(json.loads((ctx.workspace.adapter / "meta" / "seen.json").read_text()))
        return record
    trainer.evolver._one_session = spy
    trainer.train()
    assert seen_after_boot["evidence_files"] == ["README.md"]


def test_failing_tool_is_parked_and_reported(tmp_path, monkeypatch):
    if not shutil.which("bwrap"):
        pytest.skip("bwrap not installed")
    monkeypatch.setenv("FAKE_BAD_TOOL", "1")
    cfg = Config.load(make(tmp_path, monkeypatch), [])
    Trainer(cfg, log=lambda *_: None).train()
    run = tmp_path / "runs" / "mwm-test"
    final = run / "checkout"
    assert not (final / "tools" / "broken").exists()
    assert (final / "meta" / "captest.txt").read_text().startswith("FAIL: "), (final / "meta" / "captest.txt").read_text()
    assert (final / "tools" / "_rejected" / "broken" / "run.py").exists()
    prompt = (run / "step_0002" / "output" / "review_0002.prompt.md").read_text()
    assert "Executables parked by the wrapper" in prompt and "broken" in prompt


def test_failed_session_restores_the_adapter(tmp_path, monkeypatch):
    path = make(tmp_path, monkeypatch, steps=1)
    fake = tmp_path / "fakebin" / "codex"
    fake.write_text(FAKE_CODEX.replace("print(json.dumps({\"type\": \"thread.started\"",
                                       "sys.exit(3)\nprint(json.dumps({\"type\": \"thread.started\""))
    cfg = Config.load(path, ["evolver.params.bootstrap=false"])
    Trainer(cfg, log=lambda *_: None).train()
    run = tmp_path / "runs" / "mwm-test"
    sessions = json.loads((run / "step_0001" / "output" / "sessions.json").read_text())
    assert sessions[0]["status"] == "failed" and sessions[0]["retried_after"] == "failed"
    assert not list((run / "checkout" / "memory").glob("note*.md"))


def test_editing_serving_code_discards_the_session(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_EDIT_CORE", "1")
    cfg = Config.load(make(tmp_path, monkeypatch, steps=1), [])
    Trainer(cfg, log=lambda *_: None).train()
    run = tmp_path / "runs" / "mwm-test"
    sessions = json.loads((run / "step_0000" / "output" / "sessions.json").read_text()) + \
        json.loads((run / "step_0001" / "output" / "sessions.json").read_text())
    assert [s["status"] for s in sessions] == ["adopted", "rule_broken"]
    assert sessions[1]["protected_changes"] == ["interface.py"]
    final = run / "checkout"
    assert sorted(p.name for p in (final / "memory").glob("note*.md")) == ["note0.md"]   # the bootstrap's only
    assert "# edited" not in (final / "interface.py").read_text()


def test_shell_channel_without_mcp_still_passes_the_gate(tmp_path, monkeypatch):
    cfg = Config.load(make(tmp_path, monkeypatch, steps=1), ["evolver.params.learner_tools=[]"])
    Trainer(cfg, log=lambda *_: None).train()
    seen = json.loads((tmp_path / "runs" / "mwm-test" / "checkout" / "meta" / "seen.json").read_text())
    assert seen["gate_tools"] == ["echo"] and seen["echo"] == {"result": {"text": "hi"}}
