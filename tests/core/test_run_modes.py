"""Run modes, the config's `run:` block (hermit/config.py), through the entry points a user calls: Config.load, the
Trainer and hermit.trainer.trainer.train, hermit.evaluate's evaluate and checkpoint_tests, run_check, and the CLI
(hermit.cli.main). On the toy task (tests/toy_task.py: no model, no network, the sandbox off); the stand-ins are the
task's own pipelines and judges below, one of them shaped like a Harbor trial (the output and curated log of
hermit/harbor/runner.py `_output` and curate.py, its verifier output holding the gold answer), and small Evolvers.

Covers: validation by command (hermit eval needs no training keys, nor a template but for root on a tree that does
not exist yet, run.mode eval refuses hermit train, a stream's rules, empty sections, a key of a section left empty or
left out of a whole section given at once (one it does not have refused, left empty too), the eval section only where
an eval runs (and on hermit check's run line only there; evaluate()'s own check of eval.repeats), a feedback below
the Evolver's min_feedback (the tutorial's and the notes example's Evolvers' too) or below what its params make it
read, a stream's validation rows that are train rows, checkpoints checked against the stream, and every order's,
before training, a checkpoint's `after` that YAML reads as a number, and one of Harbor's own acting agents, which
takes no adapter); the feedback policy in the learning material and in run_agent's results (a timeout that is the
verifier's verdict included), and an Evolver that hides the labels itself; run_agent's scope in a stream, and no step
of a multi-step training task; a run that is not the agent's result (infra) left out of every mean and counted, and
run again by eval (a step of a multi-step task never alone, a row with a `steps` field of its own as any row); each
repeat's rows; pass@k by Harbor task and by step, and what counts as solved; the stream summary and its card (an
Evolver's own scores, a row its protocol leaves unscored, a stream it leaves wholly unscored, a stream its budget
stopped); data.shuffle's order of a multi-step task's rows and of any other row (a task named twice included), and an
Evolver's request of a step ahead of an earlier one refused, and so (before any run, and by hermit check) an order
taken as given that lists one (an order file, a stream's split, an unshuffled split, a seed's rows of a task) and a
split an eval or a train tests on that does, hermit.harbor.dataset's splits never, nor an eval config's train split,
which hermit eval never runs; orders; checkpoint tests after training, during it (outside the training budget's
hours), resumed by hermit eval --checkpoints, and of a run that recorded no step; a Harbor agent's streams stopped
with the pipeline of a train that finished, kept by one stopped midway; a stream's final adapter; split mode's test
of the final adapter; eval on another split; hermit check of an eval config; the run's new secrets.
"""
import json
import os
from pathlib import Path
import random
import re

import pytest
import yaml

from hermit.cli import main
from hermit.config import Config
from hermit.evaluate import evaluate
from hermit.settings_check import run_check
from hermit.task import TaskData
from hermit.trainer.trainer import Trainer, train, train_order, warnings_for
from hermit.tree import git

from toy_task import PIPELINE, make_task

QUIET = {"log": lambda *_: None}
STREAM = ("evolver.name=learn", "run.mode=stream", "data.step_size=2", "trainer.epochs=1")

EVOLVERS = '''
import json
import shutil
from hermit.agent.evolver.base import Evolver, FinalResult, StepResult


class Learn(Evolver):
    """Writes the toy rule at every step; runs params.probe through run_agent and keeps what came back
    (state/probes.jsonl); names params.next_at_1 as step 2's tasks."""
    min_feedback = "none"                    # it reads no verdict

    def step(self, ctx):
        (ctx.workspace.adapter / "ADAPTER.md").write_text("Always add the numbers.\\n")
        step = ctx.progress.step
        if ctx.params.get("probe"):
            out = ctx.tools["run_agent"](task_ids=list(ctx.params["probe"]))
            with open(ctx.workspace.state / "probes.jsonl", "a") as f:
                f.write(json.dumps({"step": step, "results": out["results"]}) + "\\n")
        return StepResult("done", f"step {step}", next_tasks=ctx.params.get("next_at_1") if step == 1 else None)


class Hiding(Learn):
    hides_labels = True


class Choose(Learn):
    chooses_tasks = True


class Swap(Learn):
    """Hands back another adapter at the end, as a method that picks its final on validation does."""

    def finalize(self, ctx):
        other = ctx.workspace.scratch / "picked"
        shutil.copytree(ctx.workspace.adapter, other, ignore=shutil.ignore_patterns(".git"))
        (other / "ADAPTER.md").write_text("picked on validation\\n")
        return FinalResult(adapter=other, summary="picked another adapter")


class Graded(Learn):
    """Scores its own steps, as Harness²'s protocol does: rows of other steps, and a score that is no number, are
    not taken."""

    def step_scores(self, result):
        return {"t0": 0.25, "t1": 0.25, "t2": "high", "t3": 0.75, "t9": 1.0}


class Unscored(Learn):
    """Its protocol leaves rows unscored (None), as Harness²'s does a deliverable whose execution was infra."""

    def step_scores(self, result):
        return {"t0": 0.25, "t1": None, "t3": None}


class Ungraded(Learn):
    """Its protocol leaves every row unscored, as Harness²'s does a stream whose every execution was infra."""

    def step_scores(self, result):
        return {f"t{i}": None for i in range(6)}


class Gated(Learn):
    """Reads each run's score, and with params.gate soft what else the judge returns (as SkillOpt's gate_metric)."""
    min_feedback = "score"

    @classmethod
    def min_feedback_for(cls, params):
        return "full" if params.get("gate") == "soft" else cls.min_feedback
'''

# A Harbor trial's shape (hermit/harbor/runner.py _output, curate.py): the verdict in output.reward/rewards/status and
# pipeline_extra, the verifier's output (here the gold answer, as LAB-Bench's and OfficeQA's verifiers print it) and
# the trial's result files in its log directory.
HARBORISH = '''
import json
from pathlib import Path
from hermit.task import RunOutput

def run(task, adapter):
    text = (Path(adapter) / "ADAPTER.md").read_text() if adapter else ""
    answer = str(task["a"] + task["b"]) if "add the numbers" in text else "?"
    reward = float(answer == str(task["a"] + task["b"]))
    log = Path(__file__).parent / ".logs" / task["id"]
    (log / "verifier").mkdir(parents=True, exist_ok=True)
    (log / "agent").mkdir(exist_ok=True)
    (log / "agent" / "agent-codex.jsonl").write_text(json.dumps({"answer": answer}) + "\\n")
    (log / "verifier" / "test-stdout.txt").write_text(f"expected GOLD-{task['a'] + task['b']}\\n")
    (log / "verifier" / "reward.txt").write_text(f"{reward}\\n")
    for name in ("result.json", "harbor.log", "exception.txt", "driver.log"):
        (log / name).write_text(f"reward {reward}: GOLD-{task['a'] + task['b']}\\n")
    output = {"harbor_task": task["id"], "rollout": None, "reward": reward, "rewards": {"reward": reward, "f1": reward},
              "status": "ok", "failure_cause": None, "exception_type": None, "trial": task["id"] + "__a",
              "answer": answer}
    return RunOutput(output=output, log=log, cost_usd=0.01, wall_s=0.1, tokens=100,
                     api_speed={"model_calls": 1, "status": "ok"},
                     extra={"status": "ok", "verifier_reward_raw": reward, "failure_cause": None,
                            "exception_type": None, "exception_message": "", "harbor_job": str(log), "exit_code": 0,
                            "timed_out": False})
'''
HARBORISH_JUDGE = '''
from pathlib import Path

def score(task, output):
    if task["id"] == "t1":                     # a judge that fails names the answer in its message
        raise ValueError(f"expected GOLD-{task['a'] + task['b']}")
    tail = (Path(output.log) / "verifier" / "test-stdout.txt").read_text()
    return {"score": output.output["reward"], "feedback": f"reward {output.output['reward']}; verifier output (tail): "
            f"{tail}", "status": output.output["status"], "rewards": output.output["rewards"]}
'''
HARBORISH_RUN = ("agent.name=harborish.py:run", "judge.path=harborish_judge.py")
# Harbor trials that ran out of time, as hermit/harbor/runner.py's _output gives them (scored by hermit.harbor.judge):
# t0's solution too slow for its verifier (verifier_timeout: agent: the verifier's verdict, solution_timed_out), t1's
# session out of the agent's time (AgentTimeoutError: how the session itself ended); both say timed_out.
TIMED_OUT = '''
from pathlib import Path
from hermit.task import RunOutput

def run(task, adapter):
    cause, error = {"t0": ("solution_timed_out", "VerifierTimeoutError"),
                    "t1": ("agent_exhausted_budget", "AgentTimeoutError")}.get(task["id"], (None, None))
    status = "timeout" if cause else "ok"
    log = Path(__file__).parent / ".logs" / task["id"]
    (log / "agent").mkdir(parents=True, exist_ok=True)
    (log / "agent" / "agent-codex.jsonl").write_text("{}\\n")
    output = {"harbor_task": task["id"], "rollout": None, "reward": None if cause else 1.0, "rewards": None,
              "status": status, "failure_cause": cause, "exception_type": error, "trial": task["id"] + "__a"}
    return RunOutput(output=output, log=log, cost_usd=0.01, wall_s=0.1, tokens=100,
                     api_speed={"model_calls": 1, "status": status},
                     extra={"status": status, "timed_out": cause is not None, "exception_type": error,
                            "exception_message": "", "failure_cause": cause, "halt": False,
                            "verifier_reward_raw": None})
'''

# A judge that fails when it is handed a run the pipeline said is not the agent's result: such a run is never judged.
STRICT_JUDGE = '''
def score(task, output):
    assert not isinstance(output.output, dict), "an unscored run was judged"
    ok = output.output == str(task["a"] + task["b"])
    return {"score": float(ok), "feedback": "correct" if ok else "wrong"}
'''
INFRA = PIPELINE.replace("    text = ", "    if task['id'] in ('t1', 't7'):\n"
                                      "        return RunOutput(output={'never': 'judged'}, cost_usd=0.01,\n"
                                      "                         extra={'unscored': 'model_provider_failed'})\n"
                                      "    text = ")
# Infra for a row's first `flaky` attempts (its count of attempts in .attempts/<id>), then a run.
FLAKY = PIPELINE.replace("    text = ", "    seen = Path(__file__).parent / '.attempts' / task['id']\n"
                                      "    seen.parent.mkdir(exist_ok=True)\n"
                                      "    n = int(seen.read_text()) + 1 if seen.exists() else 1\n"
                                      "    seen.write_text(str(n))\n"
                                      "    if n <= int(task.get('flaky', 0)):\n"
                                      "        cause = task.get('cause', 'sandbox_failed')\n"
                                      "        return RunOutput(output=None,\n"
                                      "                         extra={'unscored': cause, 'failure_cause': cause})\n"
                                      "    text = ")
