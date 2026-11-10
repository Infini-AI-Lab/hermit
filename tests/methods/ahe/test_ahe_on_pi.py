"""The AHE Adapter's learners on Pi (`evolver.base: pi`): the Agent Debugger's session against the stand-in `pi`
(tests/pi_stand.py), on the Evolve Agent's settings, which the agent Evolver's own Pi route runs (tests/core)."""
from dataclasses import fields
import json

import pytest

from claude_stand import roles_ctx
from pi_stand import bound_params, pi_stand  # noqa: F401 (pi_stand: the fixture)


def test_the_ahe_learners_run_on_pi_with_its_bound_model(pi_stand, tmp_path):  # noqa: F811
    from hermit.agent.evolver.methods.ahe_adapter import debugger as D
    from hermit.agent.evolver.methods.ahe_adapter.evolver import AHEAdapterEvolver
    ctx = roles_ctx(tmp_path, {"login": None, "prompt": "START.md", "k": 1, **bound_params()})
    evolver = AHEAdapterEvolver()
    evolver.setup(ctx)
    base = evolver.settings(ctx, "pi")                                # the Evolve Agent's own
    assert base["provider"]["apiKey"] == "$FAKE_KEY" and base["model"] == "m-1"
    runner = D.Runner(ctx, {"model": None}, base, tmp_path / "scratch")
    job = D.Job.__new__(D.Job)
    for f in fields(D.Job):
        setattr(job, f.name, {"task": "t1"}.get(f.name) if f.name == "task" else None)
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / "question.md").write_text("why did it fail?")
    pi_stand.play(edits={"answer.md": "written answer"})
    assert runner.attempt(job, job_dir, 1, 30) == ("written answer", "")
    [call] = pi_stand.seen()
    assert call["stdin"] == "why did it fail?" and call["args"][:6] == ["--mode", "json", "--provider", "hermit", "--model",
                                                                       "m-1"]
    assert json.loads(call["files"]["models.json"])["providers"]["hermit"]["apiKey"] == "$FAKE_KEY"


def test_an_effort_pi_does_not_take_is_refused_at_setup(tmp_path):
    from hermit.agent.evolver.methods.ahe_adapter.evolver import AHEAdapterEvolver
    with pytest.raises(ValueError, match="evolver.params.effort is one of off, minimal.*for Pi, not 'bogus'"):
        AHEAdapterEvolver().setup(roles_ctx(tmp_path, {"login": None, **bound_params(), "effort": "bogus"}))
    AHEAdapterEvolver().setup(roles_ctx(tmp_path, {"login": None, **bound_params(), "effort": "max"}))

