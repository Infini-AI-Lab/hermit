"""Evo-Harness: the parsers and curations against upstream's behaviour, the `evo-harness` template, the Evolver's step
with a scripted model (reflect only on failures, per-topic and general compilation, budgets, rollback), and the whole
loop through the Trainer on a toy task (no model, no network)."""
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

import pytest
import yaml

from hermit.agent.acting import workspace
from hermit.agent.evolver.base import Access, Progress, StepContext, Usage, Workspace
from hermit.agent.evolver.methods.evo_harness import evolver as E
from hermit.agent.evolver.methods.evo_harness.prompts import CURATOR, GENERAL_CURATOR, PROPOSE_FAIL
from hermit.config import Config
from hermit.evaluate import evaluate
from hermit.spec import check_spec
from hermit.trainer.trainer import Trainer
from fakes import agent_of

ROOT = Path(__file__).resolve().parents[3]
TEMPLATE = ROOT / "hermit" / "adapter" / "templates" / "evo_harness"
BASE = "# OfficeQA skill\n\nFind the table first.\n"
BLOCK = f"{E.BEGIN}\n\n{E.END}\n"


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
    (adapter / "ADAPTER.md").write_text(BASE + "\n" + BLOCK)
    return adapter


def problems(adapter: Path) -> list[str]:
    return [i.problem for i in check_spec(adapter, TEMPLATE, deploy=False).issues]


def rerender(adapter: Path) -> None:
    f = adapter / "ADAPTER.md"
    f.write_text(E.with_block(f.read_text(), E.render_block(adapter)))


# ---------- the prompts ----------

def test_the_prompts_are_upstreams_and_format():
    assert "Propose a GENERALIZABLE skill" in PROPOSE_FAIL and "DESCRIPTION: one sentence saying WHEN" in PROPOSE_FAIL
    assert "terminal" not in CURATOR + GENERAL_CURATOR
    assert PROPOSE_FAIL.format(eval_result="e", judge_verdict="j", trajectory_signals="s", compressed_trajectory="c",
                               existing_skills_section="x")
    assert CURATOR.format(topic="t", n_skills=0, max_skills=5, existing_skills_list="", proposals_list="")
    assert GENERAL_CURATOR.format(analysis_heading="h", n_tasks=1, task_summaries="", n_general=0, max_general=5,
                                  general_skills_list="")


# ---------- the template ----------

def test_template_root_and_a_seed_pass_the_check(tmp_path):
    assert check_spec(TEMPLATE, TEMPLATE, deploy=True).passed
    assert check_spec(seed(tmp_path), TEMPLATE, deploy=True).passed


def test_a_harness_with_skills_passes_and_deploys(tmp_path):
    adapter = seed(tmp_path)
    E.write_skill(adapter / "general" / "check-form", "check-form", "For answers: mind the form", "- bare value")
    E.write_skill(adapter / "skills" / "tables--read-units", "tables--read-units", "When: a table has units", "- units")
    rerender(adapter)
    assert check_spec(adapter, TEMPLATE, deploy=True).passed
    text = (adapter / "ADAPTER.md").read_text()
    assert text.startswith(BASE) and "### check-form\n- bare value" in text
    assert "`skills/tables--read-units/SKILL.md`: When: a table has units" in text


@pytest.mark.parametrize("mutate, expected", [
    (lambda a: (a / "ADAPTER.md").write_text(BASE), "exactly once"),
    (lambda a: (a / "general" / "check-form" / "SKILL.md").write_text("no frontmatter"), "frontmatter"),
    (lambda a: (a / "general" / "check-form" / "SKILL.md").write_text(
        '---\nname: other\ndescription: "d"\n---\n\nb\n'), "not its directory"),
    (lambda a: (a / "general" / "check-form" / "SKILL.md").write_text(
        "---\nname: check-form\ndescription: a: b\n---\n\nb\n"), "JSON-quoted"),
    (lambda a: E.write_skill(a / "general" / "new-skill", "new-skill", "d", "b"), "does not name"),
    (lambda a: E.write_skill(a / "skills" / "no-topic", "no-topic", "d", "b"), "does not fit"),
    (lambda a: E.write_skill(a / "skills" / f"t--{'n' * 62}", f"t--{'n' * 62}", "d", "b"), "longer than 64"),
])
def test_check_rejects_a_broken_harness(tmp_path, mutate, expected):
    adapter = seed(tmp_path)
    E.write_skill(adapter / "general" / "check-form", "check-form", "d", "b")
    rerender(adapter)
    assert problems(adapter) == []
    mutate(adapter)
    assert any(expected in p for p in problems(adapter))


