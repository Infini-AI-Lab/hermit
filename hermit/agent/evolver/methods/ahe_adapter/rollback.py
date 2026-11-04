"""Automatic file-level rollback of the previous step's harmful changes (spec B.6; the paper's Algorithm 1, line 8).

AHE's released code only tells the Evolve Agent which changes were HARMFUL and leaves the revert to it
(`rollback: agent`); the paper rolls a harmful change back. With `rollback: auto` (the default) the Evolver does it
before the Evolve session, deterministically: for each change of manifest N-1 whose verdict (from
`analysis.evaluate_changes`) is in `rollback_verdicts`, each path of its `files[]` goes back to what it was in the
adapter commit the previous step's rollouts used (`learning_material/step_{N-1}/summary.json["adapter_commit"]`,
the adapter before that change): `git -C <adapter> show <commit>:<path>` — the Evolver's adapter copy carries the
run's git history — and a path absent there is deleted. Paths outside the template's learnable list are skipped,
not touched (the agent could not have changed them legitimately; a `files[]` entry naming `check.py` is a
manifest error, reported as such). Then `check_spec` runs on the adapter; when it fails, every touched file is put
back from the copy taken before the first write (the undo of `continual_harness/evolver.py:424-455`, per file) and
the record says `rollback_failed`. The record is what step N stores under `state/rollbacks/step_N.json`, renders in
query §8 ("already rolled back by the framework") and pre-fills as `type: rollback` manifest entries.

Two refinements of "the adapter before that change":
- The rollout commit of step N-1 is the adapter before step N-1's *session*, not before step N-1's own rollbacks:
  a path that step N-1 rolled back (its record, `previous`) is restored from the content step N-1 wrote, kept as a
  snapshot under `snapshots/<path>` (absent in that record: deleted again), never from the rollout commit, which
  still holds the change step N-1 reverted.
- `ADAPTER.md` is derived from `AGENTS.md` (the template's check requires them equal, the Evolver syncs them after
  every step): when a rollback touches `AGENTS.md`, `ADAPTER.md` is synced to it before the check (`synced`), so
  a manifest that lists `AGENTS.md` alone, the shape START.md tells the agent to write, rolls back.
- The framework's own `type: rollback` entries are never rolled back, whatever their verdict (an `rb-*` entry has
  no predicted fixes and scores INEFFECTIVE by construction; reverting it would re-apply the harmful change).

`rollback: agent` touches nothing and returns a record saying so, so the caller's bookkeeping is uniform.
"""
from __future__ import annotations

import fnmatch
import os
from pathlib import Path, PurePosixPath
import subprocess
from typing import Any, Callable, Optional, Tuple

DEFAULT_VERDICTS = ["HARMFUL"]
ROLLBACK_TYPE = "rollback"                                        # the framework's own manifest entries (rb-*)
COMPANIONS = {"AGENTS.md": "ADAPTER.md"}                          # derived files, synced after a restore
RESTORED = ("restored", "deleted", "synced")                      # the actions that changed a file
# A file as the rollback compares and restores it: its bytes and whether it is executable (git keeps the bit, and a
# skill may run its script directly), or None when there is no file.
State = Optional[Tuple[bytes, bool]]


def normalise_verdicts(verdicts: Any) -> dict[str, str]:
    """{change id: verdict} from `evaluate_changes`' output ({change_evaluations: [{change_id, verdict}]}), a list
    of such entries, or a plain mapping."""
    if not verdicts:
        return {}
    if isinstance(verdicts, dict) and "change_evaluations" in verdicts:
        verdicts = verdicts["change_evaluations"]
    if isinstance(verdicts, dict):
        return {str(k): str(v.get("verdict") if isinstance(v, dict) else v) for k, v in verdicts.items()}
    out = {}
    for entry in verdicts:
        if isinstance(entry, dict) and entry.get("verdict"):
            out[str(entry.get("change_id") or entry.get("id"))] = str(entry["verdict"])
    return out


def learnable_patterns(learnable_paths: Any) -> list[str]:
    """Glob patterns from the manifest's `structure.learnable` ({path, format} entries) or plain strings."""
    patterns = []
    for entry in learnable_paths or []:
        pattern = entry.get("path") if isinstance(entry, dict) else entry
        if pattern:
            patterns.append(str(pattern))
    return patterns


