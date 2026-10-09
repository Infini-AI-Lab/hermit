"""The glm_mwm Evolver under the real Trainer, offline: a fake pipeline and judge, a scripted fake GLM, one task.

Three epochs with rounds=2: steps 1-2 are learning sessions, step 3 a measurement round. Proves the checkpoint tags,
that step 2's evidence holds step 1's run (B) and its own (C1), and that no judge output reaches the model; that a
session which changes nothing makes the next run a second B run (labelled B-2); and that a pipeline stop (the real
pipeline's EnvironmentFailure is a SystemExit) leaves the run resumable.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
import textwrap

import pytest
import yaml

from harnessadapter.config import Config
from harnessadapter.evolver.base import Usage
from harnessadapter.trainer.trainer import Trainer
from harnessadapter.tree import git

EVOLVER_DIR = Path(__file__).resolve().parents[2] / "examples" / "mwm" / "visual_showcase" / "glm_mwm"
JUDGE_MARK = "JUDGE-ONLY-4b9d"
REASONING_MARK = "PRIVATE-REASONING-0c7e"
PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII="

PIPELINE = '''
import base64, hashlib, json, time
from pathlib import Path
import yaml
from harnessadapter.task import RunOutput

PNG = base64.b64decode("%s")
SHOTS = ("desktop-t0", "desktop-t2", "desktop-t5", "desktop-interaction", "phone")

def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=1))

class EnvironmentFailure(SystemExit):
    """As the real pipeline's: a failure every later run would repeat."""

