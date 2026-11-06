"""OPRO: the meta-prompt port (against the authors' gen_meta_prompt), the step with a scripted model and a scripted
run_agent (history, exemplars, de-duplication, filters, scoring on every training task, the hand-back), finalize,
and the whole loop through the Trainer on a toy task (no model, no network)."""
import dataclasses
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading

import pytest
import yaml

from hermit.agent.evolver.base import Access, Progress, StepContext, Usage, Workspace
from hermit.agent.evolver.methods.opro import evolver as E
from hermit.agent.evolver.methods.opro import meta_prompt as mp
from hermit.config import Config
from hermit.evaluate import evaluate
from hermit.spec import check_spec
from hermit.trainer.trainer import Trainer
from fakes import agent_of

ROOT = Path(__file__).resolve().parents[3]
TEMPLATE = ROOT / "hermit" / "adapter" / "templates" / "prompt_only"
DEFAULTS = yaml.safe_load((ROOT / "hermit" / "agent" / "evolver" / "methods" / "opro" / "evolver.yaml").read_text())


@pytest.fixture
def tmp_path():
    """A directory outside /tmp: the template check runs under bubblewrap with an empty tmpfs at /tmp."""
    base = Path.home() / ".cache" / "hermit-tests"
    base.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(dir=base))
    yield path
    if not os.environ.get("KEEP_TEST_DIRS"):
        shutil.rmtree(path, ignore_errors=True)


# ---------- the port ----------

# opt_utils.gen_meta_prompt(HISTORY, "Q_begin", "gpt-4", old_instruction_score_threshold=0.0,
# max_num_instructions=20, few_shot_qa_pairs=True, include_qa=False, data=<the two exemplars as BBH rows>,
# few_shot_index_list=[0, 1], instructions_before_exemplars=True, num_score_buckets=100, dataset_name="bbh") at
# google-deepmind/opro a76bdce.
HISTORY = [("Let's solve the problem.", 0.31, -1), ("Read the table **carefully**.", 0.5, 0), ("Low one.", 0.02, 0)]
EXEMPLARS = [("Q one?", "A1"), ("Q two?", "predicted 'x' but expected 'y'")]
UPSTREAM = """Your task is to generate the instruction <INS>. Below are some previous instructions with their scores. \
The score ranges from 0 to 100.

text:
Low one.
score:
2

text:
Let's solve the problem.
score:
31

text:
Read the table **carefully**.
score:
50


Below are some problems.

Problem:
<INS>
Q one?

Ground truth answer:
A1

Problem:
<INS>
Q two?

Ground truth answer:
predicted 'x' but expected 'y'


Generate an instruction that is different from all the instructions <INS> above, and has a higher score than all \
the instructions <INS> above. The instruction should begin with <INS> and end with </INS>. The instruction should be \
concise, effective, and generally applicable to all problems above."""


def test_the_meta_prompt_is_the_authors():
    assert mp.gen_meta_prompt(HISTORY, "Q_begin", 0.0, 20, True, False, EXEMPLARS, True, 100) == UPSTREAM


def test_threshold_and_max_num_instructions_keep_the_best_ascending():
    text = mp.gen_meta_prompt(HISTORY, "Q_begin", 0.3, 2, False, False, (), True, 100)
    assert re.findall(r"text:\n(.*)\nscore:\n(\d+)", text) == [("Let's solve the problem.", "31"),
                                                                ("Read the table **carefully**.", "50")]
    assert "Below are some problems" not in text


@pytest.mark.parametrize("raw, ins", [("Sure. <INS>Check units.</INS> Done", "Check units."),
                                      ("<INS>\nNo end tag", "No end tag"), ("No tags at all", "No tags at all"),
                                      ("Text</INS> trailing", "Text")])
def test_the_instruction_is_taken_from_the_reply_as_upstream_parses_it(raw, ins):
    assert mp.extract_instruction(raw) == ins