def test_a_description_with_a_colon_stays_parseable_yaml(tmp_path):
    E.write_skill(tmp_path / "s", "s", 'Use when: "units" differ', "- body\n---\n- rule")
    front = (tmp_path / "s" / "SKILL.md").read_text().split("---\n")[1]
    assert yaml.safe_load(front)["description"] == 'Use when: "units" differ'
    assert E.read_skill(tmp_path / "s") == ('Use when: "units" differ', "- body\n---\n- rule")


# ---------- the parsers, against upstream's behaviour ----------

REPLY = """ISSUE: wrong form
DETAIL: kept the unit

TOPIC: Table Reading!
ACTION: NEW
NAME: Strip Units
DESCRIPTION: For answers: strip units
CONTENT:
## When to use
- always
"""


def test_a_reply_parses_into_a_proposal_and_one_without_content_is_none():
    p = E.parse_proposal(REPLY, "UID1")
    assert (p["topic"], p["name"], p["action"]) == ("table-reading", "strip-units", "NEW")
    assert p["description"] == "For answers: strip units" and p["content"] == "## When to use\n- always"
    assert p["analysis"].startswith("ISSUE: wrong form")
    assert E.parse_proposal("ACTION: NONE", "UID1") is None
    assert E.parse_proposal("TOPIC: x\nACTION: NEW\nNAME: y\n", "UID1") is None         # no content
    enhance = E.parse_proposal("ACTION: ENHANCE\nTARGET: Old-One\nCONTENT:\n- x", "UID1")
    assert enhance["name"] == "old-one"
    assert E.parse_proposal("ACTION: NEW\nCONTENT:\n- x", "UID0002")["name"] == "skill-uid0002"


def proposal(name, topic="tables", content="- body", action="NEW"):
    return {"source_task": "UID1", "topic": topic, "action": action, "target": "", "name": name,
            "description": f"For {name}", "content": content}


def test_topic_curation_accepts_merges_skips_under_the_budget(tmp_path):
    adapter = seed(tmp_path)
    E.write_skill(adapter / "skills" / "tables--old", "tables--old", "For old", "- old")
    items = [proposal("a"), proposal("b"), proposal("c"), proposal("d")]
    reply = "ACCEPT: a\nACCEPT: b\nMERGE: c INTO old\nNEW_CONTENT:\n- merged\nSKIP: d\nREASON: no"
    stats = E.apply_topic_curation(reply, items, adapter, "tables", max_skills=2)
    assert stats == {"added": 1, "merged": 1, "skipped": 1}                            # b: the budget is full
    assert (adapter / "skills" / "tables--a").is_dir() and not (adapter / "skills" / "tables--b").exists()
    assert E.read_skill(adapter / "skills" / "tables--old") == ("For old", "- merged")


def test_a_merge_without_new_content_or_target_changes_nothing(tmp_path):
    adapter = seed(tmp_path)
    E.write_skill(adapter / "skills" / "tables--old", "tables--old", "For old", "- old")
    stats = E.apply_topic_curation("MERGE: a INTO old\nMERGE: a INTO ", [proposal("a")], adapter, "tables", 5)
    assert stats["merged"] == 0 and E.read_skill(adapter / "skills" / "tables--old")[1] == "- old"


def test_general_curation_adds_updates_and_deletes_under_the_budget(tmp_path):
    adapter = seed(tmp_path)
    E.write_skill(adapter / "general" / "keep", "keep", "For keep", "- old")
    E.write_skill(adapter / "general" / "drop", "drop", "For drop", "- old")
    reply = ("UPDATE_GENERAL: keep\nNEW_CONTENT:\n- new\nNo other changes\n"
             "DELETE_GENERAL: drop\nREASON: stale\n"
             "NEW_GENERAL: first-one\nDESCRIPTION: When: first\nCONTENT:\n## Strategy\n- a\n"
             "NEW_GENERAL: second-one\nDESCRIPTION: second\nCONTENT:\n- b\n")
    stats = E.apply_general_curation(reply, adapter, max_general=2)
    assert stats == {"added": 1, "updated": 1, "deleted": 1}
    assert sorted(d.name for d in E.skill_dirs(adapter / "general")) == ["first-one", "keep"]
    assert E.read_skill(adapter / "general" / "keep") == ("For keep", "- new")          # commentary left out


