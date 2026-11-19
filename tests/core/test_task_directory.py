"""Tasks as directories in Harbor's format (hermit.task.directory), read by Benchmark, and their own verifier script
(hermit.task.verify.script) run in the judge's sandbox as Harbor runs it."""
import json
from pathlib import Path
import subprocess
import shutil

import pytest

from hermit.common import RunOutput
from hermit.config import Config
from hermit.task import Benchmark
from hermit.task.directory import tasks_of
from hermit.environment.spec import EnvironmentSpec
from hermit.isolate import _deadline
from hermit.task.verify import read_reward, resolve_env, script
from toy_task import make_task


def a_task(root: Path, name: str, *, instruction="Write 42 to answer.txt.", test_sh=None, toml_extra="",
           steps=False) -> Path:
    d = root / name
    (d / "tests").mkdir(parents=True)
    (d / "environment").mkdir()
    (d / "instruction.md").write_text(instruction + "\n")
    (d / "task.toml").write_text('version = "1.0"\n[metadata]\ndifficulty = "easy"\n[agent]\ntimeout_sec = 60\n'
                                 '[verifier]\ntimeout_sec = 30\n' + toml_extra +
                                 '[environment]\ncpus = 1\nmemory_mb = 512\nnetwork_mode = "no-network"\n')
    (d / "tests" / "test.sh").write_text(test_sh or "echo 1 > /logs/verifier/reward.txt\n")
    (d / "tests" / "expected.txt").write_text("42")
    if steps:
        with open(d / "task.toml", "a") as f:
            f.write('[[steps]]\nname = "one"\n[[steps]]\nname = "two"\n')
        for step in ("one", "two"):
            (d / "steps" / step / "tests").mkdir(parents=True)
            (d / "steps" / step / "instruction.md").write_text(f"step {step}\n")
    return d


def test_a_directory_of_tasks_is_a_task_set_with_the_tests_kept_private(tmp_path):
    a_task(tmp_path / "tasks", "write-42")
    a_task(tmp_path / "tasks", "flow", steps=True)
    (tmp_path / "tasks" / "notes").mkdir()                          # a folder without task.toml is no task
    (tmp_path / "train.txt").write_text("write-42\nflow:one\n")
    bench = Benchmark.load(tmp_path / "tasks", tmp_path / "train.txt")
    assert sorted(bench.tasks) == ["flow:one", "flow:two", "write-42"]
    task = bench.tasks["write-42"]
    given = task.given()
    assert given["instruction"] == "Write 42 to answer.txt." and given["harbor_task"] == "write-42"
    assert given["metadata"] == {"difficulty": "easy"} and given["network"] == {"agent": "no-network",
                                                                                  "verifier": "no-network"}
    assert (given["cpus"], given["memory_mb"], given["agent_timeout_sec"]) == (1, 512, 60)
    assert not any("tests" in str(v) for v in given.values())       # nothing of the tests reaches the agent
    assert task.private["tests"] == str((tmp_path / "tasks" / "write-42" / "tests").resolve())
    assert task.private["verifier"] == {"timeout_sec": 30, "env": {}}
    assert task.row()["_private"]["tests"] == task.private["tests"]  # what the judge is called with
    assert bench.tasks["flow:two"].private["tests"].endswith("flow/steps/two/tests")
    assert [t.id for t in bench.split("train")] == ["write-42", "flow:one"]
    assert sorted(tasks_of(tmp_path / "tasks", rollouts=2)) == ["flow:one#1", "flow:one#2", "flow:two#1",
                                                                "flow:two#2", "write-42#1", "write-42#2"]
    with pytest.raises(ValueError, match="no task directories"):
        tasks_of(tmp_path / "tasks" / "notes")