def test_an_instruction_is_polished_and_named_by_its_md5_as_upstream():
    assert mp.polish_sentence("  read **all** tables ") == "Read all tables"
    assert mp.instruction_to_filename("Prüfe die Einheiten") == mp.instruction_to_filename("Prüfe die Einheiten")
    assert mp.instruction_to_filename("a") != mp.instruction_to_filename("b")


# ---------- the step, with a scripted model and run_agent ----------

TRAIN = ["UID1", "UID2", "UID3", "UID4"]


def seed(tmp: Path, text: str = "# OfficeQA skill\n\nFind the table first.\n") -> Path:
    adapter = tmp / "adapter"
    shutil.copytree(TEMPLATE, adapter)
    (adapter / "ADAPTER.md").write_text(text)
    return adapter


def material(tmp: Path, scores: dict) -> None:
    root = tmp / "material"
    if root.exists():
        shutil.rmtree(root)
    step = root / "step_0001"
    (step / "tasks").mkdir(parents=True)
    for tid, s in scores.items():
        rec = {"id": tid, "input": {"id": tid, "question": f"What were receipts in {tid}?"},
               "output": {"answer": f"answer-{tid}", "fail_reason": ""}, "status": "ok",
               "judge": {"score": s, "feedback": "correct" if s else f"predicted 'answer-{tid}' but expected 'gold-{tid}'"},
               "trajectory": None}
        (step / "tasks" / f"{tid}.json").write_text(json.dumps(rec))
    (root / "current").symlink_to("step_0001")


def scripted(replies: list[str]):
    seen = []

    def llm(messages, **sampling):
        seen.append((messages[0]["content"], sampling))
        return {"content": replies[(len(seen) - 1) % len(replies)]}
    llm.seen = seen
    return llm


def run_agent_by(rule):
    """Scores a candidate by `rule(ADAPTER.md text, task id)`; records every call."""
    calls = []

    def run_agent(task_ids, adapter):
        text = (Path(adapter) / "ADAPTER.md").read_text()
        calls.append((list(task_ids), text))
        return {"results": [{"id": t, "status": "ok", "judge": {"score": float(rule(text, t))}} for t in task_ids]}
    run_agent.calls = calls
    return run_agent


def context(tmp: Path, adapter: Path, llm, params: dict | None = None, run_agent=None, validation: Path | None = None,
            step: int = 1) -> StepContext:
    ws = Workspace(adapter=adapter, learning_material=tmp / "material", task_info=tmp / "task-info",
                   history=tmp / "history", state=tmp / "state", scratch=tmp / "scratch", output=tmp / f"output{step}")
    for d in (ws.state, ws.scratch, ws.output, ws.task_info):
        d.mkdir(parents=True, exist_ok=True)

    def check_spec_tool(path: str) -> dict:
        result = check_spec(Path(path), TEMPLATE, deploy=False)
        return {"passed": result.passed, "issues": [i.__dict__ for i in result.issues]}

    tools = {"check_spec": check_spec_tool}
    if run_agent:
        tools["run_agent"] = run_agent
    return StepContext(workspace=ws, access=Access(validation=validation), tools=tools, agent=agent_of(llm),
                       params={**DEFAULTS["params"], **(params or {})}, progress=Progress(step, step, True))


