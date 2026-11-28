"""The example's tests run Git with a configuration of their own: none of the operator's ignores, helpers or
identity (a test that needs a rule sets its own GIT_CONFIG_GLOBAL)."""

import pytest


@pytest.fixture(autouse=True)
def own_git_configuration(tmp_path_factory, monkeypatch):
    config = tmp_path_factory.getbasetemp() / "gitconfig"
    if not config.exists():                              # its excludes file empty: not ~/.config/git/ignore either
        (tmp_path_factory.getbasetemp() / "gitignore").write_text("")
        config.write_text(f"[user]\n\tname = t\n\temail = t@t\n[core]\n\texcludesFile = "
                          f"{tmp_path_factory.getbasetemp() / 'gitignore'}\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
