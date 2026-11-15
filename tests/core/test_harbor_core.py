"""What the core does for a Harbor task set, through the entry points a user calls: Config.load, the Trainer,
hermit.evaluate's evaluate, run_check, and the CLI (hermit.cli.main). On the toy task (tests/toy_task.py: no model, no
network, the sandbox off); the stand-ins are the task's own pipelines and judges below, and small Evolvers.

Covers: validation by command (hermit eval needs no training keys, nor a template but for an adapter of the run's
tree, which needs a tree; a config with no training split is refused by hermit train and checked by hermit check as
hermit eval runs it; the eval section only where an eval runs; one of Harbor's own acting agents, which takes no
adapter); run_agent and no step of a multi-step training task; a run that is not the agent's result (infra) left out
of every mean and counted, and run again by eval (a step of a multi-step task never alone, a task the acting agent
never runs never, a row with a `steps` field of its own as any row); each repeat's rows, each epoch's and run_agent's
(a new run each); an eval's results file of its own; data.shuffle's order of a multi-step task's rows and of any other row (a task named twice included), and an
Evolver's request of a step ahead of an earlier one, or of one whose earlier step the training split leaves out,
refused, and so (before any run, and by hermit check) a train split taken as given that lists one, a test split an
eval runs that does, any split that leaves out an earlier step of a stream, and a validation split that does either
(but for the train split's steps, which run_agent never runs), hermit.harbor.dataset's splits never; a Harbor agent's
streams stopped with the pipeline of a train that finished, kept by one closed before it finished; the run's secrets
(a Harbor task set's directories, an eval config's tree).
"""
import json
from pathlib import Path
import re

import pytest
import yaml

from hermit.cli import main
from hermit.config import Config
from hermit.evaluate import evaluate
from hermit.settings_check import run_check
from hermit.task import TaskData
from hermit.trainer.trainer import Trainer, train_order

from toy_task import PIPELINE, make_task

QUIET = {"log": lambda *_: None}
# The rows in the split's order, two a step, one epoch (as an ordered stream of a continual-learning sequence runs)
IN_ORDER = ("evolver.name=learn", "data.step_size=2", "trainer.epochs=1", "data.shuffle=false")

