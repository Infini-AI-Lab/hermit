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
from hermit.agent.evolver.methods.coevoskills_adapter.packaging import TEMPLATE
from hermit.hosts.inputs import adapter_files, adapter_folder

RECEIPT = "coevoskills-integrity.json"


def _files(root: Path) -> dict[str, tuple[str, int]]:
    """File contents and executable bits, excluding the bytecode caches created by running the skill."""
    out = {}
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"missing skill directory: {root}")
    for path in root.rglob("*"):
        if "__pycache__" in path.relative_to(root).parts or path.suffix == ".pyc":
            continue
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError(f"{path} is a link or a special file")
        if path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            out[path.relative_to(root).as_posix()] = (digest, path.stat().st_mode & 0o111)
    return out


def _fingerprint(adapter: Path) -> str:
    """Bind the receipt to this candidate's context and skill tree."""
    context = adapter / "ADAPTER.md"
    data = {"context": hashlib.sha256(context.read_bytes()).hexdigest() if context.is_file() else None,
            "skills": _files(adapter / "skills")}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def _verify_copies(adapter: Path, work: Path, record: dict) -> None:
    """Check both deployments while the acting pipeline still owns the workspace and its adapter snapshot."""
    manifest = load_manifest(adapter)
    files = adapter_files(adapter, work, manifest, record)
    if record.get("files") != files:
        raise ValueError("the deployment record differs from the candidate")
    for relative in files:                      # a redeployment tolerates a missing file; the check does not
        if not (work / relative).is_file():
            raise ValueError(f"{relative} is missing from the deployment")
    source = adapter / manifest["skills"]
    expected = sorted(path.parent.name for path in source.glob("*/SKILL.md") if path.is_file())
    if (record.get("skills") or []) != expected or (expected and not record.get("skills_dir")):
        raise ValueError("the deployment did not install the candidate's native skills")
    if not expected:
        return
    copied = adapter_folder(work, manifest) / manifest["skills"]
    native = Path(record["skills_dir"])
    for name in expected:
        candidate = _files(source / name)
        if _files(copied / name) != candidate or _files(native / name) != candidate:
            raise ValueError(f"the installed skill {name} differs from the candidate")


def record_deployment(workspace: Path, log: Path) -> None:
    """Acting-side collect hook. A failed check is recorded for the oracle; other rollouts keep their answer."""
    record = json.loads((log / "deployment.json").read_text())
    if record.get("adapter_id") != TEMPLATE:
        return
    adapter = Path(record["adapter"])
    if not (adapter / "skills").is_dir() or not any((adapter / "skills").iterdir()):
        return
    try:
        _verify_copies(adapter, workspace, record)
        receipt = {"candidate_sha256": _fingerprint(adapter)}
    except (OSError, ValueError) as exc:
        receipt = {"error": str(exc)}
    (log / RECEIPT).write_text(json.dumps(receipt))


def verify_deployment(adapter: Path, log: Path) -> None:
    """Oracle-side verification uses only the checked receipt in the Trainer's public copy of the log."""
    if not (log / RECEIPT).is_file():
        raise ValueError(f"missing {RECEIPT}; the acting collect hook must call record_deployment")
    receipt = json.loads((log / RECEIPT).read_text())
    if receipt.get("error"):
        raise ValueError(f"the candidate changed during the run: {receipt['error']}")
    if receipt.get("candidate_sha256") != _fingerprint(adapter):
        raise ValueError("the receipt belongs to a different candidate")
