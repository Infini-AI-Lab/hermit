"""Harness²'s editors (its Claude channel) on the Claude subscription's login (docs/guides/claude-subscription.md), against the stand-in `claude`
(tests/claude_stand.py). The shared login path is tested in tests/core/test_claude_subscription.py."""
import json  # noqa: F401
from pathlib import Path  # noqa: F401

import pytest

from hermit.agent.base import login as subscription  # noqa: F401
from hermit.agent.base.interface import LoginFailed  # noqa: F401
from claude_stand import TOKEN, roles_ctx, stand  # noqa: F401 (stand: the fixture)


def test_harness2s_claude_channel_signs_in_with_the_login(stand, tmp_path):
    pytest.importorskip("harness2")
    from harness2.core.spec import ChannelSpec
    from hermit.agent.evolver.methods.harness2_upstream.channel import FrameworkProposer
    params = {"login": "claude", "model": "claude-haiku-5-5", "plan_dir": str(tmp_path / "plan")}
    proposer = FrameworkProposer(spec=ChannelSpec(kind="claude_cli", model="claude-haiku-5-5"), params=params)
    cwd = tmp_path / "session"
    cwd.mkdir()
    stand.play({"case": "window_warning", "text": "edited"})
    text, cost, problem = proposer._claude_cli(cwd, "the mission", "be brief", wall=60)
    saw = stand.seen()[0]
    assert (text, problem) == ("edited", "") and saw["env"][subscription.TOKEN_ENV] == TOKEN
    assert saw["args"][saw["args"].index("--model") + 1] == "claude-haiku-5-5" and "ANTHROPIC_API_KEY" not in saw["env"]
    assert saw["args"][-2] == "--settings" and cost == 0.0
    # on the login the whole stream, not the one result object: the plan sees the session's windows
    assert saw["args"][saw["args"].index("--output-format") + 1] == "stream-json"
    assert json.loads((tmp_path / "plan" / "plan.json").read_text())["windows"]
    stand.play("api_key_won")                                  # and what paid for it: another payer ends the run
    with pytest.raises(LoginFailed):
        proposer._claude_cli(cwd, "the mission", "", wall=60)
    stand.play("not_logged_in")
    with pytest.raises(LoginFailed):
        proposer._claude_cli(cwd, "the mission", "", wall=60)
    (tmp_path / "plan" / "PAUSE").touch()                      # a session the plan never let start is no reply
    held = FrameworkProposer(spec=ChannelSpec(kind="claude_cli", model="claude-haiku-5-5"),
                             params={**params, "plan_max_wait_s": 0})
    assert held._claude_cli(cwd, "the mission", "", wall=60)[2].startswith("not run: ")
