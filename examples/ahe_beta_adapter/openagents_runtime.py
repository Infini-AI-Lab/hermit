"""Resolve and verify the OpenAgents source used by an AHE-Hermit run."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import shutil
import subprocess


_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class OpenAgentsSpec:
    repository: str
    commit: str


def from_compatibility(ahe_root: Path, *, repository: str | None = None,
                       commit: str | None = None) -> OpenAgentsSpec | None:
    """Read the OpenAgents pin from AHE's compatibility manifest.

    A test fixture may omit the manifest entirely. A real AHE checkout may carry
    the OpenAgents pin directly under ``openagents.commit`` or under the existing
    execution/observation reader pin used by AHE's compatibility manifest.
    """
    manifest = Path(ahe_root) / "backend/compatibility.json"
    if repository is None and commit is None and not manifest.is_file():
        return None
    data = json.loads(manifest.read_text(encoding="utf-8")) if manifest.is_file() else {}
    entry = data.get("openagents") or {}
    repository = repository or entry.get("repository")
    commit = commit or entry.get("commit")
    if not commit:
        for section in ("execution_evidence", "observation_transport"):
            reader = data.get(section) or {}
            commit = reader.get("reader_release_commit") or reader.get("reader_candidate_commit")
            if commit:
                break
    if not repository:
        raise ValueError("OpenAgents repository is missing from compatibility.json")
    if not commit or not _FULL_SHA.fullmatch(str(commit)):
        raise ValueError("OpenAgents compatibility entry must pin a full 40-character commit")
    return OpenAgentsSpec(str(repository), str(commit))


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=check,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def ensure_checkout(spec: OpenAgentsSpec, destination: Path) -> Path:
    """Materialize exactly ``spec.commit`` in an isolated checkout."""
    destination = Path(destination).resolve()
    if destination.exists():
        if not (destination / ".git").exists():
            raise ValueError(f"OpenAgents checkout is not a Git repository: {destination}")
        if _git(destination, "status", "--porcelain"):
            raise ValueError(f"OpenAgents checkout has local changes: {destination}")
        _git(destination, "fetch", "--no-tags", "origin", spec.commit)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", "--no-checkout", spec.repository, str(destination)],
            check=True,
        )
        _git(destination, "fetch", "--no-tags", "origin", spec.commit)
    _git(destination, "checkout", "--detach", spec.commit)
    actual = _git(destination, "rev-parse", "HEAD")
    if actual != spec.commit:
        raise RuntimeError(
            f"OpenAgents checkout resolved to {actual}, expected {spec.commit}"
        )
    return destination


def verify_checkout(manifest: Path) -> dict:
    """Verify a prepared manifest and return its normalized data."""
    data = json.loads(Path(manifest).read_text(encoding="utf-8"))
    spec = OpenAgentsSpec(str(data["repository"]), str(data["commit"]))
    checkout = Path(data["checkout"]).resolve()
    ensure_checkout(spec, checkout)
    return {"repository": spec.repository, "commit": spec.commit, "checkout": str(checkout)}


def write_manifest(path: Path, spec: OpenAgentsSpec, checkout: Path) -> dict:
    data = {
        "repository": spec.repository,
        "commit": spec.commit,
        "checkout": str(Path(checkout).resolve()),
        "runner": "uv run --locked --extra nexau --project <checkout> openagents",
    }
    path = Path(path)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return data


def ensure_environment(checkout: Path) -> Path:
    """The pinned checkout's own environment (`.venv`, its lock, the nexau extra and the development extra, whose
    tools its reviews run, as AHE-Beta's worker installs them), made on this machine, where the network is: the
    delivery Evolver runs the worker from it in the run's sandbox, which installs nothing."""
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("uv is required to prepare the pinned OpenAgents checkout")
    subprocess.run([uv, "sync", "--locked", "--extra", "nexau", "--extra", "dev", "--project", str(checkout)],
                   check=True, capture_output=True, text=True)
    program = Path(checkout) / ".venv/bin/openagents"
    if not program.exists():
        raise RuntimeError(f"the OpenAgents environment has no openagents program: {program}")
    return program


def command(manifest: Path, args: list[str]) -> list[str]:
    """Build the pinned source command used by the launcher."""
    data = verify_checkout(manifest)
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("uv is required to run the pinned OpenAgents checkout")
    return [
        uv,
        "run",
        "--locked",
        "--extra",
        "nexau",
        "--project",
        data["checkout"],
        "openagents",
        *args,
    ]
