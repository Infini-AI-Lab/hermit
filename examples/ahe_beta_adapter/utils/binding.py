"""Bind tested adapter bytes to the delivery repository without equating Git histories."""

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

from hermit.adapter.manifest import files_of, read_manifest, structure_of


def fingerprint(root):
    root = Path(root)
    manifest = root / ".hermit/adapter.yaml"
    structure = structure_of(read_manifest(root)) if manifest.is_file() else None
    digest = hashlib.sha256()
    for relative in files_of(root):
        # Hermit owns the card/history, not the learning method's delivery repository.
        if relative == ".hermit/CARD.md":
            continue
        if structure is not None and not (
            structure.is_fixed(relative) or structure.learnable_for(relative) is not None
        ):
            continue
        path = root / relative
        if path.is_symlink():
            raise ValueError("adapter binding does not accept symlinks")
        digest.update(relative.encode() + b"\0")
        digest.update(b"1\0" if path.stat().st_mode & 0o100 else b"0\0")   # the one executable bit Git keeps
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


def ignored(root) -> list[str]:
    """The files under `root` this machine's Git ignores (a global excludes file): Hermit makes the adapter's tree with
    `git add -A`, which leaves them out, so the adapter that runs would not be these files."""
    files = [p.relative_to(root).as_posix() for p in Path(root).rglob("*")
             if p.is_file() and "__pycache__" not in p.parts]
    with tempfile.TemporaryDirectory(prefix="ahe-ignores-") as tmp:
        subprocess.run(["git", "init", "-q", tmp], check=True, capture_output=True)
        said = subprocess.run(["git", "-C", tmp, "check-ignore", "--no-index", "--stdin"], input="\n".join(files),
                              capture_output=True, text=True)
    return said.stdout.split()


def git(repo, *args, env=None):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True, env=env).strip()


def read(path, adapter):
    value = json.loads(Path(path).read_text())
    if value["adapter_sha256"] != fingerprint(adapter):
        raise ValueError("tested adapter differs from the frozen delivery binding")
    return value


def write(path, *, adapter, repo, run_id, round_no):
    """The binding of a clean delivery clone's commit to `adapter`, which holds the same files (the seed's, before
    any of AHE-Beta's team ran: prepare.py)."""
    if git(repo, "status", "--porcelain"):
        raise ValueError("delivery checkout must be clean")
    if subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", "HEAD", "refs/remotes/origin/main"],
                      capture_output=True).returncode != 0:   # AHE-Beta checks each delivery descends from it
        raise ValueError("the delivery checkout's commit is not on its origin's main (clone it afresh)")
    if fingerprint(repo) != fingerprint(adapter):
        raise ValueError("delivery checkout and Hermit adapter have different contents")
    return record(path, adapter=adapter, commit=git(repo, "rev-parse", "HEAD"), run_id=run_id, round_no=round_no,
                  anew=True)


def record(path, *, adapter, commit, run_id, round_no, anew=False):
    """The binding of `adapter`'s files to the delivery commit that holds them, for the sessions of `round_no` on:
    added to the run's earlier ones (`history`, which `bound` reads), unless `anew` (a new run's seed), written whole.
    The latest is also at the top, as a binding was before it had a history."""
    entry = {"round": round_no, "harness_sha": commit, "adapter_sha256": fingerprint(adapter)}
    path = Path(path)
    earlier = []
    if not anew and path.exists():
        value = json.loads(path.read_text())
        if value.get("run_id") != run_id:
            raise ValueError(f"the delivery binding {path} is run {value.get('run_id')!r}'s, not {run_id!r}'s")
        earlier = [e for e in entries(value) if e.get("round") != round_no]
    value = {"run_id": run_id, **entry, "history": sorted([*earlier, entry], key=lambda e: int(e.get("round") or 0))}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)
    return value


def entries(value: dict) -> list[dict]:
    """A binding's commits, oldest round first (one written before bindings kept a history: its own)."""
    if isinstance(value.get("history"), list):
        return [e for e in value["history"] if isinstance(e, dict)]
    return [{key: value.get(key) for key in ("round", "harness_sha", "adapter_sha256")}]


def bound(value: dict, *, adapter_sha256, round_no=None) -> dict | None:
    """The commit a session of `round_no` runs under: the latest of the run's, up to that round, whose files are its
    adapter's. After a failed step the next round runs the last delivered adapter (its own round was never bound); a
    step the Trainer redoes after a crash runs the adapter it had (a binding written for the round after is not its).
    None when no commit holds that adapter."""
    fits = [e for e in entries(value) if e.get("adapter_sha256") == adapter_sha256
            and (round_no is None or int(e.get("round") or 0) <= int(round_no))]
    return max(fits, key=lambda e: int(e.get("round") or 0)) if fits else None
