"""OfficeQA's task directories (utils/benchmark/prepare.py) read back as the rows its agents always read: a question
written as a task directory and loaded by hermit.task gives exactly its row again (agent/officeqa_fields.py)."""
import importlib.util
from pathlib import Path

from hermit.common import load_callable
from hermit.task import Benchmark

UTILS = Path(__file__).resolve().parents[1]


def _prepare():
    spec = importlib.util.spec_from_file_location("officeqa_prepare", UTILS / "benchmark" / "prepare.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)                    # its work is in main(): importing it writes nothing
    return module


def test_a_question_round_trips_through_its_task_directory(tmp_path):
    officeqa_task = load_callable(str(UTILS / "agent" / "officeqa_fields.py") + ":officeqa_task", UTILS)
    rows = [{"id": "UID0001", "question": 'What was the "total"\r\nin 1953? Use C:\\path and \u2019quotes\u2019.',
             "category": "Hard", "source_files": ["treasury_bulletin_1953_01.txt"],
             "source_docs": ["https://example.org/a?b=1&c=\"d\""]},
            {"id": "UID0002", "question": "Plain?", "category": "Easy", "source_files": [], "source_docs": []}]
    prepare = _prepare()
    for row in rows:
        prepare.write_task(tmp_path / "tasks", row)
    (tmp_path / "train.txt").write_text("UID0001\nUID0002\n")
    bench = Benchmark.load(tmp_path / "tasks", tmp_path / "train.txt")
    assert [officeqa_task(t.given()) for t in bench.split("train")] == rows
    assert list(officeqa_task(bench.tasks["UID0001"].given())) == list(rows[0])     # the same keys, in order
    assert officeqa_task(rows[1]) == rows[1]                                         # a row as it is
    assert bench.tasks["UID0001"].private["tests"] is None                           # judged on the host
    assert bench.tasks["UID0001"].given()["network"]["agent"] == "no-network"        # no network but its model