HOOKS = '''
import json
import os
from pathlib import Path

def state(run_dir, record, commit):
    recorded = len((Path(run_dir) / "steps.jsonl").read_text().splitlines())
    with open(Path(__file__).parent / "state_calls.jsonl", "a") as f:
        f.write(json.dumps({"step": record and record["step"], "commit": commit, "recorded": recorded}) + "\\n")
    return {"HERMIT_TEST_STATE": f"step-{record['step'] if record else 0}"}

def score(records, name):
    return {"name": name, "rows": len(records), "state": os.environ.get("HERMIT_TEST_STATE")}
'''
# The toy pipeline, reporting the checkpoint state's environment it ran with (in api_speed, which results.jsonl keeps).
STATE_PIPELINE = "import os\n" + PIPELINE.replace('api_speed={"tps": 50}',
                                                  'api_speed={"tps": 50, "state": os.environ.get("HERMIT_TEST_STATE")}')
CHECKPOINTS = ("run.checkpoints=[{name: early, after: t1, split: cp.txt, adapters: [checkpoint, root]}, "
               "{name: last, after: end, split: cp.txt, adapters: [checkpoint, final]}]",
               "run.state=hooks.py:state", "run.score=hooks.py:score")


def _task(tmp: Path) -> Path:
    """The toy task (t0-t5 train, t6 val, t7-t9 test), this file's Evolvers and hooks, and a checkpoint split."""
    path = make_task(tmp)
    for cls in ("Learn", "Hiding", "Choose", "Swap", "Graded", "Unscored", "Ungraded", "Gated"):
        (tmp / cls.lower()).mkdir()
        (tmp / cls.lower() / "evolver.py").write_text(EVOLVERS)
        (tmp / cls.lower() / "evolver.yaml").write_text(f"class: evolver.py:{cls}\nneeds: []\n")
    (tmp / "harborish.py").write_text(HARBORISH)
    (tmp / "harborish_judge.py").write_text(HARBORISH_JUDGE)
    (tmp / "hooks.py").write_text(HOOKS)
    (tmp / "cp.txt").write_text("t7\nt8\n")
    return path


def load(path: Path, *overrides: str, command: str | None = None) -> Config:
    return Config.load(path, list(overrides), command=command)


def _rows(tmp: Path, **fields) -> None:
    """Rewrite the toy rows with more fields per id ({id: {field: value}})."""
    rows = [json.loads(line) for line in (tmp / "tasks.jsonl").read_text().splitlines()]
    (tmp / "tasks.jsonl").write_text("".join(json.dumps({**r, **fields.get(r["id"], {})}) + "\n" for r in rows))


def _multi_step(tmp: Path, *tasks: str) -> None:
    """3-step tasks (rows <task>:s1..s3, as hermit.harbor.dataset writes them) the toy pipeline and judge can run."""
    with open(tmp / "tasks.jsonl", "a") as f:
        for task in tasks:
            for i in (1, 2, 3):
                f.write(json.dumps({"id": f"{task}:s{i}", "a": i, "b": 0, "harbor_task": task, "step": f"s{i}",
                                    "step_index": i, "steps": 3}) + "\n")


def _steps(tmp: Path, run: str = "toy-run") -> list[dict]:
    return [json.loads(line) for line in (tmp / "runs" / run / "steps.jsonl").read_text().splitlines()]


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _text(root: Path) -> str:
    return "".join(p.read_text(errors="replace") for p in sorted(root.rglob("*")) if p.is_file() and not p.is_symlink())


def _printed(capsys) -> dict | list:
    """The JSON a command printed after its progress lines."""
    lines = capsys.readouterr().out.splitlines()
    start = next(i for i, line in enumerate(lines) if line in ("{", "["))
    return json.loads("\n".join(lines[start:]))


def _card(tmp: Path, run: str = "toy-run") -> str:
    return git(tmp / "runs" / "trees" / "prompt-only", "show", f"{run}/final:.hermit/CARD.md")


# ---------- validation by command ----------

def test_hermit_eval_needs_no_training_keys_and_names_the_run_after_its_config(tmp_path, capsys):
    """An eval-only config (a baseline) names no train split, template or Evolver; hermit train still needs them."""
    make_task(tmp_path)
    (tmp_path / "baseline.yaml").write_text(yaml.safe_dump({
        "data": {"tasks": "tasks.jsonl", "test": "test.txt"}, "agent": {"name": "pipeline.py:run"},
        "judge": {"path": "judge.py"}, "sandbox": {"enabled": False}}))
    with pytest.raises(ValueError, match="data.train is required"):
        Config.load(tmp_path / "baseline.yaml", command="train")
    assert Config.load(tmp_path / "baseline.yaml", command="eval").run_name() == "baseline"
    assert main(["eval", "--config", str(tmp_path / "baseline.yaml"), "--adapter", "none"]) == 0
    summary = _printed(capsys)
    assert summary["tasks"] == 3 and summary["mean_score"] == 0.0 and summary["unscored"] == 0
    assert len(_jsonl(tmp_path / "runs" / "baseline" / "eval" / "none" / "results.jsonl")) == 3


def test_an_eval_config_tests_its_templates_adapter_as_root_with_no_training_run(tmp_path, capsys):
    """A baseline of the template's own adapter needs no hermit train first: root is the template's adapter, as the
    tree hermit train begins holds it; a name only a training run makes (final) says so."""
    make_task(tmp_path)
    path = tmp_path / "baseline.yaml"
    path.write_text(yaml.safe_dump({"data": {"tasks": "tasks.jsonl", "test": "test.txt"},
                                    "agent": {"name": "pipeline.py:run"}, "judge": {"path": "judge.py"},
                                    "template": "prompt-only@1.0", "sandbox": {"enabled": False},
                                    "run": {"mode": "eval"}}))
    with pytest.raises(SystemExit, match="no hermit train ran"):
        evaluate(load(path, command="eval"), "final", **QUIET)
    assert main(["eval", "--config", str(path), "--adapter", "root"]) == 0
    assert _printed(capsys)["tasks"] == 3
    assert len(_jsonl(tmp_path / "runs" / "baseline" / "eval" / "root" / "results.jsonl")) == 3
    with pytest.raises(SystemExit, match="is no adapter of the run's tree"):    # the tree now holds root alone
        evaluate(load(path, command="eval"), "final", **QUIET)


def test_root_on_a_tree_that_does_not_exist_yet_needs_the_template_and_trainer_tree_dir_alone_is_refused(tmp_path):
    """trainer.tree_dir alone finds a tree that exists: root on one that does not exist yet needs template, whose
    adapter begins the tree, and without one hermit eval refuses it by name, as its other settings, before anything
    runs; once a tree is there, trainer.tree_dir alone tests its root."""
    make_task(tmp_path)
    path = tmp_path / "baseline.yaml"
    path.write_text(yaml.safe_dump({"data": {"tasks": "tasks.jsonl", "test": "test.txt"},
                                    "agent": {"name": "pipeline.py:run"}, "judge": {"path": "judge.py"},
                                    "trainer": {"tree_dir": "tree"}, "sandbox": {"enabled": False},
                                    "run": {"mode": "eval"}}))
    args = ["eval", "--config", str(path), "--adapter", "root"]
    tree = (tmp_path / "tree").resolve()
    with pytest.raises(SystemExit, match=re.escape(f"root on a tree that does not exist yet ({tree}, trainer.tree_dir) "
                                                   "needs template, whose adapter begins the tree")):
        main(args)
    assert not tree.exists() and not (tmp_path / "runs").exists()
    assert main([*args, "template=prompt-only@1.0"]) == 0 and (tree / ".git").exists()
    assert main(args) == 0
    assert len(_jsonl(tmp_path / "runs" / "baseline" / "eval" / "root" / "results.jsonl")) == 3


def test_an_eval_config_is_refused_by_hermit_train_and_checked_as_hermit_eval_runs_it(tmp_path, capsys):
    make_task(tmp_path)
    path = tmp_path / "baseline.yaml"
    path.write_text(yaml.safe_dump({"data": {"tasks": "tasks.jsonl", "test": "test.txt"},
                                    "agent": {"name": "pipeline.py:run"}, "judge": {"path": "judge.py"},
                                    "sandbox": {"enabled": False}, "run": {"mode": "eval"}}))
    assert main(["train", "--config", str(path)]) == 2
    assert "run.mode is eval: this config is for hermit eval" in capsys.readouterr().err
    lines = []
    assert run_check(Config.load(path, command="check"), log=lines.append)
    text = "\n".join(lines)
    assert "[ok] run — mode eval, feedback full" in text and "[ok] evolver — not used" in text
    assert "[ok] budgets — hermit eval runs without a budget" in text and "[ok] agent probe — task t7:" in text


def test_a_stream_names_its_feedback_runs_one_epoch_and_takes_its_rows_in_order(tmp_path):
    path = _task(tmp_path)
    with pytest.raises(ValueError, match="run.mode: stream needs run.feedback"):
        load(path, *STREAM)
    with pytest.raises(ValueError, match="trainer.epochs must be 1"):
        load(path, *STREAM, "run.feedback=full", "trainer.epochs=2")
    config = load(path, *STREAM, "run.feedback=full")
    assert config["data"]["shuffle"] is False            # the toy task leaves the default, true: set, and said
    assert any("data.shuffle was set to false" in w for w in warnings_for(config, val_given=True))
    for key, value in (("orders", "[1]"), ("checkpoints", "[{name: a, after: t1}]")):
        with pytest.raises(ValueError, match=f"run.{key} is for run.mode: stream"):
            load(path, "evolver.name=learn", "run.mode=split", f"run.{key}={value}")
    with pytest.raises(ValueError, match="run.feedback is full, score or none"):
        load(path, "run.feedback=some")


def test_an_empty_run_or_eval_section_is_its_defaults_and_eval_repeats_reads_as_hermit_eval_reads_it(tmp_path):
    """`run:` or `eval:` left empty (null) is that section's defaults, an override of one of its keys included;
    eval.repeats is read as hermit eval has always read it ("3" and 2.0 are 3 and 2); a run block that is no section
    is refused, by name."""
    path = make_task(tmp_path)
    path.write_text(yaml.safe_dump({**yaml.safe_load(path.read_text()), "run": None, "eval": None}))
    config = load(path, "eval.infra_retries=2")
    assert config.mode is None and config["eval"] == {"repeats": 1, "infra_retries": 2, "infra_wait_s": 600}
    assert [load(path, f"eval.repeats={value}", command="eval")["eval"]["repeats"] for value in ("'3'", "2.0")] == \
        [3, 2]
    with pytest.raises(ValueError, match="run is a section of settings"):
        load(path, "run=stream")


def test_the_eval_section_is_checked_only_where_an_eval_runs(tmp_path):
    """A training run that tests no adapter never reads eval.*: eval.repeats 0 trains, as it did before the run modes,
    loaded for hermit train, for hermit check or by the Python API (Config.load with no command, then
    Trainer(config).train()). hermit eval refuses it as the config loads, and so does every command for an eval
    config or a training run that tests an adapter (split's test of the final adapter on data.test, a stream's
    checkpoint tests). (eval.repeats null is its default, 1: the next test.)"""
    path = _task(tmp_path)
    tests_one = (("run.mode=split",), (*STREAM, "run.feedback=full", "run.checkpoints=[{name: a, after: t1}]"))
    for command in ("train", "check"):
        load(path, "evolver.name=learn", "eval.repeats=0", command=command)
    config = Config.load(path, ["evolver.name=learn", "trainer.epochs=1", "eval.repeats=0",
                                "trainer.run_name=repeats-0"])           # the Python API
    assert Trainer(config, **QUIET).train()["steps"] == 2
    for command, tests in [("eval", ()), *[(command, ("run.mode=eval",)) for command in ("eval", "check", None)],
                           *[(command, t) for command in ("train", "check", None) for t in tests_one]]:
        with pytest.raises(ValueError, match="eval.repeats must be a whole number of at least 1, not 0"):
            load(path, "evolver.name=learn", *tests, "eval.repeats=0", command=command)


