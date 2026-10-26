"""Continual Harness: the continual-harness template (the harness state in Codex's layout), the Refiner's reading of
a step (Codex event streams, failure signatures, the agent's notes) and its four passes applied to an adapter with a
scripted model (no model, no Docker, no network)."""
import json
import os
from pathlib import Path
import shutil
import tempfile

import pytest

from hermit.algorithms.evolvers.continual_harness import evolver as E
from hermit.evolver.base import Progress, StepContext, Workspace, Access
from hermit.spec import check_spec

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "hermit" / "algorithms" / "templates" / "continual_harness"
AGENT_DIR = ROOT / "examples" / "officeqa" / "utils" / "agent"


@pytest.fixture
def tmp_path():
    """A directory outside /tmp: the template check runs under bubblewrap with an empty tmpfs at /tmp."""
    base = Path.home() / ".cache" / "hermit-tests"
    base.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(dir=base))
    yield path
    if not os.environ.get("KEEP_TEST_DIRS"):
        shutil.rmtree(path, ignore_errors=True)


def seed(tmp: Path) -> Path:
    adapter = tmp / "adapter"
    shutil.copytree(TEMPLATE, adapter, ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("AGENTS.md", "ADAPTER.md"):
        (adapter / name).write_text("# OfficeQA Skill\n\nNarrow to the right file first.\n")
    return adapter


EVENTS = [
    {"type": "thread.started", "thread_id": "t"},
    {"type": "item.completed", "item": {"id": "1", "type": "reasoning", "text": "Find the 1944 bulletin."}},
    {"type": "item.completed", "item": {"id": "2", "type": "command_execution", "command": "rg -n Receipts docs",
                                        "aggregated_output": "docs/root_1/transformed/x.txt:3: Receipts 44,463",
                                        "exit_code": "0", "status": "completed"}},
    {"type": "item.completed", "item": {"id": "3", "type": "command_execution",
                                        "command": "python3 .agents/skills/sum/scripts/sum.py",
                                        "aggregated_output": "Traceback: NameError", "exit_code": "1",
                                        "status": "failed"}},
    {"type": "item.completed", "item": {"id": "4", "type": "error", "message": "model metadata unavailable"}},
    {"type": "item.completed", "item": {"id": "5", "type": "agent_message", "text": "<answer>44,463</answer>"}},
    {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}},
]


def material(tmp: Path, records: list[dict], notes: dict[str, str] | None = None) -> Path:
    """A learning material tree with one current step: records as the Trainer writes them, a Codex stream and,
    per task, the notes the session left."""
    root = tmp / "material"
    step = root / "step_0001"
    (step / "tasks").mkdir(parents=True)
    for r in records:
        traj = step / "trajectories" / r["id"]
        traj.mkdir(parents=True)
        (traj / "agent-codex.jsonl").write_text("".join(json.dumps(e) + "\n" for e in EVENTS))
        if notes and r["id"] in notes:
            (traj / "memory").mkdir()
            (traj / "memory" / "NOTES.md").write_text(notes[r["id"]])
        (step / "tasks" / f"{r['id']}.json").write_text(json.dumps({**r, "trajectory": f"trajectories/{r['id']}"}))
    (root / "current").symlink_to("step_0001")
    return root


def context(tmp: Path, adapter: Path, llm, params: dict | None = None) -> StepContext:
    ws = Workspace(adapter=adapter, learning_material=tmp / "material", task_info=tmp / "task-info",
                   history=tmp / "history", state=tmp / "state", scratch=tmp / "scratch", output=tmp / "output")
    for d in (ws.state, ws.scratch, ws.output, ws.task_info):
        d.mkdir(parents=True, exist_ok=True)
    (ws.task_info / "README.md").write_text("Questions about U.S. Treasury Bulletins.\n")

    def check_spec_tool(path: str) -> dict:
        result = check_spec(Path(path), TEMPLATE, deploy=False)
        return {"passed": result.passed, "issues": [i.__dict__ for i in result.issues]}

    tools = {"llm": llm, "check_spec": check_spec_tool}
    return StepContext(workspace=ws, access=Access(), tools=tools,
                       params={"feedback": True, "trace_chars": 4000, "components": E.PASSES, **(params or {})},
                       progress=Progress(1, 1, False))