def clean_path(path: str) -> str | None:
    """The adapter-relative POSIX form of a manifest path; None for anything that leaves the adapter."""
    text = str(path).strip().replace("\\", "/")
    if text.startswith("adapter/"):
        text = text[len("adapter/"):]
    pure = PurePosixPath(text)
    if not text or pure.is_absolute() or ".." in pure.parts or any(p in ("", ".") for p in pure.parts[1:]):
        return None
    return str(PurePosixPath(*[p for p in pure.parts if p != "."])) if pure.parts else None


def is_learnable(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def show(adapter: Path, commit: str, path: str) -> State:
    """The file at `commit` (its bytes and its executable bit), or None when no file was there (nothing, a
    directory, or an unknown commit)."""
    entry = subprocess.run(["git", "-C", str(adapter), "ls-tree", commit, "--", path], capture_output=True, text=True)
    fields = entry.stdout.split("\t", 1)[0].split() if entry.returncode == 0 else []
    if len(fields) != 3 or fields[1] != "blob":
        return None
    blob = subprocess.run(["git", "-C", str(adapter), "cat-file", "blob", fields[2]], capture_output=True)
    return (blob.stdout, fields[0] == "100755") if blob.returncode == 0 else None


def _read(path: Path) -> State:
    return (path.read_bytes(), bool(path.stat().st_mode & 0o111)) if path.is_file() else None


def _put(path: Path, state: State) -> None:
    if state is None:
        path.unlink(missing_ok=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(state[0])
        os.chmod(path, 0o755 if state[1] else 0o644)            # the two modes git records


def _check(check_spec_tool: Callable | None, adapter: Path) -> dict:
    if check_spec_tool is None:
        return {"passed": True, "issues": []}
    result = check_spec_tool(path=str(adapter))
    if isinstance(result, list):                                  # the bare issue list form
        return {"passed": not result, "issues": result}
    return {"passed": bool(result.get("passed", True)), "issues": list(result.get("issues") or [])}


def previous_source(previous: dict | None, path: str) -> tuple[bool, State]:
    """(known, state): what the previous step's rollback left at `path`, when its record says it touched it:
    the snapshot it kept (`snapshots/<path>`), or None for a path it deleted. (False, None) when that record does
    not cover the path, or was discarded with its step."""
    if not previous or previous.get("discarded"):
        return False, None
    for change in previous.get("changes") or []:
        for f in change.get("files") or []:
            if f.get("path") != path or f.get("action") not in RESTORED:
                continue
            if f["action"] == "deleted":
                return True, None
            snapshot = Path(str(previous.get("snapshots") or "")) / path if previous.get("snapshots") else None
            if snapshot is not None and snapshot.is_file():
                return True, _read(snapshot)                      # kept with its executable bit
            return False, None                                    # no snapshot left: the rollout commit it is
    return False, None


def rollback(adapter_dir: Path, manifest: dict | None, verdicts: Any, parent_commit: str | None,
             learnable_paths: Any, check_spec_tool: Callable | None, params: dict, *,
             previous: dict | None = None, snapshots: Path | None = None) -> dict:
    """Revert the files of every change of `manifest` whose verdict is in `params.rollback_verdicts` to
    `parent_commit` (or to what the previous step's rollback, `previous`, left there) and return the record for
    `state/rollbacks/step_N.json`; the restored content is kept under `snapshots/<path>` for the next step."""
    mode = str(params.get("rollback", "auto"))
    wanted = [str(v) for v in (params.get("rollback_verdicts") or DEFAULT_VERDICTS)]
    by_id = normalise_verdicts(verdicts)
    record: dict[str, Any] = {"mode": mode, "parent_commit": parent_commit, "verdicts": wanted, "changes": [],
                              "touched": [], "rollback_failed": False, "check": None, "note": "",
                              "snapshots": str(snapshots) if snapshots else None}
    changes = [c for c in (manifest or {}).get("changes") or [] if isinstance(c, dict)]
    targets, kept = [], []
    for c in changes:
        if by_id.get(str(c.get("id") or c.get("change_id"))) not in wanted:
            continue
        (kept if str(c.get("type") or "") == ROLLBACK_TYPE else targets).append(c)
    record["changes"] = [{"id": str(c.get("id") or c.get("change_id")), "verdict": by_id.get(str(c.get("id") or c.get("change_id"))),
                          "files": [{"path": str(f), "action": "left in place",
                                     "reason": "a rollback is not reverted: that would re-apply the change it reverted"}
                                    for f in c.get("files") or []]} for c in kept]
    if mode == "agent":
        record["note"] = ("rollback: agent — nothing reverted by the framework; the Evolve Agent compares with "
                          f"`git show {parent_commit}:<path>` and decides (the released code's behaviour)")
        record["changes"] += [{"id": str(c.get("id") or c.get("change_id")), "verdict": by_id.get(str(c.get("id") or c.get("change_id"))),
                               "files": [{"path": str(f), "action": "left to the agent"} for f in c.get("files") or []]}
                              for c in targets]
        return record
    if not targets:
        record["note"] = ("no change of the previous manifest has a verdict in the list" if not kept
                          else "only the framework's own rollback entries have a verdict in the list: left in place")
        return record
    if not parent_commit:
        record["note"] = "no parent commit known: nothing reverted"
        record["changes"] += [{"id": str(c.get("id") or c.get("change_id")), "verdict": by_id.get(str(c.get("id") or c.get("change_id"))),
                               "files": [{"path": str(f), "action": "skipped", "reason": "no parent commit"}
                                         for f in c.get("files") or []]} for c in targets]
        return record
    adapter = Path(adapter_dir)
    patterns = learnable_patterns(learnable_paths)
    before: dict[str, State] = {}                                 # the pre-rollback copy of every touched file
    restored: dict[str, State] = {}                               # what each touched path holds after the rollback
    for change in targets:
        cid = str(change.get("id") or change.get("change_id"))
        entry = {"id": cid, "verdict": by_id.get(cid), "description": str(change.get("description") or "")[:300],
                 "files": []}
        for raw in change.get("files") or []:
            path = clean_path(raw)
            if path is None:
                entry["files"].append({"path": str(raw), "action": "skipped", "reason": "not a path inside the adapter"})
                continue
            if patterns and not is_learnable(path, patterns):
                entry["files"].append({"path": path, "action": "skipped", "reason": "not a learnable path"})
                continue
            target = adapter / path
            current = _read(target)
            known, parent = previous_source(previous, path)
            source = "the previous step's rollback" if known else "the parent"
            if not known:
                parent = show(adapter, parent_commit, path)
            if parent is None and current is None:
                entry["files"].append({"path": path, "action": "absent", "reason": f"in neither {source} nor the adapter"})
                continue
            if parent == current:
                entry["files"].append({"path": path, "action": "unchanged", "reason": f"already {source}'s content"})
                continue
            before.setdefault(path, current)
            _put(target, parent)
            restored[path] = parent
            entry["files"].append({"path": path, "action": "restored" if parent is not None else "deleted", "source": source})
        for path, companion in COMPANIONS.items():                # ADAPTER.md follows AGENTS.md
            if path not in restored or companion in restored:
                continue
            content, current = restored[path], _read(adapter / companion)
            if content is None or current is None or content[0] == current[0]:  # no companion file: nothing to sync
                continue
            before.setdefault(companion, current)
            _put(adapter / companion, (content[0], current[1]))     # the text follows; the companion's mode stays
            restored[companion] = (content[0], current[1])
            entry["files"].append({"path": companion, "action": "synced", "reason": f"{companion} is {path}'s copy"})
        record["changes"].append(entry)
    record["touched"] = sorted(before)
    if before:
        check = _check(check_spec_tool, adapter)
        record["check"] = check
        if not check["passed"]:
            for path, content in before.items():
                _put(adapter / path, content)
            record["rollback_failed"] = True
            record["note"] = ("check_spec failed after the rollback; every touched file was put back: "
                              + "; ".join(str(i.get("problem", i))[:200] if isinstance(i, dict) else str(i)[:200]
                                          for i in check["issues"][:3]))
            for entry in record["changes"]:
                for f in entry["files"]:
                    if f["action"] in RESTORED:
                        f["action"], f["reason"] = "undone", "check_spec failed after the rollback"
        elif snapshots is not None:
            for path, content in restored.items():
                if content is not None:
                    _put(Path(snapshots) / path, content)
    return record
