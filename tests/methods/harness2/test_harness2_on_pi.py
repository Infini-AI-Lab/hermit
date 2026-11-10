"""Harness²'s editors on Pi (`evolver.base: pi`), against the stand-in `pi` (tests/pi_stand.py), and a Pi acting agent's
trajectory in upstream's grammar."""
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
    assert call["args"][call["args"].index("--thinking") + 1] == "xhigh"         # Claude Code's max is Pi's xhigh


def test_a_pi_session_that_failed_is_no_reply(pi_stand, tmp_path):  # noqa: F811
    prop = proposer()
    cwd = tmp_path / "session"
    cwd.mkdir()
    pi_stand.play("failure")
    text, cost, problem = prop._claude_cli(cwd, "the mission", "", wall=60)
    assert text == "" and cost == 0.0 and problem.startswith("rc=0") and prop.served == 0


def test_a_pi_trajectory_reads_in_upstreams_grammar():
    from hermit.agent.evolver.methods.harness2_upstream.render import render_pi_events
    text = render_pi_events(STREAMS / "success.jsonl", obs_trunc=1000, max_chars=100000)
    assert "[ACTION] bash: echo hi && exit 3" in text and "[OBSERVATION ERROR] hi" in text
    assert '"tool": "echo"' in text and text.rstrip().endswith("[ASSISTANT] the answer")