def test_an_update_of_a_general_skill_new_in_the_same_reply_is_ignored_as_upstream(tmp_path):
    """The update would write the skill again without the description the reply gave it."""
    adapter = seed(tmp_path)
    reply = "NEW_GENERAL: fresh\nDESCRIPTION: For fresh\nCONTENT:\n- a\nUPDATE_GENERAL: fresh\nNEW_CONTENT:\n- b\n"
    assert E.apply_general_curation(reply, adapter, max_general=2) == {"added": 1, "updated": 0, "deleted": 0}
    assert E.read_skill(adapter / "general" / "fresh") == ("For fresh", "- a")


def test_signals_are_counted_from_the_frameworks_steps():
    from hermit.agent.base.trajectory import Step
    steps = [Step("message", "hi"), Step("command", command="rg x", exit_code=0, output="ok")] + [
        Step("command", command="cat y", exit_code=1, output="timed out")] * 3
    s = E.signals(steps)
    assert (s["n_turns"], s["n_tool_calls"], s["n_errors"], s["n_timeouts"]) == (1, 4, 3, 3)
    assert s["repeated_commands"] == ["cat y"]
    assert "Submitted: True" in E.format_signals(s, True) and "Repeated commands: cat y" in E.format_signals(s, True)


@pytest.mark.parametrize("level, shown, hidden", [("minimal", "FAILED", "expected"), ("standard", "expected '2'", None)])
def test_each_feedback_level_shows_only_its_share_of_the_judges_feedback(level, shown, hidden):
    text = E.eval_text({"status": "ok", "judge": {"feedback": "predicted '1' but expected '2'"}}, level)
    assert shown in text and (hidden is None or hidden not in text)


# ---------- the step, with a scripted model ----------

def material(tmp: Path, records: list[dict]) -> Path:
    root = tmp / "material"
    step = root / "step_0001"
    (step / "tasks").mkdir(parents=True)
    for r in records:
        (step / "tasks" / f"{r['id'].replace('/', '_')}.json").write_text(json.dumps({"trajectory": None, **r}))
    (step / "summary.json").write_text(json.dumps({"tasks": [r["id"] for r in records]}))
    (root / "current").symlink_to("step_0001")
    return root


def record(tid: str, score: float) -> dict:
    return {"id": tid, "input": {"id": tid, "question": f"What were receipts in {tid}?"},
            "output": {"answer": "44"}, "status": "ok", "error": None,
            "judge": {"score": score, "feedback": "correct" if score else "predicted '44' but expected '45'"},
            "messages": [{"role": "system", "content": "SKILL"}, {"role": "user", "content": "task"},
                         {"role": "assistant", "content": "Looking. <answer>44</answer>"}]}


class Learner:
    """Reflection: one proposal per task; topic curator: accept all; general curator: one new skill."""

    def __init__(self, general="NEW_GENERAL: mind-the-form\nDESCRIPTION: For answers\nCONTENT:\n- bare value\n"):
        self.prompts = []
        self.general = general

    def __call__(self, messages=None, **_):
        text = messages[0]["content"]
        self.prompts.append(text)
        if "meta-learning curator" in text:
            return {"content": self.general}
        if "skill curator" in text:
            names = re.findall(r"### \[\w+\] (\S+)", text)
            return {"content": "\n".join(f"ACCEPT: {n}" for n in names)}
        tid = re.search(r"receipts in (\w+)\?", text).group(1).lower()
        return {"content": f"ISSUE: i\nDETAIL: d\nTOPIC: tables\nACTION: NEW\nNAME: skill-{tid}\n"
                           f"DESCRIPTION: For {tid}: a\nCONTENT:\n- lesson {tid}\n"}


def context(tmp: Path, adapter: Path, llm, params=None, tools=None) -> StepContext:
    for name in ("state", "scratch", "output", "history", "task_info"):
        (tmp / name).mkdir(exist_ok=True)
    ws = Workspace(adapter=adapter, learning_material=tmp / "material", task_info=tmp / "task_info",
                   history=tmp / "history", state=tmp / "state", scratch=tmp / "scratch", output=tmp / "output")
    def check_spec_tool(path):
        result = check_spec(Path(path), TEMPLATE, deploy=False)
        return {"passed": result.passed, "issues": [i.__dict__ for i in result.issues]}
    return StepContext(workspace=ws, access=Access(), tools={"check_spec": check_spec_tool, **(tools or {})},
                       params={"max_general_skills": 5, "max_skills_per_topic": 5, "workers": 2,
                               "feedback_level": "standard", **(params or {})},
                       agent=agent_of(llm), progress=Progress(epoch=1, step=1, is_epoch_end=False))