def test_the_first_step_scores_the_seed_and_new_instructions_on_every_training_task(tmp_path):
    adapter = seed(tmp_path)
    material(tmp_path, {"UID1": 1.0, "UID2": 0.0, "UID3": 0.0, "UID4": 1.0})
    llm = scripted(["<INS>Check the **units**.</INS>", "<INS>check the units.</INS>", "<INS>Use <INS> tags</INS>",
                    "Report in millions."])
    run_agent = run_agent_by(lambda text, t: "millions" in text or t == "UID1")
    result = E.OPROEvolver().step(context(tmp_path, adapter, llm, {"n_candidates": 4, "num_exemplars": 2},
                                          run_agent=run_agent))
    assert result.status == "done"
    state = json.loads((tmp_path / "state" / "opro.json").read_text())
    # The seed is the initial instruction, scored by the step's rollouts, at step -1.
    assert state["history"][0] == ["# OfficeQA skill\n\nFind the table first.", 0.5, -1]
    # The meta-prompt: OPRO's, with the seed at 50 and two training problems.
    meta, sampling = llm.seen[0]
    assert meta.startswith("Your task is to generate the instruction <INS>.")
    assert "text:\n# OfficeQA skill\n\nFind the table first.\nscore:\n50\n" in meta
    assert meta.count("Problem:\n<INS>\nWhat were receipts in") == 2
    shown = state["steps"][0]["exemplars"]
    for tid in shown:
        answer = f"answer-{tid}" if tid in ("UID1", "UID4") else f"predicted 'answer-{tid}' but expected 'gold-{tid}'"
        assert f"What were receipts in {tid}?\n\nGround truth answer:\n{answer}\n" in meta
    assert sampling == {"temperature": 1.0} and len(llm.seen) == 4
    # Polished ("**" removed, capitalised), so the second reply is a duplicate; "INS" is filtered.
    assert state["steps"][0]["skipped"] == ["already evaluated", "contains 'INS'"]
    assert [c[1] for c in run_agent.calls] == ["Check the units.\n", "Report in millions.\n"]
    assert all(c[0] == TRAIN for c in run_agent.calls)
    assert [h[1:] for h in state["history"]] == [[0.5, -1], [0.25, 0], [1.0, 0]]
    # The best on training is handed back.
    assert (adapter / "ADAPTER.md").read_text() == "Report in millions.\n"
    assert (adapter / "exports.yaml").exists()
    assert (tmp_path / "output1" / "meta_prompt.md").read_text() == meta
    assert (tmp_path / "output1" / "reply.3.txt").read_text() == "Report in millions."


def test_later_steps_add_to_the_history_and_skip_what_was_evaluated(tmp_path):
    adapter = seed(tmp_path)
    material(tmp_path, dict.fromkeys(TRAIN, 0.0))
    run_agent = run_agent_by(lambda text, t: "millions" in text and t != "UID4")
    E.OPROEvolver().step(context(tmp_path, adapter, scripted(["<INS>Report in millions.</INS>"]),
                                 {"n_candidates": 1}, run_agent=run_agent))
    material(tmp_path, {"UID1": 1.0, "UID2": 1.0, "UID3": 0.0, "UID4": 0.0})      # the rollouts re-score the best
    llm = scripted(["<INS>Report in millions.</INS>", "<INS>Name the fiscal year.</INS>"])
    E.OPROEvolver().step(context(tmp_path, adapter, llm, {"n_candidates": 2}, run_agent=run_agent, step=2))
    state = json.loads((tmp_path / "state" / "opro.json").read_text())
    assert "text:\nReport in millions.\nscore:\n75\n" in llm.seen[0][0]
    assert state["steps"][1]["rescore"] == 0.5 and state["steps"][1]["skipped"] == ["already evaluated"]
    assert [h[2] for h in state["history"]] == [-1, 0, 1]
    assert (adapter / "ADAPTER.md").read_text() == "Report in millions.\n"         # still the best


def test_exemplars_change_with_the_step_and_repeat_with_the_seed(tmp_path):
    picks = []
    for step in (1, 2, 1):
        sub = tmp_path / f"s{len(picks)}"
        sub.mkdir()
        adapter = seed(sub)
        material(sub, {f"UID{i}": 0.0 for i in range(20)})
        E.OPROEvolver().step(context(sub, adapter, scripted(["<INS>x</INS>"]), {"n_candidates": 1},
                                     run_agent=run_agent_by(lambda *_: 0), step=step))
        picks.append(json.loads((sub / "state" / "opro.json").read_text())["steps"][0]["exemplars"])
    assert picks[0] == picks[2] != picks[1] and len(picks[0]) == 3


