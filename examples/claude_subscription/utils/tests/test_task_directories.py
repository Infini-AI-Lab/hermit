"""The sentiment tasks as task directories (utils/benchmark/tasks/) read back, through each config's data.view, as
exactly the rows {id, prompt} they were in tasks.jsonl, in the same splits: the prompts and the records stay
byte for byte what they were."""
import json
from pathlib import Path

import pytest

from hermit.config import Config
from hermit.task import Benchmark

HERE = Path(__file__).resolve().parents[2]
PREFIX = "Classify the sentiment of this review: "
REVIEWS = {
    "train-001": "The service was wonderful and I loved the meal.",
    "train-002": "The product broke immediately and support was awful.",
    "train-003": "The package contains three cables.",
    "val-001": "The staff were friendly and the room was spotless.",
    "val-002": "The delivery was late and the box arrived crushed.",
    "val-003": "The manual is printed in English and French.",
    "test-001": "This was a delightful experience.",
    "test-002": "I regret buying this disappointing device.",
    "test-003": "The shop opens at nine.",
}
ROWS = [{"id": tid, "prompt": PREFIX + review} for tid, review in REVIEWS.items()]      # tasks.jsonl, line by line


@pytest.mark.parametrize("config", ["run.yaml", "run_react.yaml", "run_workspace_learner.yaml"])
def test_each_config_reads_the_rows_tasks_jsonl_held(config):
    bench = Benchmark.from_config(Config.load(HERE / config))
    by_id = sorted(ROWS, key=lambda r: r["id"])                             # the same keys, in order, and types
    assert [json.dumps(t.given()) for t in bench.tasks.values()] == [json.dumps(r) for r in by_id]
    for name in ("train", "val", "test"):
        assert [t.given() for t in bench.split(name)] == [r for r in ROWS if r["id"].startswith(name)]
    task = bench.tasks["test-003"]
    assert task.private["tests"] is None                                    # judged on the host (judge.py)
    assert task.private["needs"]["network"]["agent"] == "no-network"        # what every config gives