def scripted(replies: dict[str, object]):
    """A model that answers each pass by the heading in its prompt; records the prompts it saw."""
    seen = []

    def llm(messages, **_):
        prompt = messages[0]["content"]
        seen.append(prompt)
        for key, reply in replies.items():
            if key in prompt:
                return {"content": reply if isinstance(reply, str) else json.dumps(reply)}
        return {"content": "{}"}
    llm.seen = seen
    return llm


RECORD = {"id": "UID0003", "status": "ok", "error": None, "output": {"answer": "44,463", "fail_reason": ""},
          "judge": {"score": 0.0, "feedback": "predicted '44,463' but expected '44,463 million'"},
          "cost_usd": None, "wall_s": 20.0, "api_speed": None, "tokens": 15, "messages": None,
          "pipeline_extra": {"harness": "codex", "exit_code": 0, "session_timed_out": False}}


def test_template_root_and_seed_pass_the_check(tmp_path):
    assert check_spec(TEMPLATE, TEMPLATE, deploy=True).passed
    assert check_spec(seed(tmp_path), TEMPLATE, deploy=True).passed


def test_check_rejects_off_surface_files_and_bad_entries(tmp_path):
    adapter = seed(tmp_path)
    (adapter / "policy.md").write_text("stray\n")
    (adapter / ".agents" / "skills" / "sum").mkdir(parents=True)
    (adapter / ".agents" / "skills" / "sum" / "SKILL.md").write_text("no frontmatter\n")
    (adapter / ".agents" / "skills" / "sum" / "scripts").mkdir()
    (adapter / ".agents" / "skills" / "sum" / "scripts" / "sum.py").write_text("def (:\n")
    problems = [i.problem for i in check_spec(adapter, TEMPLATE, deploy=False).issues]
    assert any("off the harness surface" in p for p in problems)
    assert any("description" in p for p in problems)
    assert any("does not compile" in p for p in problems)
    (adapter / "memory" / "NOTES.md").unlink()
    problems = [i.problem for i in check_spec(adapter, TEMPLATE, deploy=False).issues]
    assert any("memory/NOTES.md is missing" in p for p in problems)


def test_codex_trajectory_and_failure_signatures(tmp_path):
    root = material(tmp_path, [RECORD])
    record, traj = E.read_records(root)[0]
    text = E.codex_trajectory(E.codex_events(traj / "agent-codex.jsonl"))
    assert "[reasoning] Find the 1944 bulletin." in text
    assert "[command exit 1] python3 .agents/skills/sum/scripts/sum.py" in text
    assert "[assistant] <answer>44,463</answer>" in text
    sig = E.failure_signatures(record, traj, feedback=True)
    assert any(s.startswith("1 command(s) failed") and "sum.py" in s for s in sig)
    assert any("1 error event(s)" in s for s in sig)
    assert any(s.startswith("judged wrong: predicted") for s in sig)
    assert not any("judged" in s for s in E.failure_signatures(record, traj, feedback=False))
    timed = {**RECORD, "output": {"answer": "", "fail_reason": "session timed out"},
             "pipeline_extra": {"session_timed_out": True}}
    sig = E.failure_signatures(timed, None, feedback=False)
    assert "the session hit its wall clock" in sig and any(s.startswith("no answer delivered") for s in sig)


