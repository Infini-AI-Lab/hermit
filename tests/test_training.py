"""End-to-end tests of the Trainer on a toy task (no network, no model)."""
import json
from pathlib import Path
import random
import subprocess
import textwrap

import pytest
import yaml

from hermit.config import Config
from hermit.evaluate import evaluate
from hermit.settings_check import run_check
from hermit.trainer.trainer import Trainer
from hermit.tree import INVALID_MARK, git

TEMPLATE = Path(__file__).resolve().parents[1] / "hermit" / "adapter" / "templates" / "prompt_only"

PIPELINE = '''
from pathlib import Path
from hermit.task import RunOutput

def run(task, adapter):
    text = (Path(adapter) / "ADAPTER.md").read_text() if adapter else ""
    answer = str(task["a"] + task["b"]) if "add the numbers" in text else "?"
    log = Path(__file__).parent / ".logs" / f"{task['id']}.txt"
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
from hermit.agent.evolver.base import Evolver, StepResult

class Toy(Evolver):
    def step(self, ctx):
        mode = ctx.params.get("mode", "learn")
        tasks = list((ctx.workspace.learning_material / "current" / "tasks").glob("*.json"))
        assert tasks, "material must exist"
        if mode == "learn":
            (ctx.workspace.adapter / "ADAPTER.md").write_text("Always add the numbers.\\n")
        elif mode == "break":
            (ctx.workspace.adapter / ".hermit" / "EVOLVER.md").write_text("changed the metadata\\n")
            (ctx.workspace.adapter / "stray.txt").write_text("outside every learnable path\\n")
            (ctx.workspace.adapter / "ADAPTER.md").write_text("Always add the numbers.\\n")
        elif mode == "nothing":
            pass
        elif mode == "stop":
            return StepResult("stop", "converged")
        elif mode == "spend":                       # spends, and is stopped by the Trainer's budget
            from hermit.agent.evolver.base import Usage
            ctx.cancel.wait(30)
            return StepResult("failed", "stopped", usage=[Usage(model="m", input_tokens=1234, output_tokens=0,
                                                                calls=1, cost_usd=0.5)])
        elif mode == "probe":                        # runs a training task through the run_agent tool
            import json as _json
            out = ctx.tools["run_agent"](task_ids=["t0"])
            (ctx.workspace.output / "probe.json").write_text(_json.dumps(out["results"][0], default=str))
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
              "task_info": "task-info", "agent": {"name": "pipeline.py:run", "concurrency": 2},
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
    final = git(tree, "show", "toy-run/final:.hermit/CARD.md")
    assert "Adapter card: toy-run" in final
    meta = git(tree, "show", "toy-run/final:.hermit/adapter.yaml")
    assert meta == (TEMPLATE / ".hermit" / "adapter.yaml").read_text().strip()   # the metadata is the template's
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
    assert git(tree, "show", "toy-run/final:.hermit/EVOLVER.md") == (TEMPLATE / ".hermit" / "EVOLVER.md").read_text().strip()
    assert "add the numbers" in git(tree, "show", "toy-run/final:ADAPTER.md")   # the valid part is kept
    feedback = json.loads((tmp_path / "runs/toy-run/learning_material/step_0002/check_feedback.json").read_text())
    assert {f["path"] for f in feedback} >= {".hermit/EVOLVER.md", "stray.txt"}


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


def test_a_budget_stop_keeps_what_the_stopped_call_spent(tmp_path):
    cfg = load(make_task(tmp_path, evolver={"name": "toy", "params": {"mode": "spend"}}), "trainer.budget.usd=null",
               "trainer.budget.hours=0.003")   # 10.8 s: past the preparation
    summary = Trainer(cfg, log=lambda *_: None).train()
    assert summary["stop_reason"].startswith("hard budget")
    assert summary["spending"]["tokens"]["evolver"] == 1234 and summary["spending"]["usd"]["evolver"] == 0.5


def test_run_agent_says_how_a_session_ended_when_the_pipeline_reports_it(tmp_path):
    path = make_task(tmp_path, evolver={"name": "toy", "params": {"mode": "probe"}})
    (tmp_path / "pipeline.py").write_text(PIPELINE.replace("tokens=100)", "tokens=100, extra={'exit_code': 1, "
                                                                         "'timed_out': False, 'workspace': '/w'})"))
    Trainer(load(path, "trainer.epochs=1"), log=lambda *_: None).train()
    probe = json.loads((tmp_path / "runs/toy-run/step_0001/output/probe.json").read_text())
    assert probe["exit_code"] == 1 and probe["timed_out"] is False and "workspace" not in probe


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
    assert "| final | 3 |" in git(tree, "show", "toy-run/final:.hermit/CARD.md")


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
    assert "[ok] template" in text and "[ok] agent probe" in text and "does not guarantee" in text
    (tmp_path / "test.txt").write_text("t0\n")
    lines = []
    assert not run_check(load(tmp_path / "run.yaml"), log=lines.append)
    assert any("share" in l for l in lines)


def test_overrides_reject_unknown_keys(tmp_path):
    path = make_task(tmp_path)
    with pytest.raises(ValueError, match="Unknown config key"):
        load(path, "trainer.epoch=3")
    assert load(path, "evolver.params.mode=break")["evolver"]["params"]["mode"] == "break"


def test_no_change_means_no_commit(tmp_path):
    path = make_task(tmp_path, evolver={"name": "toy", "params": {"mode": "nothing"}})
    summary = Trainer(load(path, "trainer.epochs=1"), log=lambda *_: None).train()
    steps = [json.loads(l) for l in (tmp_path / "runs/toy-run/steps.jsonl").read_text().splitlines()]
    assert not any(s["changed"] for s in steps)


def test_warnings_name_risky_settings(tmp_path):
    from hermit.trainer.trainer import warnings_for
    text = " ".join(warnings_for(load(make_task(tmp_path)), val_given=True))
    assert "sandbox.enabled is false" in text and "sandbox.network is open" in text and "sandbox.env is not set" in text


def test_shuffle_false_keeps_the_split_order(tmp_path):
    cfg = load(make_task(tmp_path), "data.shuffle=false", "trainer.epochs=1")
    Trainer(cfg, log=lambda *_: None).train()
    material = tmp_path / "runs" / "toy-run" / "learning_material"
    order = [t for step in ("step_0001", "step_0002")
             for t in json.loads((material / step / "summary.json").read_text())["tasks"]]
    assert order == [f"t{i}" for i in range(6)]


def test_shuffle_must_be_boolean(tmp_path):
    with pytest.raises(ValueError, match="data.shuffle"):
        load(make_task(tmp_path), "data.shuffle=sometimes").validate()


BOOT_EVOLVER = '''
import subprocess
from hermit.agent.evolver.base import Evolver, StepResult

