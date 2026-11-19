"""The five Harbor benchmarks of examples/ (examples/README.md#harbor-benchmarks) hold the protocols their READMEs
state, read from their committed files alone (each benchmark file, its lock, its rows and splits, the configs), with
no download: the in-sample benchmarks train on every task's rollouts #1 and #2 and test on #3 and #4; the SkillOpt
benchmarks' splits are `hermit.harbor.dataset.split` of the locked tasks with SkillOpt's 2:1:7 and seed 42; a method
trains and validates on no row it is tested on, tests on the rows of the baseline beside it, shares its profile's
jobs_dir (the smoke keeps its own, on rows no run takes) and names its profile's CLI, model, login and effort for the
acting agent and every learner, whose wait for the plan's window has no limit of its own (AHE's clocks count it);
every learner runs in its sandbox and no agent searches the web; SkillOpt sorts a rollout as its benchmark scores it;
a baseline is a config for `hermit eval` alone (the keys every config names, a training split, a template and an
Evolver, are placeholders, and no Evolver has the name: `hermit train` refuses it) and a method's a training config;
every config loads as `hermit` loads it, prepared or not, which refuses a key it does not know (a block of its own for
how a config runs, say); and the Harbor settings every config gives (its own over its benchmark file's) pass the
Harbor environments' own checks of them (hermit.environment.harbor)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest
import yaml

from hermit.adapter.manifest import read_manifest
from hermit.agent.evolver.loader import find
from hermit.config import Config
from hermit.harbor.dataset import split

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
IN_SAMPLE = ["terminal_bench_2_0", "terminal_bench_science"]
SKILLOPT_SPLIT = ["algotune", "labbench"]
METHODS = {"terminal_bench_2_0": "ahe_adapter", "terminal_bench_science": "ahe_adapter", "algotune": "skillopt",
           "labbench": "skillopt", "swebench_verified": None}
PROFILES = {"codex": {"cli": "codex", "model": "gpt-5.6-luna", "login": "chatgpt"},
            "claude": {"cli": "claude", "model": "claude-haiku-5-5", "login": "claude"}}
LEARNS_ON = {"ahe_adapter": ("../utils/rows/train.txt", None),            # (data.train, data.val) of a method's run
             "skillopt": ("../utils/splits/train.txt", "../utils/splits/val.txt")}
# a baseline's data.train, template and evolver: what every config names, which hermit eval uses none of
PLACEHOLDERS = ("../utils/splits/smoke.txt", "prompt-only@1.0", {"name": "eval-only"})


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
        agent = config["agent"]
        assert agent["base"] == profile["cli"], name
        assert agent["model"] == {"name": profile["model"], "login": profile["login"], "effort": "medium"}, name
        assert agent[profile["cli"]]["web_search"] is False, name
        assert config["environment"]["harbor"]["benchmark"] == "../utils/benchmark.yaml", name
        if name != "baseline":                               # every learner on the acting model
            if "llm" in config:                              # SkillOpt's learner
                llm = config["llm"]
                assert (llm["cli"], llm["model"], llm["login"], llm["effort"]) == (profile["cli"], profile["model"],
                                                                                   profile["login"], "medium")
                assert llm["web_search"] is False
                assert "plan_max_wait_s" in llm and llm["plan_max_wait_s"] is None    # a hold waited out
            else:                                            # AHE's Debugger and Evolve Agent
                evolver = config["evolver"]
                assert evolver["base"] == profile["cli"]
                assert evolver["model"] == {"name": profile["model"], "login": profile["login"], "effort": "medium"}
                assert evolver[profile["cli"]]["web_search"] is False
                own = evolver[profile["cli"]]
                assert "plan_max_wait_s" in own and own["plan_max_wait_s"] is None    # within AHE's clocks
    base = got["baseline"]
    assert (base["data"]["train"], base["template"], base["evolver"]) == PLACEHOLDERS   # hermit eval alone
    if "run" not in got:
        return
    run, smoke = got["run"], got["smoke"]
    assert run["data"]["test"] == base["data"]["test"]
    assert run["judge"] == base["judge"]
    jobs = lambda config: config["environment"]["harbor"]["jobs_dir"]
    assert jobs(run) == jobs(base)                                          # the profile's environments
    assert jobs(smoke) != jobs(run)
    assert smoke["data"]["train"] == smoke["data"]["test"] == "../utils/splits/smoke.txt"
    where = EXAMPLES / bench / METHODS[bench]
    taken = {row for part in ("train", "val", "test") if run["data"].get(part)
             for row in lines(where / run["data"][part])}
    assert not set(lines(where / smoke["data"]["test"])) & taken            # the smoke's rows: no run takes them
    for config in (run, smoke):
        assert config["data"]["train"] and config["template"] and config["evolver"]["name"]


@pytest.mark.parametrize("p", PROFILES)
@pytest.mark.parametrize("bench", [b for b, m in METHODS.items() if m])
def test_a_method_trains_and_validates_on_no_row_it_is_tested_on(bench, p):
    where = EXAMPLES / bench / METHODS[bench]
    data = read(where / f"run_{p}.yaml")["data"]
    train, val = LEARNS_ON[METHODS[bench]]
    assert (data["train"], data.get("val")) == (train, val)
    learned = set(lines(where / train)) | (set(lines(where / val)) if val else set())
    tested = set(lines(where / data["test"]))
    assert learned and tested and not learned & tested


def every_config():
    return [path for bench in METHODS for path in sorted((EXAMPLES / bench).glob("*/*.yaml"))
            if path.parent.name != "utils"]


@pytest.mark.parametrize("bench", METHODS)
def test_every_learner_is_sandboxed_and_no_agent_searches_the_web(bench):
    assert "web_search" not in read(EXAMPLES / bench / "utils" / "benchmark.yaml")       # the agent's, not the task set's
    for path in sorted((EXAMPLES / bench).glob("*/*.yaml")):
        if path.parent.name == "utils":
            continue
        config = read(path)
        agent = config["agent"]
        assert agent[agent["base"]]["web_search"] is False, path.name                    # every config sets it off
        if "evolver" in config and path.parent.name != "baseline":        # a baseline's Evolver: a placeholder
            sandbox = config["sandbox"]
            assert sandbox["enabled"] is True and sandbox.get("enforce", "required") == "required", path.name
        if "llm" in config:
            assert config["llm"]["isolation"] == "sandbox", path.name


def algotune_score(speedup: float) -> float:
    spec = importlib.util.spec_from_file_location("algotune_judge", EXAMPLES / "algotune" / "utils" / "judge.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.speedup_score(speedup)


@pytest.mark.parametrize("p", PROFILES)
@pytest.mark.parametrize("bench", SKILLOPT_SPLIT)
def test_skillopt_sorts_a_rollout_as_its_benchmark_scores_it(bench, p):
    for name in ("run", "smoke"):
        params = read(EXAMPLES / bench / "skillopt" / f"{name}_{p}.yaml")["evolver"]["params"]
        assert params["codex_trace_to_optimizer"] is False, name       # the summary is of SkillOpt's own harness
        if bench == "algotune":                  # a success: a speedup over the reference (the verifier's 4 decimals)
            assert algotune_score(1.0) < params["success_score"] <= algotune_score(1.0001), name
            assert params["gate_metric"] == "soft", name
        else:                                    # the right letter: the verifier's 1
            assert "success_score" not in params and params["gate_metric"] == "hard", name


def test_labbench_new_figure_rows_are_test_questions():
    rows = EXAMPLES / "labbench" / "utils"
    new, test = lines(rows / "rows" / "test_new_figures.txt"), lines(rows / "splits" / "test.txt")
    assert new and set(new) < set(test) and new == [row for row in test if row in set(new)]


def config_id(path: Path) -> str:
    return f"{path.parts[-3]}/{path.parts[-2]}/{path.name}"


@pytest.mark.parametrize("path", every_config(), ids=config_id)
def test_a_config_loads_as_hermit_loads_it(path):
    # no download needed: a config names its benchmark's files, not its tasks
    config = Config.load(path)                           # a key hermit does not know, a `run:` block say, fails here
    d = config.data
    assert d["agent"]["name"] in ("codex", "claude-code") and d["environment"]["harbor"]
    assert (d["judge"]["path"], d["judge"]["name"]) in (("hermit.task.verify", "environment"),
                                                        ("../utils/judge.py", "score"))
    read_manifest(config.template_dir())                 # hermit eval reads the template's id (its tree's folder)
    if path.parent.name == "baseline":                   # the Trainer's lookup: hermit train refuses the config
        with pytest.raises(ValueError, match="No Evolver named 'eval-only'"):
            find(d["evolver"]["name"], config.base)
    else:
        assert find(d["evolver"]["name"], config.base).name == d["evolver"]["name"]


@pytest.mark.parametrize("path", every_config(), ids=config_id)
def test_a_configs_harbor_settings_pass_the_environments_own_checks(path, tmp_path):
    """The run's Harbor environments built from a config's settings (its own over its benchmark file's) as a run builds
    them: what they refuse at the start (an unknown key, a cleanup mode or a verifier_timeout they do not know, a
    multiplier below 1, no tasks_dir) is refused here. Harbor's own command is stood in for (this interpreter, with a
    python beside it), and the jobs directory is a temporary one; no environment is started."""
    from hermit.environment.harbor import HarborEnvironments
    config = Config.load(path)
    settings = {**config.data["environment"]["harbor"], "harbor": sys.executable, "jobs_dir": str(tmp_path / "jobs")}
    environments = HarborEnvironments(settings, base=config.base)
    assert environments.tasks_dir.name and environments.cleanup in ("images_keep", "rmi_task_image", "rmi_over_gb")
    assert environments.environment_config()["delete"] is True
