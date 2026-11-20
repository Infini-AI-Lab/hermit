"""A resumed cohort reuses valid native evidence and stops on unavailable trials."""

import json
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from acting import AheHarborAgent
from binding import write
from hermit.harbor.runner import HarborAgent
from hermit.task import RunOutput, RunUnavailable


def _fake_harbor(tmp_path):
    path = tmp_path / "harbor"
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return str(path)


def test_completed_rollout_is_reused_after_delivery_but_requires_its_evidence(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    adapter = tmp_path / "adapter"
    adapter.mkdir()
    (adapter / "ADAPTER.md").write_text("Solve the task.")
    for args in (
        ["init", "-b", "main"],
        ["config", "user.name", "Test"],
        ["config", "user.email", "test@example.invalid"],
        ["add", "."],
        ["commit", "-m", "fixture"],
    ):
        subprocess.run(
            ["git", "-C", str(adapter), *args], check=True, capture_output=True
        )
    binding = tmp_path / "binding.json"
    write(binding, adapter=adapter, repo=adapter, run_id="test", round_no=1)
    runner = AheHarborAgent(
        {
            "tasks_dir": str(tmp_path / "tasks"),
            "jobs_dir": str(tmp_path / "jobs"),
            "binding": str(binding),
            "harbor": _fake_harbor(tmp_path),
            "model": "openai/test-model",
            "web_search": False,
            "sandbox_enforce": "best_effort",
            "acting_agent": {"cli": "codex", "settings": {}},
        }
    )
    task = {"id": "r1-user1", "ahe_session": {"round": 1}}
    log = tmp_path / "job/log"
    (log / "agent").mkdir(parents=True)
    (log / "ahe-session").mkdir()
    (log / "agent/user-verdict.json").write_text(
        json.dumps(
            {
                "score": 0,
                "usage": {"total_tokens": 7},
            }
        )
    )
    calls = []

    def trial(self, task, adapter):
        calls.append(task["id"])
        return RunOutput(output={"status": "ok"}, log=str(log), tokens=13)

    monkeypatch.setattr(HarborAgent, "run", trial)
    first = runner.run(task, adapter)
    assert first.tokens == 20 and first.extra["actor_tokens"] == 13
    write(binding, adapter=adapter, repo=adapter, run_id="test", round_no=2)
    assert runner.run(task, adapter) == first
    assert calls == ["r1-user1"]
    (log / "ahe-session").rmdir()
    with pytest.raises(RunUnavailable, match="lost its native evidence"):
        runner.run(task, adapter)


def test_unexpected_runner_error_is_unavailable_not_a_task_score(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    runner = AheHarborAgent(
        {
            "tasks_dir": str(tmp_path),
            "harbor": _fake_harbor(tmp_path),
            "model": "openai/test-model",
            "web_search": False,
            "sandbox_enforce": "best_effort",
            "acting_agent": {"cli": "codex", "settings": {}},
        }
    )

    def fail(*args):
        raise OSError("connection lost")

    monkeypatch.setattr(runner, "_run", fail)
    with pytest.raises(RunUnavailable, match="connection lost"):
        runner.run({"id": "trial"}, tmp_path)