def test_a_key_of_run_or_eval_left_empty_or_left_out_of_a_whole_section_is_its_default(tmp_path, capsys):
    """A whole section given at once (`eval={repeats: 2}`, which hermit eval took before the run modes) keeps the
    defaults of the keys it leaves out, for the overrides after it too; a key left empty (null), in the file or by an
    override, is its default, as a section left empty is; a key the section does not have is refused, as in the
    file."""
    path = _task(tmp_path)
    defaults = {"repeats": 1, "infra_retries": 0, "infra_wait_s": 600}
    assert load(path, "eval={repeats: 2}", command="eval")["eval"] == {**defaults, "repeats": 2}
    assert load(path, "eval={repeats: 2}", "eval.infra_retries=3", command="eval")["eval"] == \
        {**defaults, "repeats": 2, "infra_retries": 3}
    assert load(path, "eval.repeats=null", "eval.infra_retries=null", "eval.infra_wait_s=null",
                command="eval")["eval"] == defaults
    config = load(path, "evolver.name=learn", "trainer.epochs=1", "run={mode: stream, feedback: full}",
                  "run.checkpoints=[{name: a, after: t1}]", "run.checkpoints_during=null")
    assert (config.mode, config.feedback, config["run"]["checkpoints_during"]) == ("stream", "full", False)
    assert [c["name"] for c in config.checkpoints()] == ["a"]
    assert load(path, "run=null", "run.mode=split", "evolver.name=learn").mode == "split"
    with pytest.raises(ValueError, match="Unknown config key: eval.repeat$"):
        load(path, "eval={repeat: 2}", command="eval")
    path.write_text(yaml.safe_dump({**yaml.safe_load(path.read_text()), "eval": {"infra_retries": None},
                                    "run": {"checkpoints_during": None}}))
    assert load(path, command="eval")["eval"] == defaults and load(path)["run"]["checkpoints_during"] is False
    assert main(["eval", "--config", str(path), "--adapter", "none", "eval={repeats: 2}"]) == 0
    assert _printed(capsys)["repeats"] == 2


def test_a_whole_section_override_naming_a_key_its_section_does_not_have_is_refused_even_left_empty(tmp_path):
    """`eval={repeat: null}` names a key the eval section does not have, as `eval: {repeat: null}` in the file and
    `eval.repeat=null` do: it is refused by name as they are, not read as a key left empty (its default)."""
    path = _task(tmp_path)
    for item, key in (("eval={repeat: null}", "eval.repeat"), ("run={mod: null}", "run.mod")):
        with pytest.raises(ValueError, match=f"Unknown config key: {key}$"):
            load(path, "evolver.name=learn", item)


def test_evaluate_through_the_python_api_refuses_eval_repeats_below_one(tmp_path):
    """A training config the Python API loaded (Config.load with no command) is not checked for eval.repeats as it
    loads: evaluate() refuses eval.repeats 0, or one that is no number, itself, before any pipeline, as hermit eval
    refuses it as the config loads."""
    path = _task(tmp_path)
    for value in ("0", "'x'"):
        config = load(path, "evolver.name=learn", f"eval.repeats={value}")
        with pytest.raises(ValueError, match=f"eval.repeats must be a whole number of at least 1, not {value}$"):
            evaluate(config, "none", **QUIET)
    assert not (tmp_path / "runs" / "toy-run" / "eval").exists()           # no eval started: nothing written


def test_hermit_checks_run_line_names_evals_runs_again_of_infra_rows_only_where_an_eval_runs(tmp_path):
    """eval.infra_retries is on hermit check's run line where an eval runs (an eval config, split's test of the final
    adapter on data.test, a stream's checkpoint tests), whose eval section the config's load checked; a training run
    that tests no adapter (train only, a split without data.test, a stream without checkpoints) leaves its eval section
    unchecked and unused, so its run line says nothing of it (-1 included)."""
    path = _task(tmp_path)
    retry = ("eval.infra_retries=2", "eval.infra_wait_s=0")
    checkpoint = (*STREAM, "run.feedback=full", "run.checkpoints=[{name: a, after: t1}]")
    for overrides, evaluates in ((("evolver.name=learn", *retry), False),
                                 (("evolver.name=learn", "eval.infra_retries=-1"), False),
                                 (("evolver.name=learn", "run.mode=split", "data.test=null", *retry), False),
                                 ((*STREAM, "run.feedback=full", *retry), False),
                                 (("run.mode=eval", *retry), True),
                                 (("evolver.name=learn", "run.mode=split", *retry), True),
                                 ((*checkpoint, *retry), True)):
        lines = []
        run_check(load(path, *overrides, command="check"), probe=False, log=lines.append)
        line = next(line for line in lines if line.startswith("[ok] run — "))
        named = line.endswith(", eval runs infra rows again up to 2 time(s), 0 s apart")
        assert (named, "infra rows again" in line) == (evaluates, evaluates), line


def test_a_feedback_below_what_the_evolver_reads_is_refused_before_anything_runs_and_named_by_check(tmp_path):
    """GEPA chooses by the judge's scores (its min_feedback: score): run.feedback none is refused before anything
    runs, naming both, and hermit check reports it; a CLI Evolver needs no verdict (its session reads the material
    as the run shows it)."""
    path = _task(tmp_path)
    gepa = (*STREAM, "run.feedback=none", "evolver.name=gepa")
    with pytest.raises(SystemExit, match=r"run.feedback is none, below what the Evolver gepa reads \(its "
                                         r"min_feedback score\): set run.feedback to score or full"):
        train(load(path, *gepa), **QUIET)
    assert not (tmp_path / "runs").exists()
    lines = []
    assert not run_check(load(path, *gepa, command="check"), probe=False, log=lines.append)
    assert "    - run.feedback is none, below what the Evolver gepa reads (its min_feedback score): set run.feedback " \
           "to score or full" in lines
    lines = []
    assert run_check(load(path, *STREAM, "run.feedback=none", "evolver.name=codex", "evolver.params.web_search=false",
                          command="check"), probe=False, log=lines.append), lines


def test_hermit_check_names_a_feedback_below_what_the_evolver_reads_with_its_params(tmp_path):
    """An Evolver whose params decide the least it reads (min_feedback_for, as SkillOpt's gate_metric does): hermit
    check reports run.feedback score below what it reads with params.gate soft, naming the params, and takes score
    with any other gate."""
    path = _task(tmp_path)
    refused = "    - run.feedback is score, below what the Evolver gated reads (its min_feedback full with these " \
              "evolver.params): set run.feedback to full"
    for gate, reads_full in (("soft", True), ("hard", False)):
        lines = []
        ok = run_check(load(path, *STREAM, "run.feedback=score", "evolver.name=gated", f"evolver.params.gate={gate}",
                            command="check"), probe=False, log=lines.append)
        assert (refused in lines, ok) == (reads_full, not reads_full), lines


def test_the_tutorials_evolver_reads_the_judges_feedback_and_is_refused_a_feedback_that_hides_it(tmp_path):
    """docs/_examples/first_training's learner (the custom-Evolver tutorial's) reads each record's judge feedback:
    under run.feedback score it would fail on the first record, so hermit train refuses it before anything runs."""
    run = Path(__file__).resolve().parents[2] / "docs" / "_examples" / "first_training" / "run.yaml"
    with pytest.raises(SystemExit, match=re.escape("run.feedback is score, below what the Evolver learner reads (its "
                                                   "min_feedback full): set run.feedback to full")):
        train(load(run, f"trainer.output_dir={tmp_path / 'runs'}", "run.feedback=score"), **QUIET)
    assert not (tmp_path / "runs").exists()


def test_the_notes_evolver_example_writes_the_judges_feedback_and_is_refused_a_feedback_that_hides_it(tmp_path):
    """docs/_examples/notes_evolver turns the judge's feedback on each failed task into a note: under run.feedback
    score it would write a 'no feedback' note for each, so hermit train refuses it before anything runs."""
    path = _task(tmp_path)
    notes = Path(__file__).resolve().parents[2] / "docs" / "_examples" / "notes_evolver"
    with pytest.raises(SystemExit, match=re.escape("run.feedback is score, below what the Evolver notes_evolver "
                                                   "reads (its min_feedback full): set run.feedback to full")):
        train(load(path, f"evolver.name={notes}", "run.feedback=score"), **QUIET)
    assert not (tmp_path / "runs").exists()


def test_a_stream_whose_validation_rows_are_train_rows_is_refused_before_training(tmp_path):
    """run_agent runs a validation row at any step: one that is a train row too could be run before the stream scores
    it. A split may still share them (hermit check reports it, as before)."""
    path = _task(tmp_path)
    (tmp_path / "val.txt").write_text("t6\nt5\n")
    with pytest.raises(SystemExit, match=r"train refuses to start: run.mode: stream: the train split and the "
                                         r"validation split share 1 row\(s\) \(e.g. \['t5'\]\)"):
        train(load(path, *STREAM, "run.feedback=full"), **QUIET)
    assert not (tmp_path / "runs" / "toy-run").exists()
    assert Trainer(load(path, "evolver.name=learn", "trainer.epochs=1"), **QUIET).train()["steps"] == 2


def test_a_checkpoint_inside_a_step_or_outside_the_stream_is_refused_before_training(tmp_path):
    """The steps' tasks are known ahead for an Evolver that does not choose them: a checkpoint must follow a step."""
    path = _task(tmp_path)
    with pytest.raises(SystemExit, match="after 't2' is inside step 2's tasks .* name the step's last task, 't3'"):
        Trainer(load(path, *STREAM, "run.feedback=full", "run.checkpoints=[{name: mid, after: t2, split: cp.txt}]"),
                **QUIET).train()
    with pytest.raises(SystemExit, match="after 't9' is not a row of the train split"):
        Trainer(load(path, *STREAM, "run.feedback=full", "run.checkpoints=[{name: out, after: t9, split: cp.txt}]"),
                **QUIET).train()
    assert not (tmp_path / "runs" / "toy-run" / "steps.jsonl").exists()


def test_a_checkpoint_after_a_task_id_yaml_reads_as_a_number_is_refused_saying_to_quote_it(tmp_path):
    """`after: 42` is the number 42 to YAML, which no task id is: the refusal says to write it in quotes, as the split
    file spells it (it names no id to write: YAML may have read `012` as 10)."""
    path = _task(tmp_path)
    with pytest.raises(ValueError, match=re.escape("run.checkpoints a: after is a task id of the train split (the last "
                                                   "task of a step), or end, not the number 42 (YAML read the id as a "
                                                   "number): write the task id in quotes, as the split file spells "
                                                   "it")):
        load(path, *STREAM, "run.feedback=full", "run.checkpoints=[{name: a, after: 42, split: cp.txt}]")
    assert load(path, *STREAM, "run.feedback=full",
                "run.checkpoints=[{name: a, after: '42', split: cp.txt}]").checkpoints()[0]["after"] == "42"
    with pytest.raises(ValueError, match=r"after is a task id of the train split \(the last task of a step\), or end$"):
        load(path, *STREAM, "run.feedback=full", "run.checkpoints=[{name: a, after: [t1], split: cp.txt}]")