class Boot(Evolver):
    def bootstrap(self, ctx):
        assert not list(ctx.workspace.learning_material.glob("step_*")), "no rollouts before the bootstrap"
        (ctx.workspace.adapter / "ADAPTER.md").write_text("Always add the numbers.\\n")
        return StepResult("done", "bootstrapped")

    def step(self, ctx):
        log = subprocess.run(["git", "-C", str(ctx.workspace.adapter), "log", "--format=%s"],
                             capture_output=True, text=True).stdout
        (ctx.workspace.state / f"git_{ctx.progress.step}.txt").write_text(log)
        branches = subprocess.run(["git", "-C", str(ctx.workspace.adapter), "branch", "-a"],
                                  capture_output=True, text=True).stdout
        (ctx.workspace.state / f"branches_{ctx.progress.step}.txt").write_text(branches)
        return StepResult("done", "nothing")
'''


def _boot_task(tmp):
    path = make_task(tmp)
    (tmp / "boot").mkdir()
    (tmp / "boot" / "evolver.py").write_text(BOOT_EVOLVER)
    (tmp / "boot" / "evolver.yaml").write_text("class: evolver.py:Boot\nneeds: []\n")
    return path


def test_bootstrap_is_step_zero_before_any_rollout(tmp_path):
    cfg = load(_boot_task(tmp_path), "evolver.name=boot", "trainer.epochs=1")
    Trainer(cfg, log=lambda *_: None).train()
    run = tmp_path / "runs" / "toy-run"
    steps = [json.loads(l) for l in (run / "steps.jsonl").read_text().splitlines()]
    assert [s["step"] for s in steps] == [0, 1, 2] and steps[0]["bootstrap"] and steps[0]["changed"]
    assert steps[1]["parent"] == steps[0]["commit"]
    assert steps[1]["rollout"]["mean_score"] == 1.0              # the first rollouts already use the bootstrap
    tags = subprocess.run(["git", "-C", str(tmp_path / "runs" / "trees" / "prompt-only"), "tag"],
                          capture_output=True, text=True).stdout.split()
    assert "toy-run/bootstrap" in tags


def test_evolver_workspace_carries_git_history_rollouts_do_not(tmp_path):
    cfg = load(_boot_task(tmp_path), "evolver.name=boot", "trainer.epochs=1")
    Trainer(cfg, log=lambda *_: None).train()
    run = tmp_path / "runs" / "toy-run"
    log = (run / "workspace" / "state" / "git_2.txt").read_text().splitlines()
    assert any(line.startswith("bootstrap (step 0)") for line in log) and log[-1].startswith("root:")
    assert not (run / "rollout_adapter" / ".git").exists()
    branches = (run / "workspace" / "state" / "branches_2.txt").read_text().split()
    assert branches == ["*", "adapter"]                             # this run's ancestry only


def test_resume_does_not_repeat_the_bootstrap(tmp_path):
    cfg = load(_boot_task(tmp_path), "evolver.name=boot", "trainer.epochs=1")
    Trainer(cfg, log=lambda *_: None).train()
    run = tmp_path / "runs" / "toy-run"
    lines = (run / "steps.jsonl").read_text().splitlines()
    (run / "steps.jsonl").write_text(lines[0] + "\n")             # as if the run died after step 0
    (run / "summary.json").unlink()
    Trainer(load(tmp_path / "run.yaml", "evolver.name=boot", "trainer.epochs=1"), log=lambda *_: None).train()
    steps = [json.loads(l) for l in (run / "steps.jsonl").read_text().splitlines()]
    assert [s["step"] for s in steps] == [0, 1, 2]


# ---------- the Evolver chooses the next step's tasks ----------

PICK_EVOLVER = '''
import json
from hermit.agent.evolver.base import Evolver, StepResult