def test_notes_merge_and_render():
    current = E.render_notes([("Layout", "Bulletins are under docs/root_1/transformed."), ("Units", "Millions.")])
    incoming = "# Notes\n\n## units\n\nFigures are in millions of dollars.\n\n## Pitfall\n\nFiscal years end in June.\n"
    merged, changed = E.merge_notes(current, incoming)
    assert changed == 2
    assert E.sections(merged) == [("Layout", "Bulletins are under docs/root_1/transformed."),
                                  ("Units", "Figures are in millions of dollars."),
                                  ("Pitfall", "Fiscal years end in June.")]
    assert E.merge_notes(merged, incoming)[1] == 0
    assert E.PLACEHOLDER in E.render_notes([])
    assert E.merge_notes(E.render_notes([]), E.render_notes([]))[1] == 0


def test_notes_stay_within_the_cap_oldest_first():
    entries = [(f"Topic {i}", "x" * 1000) for i in range(10)]
    text = E.fit_notes(entries, limit=3500)
    kept = [t for t, _ in E.sections(text)]
    assert kept == ["Topic 7", "Topic 8", "Topic 9"] and len(text.encode()) <= 3500
    merged, changed = E.merge_notes(E.render_notes(entries), "## New\n\nfresh\n", limit=3500)
    assert changed == 1 and [t for t, _ in E.sections(merged)][-1] == "New" and len(merged.encode()) <= 3500
    one = E.fit_notes([("Only", "y" * 5000)], limit=1000)
    assert len(one.encode()) <= 1000 and E.sections(one)[0][0] == "Only"


def test_step_runs_four_passes_and_folds_the_agents_notes(tmp_path):
    adapter = seed(tmp_path)
    material(tmp_path, [RECORD, {**RECORD, "id": "UID0004"}],
             notes={"UID0004": "# Notes\n\n## Layout\n\nBulletins are under docs/root_1/transformed.\n"})
    llm = scripted({
        "rewrites the BASE POLICY": "Here it is:\n```markdown\n# OfficeQA Skill\n\nNarrow to the right file first. "
                                    "State units as the question does.\n```",
        "maintains the SUB-AGENTS": {"analysis": "no delegation needed", "create": [
            {"name": "Unit Checker", "description": "checks a figure's unit against the question",
             "instructions": "Compare the unit of the figure with the unit the question asks for."}]},
        "maintains the SKILLS": {"analysis": "the sum helper fails", "add": [
            {"name": "sum", "description": "sums a column of figures", "body": "Run the helper on the figures.",
             "scripts": {"sum.py": "import sys\nprint(sum(float(x) for x in sys.argv[1:]))\n"}}]},
        "memory curator": {"analysis": "one note from the agent", "add": [
            {"title": "Units", "content": "Treasury Bulletin tables are in millions of dollars."}]},
    })
    ctx = context(tmp_path, adapter, llm)
    result = E.ContinualHarnessEvolver().step(ctx)
    assert result.status == "done", result.summary
    assert "policy: AGENTS.md" in result.summary and "+unit_checker" in result.summary and "+sum" in result.summary
    assert "+Units" in result.summary and "notes from UID0004 (1)" in result.summary
    assert "State units" in (adapter / "AGENTS.md").read_text()
    assert (adapter / "ADAPTER.md").read_text() == (adapter / "AGENTS.md").read_text()
    assert (adapter / "sub_agents" / "unit_checker" / "AGENT.md").read_text().startswith('---\ndescription: "checks')
    assert (adapter / ".agents" / "skills" / "sum" / "scripts" / "sum.py").is_file()
    notes = E.sections((adapter / "memory" / "NOTES.md").read_text())
    assert [t for t, _ in notes] == ["Layout", "Units"]
    assert check_spec(adapter, TEMPLATE, deploy=False).passed
    state = json.loads((tmp_path / "state" / "continual_harness.json").read_text())["steps"][0]
    assert state["signals"] == 6 and set(state["passes"]) == set(E.PASSES)
    assert (tmp_path / "output" / "report.md").read_text().startswith("# Continual Harness, step 1")
    # every pass saw the task, the rollouts and the judge's verdict
    assert all("=== RECENT ROLLOUTS (2) ===" in p and "judged wrong" in p for p in llm.seen)
    assert "Treasury Bulletins" in llm.seen[0]


