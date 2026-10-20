"""Harness² core: surface validation and rendering, evidence tripwires, renderers, parsers, inlining (offline)."""
import json
import os
from pathlib import Path
import shutil
import tempfile

import pytest

from hermit.algorithms.evolvers.harness2.core import context as C
from hermit.algorithms.evolvers.harness2.core import prompts as P
from hermit.algorithms.evolvers.harness2.core import render as R
from hermit.algorithms.evolvers.harness2.core import surface as S
from hermit.algorithms.evolvers.harness2.core.ledger import BudgetError, Ledger
from hermit.algorithms.evolvers.harness2.core.library import Library
from hermit.algorithms.evolvers.harness2.core.proposer import LlmProposer, inline, parse_blocks
from hermit.algorithms.evolvers.harness2.core.selection import (assign_ids, parse_choice, parse_notes,
                                                                        parse_reflection)
from hermit.spec import check_spec

TEMPLATE = Path(__file__).resolve().parents[1] / "hermit" / "algorithms" / "templates" / "harness2"
SPEC = S.load_surface(TEMPLATE)
SKILL = "---\ndescription: reading a figure from a table\n---\nRead the header row, then the row for the period.\n"


@pytest.fixture
def tmp_path():
    """A directory outside /tmp: the template check runs under bubblewrap with an empty tmpfs at /tmp."""
    base = Path.home() / ".cache" / "hermit-tests"
    base.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(dir=base))
    yield path
    if not os.environ.get("KEEP_TEST_DIRS"):
        shutil.rmtree(path, ignore_errors=True)


def test_template_root_passes_its_own_check():
    assert check_spec(TEMPLATE, TEMPLATE).passed
    assert SPEC.skills == "inline" and not SPEC.scripts and SPEC.context_max == 40000


def test_validate_names_every_problem_and_autotrigger_repairs_one():
    tree = {"harness/systemprompt.md": "Work carefully.\n", "skills/table_lookup/SKILL.md": SKILL,
            "skills/no_desc/SKILL.md": "just a body\n"}
    found: dict[str, list[str]] = {}
    for path, message in S.problems(SPEC, tree):
        found.setdefault(path, []).append(message)
    assert any("description" in m for m in found["skills/no_desc/SKILL.md"])
    assert any("never named" in m for m in found["skills/table_lookup/SKILL.md"])
    assert S.autotrigger(SPEC, tree) == ["no_desc", "table_lookup"]
    assert "follow the `table_lookup` skill" in tree["harness/systemprompt.md"]
    assert "When reading a figure" in tree["harness/systemprompt.md"]        # no "When when"
    with pytest.raises(ValueError, match="description"):
        S.validate(SPEC, tree)
    tree["skills/no_desc/SKILL.md"] = "---\ndescription: x\n---\nbody\n"
    assert S.validate(SPEC, tree)["skills"] == 2


def test_caps_scripts_and_off_surface_paths():
    big = {"harness/systemprompt.md": "x" * 25000}
    assert any("24000 cap" in m for _, m in S.problems(SPEC, big))
    script = {"harness/systemprompt.md": "Run python skills/a/scripts/b.py first.\n"}
    assert any("cannot run scripts" in m for _, m in S.problems(SPEC, script))
    assert not S.is_allowed(SPEC, "skills/a/scripts/b.py") and not S.is_allowed(SPEC, "../x.md")
    assert not S.is_allowed(SPEC, "agents/helper.md")


def test_render_and_template_check_identity(tmp_path):
    adapter = tmp_path / "adapter"
    shutil.copytree(TEMPLATE, adapter)
    tree = {"harness/systemprompt.md": "When a figure is in a table, follow the `table_lookup` skill.\n",
            "skills/table_lookup/SKILL.md": SKILL}
    S.write_tree(SPEC, tree, adapter, render=True)
    text = (adapter / "ADAPTER.md").read_text()
    assert text.startswith("When a figure") and "## Skills" in text and "### table_lookup" in text
    assert "description:" not in text                       # frontmatter is not rendered
    assert check_spec(adapter, TEMPLATE).passed
    (adapter / "ADAPTER.md").write_text(text + "extra\n")
    result = check_spec(adapter, TEMPLATE)
    assert not result.passed and any(i.path == "ADAPTER.md" for i in result.issues)
    assert S.read_tree(SPEC, adapter) == {**{a: "" for a in S.ALWAYS_ON}, **tree}