def test_a_reward_is_read_as_harbor_writes_it(tmp_path):
    (tmp_path / "reward.txt").write_text("0.5\n")
    assert read_reward(tmp_path, "the script said hi").score == 0.5
    assert read_reward(tmp_path, "the script said hi").feedback == "the script said hi"
    (tmp_path / "feedback.txt").write_text("expected 42, found 41")
    (tmp_path / "reward.json").write_text(json.dumps({"reward": 1, "style": 0.25}))
    verdict = read_reward(tmp_path)
    assert (verdict.score, verdict.feedback, verdict.extra) == (1.0, "expected 42, found 41",
                                                               {"rewards": {"reward": 1.0, "style": 0.25}})
    (tmp_path / "reward.json").write_text(json.dumps({"a": 1, "b": 0}))
    assert read_reward(tmp_path).score == 0.5                     # no `reward`: the mean of the named numbers
    (tmp_path / "reward.json").write_text(json.dumps({"a": "yes"}))
    (tmp_path / "reward.json").write_text(json.dumps({"reward": 1, "f1": 1, "answered": True}))
    assert read_reward(tmp_path).score == 1.0                     # a boolean beside them, as Harbor takes it
    (tmp_path / "reward.json").write_text(json.dumps({"a": "yes"}))
    with pytest.raises(ValueError, match="named finite numbers"):
        read_reward(tmp_path)
    with pytest.raises(RuntimeError, match="wrote no reward"):
        read_reward(tmp_path / "missing", "tail")


def test_the_verifier_script_runs_only_in_the_judges_sandbox():
    with pytest.raises(RuntimeError, match="judge's sandbox"):
        script({"id": "t", "_private": {"tests": "/nowhere"}}, RunOutput(output="42"))


def _bwrap_works() -> bool:
    probe = ["bwrap", "--unshare-user", "--unshare-net", "--ro-bind", "/", "/", "true"]
    return bool(shutil.which("bwrap")) and subprocess.run(probe, capture_output=True).returncode == 0


needs_bwrap = pytest.mark.skipif(not _bwrap_works(), reason="needs a working bubblewrap")

AGENT = """
import json, os
from pathlib import Path
from hermit.task import RunOutput

def run(task, adapter):
    work = os.getcwd()
    Path(work, "answer.txt").write_text("42" if "42" in task["instruction"] else "0")
    return RunOutput(output="done", extra={"workspace": work, "sees_tests": os.path.exists("/tests"),
                                           "fields": sorted(task)})
"""

TEST_SH = """set -u
expected=$(cat /tests/expected.txt)
found=$(cat "$HERMIT_WORKSPACE/answer.txt" 2>/dev/null || echo missing)
cat "$HERMIT_OUTPUT" > /logs/verifier/seen_output.txt
if [ "$found" = "$expected" ]; then echo 1 > /logs/verifier/reward.txt; else echo 0 > /logs/verifier/reward.txt; fi
echo "expected $expected, found $found" > /logs/verifier/feedback.txt
"""


