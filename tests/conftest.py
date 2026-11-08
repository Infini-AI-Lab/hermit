"""The fixtures every test under tests/ gets. The groups (core/, methods/<name>/) and the options (--changed,
--integration) are the repository root's conftest.py's (tests/README.md)."""
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def shared_login(tmp_path_factory, monkeypatch):
    """The shared copy of a ChatGPT login (hermit.agent.base.login.ROOT), both logins' plans and the CLI homes in a
    directory of the test's own, and no sign-in or token of the user's: never ~/.cache, ~/.codex or ~/.claude."""
    from hermit.agent.base import login
    root = tmp_path_factory.mktemp("codex-login")
    monkeypatch.setattr(login, "ROOT", root)
    monkeypatch.setattr(login, "PLAN", root / "plan")         # and its plan
    monkeypatch.setattr(login, "CLAUDE_PLAN", root / "claude-plan")   # and a Claude subscription's
    monkeypatch.delenv(login.TOKEN_ENV, raising=False)        # the user's own token reaches no test's CLI
    monkeypatch.setenv("HERMIT_HOMES", str(root / "homes"))   # and the CLI homes, never the user's ~/.cache
    # and no session copies the user's own sign-in (~/.codex/auth.json, ~/.claude/.credentials.json): a test that
    # copies one names its own (CODEX_HOME, or HOME with a .claude of its own)
    from hermit.agent.base import cli
    monkeypatch.setenv("CODEX_HOME", str(root / "user-codex"))
    real_home = Path.home()
    monkeypatch.setattr(cli.Claude, "user_home", classmethod(
        lambda c: root / "user-claude" if Path.home() == real_home else Path.home() / c.user_folder))
    return root

