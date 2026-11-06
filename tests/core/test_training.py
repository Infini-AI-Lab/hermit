"""End-to-end tests of the Trainer on a toy task (no network, no model)."""
import json
import os
from pathlib import Path
import random
import subprocess

import pytest

from hermit.config import Config
from hermit.evaluate import evaluate
from hermit.settings_check import run_check
from hermit.trainer.trainer import Trainer
from hermit.tree import INVALID_MARK, git

from toy_task import EVOLVER, JUDGE, PIPELINE, TEMPLATE, make_task  # noqa: F401


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
    # Evaluating the final, root and no adapter on the test split; the final card gets the score.
    final = evaluate(cfg, "final", log=lambda *_: None)
    assert final["mean_score"] == 1.0 and final["tasks"] == 3
    assert evaluate(cfg, "root", log=lambda *_: None)["mean_score"] == 0.0
    assert evaluate(cfg, "none", log=lambda *_: None)["mean_score"] == 0.0
    assert "| final | 3 |" in git(tree, "show", "toy-run/final:.hermit/CARD.md")
    # A second run started from a checkpoint of this one starts from the learned adapter.
    Trainer(load(tmp_path / "run.yaml", "trainer.run_name=second", "start_from=toy-run/ckpt-step-1",
                 "trainer.epochs=1"), log=lambda *_: None).train()
    steps = [json.loads(l) for l in (tmp_path / "runs/second/steps.jsonl").read_text().splitlines()]
    assert steps[0]["rollout"]["mean_score"] == 1.0


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
    # The log is a copy in the run's agent_logs/ (shown at /w/agent-logs; here the sandbox is off: the host path).
    assert "/toy-run/agent_logs/" in probe["log"] and "question" in probe["log_text"]


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


def test_warnings_name_risky_settings(tmp_path):
    from hermit.trainer.trainer import warnings_for
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    text = " ".join(warnings_for(load(make_task(tmp_path / "a", environment__network="public")), val_given=True))
    assert "sandbox.enabled is false" in text and "environment.network is public" in text
    on = make_task(tmp_path / "b", sandbox__enabled=True, sandbox__enforce="best_effort")
    assert "sandbox.enforce is best_effort" in " ".join(warnings_for(load(on), val_given=True))


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
    # The Evolver's workspace carries the git history; the rollouts' adapter does not.
    log = (run / "workspace" / "state" / "git_2.txt").read_text().splitlines()
    assert any(line.startswith("bootstrap (step 0)") for line in log) and log[-1].startswith("root:")
    assert not (run / "rollout_adapter" / ".git").exists()
    branches = (run / "workspace" / "state" / "branches_2.txt").read_text().split()
    assert branches == ["*", "adapter"]                             # this run's ancestry only
    # A resume does not repeat the bootstrap.
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
    """Names the last remaining task as the next step's only task (mode `reverse`), or a bad request (mode `each`:
    a different bad request at each step)."""
    chooses_tasks = True

    def bootstrap(self, ctx):
        return self.step(ctx)

    def step(self, ctx):
        p = ctx.progress
        with open(ctx.workspace.state / "seen.jsonl", "a") as f:
            f.write(json.dumps({"step": p.step, "remaining": list(p.remaining),
                                "listed": "have still to run" in ctx.message}) + "\\n")
        mode = ctx.params.get("mode", "reverse")
        each = [["t9"], ["t0", "t0"], ["t0", "t1", "t2", "t3", "t4"], "t4"]   # outside, twice, too many, no list
        nxt = {"reverse": [p.remaining[-1]] if p.remaining else None, "bad": ["t9"], "each": each[min(p.step, 3)],
               "boundary": None if p.remaining else ["t3"], "empty": ()}.get(mode)
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


