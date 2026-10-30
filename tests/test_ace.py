"""ACE: the playbook port (against upstream's behaviour), the `ace` template, the Evolver's step with a scripted model
and a scripted run_agent (reflection, retry rounds, curation, rollback, validation, de-duplication), and the whole
loop through the Trainer on a toy task (no model, no network)."""
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

import pytest
import yaml

from hermit.algorithms.evolvers.ace import evolver as E
from hermit.algorithms.evolvers.ace import playbook as pb
from hermit.algorithms.evolvers.ace.prompts import CURATOR_PROMPT, CURATOR_PROMPT_NO_GT, REFLECTOR_PROMPT_NO_GT
from hermit.config import Config
from hermit.evaluate import evaluate
from hermit.evolver.base import Access, Progress, StepContext, Usage, Workspace
from hermit.spec import check_spec
from hermit.trainer.trainer import Trainer

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "hermit" / "algorithms" / "templates" / "ace"
KINDS = ("ALPHA", "BETA", "GAMMA", "DELTA")


@pytest.fixture
def tmp_path():
    """A directory outside /tmp: the template check runs under bubblewrap with an empty tmpfs at /tmp."""
    base = Path.home() / ".cache" / "hermit-tests"
    base.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(dir=base))
    yield path
    if not os.environ.get("KEEP_TEST_DIRS"):
        shutil.rmtree(path, ignore_errors=True)


PLAYBOOK = """## STRATEGIES & INSIGHTS
[sai-00001] helpful=0 harmful=0 :: Narrow to the right bulletin first.

## FORMULAS & CALCULATIONS
[calc-00002] helpful=3 harmful=1 :: Sum the monthly figures for a fiscal year.

## COMMON MISTAKES TO AVOID

## OTHERS"""


# ---------- the playbook, against upstream's behaviour ----------

def test_parse_and_format_round_trip():
    line = "[calc-00002] helpful=3 harmful=1 :: Sum the monthly figures :: twice."
    parsed = pb.parse_playbook_line(line)
    assert (parsed["id"], parsed["helpful"], parsed["harmful"]) == ("calc-00002", 3, 1)
    assert parsed["content"] == "Sum the monthly figures :: twice."
    assert pb.format_playbook_line("calc-00002", 3, 1, parsed["content"]) == line
    assert pb.parse_playbook_line("## OTHERS") is None
    assert pb.get_next_global_id(PLAYBOOK) == 3


@pytest.mark.parametrize("section, slug", [
    ("formulas_and_calculations", "calc"), ("COMMON MISTAKES TO AVOID", "err"), ("others", "misc"),
    ("STRATEGIES & INSIGHTS", "sai"), ("verification", "veri"), ("api_usage_notes", "aun")])
def test_section_slug(section, slug):
    assert pb.get_section_slug(section) == slug


def test_update_bullet_counts_helpful_harmful_neutral():
    tags = [{"id": "sai-00001", "tag": "helpful"}, {"id": "calc-00002", "tag": "harmful"},
            {"bullet": "calc-00002", "tag": "neutral"}, {"id": "nope-00009", "tag": "helpful"}]
    out = pb.update_bullet_counts(PLAYBOOK, tags)
    assert "[sai-00001] helpful=1 harmful=0 ::" in out
    # As upstream's tag map: the later tag for an id wins (here neutral), so calc-00002 is unchanged.
    assert "[calc-00002] helpful=3 harmful=1 ::" in out
    assert pb.update_bullet_counts(PLAYBOOK, []) == PLAYBOOK


def test_apply_curator_operations_adds_to_sections_and_falls_back_to_others():
    ops = [{"type": "ADD", "section": "formulas_and_calculations", "content": "Convert\nmillions  first."},
           {"type": "ADD", "section": "no such section", "content": "Check units."},
           {"type": "UPDATE", "bullet_id": "sai-00001", "content": "ignored"}]
    out, next_id, added = pb.apply_curator_operations(PLAYBOOK, ops, 3)
    lines = out.split("\n")
    # As upstream: a new bullet goes at the end of its section, just before the next header (after a blank line).
    nxt = lines.index("## COMMON MISTAKES TO AVOID")
    assert lines[nxt - 1] == "[calc-00003] helpful=0 harmful=0 :: Convert millions first."
    assert lines[nxt - 3].startswith("[calc-00002]")
    assert lines[lines.index("## OTHERS") + 1] == "[misc-00004] helpful=0 harmful=0 :: Check units."
    assert next_id == 5 and [a["id"] for a in added] == ["calc-00003", "misc-00004"]
    assert "ignored" not in out


def test_stats_and_bullets_for_the_reflector():
    stats = pb.get_playbook_stats(PLAYBOOK)
    assert stats["total_bullets"] == 2 and stats["unused"] == 1
    assert stats["by_section"]["FORMULAS & CALCULATIONS"] == {"count": 1, "helpful": 3, "harmful": 1}
    assert pb.extract_playbook_bullets(PLAYBOOK, []) == "(No bullets used by generator)"
    assert pb.extract_playbook_bullets(PLAYBOOK, ["x-00009"]) == \
        "(Generator referenced bullet IDs but none were found in playbook)"
    assert pb.extract_playbook_bullets(PLAYBOOK, ["calc-00002"]).startswith("[calc-00002] helpful=3 harmful=1 ::")


