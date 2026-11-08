"""EvoSafeHarness's per-role sessions on the Claude subscription's login (docs/guides/claude-subscription.md), against the stand-in `claude`
(tests/claude_stand.py). The shared login path is tested in tests/core/test_claude_subscription.py."""
import json  # noqa: F401
from pathlib import Path  # noqa: F401

import pytest

from hermit.agent.base import login as subscription  # noqa: F401
from hermit.agent.base.interface import LoginFailed  # noqa: F401
from claude_stand import TOKEN, roles_ctx, stand  # noqa: F401 (stand: the fixture)


def test_evosafes_roles_read_a_lost_sign_in_and_a_refusal_from_the_base_agent(stand, tmp_path):
    from types import SimpleNamespace as NS
    from hermit.agent.evolver.methods.evosafe import claude_code, cli_roles
    assert "usage_credits" in cli_roles.CLI_SETTINGS
    ws = NS(scratch=tmp_path / "scratch", output=tmp_path / "out")
    ws.scratch.mkdir()
    ws.output.mkdir()
    ctx = NS(workspace=ws, params={"login": "claude", "model": "m"}, sandbox=None, cancel=None, tools={})
    stand.play("not_logged_in")                                # past EvoSafe's own error handling: the run ends
    with pytest.raises(LoginFailed, match="an EvoSafe trial-001-designer session"):
        claude_code.call_role(ctx, [{"role": "user", "content": "x"}], {"type": "object", "properties": {}},
                              "trial-001-designer")
    stand.play("model_refusal")                                # Claude Code's refusal: a refusal, never retried
    response, _, error = claude_code.call_role(ctx, [{"role": "user", "content": "x"}],
                                               {"type": "object", "properties": {}}, "trial-002-designer")
    assert response is None and error.startswith("SAFETY_REVIEW_REFUSAL")


def test_evosafes_roles_keep_an_unlimited_plan_wait(tmp_path, monkeypatch):
    """plan_max_wait_s: null is no limit, as for every other learner: it reaches the role's session."""
    from types import SimpleNamespace as NS
    from hermit.agent.evolver.methods.evosafe import cli_roles
    seen = {}

    class Agent:
        def __init__(self, settings):
            seen.update(settings)

        def run(self, *args, **kwargs):
            raise KeyboardInterrupt                  # the settings are all this test needs
    monkeypatch.setattr(cli_roles, "make", lambda backend, settings: Agent(settings))
    ws = NS(scratch=tmp_path / "scratch", output=tmp_path / "out")
    ws.scratch.mkdir()
    ws.output.mkdir()
    ctx = NS(workspace=ws, params={"login": "claude", "model": "m", "plan_max_wait_s": None}, sandbox=None,
             cancel=None, tools={})
    with pytest.raises(KeyboardInterrupt):
        cli_roles.call_role(ctx, [{"role": "user", "content": "x"}], {"type": "object", "properties": {}},
                            "trial-001-designer", "claude")
    assert "plan_max_wait_s" in seen and seen["plan_max_wait_s"] is None