def test_one_of_harbors_own_acting_agents_takes_no_adapter_so_training_and_an_adapters_eval_are_refused(tmp_path):
    """A Harbor task set's acting agent that is one of Harbor's own (by name, or a class that is no HermitXXX; here
    in the task set's benchmark file) takes no adapter, so every rollout of a training run would fail: hermit train
    refuses it before anything runs and hermit check reports it; hermit eval refuses any adapter but none."""
    path = _task(tmp_path)
    (tmp_path / "utils").mkdir()
    harbor = ("agent.name=harbor", "judge.path=hermit.harbor.judge",
              "agent.params={benchmark: utils/benchmark.yaml, web_search: false}")
    for acting, name in (("{name: oracle}", "oracle"), ("{import_path: 'my_agents:Mine'}", "my_agents:Mine")):
        (tmp_path / "utils" / "benchmark.yaml").write_text(f"tasks_dir: tasks\njobs_dir: {tmp_path / 'jobs'}\n"
                                                           f"harbor: {tmp_path / 'none'}\nacting_agent: {acting}\n")
        refused = f"the acting agent {name} (agent.params.acting_agent) is one of Harbor's own agents, which takes " \
                  "no adapter"
        with pytest.raises(SystemExit, match=re.escape(f"train refuses to start: {refused}: every rollout would fail")):
            train(load(path, *harbor, "evolver.name=learn", command="train"), **QUIET)
        lines = []
        assert not run_check(load(path, *harbor, "evolver.name=learn", command="check"), probe=False, log=lines.append)
        assert any(line.startswith(f"    - {refused}") for line in lines[lines.index("[PROBLEM] task"):])
        with pytest.raises(SystemExit, match=re.escape(f"{refused}: evaluate it with --adapter none")):
            evaluate(load(path, *harbor, command="eval"), "root", **QUIET)
    assert not (tmp_path / "runs").exists()


# ---------- the feedback policy ----------

def test_score_feedback_shows_each_score_alone_and_none_of_the_verifiers_output(tmp_path):
    path = _task(tmp_path)
    Trainer(load(path, *STREAM, *HARBORISH_RUN, "run.feedback=full", "trainer.run_name=full"), **QUIET).train()
    assert "GOLD-" in _text(tmp_path / "runs" / "full" / "learning_material")   # full: the answer is in the feedback
    Trainer(load(path, *STREAM, *HARBORISH_RUN, "run.feedback=score"), **QUIET).train()
    material = tmp_path / "runs" / "toy-run" / "learning_material"
    assert "GOLD-" not in _text(material)
    record = json.loads((material / "step_0002" / "tasks" / "t2.json").read_text())
    assert record["judge"] == {"score": 1.0}
    assert record["output"] == {"harbor_task": "t2", "rollout": None, "trial": "t2__a", "answer": "6"}
    assert record["pipeline_extra"] == {"exit_code": 0, "timed_out": False}
    assert record["api_speed"] == {"model_calls": 1}
    assert sorted(p.name for p in (material / "step_0002" / "trajectories" / "t2").iterdir()) == ["agent"]
    assert [line["mean_score"] for line in _jsonl(material / "index.jsonl")] == [0.0, 1.0, 1.0]
    assert "its score alone" in (material / "README.md").read_text()


def test_no_feedback_shows_no_verdict_in_the_material_or_in_run_agents_results(tmp_path):
    path = _task(tmp_path)
    Trainer(load(path, *STREAM, *HARBORISH_RUN, "run.feedback=none", "evolver.params.probe=[t0, t6]"),
            **QUIET).train()
    run = tmp_path / "runs" / "toy-run"
    material = run / "learning_material"
    assert "GOLD-" not in _text(material) + _text(run / "agent_logs") + _text(run / "workspace" / "state")
    record = json.loads((material / "step_0001" / "tasks" / "t0.json").read_text())
    assert "judge" not in record and "reward" not in record["output"] and record["status"] == "ok"
    assert "mean_score" not in json.loads((material / "step_0001" / "summary.json").read_text())
    assert not any("mean_score" in line for line in _jsonl(material / "index.jsonl"))
    shown = _jsonl(run / "workspace" / "state" / "probes.jsonl")[0]["results"]
    assert [r["id"] for r in shown] == ["t0", "t6"] and not any("judge" in r for r in shown)
    assert sorted(p.name for p in Path(shown[0]["log"]).iterdir()) == ["agent"]     # its log without the verdict's
    assert "log" not in shown[1]                                                    # a validation run names none
    assert "You are shown no verdict on any task" in (material / "README.md").read_text()


def test_an_evolver_that_hides_labels_itself_is_given_the_full_records(tmp_path):
    from hermit.agent.evolver.methods.harness2_upstream.evolver import Harness2UpstreamEvolver
    assert Harness2UpstreamEvolver.hides_labels is True
    path = _task(tmp_path)
    Trainer(load(path, *STREAM, "run.feedback=none", "evolver.name=hiding"), **QUIET).train()
    run = tmp_path / "runs" / "toy-run"
    record = json.loads((run / "learning_material" / "step_0001" / "tasks" / "t0.json").read_text())
    assert record["judge"]["feedback"] == "expected the sum of 0 and 0"
    assert yaml.safe_load((run / "config.yaml").read_text())["run"] == {"mode": "stream", "feedback": "none",
                                                                          "shown": "full"}


def test_a_step_that_ended_its_stream_under_its_min_reward_is_a_verdict_score_and_none_leave_out(tmp_path):
    """A multi-step task's step scored under its min_reward ends the task's stream (hermit.harbor.runner's
    pipeline_extra.aborted), which says how the step was scored."""
    from hermit.trainer.material import Material
    record = {"id": "m:s1", "input": {"id": "m:s1"}, "status": "ok", "output": {"harbor_task": "m", "step": "s1"},
              "judge": {"score": 0.0, "feedback": "reward 0.0"}, "cost_usd": 0.0, "wall_s": 1.0, "log": None,
              "pipeline_extra": {"aborted": True, "exit_code": 0}}
    extra = {}
    for policy in ("full", "score", "none"):
        step = Material(tmp_path / policy, policy=policy).write(1, 1, "c0", False, [record], None)
        extra[policy] = json.loads((step / "tasks" / "m_s1.json").read_text())["pipeline_extra"]
    assert extra == {"full": {"aborted": True, "exit_code": 0}, "score": {"exit_code": 0}, "none": {"exit_code": 0}}


def test_score_and_no_feedback_show_no_timeout_that_is_the_verifiers_verdict_and_keep_the_sessions_own(tmp_path):
    """A solution too slow for its verifier (verifier_timeout: agent) times out by the verifier's verdict: under score
    and none, neither the material nor run_agent's results say it timed out (timed_out false, as for a run that did
    not); a session that ran out of the agent's time still says so, and full shows both as the rollout made them."""
    path = _task(tmp_path)
    (tmp_path / "timed.py").write_text(TIMED_OUT)
    for policy in ("full", "score", "none"):
        Trainer(load(path, *STREAM, "agent.name=timed.py:run", "judge.path=hermit.harbor.judge",
                     f"run.feedback={policy}", f"trainer.run_name={policy}", "evolver.params.probe=[t0, t1]"),
                **QUIET).train()
        run = tmp_path / "runs" / policy
        tasks = run / "learning_material" / "step_0001" / "tasks"
        material = {t: json.loads((tasks / f"{t}.json").read_text())["pipeline_extra"]["timed_out"]
                    for t in ("t0", "t1")}
        probed = {r["id"]: r["timed_out"] for r in _jsonl(run / "workspace" / "state" / "probes.jsonl")[0]["results"]}
        too_slow = policy == "full"
        assert material == probed == {"t0": too_slow, "t1": True}, policy


# ---------- run_agent ----------

def test_run_agent_in_a_stream_runs_the_rows_reached_and_validation_rows_and_says_why_it_refuses_others(tmp_path):
    path = _task(tmp_path)
    Trainer(load(path, *STREAM, "run.feedback=full", "evolver.params.probe=[t0, t3, t6, t9]"), **QUIET).train()
    seen = {p["step"]: {r["id"]: r for r in p["results"]} for p in
            _jsonl(tmp_path / "runs" / "toy-run" / "workspace" / "state" / "probes.jsonl")}
    assert seen[1]["t0"]["status"] == "ok" and seen[1]["t6"]["status"] == "ok"     # step 1 ran t0, t1; t6: validation
    assert seen[1]["t3"]["status"] == "error" and "an earlier step of the stream" in seen[1]["t3"]["error"]
    assert seen[2]["t3"]["status"] == "ok" and seen[3]["t9"]["status"] == "error"   # step 2 ran t2, t3; t9: test
    Trainer(load(path, "evolver.name=learn", "trainer.epochs=1", "trainer.run_name=split",
                 "evolver.params.probe=[t5, t9]"), **QUIET).train()
    first = {r["id"]: r for r in _jsonl(tmp_path / "runs" / "split" / "workspace" / "state" / "probes.jsonl")[0]
             ["results"]}
    assert first["t5"]["status"] == "ok"                     # a training task, however far ahead (as before)
    assert first["t9"]["status"] == "error" and "not a training or validation task" in first["t9"]["error"]


def test_run_agent_runs_no_step_of_a_multi_step_training_task_and_says_why(tmp_path):
    """A training row that is a step of a multi-step task runs in the Trainer's own stream of its task: run by
    run_agent it would find its turn taken (a step the Trainer ran) or take the Trainer's (one it has not reached,
    whose own step would then be lost). It comes back refused, as any row run_agent does not run, in a split (the
    validation split naming one of them too) and in a stream, and the tool's description says so; a validation task's
    step runs as before, and so does a training row of a task of another kind with a `steps` field of its own."""
    path = _task(tmp_path)
    _multi_step(tmp_path, "m", "v")
    _rows(tmp_path, t2={"steps": 4})
    (tmp_path / "ms.txt").write_text("t0\nt1\nm:s1\nm:s2\nm:s3\nt2\n")
    probe = ("data.train=ms.txt", "evolver.params.probe=[m:s1, m:s3, t2, v:s1]")
    for run, args, val in (("split", ("evolver.name=learn", "run.mode=split", "trainer.epochs=1"), "t6\nv:s1\nm:s3\n"),
                           ("stream", (*STREAM, "run.feedback=full"), "t6\nv:s1\n")):
        (tmp_path / "val.txt").write_text(val)
        Trainer(load(path, *args, *probe, f"trainer.run_name={run}"), **QUIET).train()
        last = {r["id"]: r for r in _jsonl(tmp_path / "runs" / run / "workspace" / "state" / "probes.jsonl")[-1]
                ["results"]}                                  # the last step's: every row reached, in the stream too
        for step in ("m:s1", "m:s3"):
            assert last[step]["status"] == "error", (run, last[step])
            assert f"{step!r} is a step of a multi-step training task, which runs only in the Trainer's own stream" \
                in last[step]["error"], run
        assert last["t2"]["status"] == "ok" and last["v:s1"]["status"] == "ok", run
    tool = Trainer(load(path, "evolver.name=learn", *probe), **QUIET)._run_agent_tool()
    assert "on training or validation tasks (not a step of a multi-step training task) through" in tool.description