def test_cited_ids_skip_copies_of_the_playbook_and_unknown_ids():
    text = ("$ cat SKILL.md\n[sai-00001] helpful=0 harmful=0 :: Narrow ...\n"
            "Applying [calc-00002]: summing. Also [zzz-00009] and the example [slug-00001].")
    assert pb.cited_bullet_ids(text, PLAYBOOK) == ["calc-00002"]


@pytest.mark.parametrize("reply", [
    '{"reasoning": "r", "bullet_tags": [{"id": "sai-00001", "tag": "helpful"}]}',
    'Here:\n```json\n{"reasoning": "r", "bullet_tags": [{"id": "sai-00001", "tag": "helpful"}]}\n```',
    'prose "bullet_tags": [{"id": "sai-00001", "tag": "helpful"}] and an unclosed {',
])
def test_bullet_tags_from_a_reflector_reply(reply):
    assert pb.extract_bullet_tags(reply) == [{"id": "sai-00001", "tag": "helpful"}]


@pytest.mark.parametrize("reply, error", [
    ("no json at all", "Failed to extract"),
    ('{"operations": []}', "reasoning"),
    ('{"reasoning": "r"}', "operations"),
    ('{"reasoning": "r", "operations": [{"type": "ADD", "section": "x"}]}', "missing fields"),
])
def test_curator_reply_validation(reply, error):
    with pytest.raises(ValueError, match=error):
        pb.extract_and_validate_operations(reply)


def test_find_similar_groups_and_merge():
    vectors = [[1.0, 0.0], [0.99, 0.141], [0.0, 1.0], [0.995, 0.0998]]
    assert pb.find_similar_groups(vectors, 0.9) == [[0, 1, 3]]
    text = PLAYBOOK + "\n[misc-00003] helpful=1 harmful=0 :: a\n[misc-00004] helpful=0 harmful=0 :: b"
    merged = pb.merge_groups(text, [[0, 2]], {0: "[sai-00001] helpful=1 harmful=0 :: merged"})
    assert "[sai-00001] helpful=1 harmful=0 :: merged" in merged and "misc-00003" not in merged
    assert "calc-00002" in merged and "misc-00004" in merged
    kept = pb.merge_groups(text, [[0, 2]], {0: None})      # a failed merge keeps the first bullet as it was
    assert "Narrow to the right bulletin first." in kept and "misc-00003" not in kept


# ---------- the template ----------

def seed(tmp: Path, playbook: str | None = PLAYBOOK, base: str = "# OfficeQA skill\n\nFind the table first.\n") -> Path:
    adapter = tmp / "adapter"
    shutil.copytree(TEMPLATE, adapter, ignore=shutil.ignore_patterns("__pycache__"))
    text = pb.with_playbook(base, playbook) if playbook is not None else base
    (adapter / "ADAPTER.md").write_text(text)
    return adapter


def problems(adapter: Path) -> list[str]:
    return [i.problem for i in check_spec(adapter, TEMPLATE, deploy=False).issues]


def test_template_root_and_a_seed_pass_the_check(tmp_path):
    assert check_spec(TEMPLATE, TEMPLATE, deploy=True).passed
    assert check_spec(seed(tmp_path), TEMPLATE, deploy=True).passed
    assert (TEMPLATE / "ADAPTER.md").read_text() == pb.with_playbook("")


@pytest.mark.parametrize("mutate, expected", [
    (lambda t: t.replace(pb.END, ""), "exactly once"),
    (lambda t: t.replace("[calc-00002] helpful=3", "[calc-00002] helpful=x"), "neither"),
    (lambda t: t.replace("calc-00002", "sai-00001"), "appears twice"),
    (lambda t: t.replace("[calc-00002]", "[CALC-2]"), "not of the form"),
])
def test_check_rejects_a_broken_playbook(tmp_path, mutate, expected):
    adapter = seed(tmp_path)
    (adapter / "ADAPTER.md").write_text(mutate((adapter / "ADAPTER.md").read_text()))
    assert any(expected in p for p in problems(adapter))
    assert any(expected in p for p in pb.playbook_problems((adapter / "ADAPTER.md").read_text()))


# ---------- the step, with a scripted model and run_agent ----------

def material(tmp: Path, records: list[dict]) -> Path:
    root = tmp / "material"
    step = root / "step_0001"
    (step / "tasks").mkdir(parents=True)
    for r in records:
        (step / "tasks" / f"{r['id']}.json").write_text(json.dumps({**r, "trajectory": None}))
    (step / "summary.json").write_text(json.dumps({"tasks": [r["id"] for r in records]}))
    (root / "current").symlink_to("step_0001")
    return root


def record(tid: str, score: float, cites: str = "", feedback: str = "") -> dict:
    return {"id": tid, "input": {"id": tid, "question": f"What were receipts in {tid}?"},
            "output": {"answer": "44,463", "fail_reason": ""}, "status": "ok", "error": None,
            "judge": {"score": score, "feedback": feedback or ("correct" if score else
                                                               "predicted '44,463' but expected '44,463 million'")},
            "messages": [{"role": "system", "content": "SKILL"}, {"role": "user", "content": "task"},
                         {"role": "assistant", "content": f"Looking. {cites} <answer>44,463</answer>"}]}


