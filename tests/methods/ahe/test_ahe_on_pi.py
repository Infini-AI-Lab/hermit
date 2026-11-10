"""The AHE Adapter's Evolve Agent and Agent Debugger on Pi (`evolver.base: pi`), against the stand-in `pi`
(tests/pi_stand.py)."""
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
    runner = D.Runner(ctx, {"model": None}, evolver.settings(ctx, "pi"), tmp_path / "scratch")
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
    with pytest.raises(ValueError, match="evolver.params.effort is one of off, minimal.*for Pi, not 'max'"):
        AHEAdapterEvolver().setup(roles_ctx(tmp_path, {"login": None, **bound_params(), "effort": "max"}))


def test_the_learners_texts_name_a_pi_acting_agents_stream():
    from hermit.agent.evolver.methods.ahe_adapter import debugger as D
    text = "the Codex event stream, `agent-codex.jsonl` (the raw Codex event stream, `codex exec --json` event stream)"
    assert D.for_stream(text, "agent-pi.jsonl") == ("the Pi event stream, `agent-pi.jsonl` (the raw Pi event stream, "
                                                    "`pi --mode json` event stream)")