def test_long_instructions_are_skipped_only_with_a_limit(tmp_path):
    adapter = seed(tmp_path)
    material(tmp_path, dict.fromkeys(TRAIN, 0.0))
    E.OPROEvolver().step(context(tmp_path, adapter, scripted(["<INS>" + "x" * 600 + "</INS>"]),
                                 {"n_candidates": 1, "max_instruction_chars": 500}, run_agent=run_agent_by(lambda *_: 0)))
    state = json.loads((tmp_path / "state" / "opro.json").read_text())
    assert state["steps"][0]["skipped"] == ["longer than 500 characters"] and len(state["candidates"]) == 1


def test_a_candidate_failing_the_check_is_not_run(tmp_path):
    adapter = seed(tmp_path)
    material(tmp_path, dict.fromkeys(TRAIN, 0.0))
    run_agent = run_agent_by(lambda *_: 1)
    E.OPROEvolver().step(context(tmp_path, adapter, scripted(["<INS>" + "y" * 50000 + "</INS>"]),
                                 {"n_candidates": 1}, run_agent=run_agent))
    state = json.loads((tmp_path / "state" / "opro.json").read_text())
    assert "rejected" in state["steps"][0]["evaluated"][0] and not run_agent.calls


def test_a_failed_optimizer_call_is_recorded_and_a_hard_budget_is_not_swallowed(tmp_path):
    from hermit.trainer.budget import BudgetExceeded
    adapter = seed(tmp_path)
    material(tmp_path, dict.fromkeys(TRAIN, 0.0))

    def flaky(messages, **_):
        raise RuntimeError("endpoint down")
    result = E.OPROEvolver().step(context(tmp_path, adapter, flaky, {"n_candidates": 2},
                                          run_agent=run_agent_by(lambda *_: 0)))
    assert result.status == "done" and "2 optimizer calls failed" in result.summary

    def exhausted(**_):
        raise BudgetExceeded("usd 3 reached")
    with pytest.raises(BudgetExceeded):
        E.OPROEvolver().step(context(tmp_path, adapter, scripted(["<INS>New.</INS>"]), {"n_candidates": 1},
                                     run_agent=exhausted, step=2))


def validation(tmp: Path, ids) -> Path:
    val = tmp / "val"
    val.mkdir(exist_ok=True)
    (val / "tasks.jsonl").write_text("".join(json.dumps({"id": i}) + "\n" for i in ids))
    return val


def test_finalize_selects_on_validation_among_the_top_k_on_training(tmp_path):
    adapter = seed(tmp_path)
    material(tmp_path, dict.fromkeys(TRAIN, 0.0))
    train_rule = {"A.": 3, "B.": 2, "C.": 1, "D.": 4}
    val_rule = {"A.": 0, "B.": 1, "C.": 0, "D.": 0}

    def run_agent(task_ids, adapter):
        text = (Path(adapter) / "ADAPTER.md").read_text().strip()
        n = (val_rule if task_ids[0].startswith("V") else train_rule)[text]
        return {"results": [{"id": t, "judge": {"score": float(i < n)}} for i, t in enumerate(task_ids)]}
    llm = scripted([f"<INS>{x}.</INS>" for x in "ABCD"])
    val = validation(tmp_path, ["V1", "V2"])
    ctx = context(tmp_path, adapter, llm, {"n_candidates": 4, "top_k": 3}, run_agent=run_agent, validation=val)
    E.OPROEvolver().step(ctx)
    assert (adapter / "ADAPTER.md").read_text() == "D.\n"                       # the best on training
    final = E.OPROEvolver().finalize(ctx)
    assert (adapter / "ADAPTER.md").read_text() == "B.\n" and "top 3" in final.summary
    state = json.loads((tmp_path / "state" / "opro.json").read_text())
    assert [("val" in c) for c in state["candidates"]] == [False, True, True, False, True]
    ctx_train = context(tmp_path, adapter, llm, {"select": "train"}, run_agent=run_agent)
    E.OPROEvolver().finalize(ctx_train)
    assert (adapter / "ADAPTER.md").read_text() == "D.\n"