def scripted(tags=None, ops=None, curator: str | None = None):
    """Answers the Reflector with `tags` and the Curator with `ops` (or the raw `curator` text); records prompts."""
    seen = []

    def llm(messages, **sampling):
        prompt = messages[0]["content"]
        seen.append((prompt, sampling))
        if "master curator" in prompt:
            return {"content": curator if curator is not None else
                    json.dumps({"reasoning": "add", "operations": ops or []})}
        if "merging similar playbook bulletpoints" in prompt:
            base = re.search(r"Keep the ID from the first entry: \[([^\]]+)\]", prompt).group(1)
            return {"content": f"[{base}] helpful=9 harmful=9 :: merged guidance"}
        return {"content": json.dumps({"reasoning": "r", "error_identification": "units", "root_cause_analysis": "x",
                                       "correct_approach": "Report in millions.", "key_insight": "Keep units.",
                                       "bullet_tags": tags or []})}
    llm.seen = seen
    return llm


def context(tmp: Path, adapter: Path, llm, params: dict | None = None, run_agent=None, validation: Path | None = None,
            progress: Progress | None = None, check=None) -> StepContext:
    ws = Workspace(adapter=adapter, learning_material=tmp / "material", task_info=tmp / "task-info",
                   history=tmp / "history", state=tmp / "state", scratch=tmp / "scratch", output=tmp / "output")
    for d in (ws.state, ws.scratch, ws.output, ws.task_info):
        d.mkdir(parents=True, exist_ok=True)

    def check_spec_tool(path: str) -> dict:
        result = check_spec(Path(path), TEMPLATE, deploy=False)
        return {"passed": result.passed, "issues": [i.__dict__ for i in result.issues]}

    tools = {"llm": llm, "check_spec": check or check_spec_tool}
    if run_agent:
        tools["run_agent"] = run_agent
    defaults = yaml.safe_load((ROOT / "hermit" / "algorithms" / "evolvers" / "ace" / "evolver.yaml").read_text())["params"]
    return StepContext(workspace=ws, access=Access(validation=validation), tools=tools,
                       params={**defaults, **(params or {})}, progress=progress or Progress(1, 1, False))


def playbook_of(adapter: Path) -> str:
    return pb.split_adapter((adapter / "ADAPTER.md").read_text())[1]


def test_a_right_answer_is_reflected_on_once_and_curated(tmp_path):
    adapter = seed(tmp_path)
    material(tmp_path, [record("UID1", 1.0, cites="Applying [calc-00002].")])
    llm = scripted(tags=[{"id": "calc-00002", "tag": "helpful"}],
                   ops=[{"type": "ADD", "section": "COMMON MISTAKES TO AVOID", "content": "Keep the units."}])
    result = E.ACEEvolver().step(context(tmp_path, adapter, llm))
    assert result.status == "done", result.summary
    book = playbook_of(adapter)
    assert "[calc-00002] helpful=4 harmful=1 ::" in book
    assert "[err-00003] helpful=0 harmful=0 :: Keep the units." in book
    assert (adapter / "ADAPTER.md").read_text().startswith("# OfficeQA skill")      # the base text is kept
    reflect, curate = llm.seen[0][0], llm.seen[1][0]
    assert reflect.startswith(REFLECTOR_PROMPT_NO_GT[:60]) and E.CORRECT in reflect
    assert "[calc-00002] helpful=3 harmful=1 ::" in reflect and "[sai-00001]" not in reflect   # only cited bullets
    assert curate.startswith(CURATOR_PROMPT[:60]) and "Sample 1 out of unknown" in curate
    state = json.loads((tmp_path / "state" / "ace.json").read_text())
    assert state["next_id"] == 4 and state["steps"][0]["samples"][0]["cited"] == ["calc-00002"]
    assert (tmp_path / "output" / "report.md").is_file()


def test_a_wrong_answer_is_retried_with_the_reflection_until_right(tmp_path):
    adapter = seed(tmp_path)
    material(tmp_path, [record("UID2", 0.0, cites="Applying [sai-00001].")])
    runs = []

    def run_agent(task_ids, adapter):
        text = (Path(adapter) / "ADAPTER.md").read_text()
        runs.append(text)
        log = tmp_path / f"log{len(runs)}.txt"
        log.write_text("Applying [calc-00002] this time.")
        score = 1.0 if len(runs) == 2 else 0.0
        return {"results": [{"id": task_ids[0], "status": "ok", "output": {"answer": "x"},
                             "judge": {"score": score, "feedback": "f"}, "log": str(log)}]}
    llm = scripted(tags=[{"id": "sai-00001", "tag": "harmful"}],
                   ops=[{"type": "ADD", "section": "STRATEGIES & INSIGHTS", "content": "Report in millions."}])
    result = E.ACEEvolver().step(context(tmp_path, adapter, llm, run_agent=run_agent))
    assert result.status == "done"
    assert len(runs) == 2 and all("# Reflection" in t and "Report in millions." in t for t in runs)
    sample = json.loads((tmp_path / "state" / "ace.json").read_text())["steps"][0]["samples"][0]
    assert [r["score"] for r in sample["rounds"]] == [0.0, 1.0] and sample["correct_after_rounds"]
    assert sample["rounds"][0]["cited"] == ["calc-00002"]
    book = playbook_of(adapter)
    assert "[sai-00001] helpful=0 harmful=2 ::" in book                  # tagged harmful in each of two rounds
    assert "# Reflection" not in (adapter / "ADAPTER.md").read_text()    # the reflection is never kept
    assert E.WRONG in llm.seen[0][0] and "expected '44,463 million'" in llm.seen[0][0]