# ---------- runs that are not the agent's result ----------

def test_a_run_that_is_not_the_agents_result_is_infra_left_out_of_every_mean_and_counted(tmp_path):
    path = _task(tmp_path)
    (tmp_path / "pipeline.py").write_text(INFRA)
    (tmp_path / "judge.py").write_text(STRICT_JUDGE)
    config = load(path, *STREAM, "run.feedback=full")
    summary = Trainer(config, **QUIET).train()
    first = _steps(tmp_path)[0]["rollout"]
    assert first["scores"] == {"t0": 0.0, "t1": None} and first["unscored"] == 1 and first["mean_score"] == 0.0
    material = tmp_path / "runs" / "toy-run" / "learning_material" / "step_0001"
    t1 = json.loads((material / "tasks" / "t1.json").read_text())
    assert t1["status"] == "infra" and t1["error"] == "model_provider_failed"       # not judged: no judge_error
    assert t1["judge"] == {"score": 0.0, "feedback": "not scored: model_provider_failed", "extra": {"unscored": True}}
    assert {k: json.loads((material / "summary.json").read_text())[k] for k in ("mean_score", "unscored")} == \
        {"mean_score": 0.0, "unscored": 1}
    assert {k: summary["stream"][k] for k in ("scored", "unscored", "mean")} == {"scored": 5, "unscored": 1,
                                                                                  "mean": 0.8}
    result = evaluate(config, "final", **QUIET)                       # t7 infra, t8 and t9 solved
    assert {k: result[k] for k in ("mean_score", "solved_once", "scored", "unscored", "errors")} == \
        {"mean_score": 1.0, "solved_once": 1.0, "scored": 2, "unscored": 1, "errors": 0}


def test_eval_runs_its_infra_rows_again_and_stops_at_a_halt(tmp_path):
    path = _task(tmp_path)
    (tmp_path / "pipeline.py").write_text(FLAKY)
    _rows(tmp_path, t7={"flaky": 1}, t8={"flaky": 2})
    retry = ("eval.infra_retries=3", "eval.infra_wait_s=0")
    result = evaluate(load(path, *retry), "none", **QUIET)
    assert result["unscored"] == 0 and result["scored"] == 3 and result["infra_rerun"] == 3
    attempts = {i: int((tmp_path / ".attempts" / i).read_text()) for i in ("t7", "t8", "t9")}
    assert attempts == {"t7": 2, "t8": 3, "t9": 1}
    # a halt (a lost login, a usage limit) is not run again: every rerun would end the same way
    for name in ("t7", "t8", "t9"):
        (tmp_path / ".attempts" / name).unlink()
    _rows(tmp_path, t7={"flaky": 1}, t8={"flaky": 9, "cause": "halted"})
    lines = []
    result = evaluate(load(path, *retry), "none", log=lines.append)
    assert result["unscored"] == 2 and result["infra_rerun"] == 0 and any("a halt" in line for line in lines)


def test_eval_runs_no_step_of_a_multi_step_task_again_alone(tmp_path):
    """A step runs in its task's stream, after the steps before it: run again alone it would start the stream over and
    wait for a turn that never comes. Its infra run is kept, and the log says how many and why."""
    path = _task(tmp_path)
    (tmp_path / "pipeline.py").write_text(FLAKY)
    _rows(tmp_path, t7={"flaky": 1}, t8={"flaky": 1, "harbor_task": "m", "step": "s2", "step_index": 2, "steps": 2})
    lines = []
    result = evaluate(load(path, "eval.infra_retries=2", "eval.infra_wait_s=0"), "none", log=lines.append)
    attempts = {i: int((tmp_path / ".attempts" / i).read_text()) for i in ("t7", "t8")}
    assert attempts == {"t7": 2, "t8": 1} and result["unscored"] == 1 and result["infra_rerun"] == 1
    assert any("1 infra run(s) are steps of multi-step tasks, not run again" in line for line in lines)


def test_eval_runs_again_the_infra_run_of_a_row_whose_own_steps_field_makes_no_step_of_a_multi_step_task(tmp_path):
    """A step of a multi-step task is a Harbor row (its `harbor_task`) with `steps` > 1, as the Trainer reads it:
    another task's own `steps` field (t7's) makes no step of one, and eval runs its infra run again, as it always
    did; a Harbor step row (t8) it does not."""
    path = _task(tmp_path)
    (tmp_path / "pipeline.py").write_text(FLAKY)
    _rows(tmp_path, t7={"flaky": 1, "steps": 4},
          t8={"flaky": 1, "harbor_task": "m", "step": "s2", "step_index": 2, "steps": 2})
    result = evaluate(load(path, "eval.infra_retries=2", "eval.infra_wait_s=0"), "none", **QUIET)
    attempts = {i: int((tmp_path / ".attempts" / i).read_text()) for i in ("t7", "t8")}
    assert attempts == {"t7": 2, "t8": 1} and result["unscored"] == 1 and result["infra_rerun"] == 1


def test_each_repeat_hands_the_pipeline_rows_that_name_it_and_so_do_its_runs_again(tmp_path):
    """A pipeline that keeps its trials by row (Harbor's trial cache) would hand every repeat repeat 1's trials: with
    eval.repeats > 1 each repeat's rows, and its infra rows run again, name the repeat; one repeat's rows are as they
    are."""
    path = _task(tmp_path)
    (tmp_path / "pipeline.py").write_text(FLAKY.replace(
        "    seen = ", "    with open(Path(__file__).parent / 'seen.txt', 'a') as f:\n"
                      "        f.write(f\"{task['id']} {task.get('repeat')}\\n\")\n    seen = ", 1))
    _rows(tmp_path, t7={"flaky": 1})                  # repeat 1's run of t7 is infra, and runs again
    result = evaluate(load(path, "eval.repeats=2", "eval.infra_retries=1", "eval.infra_wait_s=0"), "none", **QUIET)
    seen = sorted((tmp_path / "seen.txt").read_text().splitlines())
    assert seen == ["t7 1", "t7 1", "t7 2", "t8 1", "t8 2", "t9 1", "t9 2"] and result["unscored"] == 0
    (tmp_path / "seen.txt").unlink()
    evaluate(load(path), "none", **QUIET)
    assert sorted((tmp_path / "seen.txt").read_text().splitlines()) == ["t7 None", "t8 None", "t9 None"]


def test_an_eval_that_fails_leaves_none_of_an_earlier_evals_results(tmp_path):
    """results.jsonl is emptied when an eval starts: one that failed in its first repeat leaves no rows of the eval
    before it looking like its own."""
    path = _task(tmp_path)
    evaluate(load(path), "none", **QUIET)
    results = tmp_path / "runs" / "toy-run" / "eval" / "none" / "results.jsonl"
    assert len(_jsonl(results)) == 3
    (tmp_path / "pipeline.py").write_text(PIPELINE.replace("    text = ", "    raise SystemExit('the eval stopped')\n"
                                                                          "    text = "))
    with pytest.raises(SystemExit, match="the eval stopped"):
        evaluate(load(path), "none", **QUIET)
    assert _jsonl(results) == []


def _harbor_rows(tmp: Path, judge: str) -> tuple[str, ...]:
    """Two Harbor tasks of two rows each (x#1, x#2, y#1, y#2), a pipeline whose output names the task and solves x#2
    alone, and `judge` (its body, given `solved`): the overrides that evaluate them."""
    with open(tmp / "tasks.jsonl", "a") as f:
        for task in ("x", "y"):
            for k in (1, 2):
                f.write(json.dumps({"id": f"{task}#{k}", "harbor_task": task}) + "\n")
    (tmp / "pk.txt").write_text("x#1\nx#2\ny#1\ny#2\n")
    (tmp / "pk.py").write_text("from hermit.task import RunOutput\n\ndef run(task, adapter):\n"
                               "    return RunOutput(output={'harbor_task': task['harbor_task'], "
                               "'solved': task['id'] == 'x#2'})\n")
    (tmp / "pk_judge.py").write_text(f"def score(task, output):\n    solved = output.output['solved']\n    {judge}\n")
    return "data.test=pk.txt", "agent.name=pk.py:run", "judge.path=pk_judge.py"


def test_pass_at_k_counts_a_harbor_tasks_rows_and_repeats_as_its_attempts(tmp_path):
    path = _task(tmp_path)
    run = _harbor_rows(tmp_path, "return {'score': float(solved)}")
    result = evaluate(load(path, *run, "eval.repeats=2"), "none", **QUIET)
    # x solved by one of its 4 attempts (2 rows x 2 repeats), y by none; per row, x#2 alone
    assert {k: result[k] for k in ("k", "pass_at_k", "solved_once", "mean_score")} == \
        {"k": 4, "pass_at_k": 0.5, "solved_once": 0.25, "mean_score": 0.25}


def test_the_judges_passed_decides_what_is_solved_and_a_scale_has_no_pass(tmp_path):
    """A floor score of 1.0 that is no pass (AlgoTune's invalid solvers) is not solved when the judge says so; a
    benchmark whose reward is a scale (pass_reward: null, `passed` None) gets no share of solved tasks made up."""
    path = _task(tmp_path)
    run = _harbor_rows(tmp_path, "return {'score': 1.0, 'passed': solved}")
    result = evaluate(load(path, *run), "none", **QUIET)
    assert {k: result[k] for k in ("mean_score", "pass_at_k", "solved_once")} == \
        {"mean_score": 1.0, "pass_at_k": 0.5, "solved_once": 0.25}
    (tmp_path / "pk_judge.py").write_text("def score(task, output):\n    return {'score': 0.5, 'passed': None}\n")
    result = evaluate(load(path, *run), "none", **QUIET)
    assert {k: result[k] for k in ("mean_score", "pass_at_k", "solved_once")} == \
        {"mean_score": 0.5, "pass_at_k": None, "solved_once": None}


def test_a_row_a_scale_judge_gave_no_verdict_leaves_the_solved_shares_unknown(tmp_path):
    """A benchmark whose reward is a scale has no pass (its judge returns passed: None): a row whose judge failed
    (judge_error, no `passed`) is no fail either."""
    path = _task(tmp_path)
    run = _harbor_rows(tmp_path, "if task['id'] == 'y#1':\n        raise ValueError('no verdict')\n"
                                 "    return {'score': 0.5, 'passed': None}")
    result = evaluate(load(path, *run), "none", **QUIET)
    assert {k: result[k] for k in ("mean_score", "errors", "pass_at_k", "solved_once")} == \
        {"mean_score": 0.375, "errors": 1, "pass_at_k": None, "solved_once": None}


def test_a_passed_that_is_no_bool_is_no_verdict_and_the_score_decides(tmp_path):
    """ASPIRE's judge returns `passed` as the number of seeds passed: 1 of 5 (score 0.2) on three rows, 5 of 5 on x#2.
    A count is no pass or fail: a score of at least 1 decides, as before `passed` was read."""
    path = _task(tmp_path)
    run = _harbor_rows(tmp_path, "n = 5 if solved else 1\n"
                                 "    return {'score': n / 5, 'feedback': f'{n}/5 seeds', 'passed': n, 'seeds': 5}")
    result = evaluate(load(path, *run), "none", **QUIET)
    assert {k: result[k] for k in ("solved_once", "pass_at_k")} == {"solved_once": 0.25, "pass_at_k": 0.5}