def test_a_bad_request_is_refused_where_it_is_made_and_the_order_runs(tmp_path):
    """Each step makes another kind of bad request; each is refused, for its own reason, on the step that made it."""
    cfg = load(_pick_task(tmp_path, "each"), "evolver.name=pick", "data.shuffle=false", "trainer.epochs=2")
    Trainer(cfg, log=lambda *_: None).train()
    steps = _steps(tmp_path)
    reasons = ["not training tasks still to run", "twice", "more than data.step_size", "not a list", "not a list"]
    assert [s["step"] for s in steps] == [0, 1, 2, 3, 4]
    assert all(reason in s["next_tasks_refused"] and "chosen_by" not in s for reason, s in zip(reasons, steps))
    assert [s["tasks"] for s in steps if not s.get("bootstrap")] == [["t0", "t1", "t2", "t3"], ["t4", "t5"]] * 2
    material = tmp_path / "runs" / "toy-run" / "learning_material"
    order = [t for step in ("step_0001", "step_0002")
             for t in json.loads((material / step / "summary.json").read_text())["tasks"]]
    assert order == [f"t{i}" for i in range(6)]          # the material's summaries keep the split order too


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


def test_syncing_a_learners_adapter_never_follows_a_link(tmp_path):
    """The learner's working copy reaches the tree through Tree.sync on the host: a link to a file outside it (one
    the learner could not read) is not copied, a link inside it stays a link, and a pipe is skipped unread."""
    from hermit.tree import Tree
    secret = tmp_path / "secret.txt"
    secret.write_text("host secret\n")
    src, checkout = tmp_path / "adapter", tmp_path / "checkout"
    src.mkdir()
    checkout.mkdir()
    (src / "ADAPTER.md").write_text("context\n")
    (src / "leak.md").symlink_to(secret)
    (src / "alias.md").symlink_to("ADAPTER.md")
    os.mkfifo(src / "pipe")
    Tree.sync(src, checkout)
    assert sorted(p.name for p in checkout.iterdir()) == ["ADAPTER.md", "alias.md"]
    assert (checkout / "alias.md").is_symlink() and os.readlink(checkout / "alias.md") == "ADAPTER.md"


def test_the_host_checks_and_repairs_a_learners_adapter_without_following_its_links(tmp_path):
    """check_spec reads a sanitized snapshot (a linked exports file is not followed, so its target is not echoed);
    the Trainer replaces the working copy by its sanitized copy before checking and repairing it, so a repair never
    writes through a link to a host file."""
    import shutil as _shutil
    from hermit.adapter.manifest import EXPORTS
    from hermit.spec import check_spec
    from hermit.trainer.trainer import _sanitize_in_place
    from toy_task import TEMPLATE
    secret = tmp_path / "answers.jsonl"
    secret.write_text("q1: [gold-answer-42\n")             # not YAML: a parser's error would quote it
    adapter = tmp_path / "adapter"
    _shutil.copytree(TEMPLATE, adapter)
    (adapter / EXPORTS).unlink()
    (adapter / EXPORTS).symlink_to(secret)
    result = check_spec(adapter, TEMPLATE, deploy=False)
    assert "gold-answer-42" not in json.dumps(result.to_dict())
    _sanitize_in_place(adapter)
    assert not (adapter / EXPORTS).exists() and adapter.is_dir()        # the link is gone, nothing points out
    assert secret.read_text() == "q1: [gold-answer-42\n"


def test_the_check_snapshot_leaves_out_git_history(tmp_path, monkeypatch):
    import shutil as _shutil
    import hermit.spec as spec_module
    from toy_task import TEMPLATE
    adapter = tmp_path / "adapter"
    _shutil.copytree(TEMPLATE, adapter)
    (adapter / ".git" / "objects").mkdir(parents=True)
    (adapter / ".git" / "objects" / "old.pack").write_bytes(b"x" * 200_000)       # history, not the adapter
    monkeypatch.setattr(spec_module, "SNAPSHOT_BYTES", 100_000)
    result = spec_module.check_spec(adapter, TEMPLATE, deploy=False)
    assert not any("bytes" in i.problem for i in result.issues)