def test_retry_rounds_stop_at_max_num_rounds(tmp_path):
    adapter = seed(tmp_path)
    material(tmp_path, [record("UID2", 0.0)])
    calls = []

    def run_agent(task_ids, adapter):
        calls.append(1)
        return {"results": [{"id": task_ids[0], "status": "ok", "output": {"answer": "x"},
                             "judge": {"score": 0.0, "feedback": "f"}}]}
    E.ACEEvolver().step(context(tmp_path, adapter, scripted(), {"max_num_rounds": 2}, run_agent=run_agent))
    assert len(calls) == 2


def test_label_free_mode_neither_shows_the_verdict_nor_retries(tmp_path):
    adapter = seed(tmp_path)
    material(tmp_path, [record("UID2", 0.0)])
    llm = scripted()
    run_agent = lambda **_: pytest.fail("no retry without feedback")
    result = E.ACEEvolver().step(context(tmp_path, adapter, llm, {"feedback": False}, run_agent=run_agent))
    assert result.status == "done"
    reflect, curate = llm.seen[0][0], llm.seen[1][0]
    assert "expected" not in reflect and E.WRONG not in reflect and E.CORRECT not in reflect
    assert curate.startswith(CURATOR_PROMPT_NO_GT[:60]) and "environment feedback that will NOT" in curate


def test_an_unusable_curator_reply_leaves_the_playbook(tmp_path):
    adapter = seed(tmp_path)
    material(tmp_path, [record("UID1", 1.0)])
    before = playbook_of(adapter)
    result = E.ACEEvolver().step(context(tmp_path, adapter, scripted(curator="I would add things.")))
    assert result.status == "done" and playbook_of(adapter) == before
    sample = json.loads((tmp_path / "state" / "ace.json").read_text())["steps"][0]["samples"][0]
    assert sample["curator"].startswith("skipped: Failed to extract")


def test_curator_frequency_counts_samples(tmp_path):
    adapter = seed(tmp_path)
    material(tmp_path, [record(f"UID{i}", 1.0) for i in range(4)])
    llm = scripted(ops=[{"type": "ADD", "section": "OTHERS", "content": "note"}])
    E.ACEEvolver().step(context(tmp_path, adapter, llm, {"curator_frequency": 2}))
    assert sum(1 for p, _ in llm.seen if "master curator" in p) == 2
    assert len(pb.bullet_ids_of(playbook_of(adapter))) == 4


def test_a_playbook_failing_the_check_is_rolled_back(tmp_path):
    adapter = seed(tmp_path)
    material(tmp_path, [record("UID1", 1.0)])
    before = (adapter / "ADAPTER.md").read_text()
    llm = scripted(ops=[{"type": "ADD", "section": "OTHERS", "content": "note"}])
    failing = lambda path: {"passed": False, "issues": [{"problem": "too large"}]}
    result = E.ACEEvolver().step(context(tmp_path, adapter, llm, check=failing))
    assert result.status == "failed" and "too large" in result.summary
    assert (adapter / "ADAPTER.md").read_text() == before


def test_bootstrap_adds_the_playbook_to_a_plain_skill(tmp_path):
    adapter = seed(tmp_path, playbook=None)
    ev = E.ACEEvolver()
    ctx = context(tmp_path, adapter, scripted())
    assert ev.bootstrap(ctx).status == "done"
    text = (adapter / "ADAPTER.md").read_text()
    assert text.startswith("# OfficeQA skill") and playbook_of(adapter) == pb.EMPTY_PLAYBOOK
    assert check_spec(adapter, TEMPLATE, deploy=False).passed
    assert ev.bootstrap(ctx) is None


def test_validation_keeps_the_best_playbook_for_finalize(tmp_path):
    adapter = seed(tmp_path)
    val = tmp_path / "val"
    val.mkdir()
    (val / "tasks.jsonl").write_text("".join(json.dumps({"id": f"V{i}"}) + "\n" for i in range(4)))
    scores = iter([0.5, 0.25])

    def run_agent(task_ids, adapter):
        s = next(scores)
        return {"results": [{"id": t, "judge": {"score": 1.0 if i < s * len(task_ids) else 0.0}}
                            for i, t in enumerate(task_ids)]}
    ev = E.ACEEvolver()
    llm = scripted(ops=[{"type": "ADD", "section": "OTHERS", "content": "first"}])
    material(tmp_path, [record("UID1", 1.0)])
    ev.step(context(tmp_path, adapter, llm, run_agent=run_agent, validation=val, progress=Progress(1, 1, True)))
    best = playbook_of(adapter)
    shutil.rmtree(tmp_path / "material")
    material(tmp_path, [record("UID3", 1.0)])
    llm = scripted(ops=[{"type": "ADD", "section": "OTHERS", "content": "second"}])
    ev.step(context(tmp_path, adapter, llm, run_agent=run_agent, validation=val, progress=Progress(2, 2, True)))
    assert "second" in playbook_of(adapter)
    final = ev.finalize(context(tmp_path, adapter, llm, validation=val))
    assert playbook_of(adapter) == best and "0.500" in final.summary
    last = tmp_path / "state" / "last_adapter"                       # the last playbook, kept for evaluation
    assert "second" in playbook_of(last) and check_spec(last, TEMPLATE, deploy=False).passed
    assert not (last / ".git").exists()
    state = json.loads((tmp_path / "state" / "ace.json").read_text())
    assert [v["score"] for v in state["validations"]] == [0.5, 0.25] and state["epoch_sizes"] == {"1": 1}


