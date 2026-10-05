"""End-to-end tests of the Trainer on a toy task (no network, no model)."""
import json
from pathlib import Path
import subprocess
import textwrap

import pytest
import yaml

from harnessadapter.config import Config
from harnessadapter.evaluate import evaluate
from harnessadapter.settings_check import run_check
from harnessadapter.trainer.trainer import Trainer
from harnessadapter.tree import INVALID_MARK, git

TEMPLATE = Path(__file__).resolve().parents[1] / "harnessadapter" / "templates" / "prompt_only"

PIPELINE = '''
from pathlib import Path
from harnessadapter.task import RunOutput

def run(task, adapter):
    text = (Path(adapter) / "ADAPTER.md").read_text() if adapter else ""
    answer = str(task["a"] + task["b"]) if "add the numbers" in text else "?"
    log = Path(__file__).parent / "logs" / f"{task['id']}.txt"
    log.parent.mkdir(exist_ok=True)
    log.write_text(f"question {task['a']}+{task['b']} answer {answer}")
    return RunOutput(output=answer, log=log, cost_usd=0.01, wall_s=0.1, api_speed={"tps": 50}, tokens=100)
'''
JUDGE = '''
def score(task, output):
    ok = output.output == str(task["a"] + task["b"])
    return {"score": float(ok), "feedback": "correct" if ok else f"expected the sum of {task['a']} and {task['b']}"}
'''
EVOLVER = '''
from harnessadapter.evolver.base import Evolver, StepResult

class Toy(Evolver):
    def step(self, ctx):
        mode = ctx.params.get("mode", "learn")
        tasks = list((ctx.workspace.learning_material / "current" / "tasks").glob("*.json"))
        assert tasks, "material must exist"
        if mode == "learn":
            (ctx.workspace.adapter / "ADAPTER.md").write_text("Always add the numbers.\\n")
        elif mode == "break":
            (ctx.workspace.adapter / "EVOLVER.md").write_text("changed a fixed file\\n")
            (ctx.workspace.adapter / "stray.txt").write_text("outside every learnable path\\n")
            (ctx.workspace.adapter / "ADAPTER.md").write_text("Always add the numbers.\\n")
        elif mode == "stop":
            return StepResult("stop", "converged")
        (ctx.workspace.state / "seen.txt").write_text(str(ctx.progress.step))
        (ctx.workspace.output / "report.md").write_text(f"step {ctx.progress.step}: {mode}\\n")
        return StepResult("done", f"{mode} at step {ctx.progress.step}")
'''


