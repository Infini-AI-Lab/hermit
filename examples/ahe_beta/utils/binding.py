"""Bind tested adapter bytes to the delivery repository without equating Git histories."""

import hashlib
import json
from pathlib import Path
import subprocess

from hermit.adapter.manifest import files_of


def fingerprint(root):
    root = Path(root)
    digest = hashlib.sha256()
    for relative in files_of(root):
        # Hermit owns the card/history, not the learning method's delivery repository.
        if relative == ".hermit/CARD.md":
            continue
        path = root / relative
        if path.is_symlink():
            raise ValueError("adapter binding does not accept symlinks")
        digest.update(relative.encode() + b"\0")
        digest.update(str(path.stat().st_mode & 0o111).encode() + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def read(path, adapter):
    value = json.loads(Path(path).read_text())
    if value["adapter_sha256"] != fingerprint(adapter):
        raise ValueError("tested adapter differs from the frozen delivery binding")
    return value


def write(path, *, adapter, repo, run_id, round_no):
    if git(repo, "status", "--porcelain"):
        raise ValueError("delivery checkout must be clean")
    if fingerprint(repo) != fingerprint(adapter):
        raise ValueError("delivery checkout and Hermit adapter have different contents")
    value = {
        "run_id": run_id,
        "round": round_no,
        "harness_sha": git(repo, "rev-parse", "HEAD"),
        "adapter_sha256": fingerprint(adapter),
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)
    return value
