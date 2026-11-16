"""Infrastructure interruptions remain resumable, never ordinary zero-score tasks."""

import pytest
from hermit.task import RunOutput, RunUnavailable
from hermit.trainer.rollout import run_one, run_tasks


def unavailable(*args):
    raise RunUnavailable("provider unavailable; artifacts preserved")


def test_pipeline_infrastructure_error_is_not_scored():
    with pytest.raises(RunUnavailable):
        run_one(
            {"id": "x"}, None, unavailable, lambda *_: pytest.fail("must not grade")
        )


def test_judge_infrastructure_error_is_not_scored():
    with pytest.raises(RunUnavailable):
        run_one({"id": "x"}, None, lambda *_: RunOutput(output="answer"), unavailable)


def test_pending_tasks_stop_after_infrastructure_error():
    seen = []

    def pipeline(task, adapter):
        seen.append(task["id"])
        raise RunUnavailable("offline")

    with pytest.raises(RunUnavailable):
        run_tasks(
            [{"id": str(i)} for i in range(8)],
            None,
            pipeline,
            lambda *_: 0,
            concurrency=1,
        )
    assert seen == ["0"]


def test_evolver_unavailable_leaves_training_step_unfinished(tmp_path):
    from hermit.config import Config
    from hermit.trainer.trainer import Trainer
    from toy_task import make_task

    config = make_task(tmp_path)
    (tmp_path / "toy/evolver.py").write_text("""
from hermit.agent.evolver.base import Evolver
from hermit.task import RunUnavailable
class Toy(Evolver):
    def step(self, ctx):
        raise RunUnavailable("delivery unavailable")
""")
    with pytest.raises(RunUnavailable, match="delivery unavailable"):
        Trainer(Config.load(config), log=lambda *_: None).train()
    records = tmp_path / "runs/toy-run/steps.jsonl"
    assert not records.exists() or not records.read_text().strip()
    assert not (tmp_path / "runs/toy-run/summary.json").exists()
