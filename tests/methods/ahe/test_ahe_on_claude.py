"""The AHE Adapter's Evolve Agent and Agent Debugger on the Claude subscription's login (docs/guides/claude-subscription.md), against the stand-in `claude`
(tests/claude_stand.py). The shared login path is tested in tests/core/test_claude_subscription.py."""
import json  # noqa: F401
from pathlib import Path  # noqa: F401

import pytest

from hermit.agent.base import login as subscription  # noqa: F401
from hermit.agent.base.interface import LoginFailed  # noqa: F401
from claude_stand import TOKEN, roles_ctx, stand  # noqa: F401 (stand: the fixture)


def test_the_ahe_learners_take_the_evolvers_cli(stand, tmp_path):
    from hermit.agent.evolver.methods.ahe_adapter import debugger as D
    from hermit.agent.evolver.methods.ahe_adapter.evolver import AHEAdapterEvolver
    from dataclasses import fields
    ctx = roles_ctx(tmp_path, {"prompt": "START.md", "k": 1})
    evolver = AHEAdapterEvolver()
    evolver.setup(ctx)                                               # no refusal for claude
    with pytest.raises(ValueError, match="codex or claude"):
        AHEAdapterEvolver().setup(roles_ctx(tmp_path, {"backend": "pi"}))
    base = evolver.settings(ctx, "claude")
    runner = D.Runner(ctx, {"model": None}, base, tmp_path / "scratch")
    job = D.Job.__new__(D.Job)
    for f in fields(D.Job):
        setattr(job, f.name, {"task": "t1"}.get(f.name) if f.name == "task" else None)
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / "question.md").write_text("why did it fail?")
    assert runner.backend == "claude"
    stand.play({"case": "reply", "text": "the analysis", "edit": ["answer.md", "written answer"]})
    answer, reason = runner.attempt(job, job_dir, 1, 30)
    assert answer == "written answer" and reason == ""
    saw = stand.seen()[0]
    assert saw["prompt"] == "why did it fail?" and saw["env"][subscription.TOKEN_ENV] == TOKEN
    assert not any(a.startswith("model_provider") for a in saw["args"]) and runner.usage[0].cost_usd is None
    stand.play("not_logged_in")
    with pytest.raises(LoginFailed):
        runner.attempt(job, job_dir, 2, 30)


def _jobs(D, n):
    from dataclasses import fields
    out = []
    for i in range(n):
        job = D.Job.__new__(D.Job)
        for f in fields(D.Job):
            setattr(job, f.name, f"t{i}" if f.name == "task" else
                    [] if f.name in ("trials", "trace_rewards", "verifier_outputs") else
                    0 if f.name.startswith("n_") else None)
        job.mode, job.is_timeout = "debug", False
        out.append(job)
    return out


def test_the_agent_debugger_stops_at_a_lost_sign_in_or_a_refused_setting(stand, tmp_path, monkeypatch):
    """A lost sign-in met by one job ends the stage: no queued job starts a session on it. A setting Claude Code
    refuses is refused, not turned into every job's canned fallback; setup() refuses it first."""
    from hermit.agent.evolver.methods.ahe_adapter import debugger as D
    from hermit.agent.evolver.methods.ahe_adapter.evolver import AHEAdapterEvolver
    from hermit.agent.base.interface import Unsupported

    def prepared(job, scratch, params=None, texts=None):
        d = Path(scratch) / "jobs" / job.task
        d.mkdir(parents=True, exist_ok=True)
        (d / "question.md").write_text("why?")
        return d
    monkeypatch.setattr(D, "prepare_job_dir", prepared)
    ctx = roles_ctx(tmp_path / "one", {"prompt": "START.md"})
    stand.play("reply", "not_logged_in")
    with pytest.raises(LoginFailed):
        D.run_jobs(_jobs(D, 10), ctx, {**D.settings(ctx.params), "concurrency": 1, "retries": 1},
                   base=AHEAdapterEvolver().settings(ctx, "claude"), scratch=tmp_path / "one" / "scratch")
    assert len(stand.seen()) == 2                                       # the one served and the one that met it
    ctx = roles_ctx(tmp_path / "two", {"prompt": "START.md", "debugger": {"effort": "minimal"}})
    stand.play("reply")
    with pytest.raises(Unsupported, match="not 'minimal'"):
        D.run_jobs(_jobs(D, 3), ctx, {**D.settings(ctx.params), "retries": 2, "backoff_s": 0},
                   base=AHEAdapterEvolver().settings(ctx, "claude"), scratch=tmp_path / "two" / "scratch")
    assert stand.seen() == []
    with pytest.raises(ValueError, match="debugger.effort is one of"):
        AHEAdapterEvolver().setup(ctx)