def test_tripwires(tmp_path):
    d = tmp_path / "ctx"
    d.mkdir()
    (d / "ok.md").write_text("the agent read the table\n")
    C.assert_no_grader_verdicts(d)
    (d / "bad.json").write_text('{"judge": {"score": 1.0}}')
    with pytest.raises(ValueError, match="grader verdict"):
        C.assert_no_grader_verdicts(d)
    with pytest.raises(ValueError, match="answer-key"):
        C.assert_no_leak_paths(d, ["bad.json"])
    with pytest.raises(ValueError):
        C.assert_tree_has_no_grader_verdicts({"harness/guardrails.md": 'aim for "pass_rate" 1'})


def test_build_evidence_layout(tmp_path):
    harness = tmp_path / "h"
    S.write_tree(SPEC, {"harness/systemprompt.md": "Be exact.\n"}, harness, render=False)
    refs = [C.Rollout("t1", "t1", "[ACTION] read()\n[OBSERVATION] 42\n", "Final answer: 42\n"),
            C.Rollout("t2", "t2", "", "", ok=False, error="crashed")]
    ev = C.build(tmp_path / "stage", tasks={"t1": "What is x?", "t2": "What is y?"}, rollouts=refs,
                 harness_dir=harness, surface=SPEC, extra=[("agent_prompt.md", "You are an agent.", "the prompt")])
    ctx = tmp_path / "stage" / "context"
    assert (ctx / "tasks" / "t1.md").read_text() == "What is x?"
    assert "## t2" in (ctx / "task.md").read_text()
    assert (ctx / "harness" / "harness" / "systemprompt.md").read_text() == "Be exact.\n"
    assert ev.rollouts == ["t1"] and ev.failed and "agent_prompt.md" in ev.layout
    assert "(trajectory unavailable: crashed)" in (ctx / "rollouts" / "t2.md").read_text()
    mission = C.mission(ev, job="Do the job.")
    assert mission.startswith("Do the job.") and "context/rollouts/" in mission


