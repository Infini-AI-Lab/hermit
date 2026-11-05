"""The fixtures every test under tests/ gets. The groups (core/, methods/<name>/) and the options (--changed,
--integration) are the repository root's conftest.py's (tests/README.md)."""
import pytest


@pytest.fixture(autouse=True)
def shared_login(tmp_path_factory, monkeypatch):
    """The shared copy of a ChatGPT login (hermit.agent.base.login.ROOT) in a directory of the test's own, never the
    user's ~/.cache."""
    from hermit.agent.base import login
    root = tmp_path_factory.mktemp("codex-login")
    monkeypatch.setattr(login, "ROOT", root)
    monkeypatch.setattr(login, "PLAN", root / "plan")         # and its plan
    monkeypatch.setenv("HERMIT_HOMES", str(root / "homes"))   # and the CLI homes, never the user's ~/.cache
    return root

