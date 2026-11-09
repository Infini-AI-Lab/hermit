"""The RRSI Adapter's roles on the Claude subscription's login (docs/guides/claude-subscription.md), against the stand-in `claude`
(tests/claude_stand.py). The shared login path is tested in tests/core/test_claude_subscription.py."""
import json  # noqa: F401
from pathlib import Path  # noqa: F401

import pytest

from hermit.agent.base import login as subscription  # noqa: F401
from hermit.agent.base.interface import LoginFailed  # noqa: F401
from claude_stand import TOKEN, roles_ctx, stand  # noqa: F401 (stand: the fixture)


def test_rrsi_roles_follow_the_proposer_onto_claude_code(stand, tmp_path):
    """The analyst and the critic answer from their prompt with no tools; a digester reads its traces with the tools that
    only read; a binding's provider settings are Claude Code's; a lost sign-in ends the run."""
    pytest.importorskip("rrsi")
    from hermit.agent.evolver.methods.rrsi_adapter.evolver import RRSIAdapterEvolver
    from hermit.agent.evolver.methods.rrsi_adapter import roles as R
    ctx = roles_ctx(tmp_path)
    evolver = RRSIAdapterEvolver()
    evolver.directory = Path(R.__file__).parent
    roles = evolver._roles(ctx)
    assert roles.backend == "claude" and roles.settings["login"] == "claude"
    stand.play({"case": "reply", "text": "the analysis"})
    assert roles.generate("look at this", system="You are the analyst.") == "the analysis"
    analyst = stand.seen()[0]
    assert "--restricted" in analyst["args"] and analyst["args"][analyst["args"].index("--tools") + 1] == ""
    assert analyst["system"].startswith("You are the analyst.") and analyst["prompt"] == "look at this"
    assert not any("developer_instructions" in a for a in analyst["args"])
    assert roles.usage and roles.usage[0].cost_usd is None
    roles.traces = tmp_path / "traces"
    roles.traces.mkdir()
    stand.play({"case": "reply", "text": "digest"})
    assert roles.generate("read it", system="You are a trajectory digester.") == "digest"
    digester = stand.seen()[0]
    assert digester["args"][digester["args"].index("--tools") + 1] == "Read,Grep,Glob" and "--restricted" not in digester["args"]
    assert digester["args"][digester["args"].index("--append-system-prompt") + 1].startswith("You are a trajectory digester")
    assert digester["cwd"] == str(roles.traces.resolve())
    stand.play("not_logged_in")
    with pytest.raises(LoginFailed):
        roles.generate("again", system="You are the critic.")


def test_rrsi_roles_on_claude_code_each_run_in_a_view_of_their_own(tmp_path, monkeypatch):
    """With the run's sandbox on, a role's Claude Code session runs in a view of it that shows its own call directory
    (a digester's, the round's traces too): the view is the session's sandbox through `instructed`'s configured copy."""
    pytest.importorskip("rrsi")
    from types import SimpleNamespace as NS
    from hermit.agent.base.cli import Claude
    from hermit.agent.base.interface import Result
    from hermit.agent.evolver.methods.rrsi_adapter import roles as R
    seen = []

    def run(self, prompt, *, workspace=None, tools=(), limits=None, session=None, roles=()):
        seen.append((self.settings, workspace))
        return Result(reply="ok", exit_code=0, extra={"served": True, "unavailable": False, "started": True,
                                                      "cancelled": False, "output_tail": ""})
    monkeypatch.setattr(Claude, "run", run)

    class Box:                                    # the run's sandbox, on: each session gets a view of it
        enabled = True

        def view(self, **kw):
            return NS(enabled=True, readable=list(kw.get("readable") or []), writable=list(kw.get("writable") or []),
                      close=lambda: None)
    roles = R.Roles(NS(cancel=None, sandbox=Box()), {"model": "m", "login": "claude"}, tmp_path / "roles",
                    backend="claude")
    roles.traces = tmp_path / "traces"
    roles.traces.mkdir()
    assert roles.generate("p", system="You are the analyst.") == "ok"
    assert roles.generate("p", system="You are a trajectory digester.") == "ok"
    (analyst, analyst_cwd), (digester, digester_cwd) = seen
    assert analyst["sandbox"].writable == [tmp_path / "roles" / "00001"] and analyst["sandbox"].readable == []
    assert analyst["text_only"] and analyst["system_prompt"] == "You are the analyst."
    assert analyst_cwd == tmp_path / "roles" / "00001" / "work"
    assert digester["sandbox"].writable == [tmp_path / "roles" / "00002"]
    assert digester["sandbox"].readable == [roles.traces] and digester_cwd == roles.traces
    assert digester["allowed_tools"] == R.DIGESTER_TOOLS and "text_only" not in digester
    assert digester["append_system_prompt"] == "You are a trajectory digester."


def test_rrsi_roles_on_a_binding_take_claudes_provider_settings(stand, tmp_path):
    pytest.importorskip("rrsi")
    from hermit.agent.evolver.methods.rrsi_adapter.evolver import RRSIAdapterEvolver
    binding = {"backend": "b", "base_url": "http://127.0.0.1:9/v1", "model": "their-model", "api_key_env": None,
               "key_required": False, "apis": ["messages"]}
    # the proposer's own provider settings (a bound backend's context window, say) are not the roles' model's
    ctx = roles_ctx(tmp_path, {"roles": {"binding": binding},
                               "env": {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "32768", "KEEP_ME": "1"}})
    roles = RRSIAdapterEvolver()._roles(ctx)
    assert roles.settings["model"] == "their-model" and "login" not in roles.settings
    assert roles.settings["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9"
    assert "CLAUDE_CODE_MAX_CONTEXT_TOKENS" not in roles.settings["env"] and roles.settings["env"]["KEEP_ME"] == "1"
    assert not any("model_provider" in str(a) for a in roles.settings.get("args") or [])


def test_an_rrsi_roles_failed_sign_in_outranks_the_stop_that_followed(stand, tmp_path, monkeypatch):
    pytest.importorskip("rrsi")
    import threading
    from hermit.agent.base.interface import Result
    from hermit.agent.evolver.methods.rrsi_adapter.evolver import RRSIAdapterEvolver
    from hermit.agent.evolver.methods.rrsi_adapter import roles as R
    ctx = roles_ctx(tmp_path)
    ctx.cancel = threading.Event()
    evolver = RRSIAdapterEvolver()
    evolver.directory = Path(R.__file__).parent
    roles = evolver._roles(ctx)

    def hung(system, prompt):
        ctx.cancel.set()
        return Result(reply="", extra={"login_failed": True, "cancelled": True, "failure": "Not logged in"})
    monkeypatch.setattr(roles, "_session", hung)
    with pytest.raises(LoginFailed, match="Not logged in"):
        roles.generate("again", system="You are the critic.")