def test_finalize_after_validations_that_all_scored_zero_returns_the_initial_playbook(tmp_path):
    adapter = seed(tmp_path)
    val = tmp_path / "val"
    val.mkdir()
    (val / "tasks.jsonl").write_text(json.dumps({"id": "V0"}) + "\n")
    run_agent = lambda task_ids, adapter: {"results": [{"id": t, "judge": {"score": 0.0}} for t in task_ids]}
    material(tmp_path, [record("UID1", 1.0)])
    ev = E.ACEEvolver()
    llm = scripted(ops=[{"type": "ADD", "section": "OTHERS", "content": "added"}])
    ev.step(context(tmp_path, adapter, llm, run_agent=run_agent, validation=val, progress=Progress(1, 1, True)))
    assert "added" in playbook_of(adapter)
    ev.finalize(context(tmp_path, adapter, llm, validation=val))
    assert playbook_of(adapter) == PLAYBOOK


def test_validation_every_n_samples_and_score_initial(tmp_path):
    adapter = seed(tmp_path)
    val = tmp_path / "val"
    val.mkdir()
    (val / "tasks.jsonl").write_text(json.dumps({"id": "V0"}) + "\n")
    calls = []

    def run_agent(task_ids, adapter):
        calls.append((Path(adapter) / "ADAPTER.md").read_text())
        return {"results": [{"id": t, "judge": {"score": 1.0}} for t in task_ids]}
    material(tmp_path, [record(f"UID{i}", 1.0) for i in range(3)])
    ev = E.ACEEvolver()
    ev.step(context(tmp_path, adapter, scripted(), {"val_every": 2, "score_initial": True}, run_agent=run_agent,
                    validation=val))
    state = json.loads((tmp_path / "state" / "ace.json").read_text())
    assert [v["source"] for v in state["validations"]] == ["initial", "epoch 1 sample 3"]
    assert state["best"]["source"] == "initial"                       # a tie does not replace the best


def test_dedup_merges_similar_bullets(tmp_path, monkeypatch):
    adapter = seed(tmp_path)
    material(tmp_path, [record("UID1", 1.0)])
    llm = scripted(ops=[{"type": "ADD", "section": "STRATEGIES & INSIGHTS", "content": "Narrow to the bulletin first."}])
    vectors = {"Narrow to the right bulletin first.": [1.0, 0.0], "Narrow to the bulletin first.": [0.99, 0.141],
               "Sum the monthly figures for a fiscal year.": [0.0, 1.0]}
    monkeypatch.setattr(E.ACEEvolver, "_embed", lambda self, ctx, texts: [vectors[t] for t in texts])
    E.ACEEvolver().step(context(tmp_path, adapter, llm, {"use_bulletpoint_analyzer": True}))
    book = playbook_of(adapter)
    assert "[sai-00001] helpful=0 harmful=0 :: merged guidance" in book      # first id, summed counts kept
    assert "sai-00003" not in book and "calc-00002" in book
    assert next(s for p, s in llm.seen if "merging similar" in p)["temperature"] == 0.3


def test_a_log_directory_is_read_without_its_subdirectories_and_prompts(tmp_path):
    log = tmp_path / "pred"
    (log / "codex_exec").mkdir(parents=True)
    (log / "codex_exec" / "task.md").write_text("ORACLE PAGES")
    (log / "target_system_prompt.txt").write_text("THE SKILL")
    (log / "conversation.json").write_text('[{"content": "Applying [calc-00002]"}]')
    text = E.log_text(log)
    assert "Applying [calc-00002]" in text and "ORACLE PAGES" not in text and "THE SKILL" not in text


def test_samples_are_processed_in_the_order_the_trainer_ran_them(tmp_path):
    adapter = seed(tmp_path)
    material(tmp_path, [record("UID9", 1.0), record("UID1", 1.0), record("UID5", 1.0)])
    E.ACEEvolver().step(context(tmp_path, adapter, scripted()))
    state = json.loads((tmp_path / "state" / "ace.json").read_text())
    assert [x["id"] for x in state["steps"][0]["samples"]] == ["UID9", "UID1", "UID5"]


@pytest.mark.parametrize("where", ["llm", "run_agent"])
def test_a_hard_budget_is_not_swallowed(tmp_path, where):
    from hermit.trainer.budget import BudgetExceeded

    def exhausted(**_):
        raise BudgetExceeded("usd 3 reached")
    adapter = seed(tmp_path)
    material(tmp_path, [record("UID2", 0.0)])
    llm = exhausted if where == "llm" else scripted()
    with pytest.raises(BudgetExceeded):
        E.ACEEvolver().step(context(tmp_path, adapter, llm, run_agent=exhausted))