EVOLVERS = '''
import json
from hermit.agent.evolver.base import Evolver, StepResult


class Learn(Evolver):
    """Writes the toy rule at every step; runs params.probe through run_agent and keeps what came back
    (state/probes.jsonl); names params.next_at_1 as step 2's tasks."""

    def step(self, ctx):
        (ctx.workspace.adapter / "ADAPTER.md").write_text("Always add the numbers.\\n")
        step = ctx.progress.step
        if ctx.params.get("probe"):
            out = ctx.tools["run_agent"](task_ids=list(ctx.params["probe"]))
            with open(ctx.workspace.state / "probes.jsonl", "a") as f:
                f.write(json.dumps({"step": step, "results": out["results"]}) + "\\n")
        return StepResult("done", f"step {step}", next_tasks=ctx.params.get("next_at_1") if step == 1 else None)


class Choose(Learn):
    chooses_tasks = True
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
def _task(tmp: Path) -> Path:
    """The toy task (t0-t5 train, t6 val, t7-t9 test) and this file's Evolvers."""
    path = make_task(tmp)
    for cls in ("Learn", "Choose"):
        (tmp / cls.lower()).mkdir()
        (tmp / cls.lower() / "evolver.py").write_text(EVOLVERS)
        (tmp / cls.lower() / "evolver.yaml").write_text(f"class: evolver.py:{cls}\nneeds: []\n")
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


def _printed(capsys) -> dict | list:
    """The JSON a command printed after its progress lines."""
    lines = capsys.readouterr().out.splitlines()
    start = next(i for i, line in enumerate(lines) if line in ("{", "["))
    return json.loads("\n".join(lines[start:]))


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


def test_an_adapter_of_the_runs_tree_needs_a_tree_and_trainer_tree_dir_alone_finds_one(tmp_path):
    """root, final, a tag or a commit name an adapter of a training run's tree, which hermit train makes: with no tree
    there, hermit eval refuses it by name before anything runs; an eval config with no template finds a tree that
    exists by trainer.tree_dir alone, and tests its root."""
    path = _task(tmp_path)
    baseline = tmp_path / "baseline.yaml"
    baseline.write_text(yaml.safe_dump({"data": {"tasks": "tasks.jsonl", "test": "test.txt"},
                                        "agent": {"name": "pipeline.py:run"}, "judge": {"path": "judge.py"},
                                        "trainer": {"tree_dir": "tree"}, "sandbox": {"enabled": False}}))
    args = ["eval", "--config", str(baseline), "--adapter", "root"]
    tree = (tmp_path / "tree").resolve()
    with pytest.raises(SystemExit, match=re.escape(f"root names an adapter of a training run's tree, and there is "
                                                   f"none at {tree} (no hermit train ran)")):
        main(args)
    assert not tree.exists() and not (tmp_path / "runs").exists()
    Trainer(load(path, "evolver.name=learn", "trainer.epochs=1", f"trainer.tree_dir={tree}"), **QUIET).train()
    assert main(args) == 0
    assert len(_jsonl(tmp_path / "runs" / "baseline" / "eval" / "root" / "results.jsonl")) == 3


def test_a_config_with_no_training_split_is_refused_by_hermit_train_and_checked_as_hermit_eval_runs_it(tmp_path,
                                                                                                         capsys):
    make_task(tmp_path)
    path = tmp_path / "baseline.yaml"
    path.write_text(yaml.safe_dump({"data": {"tasks": "tasks.jsonl", "test": "test.txt"},
                                    "agent": {"name": "pipeline.py:run"}, "judge": {"path": "judge.py"},
                                    "sandbox": {"enabled": False}}))
    assert main(["train", "--config", str(path)]) == 2
    assert "hermit train: data.train is required" in capsys.readouterr().err
    lines = []
    assert run_check(Config.load(path, command="check"), log=lines.append)
    text = "\n".join(lines)
    assert "[ok] template — none (hermit eval of a directory or none)" in text and "[ok] evolver — not used" in text
    assert "[ok] budgets — hermit eval runs without a budget" in text and "[ok] agent probe — task t7:" in text


def test_the_eval_section_is_checked_only_where_an_eval_runs(tmp_path):
    """A training run never reads eval.*: eval.repeats 0 trains, loaded for hermit train, for hermit check or by the
    Python API (Config.load with no command, then Trainer(config).train()). hermit eval refuses it as the config
    loads, and so does every command for a config with no training split, which is for hermit eval alone. (eval.repeats
    null is its default, 1.)"""
    path = _task(tmp_path)
    for command in ("train", "check"):
        load(path, "evolver.name=learn", "eval.repeats=0", command=command)
    config = Config.load(path, ["evolver.name=learn", "trainer.epochs=1", "eval.repeats=0",
                                "trainer.run_name=repeats-0"])           # the Python API
    assert Trainer(config, **QUIET).train()["steps"] == 2
    for command, args in (("eval", ()), *[(command, ("data.train=null",)) for command in ("eval", "check", None)]):
        with pytest.raises(ValueError, match="eval.repeats must be a whole number of at least 1, not 0"):
            load(path, "evolver.name=learn", *args, "eval.repeats=0", command=command)


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
            Trainer(load(path, *harbor, "evolver.name=learn", command="train"), **QUIET).train()
        lines = []
        assert not run_check(load(path, *harbor, "evolver.name=learn", command="check"), probe=False, log=lines.append)
        assert any(line.startswith(f"    - {refused}") for line in lines[lines.index("[PROBLEM] task"):])
        with pytest.raises(SystemExit, match=re.escape(f"{refused}: evaluate it with --adapter none")):
            evaluate(load(path, *harbor, command="eval"), "root", **QUIET)
    assert not (tmp_path / "runs").exists()
    with pytest.raises(ValueError, match="is not an executable file"):          # none goes on to the pipeline
        evaluate(load(path, *harbor, command="eval"), "none", **QUIET)




# ---------- run_agent ----------


def test_run_agent_runs_no_step_of_a_multi_step_training_task_and_says_why(tmp_path):
    """A training row that is a step of a multi-step task runs in the Trainer's own stream of its task: run by
    run_agent it would find its turn taken (a step the Trainer ran) or take the Trainer's (one it has not reached,
    whose own step would then be lost). It comes back refused, as any row run_agent does not run (the validation
    split naming one of them too), and the tool's description says so; a validation task's step runs as before, and so
    does a training row of a task of another kind with a `steps` field of its own."""
    path = _task(tmp_path)
    _multi_step(tmp_path, "m", "v")
    _rows(tmp_path, t2={"steps": 4})
    (tmp_path / "ms.txt").write_text("t0\nt1\nm:s1\nm:s2\nm:s3\nt2\n")
    probe = ("data.train=ms.txt", "evolver.params.probe=[m:s1, m:s3, t2, v:s1]")
    (tmp_path / "val.txt").write_text("t6\nv:s1\nm:s3\n")
    Trainer(load(path, "evolver.name=learn", "trainer.epochs=1", *probe), **QUIET).train()
    last = {r["id"]: r for r in _jsonl(tmp_path / "runs" / "toy-run" / "workspace" / "state" / "probes.jsonl")[-1]
            ["results"]}
    for step in ("m:s1", "m:s3"):
        assert last[step]["status"] == "error", last[step]
        assert f"{step!r} is a step of a multi-step training task, which runs only in the Trainer's own stream" \
            in last[step]["error"]
    assert last["t2"]["status"] == "ok" and last["v:s1"]["status"] == "ok"
    tool = Trainer(load(path, "evolver.name=learn", *probe), **QUIET)._run_agent_tool()
    assert "on training or validation tasks (not a step of a multi-step training task) through" in tool.description


# ---------- runs that are not the agent's result ----------

def test_a_run_that_is_not_the_agents_result_is_infra_left_out_of_every_mean_and_counted(tmp_path):
    path = _task(tmp_path)
    (tmp_path / "pipeline.py").write_text(INFRA)
    (tmp_path / "judge.py").write_text(STRICT_JUDGE)
    config = load(path, *IN_ORDER)
    Trainer(config, **QUIET).train()
    first = _steps(tmp_path)[0]["rollout"]
    assert first["scores"] == {"t0": 0.0, "t1": None} and first["unscored"] == 1 and first["mean_score"] == 0.0
    material = tmp_path / "runs" / "toy-run" / "learning_material" / "step_0001"
    t1 = json.loads((material / "tasks" / "t1.json").read_text())
    assert t1["status"] == "infra" and t1["error"] == "model_provider_failed"       # not judged: no judge_error
    assert t1["judge"] == {"score": 0.0, "feedback": "not scored: model_provider_failed", "extra": {"unscored": True}}
    assert {k: json.loads((material / "summary.json").read_text())[k] for k in ("mean_score", "unscored")} == \
        {"mean_score": 0.0, "unscored": 1}
    scores = [v for step in _steps(tmp_path) for v in step["rollout"]["scores"].values()]
    assert scores.count(None) == 1 and sum(v for v in scores if v is not None) / 5 == 0.8
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


def test_eval_runs_no_task_again_that_its_acting_agent_never_runs(tmp_path):
    """A task Harbor runs for none of Hermit's agents (`unsupported_task`) would be refused the same way on every
    pass: its infra run is kept, and the log says how many and why, while the other infra runs are run again."""
    path = _task(tmp_path)
    (tmp_path / "pipeline.py").write_text(FLAKY)
    _rows(tmp_path, t7={"flaky": 1}, t8={"flaky": 9, "cause": "unsupported_task"})
    lines = []
    result = evaluate(load(path, "eval.infra_retries=2", "eval.infra_wait_s=0"), "none", log=lines.append)
    attempts = {i: int((tmp_path / ".attempts" / i).read_text()) for i in ("t7", "t8")}
    assert attempts == {"t7": 2, "t8": 1} and result["unscored"] == 1 and result["infra_rerun"] == 1
    assert any("1 infra run(s) are of tasks Harbor runs for none of Hermit's agents" in line for line in lines)


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


def test_each_epoch_hands_the_pipeline_rows_that_name_it_and_run_agents_rows_ask_for_a_new_run(tmp_path):
    """A pipeline that keeps its trials by row (Harbor's trial cache) would hand a later epoch the trials of the epoch
    before wherever the adapter stayed as it was (an Evolver whose every candidate was rejected, a full rollback), a
    learner's run_agent a trial it already had, and a task a split names twice its first run again: with
    trainer.epochs > 1 each epoch's rows name the epoch, and run_agent's rows, and those of a task the epoch's order
    names twice, ask for a new run (`fresh`); one epoch's rows, of a task named once, are as they are."""
    path = _task(tmp_path)
    (tmp_path / "pipeline.py").write_text(PIPELINE.replace(
        "    text = ", "    with open(Path(__file__).parent / 'seen.txt', 'a') as f:\n"
                      "        f.write(f\"{task['id']} {task.get('epoch')} {task.get('fresh')}\\n\")\n    text = ", 1))
    Trainer(load(path, "evolver.name=learn", "evolver.params.probe=[t0, t6]", "data.step_size=6"), **QUIET).train()
    rollouts = [f"t{i} {epoch} None" for i in range(6) for epoch in (1, 2)]
    probes = ["t0 None True", "t6 None True"] * 2                 # one run_agent call a step, a step an epoch
    assert sorted((tmp_path / "seen.txt").read_text().splitlines()) == sorted(rollouts + probes)
    (tmp_path / "seen.txt").unlink()
    (tmp_path / "twice.txt").write_text("t0\nt1\nt0\n")             # a split may name a task twice
    Trainer(load(path, "evolver.name=learn", "trainer.epochs=1", "data.train=twice.txt", "data.step_size=1",
                 "trainer.run_name=once"), **QUIET).train()
    assert sorted((tmp_path / "seen.txt").read_text().splitlines()) == ["t0 None True", "t0 None True",
                                                                        "t1 None None"]


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




# ---------- the order a multi-step task's steps run in ----------

def test_data_shuffle_moves_a_multi_step_tasks_rows_as_one_in_step_order_and_any_other_row_as_before(tmp_path):
    """A step taken ahead of an earlier step of its task waits for it in the task's stream (for hours), then ends
    infra: under data.shuffle (the default) each epoch takes a multi-step task's rows together and in step order (the
    split lists them out of it: the Trainer's one epoch of them, as such a split trains, and the order of another);
    a train split without one (a task's own `steps` and `step_index` fields make no step of one) is shuffled as it
    always was, row by row, each copy of a task it names twice a row of its own."""
    path = _task(tmp_path)
    _multi_step(tmp_path, "m", "n")
    rows = ["t0", "m:s3", "m:s1", "t1", "n:s1", "m:s2", "n:s2", "t2", "n:s3"]
    (tmp_path / "ms.txt").write_text("\n".join(rows) + "\n")
    config = load(path, "evolver.name=learn", "data.train=ms.txt", "trainer.epochs=1")
    Trainer(config, **QUIET).train()
    data = TaskData(tmp_path / "tasks.jsonl", tmp_path / "ms.txt", None, None)
    trained = [t for s in _steps(tmp_path) for t in s["tasks"]]
    assert trained == train_order(config, data, 1)
    for epoch, taken in ((1, trained), (2, train_order(config, data, 2))):
        assert sorted(taken) == sorted(rows), (epoch, taken)
        for task in "mn":
            first = taken.index(f"{task}:s1")
            assert taken[first:first + 3] == [f"{task}:s1", f"{task}:s2", f"{task}:s3"], (epoch, taken)
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


def test_an_evolvers_request_of_a_step_whose_earlier_step_the_training_split_leaves_out_is_refused(tmp_path):
    """An earlier step of a stream the training split does not hold never runs (hermit train refuses such a split
    before it trains), and a step named without it would wait for it in the task's stream (for hours), then end
    infra: the request is refused, during an epoch and at its end, as one naming a step ahead of an earlier one still
    to run is, saying so; the split's other tasks are taken."""
    from hermit.agent.evolver.base import Progress, StepResult
    path = _task(tmp_path)
    _multi_step(tmp_path, "m")
    (tmp_path / "gap.txt").write_text("t0\nm:s2\nt1\nm:s3\n")
    trainer = Trainer(load(path, "evolver.name=choose", "data.train=gap.txt", "data.step_size=2",
                           "data.shuffle=false", "trainer.epochs=1"), **QUIET)

    def answer(remaining, wanted):
        progress = Progress(1, 1, not remaining, remaining=tuple(remaining))
        return trainer._request(progress, StepResult("done", "chose", next_tasks=wanted))
    never = " (not in the training split: it never runs)"
    for remaining in (["m:s2", "t1", "m:s3"], []):                 # t0 ran; at the epoch's end, for the next
        assert answer(remaining, ["m:s2", "m:s3"])["next_tasks_refused"] == (
            "['m:s2', 'm:s3']: a step of a multi-step task ahead of an earlier one still to run (a task's steps run in "
            f"order, in its stream: name the earlier one first): m:s2 ahead of m:s1{never}, m:s3 ahead of m:s1{never}")
        assert answer(remaining, ["t1"]) == {"next_tasks": ["t1"]}


AHEAD = ("lists a step of a multi-step task ahead of an earlier step of its stream, which it would wait for (up to "
         "hours, then ending infra): m:s2 ahead of m:s1 (task m); a task's steps run in order, each after the one "
         "before it: list them in step order")


def test_a_train_split_taken_as_given_that_lists_a_step_ahead_of_an_earlier_one_is_refused_before_training(tmp_path):
    """A step taken ahead of an earlier step of its task waits for it in the task's stream (for hours), then ends
    infra, and so does each step after it: a train split the Trainer takes as it is given (data.shuffle false, as an
    ordered stream runs) that lists one is refused before anything runs, naming the task and the steps, and hermit
    check reports it; the same rows in step order train."""
    path = _task(tmp_path)
    _multi_step(tmp_path, "m")
    (tmp_path / "ahead.txt").write_text("t0\nm:s2\nt1\nm:s1\nm:s3\n")
    (tmp_path / "inorder.txt").write_text("t0\nm:s1\nt1\nm:s2\nm:s3\n")
    for args in ((*IN_ORDER, "data.train=ahead.txt"), ("evolver.name=learn", "data.shuffle=false", "data.train=ahead.txt")):
        with pytest.raises(SystemExit, match=re.escape(f"train refuses to start: the train split {AHEAD}")):
            Trainer(load(path, *args), **QUIET).train()
        lines = []
        assert not run_check(load(path, *args, command="check"), probe=False, log=lines.append)
        assert f"    - the train split {AHEAD}" in lines, args
    assert not (tmp_path / "runs").exists()
    assert run_check(load(path, *IN_ORDER, "data.train=inorder.txt", command="check"), probe=False,
                     log=lambda *_: None)
    Trainer(load(path, *IN_ORDER, "data.train=inorder.txt"), **QUIET).train()
    assert [s["tasks"] for s in _steps(tmp_path)] == [["t0", "m:s1"], ["t1", "m:s2"], ["m:s3"]]


def test_a_train_split_with_steps_of_a_multi_step_task_trains_one_epoch_and_more_are_refused(tmp_path):
    """A run runs each step of a task's stream once, in one environment (the stream is the run's): a second epoch's
    steps would find their turns taken and end infra, every one left out of the means. So hermit train refuses a train
    split with such steps and trainer.epochs above 1 before anything runs, and hermit check reports it; one epoch
    trains, and a train split without them trains its epochs as before."""
    path = _task(tmp_path)
    _multi_step(tmp_path, "m")
    (tmp_path / "steps.txt").write_text("t0\nm:s1\nm:s2\nm:s3\n")
    args = ("evolver.name=learn", "data.step_size=2", "data.shuffle=false", "data.train=steps.txt")
    refused = "and trainer.epochs is 2: a run runs each step of a task's stream once, in one environment"
    with pytest.raises(SystemExit, match=re.escape(refused)):
        Trainer(load(path, *args, "trainer.epochs=2"), **QUIET).train()
    assert not (tmp_path / "runs").exists()
    lines = []
    assert not run_check(load(path, *args, "trainer.epochs=2", command="check"), probe=False, log=lines.append)
    assert any(refused in line for line in lines)
    assert run_check(load(path, "evolver.name=learn", "trainer.epochs=2", command="check"), probe=False,
                     log=lambda *_: None)
    Trainer(load(path, *args, "trainer.epochs=1"), **QUIET).train()
    assert [s["tasks"] for s in _steps(tmp_path)] == [["t0", "m:s1"], ["m:s2", "m:s3"]]


def test_the_splits_dataset_prepare_writes_list_each_streams_steps_in_step_order_and_none_is_refused(tmp_path):
    """hermit.harbor.dataset prepare writes a multi-step task's rows rollout by rollout, each rollout's steps in step
    order, into every split and its smoke rows: none is refused (hermit check of each as a train split taken in its
    order, and as an eval's test split); the same rows with one rollout's steps swapped are."""
    from hermit.harbor import dataset
    from hermit.task import read_ids
    from hermit.trainer.trainer import out_of_step
    path = _task(tmp_path)

    def download(ref, output):                     # Harbor's export of the set: <output>/<name>/<task>/
        tasks = Path(output) / "demo"
        for name, steps in (("alpha", ()), ("beta", ()), ("flow", ("s1", "s2", "s3")), ("gamma", ())):
            (tasks / name / "tests").mkdir(parents=True)
            (tasks / name / "task.toml").write_text('schema_version = "1.4"\n[agent]\ntimeout_sec = 60.0\n'
                                                    + "".join(f'\n[[steps]]\nname = "{s}"\n' for s in steps))
            (tasks / name / "instruction.md").write_text(f"Do {name}.\n")
            for step in steps:
                (tasks / name / "steps" / step).mkdir(parents=True)
                (tasks / name / "steps" / step / "instruction.md").write_text(f"Step {step} of {name}.\n")
        return tasks
    (tmp_path / "utils").mkdir()
    (tmp_path / "utils" / "benchmark.yaml").write_text(yaml.safe_dump({"tasks_dir": "tasks", "dataset": {
        "ref": "demo@1.0", "data": str(tmp_path / "data"), "rollouts": 2, "smoke": ["flow"],
        "split": {"train": 2, "test": "rest", "seed": 0}}}))
    dataset.prepare(tmp_path / "utils" / "benchmark.yaml", download_fn=download, log=lambda *_: None)
    tasks = TaskData(tmp_path / "utils" / "tasks.jsonl", None, None, None).tasks
    splits = {name: read_ids(tmp_path / "utils" / "splits" / f"{name}.txt") for name in dataset.SPLITS}
    flow = [i for i in splits["all"] if i.startswith("flow:")]
    assert flow == ["flow:s1#1", "flow:s2#1", "flow:s3#1", "flow:s1#2", "flow:s2#2", "flow:s3#2"]
    assert {name: out_of_step(ids, tasks, name) for name, ids in splits.items()} == dict.fromkeys(splits)
    swapped = [*flow[:3], flow[4], flow[3], flow[5]]
    assert "flow:s2#2 ahead of flow:s1#2 (task flow)" in out_of_step(swapped, tasks, "it")
    # each rollout is a stream of its own: rollout 2's steps alone are one whole; without its first step, not
    assert out_of_step(flow[3:], tasks, "it") is None
    assert "flow:s2#2 ahead of flow:s1#2, which it leaves out (task flow)" in out_of_step(flow[:3] + flow[4:], tasks,
                                                                                         "it")
    harbor = ("data.tasks=utils/tasks.jsonl", "data.val=null")
    trains = [(*IN_ORDER, f"data.train=utils/splits/{n}.txt", "data.test=null") for n in ("all", "train", "test", "smoke")]
    for args in (*trains, ("data.train=null", "data.test=utils/splits/all.txt")):
        lines = []
        assert run_check(load(path, *harbor, *args, command="check"), probe=False, log=lines.append), (args, lines)




def test_a_train_that_finishes_stops_its_harbor_streams_with_its_pipeline_and_one_stopped_midway_keeps_them(
        tmp_path, monkeypatch):
    """The Trainer's pipeline keeps a Harbor task set's streams for a resume (hermit.isolate.agent_pipeline gives its
    HarborAgent keep_streams); a run that finished (summary.json: it never resumes) closes its pipeline with them
    stopped (HarborAgent.close), as any pipeline that never resumes does, and a run whose pipeline is closed before it
    finished, which resumes, keeps them. The stand-in has a HarborAgent's shape: the pipeline is its bound run, and
    keep_streams is HarborAgent's own, read from its params."""
    import hermit.trainer.trainer
    from hermit.harbor.runner import HarborAgent
    path = _task(tmp_path)
    kept = []                                        # keep_streams as each pipeline of the Trainer's was closed

    class Streams:
        keep_streams = HarborAgent.keep_streams

        def __init__(self, run):
            self.params, self.inner = {"keep_streams": True}, run

        def run(self, task, adapter):
            return self.inner(task, adapter)
    built = hermit.trainer.trainer.agent_pipeline

    def pipeline(config, role, **kwargs):
        run, close = built(config, role, **kwargs)
        agent = Streams(run)

        def close_agent():
            kept.append(agent.keep_streams)
            close()
        return agent.run, close_agent
    monkeypatch.setattr(hermit.trainer.trainer, "agent_pipeline", pipeline)
    trainer = Trainer(load(path, *IN_ORDER), **QUIET)
    trainer._prepare()                               # its pipeline, then closed before the run finished
    trainer._close_sandboxes()
    assert kept == [True]                            # not finished: its resume meets its streams again
    summary = Trainer(load(path, *IN_ORDER), **QUIET).train()
    assert summary["steps"] == 3 and kept == [True, False]




def test_a_test_split_that_lists_a_step_ahead_of_an_earlier_one_is_refused_before_any_eval_runs_it(tmp_path):
    """hermit eval runs a split's rows in its order (one after the other with agent.concurrency 1): a step ahead of an
    earlier step of its stream would wait for it, then end infra. data.test is refused before any run, naming the
    task and the steps, and hermit check of an eval config reports it. The same rows in step order are evaluated."""
    path = _task(tmp_path)
    _multi_step(tmp_path, "m")
    (tmp_path / "ahead.txt").write_text("t7\nm:s2\nt8\nm:s1\nm:s3\n")
    (tmp_path / "inorder.txt").write_text("t7\nm:s1\nt8\nm:s2\nm:s3\n")
    ahead = (tmp_path / "ahead.txt").resolve()
    with pytest.raises(SystemExit, match=re.escape(f"eval refuses to start: the split {ahead} {AHEAD}")):
        evaluate(load(path, "data.test=ahead.txt", command="eval"), "none", **QUIET)
    assert not (tmp_path / "runs").exists()
    lines = []
    assert not run_check(load(path, "data.train=null", "data.test=ahead.txt", command="check"), probe=False,
                         log=lines.append)
    assert f"    - the test split {AHEAD}" in lines
    result = evaluate(load(path, "data.test=inorder.txt"), "none", **QUIET)
    assert result["tasks"] == 5 and result["scored"] == 5


def test_hermit_check_of_a_training_config_reports_a_test_split_that_hermit_eval_would_refuse(tmp_path):
    """hermit check of a training config checks its test split's step order too: hermit eval of the same config
    refuses a test split that lists a step ahead of an earlier step of its stream."""
    path = _task(tmp_path)
    _multi_step(tmp_path, "m")
    (tmp_path / "ahead.txt").write_text("t7\nm:s2\nt8\nm:s1\nm:s3\n")
    lines = []
    assert not run_check(load(path, *IN_ORDER, "data.test=ahead.txt", command="check"), probe=False,
                         log=lines.append)
    assert f"    - the test split {AHEAD}" in lines


def test_hermit_checks_probe_runs_no_later_step_of_a_multi_step_task(tmp_path):
    """The probe runs one row alone, and a later step of a multi-step task alone would wait for the steps before it
    (for hours, then end infra): it runs the split's first row that is no such step. data.shuffle may list m:s2 ahead
    of m:s1 (the Trainer runs a task's steps in step order)."""
    path = _task(tmp_path)
    _multi_step(tmp_path, "m")
    (tmp_path / "late.txt").write_text("m:s2\nm:s1\nm:s3\nt0\n")
    lines = []
    run_check(load(path, "evolver.name=learn", "data.train=late.txt", command="check"), probe=True, log=lines.append)
    assert any("agent probe" in line and "task m:s1:" in line for line in lines), lines


def _leaves_out(*pairs: tuple[str, str]) -> str:
    """AHEAD for a list that names each step of `pairs` (the step, the earlier step) of task m but not the earlier."""
    return AHEAD.replace("m:s2 ahead of m:s1 (task m)",
                         ", ".join(f"{t} ahead of {e}, which it leaves out (task m)" for t, e in pairs))


def test_a_split_that_leaves_out_an_earlier_step_of_a_stream_is_refused_before_any_eval_or_train(tmp_path):
    """A step whose stream's earlier step never runs waits for it (for hours: the stream's driver starts at its first
    step), then ends infra, as one listed ahead of it does: a split that lists m:s2 and m:s3 without m:s1, or m:s1
    and m:s3 without m:s2, is refused as hermit eval starts, naming the step it leaves out, and hermit check reports it
    (an eval config's test split, a train split); hermit train refuses such a train split before it trains (taken as
    given, and as data.shuffle takes it, which moves a task's rows together but adds none the split leaves out). A
    split of a stream's first steps alone is evaluated."""
    path = _task(tmp_path)
    _multi_step(tmp_path, "m")
    (tmp_path / "no_s1.txt").write_text("t7\nm:s2\nt8\nm:s3\n")
    (tmp_path / "no_s2.txt").write_text("t7\nm:s1\nt8\nm:s3\n")
    (tmp_path / "train_no_s1.txt").write_text("t0\nm:s2\nt1\nm:s3\n")
    (tmp_path / "first.txt").write_text("t7\nm:s1\nt8\nm:s2\n")
    no_s1, no_s2 = (tmp_path / "no_s1.txt").resolve(), (tmp_path / "no_s2.txt").resolve()
    gap = _leaves_out(("m:s2", "m:s1"), ("m:s3", "m:s1"))
    for split, named, problem in (("no_s1.txt", no_s1, gap), ("no_s2.txt", no_s2, _leaves_out(("m:s3", "m:s2")))):
        with pytest.raises(SystemExit, match=re.escape(f"eval refuses to start: the split {named} {problem}")):
            evaluate(load(path, f"data.test={split}", command="eval"), "none", **QUIET)
    for args, reported in ((("data.train=null", "data.test=no_s1.txt"), f"the test split {gap}"),
                           ((*IN_ORDER, "data.train=train_no_s1.txt"), f"the train split {gap}")):
        lines = []
        assert not run_check(load(path, *args, command="check"), probe=False, log=lines.append)
        assert f"    - {reported}" in lines, (args, lines)
    for args in ((*IN_ORDER, "data.train=train_no_s1.txt"), ("evolver.name=learn", "data.train=train_no_s1.txt")):
        with pytest.raises(SystemExit, match=re.escape(f"train refuses to start: the train split {gap}")):
            Trainer(load(path, *args), **QUIET).train()
    assert not (tmp_path / "runs").exists()
    result = evaluate(load(path, "data.test=first.txt"), "none", **QUIET)
    assert result["tasks"] == 4 and result["scored"] == 4


def test_a_validation_split_that_lists_a_step_ahead_of_an_earlier_one_or_without_it_is_refused_before_training(
        tmp_path):
    """GEPA, SkillOpt, ACE, OPRO and EvoSafe give run_agent the validation split's rows in its order, in one call: a
    step it lists ahead of an earlier step of its stream, or without it, waits for it there (for hours), then ends
    infra. hermit train refuses such a validation split before it trains, the train split shuffled or not, and hermit
    check reports it; neither checks the validation rows run_agent never runs (the train split's steps), nor an eval
    config's validation split, which nothing runs."""
    path = _task(tmp_path)
    _multi_step(tmp_path, "m")
    (tmp_path / "ahead.txt").write_text("t6\nm:s2\nm:s1\nm:s3\n")
    (tmp_path / "gap.txt").write_text("t6\nm:s2\nm:s3\n")
    gap = _leaves_out(("m:s2", "m:s1"), ("m:s3", "m:s1"))
    for args, problem in ((("evolver.name=learn", "data.val=ahead.txt"), AHEAD),
                          (("evolver.name=learn", "data.val=gap.txt"), gap), ((*IN_ORDER, "data.val=gap.txt"), gap)):
        with pytest.raises(SystemExit, match=re.escape(f"train refuses to start: the validation split {problem}")):
            Trainer(load(path, *args), **QUIET).train()
        lines = []
        assert not run_check(load(path, *args, command="check"), probe=False, log=lines.append)
        assert f"    - the validation split {problem}" in lines, (args, lines)
    assert not (tmp_path / "runs").exists()
    (tmp_path / "ms.txt").write_text("t0\nt1\nm:s1\nm:s2\nm:s3\nt2\n")
    (tmp_path / "trained.txt").write_text("t6\nm:s3\n")       # a step of a training task: run_agent never runs it
    lines = []
    run_check(load(path, "evolver.name=learn", "data.train=ms.txt", "data.val=trained.txt", command="check"),
              probe=False, log=lines.append)
    assert not any(line.startswith("    - the validation split") for line in lines), lines
    lines = []
    assert run_check(load(path, "data.train=null", "data.val=ahead.txt", command="check"), probe=False,
                     log=lines.append), lines


# ---------- run secrets ----------

def test_a_harbor_task_sets_tasks_and_jobs_directories_are_run_secrets(tmp_path):
    """A Harbor task set's tasks hold their tests and solutions, and its jobs every trial's raw verifier output: no
    path a sandbox or the learner may show."""
    from hermit.isolate import run_secrets
    _task(tmp_path)
    (tmp_path / "utils").mkdir()
    (tmp_path / "utils" / "benchmark.yaml").write_text(f"tasks_dir: tasks\njobs_dir: {tmp_path / 'jobs'}\n")
    (tmp_path / "baseline.yaml").write_text(yaml.safe_dump({
        "data": {"tasks": "tasks.jsonl", "test": "test.txt"}, "judge": {"path": "hermit.harbor.judge"},
        "agent": {"name": "harbor", "params": {"benchmark": "utils/benchmark.yaml", "web_search": False,
                                               "acting_agent": {"name": "oracle"}}},
        "sandbox": {"enabled": False}}))
    secrets = set(run_secrets(Config.load(tmp_path / "baseline.yaml", command="eval")))
    assert {(tmp_path / "utils" / "tasks").resolve(), (tmp_path / "jobs").resolve()} <= secrets


def test_an_eval_configs_tree_is_a_run_secret_with_no_template_to_find_it_by(tmp_path):
    """An eval config that tests another run's tree names it (trainer.tree_dir) and no template: the tree, which
    holds every step's adapter and the run's records, is still no path a sandbox or the learner may show."""
    from hermit.isolate import run_secrets
    path = _task(tmp_path)
    Trainer(load(path, *IN_ORDER), **QUIET).train()
    tree = tmp_path / "runs" / "trees" / "prompt-only"
    eval_only = ("template=null", f"trainer.tree_dir={tree}", "data.train=null")
    assert tree.resolve() in set(run_secrets(load(path, *eval_only, command="eval")))
    with pytest.raises(ValueError, match="overlaps .*prompt-only"):
        load(path, *eval_only, f"environment.read=[{tree / '.hermit'}]", command="eval")
