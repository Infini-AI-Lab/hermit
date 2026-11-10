"""Harness²'s editors on Pi (`evolver.base: pi`), against the stand-in `pi` (tests/pi_stand.py), and a Pi acting agent's
trajectory in upstream's grammar."""
import json

import pytest

from pi_stand import STREAMS, bound_params, pi_stand  # noqa: F401 (pi_stand: the fixture)


def proposer(**more):
    pytest.importorskip("harness2")
    from harness2.core.spec import ChannelSpec
    from hermit.agent.evolver.methods.harness2_upstream.channel import FrameworkProposer
    return FrameworkProposer(spec=ChannelSpec(kind="claude_cli", model="m-1", effort="max"),
                             params={**bound_params(), **more})


def test_harness2s_session_on_pi_takes_its_bound_model_and_keeps_its_usage(pi_stand, tmp_path):  # noqa: F811
    prop = proposer()
    cwd = tmp_path / "session"
    cwd.mkdir()
    text, cost, problem = prop._claude_cli(cwd, "the mission", "be brief", wall=60)
    [call] = pi_stand.seen()
    assert (text, problem) == ("the answer", "") and prop.served == 1 and cost == pytest.approx(prop.usages[0].cost_usd)
    assert call["stdin"] == "the mission" and "--no-session" in call["args"]
    assert call["args"][:6] == ["--mode", "json", "--provider", "hermit", "--model", "m-1"]
    assert call["args"][call["args"].index("--append-system-prompt") + 1] == "be brief"
    assert call["args"][call["args"].index("--thinking") + 1] == "max"           # Claude Code's levels are Pi's too
    model = json.loads(call["files"]["models.json"])["providers"]["hermit"]["models"][0]
    assert model["thinkingLevelMap"] == {"max": "max"}                         # sent as max, not Pi's high
    assert call["extensions"] == ["hermit-command-limit.js"]
    proposer(command_timeout_s=0)._claude_cli(cwd, "the mission", "", wall=60)   # evolver.pi's, passed on
    assert pi_stand.seen()[0]["extensions"] == []


def test_a_pi_session_that_failed_is_no_reply(pi_stand, tmp_path):  # noqa: F811
    prop = proposer()
    cwd = tmp_path / "session"
    cwd.mkdir()
    pi_stand.play("failure")
    text, cost, problem = prop._claude_cli(cwd, "the mission", "", wall=60)
    assert text == "" and cost == 0.0 and problem.startswith("rc=0") and prop.served == 0


def test_a_pi_reply_cut_at_its_limit_is_no_reply_and_what_it_spent_is_kept(pi_stand, tmp_path):  # noqa: F811
    prop = proposer()
    cwd = tmp_path / "session"
    cwd.mkdir()
    pi_stand.play(cut=True)
    text, cost, problem = prop._claude_cli(cwd, "the mission", "", wall=60)
    assert text == "" and "cut at its output limit" in problem and prop.served == 0
    assert cost == pytest.approx(prop.usages[0].cost_usd) and prop.usages[0].output_tokens == 20


def test_a_session_on_pi_keeps_its_models_key_where_upstream_drops_anthropics(pi_stand, tmp_path, monkeypatch):  # noqa: F811
    """Upstream drops ANTHROPIC_API_KEY for its Claude sessions; a Pi session bound to a backend whose key is in that
    variable (the built-in anthropic backend's) needs it."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
    prop = proposer(**bound_params(key_env="ANTHROPIC_API_KEY"))
    cwd = tmp_path / "session"
    cwd.mkdir()
    prop._claude_cli(cwd, "the mission", "", wall=60)
    [call] = pi_stand.seen()
    assert "ANTHROPIC_API_KEY" in call["keys"]


def test_harness2_runs_its_editors_on_pi_and_pins_pis_binary(pi_stand, tmp_path):  # noqa: F811
    pytest.importorskip("harness2")
    from types import SimpleNamespace as NS
    from hermit.agent.evolver.methods.harness2_upstream.evolver import Harness2UpstreamEvolver
    Harness2UpstreamEvolver().setup(NS(params=bound_params()))
    # refused at setup, not by each session: upstream would take a failed session for the starting harness
    with pytest.raises(ValueError, match="takes its model from evolver.base: pi and evolver.model"):
        Harness2UpstreamEvolver().setup(NS(params={"backend": "pi"}))
    with pytest.raises(ValueError, match="Pi takes effort .*not 'huge'"):
        Harness2UpstreamEvolver().setup(NS(params={**bound_params(), "effort": "huge"}))
    messages = {**bound_params(), "effort": "max"}
    messages["provider"] = {**messages["provider"], "api": "anthropic-messages"}
    with pytest.raises(ValueError, match="Messages API high's thinking budget for effort max"):
        Harness2UpstreamEvolver().setup(NS(params=messages))
    with pytest.raises(ValueError, match="not the sign-in of backend pi"):
        Harness2UpstreamEvolver().setup(NS(params={"backend": "pi", "login": "claude", "model": "m"}))
    ws = NS(state=tmp_path / "state")
    ws.state.mkdir()
    assert Harness2UpstreamEvolver()._claude(ws, "pi") == str((pi_stand.folder / "pi").resolve())


def test_a_pi_acting_agents_trajectory_is_read_by_the_bench(tmp_path):
    pytest.importorskip("harness2")
    import shutil
    from types import SimpleNamespace as NS
    from hermit.agent.evolver.methods.harness2_upstream.bench import FrameworkBench
    (tmp_path / "log").mkdir()
    shutil.copy(STREAMS / "success.jsonl", tmp_path / "log" / "agent-pi.jsonl")
    text = FrameworkBench.trajectory(NS(_run=lambda run_dir: {}), tmp_path, obs_trunc=1000)
    assert "[ACTION] bash: echo hi && exit 3" in text


def test_a_pi_trajectory_reads_in_upstreams_grammar():
    from hermit.agent.evolver.methods.harness2_upstream.render import render_pi_events
    text = render_pi_events(STREAMS / "success.jsonl", obs_trunc=1000, max_chars=100000)
    assert "[ACTION] bash: echo hi && exit 3" in text and "[OBSERVATION ERROR] hi" in text
    assert '"tool": "echo"' in text and text.rstrip().endswith("[ASSISTANT] the answer")
