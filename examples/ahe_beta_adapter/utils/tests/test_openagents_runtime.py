"""The AHE example materializes the pinned OpenAgents checkout."""

import json
from pathlib import Path
import subprocess

from examples.ahe_beta_adapter.openagents_runtime import (
    OpenAgentsSpec,
    ensure_checkout,
    from_compatibility,
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def test_the_openagents_environment_is_the_checkouts_whatever_uv_is_pointed_at(tmp_path, monkeypatch):
    """An inherited UV_PROJECT_ENVIRONMENT (the operator's active environment, say) was the one `uv sync` made exact to
    OpenAgents' lock, its other packages removed."""
    import examples.ahe_beta_adapter.openagents_runtime as runtime
    checkout = tmp_path / "openagents"
    checkout.mkdir()
    uv = tmp_path / "uv"
    uv.write_text("#!/bin/sh\nprintf '%s' \"$UV_PROJECT_ENVIRONMENT\" > \"$0.target\"\n"
                  f"mkdir -p {checkout}/.venv/bin && touch {checkout}/.venv/bin/openagents\n")
    uv.chmod(0o755)
    monkeypatch.setattr(runtime.shutil, "which", lambda name: str(uv) if name == "uv" else None)
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", str(tmp_path / "the-operators-env"))
    runtime.ensure_environment(checkout)
    assert (tmp_path / "uv.target").read_text() == str(checkout.resolve() / ".venv")


def test_an_openagents_environment_linked_out_of_its_checkout_is_refused(tmp_path, monkeypatch):
    """A checkout's `.venv` linked to a shared environment: `uv sync` made that one exact to OpenAgents' lock."""
    import pytest
    import examples.ahe_beta_adapter.openagents_runtime as runtime
    checkout = tmp_path / "openagents"
    checkout.mkdir()
    (tmp_path / "shared").mkdir()
    (checkout / ".venv").symlink_to(tmp_path / "shared")
    uv = tmp_path / "uv"
    uv.write_text("#!/bin/sh\ntouch \"$0.ran\"\n")
    uv.chmod(0o755)
    monkeypatch.setattr(runtime.shutil, "which", lambda name: str(uv) if name == "uv" else None)
    with pytest.raises(RuntimeError, match="leads out of it"):
        runtime.ensure_environment(checkout)
    assert not (tmp_path / "uv.ran").exists()


def test_an_openagents_clone_stopped_halfway_is_not_left_in_place(tmp_path):
    """A clone whose fetch of the pin failed (or a stop) was left without its files at the destination, which every
    retry refused as a checkout with local changes."""
    import pytest
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-q")
    (source / "a.py").write_text("x = 1\n")
    _git(source, "add", "-A")
    _git(source, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "a")
    with pytest.raises(subprocess.CalledProcessError):                         # a commit the source does not have
        ensure_checkout(OpenAgentsSpec(str(source), "0" * 40), tmp_path / "openagents")
    assert not (tmp_path / "openagents").exists() and not list(tmp_path.glob(".openagents.*"))
    pin = _git(source, "rev-parse", "HEAD")
    assert ensure_checkout(OpenAgentsSpec(str(source), pin), tmp_path / "openagents") == \
        (tmp_path / "openagents").resolve()


def test_compatibility_requires_an_immutable_openagents_commit(tmp_path):
    root = tmp_path / "ahe"
    (root / "backend").mkdir(parents=True)
    (root / "backend/compatibility.json").write_text(
        json.dumps({"openagents": {"repository": "https://example.invalid/OpenAgents"}})
    )
    try:
        from_compatibility(root)
    except ValueError as exc:
        assert "full 40-character commit" in str(exc)
    else:
        raise AssertionError("an OpenAgents repository without a commit must be rejected")


def test_compatibility_uses_the_existing_execution_reader_pin(tmp_path):
    root = tmp_path / "ahe"
    (root / "backend").mkdir(parents=True)
    (root / "backend/compatibility.json").write_text(
        json.dumps(
            {
                "openagents": {"repository": "https://example.invalid/OpenAgents"},
                "execution_evidence": {"reader_candidate_commit": "a" * 40},
            }
        )
    )
    spec = from_compatibility(root)
    assert spec is not None and spec.commit == "a" * 40


def test_ensure_checkout_uses_the_requested_commit(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    for args in (
        ["init", "-b", "main"],
        ["config", "user.name", "Test"],
        ["config", "user.email", "test@example.invalid"],
    ):
        subprocess.run(["git", "-C", str(source), *args], check=True, capture_output=True)
    (source / "README.md").write_text("one\n")
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-m", "one"], check=True, capture_output=True)
    first = _git(source, "rev-parse", "HEAD")
    (source / "README.md").write_text("two\n")
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-m", "two"], check=True, capture_output=True)
    second = _git(source, "rev-parse", "HEAD")

    checkout = ensure_checkout(OpenAgentsSpec(str(source), first), tmp_path / "checkout")
    assert _git(checkout, "rev-parse", "HEAD") == first
    ensure_checkout(OpenAgentsSpec(str(source), second), checkout)
    assert _git(checkout, "rev-parse", "HEAD") == second
    source.rename(tmp_path / "gone")                                          # the remote out of reach: a checkout at
    assert ensure_checkout(OpenAgentsSpec(str(source), second), checkout) == checkout   # its pin needs none of it


def test_the_worker_environment_has_the_tools_its_reviews_run(tmp_path, monkeypatch):
    """AHE-Beta's worker installs OpenAgents' development extra (its reviews run ruff and pytest)."""
    import shutil
    import subprocess
    from examples.ahe_beta_adapter.openagents_runtime import ensure_environment
    seen = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: seen.append(argv))
    monkeypatch.setattr(shutil, "which", lambda name, **kw: "/usr/bin/uv")    # not this machine's
    (tmp_path / ".venv/bin").mkdir(parents=True)
    (tmp_path / ".venv/bin/openagents").write_text("")
    ensure_environment(tmp_path)
    assert seen[0][seen[0].index("sync"):][:6] == ["sync", "--locked", "--extra", "nexau", "--extra", "dev"]