def test_the_learners_sandbox_reaches_every_ChatGPT_login_it_runs_on(tmp_path):
    """RRSI's role sessions on a ChatGPT login (the proposer on a model binding) reach the login's hosts too."""
    trainer = Trainer(load(make_task(tmp_path), "evolver.params.roles={login: chatgpt}"), log=lambda *_: None)
    trainer._prepare()
    try:
        assert {"chatgpt.com", "auth.openai.com"} <= set(trainer.sandbox.allow)
    finally:
        trainer._close_sandboxes()


def test_a_run_agent_log_the_learner_opens_is_scrubbed_of_model_keys(tmp_path, monkeypatch):
    """The acting agent holds its model's key: the copy of its log shown to the learner does not."""
    monkeypatch.setenv("AGENT_API_KEY", "sk-agent-key-0123456789abcdef")
    trainer = Trainer(load(make_task(tmp_path)), log=lambda *_: None)
    log = tmp_path / "logs" / "t1.log"
    log.parent.mkdir()
    log.write_text("env: AGENT_API_KEY=sk-agent-key-0123456789abcdef\n")
    shown = Path(trainer._visible_log({"id": "t1", "log": str(log)})).read_text()
    assert "sk-agent-key" not in shown and "<AGENT_API_KEY>" in shown


LEAK_EVOLVER = '''
import os
from hermit.agent.evolver.base import Evolver, FinalResult, StepResult, Usage

class Leak(Evolver):
    def step(self, ctx):
        key = os.environ["LEARNER_API_KEY"]
        (ctx.workspace.adapter / "ADAPTER.md").write_text("Always add the numbers.\\n")
        (ctx.workspace.output / "report.md").write_text(f"report with {key}\\n")
        (ctx.workspace.state / "notes.txt").write_text(f"notes with {key}\\n")
        home = ctx.workspace.state.parent / "agent_home" / "sessions"
        home.mkdir(parents=True, exist_ok=True)
        (home / "rollout.jsonl").write_text(f"transcript with {key}\\n")
        return StepResult(ctx.params.get("status", "done"), f"used {key}", rationale=f"because {key}",
                          extra={"seen": [key], key: 1},
                          usage=[Usage(model=key, calls=1)])

    def finalize(self, ctx):
        return FinalResult(adapter=ctx.workspace.adapter, summary=f"final with {os.environ['LEARNER_API_KEY']}")
'''


def test_what_the_learner_reports_is_kept_without_its_model_key(tmp_path, monkeypatch):
    """The learner holds its model's key: its summary, rationale, extra (keys too), usage and final summary reach
    steps.jsonl, step_result.json, summary.json and the commit messages scrubbed."""
    key = "sk-learner-key-0123456789abcdef"
    monkeypatch.setenv("LEARNER_API_KEY", key)
    path = make_task(tmp_path)
    (tmp_path / "leak").mkdir()
    (tmp_path / "leak" / "evolver.py").write_text(LEAK_EVOLVER)
    (tmp_path / "leak" / "evolver.yaml").write_text("class: evolver.py:Leak\nneeds: []\n")
    Trainer(load(path, "evolver.name=leak", "trainer.epochs=1"), log=lambda *_: None).train()
    run = tmp_path / "runs" / "toy-run"
    kept = (run / "steps.jsonl").read_text() + (run / "step_0001" / "step_result.json").read_text()
    kept += (run / "summary.json").read_text()
    kept += git(tmp_path / "runs" / "trees" / "prompt-only", "log", "--all", "--format=%B")
    assert key not in kept and "<LEARNER_API_KEY>" in kept
    # The learner's own files the run keeps (its workspace's output and state) are scrubbed when it finishes.
    own = [p for p in (run / "workspace").rglob("*") if p.is_file()
           and p.name in ("report.md", "notes.txt", "rollout.jsonl")]
    assert len(own) == 3 and all(key not in p.read_text() and "<LEARNER_API_KEY>" in p.read_text() for p in own)
    # A status the learner made up is recorded too: scrubbed like the rest.
    Trainer(load(path, "evolver.name=leak", "trainer.epochs=1", "trainer.run_name=odd",
                 f"evolver.params.status=odd-{key}"), log=lambda *_: None).train()
    assert key not in (tmp_path / "runs" / "odd" / "steps.jsonl").read_text()


