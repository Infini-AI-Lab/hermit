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

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import random
import re
import shutil
import time

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


# ---- the plan: what a ChatGPT login allows, shared by every session on this machine -------------------------------
# A login's sessions draw on its plan's usage windows and its rate limit. Under ROOT/plan/ (`plan_dir`) a person can
# steer every running session through files, read again before each session:
#   PAUSE       while it exists, no session starts
#   MAX_USED    a number: no session starts while the plan's fullest window is at or above that percent
#   SLOTS       a number: at most that many sessions run at once on this machine (past some number at once the
#               login's backend answers 429 and a short session spends its clock waiting); 0 or none: no limit
# and the sessions keep the plan's latest report there (plan.json, and every report in plan.jsonl). A session the plan
# refused for its usage limit holds every session back until the window resets, and is run again: it never ran, so it
# is no answer. (Ported from the OfficeQA GPT-5.6-Luna work, examples/officeqa/utils/agent/codex_login.py in PR #54.)
LIMIT = re.compile(r"hit your usage limit|usage_limit_reached|usageLimitExceeded", re.I)
OFF = ("apps", "plugins", "remote_plugin", "browser_use", "browser_use_external", "computer_use", "in_app_browser",
       "skill_mcp_dependency_install")      # what reaches past the workspace on a login: connectors, plugins, browsers


def config_lines() -> list[str]:
    """config.toml lines for a session on the login: the file login, nothing that reaches outside the workspace, no
    update check."""
    return ['cli_auth_credentials_store = "file"', "check_for_update_on_startup = false",
            *(f"features.{name} = false" for name in OFF)]


def plan_dir(root: Path | None = None) -> Path:
    return Path(root or ROOT) / "plan"


def _number(path: Path) -> float | None:
    try:
        return float(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _used(report: dict) -> tuple[float, float]:
    """(percent used, reset time) of the report's fullest window."""
    windows = [w for w in (report.get("primary"), report.get("secondary")) if isinstance(w, dict)]
    if not windows:
        return 0.0, 0.0
    w = max(windows, key=lambda w: float(w.get("used_percent") or 0))
    return float(w.get("used_percent") or 0), float(w.get("resets_at") or 0)


def held(root: Path | None = None) -> str:
    """Why no session may start now ("" when one may)."""
    d = plan_dir(root)
    if (d / "PAUSE").exists():
        return f"paused ({d / 'PAUSE'} exists)"
    try:
        report = json.loads((d / "plan.json").read_text())
    except (OSError, ValueError):
        return ""
    used, resets = _used(report)
    limit = _number(d / "MAX_USED")
    refused = report.get("rate_limit_reached_type")
    if (refused or (limit is not None and used >= limit)) and not (resets and time.time() >= resets):
        return (f"plan usage {used:.0f}%{f' (limit {limit:.0f}%)' if limit is not None else ''}"
                f"{', refused' if refused else ''} until {time.strftime('%Y-%m-%d %H:%M', time.localtime(resets))}")
    return ""


def wait(root: Path | None = None, cancel=None, log=None) -> bool:
    """Block while `held`; False when `cancel` was set meanwhile."""
    told = False
    while (reason := held(root)):
        if not told and log is not None:
            log(f"sessions on the login held back: {reason}")
            told = True
        if cancel is not None and cancel.wait(30):
            return False
        if cancel is None:
            time.sleep(30)
    return True


@contextmanager
def slot(root: Path | None = None, cancel=None, log=None):
    """Hold one of the machine's session slots while the block runs (none without a SLOTS limit). Yields False when
    `cancel` was set while waiting for one."""
    d, told = plan_dir(root), False
    while True:
        n = int(_number(d / "SLOTS") or 0)
        if n <= 0:
            yield True
            return
        (d / "slots").mkdir(parents=True, exist_ok=True)
        for i in random.sample(range(n), n):
            fd = os.open(d / "slots" / f"{i}.lock", os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                continue
            try:
                yield True
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
            return
        if not told and log is not None:
            log(f"waiting for one of the {n} session slots")
            told = True
        if cancel is not None and cancel.wait(2 + random.random() * 2):
            yield False
            return
        if cancel is None:
            time.sleep(2 + random.random() * 2)


def usage_of(home: Path) -> dict | None:
    """The plan's usage the latest session log under a Codex home reports (Codex's rate_limits), or None."""
    logs = sorted(Path(home).glob("sessions/**/*.jsonl"), key=lambda p: p.stat().st_mtime)
    for log in reversed(logs):
        last = None
        for line in log.read_text(errors="replace").splitlines():
            if '"rate_limits"' not in line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            payload = event.get("payload") or {}
            if isinstance(payload, dict) and isinstance(payload.get("rate_limits"), dict):
                last = {"time": event.get("timestamp"), **payload["rate_limits"]}
        if last:
            return last
    return None


def note_usage(root: Path | None, home: Path) -> dict | None:
    """Keep a session's report of the plan's usage as plan.json when it is newer, and append it to plan.jsonl. A
    record that cannot be written (a full disk) is skipped: it never fails the session."""
    try:
        return _note_usage(root, home)
    except OSError:
        return None


def _note_usage(root: Path | None, home: Path) -> dict | None:
    seen = usage_of(home)
    if not seen:
        return None
    d = plan_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    used, resets = _used(seen)
    with open(d / "plan.jsonl", "a") as out:
        out.write(json.dumps({"time": seen.get("time"), "used_percent": used, "resets_at": resets,
                              "reached": seen.get("rate_limit_reached_type")}) + "\n")
    with open(d / ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            current = json.loads((d / "plan.json").read_text())
        except (OSError, ValueError):
            current = {}
        if str(seen.get("time") or "") >= str(current.get("time") or ""):
            tmp = d / f"plan.{os.getpid()}.tmp"
            tmp.write_text(json.dumps(seen, indent=1))
            os.replace(tmp, d / "plan.json")
    return seen


def refused(output: str) -> bool:
    """Whether a session's output reports the plan's usage limit (no answer: it is run again after the wait)."""
    return bool(LIMIT.search(output or ""))


def hold(root: Path | None = None) -> None:
    """A session was refused for the plan's limit: hold every session back until a report says otherwise (or for an
    hour, when no report says when the window resets). If the hold cannot be written, this session waits five minutes
    itself rather than ask again at once."""
    try:
        _hold(root)
    except OSError:
        time.sleep(300)


def _hold(root: Path | None = None) -> None:
    d = plan_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    with open(d / ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            current = json.loads((d / "plan.json").read_text())
        except (OSError, ValueError):
            current = {}
        current["rate_limit_reached_type"] = current.get("rate_limit_reached_type") or "session_refused"
        window = current.setdefault("primary", {})
        if not window.get("resets_at") or float(window["resets_at"]) < time.time():
            window["resets_at"] = time.time() + 3600
        (d / "plan.json").write_text(json.dumps(current, indent=1))