def test_only_failures_are_reflected_on_and_the_harness_is_compiled(tmp_path):
    adapter = seed(tmp_path)
    material(tmp_path, [record("UID1", 1.0), record("UID2", 0.0), record("UID3", 0.0)])
    llm = Learner()
    result = E.EvoHarnessEvolver().step(context(tmp_path, adapter, llm))
    assert result.status == "done"
    reflections = [p for p in llm.prompts if "You attempted the task below" in p]
    assert len(reflections) == 2 and not any("UID1?" in p for p in reflections)
    assert all("Eval: predicted '44' but expected '45'" in p and "Looking." in p for p in reflections)
    assert "No judge assessment available." in reflections[0]
    text = (adapter / "ADAPTER.md").read_text()
    assert text.startswith(BASE) and "### mind-the-form\n- bare value" in text
    assert "`skills/tables--skill-uid2/SKILL.md`: For uid2: a" in text
    assert problems(adapter) == []
    state = json.loads((tmp_path / "state" / "evo_harness.json").read_text())["steps"][0]
    assert (state["tasks"], state["failed"], state["general_skills"], state["topic_skills"]) == (3, 2, 1, 2)
    assert state["topics"] == {"tables": {"added": 2, "merged": 0, "skipped": 0}}
    assert (tmp_path / "output" / "report.md").is_file()
    assert (tmp_path / "output" / "calls" / "curate.general.reply.md").is_file()


def test_the_next_step_sees_the_harness(tmp_path):
    adapter = seed(tmp_path)
    material(tmp_path, [record("UID1", 0.0), record("UID2", 0.0)])
    E.EvoHarnessEvolver().step(context(tmp_path, adapter, Learner()))
    llm = Learner()
    E.EvoHarnessEvolver().step(context(tmp_path, adapter, llm))
    assert any("- **skill-uid1** [tables]: For uid1: a" in p and "- **mind-the-form** [general]" in p
               for p in llm.prompts)
    assert any("(2/5 slots used)" in p for p in llm.prompts)


def test_one_failure_runs_no_general_curator_and_a_pass_runs_nothing(tmp_path):
    adapter = seed(tmp_path)
    material(tmp_path, [record("UID1", 0.0)])
    llm = Learner()
    E.EvoHarnessEvolver().step(context(tmp_path, adapter, llm))
    assert not any("meta-learning curator" in p for p in llm.prompts) and len(llm.prompts) == 2
    shutil.rmtree(tmp_path / "material")
    material(tmp_path, [record("UID1", 1.0)])
    llm = Learner()
    assert E.EvoHarnessEvolver().step(context(tmp_path, adapter, llm)).status == "done"
    assert llm.prompts == []


def test_zero_budgets_turn_the_levels_off(tmp_path):
    adapter = seed(tmp_path)
    material(tmp_path, [record("UID1", 0.0), record("UID2", 0.0)])
    llm = Learner()
    E.EvoHarnessEvolver().step(context(tmp_path, adapter, llm, {"max_general_skills": 0, "max_skills_per_topic": 0}))
    assert not any("curator" in p.split("\n")[0] for p in llm.prompts)
    assert E.skill_dirs(adapter / "general") == [] and E.skill_dirs(adapter / "skills") == []


def test_a_failed_learner_call_is_skipped(tmp_path):
    adapter = seed(tmp_path)
    material(tmp_path, [record("UID1", 0.0), record("UID2", 0.0)])

    def broken(messages=None, **_):
        raise RuntimeError("no reply")
    result = E.EvoHarnessEvolver().step(context(tmp_path, adapter, broken))
    assert result.status == "done" and "learner call(s) failed" in result.summary
    assert (tmp_path / "output" / "calls" / "UID1.reflect.error.txt").is_file()


