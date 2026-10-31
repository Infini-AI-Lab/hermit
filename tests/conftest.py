import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest


@pytest.fixture(autouse=True)
def shared_login(tmp_path_factory, monkeypatch):
    """The shared copy of a ChatGPT login (hermit.agent.base.login.ROOT) in a directory of the test's own, never the
    user's ~/.cache."""
    from hermit.agent.base import login
    root = tmp_path_factory.mktemp("codex-login")
    monkeypatch.setattr(login, "ROOT", root)
    return root
