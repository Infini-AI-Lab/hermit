"""The GEPA and SkillOpt Evolvers end to end through the Trainer on a toy task, with a scripted model (no network).

A task of kind K is solved when the adapter's ADAPTER.md names RULE_K; the judge's feedback names the missing rule.
The scripted model answers each optimizer prompt the way the optimizer asks (GEPA's fenced instruction block,
SkillOpt's JSON stages) by adding the rules the feedback named.
"""
import json
from pathlib import Path
import re

import pytest
import yaml

from harnessadapter.config import Config
from harnessadapter.evaluate import evaluate
from harnessadapter.evolver.base import Usage
from harnessadapter.trainer.trainer import Trainer

TEMPLATE = Path(__file__).resolve().parents[1] / "harnessadapter" / "algorithms" / "templates" / "prompt_only"
KINDS = ("ALPHA", "BETA", "GAMMA", "DELTA")

PIPELINE = '''
from pathlib import Path
from harnessadapter.task import RunOutput

def run(task, adapter):
    text = (Path(adapter) / "ADAPTER.md").read_text() if adapter else ""
    answer = "done" if f"RULE_{task['kind']}" in text else "unsure"
    messages = [{"role": "user", "content": task["question"]},
                {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "read", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "c1", "content": f"{len(text)} characters"},
                {"role": "assistant", "content": answer}]
    return RunOutput(output=answer, messages=messages, cost_usd=0.0, tokens=0)
'''
JUDGE = '''
def score(task, output):
    ok = output.output == "done"
    return {"score": float(ok), "feedback": "CORRECT." if ok else f"INCORRECT. The adapter requires RULE_{task['kind']}."}
'''


def scripted(text: str) -> str:
    """One deterministic answer per optimizer stage."""
    rules = sorted(set(re.findall(r"requires (RULE_[A-Z]+)", text)) | set(re.findall(r'"content":\s*"- (RULE_[A-Z]+)', text)))
    if "I provided an assistant with the following instructions" in text:          # GEPA reflection
        current = text.split("```", 2)[1]
        return "```\n" + current.strip() + "".join(f"\n- {r}" for r in rules if r not in current) + "\n```"
    if "selected_indices" in text:                                                 # SkillOpt ranking
        return json.dumps({"reasoning": "all", "selected_indices": list(range(8))})
    if "slow_update_content" in text:
        return json.dumps({"reasoning": "keep", "slow_update_content": "Keep the rules that helped."})
    if "meta_skill_content" in text:
        return json.dumps({"reasoning": "keep", "meta_skill_content": "Prefer rules named by feedback."})
    edits = [{"op": "append", "content": f"- {r}", "support_count": 1, "source_type": "failure"} for r in rules]
    if "failure_summary" in text:                                                  # SkillOpt error analyst
        return json.dumps({"batch_size": 1, "failure_summary": [], "patch": {"reasoning": "rules", "edits": edits}})
    if "success_patterns" in text:
        return json.dumps({"batch_size": 1, "success_patterns": [], "patch": {"reasoning": "none", "edits": []}})
    return json.dumps({"reasoning": "merged", "edits": edits})                    # SkillOpt merges


class ScriptedModel:
    model = "scripted"

    def __init__(self):
        self.calls = 0

    def chat(self, messages, **kwargs):
        self.calls += 1
        text = "\n".join(str(m.get("content") or "") for m in messages)
        return {"content": scripted(text), "finish_reason": "stop"}, Usage("scripted", 10, 0, 10, 1, 0.0)


def make_run(tmp: Path, evolver: str, params: dict) -> Config:
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
                       "step_size": 4},
              "task_info": "task-info", "agent": {"name": "pipeline.py:run", "concurrency": 4},
              "judge": {"path": "judge.py"}, "template": str(TEMPLATE),
              "evolver": {"name": evolver, "params": params},
              "sandbox": {"enabled": False, "network": "none", "env": []},
              "trainer": {"run_name": f"{evolver}-toy", "output_dir": "runs", "epochs": 2,
                          "budget": {"hours": 1}}}
    (tmp / "run.yaml").write_text(yaml.safe_dump(config))
    return Config.load(tmp / "run.yaml")


