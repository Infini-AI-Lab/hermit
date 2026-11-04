"""The oracle's run is a test of the candidate only if the candidate ran as it was: upstream's evaluation contract keeps
the Skill immutable during the run, and an acting agent can write its workspace (Codex's sandbox lets it). After the
run, the deployed copies are checked against the candidate: the adapter's copy (`hermit/<id>/`) with the framework's
own deployment check (hermit.hosts.inputs.adapter_files: the record's digests, no link, no replaced file), and the
host's native skills (`skills_dir` of the record, Codex's `.codex/skills/`) file by file. A change fails the check:
an error of the oracle, never a pass.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from hermit.adapter.loader import load_manifest
from hermit.hosts.inputs import adapter_files


def _files(root: Path) -> dict[str, str]:
    """{relative path: sha256} of the files under `root`, Python's bytecode caches left out (running a skill's script
    writes them); a link or anything but a file is refused."""
    out = {}
    for path in sorted(root.rglob("*")) if root.is_dir() else []:
        if "__pycache__" in path.relative_to(root).parts or path.suffix == ".pyc":
            continue
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError(f"{path} is a link or a special file")
        if path.is_file():
            out[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def verify_deployment(adapter: Path, log: Path) -> None:
    """Raises ValueError unless the run's deployed copies of `adapter` are still the adapter's files."""
    adapter, log = Path(adapter), Path(log)
    work = log.parent / "work"
    record = json.loads((log / "deployment.json").read_text())
    files = adapter_files(adapter, work, load_manifest(adapter), record)
    if record.get("files") != files:
        raise ValueError("the deployment record differs from the candidate")
    for relative in files:                      # a redeployment tolerates a missing file; the check does not
        if not (work / relative).is_file():
            raise ValueError(f"{relative} is missing from the deployment")
    native = Path(record["skills_dir"]) if record.get("skills_dir") else None
    for name in record.get("skills") or []:
        if native is None or _files(native / name) != _files(adapter / "skills" / name):
            raise ValueError(f"the installed skill {name} differs from the candidate")