@needs_bwrap
def test_a_task_directory_runs_with_its_own_verifier_in_the_judges_sandbox(tmp_path):
    from hermit.isolate import SandboxedAgent, judge_for
    from hermit.trainer.rollout import run_one
    root = tmp_path / "run"
    root.mkdir()
    path = make_task(root, sandbox={"enabled": True}, agent={"name": "agent/pipeline.py:run", "concurrency": 1},
                     environment={"read": ["agent"]})
    (root / "agent").mkdir()
    (root / "agent" / "pipeline.py").write_text(AGENT)
    a_task(root / "tasks", "write-42", test_sh=TEST_SH)
    a_task(root / "tasks", "write-0", instruction="Write nothing.", test_sh=TEST_SH)
    a_task(root / "tasks", "apart", test_sh=TEST_SH, toml_extra='environment_mode = "separate"\n')
    (root / "splits.txt").write_text("write-42\nwrite-0\napart\n")
    config = Config.load(path, ["data.tasks=tasks", "data.train=splits.txt", "data.val=null", "data.test=null",
                                "judge.path=hermit.task.verify", "judge.name=script", "judge.mode=shared"])
    bench = Benchmark.from_config(config)
    agent = SandboxedAgent(config, "rollout", records=None)
    try:
        verifier = judge_for(config, agent)
        right = run_one(bench.tasks["write-42"], None, agent, verifier)
        wrong = run_one(bench.tasks["write-0"], None, agent, verifier)
        apart = run_one(bench.tasks["apart"], None, agent, verifier)
    finally:
        agent.close()
    assert right["status"] == "ok" and right["judge"]["score"] == 1.0, right
    assert right["judge"]["feedback"] == "expected 42, found 42\n"
    assert right["pipeline_extra"]["sees_tests"] is False            # the tests are the judge's, not the agent's
    assert "_private" not in right["pipeline_extra"]["fields"]       # nor anything else of the private half
    assert wrong["judge"]["score"] == 0.0 and "found 0" in wrong["judge"]["feedback"]
    assert apart["status"] == "judge_error" and "separate verifier" in apart["error"]
    assert "_private" not in right["input"]                          # the record keeps what the agent was given


def test_a_step_keeps_its_own_verifier_settings_and_a_task_asks_for_what_it_needs(tmp_path, monkeypatch):
    d = a_task(tmp_path / "tasks", "flow", steps=True,
               toml_extra='environment_mode = "shared"\n[verifier.env]\nEXPECTED = "42"\n'
                          'KEY = "${HERMIT_T_KEY:-none}"\n')
    text = (d / "task.toml").read_text().replace('[[steps]]\nname = "two"\n',
                                                 '[[steps]]\nname = "two"\n[steps.verifier]\ntimeout_sec = 2\n'
                                                 'environment_mode = "separate"\n')
    (d / "task.toml").write_text(text.replace('network_mode = "no-network"',
                                              'network_mode = "allowlist"\nallowed_hosts = ["data.example.com"]'))
    tasks = tasks_of(tmp_path / "tasks")
    assert tasks["flow:two"].private["verifier"]["timeout_sec"] == 2
    assert tasks["flow:two"].private["verifier"]["environment_mode"] == "separate"
    assert tasks["flow:one"].private["verifier"]["environment_mode"] == "shared"
    env = tasks["flow:one"].private["verifier"]["env"]
    monkeypatch.delenv("HERMIT_T_KEY", raising=False)
    assert resolve_env(env) == {"EXPECTED": "42", "KEY": "none"}
    monkeypatch.setenv("HERMIT_T_KEY", "k")
    assert resolve_env(env)["KEY"] == "k"
    with pytest.raises(RuntimeError, match="HERMIT_T_MISSING"):
        resolve_env({"X": "${HERMIT_T_MISSING}"})
    given = tasks["flow:one"].given()
    assert given["allowed_hosts"] == ["data.example.com"]
    other = EnvironmentSpec(network="allowlist", allowed_hosts=("api.example.com",))
    assert "data.example.com" in " ".join(other.problems_for(given))
    assert EnvironmentSpec(network="allowlist", allowed_hosts=("data.example.com",)).problems_for(given) == []
    loud = {**given, "network": {"agent": "no-network", "verifier": "public"}}
    assert EnvironmentSpec().problems_for(loud) == []                       # judged on the host: the agent's only
    assert "verifier needs network public" in " ".join(EnvironmentSpec().problems_for(loud, verifier=True))


def test_a_task_directorys_own_agent_timeout_bounds_a_call():
    assert _deadline(None, {"agent_timeout_sec": 60}) == 120              # its own, with the worker's start-up
    assert _deadline(90.0, {"agent_timeout_sec": 60}) == 90.0              # the run's when stricter
    assert _deadline(600.0, {"agent_timeout_sec": 60}) == 120
    assert _deadline(600.0, {"id": "row"}) == 600.0 and _deadline(None, {}) is None
