"""Resolve and verify the OpenAgents source used by an AHE-Hermit run."""

from __future__ import annotations

from dataclasses import dataclass
import itertools
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


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
        if _git(destination, "status", "--porcelain", "--untracked-files=all"):   # whatever Git's settings say
            raise ValueError(f"OpenAgents checkout has local changes: {destination}")
        _git(destination, "fetch", "--no-tags", "origin", spec.commit)   # a pin already here: Git asks no remote
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Made in a folder of this call's own beside it, checked out, then renamed whole: a stop leaves no clone
        # without its files, which a retry would refuse as one with local changes.
        scratch = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", suffix=".cloning", dir=destination.parent))
        try:
            subprocess.run(["git", "clone", "--no-checkout", spec.repository, str(scratch / "checkout")], check=True)
            _git(scratch / "checkout", "fetch", "--no-tags", "origin", spec.commit)
            _git(scratch / "checkout", "checkout", "--detach", spec.commit)
            os.replace(scratch / "checkout", destination)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
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
                   check=True, capture_output=True, text=True, env=own_environment(checkout))
    program = Path(checkout) / ".venv/bin/openagents"
    if not program.exists():
        raise RuntimeError(f"the OpenAgents environment has no openagents program: {program}")
    return program


def own_venv(checkout: Path) -> Path:
    """The checkout's own `.venv`, refused when it leads out of the checkout (a link to a shared environment): uv would
    make that environment OpenAgents', its other packages removed, and the worker would be shown it."""
    root = Path(checkout).resolve()
    if not (root / ".venv").resolve().is_relative_to(root):
        raise RuntimeError(f"the OpenAgents checkout's .venv leads out of it, to {(root / '.venv').resolve()}: it is "
                           "the checkout's own environment (uv syncs it, the worker is shown it)")
    return root / ".venv"


def own_environment(checkout: Path) -> dict:
    """This process's environment with uv pointed at the checkout's own `.venv` (own_venv): an inherited
    UV_PROJECT_ENVIRONMENT (an active environment of the operator's, say) would be synced to OpenAgents' lock, its
    other packages removed."""
    return {**os.environ, "UV_PROJECT_ENVIRONMENT": str(own_venv(checkout))}


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


def apart(folders: dict, cohort, shown=()) -> list[str]:
    """What is wrong with the delivery's folders, each granted to one process (the worker its clone and home, this
    machine installs and runs the OpenAgents checkout, the controller writes its state and the delivery clone): two
    that overlap, one that holds the cohort (its assignments.json: the personas), or a folder the worker is shown
    besides that is in the cohort, holds it or overlaps one of them (the controller's state keeps the users' native
    records). Inside the cohort, apart, is where bootstrap.py and prepare.py put them."""
    def overlap(one, other):
        return one == other or one in other.parents or other in one.parents
    named = {name: Path(path).resolve() for name, path in folders.items()}
    cohort = Path(cohort).resolve()
    problems = [f"{a} and {b} overlap ({named[a]}, {named[b]})" for a, b in itertools.combinations(named, 2)
                if overlap(named[a], named[b])]
    for name, path in named.items():
        if path == cohort or path in cohort.parents:
            problems.append(f"{name} holds the cohort's personas ({cohort})")
    for path in (Path(p).resolve() for p in shown):
        if path == cohort or path in cohort.parents or cohort in path.parents:
            problems.append(f"a folder the worker is shown ({path}) is in or holds the cohort ({cohort}): its "
                            "personas, its users' records")
        problems += [f"a folder the worker is shown ({path}) overlaps {name} ({other})"
                     for name, other in named.items() if overlap(path, other)]
    return problems
