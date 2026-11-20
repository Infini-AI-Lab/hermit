"""Both CL-Bench venues' task directories (utils/benchmark/prepare.py's write_task) read back, through every config's
data.view, as exactly the rows tasks.jsonl held: the host, its records and the learning material are unchanged. Each
row stays one task; the stream's state is the host's, in the agent's own loop."""
import importlib.util
import json
from pathlib import Path

import pytest

from hermit.config import Config
from hermit.task import Benchmark

EXAMPLES = Path(__file__).resolve().parents[3]
ROWS = {"clbench_database": [{"id": "o0_q001", "order": 0, "position": 0, "question_id": "1", "block": 1},
                             {"id": "o0_q002", "order": 0, "position": 1, "question_id": "5", "block": 2}],
        "clbench_poker": [{"id": "o0_h001", "order": 0, "hand_num": 1, "block": 1},
                          {"id": "o0_h003", "order": 0, "hand_num": 3, "block": 1}]}


def _prepare(venue):
    spec = importlib.util.spec_from_file_location(f"{venue}_prepare", EXAMPLES / venue / "utils/benchmark/prepare.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)                    # its work is in main(): importing it writes nothing
    return module


@pytest.mark.parametrize("venue", sorted(ROWS))
def test_a_venues_tasks_read_back_as_their_rows_in_every_config(venue, tmp_path, monkeypatch):
    prepare = _prepare(venue)
    for row in ROWS[venue]:
        prepare.write_task(tmp_path / "tasks", row)
    (tmp_path / "order0.txt").write_text("".join(r["id"] + "\n" for r in ROWS[venue]))
    configs = sorted((EXAMPLES / venue).glob("*/run.yaml"))
    assert len(configs) == 3
    for path in configs:
        config = Config.load(path)
        data = config.data["data"]
        assert config.path(data["tasks"]) == (EXAMPLES / venue / "utils/benchmark/tasks").resolve(), path
        monkeypatch.setitem(data, "tasks", str(tmp_path / "tasks"))
        monkeypatch.setitem(data, "train", str(tmp_path / "order0.txt"))
        bench = Benchmark.from_config(config)
        assert [json.dumps(t.given()) for t in bench.split("train")] == [json.dumps(r) for r in ROWS[venue]], path
        task = bench.split("train")[0]
        assert task.private["tests"] is None and task.private["needs"]["network"]["agent"] == "no-network"
