"""The five Harbor benchmarks of examples/ (examples/README.md#harbor-benchmarks) hold the protocols their READMEs
state, read from their committed files alone (each benchmark file, its lock, its rows and splits, the configs), with
no download: the in-sample benchmarks train on every task's rollouts #1 and #2 and test on #3 and #4; the SkillOpt
benchmarks' splits are `hermit.harbor.dataset.split` of the locked tasks with SkillOpt's 2:1:7 and seed 42; a method
trains and validates on no row it is tested on, tests on the rows of the baseline beside it, shares its profile's
jobs_dir (the smoke keeps its own, on rows no run takes) and names its profile's CLI, model, login and effort for the
acting agent and every learner, whose wait for the plan's window has no limit of its own (AHE's clocks count it);
every learner runs in its
sandbox and no agent searches the web; SkillOpt sorts a rollout as its benchmark scores it; a baseline is a config for
`hermit eval` alone (the keys every config names, a training split, a template and an Evolver, are placeholders, and
no Evolver has the name: `hermit train` refuses it) and a method's a training config; every config loads as `hermit`
loads it, prepared or not, which refuses a key it does not know (a block of its own for how a config runs, say); and
the params every config hands the Harbor runner pass the runner's own checks of them at its start (no trial cache,
say)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest
import yaml

from hermit.adapter.manifest import read_manifest
from hermit.agent.acting.agent import load_agent
from hermit.agent.evolver.loader import find
from hermit.config import Config
from hermit.harbor.dataset import split
from hermit.harbor.runner import INFRA_RETRIES, INFRA_WAIT_S

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
    assert run["agent"]["params"]["jobs_dir"] == base["agent"]["params"]["jobs_dir"]   # the profile's trials
    assert smoke["agent"]["params"]["jobs_dir"] != run["agent"]["params"]["jobs_dir"]
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
    assert read(EXAMPLES / bench / "utils" / "benchmark.yaml")["web_search"] is False    # the acting agent's, shared
    for path in sorted((EXAMPLES / bench).glob("*/*.yaml")):
        if path.parent.name == "utils":
            continue
        config = read(path)
        assert config["agent"]["params"].get("web_search", False) is False, path.name     # no config turns it on
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
    assert d["agent"]["name"] == "harbor"
    read_manifest(config.template_dir())                 # hermit eval reads the template's id (its tree's folder)
    if path.parent.name == "baseline":                   # the Trainer's lookup: hermit train refuses the config
        with pytest.raises(ValueError, match="No Evolver named 'eval-only'"):
            find(d["evolver"]["name"], config.base)
    else:
        assert find(d["evolver"]["name"], config.base).name == d["evolver"]["name"]


@pytest.mark.parametrize("path", every_config(), ids=config_id)
def test_a_configs_params_pass_the_runners_own_checks(path, tmp_path, monkeypatch):
    """The runner built from a config's params (its own over its benchmark file's) as hermit builds it, its startup
    checks (check_agent) included: what the runner refuses of its params at its start (a trial cache, a count or a
    cleanup mode it cannot read, a benchmark file's model reference, a login for another CLI, no web_search, a setting
    the CLI's Base Agent refuses as it renders the launch plan) is refused here. This machine's own are stood in for,
    as the run's and hermit check's: the sign-in (its check, and a stand-in for a Claude Code session's token
    variable), bubblewrap and its start of the strict adapter sandbox, and Harbor's own `harbor` (this interpreter).
    The runner's own infra retries apply: the configs set none."""
    import shutil
    from hermit.agent.base import login as subscription
    from hermit.harbor import runner
    which = shutil.which
    monkeypatch.setattr(subscription, "problems_of", lambda *args, **kwargs: [])
    monkeypatch.setattr(shutil, "which", lambda name, *args, **kwargs:
                        "/usr/bin/bwrap" if name == "bwrap" else which(name, *args, **kwargs))
    monkeypatch.setattr(runner, "sandbox_fault", lambda enforce, *, fresh=True: None)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "stand-in-token-of-no-account")
    config = Config.load(path)
    params = {**config.data["agent"]["params"], "harbor": sys.executable, "jobs_dir": str(tmp_path / "jobs")}
    agent = load_agent(config.data["agent"]["name"], params, config.base)
    assert (agent.infra_retries, agent.infra_wait_s) == (INFRA_RETRIES, INFRA_WAIT_S) == (2, 300.0)
