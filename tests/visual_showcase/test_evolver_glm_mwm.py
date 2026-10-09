"""The glm_mwm Evolver offline: a scripted fake GLM, fake learning material for steps 1..3, no network, no key."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import threading

import pytest

from harnessadapter.evolver.base import Access, Progress, StepContext, Usage, Workspace
from harnessadapter.evolver.loader import find, instantiate

EVOLVER_DIR = Path(__file__).resolve().parents[2] / "examples" / "mwm" / "visual_showcase" / "glm_mwm"
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII=")
SHOTS = ("desktop-t0", "desktop-t2", "desktop-t5", "desktop-interaction", "phone")
JUDGE_MARK = "JUDGE-ONLY-7f3a"            # must never reach the learner
OTHER_CASE_MARK = "OTHER-CASE-91c2"
REASONING_MARK = "PRIVATE-REASONING-55e1"
CASES = {"common_contract": {"submission": "index.html", "intensity": {"initial": 0.4, "inspection": 0.75}},
         "cases": [{"id": "waves", "title_en": "Ocean waves", "prompt": "Ocean waves\n\nCreate an ocean.",
                    "rubric": [{"id": "surface", "observation_en": "The ocean is recognizable."}]},
                   {"id": "lava", "title_en": "Lava lamp", "prompt": "Lava lamp", "rubric": []}]}


def load():
    evolver, params = instantiate(find(str(EVOLVER_DIR)), {})
    return evolver, params, sys.modules[type(evolver).__module__]


def save(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=1))


def png(tag: str) -> bytes:
    return PNG + tag.encode()          # a PNG signature with distinct bytes per image


def run_log(root: Path, case: str, arm: str, tag: str) -> Path:
    """A run log in the pipeline's layout, plus a judge record and a score the learner must not see."""
    log = root / f"log-{tag}"
    (log / "submission").mkdir(parents=True)
    (log / "submission" / "index.html").write_text(f"<!doctype html><title>{tag}</title>")
    source = hashlib.sha256((log / "submission" / "index.html").read_bytes()).hexdigest()
    shots = []
    for name in SHOTS:
        data = png(f"{tag}-{name}")
        (log / "capture").mkdir(exist_ok=True)
        (log / "capture" / f"{name}.png").write_bytes(data)
        shots.append({"id": name, "file": f"{name}.png", "sha256": hashlib.sha256(data).hexdigest(),
                      "bytes": len(data)})
    (log / "capture" / "diagnostic-paused.png").write_bytes(png("diagnostic"))
    save(log / "capture" / "summary.json", {"case": case, "capture_ok": True, "screenshots": shots,
                                             "input": {"index_sha256": source},
                                             "observations": {"animation": [0.2, 0.3]}})
    check = log / "checks" / "check-01" / "capture"
    check.mkdir(parents=True)
    data = png(f"{tag}-check")
    (check / "desktop-t0.png").write_bytes(data)
    save(check / "summary.json", {"screenshots": [{"file": "desktop-t0.png",
                                                   "sha256": hashlib.sha256(data).hexdigest()}]})
    save(log / "result.json", {"arm": arm, "case": case, "status": "completed", "valid_submission": True,
                               "submission_sha256": source, "score": 0.731, "rubric_score": 0.5})
    save(log / "calls" / "01.json", {"call": 1, "assistant": {"role": "assistant", "content": f"actor {tag}"}})
    save(log / "tools" / "01.json", {"name": "write_file", "arguments": {"content": "<html></html>"},
                                     "result": {"ok": True}})
    save(log / "judge" / "judge.json", {"score": 0.731, "rubric": JUDGE_MARK})
    save(log / "case.json", {"id": case})
    return log


def material(root: Path, steps: int, *, other_case_in_step: int | None = 2, commits=None) -> Path:
    """learning_material/step_0001..steps as the Trainer writes it. `commits` are the adapter commits the steps' runs
    used (default: every session changed the adapter); a run on the root commit (the first) is arm B, else arm C."""
    mat = root / "material"
    commits = commits or [f"commit{n}" for n in range(1, steps + 1)]
    for n in range(1, steps + 1):
        d = mat / f"step_{n:04d}"
        rows = [("waves", "waves", "harness" if commits[n - 1] == commits[0] else "adapted", f"waves{n}")]
        if n == other_case_in_step:
            rows.append(("lava", "lava", "adapted", OTHER_CASE_MARK))
        for task_id, case, arm, tag in rows:
            log = run_log(root / "logs", case, arm, tag)
            shutil.copytree(log, d / "trajectories" / task_id)
            save(d / "tasks" / f"{task_id}.json", {
                "id": task_id, "input": {"id": task_id, "case": case, "title": case, "rubric": ["r"]},
                "output": {"case": case, "arm": arm}, "status": "ok", "trajectory": f"trajectories/{task_id}",
                "judge": {"score": 0.731, "feedback": JUDGE_MARK, "extra": {"rubric_score": 0.5}}})
        save(d / "summary.json", {"step": n, "adapter_commit": commits[n - 1], "mean_score": 0.731})
    save(mat / f"step_{steps:04d}" / "check_feedback.json",
         [{"path": "consumption/brief.md", "restored_from": "parent", "reason": "max_bytes 6000 exceeded"}])
    (mat / "index.jsonl").write_text(json.dumps({"step": 1, "mean_score": 0.731}) + "\n")
    return mat