def test_the_step_needs_the_model(tmp_path):
    adapter = seed(tmp_path)
    material(tmp_path, dict.fromkeys(TRAIN, 0.0))
    ctx = context(tmp_path, adapter, scripted(["x"]))
    no_llm = StepContext(workspace=ctx.workspace, access=ctx.access, tools=ctx.tools, params=ctx.params,
                         progress=ctx.progress)
    assert E.OPROEvolver().step(no_llm).status == "failed"


# ---------- the whole loop through the Trainer, on a toy task ----------

PIPELINE = '''
from pathlib import Path
from hermit.task import RunOutput

def run(task, adapter):
    text = (Path(adapter) / "ADAPTER.md").read_text() if adapter else ""
    ok = ("RULE_" + task["kind"]) in text
    return RunOutput(output="done" if ok else "unsure", cost_usd=0.0, tokens=0)
'''
JUDGE = '''
def score(task, output):
    ok = output.output == "done"
    return {"score": float(ok), "feedback": "correct" if ok else f"expected the answer under RULE_{task['kind']}"}
'''
KINDS = ("ALPHA", "BETA", "GAMMA", "DELTA")


class ScriptedOptimizer:
    """Adds to the best instruction shown one rule that the exemplars' feedback names and it lacks."""
    model = "scripted"

    def __init__(self):
        self.prompts = []

    def chat(self, messages, **kwargs):
        text = messages[0]["content"]
        self.prompts.append(text)
        olds = re.findall(r"text:\n(.*?)\nscore:\n(\d+)\n", text, re.S)
        best = olds[-1][0]
        missing = [r for r in sorted(set(re.findall(r"RULE_[A-Z]+", text.split("Below are some problems.")[1])))
                   if r not in best]
        new = f"{best} Apply {missing[len(self.prompts) % len(missing)]}." if missing else best + " Again."
        return {"content": f"<INS>{new}</INS>", "finish_reason": "stop"}, Usage("scripted", 10, 0, 10, 1, 0.0)


