"""AlgoTune's judge (utils/judge.py): a trial's score is 1 - 1/speedup, so the mean m of the scores is 1 - 1/H for the
harmonic mean H of the speedups, AlgoTune's own score of a set of tasks. The trials are the runner's outputs as the
Harbor judge reads them (hermit.harbor.judge): the verifier's reward is the speedup, with no pass reward."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

JUDGE = Path(__file__).resolve().parents[1] / "judge.py"


@pytest.fixture(scope="module")
def judge():
    spec = importlib.util.spec_from_file_location("algotune_judge", JUDGE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def trial(status="ok", reward=None, log=None):
    """A Harbor trial's output as the runner hands it to the judge, on a benchmark with no pass reward."""
    out = {"status": status, "reward": reward, "rewards": None if reward is None else {"reward": reward}}
    return SimpleNamespace(output=out, log=log, extra={"judge": {"pass_reward": None}})


@pytest.mark.parametrize("speedup, score", [(None, 0.0), (0.0, 0.0), (0.5, 0.0), (1.0, 0.0), (2.0, 0.5),
                                            (4.0, 0.75), (10.0, 0.9)])
def test_a_speedup_scores_one_less_its_inverse(judge, speedup, score):
    assert judge.speedup_score(speedup) == pytest.approx(score)


def test_a_trial_keeps_its_speedup_beside_the_score(judge, tmp_path):
    (tmp_path / "verifier").mkdir()
    (tmp_path / "verifier" / "test-stdout.txt").write_text("100 instances valid; speedup 2.5\n")
    record = judge.score({}, trial(reward=2.5, log=tmp_path))
    assert record["speedup"] == 2.5 and record["score"] == pytest.approx(0.6)
    assert record["passed"] is None                          # a speedup has no pass
    assert record["feedback"].startswith("speedup 2.5x (score 0.600): reward 2.5")
    assert "speedup 2.5" in record["feedback"].split("verifier output (tail):")[1]


def test_no_speedup_and_a_timed_out_agent_score_their_reward(judge):
    floor = judge.score({}, trial(reward=1.0))               # invalid outputs, or not faster: AlgoTune's 1x
    assert floor["speedup"] == 1.0 and floor["score"] == 0.0
    late = judge.score({}, trial(status="timeout", reward=3.0))   # the verifier still timed the agent's solver
    assert late["speedup"] == 3.0 and late["score"] == pytest.approx(2 / 3)


def test_infra_is_no_speedup_and_no_score(judge):
    record = judge.score({}, trial(status="infra", reward=5.0))
    assert record["speedup"] is None and record["score"] == 0.0
    assert not record["feedback"].startswith("speedup")


def test_the_mean_score_gives_the_harmonic_mean(judge, tmp_path):
    speedups = [2.0, 1.0, 4.0, 1.0]                          # AlgoTune's H: 4 / (1/2 + 1 + 1/4 + 1) = 16/11
    rows = [{"id": f"t{i}#0", "status": "ok", "judge": judge.score({}, trial(reward=s))}
            for i, s in enumerate(speedups)]
    rows.append({"id": "t9#0", "status": "infra", "judge": None})
    results = tmp_path / "results.jsonl"
    results.write_text("".join(json.dumps(r) + "\n" for r in rows) + "\n")
    summary = judge.harmonic_mean(results)
    assert summary["trials"] == 4 and summary["infra"] == 1 and summary["sped_up"] == 2
    assert summary["harmonic_mean_speedup"] == pytest.approx(len(speedups) / sum(1 / s for s in speedups))
    assert summary["mean_score"] == pytest.approx(1 - 1 / summary["harmonic_mean_speedup"])


def test_an_eval_with_no_scored_trial_has_no_mean(judge, tmp_path):
    results = tmp_path / "results.jsonl"
    results.write_text(json.dumps({"id": "t0#0", "status": "infra", "judge": None}) + "\n")
    assert judge.harmonic_mean(results) == {"trials": 0, "infra": 1, "mean_score": None,
                                            "harmonic_mean_speedup": None, "sped_up": 0}