def adapter(path: Path, origin: bool = False) -> Path:
    """A workspace adapter; `origin` marks one the Trainer committed a learned change to."""
    files = {"adapter.yaml": "template: {id: visual-mwm, version: '1.0'}\n"
                             + ("origin: {template: visual-mwm, version: '1.0'}\n" if origin else ""),
             "ADAPTER.md": "fixed\n",
             "EVOLVER.md": "guide\n", "interface.py": "def wm(name, args=None):\n    return {}\n",
             "memory/README.md": "memory\n", "memory/_template.md": "brief: template\n",
             "consumption/brief.md": "short brief\n", "meta/journal.md": "# Journal\n",
             "meta/cases/README.md": "cases\n", "examples/README.md": "examples\n"}
    for name, text in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(text)
    return path


class FakeClient:
    """A scripted ChatClient: each reply is an assistant message (dict), an Exception, or a function of messages."""
    model = "fake-glm"

    def __init__(self, replies, usage=(100, 40, 20)):
        self.replies, self.requests, self.timeout, self.usage = list(replies), [], None, usage

    def chat(self, messages, **kwargs):
        self.requests.append({"messages": json.loads(json.dumps(messages)),
                              "kwargs": {k: v for k, v in kwargs.items() if k != "tools"},
                              "tools": kwargs.get("tools"), "timeout": self.timeout})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        prompt, cached, completion = self.usage
        return dict(reply), Usage(self.model, prompt, cached, completion, 1, 0.001)