def test_a_directory_the_learner_replaced_with_a_file_is_synced(tmp_path):
    """The checkout has node/old.txt, the learner's copy a file `node`: the sync replaces the directory."""
    from hermit.tree import Tree
    source, checkout = tmp_path / "source", tmp_path / "checkout"
    (checkout / "node").mkdir(parents=True)
    (checkout / "node" / "old.txt").write_text("old\n")
    source.mkdir()
    (source / "node").write_text("now a file\n")
    Tree.sync(source, checkout)
    assert (checkout / "node").read_text() == "now a file\n"


def test_what_the_agents_report_reaches_steps_jsonl_scrubbed(tmp_path, monkeypatch):
    """The acting agent holds its key: a field of its own (api_speed here) is kept in steps.jsonl scrubbed."""
    key = "sk-agent-key-0123456789abcdef"
    monkeypatch.setenv("AGENT_API_KEY", key)
    path = make_task(tmp_path)
    leaky = 'api_speed={"tps": 50, "via": os.environ["AGENT_API_KEY"]}'
    pipeline = (tmp_path / "pipeline.py").read_text().replace('api_speed={"tps": 50}', leaky)
    (tmp_path / "pipeline.py").write_text("import os\n" + pipeline)
    Trainer(load(path, "trainer.epochs=1"), log=lambda *_: None).train()
    steps = (tmp_path / "runs" / "toy-run" / "steps.jsonl").read_text()
    assert key not in steps and "<AGENT_API_KEY>" in steps
    evaluate(load(path, "trainer.epochs=1"), "final", log=lambda *_: None)      # and eval's results
    results = [p.read_text() for p in (tmp_path / "runs").rglob("results.jsonl")]
    assert results and all(key not in r for r in results) and any("<AGENT_API_KEY>" in r for r in results)


def test_sanitizing_replaces_a_directory_the_learner_left_read_only(tmp_path):
    from hermit.trainer.trainer import _sanitize_in_place
    adapter = tmp_path / "adapter"
    (adapter / "assets").mkdir(parents=True)
    (adapter / "assets" / "a.txt").write_text("a")
    os.chmod(adapter / "assets", 0o555)
    os.chmod(adapter, 0o555)
    try:
        _sanitize_in_place(adapter)
        assert (adapter / "assets" / "a.txt").read_text() == "a"
    finally:
        for p in (adapter, adapter / "assets"):
            if p.exists():
                os.chmod(p, 0o755)


def test_scrubbing_reaches_a_directory_the_learner_left_read_only(tmp_path, monkeypatch):
    from hermit.redact import redact_tree
    key = "sk-sealed-key-0123456789abcdef"
    monkeypatch.setenv("SEALED_API_KEY", key)
    sealed = tmp_path / "state" / "sealed"
    sealed.mkdir(parents=True)
    (sealed / "notes.txt").write_text(f"key {key}\n")
    os.chmod(sealed, 0o555)
    try:
        assert redact_tree(tmp_path / "state") == ["sealed/notes.txt"]
        assert (sealed / "notes.txt").read_text() == "key <SEALED_API_KEY>\n"
    finally:
        os.chmod(sealed, 0o755)


