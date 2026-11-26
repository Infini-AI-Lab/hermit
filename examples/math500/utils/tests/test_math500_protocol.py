"""MATH-500's protocol, read from its committed files alone (no download): MATH-500's 500 problems are tested, and
100 training, 50 validation and 2 smoke problems of MATH's training split are for a method that learns, none a test
problem; each baseline config names its profile's CLI, model, login and effort, with no web search, the task's own time,
the answer hook granted to the task sandbox, four attempts a problem, and is `hermit eval`'s alone."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

from hermit.agent.evolver.loader import find
from hermit.config import Config

FOLDER = Path(__file__).resolve().parents[2]
SPLITS = FOLDER / "utils" / "benchmark" / "splits"
PROFILES = {"codex": ("codex", "gpt-5.6-luna", "chatgpt"), "claude": ("claude", "claude-haiku-5-5", "claude")}


def lines(name: str) -> list[str]:
    return (SPLITS / f"{name}.txt").read_text().split()


def prepare():
    path = FOLDER / "utils" / "benchmark" / "prepare.py"
    spec = importlib.util.spec_from_file_location(f"{FOLDER.name}_prepare", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("p", PROFILES)
def test_a_baseline_names_its_profile_and_the_tasks_needs_and_is_hermit_evals_alone(p):
    cli, model, login = PROFILES[p]
    path = FOLDER / "baseline" / f"{p}.yaml"
    config = yaml.safe_load(path.read_text())
    agent = config["agent"]
    assert (agent["base"], agent["model"]) == (cli, {"name": model, "login": login, "effort": "medium"})
    assert agent[cli]["web_search"] is False
    assert agent[cli]["timeout_s"] == prepare().TIMEOUT_SEC             # the session ends on the task's own time
    assert config["environment"]["read"] == ["../../math_grading/utils"]      # the answer hook, in the task sandbox
    assert agent["params"]["answer"] == "../../math_grading/utils/grade.py:answer"
    assert config["judge"]["path"] == "../utils/benchmark/judge.py"
    assert config["data"]["test"] == "../utils/benchmark/splits/test.txt" and config["eval"]["repeats"] == 4
    assert (config["data"]["train"], config["template"], config["evolver"]) == (
        "../utils/benchmark/splits/smoke.txt", "prompt-only@1.0", {"name": "eval-only"})
    loaded = Config.load(path)                               # loads as hermit loads it; hermit train refuses it
    with pytest.raises(ValueError, match="No Evolver named 'eval-only'"):
        find(loaded.data["evolver"]["name"], loaded.base)


def test_math_500_is_tested_and_math_training_problems_are_learned_on():
    test, train, val, smoke = lines("test"), lines("train"), lines("val"), lines("smoke")
    assert (len(test), len(set(test)), len(train), len(val), len(smoke)) == (500, 500, 100, 50, 2)
    assert all(t.startswith("test-") for t in test)
    learned = train + val + smoke
    assert len(set(learned)) == 152 and all(t.startswith("train-") for t in learned)