def test_a_long_topic_and_name_become_a_skill_codex_loads(tmp_path):
    """Codex skips a skill whose name passes 64 characters and says so only in its log; two of a measured harness's
    17 skills were lost that way."""
    adapter = seed(tmp_path)
    long = "historical-bibliographic-research-across-archives"
    reply = f"TOPIC: {long}\nACTION: NEW\nNAME: cross-source-publication-date-verification-of-editions\nCONTENT:\n- x"
    proposal = E.parse_proposal(reply, "UID1")
    assert len(proposal["topic"]) <= E.MAX_TOPIC and not proposal["topic"].endswith("-")
    E.apply_topic_curation(f"ACCEPT: {proposal['name']}", [proposal], adapter, proposal["topic"], 5)
    (skill,) = E.skill_dirs(adapter / "skills")
    assert len(skill.name) <= 64 and skill.name.startswith("historical-bibliographic--cross-source")
    rerender(adapter)
    assert problems(adapter) == []


def test_a_task_id_with_a_path_separator_keeps_its_records(tmp_path):
    adapter = seed(tmp_path)
    material(tmp_path, [record("group/UID1", 0.0)])
    result = E.EvoHarnessEvolver().step(context(tmp_path, adapter, lambda messages=None, **_: {"content": "ACTION: NONE"}))
    assert result.status == "done"
    assert (tmp_path / "output" / "calls" / "group_UID1.reflect.reply.md").read_text() == "ACTION: NONE"