def test_a_step_of_a_multi_step_task_is_a_task_of_its_own_whose_attempts_are_its_rollouts(tmp_path):
    """Two 3-step tasks (rows <task>:<step>, as hermit.harbor.dataset writes them), only m's first step passing, in
    both repeats: each step is a task, its 2 repeats its attempts (not m one task of 6 attempts, one of them passed)."""
    path = _task(tmp_path)
    with open(tmp_path / "tasks.jsonl", "a") as f:
        for task in ("m", "n"):
            for i, step in enumerate(("s1", "s2", "s3"), start=1):
                f.write(json.dumps({"id": f"{task}:{step}", "harbor_task": task, "step": step, "step_index": i,
                                    "steps": 3}) + "\n")
    (tmp_path / "ms.txt").write_text("".join(f"{task}:{step}\n" for task in "mn" for step in ("s1", "s2", "s3")))
    (tmp_path / "ms.py").write_text("from hermit.task import RunOutput\n\ndef run(task, adapter):\n"
                                    "    return RunOutput(output={'harbor_task': task['harbor_task'], "
                                    "'step': task['step'], 'reward': float(task['id'] == 'm:s1')})\n")
    (tmp_path / "ms_judge.py").write_text("def score(task, output):\n    return {'score': output.output['reward']}\n")
    result = evaluate(load(path, "data.test=ms.txt", "agent.name=ms.py:run", "judge.path=ms_judge.py",
                           "eval.repeats=2"), "none", **QUIET)
    assert {k: result[k] for k in ("k", "pass_at_k", "solved_once")} == {"k": 2, "pass_at_k": 1 / 6,
                                                                         "solved_once": 1 / 6}


# ---------- a stream's score ----------

def test_a_stream_reports_its_prequential_score_and_its_halves(tmp_path):
    """The rows of step 1 are scored by the starting adapter, those after by what step 1 taught: 0, 0, 1, 1, 1, 1."""
    path = _task(tmp_path)
    summary = Trainer(load(path, *STREAM, "run.feedback=none"), **QUIET).train()
    assert summary["stream"] == {"rows": 6, "reached": 6, "scored": 6, "unscored": 0, "mean": 4 / 6,
                                 "first_half": 1 / 3, "second_half": 1.0, "coverage": 1.0}
    run = tmp_path / "runs" / "toy-run"
    assert [s["mean"] for s in json.loads((run / "stream_summary.json").read_text())["steps"]] == [0.0, 1.0, 1.0]
    assert json.loads((run / "summary.json").read_text())["stream"] == summary["stream"]
    card = _card(tmp_path)
    assert "## Stream" in card and "Mean 0.667; first half 0.333, second half 1.000" in card
    assert "Each of the stream's 6 rows scored once, by its rollout before the learner saw it: 6 scored" in card
    assert "feedback none: the learner was shown no verdict" in card


def test_an_evolver_that_scores_its_own_steps_gives_the_stream_its_score_with_the_rollouts_beside(tmp_path):
    """Harness²'s protocol scores each step's task itself (the deliverable it chose without the labels): an Evolver's
    step_scores take the rollouts' place for the step's tasks it gives a number, the rollouts' mean kept beside.
    Rollouts 0, 0, 1, 1, 1, 1; its own 0.25 (t0), 0.25 (t1), 0.75 (t3)."""
    path = _task(tmp_path)
    summary = Trainer(load(path, *STREAM, "run.feedback=none", "evolver.name=graded"), **QUIET).train()
    stream = summary["stream"]
    assert stream["mean"] == pytest.approx(4.25 / 6) and stream["own_scores"] == 3
    assert stream["rollout_mean"] == pytest.approx(4 / 6)
    assert stream["first_half"] == pytest.approx(0.5) and stream["second_half"] == pytest.approx(2.75 / 3)
    steps = json.loads((tmp_path / "runs" / "toy-run" / "stream_summary.json").read_text())["steps"]
    assert [s.get("own") for s in steps] == [["t0", "t1"], ["t3"], None]
    assert [s.get("own_scores") for s in _steps(tmp_path)] == [{"t0": 0.25, "t1": 0.25}, {"t3": 0.75}, None]
    card = _card(tmp_path)                 # its own scores were not given before the learner saw the rows
    assert "Each of the stream's 6 rows scored once: 6 scored" in card and "before the learner saw it:" not in card
    assert "3 row(s) scored by the Evolver's own protocol (its step's deliverable, Evolver.step_scores) in place of " \
           "the rollout, 3 by their rollouts before the learner saw them; the rollouts' mean 0.667." in card


def test_a_row_the_evolvers_protocol_leaves_unscored_is_counted_and_left_out_of_the_streams_mean(tmp_path):
    """step_scores maps a task to None for a row its protocol leaves unscored (Harness²'s deliverable whose execution
    was infra): the rollout's score does not take its place, and the card counts the row as the summary does:
    unscored, not among the rows their rollouts scored. Rollouts 0, 0, 1, 1, 1, 1; its own 0.25 (t0), none (t1,
    t3)."""
    path = _task(tmp_path)
    stream = Trainer(load(path, *STREAM, "run.feedback=none", "evolver.name=unscored"), **QUIET).train()["stream"]
    assert {k: stream[k] for k in ("scored", "unscored", "own_scores", "coverage")} == \
        {"scored": 4, "unscored": 2, "own_scores": 1, "coverage": round(4 / 6, 4)}
    assert stream["mean"] == pytest.approx(3.25 / 4) and stream["rollout_mean"] == pytest.approx(4 / 6)
    assert stream["first_half"] == pytest.approx(1.25 / 2) and stream["second_half"] == pytest.approx(1.0)
    steps = json.loads((tmp_path / "runs" / "toy-run" / "stream_summary.json").read_text())["steps"]
    assert [(s["scored"], s["unscored"], s.get("own")) for s in steps] == [(1, 1, ["t0", "t1"]), (1, 1, ["t3"]),
                                                                         (2, 0, None)]
    assert [s.get("own_scores") for s in _steps(tmp_path)] == [{"t0": 0.25, "t1": None}, {"t3": None}, None]
    card = _card(tmp_path)
    assert "Each of the stream's 6 rows scored once: 4 scored, 2 not (infra), 0 not reached." in card
    assert "1 row(s) scored by the Evolver's own protocol (its step's deliverable, Evolver.step_scores) in place of " \
           "the rollout, 3 by their rollouts before the learner saw them; the rollouts' mean 0.667." in card


def test_a_stream_whose_evolvers_protocol_scores_no_row_says_so_on_its_card_with_the_rollouts_mean(tmp_path):
    """Harness²'s stream whose every execution was infra: its protocol left every row unscored (own_scores 0), so no
    row is its rollout's either. The card counts the rows as the summary does and gives the rollouts' mean beside."""
    path = _task(tmp_path)
    stream = Trainer(load(path, *STREAM, "run.feedback=none", "evolver.name=ungraded"), **QUIET).train()["stream"]
    assert {k: stream[k] for k in ("scored", "unscored", "own_scores", "mean", "coverage")} == \
        {"scored": 0, "unscored": 6, "own_scores": 0, "mean": None, "coverage": 0.0}
    assert stream["rollout_mean"] == pytest.approx(4 / 6)
    card = _card(tmp_path)
    assert "Each of the stream's 6 rows scored once: 0 scored, 6 not (infra), 0 not reached. Mean n/a;" in card
    assert "0 row(s) scored by the Evolver's own protocol (its step's deliverable, Evolver.step_scores) in place of " \
           "the rollout, 0 by their rollouts before the learner saw them; the rollouts' mean 0.667." in card
    assert "by its rollout before the learner saw it" not in card


def test_a_stream_its_budget_stopped_says_how_much_of_it_was_scored(tmp_path):
    path = _task(tmp_path)
    summary = Trainer(load(path, *STREAM, "run.feedback=full", "trainer.budget.usd=0.03"), **QUIET).train()
    stream = summary["stream"]
    assert stream["reached"] == 2 and stream["coverage"] == round(2 / 6, 4) and stream["second_half"] is None
    assert stream["stopped"].startswith("hard budget")
    card = _card(tmp_path)                       # the rows it did not reach were never scored
    assert "The stream reached 2 of its 6 rows, each scored once, by its rollout before the learner saw it: 2 " \
           "scored, 0 not (infra), 4 not reached. Mean 0.000;" in card and "Each of the stream's" not in card


def test_a_stream_keeps_the_adapter_its_last_step_left_whatever_finalize_picks(tmp_path):
    path = _task(tmp_path)
    summary = Trainer(load(path, *STREAM, "run.feedback=full", "evolver.name=swap"), **QUIET).train()
    tree = tmp_path / "runs" / "trees" / "prompt-only"
    assert "Always add the numbers" in git(tree, "show", "toy-run/final:ADAPTER.md")
    assert any("the final adapter is the one the stream's last step left" in w for w in summary["warnings"])
    Trainer(load(path, "evolver.name=swap", "trainer.epochs=1", "trainer.run_name=split"), **QUIET).train()
    assert "picked on validation" in git(tree, "show", "split/final:ADAPTER.md")     # a split keeps its pick


# ---------- orders ----------

def test_data_shuffle_moves_a_multi_step_tasks_rows_as_one_in_step_order_and_any_other_row_as_before(tmp_path):
    """A step taken ahead of an earlier step of its task waits for it in the task's stream (for hours), then ends
    infra: under data.shuffle (the default), in run.mode null and split, each epoch takes a multi-step task's rows
    together and in step order (the split lists them out of it); a train split without one (a task's own `steps` and
    `step_index` fields make no step of one) is shuffled as it always was, row by row, each copy of a task it names
    twice a row of its own."""
    path = _task(tmp_path)
    _multi_step(tmp_path, "m", "n")
    rows = ["t0", "m:s3", "m:s1", "t1", "n:s1", "m:s2", "n:s2", "t2", "n:s3"]
    (tmp_path / "ms.txt").write_text("\n".join(rows) + "\n")
    for mode in ("null", "split"):
        Trainer(load(path, "evolver.name=learn", f"run.mode={mode}", "data.train=ms.txt",
                     f"trainer.run_name=mode-{mode}"), **QUIET).train()
        for epoch in (1, 2):
            taken = [t for s in _steps(tmp_path, f"mode-{mode}") if s["epoch"] == epoch for t in s["tasks"]]
            assert sorted(taken) == sorted(rows), (mode, epoch, taken)
            for task in "mn":
                first = taken.index(f"{task}:s1")
                assert taken[first:first + 3] == [f"{task}:s1", f"{task}:s2", f"{task}:s3"], (mode, epoch, taken)
    config = load(path, "evolver.name=learn")
    _rows(tmp_path, t3={"steps": 4, "step_index": "last"})
    (tmp_path / "twice.txt").write_text("t0\nt1\nt2\nt0\nt3\nt4\nt5\n")          # a split may name a task twice
    data = TaskData(tmp_path / "tasks.jsonl", tmp_path / "twice.txt", None, None)
    # as data.shuffle always shuffled it: random.Random(data.shuffle_seed 0, plus the epoch).shuffle(the rows)
    assert [train_order(config, data, epoch) for epoch in (1, 2)] == [["t0", "t5", "t4", "t2", "t0", "t3", "t1"],
                                                                      ["t2", "t0", "t1", "t3", "t4", "t0", "t5"]]