def test_the_learners_grants_are_searched_again_before_every_learner_call(tmp_path, monkeypatch):
    import hermit.trainer.trainer as trainer_module
    calls = []

    class Counted(trainer_module.Recheck):
        def __call__(self):
            calls.append(1)
            return super().__call__()
    monkeypatch.setattr(trainer_module, "Recheck", Counted)
    summary = Trainer(load(make_task(tmp_path), "trainer.epochs=1"), log=lambda *_: None).train()
    assert len(calls) == summary["steps"] + 2          # setup, every step, finalize


def test_the_card_records_the_acting_agents_sandbox_and_the_judges_place():
    from hermit.trainer import card
    text = card.render(template={"id": "t", "version": "1"}, run_name="r", parent="p", evolver="e", task="x",
                       epochs_done=1, steps_done=1, budget={"usd": {}, "seconds": 0}, warnings=[], summary="s",
                       settings={"sandbox": {}, "environment": {"image": "img:1", "network": "allowlist"},
                                 "judge_mode": "separate", "evolver_access": {}})
    assert '"image": "img:1"' in text and "judge: separate" in text


def test_scrubbing_reads_a_file_and_walks_a_root_the_learner_left_unreadable(tmp_path, monkeypatch):
    from hermit.redact import redact_tree
    key = "sk-hidden-key-0123456789abcdef"
    monkeypatch.setenv("HIDDEN_API_KEY", key)
    root = tmp_path / "state"
    root.mkdir()
    (root / "a.txt").write_text(f"{key}\n")
    os.chmod(root / "a.txt", 0o200)                               # write-only: unreadable
    os.chmod(root, 0o300)                                         # the root itself unreadable
    try:
        assert redact_tree(root) == ["a.txt"]
        assert stat_mode(root / "a.txt") == 0o200                  # its mode kept
        os.chmod(root / "a.txt", 0o600)
        assert (root / "a.txt").read_text() == "<HIDDEN_API_KEY>\n"
    finally:
        os.chmod(root, 0o755)


def stat_mode(path):
    import stat
    return stat.S_IMODE(os.stat(path).st_mode)


def test_a_learners_unreadable_files_are_copied_and_sanitized(tmp_path):
    from hermit.trainer.trainer import _copy_dir, _sanitize_in_place
    out = tmp_path / "output"
    (out / "sealed").mkdir(parents=True)
    (out / "sealed" / "a.txt").write_text("a")
    (out / "b.txt").write_text("b")
    os.chmod(out / "b.txt", 0o200)
    os.chmod(out / "sealed", 0o300)
    _copy_dir(out, tmp_path / "copy")
    assert (tmp_path / "copy" / "sealed" / "a.txt").read_text() == "a" and (tmp_path / "copy" / "b.txt").exists()
    os.chmod(out / "b.txt", 0o200)
    os.chmod(out / "sealed", 0o300)
    _sanitize_in_place(out)
    assert (out / "sealed" / "a.txt").read_text() == "a"


def test_a_refused_run_closes_what_the_trainer_started(tmp_path, monkeypatch):
    trainer = Trainer(load(make_task(tmp_path)), log=lambda *_: None)
    closed = []
    monkeypatch.setattr(trainer, "_prepare", lambda: (_ for _ in ()).throw(RuntimeError("refused")))
    monkeypatch.setattr(trainer, "_close_sandboxes", lambda: closed.append(1))
    with pytest.raises(RuntimeError, match="refused"):
        trainer.train()
    assert closed


def test_what_the_learner_is_shown_of_the_task_and_the_llms_home_are_scrubbed(tmp_path, monkeypatch):
    """evolver_access.environment (an evidence store the agents wrote) is scrubbed before the learner's calls;
    environment.write and a configured llm.cli home when the run finishes."""
    key = "sk-evidence-key-0123456789abcdef"
    monkeypatch.setenv("EVIDENCE_API_KEY", key)
    path = make_task(tmp_path)
    for name in ("evidence", "task-state", "llm-home"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "x.txt").write_text(key)
    Trainer(load(path, "trainer.epochs=1", "evolver_access.environment=[evidence]", "environment.write=[task-state]",
                 f"llm={{cli: codex, model: gpt-x, login: chatgpt, web_search: false, home: {tmp_path / 'llm-home'}}}"),
            log=lambda *_: None).train()
    for name in ("evidence", "task-state", "llm-home"):
        assert (tmp_path / name / "x.txt").read_text() == "<EVIDENCE_API_KEY>", name