def test_a_pass_that_breaks_the_check_is_dropped_alone(tmp_path):
    adapter = seed(tmp_path)
    material(tmp_path, [RECORD])
    llm = scripted({
        "rewrites the BASE POLICY": "```\n# OfficeQA Skill\n\nCheck units.\n```",
        "maintains the SKILLS": {"add": [{"name": "broken", "description": "a helper that does not compile",
                                          "body": "Run it.", "scripts": {"run.py": "def (:\n"}}]},
    })
    result = E.ContinualHarnessEvolver().step(context(tmp_path, adapter, llm))
    assert result.status == "done"
    assert "policy: AGENTS.md" in result.summary and "dropped skills" in result.summary
    assert not (adapter / ".agents" / "skills" / "broken").exists()
    assert "Check units." in (adapter / "AGENTS.md").read_text()
    assert check_spec(adapter, TEMPLATE, deploy=False).passed
    assert result.extra["passes"]["skills"]["rejected"]


def test_policy_over_the_cap_is_asked_to_compress_then_dropped(tmp_path):
    adapter = seed(tmp_path)
    material(tmp_path, [RECORD])
    calls = []

    def llm(messages, **_):
        calls.append(messages[-1]["content"])
        return {"content": "```\n" + "x" * 9000 + "\n```"}
    result = E.ContinualHarnessEvolver().step(context(tmp_path, adapter, llm, {"components": ["policy"]}))
    assert result.status == "done" and "unchanged" in result.summary
    assert "over the cap" in calls[-1]
    assert (adapter / "AGENTS.md").read_text().startswith("# OfficeQA Skill")


def test_feedback_off_shows_no_verdict(tmp_path):
    adapter = seed(tmp_path)
    material(tmp_path, [RECORD])
    llm = scripted({})
    E.ContinualHarnessEvolver().step(context(tmp_path, adapter, llm, {"feedback": False, "components": ["policy"]}))
    assert "judged" not in llm.seen[0] and "expected" not in llm.seen[0]


def test_codex_agent_deploys_and_collects_the_memory(tmp_path):
    import sys
    sys.path.insert(0, str(AGENT_DIR))
    import codex_agent as A
    adapter = seed(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    deployed = A.deploy_harness(adapter, work)
    assert deployed["memory"] and (work / "memory" / "NOTES.md").is_file()
    assert "memory/NOTES.md" in A.MEMORY_NOTE and "memory" in A.ROOT_ENTRIES
    assert not A.deploy_harness(ROOT / "hermit" / "algorithms" / "templates" / "harness2_codex", tmp_path / "w2")["memory"] \
        if (tmp_path / "w2").mkdir() is None else True


def test_frontmatter_round_trip_survives_colons_and_quotes(tmp_path):
    import sys
    sys.path.insert(0, str(AGENT_DIR))
    import codex_agent as A
    description = 'Delegate when figures (e.g., "tendered" vs received: gross vs net) could match'
    text = E.with_frontmatter(description, "Compare them.")
    assert E.frontmatter(text) == ({"description": description}, "Compare them.\n")
    assert A.frontmatter(text)[0] == {"description": description}
    adapter = seed(tmp_path)
    d = adapter / ".agents" / "skills" / "figures"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(text)
    assert check_spec(adapter, TEMPLATE, deploy=False).passed


def test_json_and_block_parsing():
    assert E._json_object('Sure:\n```json\n{"a": 1}\n```') == {"a": 1}
    assert E._json_object('{"a": [1, 2]} trailing') == {"a": [1, 2]}
    assert E._json_object("no json here") is None
    assert E._last_block("text ```python\nprint(1)\n``` more") == "print(1)\n"
    assert E.slug("Unit Checker!") == "unit_checker" and E.slug("") .startswith("entry_")