def test_an_evolvers_request_of_a_step_ahead_of_an_earlier_step_of_its_task_is_refused_and_the_order_runs(tmp_path):
    """A step taken ahead of an earlier step of its task still to run would wait for it in the task's stream (for
    hours), then end infra, not run again that epoch: an Evolver that chooses its tasks may name a step of a
    multi-step task only when each earlier step of its stream (the task's rollout) has run or comes before it in the
    request. Another request is refused where it is made, as any bad request is, and the next step runs the order's
    tasks; at an epoch's end the request is for the next epoch, which runs every step again."""
    from hermit.agent.evolver.base import Progress, StepResult
    path = _task(tmp_path)
    _multi_step(tmp_path, "m")
    (tmp_path / "ms.txt").write_text("m:s1\nt0\nt1\nt2\nm:s2\nm:s3\n")
    chosen = ("evolver.name=choose", "data.train=ms.txt", "data.step_size=2", "data.shuffle=false", "trainer.epochs=1")
    for run, request, tasks in (("ahead", "[m:s3, m:s2]", [["m:s1", "t0"], ["t1", "t2"], ["m:s2", "m:s3"]]),
                                ("after", "[m:s2, m:s3]", [["m:s1", "t0"], ["m:s2", "m:s3"], ["t1", "t2"]])):
        Trainer(load(path, *chosen, f"evolver.params.next_at_1={request}", f"trainer.run_name={run}"),
                **QUIET).train()
        steps = _steps(tmp_path, run)
        assert [s["tasks"] for s in steps] == tasks, run
        assert [s.get("chosen_by") for s in steps] == [None, None if run == "ahead" else "evolver", None], run
    refused = _steps(tmp_path, "ahead")[0]
    assert "next_tasks" not in refused and refused["next_tasks_refused"] == (
        "['m:s3', 'm:s2']: a step of a multi-step task ahead of an earlier one still to run (a task's steps run in "
        "order, in its stream: name the earlier one first): m:s3 ahead of m:s2")
    assert _steps(tmp_path, "after")[0]["next_tasks"] == ["m:s2", "m:s3"]
    # each rollout of a task is a stream of its own; at an epoch's end (nothing remaining) every step is to run again
    with open(tmp_path / "tasks.jsonl", "a") as f:
        for k in (1, 2):
            for i in (1, 2):
                f.write(json.dumps({"id": f"r:s{i}#{k}", "a": i, "b": 0, "harbor_task": "r", "step": f"s{i}",
                                    "step_index": i, "steps": 2, "rollout": k}) + "\n")
    (tmp_path / "rs.txt").write_text("m:s1\nm:s2\nm:s3\nr:s1#1\nr:s2#1\nr:s1#2\nr:s2#2\n")
    trainer = Trainer(load(path, *chosen, "data.train=rs.txt"), **QUIET)

    def answer(remaining, wanted):
        progress = Progress(1, 1, not remaining, remaining=tuple(remaining))
        return trainer._request(progress, StepResult("done", "chose", next_tasks=wanted))
    left = ["r:s2#1", "r:s1#2", "r:s2#2"]                       # r:s1#1 ran
    assert answer(left, ["r:s2#1"]) == {"next_tasks": ["r:s2#1"]}
    assert answer(left, ["r:s2#2"])["next_tasks_refused"].endswith(": r:s2#2 ahead of r:s1#2")
    assert answer([], ["m:s3", "m:s2"])["next_tasks_refused"].endswith(": m:s3 ahead of m:s1, m:s2 ahead of m:s1")
    assert answer([], ["m:s1", "m:s2"]) == {"next_tasks": ["m:s1", "m:s2"]}


AHEAD = ("lists a step of a multi-step task ahead of an earlier step of its stream, which it would wait for (up to "
         "hours, then ending infra): m:s2 ahead of m:s1 (task m); a task's steps run in order, each after the one "
         "before it: list them in step order")


def test_an_order_taken_as_given_that_lists_a_step_ahead_of_an_earlier_one_is_refused_before_training(tmp_path):
    """A step taken ahead of an earlier step of its task waits for it in the task's stream (for hours), then ends
    infra, and so does each step after it: an order the Trainer takes as it is given (an order file, a stream's own
    split, a split under data.shuffle false, a seed's rows of a task) that lists one is refused before anything runs,
    naming the task and the steps, and hermit check reports it; the same rows in step order train."""
    path = _task(tmp_path)
    _multi_step(tmp_path, "m")
    (tmp_path / "ahead.txt").write_text("t0\nm:s2\nt1\nm:s1\nm:s3\n")
    (tmp_path / "inorder.txt").write_text("t0\nm:s1\nt1\nm:s2\nm:s3\n")
    stream = (*STREAM, "run.feedback=full")
    for args, which in (((*stream, "run.orders=[ahead.txt]", "data.train=null"), "order 0's train split"),
                        ((*stream, "run.orders=[3]", "data.train=ahead.txt"), "order 0's train split"),
                        ((*stream, "data.train=ahead.txt"), "the train split"),
                        (("evolver.name=learn", "data.shuffle=false", "data.train=ahead.txt"), "the train split")):
        order = "order 0: " if "order 0" in which else ""
        with pytest.raises(SystemExit, match=re.escape(f"train refuses to start: {order}{which} {AHEAD}")):
            train(load(path, *args), **QUIET)
        lines = []
        assert not run_check(load(path, *args, command="check"), probe=False, log=lines.append)
        assert f"    - {which} {AHEAD}" in lines, args
    assert not (tmp_path / "runs").exists()
    assert run_check(load(path, *stream, "data.train=inorder.txt", command="check"), probe=False, log=lambda *_: None)
    train(load(path, *stream, "data.train=inorder.txt"), **QUIET)
    assert [s["tasks"] for s in _steps(tmp_path)] == [["t0", "m:s1"], ["t1", "m:s2"], ["m:s3"]]


def test_each_order_is_a_run_of_its_own_and_streams_json_combines_them(tmp_path, capsys):
    """A seed orders the tasks, each task's rows together (a Harbor task's #k rows share its harbor_task)."""
    path = _task(tmp_path)
    tasks = {"p": ["t0", "t1"], "q": ["t2", "t3"], "r": ["t4", "t5"]}
    _rows(tmp_path, **{i: {"harbor_task": name} for name, ids in tasks.items() for i in ids})
    args = ["train", "--config", str(path), *STREAM, "run.feedback=full", "run.orders=[3, 7]"]
    assert main(args) == 0
    combined = _printed(capsys)
    for i, seed in enumerate((3, 7)):
        names = sorted(tasks)
        random.Random(seed).shuffle(names)
        assert [s["tasks"] for s in _steps(tmp_path, f"toy-run-o{i}")] == [tasks[n] for n in names]
    assert [(o["order"], o["seed"], o["run"]) for o in combined["orders"]] == [(0, 3, "toy-run-o0"),
                                                                              (1, 7, "toy-run-o1")]
    assert combined["mean"] == 4 / 6 and all(o["mean"] == 4 / 6 for o in combined["orders"])
    assert json.loads((tmp_path / "runs" / "toy-run" / "streams.json").read_text()) == combined
    assert main(args) == 0 and "finished before; kept" in capsys.readouterr().out
    assert len(_steps(tmp_path, "toy-run-o0")) == 3


def test_an_order_file_is_its_runs_train_split(tmp_path):
    path = _task(tmp_path)
    (tmp_path / "o1.txt").write_text("t5\nt4\nt3\nt2\n")
    (tmp_path / "o2.txt").write_text("t1\nt0\n")
    combined = train(load(path, *STREAM, "run.feedback=full", "run.orders=[o1.txt, o2.txt]", "data.train=null"),
                     **QUIET)
    assert [s["tasks"] for s in _steps(tmp_path, "toy-run-o0")] == [["t5", "t4"], ["t3", "t2"]]
    assert [s["tasks"] for s in _steps(tmp_path, "toy-run-o1")] == [["t1", "t0"]]
    assert combined["orders"][0]["file"] == str((tmp_path / "o1.txt").resolve())


def test_every_orders_checkpoints_are_checked_before_the_first_order_trains(tmp_path):
    """Seed 3 ends step 2 with t5 (t3, t5); seed 7 runs t5 inside step 2 (t5, t3): order 1's checkpoint is refused
    before order 0 trains."""
    path = _task(tmp_path)
    with pytest.raises(SystemExit, match="train refuses to start: order 1: run.checkpoints mid: after 't5' is inside "
                                         "step 2's tasks"):
        train(load(path, *STREAM, "run.feedback=full", "run.orders=[3, 7]",
                   "run.checkpoints=[{name: mid, after: t5, split: cp.txt}]"), **QUIET)
    assert not (tmp_path / "runs").exists()


def test_resume_mode_disable_starts_a_finished_order_over_as_it_does_a_run(tmp_path):
    path = _task(tmp_path)
    args = (*STREAM, "run.feedback=full", "run.orders=[3]")
    train(load(path, *args), **QUIET)
    lines = []
    train(load(path, *args, "trainer.resume_mode=disable"), log=lines.append)
    assert not any("finished before; kept" in line for line in lines)
    assert [p.name.partition(".replaced-")[0] for p in (tmp_path / "runs").iterdir() if ".replaced-" in p.name] == \
        ["toy-run-o0"]
    assert len(_steps(tmp_path, "toy-run-o0")) == 3


def test_a_stream_that_recorded_no_step_has_no_checkpoint_to_test_and_its_orders_go_on(tmp_path):
    """Its budget stopped each order during step 1: no commit to test, and the log says so; each run finishes, the
    next order runs, and hermit train again keeps both."""
    path = _task(tmp_path)
    args = (*STREAM, "run.feedback=full", "run.orders=[3, 7]", "trainer.budget.usd=0.005",
            "run.checkpoints=[{name: last, after: end, split: cp.txt, adapters: [final]}]")
    lines = []
    combined = train(load(path, *args), log=lines.append)
    assert [o["stop_reason"].partition(":")[0] for o in combined["orders"]] == ["hard budget", "hard budget"]
    assert sum("the run recorded no step, so no checkpoint has a commit to test" in line for line in lines) == 2
    summary = json.loads((tmp_path / "runs" / "toy-run-o1" / "summary.json").read_text())
    assert summary["steps"] == 0 and summary["checkpoint_tests"] == {"last": {}}
    assert train(load(path, *args), **QUIET)["orders"] == combined["orders"]


# ---------- checkpoints ----------