def test_a_clean_file_in_a_read_only_place_is_left_as_it_is(tmp_path, monkeypatch):
    from hermit.redact import redact_tree
    monkeypatch.setenv("CLEAN_API_KEY", "sk-clean-key-0123456789abcdef")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "doc.txt").write_text("no key here\n")
    os.chmod(corpus, 0o555)
    try:
        assert redact_tree(corpus) == []
        assert stat_mode(corpus) == 0o555                          # nothing asked for write access
    finally:
        os.chmod(corpus, 0o755)


def test_a_writable_file_grant_is_scrubbed_by_train_and_eval(tmp_path, monkeypatch):
    key = "sk-grant-key-0123456789abcdef"
    monkeypatch.setenv("GRANT_API_KEY", key)
    path = make_task(tmp_path)
    (tmp_path / "state.json").write_text(key)
    Trainer(load(path, "trainer.epochs=1", "environment.write=[state.json]"), log=lambda *_: None).train()
    assert (tmp_path / "state.json").read_text() == "<GRANT_API_KEY>"
    (tmp_path / "state.json").write_text(key)
    evaluate(load(path, "trainer.epochs=1", "environment.write=[state.json]"), "final", log=lambda *_: None)
    assert (tmp_path / "state.json").read_text() == "<GRANT_API_KEY>"


def test_a_configured_link_to_the_evidence_is_scrubbed_at_its_target(tmp_path, monkeypatch):
    key = "sk-linked-evidence-0123456789abcdef"
    monkeypatch.setenv("LINKED_API_KEY", key)
    path = make_task(tmp_path)
    (tmp_path / "store").mkdir()
    (tmp_path / "store" / "x.txt").write_text(key)
    (tmp_path / "evidence-link").symlink_to(tmp_path / "store")
    Trainer(load(path, "trainer.epochs=1", f"evolver_access.environment=[{tmp_path / 'evidence-link'}]"),
            log=lambda *_: None).train()
    assert (tmp_path / "store" / "x.txt").read_text() == "<LINKED_API_KEY>"


def test_a_failed_llm_client_closes_its_sandbox(tmp_path, monkeypatch):
    import hermit.agent.base.endpoint as endpoint
    closed = []
    monkeypatch.setattr(Trainer, "_close_sandboxes", lambda self: closed.append(1))
    monkeypatch.setattr(endpoint, "cli_model", lambda *a, **k: (_ for _ in ()).throw(ValueError("bad timeout")))
    with pytest.raises(ValueError, match="bad timeout"):
        Trainer(load(make_task(tmp_path), "llm={cli: codex, model: gpt-x, login: chatgpt, web_search: false}"),
                log=lambda *_: None)
    assert closed


def test_a_run_agent_log_is_copied_whole_and_a_failed_copy_leaves_the_result(tmp_path, monkeypatch):
    """Two runs of one task at once may write one log (Harness²'s stages, the toy pipeline's .logs/<task>.txt): the
    copy the learner opens reads the file once, never through an archive's size check, which failed when the other
    run rewrote it (OSError: unexpected end of data) and with it the whole run_agent call."""
    import hermit.trainer.trainer as trainer_module
    trainer = Trainer.__new__(Trainer)
    trainer.run_dir = tmp_path / "run"
    log = tmp_path / ".logs" / "t0.txt"
    log.parent.mkdir()
    log.write_text("question 1+2 answer 3")
    copied = trainer._visible_log({"id": "t0", "log": str(log)})
    assert Path(copied).read_text() == "question 1+2 answer 3"
    (tmp_path / "logdir").mkdir()                                  # a directory log goes through the archive
    monkeypatch.setattr(trainer_module, "sanitized_copy", lambda *a, **k: (_ for _ in ()).throw(
        OSError("unexpected end of data")))
    assert trainer._visible_log({"id": "t1", "log": str(tmp_path / "logdir")}) is None


