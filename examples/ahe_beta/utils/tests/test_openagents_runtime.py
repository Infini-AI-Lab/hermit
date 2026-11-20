"""The AHE example materializes the pinned OpenAgents checkout."""

import json
from pathlib import Path
import subprocess

from examples.ahe_beta.openagents_runtime import (
    OpenAgentsSpec,
    ensure_checkout,
    from_compatibility,
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


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