def test_a_reflection_reads_the_codex_log_of_a_record_without_messages(tmp_path):
    """Codex records carry `messages: None` and a trajectory directory. The events are the shape of Codex 0.164.0's
    `agent-codex.jsonl` (`codex exec --json`), with the content replaced."""
    adapter = seed(tmp_path)
    run = {**record("UID1", 0.0), "messages": None, "trajectory": "trajectories/UID1"}
    material(tmp_path, [run])
    log = tmp_path / "material" / "step_0001" / "trajectories" / "UID1"
    log.mkdir(parents=True)
    command = {"id": "item_1", "type": "command_execution", "command": "/bin/bash -lc 'grep receipts task.md'",
               "aggregated_output": "receipts 44\n", "exit_code": 0, "status": "completed"}
    events = [{"type": "thread.started", "thread_id": "t"}, {"type": "turn.started"},
              {"type": "item.started", "item": {**command, "status": "in_progress", "exit_code": None}},
              {"type": "item.completed", "item": command},
              {"type": "item.completed", "item": {"id": "item_2", "type": "agent_message", "text": "<answer>44</answer>"}},
              {"type": "turn.completed", "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1}}]
    (log / "agent-codex.jsonl").write_text("\n".join(json.dumps(e) for e in events))
    llm = Learner()
    assert E.EvoHarnessEvolver().step(context(tmp_path, adapter, llm)).status == "done"
    assert "grep receipts task.md" in llm.prompts[0] and "<answer>44</answer>" in llm.prompts[0]
    assert "Tool calls: 1" in llm.prompts[0]


def test_general_skills_reach_a_deployment_as_text_and_topic_skills_as_files(tmp_path):
    adapter = seed(tmp_path)
    E.write_skill(adapter / "general" / "check-form", "check-form", "For answers", "- bare value")
    E.write_skill(adapter / "skills" / "tables--read-units", "tables--read-units", "For tables", "- units")
    rerender(adapter)
    deployed = tmp_path / "workspace"
    workspace._deploy({"id": "t"}, adapter, deployed, "codex", {})
    assert not list(deployed.rglob("general"))
    assert list(deployed.rglob("tables--read-units/SKILL.md"))
    assert "- bare value" in (deployed / "AGENTS.md").read_text()


def test_a_harness_failing_the_check_is_rolled_back(tmp_path):
    adapter = seed(tmp_path)
    E.write_skill(adapter / "general" / "keep", "keep", "For keep", "- old")
    rerender(adapter)
    before = (adapter / "ADAPTER.md").read_text()
    material(tmp_path, [record("UID1", 0.0), record("UID2", 0.0)])
    ctx = context(tmp_path, adapter, Learner())
    tools = {**ctx.tools, "check_spec": lambda path: {"passed": False, "issues": [{"problem": "broken"}]}}
    ctx = StepContext(**{**ctx.__dict__, "tools": tools})
    result = E.EvoHarnessEvolver().step(ctx)
    assert result.status == "failed" and "rolled back" in result.summary
    assert (adapter / "ADAPTER.md").read_text() == before
    assert [d.name for d in E.skill_dirs(adapter / "general")] == ["keep"] and E.skill_dirs(adapter / "skills") == []


def test_a_hard_budget_is_not_swallowed(tmp_path):
    from hermit.trainer.budget import BudgetExceeded
    adapter = seed(tmp_path)
    material(tmp_path, [record("UID1", 0.0)])

    def exhausted(messages=None, **_):
        raise BudgetExceeded("usd 3 reached")
    with pytest.raises(BudgetExceeded):
        E.EvoHarnessEvolver().step(context(tmp_path, adapter, exhausted))


def test_a_step_fails_without_the_block_and_bootstrap_adds_it_once(tmp_path):
    adapter = seed(tmp_path)
    (adapter / "ADAPTER.md").write_text(BASE)
    material(tmp_path, [record("UID1", 1.0)])
    ctx = context(tmp_path, adapter, Learner())
    assert E.EvoHarnessEvolver().step(ctx).status == "failed"                          # no block yet
    assert E.EvoHarnessEvolver().bootstrap(ctx).status == "done"
    assert E.EvoHarnessEvolver().bootstrap(ctx) is None
    assert (adapter / "ADAPTER.md").read_text().startswith(BASE) and problems(adapter) == []
    assert E.EvoHarnessEvolver().step(StepContext(**{**ctx.__dict__, "agent": None})).status == "failed"


# ---------- the whole loop through the Trainer, on a toy task ----------

PIPELINE = '''
from pathlib import Path
from hermit.task import RunOutput

def run(task, adapter):
    text = (Path(adapter) / "ADAPTER.md").read_text() if adapter else ""
    ok = "bare value" in text
    return RunOutput(output="done" if ok else "unsure", cost_usd=0.0, tokens=0,
                     messages=[{"role": "user", "content": task["question"]}, {"role": "assistant", "content": "ok"}])
'''
JUDGE = '''
def score(task, output):
    ok = output.output == "done"
    return {"score": 1.0 if ok else 0.0, "feedback": "correct" if ok else "the form is wrong"}
'''


class ScriptedModel:
    model = "scripted"

    def __init__(self):
        self.calls = 0
        self.learner = Learner()

    def chat(self, messages, **kwargs):
        self.calls += 1
        text = messages[0]["content"]
        if "You attempted the task below" in text:
            reply = "TOPIC: toy\nACTION: NEW\nNAME: a-rule\nDESCRIPTION: For toys\nCONTENT:\n- a lesson\n"
        else:
            reply = self.learner(messages)["content"]
        return {"content": reply, "finish_reason": "stop"}, Usage("scripted", 10, 0, 10, 1, 0.0)


def test_evo_harness_learns_through_the_trainer(tmp_path):
    (tmp_path / "task-info").mkdir()
    (tmp_path / "task-info" / "README.md").write_text("Solve toy tasks.\n")
    rows = [{"id": f"t{i:02d}", "question": f"Toy task {i}."} for i in range(8)]
    (tmp_path / "tasks.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (tmp_path / "train.txt").write_text("".join(f"t{i:02d}\n" for i in range(6)))
    (tmp_path / "test.txt").write_text("t06\nt07\n")
    (tmp_path / "pipeline.py").write_text(PIPELINE)
    (tmp_path / "judge.py").write_text(JUDGE)
    config = {"data": {"tasks": "tasks.jsonl", "train": "train.txt", "test": "test.txt", "step_size": 2,
                       "shuffle": False},
              "task_info": "task-info", "agent": {"name": "pipeline.py:run", "concurrency": 2},
              "judge": {"path": "judge.py"}, "template": str(TEMPLATE),
              "evolver": {"name": "evo_harness", "params": {"workers": 2}},
              "sandbox": {"enabled": False},
              "trainer": {"run_name": "toy", "output_dir": "runs", "epochs": 1, "budget": {"hours": 1}}}
    (tmp_path / "run.yaml").write_text(yaml.safe_dump(config))
    loaded = Config.load(tmp_path / "run.yaml")
    trainer = Trainer(loaded, log=lambda *_: None)
    trainer.llm = model = ScriptedModel()
    summary = trainer.train()
    run = tmp_path / "runs" / "toy"
    assert summary["steps"] == 3 and model.calls > 0
    steps = [json.loads(l) for l in (run / "steps.jsonl").read_text().splitlines()]
    assert all(s["status"] == "done" for s in steps)
    final = (run / "checkout" / "ADAPTER.md").read_text()
    assert "- bare value" in final and "`skills/toy--a-rule/SKILL.md`: For toys" in final
    assert evaluate(loaded, "final", log=lambda *_: None)["mean_score"] == 1.0
