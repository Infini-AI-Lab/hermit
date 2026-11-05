"""The Harbor runner against a real Docker daemon: a single-step task as one trial, a three-step task as a stream
(one container through the Trainer's steps), with the scripted agent and no model.

Needs Docker, Harbor's venv (~/.cache/hermit/harbor-venv or HERMIT_HARBOR_VENV) and HERMIT_HARBOR_SMOKE=1; skipped
otherwise. The two tasks are built here from `python:3.11-slim`; everything they leave is under a temporary
directory, and the stream's socket under the user's runtime directory.
"""
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess

import pytest

from hermit.harbor import dataset

HARBOR_VENV = Path(os.environ.get("HERMIT_HARBOR_VENV", "~/.cache/hermit/harbor-venv")).expanduser()
TOML = '''schema_version = "1.4"

[metadata]
author_name = "hermit"
difficulty = "easy"
category = "smoke"
tags = ["smoke"]

[verifier]
timeout_sec = 60.0

[agent]
timeout_sec = 90.0

[environment]
build_timeout_sec = 300.0
cpus = 1
memory_mb = 512
'''
DOCKERFILE = "FROM python:3.11-slim\nWORKDIR /app\n"


def _write(path: Path, text: str, exe: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if exe:
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def make_tasks(root: Path) -> None:
    """`echo-answer` (one step: 42 in /app/answer.txt) and `counter` (three steps: one more line in /app/log.txt
    each; a step's verifier counts the lines, so it passes only when the earlier steps' work is still there)."""
    t = root / "echo-answer"
    _write(t / "task.toml", TOML)
    _write(t / "instruction.md", "Write the number 42 to /app/answer.txt.\n")
    _write(t / "environment" / "Dockerfile", DOCKERFILE)
    _write(t / "tests" / "test.sh", '#!/bin/bash\nmkdir -p /logs/verifier\n'
           'if [ "$(cat /app/answer.txt 2>/dev/null | tr -d "[:space:]")" = "42" ]; then echo 1 > /logs/verifier/reward.txt; '
           'echo PASS; else echo 0 > /logs/verifier/reward.txt; echo "FAIL: answer.txt is $(cat /app/answer.txt 2>/dev/null)"; fi\n',
           exe=True)
    s = root / "counter"
    _write(s / "task.toml", TOML + "".join(f'\n[[steps]]\nname = "s{i}"\n' for i in (1, 2, 3)))
    _write(s / "instruction.md", "A three-step stream: each step appends one line to /app/log.txt.\n")
    _write(s / "environment" / "Dockerfile", DOCKERFILE)
    _write(s / "tests" / "test.sh", "#!/bin/bash\nmkdir -p /logs/verifier; echo 0 > /logs/verifier/reward.txt\n", exe=True)
    for i in (1, 2, 3):
        _write(s / "steps" / f"s{i}" / "instruction.md", f"Step {i}: append the line 'step {i}' to /app/log.txt.\n")
        _write(s / "steps" / f"s{i}" / "tests" / "test.sh", "#!/bin/bash\nmkdir -p /logs/verifier\n"
               f'n=$(wc -l < /app/log.txt 2>/dev/null || echo 0); if [ "$n" -eq {i} ]; then echo 1 > /logs/verifier/reward.txt; '
               f'echo PASS $n; else echo 0 > /logs/verifier/reward.txt; echo "FAIL: $n lines"; fi\n', exe=True)


def _docker_ok() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


pytestmark = pytest.mark.skipif(
    os.environ.get("HERMIT_HARBOR_SMOKE") != "1" or not (HARBOR_VENV / "bin" / "harbor").is_file() or not _docker_ok(),
    reason="HERMIT_HARBOR_SMOKE=1, Harbor's venv and Docker are needed")


@pytest.fixture(scope="module")
def tasks(tmp_path_factory):
    root = tmp_path_factory.mktemp("harbor-smoke")
    make_tasks(root / "tasks")
    rows = {r["id"]: r for r in dataset.index(root / "tasks")}
    yield root, rows
    shutil.rmtree(root, ignore_errors=True)


def _agent(root: Path, script: str, jobs: str, **params):
    from hermit.harbor.runner import HarborAgent
    return HarborAgent({"tasks_dir": str(root / "tasks"), "jobs_dir": str(root / jobs), "disk_gate_gb": 5,
                        "harbor": str(HARBOR_VENV / "bin" / "harbor"),
                        "acting_agent": {"import_path": "harborside.scripted:ScriptedAgent", "kwargs": {"script": script}},
                        **params}, base=root)


def test_single_step_trial_scores_the_verifier_reward(tasks):
    from hermit.harbor.judge import score
    root, rows = tasks
    out = _agent(root, "echo 42 > /app/answer.txt", "jobs").run(rows["echo-answer"], None)
    assert out.output["status"] == "ok" and out.output["reward"] == 1.0, out.extra
    assert out.tokens == 0 and out.log and (Path(out.log) / "verifier" / "reward.txt").read_text().strip() == "1"
    assert (Path(out.log) / "agent" / "script.txt").is_file() and (Path(out.log) / "result.json").is_file()
    assert score(rows["echo-answer"], out)["score"] == 1.0
    wrong = _agent(root, "echo 41 > /app/answer.txt", "jobs").run(rows["echo-answer"], None)
    assert wrong.output["status"] == "ok" and wrong.output["reward"] == 0.0
    verdict = score(rows["echo-answer"], wrong)
    assert verdict["score"] == 0.0 and "FAIL: answer.txt is 41" in verdict["feedback"]


def test_multi_step_task_runs_as_one_stream(tasks):
    root, rows = tasks
    agent = _agent(root, "echo step >> /app/log.txt", "jobs-stream", stream={"idle_s": 600, "start_timeout_s": 600})
    results = [agent.run(rows[f"counter:s{i}"], None) for i in (1, 2, 3)]
    for i, out in enumerate(results, start=1):
        assert out.output["status"] == "ok" and out.output["reward"] == 1.0, (i, out.extra)
        assert out.output["step"] == f"s{i}" and out.output["trial"] == "counter__s1"
        assert (Path(out.log) / "verifier" / "test-stdout.txt").read_text().strip() == f"PASS {i}"
    stream_dir = root / "jobs-stream" / "streams" / "counter__s1"
    result = json.loads((stream_dir / "trials" / "counter__s1" / "result.json").read_text())
    assert [s["step_name"] for s in result["step_results"]] == ["s1", "s2", "s3"]
    assert result["verifier_result"]["rewards"]["reward"] == 1.0           # the mean over the steps
    containers = subprocess.run(["docker", "ps", "-a", "--format", "{{.Names}}"], capture_output=True, text=True).stdout
    assert "counter__s1" not in containers                                   # the stream's environment is down