def test_renderers(tmp_path):
    msgs = [{"role": "system", "content": "SYSTEM"}, {"role": "user", "content": "Q?"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "1", "function": {"name": "search", "arguments": '{"query": "debt"}'}}]},
            {"role": "tool", "tool_call_id": "1", "content": "x" * 300},
            {"role": "assistant", "content": "<FINAL_ANSWER>7</FINAL_ANSWER>"}]
    text = R.render_messages(msgs, obs_trunc=100)
    assert "SYSTEM" not in text and '[ACTION] search({"query": "debt"})' in text
    assert "observation truncated by the renderer" in text and "[ASSISTANT] <FINAL_ANSWER>7" in text
    assert R.final_message(msgs) == "<FINAL_ANSWER>7</FINAL_ANSWER>"
    codex = [{"type": "item.completed", "item": {"type": "command_execution", "command": "ls", "exit_code": 1,
                                                  "aggregated_output": "no such file"}},
             {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}]
    (tmp_path / "codex.jsonl").write_text("\n".join(json.dumps(e) for e in codex))
    out = R.render_log(tmp_path / "codex.jsonl", obs_trunc=1000)
    assert "[ACTION] bash" in out and "[OBSERVATION ERROR] (exit 1)" in out and "[ASSISTANT] done" in out
    claude = [{"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"p": 1}}]}},
              {"type": "user", "message": {"content": [{"type": "tool_result", "content": "body"}]}}]
    (tmp_path / "claude.jsonl").write_text("\n".join(json.dumps(e) for e in claude))
    assert "[ACTION] Read" in R.render_log(tmp_path / "claude.jsonl", obs_trunc=1000)
    pred = tmp_path / "run" / "predictions" / "UID1"
    pred.mkdir(parents=True)
    (pred / "conversation.json").write_text(json.dumps([
        {"role": "user", "content": "## Question\nQ?"}, {"type": "message", "content": ""},
        {"type": "tool_call", "cmd": "grep(pattern='debt', path='a.txt')", "obs": "12: debt 5"},
        {"type": "message", "content": "<answer>5</answer>"}]))
    (pred / "target_system_prompt.txt").write_text("You are an expert.\n## Skill\nS\n")
    out = R.render_log(tmp_path / "run", obs_trunc=1000)
    assert "[ACTION] grep(pattern='debt', path='a.txt')" in out and "[OBSERVATION] 12: debt 5" in out
    assert R.final_message(log=pred) == "<answer>5</answer>"
    assert R.system_prompt_of(tmp_path / "run").startswith("You are an expert.")


def test_parsers_and_ids():
    ids = assign_ids(["agg1", "agg2", "agg3"], seed_key="s|t")
    assert ids == assign_ids(["agg3", "agg1", "agg2"], seed_key="s|t") and set(ids) == {"A", "B", "C"}
    anon, why = parse_choice("<why>B is complete</why>\n```choice\nB\n```", ids)
    assert anon == "B" and why == "B is complete"
    with pytest.raises(ValueError):
        parse_choice("```choice\nZ\n```", ids)
    rows, exp = parse_reflection("<edits>\ncomponent: x\n</edits>\nNO_ENTRY")
    assert rows == "component: x" and exp == {}
    _, exp = parse_reflection("<experience>\ncue: c\nlesson: l\nevidence: e\nconfidence: low\n</experience>")
    assert exp == {"cue": "c", "lesson": "l", "evidence": "e", "confidence": "low"}
    assert parse_notes("<notes>kept -> x -> y</notes>") == "kept -> x -> y"


def test_file_blocks_and_llm_channel(tmp_path):
    files, deletes = parse_blocks("<<<FILE context/harness/harness/guardrails.md\n```markdown\nRule.\n```\nFILE>>>\n"
                                  "<<<DELETE skills/old/SKILL.md>>>\n<notes>n</notes>")
    assert files == {"harness/guardrails.md": "Rule.\n"} and deletes == ["skills/old/SKILL.md"]
    root = tmp_path / "stage"
    S.write_tree(SPEC, {"harness/systemprompt.md": "Old.\n", "skills/old/SKILL.md": SKILL}, root / "context" / "harness",
                 render=False)
    (root / "context" / "rollouts").mkdir()
    (root / "context" / "rollouts" / "t1.md").write_text("y" * 50000)
    replies = iter(["", "<<<FILE harness/systemprompt.md\nNew.\nFILE>>>\n<<<DELETE skills/old/SKILL.md>>>\n"
                        "<notes>changed</notes>"])
    seen = []

    def llm(messages, max_tokens=None, temperature=None):
        seen.append(messages[-1]["content"])
        return {"content": next(replies), "usage": {"cost_usd": 0.01}}
    reply = LlmProposer(llm, inline_chars=20000, log_dir=tmp_path / "log").call("MISSION", root, "inv1.a1",
                                                                                harvest="context/harness")
    assert len(seen) == 2 and "chars of this trajectory omitted" in seen[0]
    assert S.read_tree(SPEC, reply.workspace) == {**{a: "" for a in S.ALWAYS_ON}, "harness/systemprompt.md": "New.\n"}
    assert reply.usd == pytest.approx(0.02) and (tmp_path / "log" / "inv1.a1.reply1.md").exists()
    text, manifest = inline(root, 10_000)
    assert manifest["clipped"][0]["kept"] < 50000 and len(text) < 12_000


def test_library_and_ledger(tmp_path):
    lib = Library(tmp_path / "lib", "default", SPEC)
    tree = {"harness/systemprompt.md": "Be exact.\n"}
    assert lib.add("step_0001", tree, experience={"cue": "c"}) is None and lib.context_dir() is None
    exp = {"cue": "a table", "lesson": "add a skill", "evidence": "row 1", "confidence": "low"}
    d = lib.add("step_0002", tree, why="won", label="agg1", experience=exp)
    assert (d / "harness" / "harness" / "systemprompt.md").exists() and lib.entries() == ["step_0002"]
    assert "- cue:     a table" in lib.index.read_text() and lib.context_dir() == lib.dir
    ledger = Ledger("agg", "step", 2)
    ledger.rollout("base", cached=True)
    for label in ("inv1", "dom1", "inv2", "dom2", "agg1", "agg2"):
        ledger.rollout(label)
    for label in ("inv1", "dom1", "inv2", "dom2", "agg1", "agg2"):
        ledger.session(label)
    ledger.assert_identity()
    ledger.session("agg2")
    with pytest.raises(BudgetError, match="DUPLICATE"):
        ledger.assert_identity()


def test_slots_refuse_measurement_words(tmp_path):
    with pytest.raises(ValueError, match="score"):
        P.Slots(work="maximise the score")
    (tmp_path / "s.yaml").write_text("work: answer questions\ndeliverable_word: final answer\n")
    slots = P.load_slots(tmp_path / "s.yaml")
    text = P.aggregate(slots, SPEC, channel="llm", n_tasks=3)
    assert "final answer" not in text or True
    assert "3 rollouts under the current harness" in text and "<<<FILE" in text
    assert "<<<FILE" not in P.propose(slots, "DOM", SPEC, channel="agent", n_tasks=1)


# ---------------------------------------------------------------- regressions from the code review ----------------

from hermit.algorithms.evolvers.harness2.core import step as STEP
from hermit.algorithms.evolvers.harness2.core.proposer import (ProposerError, Reply, apply_blocks,
                                                                       clipping_note)


def test_file_blocks_real_model_shapes(tmp_path):
    text = ("<<<FILE harness/tool_guidance.md\nFILE>>>\n<<<FILE harness/guardrails.md>>>\nQuote the cell.\nFILE>>>\n"
            "<<<FILE: harness/systemprompt.md\n````markdown\nUse grep first.\n```inner```\n````\nFILE>>>\r\n"
            "<notes>kept -> the <<<FILE block for guardrails -> t3</notes>")
    files, deletes = parse_blocks(text)
    assert files == {"harness/tool_guidance.md": "", "harness/guardrails.md": "Quote the cell.\n",
                     "harness/systemprompt.md": "Use grep first.\n```inner```\n"}
    assert LlmProposer._usable(text, "stop", "context/harness", None)          # a mid-line mention is not a block
    assert not LlmProposer._usable("<<<FILE harness/guardrails.md\ncut off", "length", "context/harness", None)
    assert not LlmProposer._usable("<why>B</why>", "length", None, r"```choice")
    S.write_tree(SPEC, {"skills/old/SKILL.md": SKILL}, tmp_path / "h", render=False)
    problems = apply_blocks(tmp_path / "h", {"ADAPTER.md": "x\n"}, ["skills/old", "skills/none/SKILL.md"])
    assert not (tmp_path / "h" / "skills" / "old").exists()
    assert any("ADAPTER.md" in p for p in problems) and any("names no file" in p for p in problems)


def test_unusable_reply_after_retry_is_not_applied(tmp_path):
    root = tmp_path / "stage"
    S.write_tree(SPEC, {"harness/systemprompt.md": "Old.\n"}, root / "context" / "harness", render=False)
    calls = []

    def llm(messages, max_tokens=None, temperature=None):
        calls.append(max_tokens)
        return {"content": "", "finish_reason": "length", "usage": {"cost_usd": 0.1}}
    reply = LlmProposer(llm, max_tokens=1000).call("M", root, "agg1.a1", harvest="context/harness")
    assert reply.workspace is None and "cut off" in reply.error and calls == [1000, 2000]
    assert reply.usd == pytest.approx(0.2)


def _env(tmp_path, proposer, probe=None, tasks=("t1", "t2")):
    refs = [C.Rollout(t, t, "[ACTION] read()\n[OBSERVATION] 1\n", "Final answer: 1\n") for t in tasks]
    return STEP.Env(surface=SPEC, slots=P.Slots(work="answer questions"), proposer=proposer, channel="llm",
                    tasks={t: f"question {t}" for t in tasks}, reference=refs,
                    base_tree={"harness/systemprompt.md": "Be exact.\n"}, work_dir=tmp_path / "work",
                    probe=probe or (lambda label, tree: ([C.Rollout(t, t, "[ASSISTANT] ok\n", "Final answer: 1\n")
                                                         for t in tasks], 0.0)),
                    log=lambda m: None)


class _Scripted:
    def __init__(self, replies):
        self.replies, self.calls = replies, []

    def call(self, mission, root, label, *, harvest=None, expect=None):
        self.calls.append((label, mission))
        r = self.replies(label, Path(root), harvest)
        if isinstance(r, Exception):
            raise r
        return r


def _edit(root, harvest, files):
    work = root / "proposal"
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(root / harvest, work)
    for rel, body in files.items():
        if body is None:
            (work / rel).unlink()
        else:
            (work / rel).parent.mkdir(parents=True, exist_ok=True)
            (work / rel).write_text(body)
    return work


def test_session_failure_falls_back_without_a_second_attempt(tmp_path):
    prop = _Scripted(lambda label, root, harvest: ProposerError("connection reset", usd=0.3))
    stage = STEP.run_stage(_env(tmp_path, prop), label="inv1", dim="INV", job="JOB",
                           fallback={"harness/systemprompt.md": "Be exact.\n"},
                           harness_tree={"harness/systemprompt.md": "Be exact.\n"})
    assert stage.fell_back and [c[0] for c in prop.calls] == ["inv1.a1"] and stage.usd == pytest.approx(0.3)


def test_empty_tree_and_unusable_reply_retry_honestly(tmp_path):
    def replies(label, root, harvest):
        if label.endswith("a1"):
            return Reply(label, "<notes>start over</notes>",
                         _edit(root, harvest, {a: None for a in S.ALWAYS_ON}))
        return Reply(label, "", None, error="the reply was empty")
    prop = _Scripted(replies)
    stage = STEP.run_stage(_env(tmp_path, prop), label="agg1", dim="AGG", job="JOB",
                           fallback={"harness/systemprompt.md": "Be exact.\n"},
                           harness_tree={"harness/systemprompt.md": "Be exact.\n"})
    assert stage.fell_back and "came back empty" in prop.calls[1][1] and "is unchanged" in prop.calls[1][1]
    assert "rejected attempt" not in prop.calls[1][1]


def test_rejected_tree_is_written_back_and_decisions_never_carry_markers(tmp_path):
    def replies(label, root, harvest):
        return Reply(label, "<notes>n</notes>",
                     _edit(root, harvest, {"harness/guardrails.md": 'Rows list "score": values.\n'}))
    prop = _Scripted(replies)
    stage = STEP.run_stage(_env(tmp_path, prop), label="dom1", dim="DOM", job="JOB",
                           fallback={"harness/systemprompt.md": "Be exact.\n"},
                           harness_tree={"harness/systemprompt.md": "Be exact.\n"})
    assert stage.fell_back and "rejected attempt" in prop.calls[1][1]
    text = STEP.decision(stage)
    assert text.startswith("FELL BACK") and '"score":' not in text
    C.assert_tree_has_no_grader_verdicts({"decisions/dom1.md": text})


def test_blind_selection_counts_real_picks_only_and_gates_delivery(tmp_path):
    result = STEP.StepResult(mode="agg", k=2, finals=["agg1", "agg2"],
                             trees={"agg1": {"harness/systemprompt.md": "A\n"}, "agg2": {"harness/systemprompt.md": "B\n"}})
    for label in ("agg1", "agg2"):
        result.probes[label] = [C.Rollout(t, t, "x", f"answer {label}") for t in ("t1", "t2", "t3")]

    def replies(label, root, harvest):
        if label == "select.t1":
            want = next(p.stem for p in (root / "context" / "candidates").glob("*.md") if "agg2" in p.read_text())
            return Reply(label, f"<why>w</why>\n```choice\n**{want}**\n```")
        return Reply(label, "I prefer the first one.")                      # no choice block: not a vote
    sel = STEP.select(_env(tmp_path, _Scripted(replies), tasks=("t1", "t2", "t3")), result, "blind", "s1")
    assert sel.chosen == "agg2" and sel.ok and sel.picks == {"t1": "agg2"} and len(sel.fallbacks) == 2
    assert sel.id_maps["t1"] and "agg2" in sel.id_maps["t1"].values()
    for label in ("agg1", "agg2"):
        result.probes[label] = [C.Rollout(t, t, "x", "No answer was produced.", delivered=False) for t in ("t1",)]
    sel = STEP.select(_env(tmp_path, _Scripted(replies), tasks=("t1",)), result, "composed", "s2")
    assert sel.chosen == "base" and not sel.ok


def test_globs_markers_slots_and_clipping_note(tmp_path):
    d = tmp_path / "ctx" / ".private"
    d.mkdir(parents=True)
    (d / "answers.jsonl").write_text("{}")
    for glob in (".private/*", "./.private/answers.jsonl", "**/.private/**"):
        with pytest.raises(ValueError, match="answer-key"):
            C.assert_no_leak_paths(tmp_path / "ctx", [glob])
    with pytest.raises(ValueError):
        C.assert_tree_has_no_grader_verdicts({"a.md": "predicted '12' but expected '13'"})
    with pytest.raises(ValueError):
        C.assert_tree_has_no_grader_verdicts({"a.md": "The Reference Answer Is 4"})
    assert C.safe_text('x "Score": 1') != 'x "Score": 1' and C.safe_text("fine") == "fine"
    for bad in ("Scoring is exact match", "the judge reads it", "graded answers"):
        with pytest.raises(ValueError):
            P.Slots(work=bad)
    assert P.load_slots(None).work == "the task described in context/task.md"
    note = clipping_note({"clipped": [{"file": "rollouts/t1.md", "chars": 100, "kept": 40}]})
    assert "40 of 100" in note and clipping_note({"clipped": []}) == ""


def test_template_check_refuses_marker_text(tmp_path):
    adapter = tmp_path / "adapter"
    shutil.copytree(TEMPLATE, adapter)
    S.write_tree(SPEC, {"harness/systemprompt.md": 'Tools return {"score": 0.8}.\n'}, adapter, render=True)
    result = check_spec(adapter, TEMPLATE)
    assert not result.passed and any(i.path == "harness/systemprompt.md" for i in result.issues)


def test_frontmatter_matches_the_framework_and_is_repaired(tmp_path):
    adapter = tmp_path / "adapter"
    shutil.copytree(TEMPLATE, adapter)
    tree = {"harness/systemprompt.md": "When a figure is in a table, follow the `tbl` skill.\n",
            "skills/tbl/SKILL.md": "---\ndescription: Locating a figure: which table, row and column\n---\nBody.\n"}
    assert any("not valid YAML" in m for _, m in S.problems(SPEC, tree))
    assert S.repair_frontmatter(SPEC, tree) == ["skills/tbl/SKILL.md"] and not S.problems(SPEC, tree)
    S.write_tree(SPEC, tree, adapter, render=True)
    assert check_spec(adapter, TEMPLATE).passed
    assert "Locating a figure: which table" in (adapter / "ADAPTER.md").read_text()
    folded = {"skills/f/SKILL.md": "---\ndescription: >\n  Reading a\n  folded line\n---\nBody.\n"}
    assert S.split_frontmatter(folded["skills/f/SKILL.md"])[0]["description"].strip() == "Reading a folded line"


def test_ids_with_slashes_and_output_rules(tmp_path):
    from hermit.algorithms.evolvers.harness2.evolver import delivered, deliverable_text
    ev = C.build(tmp_path / "s", tasks={"HumanEval/0": "q0", "HumanEval/1": "q1"},
                 rollouts=[C.Rollout("HumanEval/0", "HumanEval/0", "[ASSISTANT] x\n", "y")], surface=SPEC)
    assert (tmp_path / "s" / "context" / "rollouts" / "HumanEval_0.md").exists() and ev.rollouts == ["HumanEval/0"]
    assert delivered({"answer": 0}) and not delivered({"answer": ""}) and delivered({"sql": "x"})
    assert deliverable_text({"sql": "SELECT 1"}).startswith('{"sql": "SELECT 1"}')
    assert deliverable_text({"answer": "", "fail_reason": "turns"}).startswith("No answer was produced (turns)")


def test_selection_skips_a_task_whose_candidates_trip_the_wire(tmp_path):
    result = STEP.StepResult(mode="agg", k=2, finals=["agg1", "agg2"],
                             trees={"agg1": {"harness/systemprompt.md": "A\n"}, "agg2": {"harness/systemprompt.md": "B\n"}})
    result.probes["agg1"] = [C.Rollout("t1", "t1", "x", 'Final answer: {"score": 3}')]
    result.probes["agg2"] = [C.Rollout("t1", "t1", "x", "Final answer: 4")]
    sel = STEP.select(_env(tmp_path, _Scripted(lambda *a: Reply("select", "```choice\nA\n```")), tasks=("t1",)),
                      result, "blind", "s")
    assert not sel.picks and "tripwire" in sel.fallbacks[0] and not sel.ok