class Pick(Evolver):
    """Names the last remaining task as the next step's only task (mode `reverse`), or a bad request."""
    chooses_tasks = True

    def bootstrap(self, ctx):
        return self.step(ctx)

    def step(self, ctx):
        p = ctx.progress
        with open(ctx.workspace.state / "seen.jsonl", "a") as f:
            f.write(json.dumps({"step": p.step, "remaining": list(p.remaining),
                                "listed": "have still to run" in ctx.message}) + "\\n")
        mode = ctx.params.get("mode", "reverse")
        nxt = {"reverse": [p.remaining[-1]] if p.remaining else None, "bad": ["t9"],
               "twice": ["t0", "t0"], "many": ["t0", "t1", "t2", "t3", "t4"],
               "boundary": None if p.remaining else ["t3"], "empty": (), "word": "t4"}.get(mode)
        return StepResult("done", f"step {p.step}", next_tasks=nxt)

class Silent(Pick):
    """The same requests from an Evolver that does not declare chooses_tasks."""
    chooses_tasks = False
'''


def _pick_task(tmp, mode="reverse", cls="Pick"):
    path = make_task(tmp)
    (tmp / "pick").mkdir()
    (tmp / "pick" / "evolver.py").write_text(PICK_EVOLVER)
    (tmp / "pick" / "evolver.yaml").write_text(f"class: evolver.py:{cls}\nneeds: []\nparams: {{mode: {mode}}}\n")
    return path


def _steps(tmp):
    run = tmp / "runs" / "toy-run"
    return [json.loads(l) for l in (run / "steps.jsonl").read_text().splitlines()]


def _seen(tmp):
    return [json.loads(l) for l in (tmp / "runs" / "toy-run" / "workspace" / "state" / "seen.jsonl")
            .read_text().splitlines()]


def _tags(tmp):
    return subprocess.run(["git", "-C", str(tmp / "runs" / "trees" / "prompt-only"), "tag"],
                          capture_output=True, text=True).stdout.split()


def test_the_evolver_names_the_next_steps_tasks(tmp_path):
    cfg = load(_pick_task(tmp_path), "evolver.name=pick", "data.shuffle=false", "trainer.epochs=1")
    Trainer(cfg, log=lambda *_: None).train()
    steps = [s for s in _steps(tmp_path) if not s.get("bootstrap")]
    # the bootstrap names t5; from then on each step takes the last task still to run, one at a time
    assert [s["tasks"] for s in steps] == [["t5"], ["t4"], ["t3"], ["t2"], ["t1"], ["t0"]]
    assert all(s["chosen_by"] == "evolver" for s in steps)
    assert [s["epoch_end"] for s in steps] == [False] * 5 + [True]
    seen = _seen(tmp_path)
    assert seen[0] == {"step": 0, "remaining": [f"t{i}" for i in range(6)], "listed": True}
    assert seen[1] == {"step": 1, "remaining": [f"t{i}" for i in range(5)], "listed": True}
    assert "toy-run/ckpt-epoch-1" in _tags(tmp_path)


def test_an_evolver_that_does_not_choose_sees_nothing_ahead_and_is_refused(tmp_path):
    cfg = load(_pick_task(tmp_path, "bad", "Silent"), "evolver.name=pick", "data.shuffle=false",
               "trainer.epochs=1")
    Trainer(cfg, log=lambda *_: None).train()
    assert all(s == {**s, "remaining": [], "listed": False} for s in _seen(tmp_path))
    steps = _steps(tmp_path)
    assert all("chooses_tasks" in s["next_tasks_refused"] and "next_tasks" not in s for s in steps)
    assert [s["tasks"] for s in steps if not s.get("bootstrap")] == [["t0", "t1", "t2", "t3"], ["t4", "t5"]]


@pytest.mark.parametrize("mode, reason", [("bad", "not training tasks still to run"), ("twice", "twice"),
                                          ("many", "more than data.step_size"), ("word", "not a list")])
def test_a_bad_request_is_refused_where_it_is_made_and_the_order_runs(tmp_path, mode, reason):
    cfg = load(_pick_task(tmp_path, mode), "evolver.name=pick", "data.shuffle=false", "trainer.epochs=1")
    Trainer(cfg, log=lambda *_: None).train()
    steps = _steps(tmp_path)
    assert all(reason in s["next_tasks_refused"] and "chosen_by" not in s for s in steps)
    assert [s["tasks"] for s in steps if not s.get("bootstrap")] == [["t0", "t1", "t2", "t3"], ["t4", "t5"]]


def test_the_default_order_is_unchanged(tmp_path):
    cfg = load(make_task(tmp_path), "trainer.epochs=2", "trainer.save_freq={steps: null, epochs: 2}")
    Trainer(cfg, log=lambda *_: None).train()
    steps = _steps(tmp_path)
    expected = []
    for epoch in (1, 2):                                          # the order as the Trainer always took it
        order = [f"t{i}" for i in range(6)]
        random.Random(epoch).shuffle(order)                       # data.shuffle_seed 0, plus the epoch
        expected += [order[:4], order[4:]]
    assert [s["tasks"] for s in steps] == expected
    assert [s["epoch_end"] for s in steps] == [False, True, False, True]
    assert not any("chosen_by" in s or "next_tasks" in s or "next_tasks_refused" in s for s in steps)
    tags = _tags(tmp_path)
    assert "toy-run/ckpt-epoch-2" in tags and "toy-run/ckpt-epoch-1" not in tags


def test_a_split_that_names_a_task_twice_runs_it_twice(tmp_path):
    path = make_task(tmp_path)
    (tmp_path / "train.txt").write_text("t0\nt1\nt0\nt2\n")
    Trainer(load(path, "data.shuffle=false", "trainer.epochs=1", "data.step_size=2"), log=lambda *_: None).train()
    assert [s["tasks"] for s in _steps(tmp_path)] == [["t0", "t1"], ["t0", "t2"]]


def test_resume_of_records_without_the_new_keys(tmp_path):
    path = make_task(tmp_path)
    Trainer(load(path), log=lambda *_: None).train()
    run = tmp_path / "runs" / "toy-run"
    lines = [json.loads(l) for l in (run / "steps.jsonl").read_text().splitlines()]
    for record in lines:
        record.pop("epoch_end")                                   # as the code before this change wrote them
    (run / "steps.jsonl").write_text("".join(json.dumps(r) + "\n" for r in lines[:3]))   # died after step 3
    (run / "summary.json").unlink()
    Trainer(load(path), log=lambda *_: None).train()
    steps = _steps(tmp_path)
    assert [s["step"] for s in steps] == [1, 2, 3, 4] and steps[3]["tasks"] == lines[3]["tasks"]


def test_resume_keeps_the_evolvers_choice(tmp_path):
    path = _pick_task(tmp_path)
    trainer = Trainer(load(path, "evolver.name=pick", "data.shuffle=false"), log=lambda *_: None)
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
    Trainer(load(path, "evolver.name=pick", "data.shuffle=false"), log=lambda *_: None).train()
    steps = [s for s in _steps(tmp_path) if not s.get("bootstrap")]
    assert [s["tasks"][0] for s in steps if s["epoch"] == 1] == ["t5", "t4", "t3", "t2", "t1", "t0"]
    # epoch 1 ends with nothing left to name, so epoch 2 opens with the order's step, then the Evolver picks again
    assert [s["tasks"] for s in steps if s["epoch"] == 2] == [["t0", "t1", "t2", "t3"], ["t5"], ["t4"]]
    assert [s["step"] for s in steps] == list(range(1, 10))


def test_a_request_at_an_epochs_end_opens_the_next_epoch(tmp_path):
    cfg = load(_pick_task(tmp_path, "boundary"), "evolver.name=pick", "data.shuffle=false")
    Trainer(cfg, log=lambda *_: None).train()
    steps = [s for s in _steps(tmp_path) if not s.get("bootstrap")]
    assert [(s["epoch"], s["tasks"]) for s in steps] == [(1, ["t0", "t1", "t2", "t3"]), (1, ["t4", "t5"]),
                                                          (2, ["t3"]), (2, ["t0", "t1", "t2", "t4"]), (2, ["t5"])]
    assert steps[2]["chosen_by"] == "evolver"


COMMAND_EVOLVER = '''
from hermit.agent.evolver.agent import CommandEvolver

