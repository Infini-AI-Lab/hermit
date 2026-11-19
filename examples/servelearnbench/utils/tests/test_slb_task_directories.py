"""ServeLearnBench's task directories (utils/benchmark/prepare.py's write_task) read back, through both configs'
data.view, as exactly the rows tasks.jsonl held: the stamped instruction the benchmark's loop is given, the records and
the learning material are unchanged. Each episode stays one task; what carries across the stream is the adapter."""
import importlib.util
import json
from pathlib import Path

from hermit.config import Config
from hermit.task import Benchmark

EXAMPLE = Path(__file__).resolve().parents[2]
ROWS = [{"id": "l3a_t0001_p_amex_ret", "scenario": "retail_l3", "phase": "serving", "timestamp": 1, "block": 1,
         "instruction": 'Current timestamp: 1\n\nCustomer: Aisha (zip 37759) writes:\n"I want to send back the '
                        'Cookware Set — no longer needed. \\ \\"quoted\\""'},
        {"id": "l3a_test_w0_007", "scenario": "retail_l3", "phase": "test", "timestamp": 77,
         "instruction": "Current timestamp: 77\n\nCustomer writes:\n\tTab and\r\nCRLF kept."}]


def _module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)                    # its work is in main(): importing it writes nothing
    return module


def test_an_episode_reads_back_as_its_row_in_every_config(tmp_path, monkeypatch):
    prepare = _module("slb_prepare", EXAMPLE / "utils/benchmark/prepare.py")
    for row in ROWS:
        prepare.write_task(tmp_path / "tasks", row)
    (tmp_path / "serving.txt").write_text(ROWS[0]["id"] + "\n")
    (tmp_path / "test_w0.txt").write_text(ROWS[1]["id"] + "\n")
    for path in sorted(EXAMPLE.glob("*/retail_l3.yaml")):
        config = Config.load(path)
        data = config.data["data"]
        assert config.path(data["tasks"]) == (EXAMPLE / "utils/benchmark/tasks").resolve(), path
        monkeypatch.setitem(data, "tasks", str(tmp_path / "tasks"))
        monkeypatch.setitem(data, "train", str(tmp_path / "serving.txt"))
        monkeypatch.setitem(data, "test", str(tmp_path / "test_w0.txt"))
        bench = Benchmark.from_config(config)
        given = [t.given() for t in bench.split("train") + bench.split("test")]
        assert [json.dumps(g) for g in given] == [json.dumps(r) for r in ROWS], path
        assert bench.split("train")[0].private["needs"]["network"]["agent"] == "no-network"
    windows = _module("slb_windows", EXAMPLE / "utils/benchmark/windows.py")
    assert windows.task_rows(tmp_path / "tasks") == {r["id"]: r for r in ROWS}       # the replay's stream positions