def train(config: Config) -> tuple[dict, ScriptedModel]:
    trainer = Trainer(config, log=lambda *_: None)
    trainer.llm = model = ScriptedModel()
    return trainer.train(), model


def test_gepa_learns_the_rules(tmp_path):
    pytest.importorskip("gepa")
    config = make_run(tmp_path, "gepa", {"candidate_selection_strategy": "current_best"})
    summary, model = train(config)
    run = tmp_path / "runs" / "gepa-toy"
    assert summary["steps"] == 4 and model.calls > 0
    state = json.loads((run / "workspace" / "state" / "gepa.json").read_text())
    assert len(state["pool"]) >= 2 and state["pool"][0]["parent"] is None
    assert all(c["parent"] is not None for c in state["pool"][1:])
    final = (run / "checkout" / "ADAPTER.md").read_text()
    assert all(f"RULE_{k}" in final for k in KINDS)
    assert evaluate(config, "final", log=lambda *_: None)["mean_score"] == 1.0
    assert evaluate(config, "root", log=lambda *_: None)["mean_score"] == 0.0


def test_gepa_pareto_selection_runs(tmp_path):
    pytest.importorskip("gepa")
    summary, _ = train(make_run(tmp_path, "gepa", {}))
    assert summary["steps"] == 4
    steps = [json.loads(l) for l in (tmp_path / "runs" / "gepa-toy" / "steps.jsonl").read_text().splitlines()]
    assert all(s["status"] == "done" for s in steps)


def test_skillopt_learns_the_rules(tmp_path):
    pytest.importorskip("skillopt")
    config = make_run(tmp_path, "skillopt", {"analyst_workers": 2, "minibatch_size": 2, "merge_batch_size": 2,
                                             "slow_update_samples": 4, "total_steps": 4})
    summary, model = train(config)
    run = tmp_path / "runs" / "skillopt-toy"
    assert summary["steps"] == 4 and model.calls > 0
    state = json.loads((run / "workspace" / "state" / "skillopt.json").read_text())
    assert [h["action"] for h in state["history"]][0] in ("accept_new_best", "accept")
    assert state["history"][1]["slow_update"] == "inject_placeholder"      # end of epoch 1
    assert state["history"][3]["slow_update"] == "force_accept"            # end of epoch 2
    assert state["meta"].get("2")
    final = (run / "checkout" / "ADAPTER.md").read_text()
    assert all(f"RULE_{k}" in final for k in KINDS)
    assert evaluate(config, "final", log=lambda *_: None)["mean_score"] == 1.0


def test_optimizer_evolvers_need_validation_data(tmp_path):
    config = make_run(tmp_path, "gepa", {})
    config.data["data"]["val"] = None
    with pytest.raises(SystemExit, match="validation_data"):
        Trainer(config, log=lambda *_: None).train()


class CutOffOnce(ScriptedModel):
    """The first reply is cut off at max_tokens, as a reasoning model's can be."""

    def chat(self, messages, **kwargs):
        if self.calls == 0:
            self.calls += 1
            return {"content": "```\n# Learned instructions\n- partial", "finish_reason": "length"}, \
                Usage("scripted", 10, 0, 10, 1, 0.0)
        return super().chat(messages, **kwargs)


def test_gepa_never_takes_a_cut_off_reply(tmp_path):
    pytest.importorskip("gepa")
    config = make_run(tmp_path, "gepa", {"candidate_selection_strategy": "current_best"})
    trainer = Trainer(config, log=lambda *_: None)
    trainer.llm = CutOffOnce()
    trainer.train()
    state = json.loads((tmp_path / "runs" / "gepa-toy" / "workspace" / "state" / "gepa.json").read_text())
    assert not any("partial" in c["files"]["ADAPTER.md"] for c in state["pool"])
    assert all(f"RULE_{k}" in (tmp_path / "runs" / "gepa-toy" / "checkout" / "ADAPTER.md").read_text() for k in KINDS)