def test_the_step_needs_the_llm_tool_and_a_playbook(tmp_path):
    adapter = seed(tmp_path)
    material(tmp_path, [record("UID1", 1.0)])
    ctx = context(tmp_path, adapter, scripted())
    no_llm = StepContext(workspace=ctx.workspace, access=ctx.access, tools={}, params=ctx.params,
                         progress=ctx.progress)
    assert E.ACEEvolver().step(no_llm).status == "failed"
    (adapter / "ADAPTER.md").write_text("plain text\n")
    assert E.ACEEvolver().step(ctx).status == "failed"


# ---------- the whole loop through the Trainer, on a toy task ----------

PIPELINE = '''
import re
from pathlib import Path
from hermit.task import RunOutput

def run(task, adapter):
    text = (Path(adapter) / "ADAPTER.md").read_text() if adapter else ""
    m = re.search(r"\\[([a-z]+-\\d{5})\\] helpful=\\d+ harmful=\\d+ :: .*RULE_" + task["kind"], text)
    reply = f"Applying [{m.group(1)}]: done" if m else "unsure"
    return RunOutput(output="done" if m else "unsure", cost_usd=0.0, tokens=0,
                     messages=[{"role": "user", "content": task["question"]}, {"role": "assistant", "content": reply}])
'''
JUDGE = '''
def score(task, output):
    ok = output.output == "done"
    return {"score": float(ok), "feedback": "CORRECT." if ok else f"INCORRECT. The answer requires RULE_{task['kind']}."}
'''


class ScriptedModel:
    """Reflects by naming the rule the feedback names; curates by adding that rule unless the playbook has it."""
    model = "scripted"

    def __init__(self):
        self.calls = 0

    def chat(self, messages, **kwargs):
        self.calls += 1
        text = messages[0]["content"]
        if "master curator" in text:
            reflection = text.split("**Recent Reflection:**", 1)[1].split("**Current Playbook:**", 1)[0]
            current = text.split("**Current Playbook:**", 1)[1].split("**Question Context:**", 1)[0]
            ops = [{"type": "ADD", "section": "STRATEGIES & INSIGHTS", "content": f"State {r}."}
                   for r in sorted(set(re.findall(r"RULE_[A-Z]+", reflection))) if r not in current]
            reply = json.dumps({"reasoning": "add the rule", "operations": ops})
        else:
            rules = sorted(set(re.findall(r"requires (RULE_[A-Z]+)", text)))
            ids = re.findall(r"\[([a-z]+-\d{5})\] helpful", text.split("used by the generator to answer the question:**")[-1])
            reply = json.dumps({"reasoning": "r", "key_insight": " ".join(rules) or "nothing new",
                                "bullet_tags": [{"id": i, "tag": "helpful"} for i in ids]})
        return {"content": reply, "finish_reason": "stop"}, Usage("scripted", 10, 0, 10, 1, 0.0)