def call(name, n=[0], **arguments):
    n[0] += 1
    return {"id": f"call-{n[0]}", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def reply(*calls, content=""):
    message = {"role": "assistant", "content": content, "reasoning_content": REASONING_MARK,
               "finish_reason": "tool_calls" if calls else "stop"}
    if calls:
        message["tool_calls"] = list(calls)
    return message


def context(tmp: Path, step: int, params: dict, *, cancel=None, check=None, origin=None) -> StepContext:
    ws = Workspace(adapter=tmp / "adapter", learning_material=tmp / "material", task_info=tmp / "info",
                   history=tmp / "history", state=tmp / "state", scratch=tmp / "scratch", output=tmp / "output")
    for d in (ws.adapter, ws.task_info, ws.history, ws.state, ws.scratch, ws.output):
        d.mkdir(parents=True, exist_ok=True)
    adapter(ws.adapter, origin=step > 1 if origin is None else origin)
    tools = {"check_spec": check or (lambda **_: {"passed": True, "issues": []})}
    return StepContext(workspace=ws, access=Access(), tools=tools, params=params, cancel=cancel,
                       progress=Progress(step, step, True))


@pytest.fixture
def setup(tmp_path):
    tmp_path = tmp_path.resolve()
    save(tmp_path / "cases.json", CASES)
    evolver, params, module = load()
    params = {**params, "cases_file": str(tmp_path / "cases.json")}
    return tmp_path, evolver, params, module


def all_text(root: Path) -> str:
    return "".join(p.read_text(errors="replace") for p in root.rglob("*") if p.is_file())


# ---------- write rules, file tools, images ----------

def test_write_boundaries(setup):
    tmp, _, _, m = setup
    roots = {"adapter": tmp / "a", "scratch": tmp / "s"}
    (tmp / "a" / "memory").mkdir(parents=True)
    (tmp / "s").mkdir()
    for name in ["memory/lesson.md", "examples/waves/event.md", "consumption/brief.md", "meta/journal.md",
                 "meta/journal-archive.md", "meta/cases/01-waves.md"]:
        m.check_mutation(roots, "write_file", {"area": "adapter", "path": name, "content": "lesson"})
    for name in ["interface.py", "adapter.yaml", "tools/x/TOOL.md", "memory/a.py", "../memory/a.md", "/memory/a.md",
                 "memory/.hidden.md", "memory/a/../../evil.md", "meta/other.md", "consumption/other.md", "ADAPTER.md"]:
        with pytest.raises(ValueError):
            m.check_mutation(roots, "write_file", {"area": "adapter", "path": name, "content": "x"})
    with pytest.raises(ValueError):
        m.check_mutation(roots, "write_file", {"area": "evidence", "path": "x.md", "content": "x"})
    with pytest.raises(ValueError):
        m.check_mutation(roots, "write_file", {"area": "scratch", "path": "run.py", "content": "x"})
    brief = {"area": "adapter", "path": "consumption/brief.md"}
    with pytest.raises(ValueError, match="6001 characters; maximum is 6000"):
        m.check_mutation(roots, "write_file", {**brief, "content": "x" * 6001})
    with pytest.raises(ValueError, match="UTF-8 bytes"):      # under 6000 characters, over the template's 6000 bytes
        m.check_mutation(roots, "write_file", {**brief, "content": "—" * 2500})
    (tmp / "a" / "memory" / "a.md").write_text("x")
    with pytest.raises(ValueError):
        m.check_mutation(roots, "move_path", {"area": "adapter", "source": "memory/a.md",
                                              "destination": "interface.py"})
    m.check_mutation(roots, "move_path", {"area": "adapter", "source": "memory/a.md", "destination": "examples/a.md"})


def test_old_file_semantics_and_reply_limit(setup):
    tmp, _, _, m = setup
    (tmp / "ev").mkdir()
    (tmp / "ev" / "big.json").write_text('"q"\n' * 7000)            # 28,000 characters, more once JSON-escaped
    (tmp / "ev" / "small.md").write_text("é" * 10)
    areas = m.Areas({"evidence": tmp / "ev", "adapter": tmp / "ad", "scratch": tmp / "sc"})
    with pytest.raises(m.ToolFailure, match="^Sandboxed file result exceeded its output limit$"):
        areas.operation("read_file", {"area": "evidence", "path": "big.json"})
    with pytest.raises(m.ToolFailure, match="^ValueError: Use a relative path without parent traversal or .git$"):
        areas.operation("read_file", {"area": "evidence", "path": "../x"})
    with pytest.raises(m.ToolFailure, match="^ValueError: Read: adapter/evidence/scratch/method/environment"):
        areas.operation("write_file", {"area": "evidence", "path": "x.md", "content": "x"})
    assert areas.operation("read_file", {"area": "evidence", "path": "small.md"}) == {
        "area": "evidence", "path": "small.md", "text": "é" * 10, "next_offset": None}
    unlimited = m.Areas({"evidence": tmp / "ev"}, reply_limit=None)
    page = unlimited.operation("read_file", {"area": "evidence", "path": "big.json"})
    assert len(page["text"]) == 28000 and page["next_offset"] is None


def test_latest_image_batch_cap_and_text_retention(setup):
    _, _, _, m = setup
    messages = [{"role": "user", "content": [{"type": "text", "text": "prior"},
                                             {"type": "image_url", "image_url": {"url": "OLD"}}]}]
    pictures = [{"type": "image_url", "image_url": {"url": "NEW"}}] * 2
    records = [{"path": "B/capture/a.png", "run": "B", "base64_characters": 5_000_000},
               {"path": "C1/capture/a.png", "run": "C1", "base64_characters": 5_000_000}]
    kept = m.retain_latest_images(messages, pictures, records)
    assert len(kept) == 1 and kept[0]["path"] == "B/capture/a.png"
    assert messages[0]["content"] == [{"type": "text", "text": "prior"}]
    assert messages[1]["content"][0] == {"type": "text", "text": "Original B screenshot: B/capture/a.png"}


# ---------- evidence ----------

def test_evidence_of_step_3_has_runs_b_c1_c2_and_no_score_judge_or_other_case(setup):
    tmp, _, _, m = setup
    mat = material(tmp, 3)
    out = tmp / "evidence"
    built = m.assemble_evidence(mat, 3, "waves", tmp / "cases.json", out)
    manifest = built["manifest"]
    assert manifest["runs"] == ["B", "C1", "C2"]
    assert [e["arm"] for e in manifest["episodes"]] == ["harness", "adapted", "adapted"]
    for label in ("B", "C1", "C2"):
        spec = json.loads((out / label / "task.json").read_text())
        assert spec == {"common_contract": CASES["common_contract"], "case": CASES["cases"][0]}   # rubric included
        assert (out / label / "submission" / "index.html").is_file()
        assert sorted(p.name for p in (out / label / "capture").glob("*.png")) == sorted(f"{s}.png" for s in SHOTS)
        assert (out / label / "checks" / "check-01" / "capture" / "desktop-t0.png").is_file()
        assert not (out / label / "judge").exists() and not (out / label / "case.json").exists()
        result = json.loads((out / label / "result.json").read_text())
        assert "score" not in result and "rubric_score" not in result and result["arm"]
    assert len(manifest["original_images"]) == 3 * (5 + 1)
    text = all_text(out)
    assert JUDGE_MARK not in text and OTHER_CASE_MARK not in text and "0.731" not in text
    assert "mean_score" not in text
    for item in manifest["copied_files"]:
        assert hashlib.sha256((out / item["path"]).read_bytes()).hexdigest() == item["sha256"]
    assert {s["path"] for s in built["scrubbed"]} == {f"{x}/result.json" for x in ("B", "C1", "C2")}


def test_labels_follow_the_adapter_each_run_used(setup):
    """Session 1 failed (no commit), so step 2 reran W0 as arm B; session 2 wrote W2; session 3 changed nothing."""
    tmp, _, _, m = setup
    mat = material(tmp, 4, other_case_in_step=None, commits=["root", "root", "w2", "w2"])
    manifest = m.assemble_evidence(mat, 4, "waves", tmp / "cases.json", tmp / "evidence")["manifest"]
    assert manifest["runs"] == ["B", "B-2", "C2", "C2-2"]
    assert [e["arm"] for e in manifest["episodes"]] == ["harness", "harness", "adapted", "adapted"]
    readme = (tmp / "evidence" / "README.md").read_text()
    assert "(4 runs: B, B-2, C2, C2-2)" in readme and "A label with a suffix (-2, -3, ...) is a further" in readme
    assert "Record notes" not in readme
    sessions = m.adapter_sessions(mat, 4)
    assert sessions == {1: 0, 2: 0, 3: 2, 4: 2}
    assert m.start_text(adapter(tmp / "w2", origin=True), sessions) == "the adapter written in session 2 from generic MWM W0"
    assert m.start_text(adapter(tmp / "w0"), m.adapter_sessions(mat, 2)) == "generic MWM W0"
    assert m.start_text(tmp / "w2", {1: 0, 2: 1, 3: 1, 4: 3}) == "the adapter written in sessions 1, 3 from generic MWM W0"
    assert m.start_text(tmp / "w2", {1: 0, 2: 1, 3: 2}) == "the adapter written in sessions 1-2 from generic MWM W0"
    # The normal case keeps the step-based labels and the old README sentence.
    normal = m.assemble_evidence(material(tmp / "n", 3, other_case_in_step=None), 3, "waves", tmp / "cases.json",
                                 tmp / "normal")["manifest"]
    assert normal["runs"] == ["B", "C1", "C2"] and "suffix" not in (tmp / "normal" / "README.md").read_text()


def test_a_run_whose_final_capture_failed_is_kept_with_a_note(setup):
    """The old gate refused the whole session; here the run stays and the README says what is missing."""
    tmp, _, _, m = setup
    mat = material(tmp, 2, other_case_in_step=None)
    traj = mat / "step_0002" / "trajectories" / "waves"
    source = hashlib.sha256((traj / "submission" / "index.html").read_bytes()).hexdigest()
    save(traj / "capture" / "summary.json", {"status": "capture_process_failed", "capture_ok": False,
                                              "screenshots": [], "input": {"index_sha256": source}})
    manifest = m.assemble_evidence(mat, 2, "waves", tmp / "cases.json", tmp / "evidence")["manifest"]
    b, c1 = manifest["episodes"]
    assert b["notes"] == [] and c1["images"] == [] and c1["check_images"] == ["C1/checks/check-01/capture/desktop-t0.png"]
    assert c1["notes"] == ["the final capture is not ok (status capture_process_failed); 0 of 5 final screenshots "
                           "are indexed"]
    readme = (tmp / "evidence" / "README.md").read_text()
    assert ("Record notes (facts about the copied records, not judgements; a run without indexed screenshots has none "
            "to read):\n- C1: the final capture is not ok (status capture_process_failed); 0 of 5 final screenshots "
            "are indexed.\n") in readme
    save(traj / "result.json", {"arm": "adapted", "case": "waves", "status": "completed", "submission_sha256": "x"})
    manifest = m.assemble_evidence(mat, 2, "waves", tmp / "cases.json", tmp / "evidence")["manifest"]
    assert "the submission, result.json and the final capture do not name the same source hash" in \
        manifest["episodes"][1]["notes"]


def test_no_learner_session_when_this_steps_run_left_no_record(setup):
    tmp, evolver, params, _ = setup
    mat = material(tmp, 2, other_case_in_step=None)
    path = mat / "step_0002" / "tasks" / "waves.json"
    record = json.loads(path.read_text())
    record.update(status="error", error="RuntimeError: synthetic pipeline crash", trajectory=None, output=None)
    save(path, record)
    shutil.rmtree(mat / "step_0002" / "trajectories")
    ctx = context(tmp, 2, params)
    before = all_text(ctx.workspace.adapter)
    evolver.client = client = FakeClient([])
    result = evolver.step(ctx)
    assert (result.status, result.usage) == ("failed", []) and "left no record" in result.summary
    assert client.requests == [] and all_text(ctx.workspace.adapter) == before
    assert "synthetic pipeline crash" in (ctx.workspace.output / "report.md").read_text()


def test_memory_index_is_newest_commit_first(setup):
    """The Trainer's history view (log.jsonl newest first, diffs/<commit>.patch) gives each file's last commit; the
    exported files all share one modification time."""
    tmp, _, _, m = setup
    a = adapter(tmp / "a", origin=True)
    for name in ("alpha", "beta", "gamma", "delta"):
        (a / "memory" / f"{name}.md").write_text(f"brief: {name}\nlast_confirmed_t: 1\n")
        os.utime(a / "memory" / f"{name}.md", ns=(10**18, 10**18))
    history = tmp / "history"
    (history / "diffs").mkdir(parents=True)
    commits = [("c3" * 20, "--- a/memory/gamma.md\n+++ b/memory/gamma.md\n@@ -1 +1 @@\n"),
               ("c2" * 20, "diff --git a/memory/old.md b/memory/delta.md\nrename from memory/old.md\n"
                           "rename to memory/delta.md\n+++ b/memory/alpha.md\n"),
               ("c1" * 20, "+++ b/memory/beta.md\n+++ b/memory/gamma.md\n+++ b/memory/old.md\n"),
               ("c0" * 20, " memory/README.md | 1 +\n")]
    (history / "log.jsonl").write_text("".join(json.dumps({"commit": c}) + "\n" for c, _ in commits))
    for commit, patch in commits:
        (history / "diffs" / f"{commit[:12]}.patch").write_text(patch)
    ranks = m.edit_ranks(history)
    assert (ranks["memory/gamma.md"], ranks["memory/delta.md"], ranks["memory/alpha.md"], ranks["memory/beta.md"]) \
        == (0, 1, 1, 2)
    order = [line.split("]")[0][1:] for line in m.memory_index(a, 3, ranks).splitlines()]
    assert order == ["gamma", "alpha", "delta", "beta"]
    assert m.edit_ranks(tmp / "no-history") == {}
    assert [line.split("]")[0][1:] for line in m.memory_index(a, 3, {}).splitlines()] == \
        ["alpha", "beta", "delta", "gamma"]                       # equal times: name order


def test_private_reasoning_cannot_enter_evidence(setup):
    tmp, _, _, m = setup
    mat = material(tmp, 1)
    save(mat / "step_0001" / "trajectories" / "waves" / "calls" / "01.json", {"reasoning_content": REASONING_MARK})
    with pytest.raises(ValueError, match="Private transport fields"):
        m.assemble_evidence(mat, 1, "waves", tmp / "cases.json", tmp / "evidence")


# ---------- one session ----------

def test_session_reads_text_and_images_writes_knowledge_and_validates(setup):
    tmp, evolver, params, m = setup
    material(tmp, 3)
    checks = []
    ctx = context(tmp, 3, params, check=lambda **kw: checks.append(kw) or {
        "passed": False, "issues": [{"path": "consumption/brief.md", "check": 4, "problem": "too long"}]})
    client = FakeClient([
        reply(call("list_files", area="evidence"), call("read_file", area="evidence", path="README.md"),
              call("read_file", area="evidence", path="C2/result.json"), call("get_policy")),
        reply(call("read_image", area="evidence", path="B/capture/desktop-t0.png"),
              call("read_image", area="evidence", path="C2/checks/check-01/capture/desktop-t0.png"),
              call("read_image", area="adapter", path="memory/README.md")),
        reply(call("write_file", area="adapter", path="memory/waves-lesson.md", content="brief: waves\n"),
              call("write_file", area="adapter", path="interface.py", content="x = 1\n"),
              call("write_file", area="adapter", path="memory/x.md", content="x", path2="extra"),
              call("validate")),
        reply(content="Changes: one memory entry. Evidence: B, C1, C2. Limits: none."),
    ])
    evolver.client = client
    result = evolver.step(ctx)

    assert result.status == "done" and result.summary.startswith("session 3 (waves): completed")
    assert result.usage == [Usage("fake-glm", 400, 160, 80, 4, pytest.approx(0.004))]
    assert (ctx.workspace.adapter / "memory" / "waves-lesson.md").read_text() == "brief: waves\n"
    assert (ctx.workspace.adapter / "interface.py").read_text().startswith("def wm")
    assert len(checks) == 1 and result.extra["changed"] == ["memory/waves-lesson.md"]

    first = client.requests[0]
    assert len(first["messages"]) == 1 and first["messages"][0]["role"] == "user"       # no system message
    prompt = first["messages"][0]["content"]
    assert prompt == (ctx.workspace.output / "prompt.md").read_text()
    assert "REVIEW SESSION #3." in prompt and "task family (waves) from the supplied GLM episodes (B, C1, C2)" in prompt
    assert "starting from the adapter written in sessions 1-2 from generic MWM W0" in prompt
    assert "Repair notice:" in prompt and "consumption/brief.md (max_bytes 6000 exceeded" in prompt
    assert f"Default command working directory: {ctx.workspace.adapter.resolve()}" in prompt
    assert first["kwargs"] == {"temperature": 0, "top_k": 40, "presence_penalty": 0, "frequency_penalty": 0,
                               "reasoning_effort": 4096, "max_tokens": 32768}                # no tool_choice
    assert first["tools"] == json.loads((EVOLVER_DIR / "tools.json").read_text())
    assert first["timeout"] <= 420
    assert client.requests[1]["kwargs"]["max_tokens"] == 32768                            # 65,536 - 20 left

    second = client.requests[1]["messages"]
    tool_results = {m_["tool_call_id"]: m_["content"] for m_ in second if m_["role"] == "tool"}
    listing = json.loads(next(iter(tool_results.values())))
    assert "B/capture/desktop-t0.png" in listing["files"] and not any("judge" in f for f in listing["files"])
    assert any("GLM episodes of the waves task (3 runs: B, C1, C2)" in v for v in tool_results.values())
    assert second[1]["reasoning_content"] == REASONING_MARK                    # carried in memory only
    policy = json.loads(second[-1]["content"])
    assert policy["write_rule"].startswith("Only candidate adapter") and policy["validation"] is None

    third = client.requests[2]["messages"]
    assert [x["role"] for x in third[-4:]] == ["tool", "tool", "tool", "user"]           # images after the tools
    images = third[-1]["content"]
    assert [p["type"] for p in images] == ["text", "image_url", "text", "image_url"]
    assert images[0]["text"] == "Original B screenshot: B/capture/desktop-t0.png"
    assert images[2]["text"] == "Original C2 screenshot: C2/checks/check-01/capture/desktop-t0.png"
    raw = base64.b64decode(images[1]["image_url"]["url"].split(",", 1)[1])
    assert raw == png("waves1-desktop-t0")
    assert json.loads(third[-2]["content"]) == {"ok": False, "error": "ValueError: Read an indexed original run "
                                                "screenshot from evidence"}
    fourth = client.requests[3]["messages"]       # the latest batch stays until a newer one replaces it
    assert sum(p["type"] == "image_url" for x in fourth if isinstance(x["content"], list) for p in x["content"]) == 2
    replies = [x["content"] for x in fourth if x["role"] == "tool"][-4:]
    assert json.loads(replies[0]) == {"written": "memory/waves-lesson.md", "area": "adapter", "bytes": 13}
    assert "approved Markdown" in replies[1] and "Unknown tool argument" in replies[2]
    assert json.loads(replies[3]) == {"passed": False, "findings": ["consumption/brief.md: too long (check 4)"],
                                      "scope": m.VALIDATE_SCOPE}

    sent = json.dumps(client.requests)
    assert JUDGE_MARK not in sent and OTHER_CASE_MARK not in sent and "0.731" not in sent
    out = ctx.workspace.output
    for name in ("report.md", "transcript.md", "changes.diff", "evidence-manifest.json", "learner/result.json",
                 "learner/final.md", "learner/calls/04.json", "learner/tool-results/011.json"):
        assert (out / name).is_file(), name
    state = json.loads((out / "learner" / "result.json").read_text())
    assert state["status"] == "completed" and state["requested_tools"] == 11 and state["successful_tools"] == 8
    assert [r["run"] for r in state["images_delivered"]] == ["B", "C2"] and state["images_delivered"][0]["request"] == 3
    written = all_text(out)
    assert REASONING_MARK not in written and base64.b64encode(png("waves1-desktop-t0")).decode() not in written
    assert "memory/waves-lesson.md" in (out / "changes.diff").read_text()
    assert "Changes: one memory entry." in (out / "report.md").read_text()


def test_caps_stop_the_session(setup):
    tmp, evolver, params, _ = setup
    material(tmp, 1)
    # Tools: the third requested tool exceeds max_tools=2 and is refused; no further request is made.
    ctx = context(tmp, 1, {**params, "max_tools": 2})
    evolver.client = client = FakeClient([reply(*(call("get_policy") for _ in range(3)))] * 5)
    result = evolver.step(ctx)
    assert len(client.requests) == 1 and result.extra["learner_status"] == "budget_exhausted"
    last = json.loads((ctx.workspace.output / "learner" / "tool-results" / "003.json").read_text())
    assert "Learner tool or time budget exhausted" in last["response"]
    # Requests: two requests, then the cap.
    tmp2 = tmp / "requests"
    tmp2.mkdir()
    material(tmp2, 1)
    ctx = context(tmp2, 1, {**params, "max_requests": 2})
    evolver.client = client = FakeClient([reply(call("get_policy"))] * 5)
    result = evolver.step(ctx)
    assert len(client.requests) == 2 and result.extra["learner_status"] == "budget_exhausted"
    assert result.usage[0].calls == 2
    # Completion tokens: the per-request max_tokens shrinks to what is left, then the session stops.
    tmp3 = tmp / "tokens"
    tmp3.mkdir()
    material(tmp3, 1)
    ctx = context(tmp3, 1, {**params, "max_completion": 50})
    evolver.client = client = FakeClient([reply(call("get_policy"))] * 5, usage=(10, 0, 30))
    evolver.step(ctx)
    assert [r["kwargs"]["max_tokens"] for r in client.requests] == [50, 20]


def test_measurement_round_is_a_no_op(setup):
    tmp, evolver, params, _ = setup
    material(tmp, 6)
    ctx = context(tmp, 6, {**params, "rounds": 5})
    before = all_text(ctx.workspace.adapter)
    evolver.client = client = FakeClient([])
    result = evolver.step(ctx)
    assert (result.status, result.summary, result.usage) == ("done", "measurement round", [])
    assert client.requests == [] and all_text(ctx.workspace.adapter) == before
    assert "measurement round" in (ctx.workspace.output / "report.md").read_text()


def test_cancel_and_api_error(setup):
    tmp, evolver, params, _ = setup
    material(tmp, 1)
    cancel = threading.Event()
    cancel.set()
    ctx = context(tmp, 1, params, cancel=cancel)
    evolver.client = client = FakeClient([reply(call("get_policy"))])
    result = evolver.step(ctx)
    assert result.status == "failed" and client.requests == [] and result.usage == []
    tmp2 = tmp / "error"
    tmp2.mkdir()
    material(tmp2, 1)
    ctx = context(tmp2, 1, params)
    evolver.client = FakeClient([RuntimeError("chat/completions HTTP 500: synthetic")])
    result = evolver.step(ctx)
    assert result.status == "failed" and result.extra["learner_status"] == "api_error"
    assert result.usage == [Usage("fake-glm", calls=1, cost_usd=None)]      # a failed request: cost unknown
    record = json.loads((ctx.workspace.output / "learner" / "calls" / "01.json").read_text())
    assert "HTTP 500" in record["error"]


def test_a_failed_request_is_recorded_without_the_key(setup, monkeypatch):
    tmp, evolver, params, _ = setup
    material(tmp, 1)
    monkeypatch.setenv("FIREWORKS_API_KEY", "SYNTH-KEY-6b1f")
    ctx = context(tmp, 1, params)
    evolver.client = FakeClient([RuntimeError("chat/completions HTTP 401: echo SYNTH-KEY-6b1f")])
    result = evolver.step(ctx)
    out = ctx.workspace.output
    assert result.status == "failed" and "SYNTH-KEY-6b1f" not in all_text(out)
    assert json.loads((out / "learner" / "calls" / "01.json").read_text())["error"] == \
        "RuntimeError: chat/completions HTTP 401: echo [REDACTED]"
    assert "- Status: api_error (RuntimeError: chat/completions HTTP 401: echo [REDACTED])" in \
        (out / "report.md").read_text()


# ---------- byte fidelity of the copied inputs ----------

# The old session's working directory, a path on the machine that ran it, is written as this neutral path both in the
# filled template and in the old files the hashes below were measured on.
OLD_SESSION_ADAPTER = "/old-experiment/evolution/sessions/evolve-exkatk8q/adapter"


def test_default_paths_reach_the_task():
    """cases_file and speedgate are relative to the Evolver's folder and reach examples/tasks/visual_showcase/."""
    _, params, m = load()
    task = EVOLVER_DIR.parents[2] / "tasks" / "visual_showcase"
    assert (EVOLVER_DIR / params["cases_file"]).resolve() == task / "cases.json"
    assert (EVOLVER_DIR / params["speedgate"]).resolve() == task / "speedgate.py"
    for key in ("cases_file", "speedgate"):
        assert m.DEFAULTS[key] == params[key], key


def test_learner_limits_and_transport_are_the_old_ones():
    """The old learner's limits (config.json learner_*, the per-response cap and the file-tool reply cap) and its
    transport; evolver.yaml gives the same values as DEFAULTS."""
    _, params, m = load()
    caps = {"max_seconds": 900, "max_requests": 64, "max_tools": 120, "max_completion": 65536, "max_response": 32768,
            "reply_limit_bytes": 30000}
    transport = {"base_url": "https://api.fireworks.ai/inference/v1",
                 "model": "accounts/fireworks/models/glm-5p3-flash", "api_key_env": "FIREWORKS_API_KEY",
                 "price": {"input": 0.15, "cached_input": 0.03, "output": 0.50}}
    for key, value in {**caps, **transport}.items():
        assert m.DEFAULTS[key] == params[key] == value, key
    assert m.SETTINGS == {"temperature": 0, "top_k": 40, "presence_penalty": 0, "frequency_penalty": 0,
                          "reasoning_effort": 4096}
    assert (m.REQUEST_TIMEOUT, m.MAX_IMAGE_CHARS, m.MAX_BRIEF) == (420, 8_000_000, 6000)


def test_prompt_template_is_the_old_prompt_outside_the_narrowed_goal(setup):
    """Filled with the old session's values, the text before and after the five narrowed GOAL lines is byte-identical
    to the old control/prompt.md (sha256 of those two parts, measured on the old file with its working directory
    written as OLD_SESSION_ADAPTER)."""
    _, _, _, m = setup
    values = {"session": 1, "case": "waves", "runs": "B", "start": "generic MWM W0", "max_brief": 6000,
              "max_seconds": 900, "max_requests": 64, "max_tools": 120, "max_completion": 65536,
              "max_response": 32768, "memory_index": "(empty)", "brief_chars": 137, "notice": "",
              "docket": "Open cases (meta/cases/ — your tracking surface; delete a case when it settles):\n"
                        "(no open cases)", "working_directory": OLD_SESSION_ADAPTER}
    text = m.fill((EVOLVER_DIR / "prompt.md").read_text(), values)
    head = text.split("REVIEW SESSION #1.\n")[0] + "REVIEW SESSION #1.\n"
    tail = "Separate observed behavior" + text.split("Separate observed behavior")[1]
    digest = lambda text: hashlib.sha256(text.encode()).hexdigest()       # noqa: E731
    assert digest(head) == "d3e62b5a8fc7843710b7287be31e0bb2f5e37d1cd6e8aff04975087a326f764c"
    assert digest(tail) == "0cfc117e5bd101cbba6e823f17269aea62b2a0459869acad23706a588171ab4b"
    env = m.fill((EVOLVER_DIR / "environment.md").read_text(), {"working_directory": OLD_SESSION_ADAPTER})
    assert digest(env) == "5bb85b08c22be7ba1aa4ae5ff9fa7df3b9e70a5618678cb8accb508ade5b84eb"


def test_tool_schemas_and_method_are_the_old_bytes(setup):
    _, _, _, m = setup
    tools = (EVOLVER_DIR / "tools.json").read_bytes()
    assert hashlib.sha256(tools).hexdigest() == "fdfc549b4d8b8ae33397dfd669a07c1d91a0b9742accfef8c40acb9ae657f04b"
    assert [t["function"]["name"] for t in json.loads(tools)] == list(m.ALLOWED_TOOLS)
    digest = hashlib.sha256()
    for p in sorted((EVOLVER_DIR / "method").rglob("*")):
        if p.is_file():
            digest.update(p.relative_to(EVOLVER_DIR / "method").as_posix().encode() + b"\0" + p.read_bytes())
    assert digest.hexdigest() == "392ce947adeea6122b1a219ba8347839639e69a426452e3c16c9475a48226698"


def test_session_preparation_rolls_the_journal_and_reports_the_docket(setup):
    tmp, _, _, m = setup
    a = adapter(tmp / "a")
    sessions = "".join(f"## Session {n}\nDigest: lesson {n}\nstill open question {n}\n" for n in range(1, 8))
    (a / "meta" / "journal.md").write_text("# Journal\n\n## Older sessions (digests)\n" + sessions)
    changed, notice = m.roll_journal(a)
    assert changed and "## Session 1\n" not in (a / "meta" / "journal.md").read_text()
    assert "- Session 2: lesson 2" in (a / "meta" / "journal.md").read_text()
    assert notice.startswith("Roll notice: archived session entries 1, 2 still mention open/unresolved work")
    (a / "meta" / "cases" / "01-haze.md").write_text("x" * 3500)
    (a / "meta" / "cases" / "bad.md").write_text("x")
    assert m.cap_cases(a) == ["meta/cases/01-haze.md"]
    status = m.docket_status(a)
    assert "meta/cases/01-haze.md: " in status and "TRUNCATED" in status and "IGNORED meta/cases/bad.md" in status
    (a / "memory" / "waves.md").write_text("brief: crest shading\nlast_confirmed_t: 1\n")
    assert m.memory_index(a, 3) == "[waves] t=1 (unverified for 2 ticks) · crest shading"