def test_checkpoints_test_the_commit_a_step_left_with_the_benchmarks_state_and_score(tmp_path):
    path = _task(tmp_path)
    (tmp_path / "pipeline.py").write_text(STATE_PIPELINE)
    summary = Trainer(load(path, *STREAM, "run.feedback=full", *CHECKPOINTS), **QUIET).train()
    run = tmp_path / "runs" / "toy-run"
    early, last = json.loads((run / "checkpoints.json").read_text())["checkpoints"]
    assert early["step"] == 1 and early["commit"] == _steps(tmp_path)[0]["commit"]    # what step 1 taught
    assert early["adapters"]["checkpoint"]["label"] == "checkpoint-early-checkpoint"
    assert early["adapters"]["checkpoint"]["score"] == {"name": "early", "rows": 2, "state": "step-1"}
    assert last["step"] == 3 and last["adapters"]["final"]["commit"] == last["commit"]
    assert summary["checkpoint_tests"] == {"early": {"checkpoint": 1.0, "root": 0.0},
                                           "last": {"checkpoint": 1.0, "final": 1.0}}
    assert json.loads((run / "summary.json").read_text())["checkpoint_tests"] == summary["checkpoint_tests"]
    rows = _jsonl(run / "checkpoints" / "early" / "root" / "results.jsonl")
    assert {r["api_speed"]["state"] for r in rows} == {"step-1"}       # the state's environment reached the agent
    assert "HERMIT_TEST_STATE" not in os.environ                       # ... and only for the tests
    assert [(c["step"], c["recorded"]) for c in _jsonl(tmp_path / "state_calls.jsonl")] == [(1, 3), (3, 3)]


def test_a_checkpoints_test_runs_right_after_the_step_that_reaches_it_when_asked(tmp_path):
    """Its final adapter's test waits for the end of training."""
    path = _task(tmp_path)
    Trainer(load(path, *STREAM, "run.feedback=full", "run.checkpoints_during=true", "run.state=hooks.py:state",
                 "run.checkpoints=[{name: early, after: t1, split: cp.txt, adapters: [checkpoint, final]}]"),
            **QUIET).train()
    assert [(c["step"], c["recorded"]) for c in _jsonl(tmp_path / "state_calls.jsonl")] == [(1, 1), (1, 3)]


def test_a_checkpoints_test_during_training_spends_none_of_the_training_budgets_hours(tmp_path, monkeypatch):
    """Its cost goes to its own eval, outside trainer.budget, and so does its time: a test that takes an hour (the
    budget's clock moved on while it runs) leaves the stream's half-hour budget to its steps."""
    import time
    from types import SimpleNamespace
    import hermit.evaluate
    import hermit.trainer.budget
    path = _task(tmp_path)
    late = {"s": 0.0}
    monkeypatch.setattr(hermit.trainer.budget, "time", SimpleNamespace(time=lambda: time.time() + late["s"]))
    tests = hermit.evaluate.checkpoint_tests

    def an_hour_long(*args, **kwargs):
        late["s"] += 3600
        return tests(*args, **kwargs)
    monkeypatch.setattr(hermit.evaluate, "checkpoint_tests", an_hour_long)
    during = ("run.checkpoints_during=true", "run.checkpoints=[{name: early, after: t1, split: cp.txt}]")
    summary = Trainer(load(path, *STREAM, "run.feedback=full", "trainer.budget.hours=0.5", *during), **QUIET).train()
    assert summary["stop_reason"] == "completed all epochs" and summary["steps"] == 3
    assert summary["checkpoint_tests"] == {"early": {"checkpoint": 1.0}} and summary["spending"]["seconds"] < 1800


def test_hermit_eval_checkpoints_runs_only_the_tests_without_results(tmp_path, capsys):
    path = _task(tmp_path)
    Trainer(load(path, *STREAM, "run.feedback=full", *CHECKPOINTS), **QUIET).train()
    run = tmp_path / "runs" / "toy-run" / "checkpoints"
    (run / "early" / "root" / "summary.json").unlink()                # as if stopped while it ran
    kept = (run / "early" / "checkpoint" / "results.jsonl").stat().st_mtime_ns
    (tmp_path / "state_calls.jsonl").unlink()
    assert main(["eval", "--config", str(path), "--checkpoints", *STREAM, "run.feedback=full", *CHECKPOINTS]) == 0
    assert _printed(capsys) == {"early": {"checkpoint": 1.0, "root": 0.0}, "last": {"checkpoint": 1.0, "final": 1.0}}
    assert [c["step"] for c in _jsonl(tmp_path / "state_calls.jsonl")] == [1]        # early's state, once
    assert (run / "early" / "checkpoint" / "results.jsonl").stat().st_mtime_ns == kept


def test_a_checkpoint_inside_a_chosen_steps_tasks_stops_the_run_until_it_is_fixed(tmp_path):
    """An Evolver that chooses its tasks makes the steps' tasks known only as they run: checked when reached."""
    path = _task(tmp_path)
    chosen = (*STREAM, "run.feedback=full", "evolver.name=choose", "evolver.params.next_at_1=[t3, t2]")
    inside = "run.checkpoints=[{name: mid, after: t3, split: cp.txt}]"
    with pytest.raises(SystemExit, match="train stops: .*after 't3' is inside step 2's tasks"):
        Trainer(load(path, *chosen, inside), **QUIET).train()
    assert [s["tasks"] for s in _steps(tmp_path)] == [["t0", "t1"], ["t3", "t2"]]      # the steps so far are kept
    with pytest.raises(SystemExit, match="train refuses to go on"):
        Trainer(load(path, *chosen, inside), **QUIET).train()
    summary = Trainer(load(path, *chosen, "run.checkpoints=[{name: mid, after: t2, split: cp.txt}]"), **QUIET).train()
    assert summary["checkpoint_tests"] == {"mid": {"checkpoint": 1.0}} and summary["steps"] == 3


def test_split_mode_tests_the_final_adapter_on_the_test_split_when_training_ends(tmp_path):
    path = _task(tmp_path)
    summary = Trainer(load(path, "evolver.name=learn", "trainer.epochs=1", "run.mode=split"), **QUIET).train()
    assert summary["test"]["mean_score"] == 1.0 and summary["test"]["tasks"] == 3
    assert json.loads((tmp_path / "runs" / "toy-run" / "summary.json").read_text())["test"] == summary["test"]
    assert "| final | 3 | 1 | 1.000 | 1.000 |" in _card(tmp_path)


def test_eval_on_another_split_keeps_its_own_results_and_leaves_the_card(tmp_path, capsys):
    path = _task(tmp_path)
    Trainer(load(path, "evolver.name=learn", "trainer.epochs=1"), **QUIET).train()
    (tmp_path / "other.txt").write_text("t6\n")
    assert main(["eval", "--config", str(path), "--adapter", "final", "--split", str(tmp_path / "other.txt"),
                 "evolver.name=learn"]) == 0
    result = _printed(capsys)
    assert result["tasks"] == 1 and result["split"] == str((tmp_path / "other.txt").resolve())
    run = tmp_path / "runs" / "toy-run"
    assert len(_jsonl(run / "eval" / "final-other" / "results.jsonl")) == 1
    assert "Not evaluated yet" in _card(tmp_path) and "test" not in json.loads((run / "summary.json").read_text())


def test_a_split_that_lists_a_step_ahead_of_an_earlier_one_is_refused_before_any_eval_or_train_runs_it(tmp_path):
    """hermit eval runs a split's rows in its order (one after the other with agent.concurrency 1): a step ahead of an
    earlier step of its stream would wait for it, then end infra. data.test and a --split file are refused before
    any run, naming the task and the steps, and hermit check of an eval config reports it; hermit train refuses such a
    split it would test on before it trains (split mode's test of the final adapter, a stream's checkpoint split). The
    same rows in step order are evaluated."""
    path = _task(tmp_path)
    _multi_step(tmp_path, "m")
    (tmp_path / "ahead.txt").write_text("t7\nm:s2\nt8\nm:s1\nm:s3\n")
    (tmp_path / "inorder.txt").write_text("t7\nm:s1\nt8\nm:s2\nm:s3\n")
    ahead = (tmp_path / "ahead.txt").resolve()
    for overrides, split in ((("data.test=ahead.txt",), None), ((), ahead)):
        with pytest.raises(SystemExit, match=re.escape(f"eval refuses to start: the split {ahead} {AHEAD}")):
            evaluate(load(path, *overrides, command="eval"), "none", split=split, **QUIET)
    with pytest.raises(SystemExit, match=re.escape(f"train refuses to start: the test split {AHEAD}")):
        train(load(path, "evolver.name=learn", "run.mode=split", "data.test=ahead.txt"), **QUIET)
    checkpoint = "run.checkpoints=[{name: early, after: t1, split: ahead.txt}]"
    with pytest.raises(SystemExit, match=re.escape(f"train refuses to start: run.checkpoints early: split {ahead} "
                                                   f"{AHEAD}")):
        train(load(path, *STREAM, "run.feedback=full", checkpoint), **QUIET)
    assert not (tmp_path / "runs").exists()
    lines = []
    assert not run_check(load(path, "run.mode=eval", "data.test=ahead.txt", command="check"), probe=False,
                         log=lines.append)
    assert f"    - the test split {AHEAD}" in lines
    result = evaluate(load(path, "data.test=inorder.txt"), "none", **QUIET)
    assert result["tasks"] == 5 and result["scored"] == 5


def test_hermit_check_of_an_eval_config_refuses_no_order_of_the_train_split_hermit_eval_never_runs(tmp_path):
    """An eval config may name a train split (a training config run as run.mode eval), which hermit eval never runs:
    hermit check reports no step of it ahead of an earlier one, though data.shuffle false would take it as it is (its
    test split it checks: the test above)."""
    path = _task(tmp_path)
    _multi_step(tmp_path, "m")
    (tmp_path / "ahead.txt").write_text("t0\nm:s2\nt1\nm:s1\nm:s3\n")
    lines = []
    assert run_check(load(path, "run.mode=eval", "data.shuffle=false", "data.train=ahead.txt", command="check"),
                     probe=False, log=lines.append), lines
    assert "[ok] run — mode eval, feedback full" in lines


# ---------- run secrets ----------

def test_order_files_checkpoint_splits_a_scorer_and_a_harbor_task_sets_directories_are_run_secrets(tmp_path):
    from hermit.isolate import run_secrets
    path = _task(tmp_path)
    (tmp_path / "orders").mkdir()
    (tmp_path / "orders" / "o1.txt").write_text("t0\nt1\n")
    stream = (*STREAM, "run.feedback=full", "run.orders=[orders/o1.txt]", "run.score=hooks.py:score",
              "run.checkpoints=[{name: a, after: t1, split: cp.txt}]")
    secrets = set(run_secrets(load(path, *stream)))
    assert {(tmp_path / name).resolve() for name in ("orders/o1.txt", "cp.txt", "hooks.py")} <= secrets
    with pytest.raises(ValueError, match="overlaps"):
        load(path, *stream, "evolver_access.environment=[orders]")     # the learner would read the stream's order
    # A Harbor task set: its tasks (their tests and solutions) and its jobs (the trials' raw verifier output).
    (tmp_path / "utils").mkdir()
    (tmp_path / "utils" / "benchmark.yaml").write_text(f"tasks_dir: tasks\njobs_dir: {tmp_path / 'jobs'}\n")
    (tmp_path / "baseline.yaml").write_text(yaml.safe_dump({
        "data": {"tasks": "tasks.jsonl", "test": "test.txt"}, "judge": {"path": "hermit.harbor.judge"},
        "agent": {"name": "harbor", "params": {"benchmark": "utils/benchmark.yaml", "web_search": False,
                                               "acting_agent": {"name": "oracle"}}},
        "sandbox": {"enabled": False}, "run": {"mode": "eval"}}))
    secrets = set(run_secrets(Config.load(tmp_path / "baseline.yaml", command="eval")))
    assert {(tmp_path / "utils" / "tasks").resolve(), (tmp_path / "jobs").resolve()} <= secrets