def make_run(tmp: Path, params: dict, step_size: int = 2) -> Config:
    (tmp / "task-info").mkdir()
    (tmp / "task-info" / "README.md").write_text("Solve toy tasks; each kind has a rule.\n")
    rows = [{"id": f"t{i:02d}", "kind": KINDS[i % 4], "question": f"Toy task {i} of kind {KINDS[i % 4]}."}
            for i in range(16)]
    (tmp / "tasks.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    for name, ids in (("train", range(8)), ("val", range(8, 12)), ("test", range(12, 16))):
        (tmp / f"{name}.txt").write_text("".join(f"t{i:02d}\n" for i in ids))
    (tmp / "pipeline.py").write_text(PIPELINE)
    (tmp / "judge.py").write_text(JUDGE)
    config = {"data": {"tasks": "tasks.jsonl", "train": "train.txt", "val": "val.txt", "test": "test.txt",
                       "step_size": step_size},
              "task_info": "task-info", "agent": {"name": "pipeline.py:run", "concurrency": 2},
              "judge": {"path": "judge.py"}, "template": str(TEMPLATE),
              "evolver": {"name": "ace", "params": params},
              "sandbox": {"enabled": False, "network": "none", "env": []},
              "trainer": {"run_name": "ace-toy", "output_dir": "runs", "epochs": 2, "budget": {"hours": 1}}}
    (tmp / "run.yaml").write_text(yaml.safe_dump(config))
    return Config.load(tmp / "run.yaml")


def test_ace_learns_the_rules_through_the_trainer(tmp_path):
    config = make_run(tmp_path, {})
    trainer = Trainer(config, log=lambda *_: None)
    trainer.llm = model = ScriptedModel()
    summary = trainer.train()
    run = tmp_path / "runs" / "ace-toy"
    assert summary["steps"] == 8 and model.calls > 0
    steps = [json.loads(l) for l in (run / "steps.jsonl").read_text().splitlines()]
    assert all(s["status"] == "done" for s in steps)
    state = json.loads((run / "workspace" / "state" / "ace.json").read_text())
    assert [v["epoch"] for v in state["validations"]] == [1, 2] and state["best"]["score"] == 1.0
    final = (run / "checkout" / "ADAPTER.md").read_text()
    book = pb.split_adapter(final)[1]
    assert all(f"State RULE_{k}." in book for k in KINDS)
    assert len(pb.bullet_ids_of(book)) == 4                      # each rule added once: no duplicates
    assert any(p["helpful"] > 0 for p in map(pb.parse_playbook_line, book.split("\n")) if p)
    assert evaluate(config, "final", log=lambda *_: None)["mean_score"] == 1.0
    assert evaluate(config, "root", log=lambda *_: None)["mean_score"] == 0.0
    last = run / "workspace" / "state" / "last_adapter"
    assert evaluate(config, str(last), log=lambda *_: None)["mean_score"] == 1.0
    assert (run / "eval" / "last_adapter" / "results.jsonl").is_file()


# ---------- the user's own Codex login: the learner as `codex exec`, the acting agent's sessions ----------

FAKE_CODEX = r'''#!/usr/bin/env python3
"""A stand-in for `codex exec`: answers ACE's prompts as the scripted model does, from stdin."""
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
prompt = sys.stdin.read()
home = Path(os.environ["CODEX_HOME"])
log = Path(os.environ["FAKE_CODEX_LOG"])
with open(log, "a") as f:
    f.write(json.dumps({"args": args, "stdin_chars": len(prompt), "auth": (home / "auth.json").is_file(),
                        "auth_mode": oct((home / "auth.json").stat().st_mode & 0o777)
                        if (home / "auth.json").is_file() else None}) + "\n")
if os.environ.get("FAKE_CODEX_FAIL"):
    print("error: 401 Unauthorized", flush=True)
    sys.exit(1)
if "master curator" in prompt:
    reply = json.dumps({"reasoning": "add", "operations": [{"type": "ADD", "section": "OTHERS", "content": "via codex"}]})
else:
    reply = json.dumps({"reasoning": "r", "bullet_tags": [{"id": "calc-00002", "tag": "helpful"}]})
Path(args[args.index("-o") + 1]).write_text(reply)
print(json.dumps({"type": "thread.started", "thread_id": "t1"}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 40,
                                                      "output_tokens": 7}}))
'''


@pytest.fixture
def fake_codex(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "codex").write_text(FAKE_CODEX)
    (bin_dir / "codex").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_CODEX_LOG", str(tmp_path / "codex_calls.jsonl"))
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": {"access_token": "a"}, "last_refresh": "2026-10-01T00:00:00Z"}))
    return auth


def codex_calls(tmp_path) -> list[dict]:
    return [json.loads(l) for l in (tmp_path / "codex_calls.jsonl").read_text().splitlines()]


def test_the_codex_learner_runs_exec_under_the_login(tmp_path, fake_codex):
    adapter = seed(tmp_path)
    material(tmp_path, [record("UID1", 1.0, cites="Applying [calc-00002].")])
    ctx = context(tmp_path, adapter, llm=None, params={"learner": "codex", "learner_auth": str(fake_codex),
                                                         "learner_model": "gpt-x", "learner_effort": "medium"})
    ctx = StepContext(workspace=ctx.workspace, access=ctx.access,
                      tools={k: v for k, v in ctx.tools.items() if k != "llm"}, params=ctx.params, progress=ctx.progress)
    ev = E.ACEEvolver()
    result = ev.step(ctx)
    assert result.status == "done", result.summary
    book = playbook_of(adapter)
    assert "[calc-00002] helpful=4 harmful=1 ::" in book and ":: via codex" in book
    calls = codex_calls(tmp_path)
    assert len(calls) == 2 and all(c["auth"] and c["auth_mode"] == "0o600" for c in calls)
    args = calls[0]["args"]
    assert args[:2] == ["exec", "--json"] and args[-1] == "-" and "read-only" in args
    assert args[args.index("-m") + 1] == "gpt-x" and 'model_reasoning_effort="medium"' in args
    prompt = (tmp_path / "output" / "calls").glob("*.reflect.prompt.md").__next__().read_text()
    assert calls[0]["stdin_chars"] == len(prompt)                     # the prompt goes on stdin, not argv
    assert [(u.input_tokens, u.cached_input_tokens, u.output_tokens, u.calls) for u in result.usage] == \
        [(100, 40, 7, 1), (100, 40, 7, 1)]
    home = tmp_path / "agent_home"                                    # ws.state.parent / agent_home
    assert (home / "auth.json").is_file()
    ev.finalize(ctx)
    assert not (home / "auth.json").exists() and fake_codex.is_file()   # the run's copy goes, the user's stays


def test_a_failing_codex_learner_skips_the_call(tmp_path, fake_codex, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_FAIL", "1")
    adapter = seed(tmp_path)
    material(tmp_path, [record("UID1", 1.0)])
    before = playbook_of(adapter)
    result = E.ACEEvolver().step(context(tmp_path, adapter, scripted(),
                                         {"learner": "codex", "learner_auth": str(fake_codex)}))
    assert result.status == "done" and playbook_of(adapter) == before
    assert "learner call(s) failed" in result.summary
    errors = sorted((tmp_path / "output" / "calls").glob("*.error.txt"))
    assert errors and "401 Unauthorized" in errors[0].read_text()


def test_the_codex_learner_without_a_login_says_so(tmp_path, fake_codex):
    adapter = seed(tmp_path)
    material(tmp_path, [record("UID1", 1.0)])
    E.ACEEvolver().step(context(tmp_path, adapter, scripted(),
                                {"learner": "codex", "learner_auth": str(tmp_path / "missing.json")}))
    errors = sorted((tmp_path / "output" / "calls").glob("*.error.txt"))
    assert errors and "codex login" in errors[0].read_text()


def test_an_unknown_learner_fails_the_step(tmp_path):
    adapter = seed(tmp_path)
    material(tmp_path, [record("UID1", 1.0)])
    assert E.ACEEvolver().step(context(tmp_path, adapter, scripted(), {"learner": "gpt"})).status == "failed"


def officeqa_agent(tmp_path):
    """examples/officeqa/utils/agent/agent.py, imported from a copy beside a data.json of its own (prepare.py writes
    the real one)."""
    import importlib.util
    import sys
    utils = tmp_path / "utils"
    (utils / "agent").mkdir(parents=True)
    shutil.copy(ROOT / "examples" / "officeqa" / "utils" / "agent" / "agent.py", utils / "agent" / "agent.py")
    (utils / "data.json").write_text(json.dumps({"answers": "x", "docs": str(tmp_path / "docs")}))
    spec = importlib.util.spec_from_file_location(f"officeqa_agent_{id(tmp_path)}", utils / "agent" / "agent.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_officeqa_agent_binds_a_login_without_provider_settings(tmp_path):
    A = officeqa_agent(tmp_path)
    A._bind({"harness": "codex", "login": "chatgpt", "model": "gpt-x"})
    assert A._binding.model == "gpt-x" and A._binding.price is None
    assert A._binding.codex_args() == ["-c", 'forced_login_method = "chatgpt"']
    A._bind({"harness": "codex", "login": "chatgpt"})
    assert A._binding.model == "" and A._binding.ref == "codex-login/default"
    with pytest.raises(ValueError, match="model name"):
        A._bind({"harness": "codex", "login": "chatgpt", "model": "fireworks/glm"})
    with pytest.raises(ValueError, match="harness: codex"):
        A._harness({"login": "chatgpt"})
    with pytest.raises(ValueError, match="chatgpt or absent"):
        A._login({"login": "apikey"})
    assert A._harness({"harness": "codex"}) == "codex" and not A._login({})


@pytest.mark.parametrize("session_refresh, expect_written", [("2026-10-29T00:00:00Z", True),
                                                             ("2026-09-01T00:00:00Z", False)])
def test_a_refreshed_session_login_becomes_the_runs_copy(tmp_path, session_refresh, expect_written):
    A = officeqa_agent(tmp_path)
    params = {"codex_dir": str(tmp_path / "codex")}
    master = A._login_master(params)
    master.parent.mkdir(parents=True)
    master.write_text(json.dumps({"tokens": {"refresh_token": "old"}, "last_refresh": "2026-10-01T00:00:00Z"}))
    home = tmp_path / "home"
    home.mkdir()
    (home / "auth.json").write_text(json.dumps({"tokens": {"refresh_token": "new"}, "last_refresh": session_refresh}))
    A._keep_refreshed(params, home)
    written = json.loads(master.read_text())["tokens"]["refresh_token"] == "new"
    assert written == expect_written
    if written:
        assert oct(master.stat().st_mode & 0o777) == "0o600"


@pytest.mark.parametrize("user_refresh, expect_copied", [("2026-10-29T00:00:00Z", True),
                                                         ("2026-09-01T00:00:00Z", False)])
def test_a_newer_user_login_replaces_the_runs_copy(tmp_path, user_refresh, expect_copied):
    A = officeqa_agent(tmp_path)
    master = A._login_master({"codex_dir": str(tmp_path / "codex")})
    master.parent.mkdir(parents=True)
    master.write_text(json.dumps({"tokens": {"refresh_token": "run"}, "last_refresh": "2026-10-01T00:00:00Z"}))
    source = tmp_path / "auth.json"
    source.write_text(json.dumps({"tokens": {"refresh_token": "user"}, "last_refresh": user_refresh}))
    A._copy_login(source, master)
    copied = json.loads(master.read_text())["tokens"]["refresh_token"] == "user"
    assert copied == expect_copied
    if copied:
        assert oct(master.stat().st_mode & 0o777) == "0o600"
    A._copy_login(source, tmp_path / "codex" / "login" / "missing.json")
    assert (tmp_path / "codex" / "login" / "missing.json").is_file()


def test_each_codex_session_home_gets_the_login(tmp_path):
    import sys
    sys.path.insert(0, str(ROOT / "examples" / "officeqa" / "utils" / "agent"))
    import codex_sandbox as S
    auth = tmp_path / "auth.json"
    auth.write_text('{"tokens": {}}')
    home = tmp_path / "rollout" / "codex_home"
    home.parent.mkdir()
    S.prepare_home(home, 'forced_login_method = "chatgpt"\n', str(auth))
    assert (home / "auth.json").read_text() == '{"tokens": {}}' and oct((home / "auth.json").stat().st_mode & 0o777) == "0o600"
    assert 'forced_login_method = "chatgpt"' in (home / "config.toml").read_text()
    (home / "auth.json").write_text('{"refreshed": true}')
    S.prepare_home(home, "", str(auth))                       # a later session keeps what Codex refreshed
    assert (home / "auth.json").read_text() == '{"refreshed": true}'
    other = tmp_path / "rollout2" / "codex_home"
    other.parent.mkdir()
    S.prepare_home(other, "", None)                          # without a login: no auth.json, as before
    assert not (other / "auth.json").exists()
