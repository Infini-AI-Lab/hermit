"""The five Harbor benchmarks of examples/ (examples/README.md#harbor-benchmarks) hold the protocols their READMEs
state, read from their committed files alone (each benchmark file's dataset block, its lock, its rows and splits, the
configs), with no download: the in-sample benchmarks train on every task's rollouts #1 and #2 and test on #3 and #4;
the SkillOpt benchmarks' splits are `hermit.harbor.dataset.split` of the locked tasks with SkillOpt's 2:1:7 and seed
42; a method's configs test on the rows of the baseline beside them, share its trial cache (the smoke keeps its own)
and name their profile's CLI, model, login and effort for the acting agent and every learner. Where a benchmark is
prepared on this machine (its `utils/tasks.jsonl`), its configs also load as `hermit` loads them."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from hermit.config import Config
from hermit.harbor.dataset import split

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
IN_SAMPLE = ["terminal_bench_2_0", "terminal_bench_science"]
SKILLOPT_SPLIT = ["algotune", "labbench"]
METHODS = {"terminal_bench_2_0": "ahe_adapter", "terminal_bench_science": "ahe_adapter", "algotune": "skillopt",
           "labbench": "skillopt", "swebench_verified": None}
PROFILES = {"codex": {"cli": "codex", "model": "gpt-5.6-luna", "login": "chatgpt"},
            "claude": {"cli": "claude", "model": "claude-haiku-5-5", "login": "claude"}}


def read(path: Path):
    return yaml.safe_load(path.read_text())


def lines(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text().splitlines() if line.strip()]


def locked(bench: str) -> list[str]:
    return sorted(json.loads((EXAMPLES / bench / "utils" / "dataset.lock").read_text())["tasks"])


@pytest.mark.parametrize("bench", IN_SAMPLE)
def test_an_in_sample_benchmark_trains_on_rollouts_1_and_2_and_tests_on_3_and_4(bench):
    block = read(EXAMPLES / bench / "utils" / "benchmark.yaml")["dataset"]
    assert block["rollouts"] == 4 and block["split"] == "in_sample"
    rows = EXAMPLES / bench / "utils" / "rows"
    tasks = locked(bench)
    assert lines(rows / "train.txt") == [f"{t}#{k}" for t in tasks for k in (1, 2)]   # a task's two rollouts together
    assert lines(rows / "test.txt") == [f"{t}#{k}" for t in tasks for k in (3, 4)]


@pytest.mark.parametrize("bench", SKILLOPT_SPLIT)
def test_a_skillopt_benchmark_splits_its_locked_tasks_2_1_7_with_seed_42(bench, tmp_path):
    spec = read(EXAMPLES / bench / "utils" / "benchmark.yaml")["dataset"]["split"]
    tasks = locked(bench)
    assert spec["seed"] == 42 and spec["test"] == "rest"
    assert (spec["train"], spec["val"]) == (round(len(tasks) * 0.2), round(len(tasks) * 0.1))
    for name in tasks:                                       # the task set as prepare's split reads it: names only
        (tmp_path / name).mkdir()
        (tmp_path / name / "task.toml").write_text("")
    expected = split(tmp_path, train=spec["train"], val=spec["val"], test=spec["test"], seed=spec["seed"])
    splits = EXAMPLES / bench / "utils" / "splits"
    for part in ("train", "val", "test"):
        assert sorted(lines(splits / f"{part}.txt")) == expected[part]


def configs(bench: str, p: str) -> dict[str, dict]:
    folder, method = EXAMPLES / bench, METHODS[bench]
    out = {"baseline": read(folder / "baseline" / f"{p}.yaml")}
    if method:
        out.update(run=read(folder / method / f"run_{p}.yaml"), smoke=read(folder / method / f"smoke_{p}.yaml"))
    return out


@pytest.mark.parametrize("p", PROFILES)
@pytest.mark.parametrize("bench", METHODS)
def test_a_method_tests_on_its_baselines_rows_with_its_profiles_models(bench, p):
    profile, got = PROFILES[p], configs(bench, p)
    for name, config in got.items():
        params = config["agent"]["params"]
        assert params["acting_agent"] == {"cli": profile["cli"]}, name
        assert (params["model"], params["login"], params["effort"]) == (profile["model"], profile["login"],
                                                                        "medium"), name
        assert params["benchmark"] == "../utils/benchmark.yaml", name
        if name != "baseline":                               # every learner on the acting model
            if "llm" in config:                              # SkillOpt's learner
                llm = config["llm"]
                assert (llm["cli"], llm["model"], llm["login"], llm["effort"]) == (profile["cli"], profile["model"],
                                                                                   profile["login"], "medium")
                assert llm["web_search"] is False
            else:                                            # AHE's Debugger and Evolve Agent
                evolver = config["evolver"]
                assert evolver["base"] == profile["cli"]
                assert evolver["model"] == {"name": profile["model"], "login": profile["login"], "effort": "medium"}
                assert evolver[profile["cli"]]["web_search"] is False
    if "run" not in got:
        return
    base, run, smoke = got["baseline"], got["run"], got["smoke"]
    assert run["data"]["test"] == base["data"]["test"]
    assert run["judge"] == base["judge"]
    assert run["agent"]["params"]["jobs_dir"] == base["agent"]["params"]["jobs_dir"]   # one trial cache
    assert smoke["agent"]["params"]["jobs_dir"] != run["agent"]["params"]["jobs_dir"]
    assert smoke["data"]["train"] == smoke["data"]["test"] == "../utils/splits/smoke.txt"
    assert run["run"] == {"mode": "split"} and base["run"] == {"mode": "eval"}


def every_config():
    return [path for bench in METHODS for path in sorted((EXAMPLES / bench).glob("*/*.yaml"))
            if path.parent.name != "utils"]


@pytest.mark.parametrize("path", every_config(), ids=lambda p: f"{p.parts[-3]}/{p.parts[-2]}/{p.name}")
def test_a_config_loads_where_its_benchmark_is_prepared(path):
    if not (path.parents[1] / "utils" / "tasks.jsonl").is_file():
        pytest.skip(f"{path.parts[-3]} is not prepared here (python -m hermit.harbor.dataset prepare)")
    raw = read(path)
    if (raw.get("evolver") or {}).get("name") == "skillopt":
        pytest.importorskip("skillopt")
    command = "eval" if raw["run"]["mode"] == "eval" else "train"
    config = Config.load(path, command=command)
    assert config.data["agent"]["name"] == "harbor"
