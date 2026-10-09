"""End to end, offline: `harnessadapter train` and `eval` on run_waves.yaml with every real part (pipeline, harness,
browser capture, judge, the W0 template, the glm_mwm Evolver, the Trainer) except the network. urllib.request.urlopen is
replaced by a scripted Fireworks stand-in that answers the three GLM callers (actor, rubric judge, learner); a request
to any other URL fails the test, and the key is a synthetic value.

Two training steps (epochs=2): step 1 runs the root W0 as arm B and the learner writes memory (W1); step 2 runs W1 as
arm C, which reads that memory through `wm`, and the learner's evidence holds runs B and C1 without any judge record.
Then `eval --adapter waves-e2e/ckpt-step-1` runs W1 twice (eval.repeats=2), `eval --adapter final` runs the final
adapter (which carries the Trainer's CARD.md) once as arm C without showing the card, `eval --adapter none` runs arm B
once, and the README's arm A overrides run arm A once.

Opt in with VISUAL_REAL_CAPTURE=1: it launches Chrome 13 times (about 3 minutes). No model is called.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sys
import urllib.request

import pytest
import yaml

from harnessadapter import cli
from harnessadapter.config import Config
from harnessadapter.trainer.trainer import Trainer
from harnessadapter.tree import git

REPO = Path(__file__).resolve().parents[2]
TASK = REPO / "examples" / "tasks" / "visual_showcase"
CONFIG = REPO / "examples" / "mwm" / "visual_showcase" / "run_waves.yaml"
sys.path.insert(0, str(TASK))
import judge  # noqa: E402
import pipeline  # noqa: E402
import prepare  # noqa: E402

pytestmark = pytest.mark.skipif(os.environ.get("VISUAL_REAL_CAPTURE") != "1",
                                reason="set VISUAL_REAL_CAPTURE=1 to train with the real browser capture")

URL = "https://api.fireworks.ai/inference/v1/chat/completions"
FAKE_KEY = "synthetic-e2e-key-7d1c"                  # not a credential; checked never to reach a file
REASONING = "PRIVATE-REASONING-e2e-51a0"              # must never reach a file
JUDGE_MARK = "JUDGE-ONLY-e2e-93bf"                     # must never reach the learner or the actor
HTML_B = (Path(__file__).parent / "task_fixtures" / "harness-waves" / "submission" / "index.html").read_text()
HTML_C = HTML_B.replace("</body>", "<!-- arm C revision -->\n</body>", 1)
MEMORY_ID = "waves-crest-shading"
MEMORY = """last_confirmed_t: 1
scope: waves case, run B final capture
derived_from: B/capture/desktop-t2.png, B/submission/index.html
brief: Crest highlights tied to the displaced surface read as moving water; compare desktop-t0/t2/t5 for motion.

Observed in run B: the surface is one continuous mesh and the highlights move with it (desktop-t2, desktop-t5).
MEMORY-MARK-e2e-0b4e
"""
BRIEF = """# Environment briefing

- Ocean waves: keep one continuous surface; see memory `waves-crest-shading` for the motion check.
"""
MEMORY_2 = """last_confirmed_t: 2
scope: waves case, runs B and C1
derived_from: B/capture/phone.png, C1/capture/desktop-t5.png
brief: The phone capture keeps the controls inside the viewport in both runs.

