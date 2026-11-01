"""The user's own Codex login (`agent.params.login: chatgpt`): a ChatGPT account signed in with `codex login`, no API
key and no per-token price.

One copy of the login is kept for every session, acting and learning alike (`ROOT/login/auth.json`, outside any
run's directory); every session's home starts from it, and a token a session refreshed is written back to it under a
lock, so that no session starts from a token a refresh has replaced (ChatGPT rotates its refresh tokens). The user's
own file is read, never written: it replaces the copy when it is newer (a later `codex login`). A session in the
rollout sandbox, which cannot write that copy, gets it from the sandbox's caller and hands its refresh back the same
way (hermit.isolate).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import shutil

FILE = "auth.json"
ROOT = Path.home() / ".cache" / "hermit" / "codex-login"      # the shared copy: ROOT/login/auth.json


def source() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / FILE


def _last_refresh(path: Path):
    """When the login in `path` was refreshed, or None for a file that is no login: not a JSON object, or a
    last_refresh that does not parse or lies ahead (a session that could write the file must not pin the copy)."""
    try:
        data = json.loads(Path(path).read_text())
        when = datetime.fromisoformat(str(data["last_refresh"]).replace("Z", "+00:00"))
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when if when <= datetime.now(timezone.utc) + timedelta(hours=1) else None


def _newer(candidate: Path, than: Path) -> bool:
    mine, theirs = _last_refresh(candidate), _last_refresh(than) if Path(than).is_file() else None
    return mine is not None and (theirs is None or mine > theirs)


def _replace(src: Path, dst: Path) -> None:
    tmp = dst.with_suffix(".tmp")
    shutil.copy2(src, tmp)
    os.chmod(tmp, 0o600)
    os.replace(tmp, dst)


def prepare(root: Path, home: Path, user_file: Path | None = None) -> None:
    """The session's home gets the run's copy of the login, refreshed from the user's file when that is newer."""
    user_file = Path(user_file or source())
    master = Path(root) / "login" / FILE
    master.parent.mkdir(parents=True, exist_ok=True)
    with open(master.parent / ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if user_file.is_file() and _newer(user_file, master):
            _replace(user_file, master)
        if not master.is_file():
            raise RuntimeError(f"login: chatgpt, but there is no Codex login at {user_file} (run `codex login`)")
        _replace(master, Path(home) / FILE)


def master(root: Path | None = None) -> Path:
    return Path(root or ROOT) / "login" / FILE


def keep_refreshed(root: Path, home: Path) -> None:
    """The session's login, if Codex refreshed it (a later last_refresh), becomes the shared copy."""
    take_back(root, Path(home) / FILE)


def take_back(root: Path, path: Path) -> None:
    """A login file a session left: the shared copy if it is a later refresh of it."""
    master = Path(root) / "login" / FILE
    if not Path(path).is_file() or not master.is_file():
        return
    with open(master.parent / ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if _newer(path, master):
            _replace(path, master)