def run(task, adapter):
    manifest = yaml.safe_load((Path(adapter) / "adapter.yaml").read_text()) if adapter else {}
    arm = "adapted" if "origin" in manifest else "harness"     # the root (no origin) runs as arm B
    stop = Path(__file__).parent / "stop-once"
    if arm == "adapted" and stop.exists():
        stop.unlink()
        raise EnvironmentFailure("synthetic: chat/completions HTTP 401")
    log = Path(__file__).parent / ".logs" / f"{task['case']}-{arm}-{time.time_ns()}"
    (log / "submission").mkdir(parents=True)
    (log / "submission" / "index.html").write_text(f"<!doctype html><title>{arm}</title>")
    source = hashlib.sha256((log / "submission" / "index.html").read_bytes()).hexdigest()
    shots = []
    for name in SHOTS:
        data = PNG + f"{arm}-{name}-{log.name}".encode()
        (log / "capture").mkdir(exist_ok=True)
        (log / "capture" / f"{name}.png").write_bytes(data)
        shots.append({"file": f"{name}.png", "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})
    save(log / "capture" / "summary.json", {"capture_ok": True, "screenshots": shots,
                                             "input": {"index_sha256": source}})
    save(log / "result.json", {"arm": arm, "case": task["case"], "status": "completed", "submission_sha256": source})
    save(log / "calls" / "01.json", {"call": 1, "assistant": {"role": "assistant", "content": "made it"}})
    save(log / "tools" / "01.json", {"name": "write_file", "result": {"ok": True}})
    return RunOutput(output={"case": task["case"], "arm": arm, "log_dir": str(log)}, log=log, cost_usd=0.01,
                     tokens=10)
''' % PNG_B64

JUDGE = '''
import json
from pathlib import Path

def score(task, output):
    log = Path(output.log)
    (log / "judge").mkdir(exist_ok=True)
    (log / "judge" / "judge.json").write_text(json.dumps({"rubric": "%s", "score": 0.625}))
    return {"score": 0.625, "feedback": "", "rubric_score": 0.625, "note": "%s"}
''' % (JUDGE_MARK, JUDGE_MARK)

TEMPLATE = {
    "adapter.yaml": yaml.safe_dump({
        "template": {"id": "visual-mwm-test", "version": "1.0", "contract": "1"},
        "exports": {"context": "ADAPTER.md"},
        "structure": {"learnable": [{"path": p, "format": {"text": True}} for p in
                                    ("memory/**/*.md", "examples/**/*.md", "meta/journal.md",
                                     "meta/journal-archive.md", "meta/cases/**/*.md")]
                      + [{"path": "consumption/brief.md", "format": {"text": True, "max_bytes": 6000}}],
                      "fixed": ["ADAPTER.md", "EVOLVER.md", "README.md"]}}, sort_keys=False),
    "ADAPTER.md": "The MWM world model.\n", "EVOLVER.md": "guide\n", "README.md": "readme\n",
    "memory/README.md": "memory\n", "consumption/brief.md": "brief\n", "meta/journal.md": "# Journal\n",
    "meta/cases/README.md": "cases\n", "examples/README.md": "examples\n"}


class FakeClient:
    model = "fake-glm"

    def __init__(self, replies):
        self.replies, self.requests, self.timeout = list(replies), [], None

    def chat(self, messages, **kwargs):
        self.requests.append(json.loads(json.dumps(messages)))
        return dict(self.replies.pop(0)), Usage(self.model, 1000, 400, 50, 1, 0.002)


def call(name, **arguments):
    call.n = getattr(call, "n", 0) + 1
    return {"id": f"c{call.n}", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def reply(*calls, content="done"):
    message = {"role": "assistant", "content": content, "reasoning_content": REASONING_MARK,
               "finish_reason": "tool_calls" if calls else "stop"}
    if calls:
        message["tool_calls"] = list(calls)
    return message


def make_task(tmp: Path) -> Path:
    for name, text in TEMPLATE.items():
        (tmp / "template" / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp / "template" / name).write_text(text)
    row = {"id": "waves", "case": "waves", "title": "Ocean waves", "prompt": "Ocean waves\n\nAn ocean.",
           "rubric": ["The ocean is recognizable."], "common_contract": {"submission": "index.html"}}
    (tmp / "tasks.jsonl").write_text(json.dumps(row) + "\n" + json.dumps({**row, "id": "waves-eval"}) + "\n")
    (tmp / "train.txt").write_text("waves\n")
    (tmp / "test.txt").write_text("waves-eval\n")
    (tmp / "task-info").mkdir()
    (tmp / "task-info" / "README.md").write_text("Ocean waves.\n")
    (tmp / "cases.json").write_text(json.dumps({"common_contract": row["common_contract"], "cases": [
        {"id": "waves", "title_en": "Ocean waves", "prompt": row["prompt"], "rubric": row["rubric"]}]}))
    (tmp / "pipeline.py").write_text(PIPELINE)
    (tmp / "judge.py").write_text(textwrap.dedent(JUDGE))
    config = {"data": {"tasks": "tasks.jsonl", "train": "train.txt", "test": "test.txt", "step_size": 1},
              "task_info": "task-info", "pipeline": {"run": "pipeline.py:run", "concurrency": 1},
              "judge": {"path": "judge.py"}, "template": "template",
              "evolver": {"name": str(EVOLVER_DIR), "params": {"rounds": 2, "cases_file": str(tmp / "cases.json")}},
              "sandbox": {"enabled": False, "network": "open", "env": []},
              "trainer": {"run_name": "waves-r2", "output_dir": "runs", "tree_dir": "runs/trees/waves", "epochs": 3,
                          "save_freq": {"steps": 1, "epochs": 1}, "budget": {"usd": 5, "hours": 1},
                          "call_timeout_min": 5}}
    path = tmp / "waves.yaml"
    path.write_text(yaml.safe_dump(config))
    return path


def test_two_learning_sessions_then_a_measurement_round(tmp_path):
    tmp_path = tmp_path.resolve()
    trainer = Trainer(Config.load(make_task(tmp_path), []), log=lambda *_: None)
    client = FakeClient([
        # session 1: evidence B only
        reply(call("list_files", area="evidence"), call("read_image", area="evidence", path="B/capture/phone.png")),
        reply(call("write_file", area="adapter", path="memory/waves-surface.md",
                   content="brief: crest shading\nlast_confirmed_t: 1\n"), call("validate")),
        reply(content="Session 1 report."),
        # session 2: evidence B and C1
        reply(call("read_file", area="evidence", path="C1/result.json"),
              call("read_image", area="evidence", path="B/capture/desktop-t2.png"),
              call("read_image", area="evidence", path="C1/capture/desktop-t0.png")),
        reply(call("write_file", area="adapter", path="memory/waves-motion.md", content="brief: motion\n")),
        reply(content="Session 2 report."),
    ])
    trainer.evolver.client = client
    summary = trainer.train()

    run = tmp_path / "runs" / "waves-r2"
    steps = [json.loads(l) for l in (run / "steps.jsonl").read_text().splitlines()]
    assert [s["evolver"]["status"] for s in steps] == ["done", "done", "done"]
    assert [s["changed"] for s in steps] == [True, True, False]
    assert steps[2]["evolver"]["summary"] == "measurement round" and steps[2]["evolver"]["usage"] == []
    assert steps[0]["evolver"]["usage"][0]["calls"] == 3 and steps[0]["evolver"]["usage"][0]["cost_usd"] > 0
    assert summary["spending"]["usd"]["evolver"] > 0
    tree = Path(summary["tree"])
    tags = set(git(tree, "tag").splitlines())
    assert {"waves-r2/ckpt-step-1", "waves-r2/ckpt-step-2", "waves-r2/ckpt-step-3", "waves-r2/final"} <= tags
    assert "crest shading" in git(tree, "show", "waves-r2/ckpt-step-1:memory/waves-surface.md")
    assert "motion" in git(tree, "show", "waves-r2/ckpt-step-2:memory/waves-motion.md")
    assert git(tree, "rev-parse", "waves-r2/ckpt-step-2") == git(tree, "rev-parse", "waves-r2/ckpt-step-3")

    first, second = (json.loads((run / f"step_000{n}" / "output" / "evidence-manifest.json").read_text())
                     for n in (1, 2))
    assert first["runs"] == ["B"] and second["runs"] == ["B", "C1"]
    assert [e["arm"] for e in second["episodes"]] == ["harness", "adapted"]
    assert "B/capture/phone.png" in second["original_images"]
    assert "REVIEW SESSION #2." in (run / "step_0002" / "output" / "prompt.md").read_text()
    assert "measurement round" in (run / "step_0003" / "output" / "report.md").read_text()

    # Session 2 read step 1's run (B) and its own (C1); no judge record or score ever reached the model, and no
    # stored record keeps its reasoning or an image payload.
    session2 = client.requests[3:]
    assert len(client.requests) == 6
    shown = json.dumps(client.requests[4])
    assert "Original B screenshot: B/capture/desktop-t2.png" in shown
    assert "Original C1 screenshot: C1/capture/desktop-t0.png" in shown
    assert any(m["role"] == "tool" and '"path": "C1/result.json"' in m["content"] and '\\"arm\\": \\"adapted\\"'
               in m["content"] for m in session2[1])
    sent = json.dumps(client.requests)
    assert JUDGE_MARK not in sent and "0.625" not in sent
    assert (run / "learning_material" / "step_0002" / "trajectories" / "waves" / "judge" / "judge.json").exists()
    assert {f["path"].split("/")[0] for f in second["copied_files"]} == {"B", "C1"}
    assert not any("judge" in f["path"] for f in second["copied_files"])
    stored = "".join(p.read_text(errors="replace") for p in run.rglob("*")
                     if p.is_file() and "step_" in str(p.relative_to(run)) and "output" in p.parts)
    assert REASONING_MARK not in stored and base64.b64encode(base64.b64decode(PNG_B64)).decode()[:40] not in stored


def test_a_session_that_changes_nothing_makes_the_next_run_a_second_b(tmp_path):
    tmp_path = tmp_path.resolve()
    trainer = Trainer(Config.load(make_task(tmp_path), []), log=lambda *_: None)
    client = FakeClient([
        reply(call("read_image", area="evidence", path="B/capture/phone.png")),
        reply(content="Session 1: nothing to change yet."),
        reply(call("read_image", area="evidence", path="B-2/capture/desktop-t0.png")),
        reply(call("write_file", area="adapter", path="memory/waves-motion.md", content="brief: motion\n")),
        reply(content="Session 2 report."),
    ])
    trainer.evolver.client = client
    trainer.train()
    run = tmp_path / "runs" / "waves-r2"
    steps = [json.loads(l) for l in (run / "steps.jsonl").read_text().splitlines()]
    assert [s["changed"] for s in steps] == [False, True, False]
    second = json.loads((run / "step_0002" / "output" / "evidence-manifest.json").read_text())
    assert second["runs"] == ["B", "B-2"] and [e["arm"] for e in second["episodes"]] == ["harness", "harness"]
    prompt = (run / "step_0002" / "output" / "prompt.md").read_text()
    assert "from the supplied GLM episodes (B, B-2), starting from generic MWM W0." in prompt
    assert "Original B-2 screenshot: B-2/capture/desktop-t0.png" in json.dumps(client.requests[3])
    assert "C1" not in prompt and "session 1" not in prompt


def test_a_pipeline_stop_leaves_the_run_resumable(tmp_path):
    """Step 2's run stops everything (as on HTTP 401): no step 2 record, no summary; the same config resumes there."""
    tmp_path = tmp_path.resolve()
    config = make_task(tmp_path)
    (tmp_path / "stop-once").write_text("")
    client = FakeClient([
        reply(call("write_file", area="adapter", path="memory/waves-surface.md", content="brief: crest\n")),
        reply(content="Session 1 report."),
        reply(call("read_image", area="evidence", path="C1/capture/desktop-t0.png")),
        reply(content="Session 2 report."),
    ])
    trainer = Trainer(Config.load(config, []), log=lambda *_: None)
    trainer.evolver.client = client
    with pytest.raises(SystemExit, match="HTTP 401"):
        trainer.train()
    run = tmp_path / "runs" / "waves-r2"
    assert len((run / "steps.jsonl").read_text().splitlines()) == 1 and not (run / "summary.json").exists()
    assert len(client.requests) == 2                                   # no learner call after the stop

    trainer = Trainer(Config.load(config, []), log=lambda *_: None)    # the same command again
    trainer.evolver.client = client
    summary = trainer.train()
    steps = [json.loads(l) for l in (run / "steps.jsonl").read_text().splitlines()]
    assert summary["stop_reason"] == "completed all epochs" and [s["step"] for s in steps] == [1, 2, 3]
    second = json.loads((run / "step_0002" / "output" / "evidence-manifest.json").read_text())
    assert second["runs"] == ["B", "C1"] and len(client.requests) == 4