class Picking(CommandEvolver):
    chooses_tasks = True
'''
PROGRAM = '''
import json, os
ctx = json.load(open(os.environ["HA_CONTEXT"]))
remaining = ctx["progress"]["remaining"]
out = ctx["workspace"]["output"]
json.dump({"status": "done", "summary": "picked", "next_tasks": remaining[-1:] or None},
          open(os.path.join(out, "step_result.json"), "w"))
'''


def test_an_external_program_names_the_next_tasks(tmp_path):
    import sys
    path = make_task(tmp_path)
    (tmp_path / "cmd").mkdir()
    (tmp_path / "cmd" / "evolver.py").write_text(COMMAND_EVOLVER)
    (tmp_path / "cmd" / "program.py").write_text(PROGRAM)
    (tmp_path / "cmd" / "evolver.yaml").write_text(
        f"class: evolver.py:Picking\nneeds: []\nparams: {{command: [{sys.executable}, {tmp_path / 'cmd' / 'program.py'}]}}\n")
    Trainer(load(path, "evolver.name=cmd", "data.shuffle=false", "trainer.epochs=1"), log=lambda *_: None).train()
    assert [s["tasks"] for s in _steps(tmp_path)] == [["t0", "t1", "t2", "t3"], ["t5"], ["t4"]]


def test_an_empty_request_names_nothing(tmp_path):
    cfg = load(_pick_task(tmp_path, "empty"), "evolver.name=pick", "data.shuffle=false", "trainer.epochs=1")
    Trainer(cfg, log=lambda *_: None).train()
    steps = _steps(tmp_path)
    assert [s["tasks"] for s in steps if not s.get("bootstrap")] == [["t0", "t1", "t2", "t3"], ["t4", "t5"]]
    assert not any("next_tasks" in s or "next_tasks_refused" in s or "chosen_by" in s for s in steps)
