"""Tasks (hermit.task): the public half an agent is handed, the private half only the judge reads, the task set and
its splits, and the run that brings a task and an agent together (hermit.trainer.rollout)."""
import json
import warnings

import pytest

from hermit.common import RunOutput
from hermit.task import Benchmark, JudgeResult, Task, as_task, judge_verifier
from hermit.trainer.rollout import run_one, run_tasks


def test_a_task_keeps_the_judges_own_out_of_what_the_agent_is_handed():
    task = Task.from_row({"id": "q1", "question": "2+2?", "_private": {"answer": "4"}})
    assert task.id == "q1" and task.private == {"answer": "4"}
    given = task.given()
    assert given == {"id": "q1", "question": "2+2?"} and "_private" not in given
    given["question"] = "changed"                                  # a copy: the task is not changed through it
    assert task.public["question"] == "2+2?"
    with pytest.raises(TypeError):
        task.public["question"] = "x"                              # read-only
    with pytest.raises(ValueError, match="judge's own"):
        Task(id="q2", public={"_private": {}})
    assert Task.from_row({"task_id": 7}).id == "7" and as_task(task) is task
    with pytest.raises(ValueError, match="needs an id"):
        Task.from_row({"question": "no id"})


def test_a_benchmark_reads_its_task_set_and_splits_in_order(tmp_path):
    rows = [{"id": f"t{i}", "q": i} for i in range(4)]
    (tmp_path / "tasks.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    (tmp_path / "train.txt").write_text("t2\nt0\n")
    (tmp_path / "test.txt").write_text("# a comment\nt3\n")
    bench = Benchmark.load(tmp_path / "tasks.jsonl", tmp_path / "train.txt", None, tmp_path / "test.txt")
    assert [t.id for t in bench.split("train")] == ["t2", "t0"] and [t.id for t in bench.split("test")] == ["t3"]
    assert bench.split("val") == [] and bench.tasks["t1"].given() == {"id": "t1", "q": 1}


def test_the_run_hands_the_agent_the_public_half_and_the_verifier_the_whole_task():
    seen = {}

    def agent(given, adapter):
        seen["given"] = given
        return RunOutput(output="4", cost_usd=0.01, tokens=10)

    def verify(task, output):
        seen["task"] = task
        return JudgeResult(float(output.output == task.private["answer"]), "checked")
    verify.takes_task = True
    record = run_one(Task.from_row({"id": "q1", "question": "2+2?", "_private": {"answer": "4"}}), None, agent, verify)
    assert seen["given"] == {"id": "q1", "question": "2+2?"} and seen["task"].private == {"answer": "4"}
    assert record["status"] == "ok" and record["judge"] == {"score": 1.0, "feedback": "checked", "extra": {}}
    assert record["input"] == {"id": "q1", "question": "2+2?"}         # the record shows what the agent was given


def test_a_bare_judge_and_a_row_still_run_as_before():
    """Judges written as `score(task_row, output)` and callers that pass rows keep their records unchanged."""
    def agent(given, adapter):
        return RunOutput(output=given["q"] * 2)

    def score(row, output):
        return {"score": float(output.output == 4), "feedback": "ok", "seen": sorted(row)}
    records = run_tasks([{"id": "a", "q": 2}, Task.from_row({"id": "b", "q": 3})], None, agent, score, concurrency=2)
    assert [r["id"] for r in records] == ["a", "b"]
    assert records[0]["judge"] == {"score": 1.0, "feedback": "ok", "extra": {"seen": ["id", "q"]}}
    assert records[1]["judge"]["score"] == 0.0 and records[0]["input"] == {"id": "a", "q": 2}
    wrapped = judge_verifier(score)
    assert wrapped(as_task({"id": "c", "q": 2}), RunOutput(output=4)).score == 1.0


def test_the_environments_moved_to_the_top_level_and_the_old_path_forwards():
    import importlib
    import hermit.agent.environment as forwarded
    from hermit.environment import Local
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        importlib.reload(forwarded)                               # the warning comes with the import, once
        from hermit.agent.environment import Local as old
        from hermit.agent.environment.base import Sandboxed as old_sandboxed
    from hermit.environment.base import Sandboxed
    assert old is Local and old_sandboxed is Sandboxed
    assert any("hermit.environment" in str(w.message) for w in caught)


def test_the_names_imported_from_hermit_task_before_the_package_still_import():
    from hermit.task import (JudgeResult, RunOutput, TaskData, load_callable, load_pipeline,  # noqa: F401
                             normalise_judge, output_record, read_ids)
    from hermit import common
    assert RunOutput is common.RunOutput and load_callable is common.load_callable


def test_a_bare_judge_still_reads_the_private_half_and_records_keep_the_rows_id():
    """As before tasks were typed: a judge `score(row, output)` is called with the whole row, `_private` included (the
    agent is not); a numeric id stays a number in the record; `judge=` is still the keyword."""
    seen = {}

    def agent(given, adapter):
        seen["agent"] = given
        return RunOutput(output="4")

    def score(row, output):
        return float(output.output == row["_private"]["answer"])
    row = {"id": 7, "question": "2+2?", "_private": {"answer": "4"}}
    record = run_one(row, None, agent, judge=score)
    assert record["judge"]["score"] == 1.0 and "_private" not in seen["agent"]
    assert record["id"] == 7 and json.loads(json.dumps(record))["id"] == 7
    assert run_tasks([row], None, agent, judge=score, concurrency=1)[0]["judge"]["score"] == 1.0