def test_a_name_that_holds_a_model_key_is_renamed(tmp_path, monkeypatch):
    """A learner can put a key in a name (skills/<value>/SKILL.md) with clean contents: the name is scrubbed too, and
    with it what the adapter, the archive and the tree keep."""
    from hermit.redact import redact_tree
    key = "sk-named-key-0123456789abcdef"
    monkeypatch.setenv("NAMED_API_KEY", key)
    (tmp_path / "skills" / key).mkdir(parents=True)
    (tmp_path / "skills" / key / "SKILL.md").write_text("clean\n")
    (tmp_path / f"note-{key}.md").write_text("clean\n")
    changed = redact_tree(tmp_path)
    names = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    assert not any(key in n for n in names) and "skills/<NAMED_API_KEY>/SKILL.md" in names
    assert "note-<NAMED_API_KEY>.md" in names and changed


def test_a_key_in_a_name_under_a_read_only_directory_and_a_long_name(tmp_path, monkeypatch):
    from hermit.redact import redact_tree
    key = "sk-k-0123456789abcdef"
    monkeypatch.setenv("SHORT_API_KEY", key)
    sealed = tmp_path / "sealed"
    sealed.mkdir()
    (sealed / f"{key}.md").write_text("clean")
    long_name = "x" * (255 - len(key) - 3) + key + ".md"
    (tmp_path / long_name).write_text("clean")
    os.chmod(sealed, 0o555)
    try:
        redact_tree(tmp_path)
        names = [p.name for p in tmp_path.rglob("*")]
        assert not any(key in n for n in names) and all(len(n.encode()) <= 255 for n in names)
        assert (sealed / "<SHORT_API_KEY>.md").exists()
    finally:
        os.chmod(sealed, 0o755)




def test_a_link_target_and_a_name_not_utf8_lose_their_key(tmp_path, monkeypatch):
    from hermit.redact import redact_name, redact_tree
    key = "sk-target-key-0123456789abcdef"
    monkeypatch.setenv("TARGET_API_KEY", key)
    (tmp_path / "sessions" / key).mkdir(parents=True)
    (tmp_path / "current").symlink_to(f"sessions/{key}")
    redact_tree(tmp_path)
    assert os.readlink(tmp_path / "current") == "sessions/<TARGET_API_KEY>"
    assert (tmp_path / "current").is_dir()                         # leads where the renamed directory is
    raw = os.fsdecode(b"raw\xff-" + key.encode() + b".txt")
    assert redact_name(raw) == os.fsdecode(b"raw\xff-<TARGET_API_KEY>.txt")


def test_a_link_follows_its_directory_through_a_name_collision(tmp_path, monkeypatch):
    """sessions/<value>/ is renamed to <VAR>_1 when <VAR> is taken: current -> sessions/<value> follows it there,
    not to the other directory."""
    from hermit.redact import redact_tree
    key = "sk-collide-key-0123456789abcdef"
    monkeypatch.setenv("COLLIDE_API_KEY", key)
    (tmp_path / "sessions" / key).mkdir(parents=True)
    (tmp_path / "sessions" / key / "mine.txt").write_text("mine")
    (tmp_path / "sessions" / "<COLLIDE_API_KEY>").mkdir()
    (tmp_path / "sessions" / "<COLLIDE_API_KEY>" / "other.txt").write_text("other")
    (tmp_path / "current").symlink_to(f"sessions/{key}")
    redact_tree(tmp_path)
    assert (tmp_path / "current" / "mine.txt").read_text() == "mine"
    assert key not in os.readlink(tmp_path / "current")