Observed in B and C1: controls stay visible at 390x844.
"""
SCORES = [4, 3, 4, 2, 5]                 # rubric_score = mean((s - 1) / 4) = 0.65


def tool_call(turn, index, function, **arguments):
    return {"id": f"call-{turn}-{index}", "type": "function",
            "function": {"name": function, "arguments": json.dumps(arguments)}}


def assistant(content="", calls=()):
    message = {"role": "assistant", "content": content, "reasoning_content": REASONING}
    if calls:
        message["tool_calls"] = list(calls)
    return message


class Response:
    def __init__(self, payload):
        self.body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.body


class FakeFireworks:
    """The chat/completions endpoint, scripted per caller. Each request is kept: (caller, body, headers, timeout)."""

    def __init__(self):
        self.requests = []
        self.judge_calls = 0

    def urlopen(self, request, timeout=None):
        if request.full_url != URL:
            raise AssertionError(f"unexpected network request: {request.full_url}")
        body = json.loads(request.data)
        first = body["messages"][0]
        if first["role"] == "system" and first["content"] == judge.RUBRIC_SYSTEM:
            caller = "judge"
        elif first["role"] == "system":
            caller = "actor"
        elif first["role"] == "user" and "REVIEW SESSION #" in first["content"]:
            caller = "learner"
        else:
            raise AssertionError("request from an unknown caller")
        self.requests.append({"caller": caller, "body": body, "headers": dict(request.header_items()),
                              "timeout": timeout})
        message = getattr(self, caller)(body)
        finish = "tool_calls" if message.get("tool_calls") else "stop"
        return Response({"id": f"fake-{len(self.requests)}", "model": body["model"],
                         "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                         "usage": {"prompt_tokens": 2000, "completion_tokens": 300,
                                   "prompt_tokens_details": {"cached_tokens": 500},
                                   "completion_tokens_details": {"reasoning_tokens": 120}}})

    @staticmethod
    def turn(body):
        return sum(1 for m in body["messages"] if m["role"] == "assistant")

    def actor(self, body):
        t = self.turn(body)
        if "tools" not in body:                                      # arm A: the whole page as raw HTML
            return assistant(HTML_B)
        if pipeline.BRIDGE in body["messages"][0]["content"]:       # arm C: read the learned memory first
            script = [[tool_call(t, 1, "wm", name="memory", args={"id": MEMORY_ID})],
                      [tool_call(t, 1, "write_file", content=HTML_C)], [tool_call(t, 1, "inspect_page")]]
        else:                                                        # arm B
            script = [[tool_call(t, 1, "write_file", content=HTML_B)], [tool_call(t, 1, "inspect_page")]]
        if t < len(script):
            return assistant(calls=script[t])
        return assistant("Checked the page once with inspect_page; no unresolved errors were reported.")

    def judge(self, body):
        self.judge_calls += 1
        if self.judge_calls == 1:                                    # exercise the one retry on an unusable reply
            return assistant("The scene looks like an ocean.")
        return assistant(json.dumps({"dimensions": [
            {"dimension": n, "score": s, "screenshot": "desktop-t2", "reason": f"{JUDGE_MARK} dimension {n}."}
            for n, s in enumerate(SCORES, 1)]}))

    def learner(self, body):
        t = self.turn(body)
        session = int(re.search(r"REVIEW SESSION #(\d+)", body["messages"][0]["content"]).group(1))
        if session == 1:
            script = [[tool_call(t, 1, "list_files", area="evidence")],
                      [tool_call(t, 1, "read_file", area="evidence", path="README.md"),
                       tool_call(t, 2, "read_file", area="evidence", path="B/result.json"),
                       tool_call(t, 3, "read_image", area="evidence", path="B/capture/desktop-t2.png")],
                      [tool_call(t, 1, "write_file", area="adapter", path=f"memory/{MEMORY_ID}.md", content=MEMORY),
                       tool_call(t, 2, "write_file", area="adapter", path="consumption/brief.md", content=BRIEF),
                       tool_call(t, 3, "validate")]]
        else:
            script = [[tool_call(t, 1, "list_files", area="evidence")],
                      [tool_call(t, 1, "read_image", area="evidence", path="B/capture/phone.png"),
                       tool_call(t, 2, "read_image", area="evidence", path="C1/capture/desktop-t5.png"),
                       tool_call(t, 3, "read_file", area="evidence", path="C1/result.json")],
                      [tool_call(t, 1, "write_file", area="adapter", path="memory/waves-phone-controls.md",
                                 content=MEMORY_2), tool_call(t, 2, "validate")]]
        if t < len(script):
            return assistant(calls=script[t])
        return assistant(f"Session {session}: wrote memory from the supplied runs; validate passed.")

    def of(self, caller):
        return [r for r in self.requests if r["caller"] == caller]


def tool_replies(body):
    return [m["content"] for m in body["messages"] if m["role"] == "tool"]


def image_labels(body):
    return [p["text"] for m in body["messages"] if isinstance(m.get("content"), list)
            for p in m["content"] if p.get("type") == "text"]


def image_count(body):
    return sum(1 for m in body["messages"] if isinstance(m.get("content"), list)
               for p in m["content"] if p.get("type") == "image_url")


def test_train_two_steps_then_eval_a_checkpoint_with_repeats(tmp_path, monkeypatch):
    tmp_path = tmp_path.resolve()
    fake = FakeFireworks()
    monkeypatch.setattr(urllib.request, "urlopen", fake.urlopen)
    monkeypatch.setenv("FIREWORKS_API_KEY", FAKE_KEY)
    monkeypatch.setenv("VISUAL_LOG_ROOT", str(tmp_path / "logs"))
    monkeypatch.delenv("VISUAL_JUDGE", raising=False)
    data = tmp_path / "task"            # what prepare.py writes into the task folder, written here instead
    for relative, text in prepare.generated(prepare.check_inputs()).items():
        (data / relative).parent.mkdir(parents=True, exist_ok=True)
        (data / relative).write_text(text)
    overrides = [f"trainer.output_dir={tmp_path / 'runs'}", f"trainer.tree_dir={tmp_path / 'runs' / 'trees' / 'waves'}",
                 "trainer.run_name=waves-e2e", "trainer.epochs=2", f"data.tasks={data / 'tasks.jsonl'}",
                 f"data.train={data / 'splits' / 'waves.txt'}", f"data.test={data / 'splits' / 'waves-eval.txt'}"]
    trainer = Trainer(Config.load(CONFIG, overrides), log=lambda *_: None)
    monkeypatch.setattr(sys, "dont_write_bytecode", False)      # as without PYTHONDONTWRITEBYTECODE
    summary = trainer.train()
    monkeypatch.setattr(sys, "dont_write_bytecode", True)

    run = tmp_path / "runs" / "waves-e2e"
    tree = Path(summary["tree"])
    steps = [json.loads(l) for l in (run / "steps.jsonl").read_text().splitlines()]
    assert summary["stop_reason"] == "completed all epochs" and len(steps) == 2
    assert [s["evolver"]["status"] for s in steps] == ["done", "done"]
    assert [s["changed"] for s in steps] == [True, True] and not any(s["repaired"] for s in steps)
    assert [s["rollout"]["scores"]["waves"] for s in steps] == [0.65, 0.65]
    assert {"waves-e2e/ckpt-step-1", "waves-e2e/ckpt-step-2", "waves-e2e/ckpt-epoch-1", "waves-e2e/ckpt-epoch-2",
            "waves-e2e/final", "root"} <= set(git(tree, "tag").splitlines())

    # Step 1: arm B on the root W0 (no origin), with the real inspection and final capture.
    material = run / "learning_material"
    b_log = material / "step_0001" / "trajectories" / "waves"
    b = json.loads((b_log / "result.json").read_text())
    assert (b["arm"], b["status"], b["valid_submission"], b["inspection_calls"], b["wm_calls"]) == \
        ("harness", "completed", True, 1, 0)
    assert b["final_inspection_matches"] is True and b["usage"]["reasoning_tokens"] == 120 * 3
    b_request = json.loads((b_log / "request.json").read_text())
    assert b_request["system"] == pipeline.COMMON + "\n\n" + pipeline.LOOP
    assert [t["function"]["name"] for t in b_request["tools"]] == ["write_file", "read_file", "replace_text",
                                                                   "inspect_page"]
    assert "origin" not in yaml.safe_load(git(tree, "show", "root:adapter.yaml"))
    b_capture = json.loads((b_log / "capture" / "summary.json").read_text())
    assert b_capture["capture_ok"] is True and b_capture["input"]["index_sha256"] == b["submission_sha256"]
    b_judge = json.loads((b_log / "judge" / "judge.json").read_text())
    assert b_judge["rubric_score"] == 0.65 and len(b_judge["rubric"]["replies"]) == 2     # one retry
    actor = fake.of("actor")
    assert image_count(actor[2]["body"]) == 5                       # the inspection's five PNGs reached the actor

    # The learner of step 1 wrote memory and a brief; the Trainer committed them with an origin (W1).
    assert "MEMORY-MARK-e2e-0b4e" in git(tree, "show", f"waves-e2e/ckpt-step-1:memory/{MEMORY_ID}.md")
    assert git(tree, "show", "waves-e2e/ckpt-step-1:consumption/brief.md") == BRIEF.rstrip("\n")
    assert yaml.safe_load(git(tree, "show", "waves-e2e/ckpt-step-1:adapter.yaml"))["origin"] == \
        {"template": "visual-mwm", "version": "1.0"}

    # Step 2: arm C on W1: BRIDGE + the package (it lists the new memory) and wm, which reads it.
    c_log = material / "step_0002" / "trajectories" / "waves"
    c = json.loads((c_log / "result.json").read_text())
    assert (c["arm"], c["status"], c["valid_submission"], c["inspection_calls"], c["wm_calls"]) == \
        ("adapted", "completed", True, 1, 1)
    c_request = json.loads((c_log / "request.json").read_text())
    assert c_request["system"].startswith(pipeline.COMMON + "\n\n" + pipeline.LOOP + "\n\n" + pipeline.BRIDGE + "\n\n")
    assert f"[{MEMORY_ID}]" in c_request["system"] and "Crest highlights tied to" in c_request["system"]
    assert c_request["adapter"]["used"] is True and c_request["adapter"]["origin"] == {"template": "visual-mwm",
                                                                                      "version": "1.0"}
    assert c["adapter_context_sha256"] == c_request["adapter"]["context_sha256"]
    assert c_request["tools"][-1]["function"]["name"] == "wm"
    wm_record = json.loads((c_log / "tools" / "01.json").read_text())
    assert wm_record["name"] == "wm" and "MEMORY-MARK-e2e-0b4e" in wm_record["result"]["text"]
    assert not list((run / "rollout_adapter").rglob("__pycache__"))          # the adapter got no bytecode caches

    # The learner's evidence: step 1 holds run B; step 2 holds B and C1, both with images; nothing from the judge.
    first, second = (json.loads((run / f"step_000{n}" / "output" / "evidence-manifest.json").read_text())
                     for n in (1, 2))
    assert first["runs"] == ["B"] and second["runs"] == ["B", "C1"]
    assert [e["arm"] for e in second["episodes"]] == ["harness", "adapted"]
    assert {f["path"].split("/")[0] for f in second["copied_files"]} == {"B", "C1"}
    assert not any("judge" in f["path"] for f in second["copied_files"])
    learner = fake.of("learner")
    sessions = [[r for r in learner if f"REVIEW SESSION #{n}." in r["body"]["messages"][0]["content"]] for n in (1, 2)]
    assert [len(s) for s in sessions] == [4, 4]
    listed = json.loads(tool_replies(sessions[1][1]["body"])[0])["files"]
    assert any(p.startswith("B/") for p in listed) and any(p.startswith("C1/") for p in listed)
    assert not any("judge" in p for p in listed)
    labels = image_labels(sessions[1][2]["body"])
    assert "Original B screenshot: B/capture/phone.png" in labels
    assert "Original C1 screenshot: C1/capture/desktop-t5.png" in labels and image_count(sessions[1][2]["body"]) == 2
    assert "Original B screenshot: B/capture/desktop-t2.png" in image_labels(sessions[0][2]["body"])
    for r in learner + actor:
        text = json.dumps(r["body"])
        assert JUDGE_MARK not in text and "rubric_score" not in text and "mechanical_score" not in text

    # The transport: old settings on every request, the key only in the Authorization header.
    for r in fake.requests:
        body = r["body"]
        assert r["headers"]["Authorization"] == f"Bearer {FAKE_KEY}"
        assert {k: body[k] for k in pipeline.SAMPLING} == pipeline.SAMPLING and body["model"] == pipeline.MODEL
        assert r["timeout"] <= 420 and body["max_tokens"] <= 32768
        if r["caller"] == "learner":
            assert "tool_choice" not in body and len(body["tools"]) == 8
        elif r["caller"] == "actor":
            assert body["tool_choice"] == "auto"
        else:
            assert "tools" not in body and body["max_tokens"] == 32768

    # eval --adapter <tag> with repeats: W1 twice, as arm C.
    assert cli.main(["eval", "--config", str(CONFIG), "--adapter", "waves-e2e/ckpt-step-1",
                     *overrides, "eval.repeats=2"]) == 0
    out = run / "eval" / "waves-e2e_ckpt-step-1"
    rows = [json.loads(l) for l in (out / "results.jsonl").read_text().splitlines()]
    assert [(r["repeat"], r["id"], r["status"], r["output"]["arm"]) for r in rows] == [
        (1, "waves-eval", "ok", "adapted"), (2, "waves-eval", "ok", "adapted")]
    assert rows[0]["log"] != rows[1]["log"] and all(r["judge"]["score"] == 0.65 for r in rows)
    eval_summary = json.loads((out / "summary.json").read_text())
    assert (eval_summary["repeats"], eval_summary["tasks"], eval_summary["mean_score"]) == (2, 1, 0.65)

    # eval --adapter final: the tag carries the Trainer's CARD.md (spending; the eval then adds its score), which the
    # agent must not reach; the pipeline loads a copy without it.
    card_lines = [l for l in git(tree, "show", "waves-e2e/final:CARD.md").splitlines() if len(l) > 30]
    before = len(fake.of("actor"))
    assert cli.main(["eval", "--config", str(CONFIG), "--adapter", "final", *overrides,
                     "eval.repeats=1"]) == 0
    row = json.loads((run / "eval" / "final" / "results.jsonl").read_text().splitlines()[0])
    final_request = json.loads((Path(row["log"]) / "request.json").read_text())
    assert row["output"]["arm"] == "adapted" and final_request["adapter"]["not_shown_to_agent"] == ["CARD.md"]
    final_actor = json.dumps([r["body"] for r in fake.of("actor")[before:]])
    assert card_lines and len(fake.of("actor")) > before and not any(l in final_actor for l in card_lines)
    assert "Mean score" in git(tree, "show", "waves-e2e/final:CARD.md")      # eval wrote its score into the card

    # eval --adapter none is arm B; the README's overrides make it arm A, in a run directory of its own.
    assert cli.main(["eval", "--config", str(CONFIG), "--adapter", "none", *overrides,
                     "eval.repeats=1"]) == 0
    assert cli.main(["eval", "--config", str(CONFIG), "--adapter", "none", *overrides,
                     "eval.repeats=1", "pipeline.run=../../tasks/visual_showcase/pipeline.py:run_direct",
                     "trainer.run_name=waves-direct"]) == 0
    for name, arm in (("waves-e2e", "harness"), ("waves-direct", "direct")):
        rows = [json.loads(l) for l in (tmp_path / "runs" / name / "eval" / "none" / "results.jsonl").read_text()
                .splitlines()]
        assert [(r["status"], r["output"]["arm"], r["output"]["status"], r["judge"]["score"]) for r in rows] == [
            ("ok", arm, "completed", 0.65)]
    direct = json.loads((Path(rows[0]["log"]) / "result.json").read_text())
    assert (direct["arm"], direct["extraction"], direct["model_calls"], direct["tool_calls"]) == \
        ("direct", "raw_html", 1, 0)
    assert direct["submission_sha256"] == hashlib.sha256(HTML_B.strip().encode()).hexdigest()

    # Nothing private on disk: no reasoning text, no key value.
    for path in tmp_path.rglob("*"):
        if path.is_file() and ".git" not in path.parts:
            data = path.read_bytes()
            assert REASONING.encode() not in data and FAKE_KEY.encode() not in data, path