def make_run(tmp: Path, params: dict) -> Config:
    (tmp / "task-info").mkdir()
    (tmp / "task-info" / "README.md").write_text("Solve toy tasks; each kind has a rule.\n")
    rows = [{"id": f"t{i:02d}", "kind": KINDS[i % 4], "question": f"Toy task {i} of kind {KINDS[i % 4]}."}
            for i in range(16)]
    (tmp / "tasks.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    for name, ids in (("train", range(8)), ("val", range(8, 12)), ("test", range(12, 16))):
        (tmp / f"{name}.txt").write_text("".join(f"t{i:02d}\n" for i in ids))
    (tmp / "pipeline.py").write_text(PIPELINE)
    (tmp / "judge.py").write_text(JUDGE)
    start = tmp / "start"
    shutil.copytree(TEMPLATE, start)
    (start / "ADAPTER.md").write_text("Solve the task.\n")
    config = {"data": {"tasks": "tasks.jsonl", "train": "train.txt", "val": "val.txt", "test": "test.txt",
                       "step_size": 8},
              "task_info": "task-info", "agent": {"name": "pipeline.py:run", "concurrency": 2},
              "judge": {"path": "judge.py"}, "template": str(TEMPLATE), "start_from": "start",
              "evolver": {"name": "opro", "params": params},
              "sandbox": {"enabled": False, "network": "none", "env": []},
              "trainer": {"run_name": "opro-toy", "output_dir": "runs", "epochs": 4, "budget": {"hours": 1}}}
    (tmp / "run.yaml").write_text(yaml.safe_dump(config))
    return Config.load(tmp / "run.yaml")


def test_opro_learns_the_rules_through_the_trainer(tmp_path):
    config = make_run(tmp_path, {"n_candidates": 2, "num_exemplars": 4})
    trainer = Trainer(config, log=lambda *_: None)
    trainer.llm = model = ScriptedOptimizer()
    summary = trainer.train()
    run = tmp_path / "runs" / "opro-toy"
    assert summary["steps"] == 4 and len(model.prompts) == 8
    steps = [json.loads(l) for l in (run / "steps.jsonl").read_text().splitlines()]
    assert all(s["status"] == "done" for s in steps)
    state = json.loads((run / "workspace" / "state" / "opro.json").read_text())
    assert state["history"][0] == ["Solve the task.", 0.0, -1] and state["train_ids"] == [f"t{i:02d}" for i in range(8)]
    assert max(h[1] for h in state["history"]) == 1.0 and "final" in state
    final = (run / "checkout" / "ADAPTER.md").read_text()
    assert all(f"RULE_{k}" in final for k in KINDS)
    assert evaluate(config, "final", log=lambda *_: None)["mean_score"] == 1.0
    assert evaluate(config, "root", log=lambda *_: None)["mean_score"] == 0.0


@pytest.mark.parametrize("params, model, error", [({"select": "best"}, ScriptedOptimizer, "unknown select"),
                                                  ({}, None, "needs the run's model")])
def test_a_misconfigured_run_stops_before_its_first_rollouts(tmp_path, params, model, error):
    """The checks are in setup: a run without `llm` or with an unknown `select` spends no training pass (the step's
    own checks cover a resume)."""
    config = make_run(tmp_path, params)
    trainer = Trainer(config, log=lambda *_: None)
    trainer.llm = model() if model else None
    with pytest.raises(SystemExit, match=error):
        trainer.train()
    assert not list((tmp_path / "runs" / "opro-toy").glob("step_*"))


def test_the_problem_is_the_named_field_or_the_task():
    task = {"id": "UID1", "question": "Q?", "source_files": ["a.txt"]}
    assert E.task_text(task, "question") == "Q?"
    assert json.loads(E.task_text(task)) == {"question": "Q?", "source_files": ["a.txt"]}


# ---------- failures ----------

def mark_failed(tmp: Path, tid: str) -> None:
    f = tmp / "material" / "step_0001" / "tasks" / f"{tid}.json"
    rec = json.loads(f.read_text())
    rec.update(status="error", output=None, judge={"score": 0.0, "feedback": "the pipeline failed: ContainerError"})
    f.write_text(json.dumps(rec))


@pytest.mark.parametrize("progress", [Progress(1, 1, False), Progress(1, 3, True)])
def test_the_first_step_refuses_a_step_size_smaller_than_the_training_split(tmp_path, progress):
    # (1, 1, False): the first of several chunks; (1, 3, True): the last, short chunk of the first epoch.
    adapter = seed(tmp_path)
    material(tmp_path, dict.fromkeys(TRAIN, 1.0))
    ctx = context(tmp_path, adapter, scripted(["<INS>x.</INS>"]), run_agent=run_agent_by(lambda *_: 1))
    result = E.OPROEvolver().step(dataclasses.replace(ctx, progress=progress))
    assert result.status == "failed" and "step_size" in result.summary
    assert not (tmp_path / "state" / "opro.json").exists()


def test_failed_rollouts_never_score_the_initial_instruction_or_become_exemplars(tmp_path):
    adapter = seed(tmp_path)
    material(tmp_path, dict.fromkeys(TRAIN, 1.0))
    mark_failed(tmp_path, "UID2")
    run_agent = run_agent_by(lambda *_: 1)
    llm = scripted(["<INS>x.</INS>"])
    result = E.OPROEvolver().step(context(tmp_path, adapter, llm, {"n_candidates": 1}, run_agent=run_agent))
    state_dir = tmp_path / "state"
    state = json.loads((state_dir / "opro.json").read_text())
    # The failed initial run is run again (alone, on the starting adapter), so the initial score is 1.0, not 0.75.
    assert result.status == "done" and run_agent.calls[0] == (["UID2"], "# OfficeQA skill\n\nFind the table first.\n")
    assert state["history"][0][1] == 1.0 and state["steps"][0]["rerun"] == ["UID2"]
    assert "UID2" not in state["steps"][0]["exemplars"] and "the pipeline failed" not in llm.seen[0][0]
    # Later steps (re-scores) only skip the failed record as an exemplar.
    for step in range(2, 8):
        llm = scripted(["<INS>Step %d.</INS>" % step])
        E.OPROEvolver().step(context(tmp_path, adapter, llm, {"n_candidates": 1, "num_exemplars": 3},
                                     run_agent=run_agent_by(lambda *_: 1), step=step))
        assert "the pipeline failed" not in llm.seen[0][0]
    assert all("UID2" not in s["exemplars"] for s in json.loads((state_dir / "opro.json").read_text())["steps"][1:])


def test_an_infrastructure_failure_is_run_again_and_never_scored_as_zero(tmp_path):
    adapter = seed(tmp_path)
    material(tmp_path, dict.fromkeys(TRAIN, 0.0))
    calls = []

    def run_agent(task_ids, adapter):
        text = (Path(adapter) / "ADAPTER.md").read_text()
        calls.append((list(task_ids), text))
        down = "Always down" in text or ("Once down" in text and len([c for c in calls if c[1] == text]) == 1)
        return {"results": [{"id": t, "status": "error" if down and t == "UID3" else "ok",
                             "judge": {"score": 0.0 if down and t == "UID3" else 1.0}} for t in task_ids]}
    llm = scripted(["<INS>Once down.</INS>", "<INS>Always down.</INS>"])
    result = E.OPROEvolver().step(context(tmp_path, adapter, llm, {"n_candidates": 2}, run_agent=run_agent))
    state = json.loads((tmp_path / "state" / "opro.json").read_text())
    # "Once down": UID3 run again alone, then scored 1.0 on all four.
    assert calls[1] == (["UID3"], "Once down.\n")
    assert state["candidates"][1] == {"text": "Once down.", "step": 0, "train": dict.fromkeys(TRAIN, 1.0)}
    # "Always down": not scored, not in the history, its md5 not recorded (it may be generated again).
    assert [c["text"] for c in state["candidates"]] == ["# OfficeQA skill\n\nFind the table first.", "Once down."]
    assert "failed twice" in state["steps"][0]["evaluated"][1]["failed"]
    assert mp.instruction_to_filename("Always down.") not in state["hashes"]
    assert result.status == "done"


def test_a_failed_optimizer_call_is_tried_once_more(tmp_path):
    adapter = seed(tmp_path)
    material(tmp_path, dict.fromkeys(TRAIN, 0.0))
    calls = []

    def flaky_once(messages, **_):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("endpoint hiccup")
        return {"content": "<INS>Second try.</INS>"}
    result = E.OPROEvolver().step(context(tmp_path, adapter, flaky_once, {"n_candidates": 1},
                                          run_agent=run_agent_by(lambda *_: 1)))
    state = json.loads((tmp_path / "state" / "opro.json").read_text())
    assert len(calls) == 2 and state["candidates"][-1]["text"] == "Second try."
    assert "failed" not in result.summary and len(state["steps"][0]["errors"]) == 1


def test_the_budget_keeps_the_instructions_already_scored(tmp_path):
    from hermit.trainer.budget import BudgetExceeded
    adapter = seed(tmp_path)
    material(tmp_path, dict.fromkeys(TRAIN, 0.0))
    inner = run_agent_by(lambda *_: 1)

    def run_agent(task_ids, adapter):
        if "Second" in (Path(adapter) / "ADAPTER.md").read_text():
            raise BudgetExceeded("hard budget")
        return inner(task_ids, adapter)
    with pytest.raises(BudgetExceeded):
        E.OPROEvolver().step(context(tmp_path, adapter, scripted(["<INS>First.</INS>", "<INS>Second.</INS>"]),
                                     {"n_candidates": 2}, run_agent=run_agent))
    state = json.loads((tmp_path / "state" / "opro.json").read_text())
    assert [c["text"] for c in state["candidates"]][1:] == ["First."]


def test_finalize_after_the_budget_keeps_the_training_best(tmp_path):
    from hermit.trainer.budget import BudgetExceeded
    adapter = seed(tmp_path)
    material(tmp_path, dict.fromkeys(TRAIN, 0.0))
    E.OPROEvolver().step(context(tmp_path, adapter, scripted(["<INS>Report in millions.</INS>", "<INS>Other.</INS>"]),
                                 {"n_candidates": 2}, run_agent=run_agent_by(lambda t, _: "millions" in t)))
    calls = []

    def broke(task_ids, adapter):
        calls.append(1)
        raise BudgetExceeded("hard budget")
    final = E.OPROEvolver().finalize(context(tmp_path, adapter, scripted(["x"]), {"top_k": 3}, run_agent=broke,
                                             validation=validation(tmp_path, ["V1"])))
    assert len(calls) == 1 and "budget ran out" in final.summary
    assert (adapter / "ADAPTER.md").read_text() == "Report in millions.\n"


def test_the_initial_instruction_is_not_deduplicated_as_upstream(tmp_path):
    adapter = seed(tmp_path, "Find the table first.\n")
    material(tmp_path, dict.fromkeys(TRAIN, 0.0))
    run_agent = run_agent_by(lambda *_: 0)
    E.OPROEvolver().step(context(tmp_path, adapter, scripted(["<INS>Find the table first.</INS>"]),
                                 {"n_candidates": 1}, run_agent=run_agent))
    assert [c[1] for c in run_agent.calls] == ["Find the table first.\n"]


def test_a_stop_inside_run_agent_keeps_what_was_scored(tmp_path):
    adapter = seed(tmp_path)
    material(tmp_path, dict.fromkeys(TRAIN, 0.0))
    cancel = threading.Event()
    inner = run_agent_by(lambda *_: 1)

    def run_agent(task_ids, adapter):
        if "Second" in (Path(adapter) / "ADAPTER.md").read_text():
            cancel.set()
            raise RuntimeError("the call was stopped by the Trainer (budget or timeout)")
        return inner(task_ids, adapter)
    ctx = context(tmp_path, adapter, scripted(["<INS>First.</INS>", "<INS>Second.</INS>"]), {"n_candidates": 2},
                  run_agent=run_agent)
    result = E.OPROEvolver().step(dataclasses.replace(ctx, cancel=cancel))
    state = json.loads((tmp_path / "state" / "opro.json").read_text())
    assert result.status == "failed" and state["steps"][0]["outcome"] == "stopped by the Trainer"
    assert [c["text"] for c in state["candidates"]][1:] == ["First."]
    assert mp.instruction_to_filename("Second.") not in state["hashes"]


def test_unrounded_scores_with_num_score_buckets_null(tmp_path):
    adapter = seed(tmp_path)
    material(tmp_path, {"UID1": 1.0, "UID2": 0.0, "UID3": 0.0, "UID4": 0.0})
    llm = scripted(["<INS>x.</INS>"])
    E.OPROEvolver().step(context(tmp_path, adapter, llm, {"n_candidates": 1, "num_score_buckets": None},
                                 run_agent=run_agent_by(lambda *_: 0)))
    assert "score:\n0.25\n" in llm.seen[0][0]


def test_an_unknown_select_is_refused_before_any_call(tmp_path):
    adapter = seed(tmp_path)
    material(tmp_path, {"t1": 1.0})
    calls = []
    result = E.OPROEvolver().step(context(tmp_path, adapter, scripted(["<INS>x</INS>"]), {"select": "best"},
                                          run_agent=lambda **kw: calls.append(kw)))
    assert result.status == "failed" and "unknown select" in result.summary and not calls