def test_skillopt_retries_a_cut_off_reply_with_more_room():
    pytest.importorskip("skillopt")
    from harnessadapter.evolver.loader import find, load_class
    import sys
    module = sys.modules[load_class(find("skillopt")).__module__]
    asked = []

    def llm(messages, max_tokens=None, temperature=None):
        asked.append(max_tokens)
        cut = len(asked) == 1
        return {"content": '{"a": 1' if cut else '{"a": 1}', "finish_reason": "length" if cut else "stop", "usage": {}}
    with module.skillopt_model(llm, {}):
        from skillopt.model import chat_optimizer
        text, _ = chat_optimizer("system", "user", max_completion_tokens=100)
    assert text == '{"a": 1}' and asked == [100, 200]


def test_run_agent_names_logs_of_training_tasks_only(tmp_path):
    config = make_run(tmp_path, "gepa", {})
    (tmp_path / "pipeline.py").write_text(PIPELINE.replace(
        "return RunOutput(output=answer, messages=messages, cost_usd=0.0, tokens=0)",
        "log = Path(__file__).parent / 'logs' / f\"{task['id']}.txt\"\n"
        "    log.parent.mkdir(exist_ok=True)\n"
        "    log.write_text(answer)\n"
        "    return RunOutput(output=answer, messages=messages, log=log, cost_usd=0.0, tokens=0)"))
    trainer = Trainer(config, log=lambda *_: None)
    trainer._prepare()
    rows = trainer._run_agent_tool()(task_ids=["t00", "t08"], adapter=str(trainer.run_dir / "root"))["results"]
    assert rows[0]["log"].endswith("t00.txt") and "log" not in rows[1]          # t00 trains, t08 validates


def test_skillopt_reads_skillopt_prediction_files_and_the_paper_view(tmp_path):
    pytest.importorskip("skillopt")
    from harnessadapter.evolver.loader import find, load_class
    import sys
    module = sys.modules[load_class(find("skillopt")).__module__]
    log = tmp_path / "log"
    log.mkdir()
    entries = [{"role": "user", "content": "Q" * 5000}, {"type": "tool_call", "cmd": "read(x)", "obs": "o" * 3000},
               {"type": "message", "content": "<answer>1</answer>"}]
    (log / "conversation.json").write_text(json.dumps(entries))
    (log / "target_system_prompt.txt").write_text("S" * 5000)
    (log / "target_user_prompt.txt").write_text("U" * 5000)
    record = {"trajectory": "log", "judge": {"score": 0.0}, "output": {"answer": "1"}, "status": "ok"}
    got, system, user = module.conversation(record, tmp_path, obs_chars=1000)
    assert got[0]["content"] == "Q" * 5000 and len(got[1]["obs"]) < 1100 and len(got) == 3   # passed through
    view, system, user = module.paper_view(got, system, user)
    assert len(view) == 1 and len(system) == len(user) == 3000
    assert "[obs]    " + "o" * 800 + "\n" in view[0]["content"] and "Q" * 501 not in view[0]["content"]


def test_skillopt_shows_the_analyst_a_codex_trace_summary(tmp_path):
    pytest.importorskip("skillopt")
    from harnessadapter.evolver.loader import find, load_class
    from skillopt.gradient.reflect import fmt_minibatch_trajectories
    import sys
    module = sys.modules[load_class(find("skillopt")).__module__]
    log = tmp_path / "log"
    log.mkdir()
    (log / "conversation.json").write_text(json.dumps([{"role": "user", "content": "Q"},
                                                       {"type": "message", "turn": 1, "content": "<answer>1</answer>"}]))
    (log / "codex_trace_summary.txt").write_text("Codex Trace Summary\n- read SKILL.md: success")
    record = {"id": "t1", "trajectory": "log", "judge": {"score": 0.0}, "output": {"answer": "1"}, "status": "ok"}
    view = module.conversation(record, tmp_path)
    module.write_prediction(tmp_path / "predictions" / "t1", *module.paper_view(*view),
                            codex_summary=module.codex_trace(record, tmp_path))
    item = {"id": "t1", "task_description": "Q"}
    with module.skillopt_model(lambda **kw: None, {}):
        shown = fmt_minibatch_trajectories([item], str(tmp_path / "predictions"))
    with module.skillopt_model(lambda **kw: None, {"codex_trace_to_optimizer": False}):
        hidden = fmt_minibatch_trajectories([item], str(tmp_path / "predictions"))
    assert "#### Codex Trace Summary\nCodex Trace Summary\n- read SKILL.md: success" in shown
    assert "Codex Trace Summary" not in hidden