def make_task(tmp: Path, **overrides) -> Path:
    (tmp / "task-info").mkdir()
    (tmp / "task-info" / "README.md").write_text("Add two numbers.\n")
    rows = [{"id": f"t{i}", "a": i, "b": 2 * i} for i in range(10)]
    (tmp / "tasks.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (tmp / "train.txt").write_text("\n".join(f"t{i}" for i in range(6)) + "\n")
    (tmp / "val.txt").write_text("t6\n")
    (tmp / "test.txt").write_text("t7\nt8\nt9\n")
    (tmp / "pipeline.py").write_text(PIPELINE)
    (tmp / "judge.py").write_text(JUDGE)
    (tmp / "toy").mkdir()
    (tmp / "toy" / "evolver.py").write_text(EVOLVER)
    (tmp / "toy" / "evolver.yaml").write_text("class: evolver.py:Toy\nneeds: []\nparams: {mode: learn}\n")
    config = {"data": {"tasks": "tasks.jsonl", "train": "train.txt", "val": "val.txt", "test": "test.txt",
                       "step_size": 4},
              "task_info": "task-info", "pipeline": {"run": "pipeline.py:run", "concurrency": 2},
              "judge": {"path": "judge.py"}, "template": str(TEMPLATE), "evolver": {"name": "toy"},
              "sandbox": {"enabled": False, "network": "open"},
              "trainer": {"run_name": "toy-run", "output_dir": "runs", "epochs": 2,
                          "save_freq": {"steps": 1, "epochs": 1}, "budget": {"usd": 5}}}
    for key, value in overrides.items():
        section, _, sub = key.partition("__")
        if sub:
            config.setdefault(section, {})[sub] = value
        else:
            config[section] = value
    path = tmp / "run.yaml"
    path.write_text(yaml.safe_dump(config))
    return path


def load(path, *overrides):
    return Config.load(path, list(overrides))


def test_train_commits_tags_and_records(tmp_path):
    cfg = load(make_task(tmp_path))
    summary = Trainer(cfg, log=lambda *_: None).train()
    run = tmp_path / "runs" / "toy-run"
    steps = [json.loads(l) for l in (run / "steps.jsonl").read_text().splitlines()]
    assert [s["step"] for s in steps] == [1, 2, 3, 4]                # 6 tasks / step 4 → 2 steps × 2 epochs
    assert steps[0]["rollout"]["mean_score"] == 0.0 and steps[0]["changed"]
    assert steps[1]["rollout"]["mean_score"] == 1.0                  # the learned adapter is used next step
    assert len(steps[1]["tasks"]) == 2                               # short last step
    assert not steps[2]["changed"]                                   # the same text again: no new commit
    tree = Path(summary["tree"])
    tags = git(tree, "tag").splitlines()
    assert {"root", "toy-run/final", "toy-run/ckpt-step-1", "toy-run/ckpt-epoch-1", "toy-run/ckpt-epoch-2"} <= set(tags)
    final = git(tree, "show", "toy-run/final:CARD.md")
    assert "Adapter card: toy-run" in final
    origin = yaml.safe_load(git(tree, "show", "toy-run/final:adapter.yaml"))["origin"]
    assert origin == {"template": "prompt-only", "version": "1.0"}
    material = run / "learning_material"
    assert (material / "current").resolve().name == "step_0004"
    assert len((material / "index.jsonl").read_text().splitlines()) == 4
    record = json.loads((material / "step_0001" / "tasks" / "t0.json").read_text())
    assert record["judge"]["score"] in (0.0, 1.0) and record["trajectory"]
    assert (run / "step_0001" / "output" / "report.md").exists()
    assert (run / "step_0004" / "state" / "seen.txt").read_text() == "4"
    assert (run / "summary.json").exists() and summary["stop_reason"] == "completed all epochs"
    # The history view the Evolver read is a log plus patches; it has no write access to git.
    assert (run / "workspace" / "history" / "log.jsonl").exists()


def test_failed_check_commits_then_repairs(tmp_path):
    cfg = load(make_task(tmp_path, evolver={"name": "toy", "params": {"mode": "break"}}), "trainer.epochs=1")
    summary = Trainer(cfg, log=lambda *_: None).train()
    tree = Path(summary["tree"])
    log = git(tree, "log", "--format=%s%n%b", "toy-run").split("\n")
    assert any(INVALID_MARK in line for line in log)
    assert any(line.startswith("repair step 1") for line in log)
    files = git(tree, "ls-tree", "-r", "--name-only", "toy-run/final").splitlines()
    assert "stray.txt" not in files
    assert git(tree, "show", "toy-run/final:EVOLVER.md") == (TEMPLATE / "EVOLVER.md").read_text().strip()
    assert "add the numbers" in git(tree, "show", "toy-run/final:ADAPTER.md")   # the valid part is kept
    feedback = json.loads((tmp_path / "runs/toy-run/learning_material/step_0002/check_feedback.json").read_text())
    assert {f["path"] for f in feedback} >= {"EVOLVER.md", "stray.txt"}


def test_resume_after_crash(tmp_path):
    path = make_task(tmp_path)
    cfg = load(path)
    trainer = Trainer(cfg, log=lambda *_: None)
    calls = {"n": 0}
    original = trainer._step

    def crash_on_third(*a, **k):
        calls["n"] += 1
        if calls["n"] == 3:
            raise KeyboardInterrupt("simulated crash")
        return original(*a, **k)
    trainer._step = crash_on_third
    with pytest.raises(KeyboardInterrupt):
        trainer.train()
    run = tmp_path / "runs" / "toy-run"
    assert len((run / "steps.jsonl").read_text().splitlines()) == 2
    summary = Trainer(load(path), log=lambda *_: None).train()
    steps = [json.loads(l) for l in (run / "steps.jsonl").read_text().splitlines()]
    assert [s["step"] for s in steps] == [1, 2, 3, 4]
    assert summary["steps"] == 4


def test_budget_stops_the_run(tmp_path):
    cfg = load(make_task(tmp_path), "trainer.budget.usd=0.05")
    summary = Trainer(cfg, log=lambda *_: None).train()
    assert summary["stop_reason"].startswith("hard budget")
    assert summary["spending"]["usd"]["rollout"] >= 0.05


def test_needs_are_enforced(tmp_path):
    path = make_task(tmp_path)
    (tmp_path / "toy" / "evolver.yaml").write_text("class: evolver.py:Toy\nneeds: [environment]\n")
    with pytest.raises(SystemExit, match="needs environment"):
        Trainer(load(path), log=lambda *_: None).train()


def test_early_stop_only_when_allowed(tmp_path):
    path = make_task(tmp_path, evolver={"name": "toy", "params": {"mode": "stop"}})
    summary = Trainer(load(path), log=lambda *_: None).train()
    assert summary["steps"] == 4
    summary = Trainer(load(path, "trainer.run_name=again", "trainer.allow_early_stop=true"),
                      log=lambda *_: None).train()
    assert summary["steps"] == 1 and "stopped the run" in summary["stop_reason"]


def test_eval_final_root_none(tmp_path):
    path = make_task(tmp_path)
    cfg = load(path)
    Trainer(cfg, log=lambda *_: None).train()
    final = evaluate(cfg, "final", log=lambda *_: None)
    assert final["mean_score"] == 1.0 and final["tasks"] == 3
    assert evaluate(cfg, "root", log=lambda *_: None)["mean_score"] == 0.0
    assert evaluate(cfg, "none", log=lambda *_: None)["mean_score"] == 0.0
    tree = tmp_path / "runs" / "trees" / "prompt-only"
    assert "| final | 3 |" in git(tree, "show", "toy-run/final:CARD.md")


def test_start_from_a_checkpoint(tmp_path):
    path = make_task(tmp_path)
    Trainer(load(path), log=lambda *_: None).train()
    summary = Trainer(load(path, "trainer.run_name=second", "start_from=toy-run/ckpt-step-1", "trainer.epochs=1"),
                      log=lambda *_: None).train()
    steps = [json.loads(l) for l in (tmp_path / "runs/second/steps.jsonl").read_text().splitlines()]
    assert steps[0]["rollout"]["mean_score"] == 1.0          # started from the learned adapter


def test_check_command(tmp_path, capsys):
    cfg = load(make_task(tmp_path))
    lines = []
    assert run_check(cfg, log=lines.append)
    text = "\n".join(lines)
    assert "[ok] template" in text and "[ok] pipeline probe" in text and "does not guarantee" in text
    (tmp_path / "test.txt").write_text("t0\n")
    lines = []
    assert not run_check(load(tmp_path / "run.yaml"), log=lines.append)
    assert any("share" in l for l in lines)


def test_overrides_reject_unknown_keys(tmp_path):
    path = make_task(tmp_path)
    with pytest.raises(ValueError, match="Unknown config key"):
        load(path, "trainer.epoch=3")
    assert load(path, "evolver.params.mode=break")["evolver"]["params"]["mode"] == "break"
