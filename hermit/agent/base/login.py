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
import stat
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
    if os.path.lexists(tmp):                        # a link a session left beside the login: never written through
        shutil.rmtree(tmp) if os.path.isdir(tmp) and not os.path.islink(tmp) else os.unlink(tmp)
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
    take_back(root, Path(home) / FILE, within=Path(home))


def _plain(path: Path, home: Path | None = None) -> bool:
    """Whether `path` is a regular file reached through no link below `home`: a session can write its home, and a
    link it leaves there (to another login, to any file) is never read as the session's own."""
    path = Path(path)
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):       # a link, a pipe, a directory
            return False
    except OSError:
        return False
    if home is not None:
        try:
            rel = path.relative_to(home)
        except ValueError:
            return False
        current = Path(home)
        for part in rel.parts[:-1]:
            current = current / part
            if current.is_symlink():
                return False
    return True


def remove(path: Path, within: Path) -> None:
    """Remove a login copy at `path` below `within` (a directory a session could write): a link on the way (a
    directory, or the file) is removed itself, never followed, so nothing outside `within` is deleted."""
    path, within = Path(path), Path(within)
    try:
        rel = path.relative_to(within)
    except ValueError:
        return
    current = within
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            current.unlink()
            return
        if not current.exists():
            return
    if _plain(path, within):
        path.unlink()


def _account(path: Path):
    """The ChatGPT account a login file is for (`tokens.account_id`), or None."""
    try:
        tokens = json.loads(Path(path).read_text()).get("tokens")
        return tokens.get("account_id") if isinstance(tokens, dict) else None
    except (OSError, ValueError, AttributeError):
        return None


def take_back(root: Path, path: Path, within: Path | None = None) -> None:
    """A login file a session left: the shared copy if it is a later refresh of it (a regular file, never one read
    through a link: none below `within`, the directory the caller trusts, and not the file itself) for the account
    the copy names, when it names one (a session could write another account's login there)."""
    master = Path(root) / "login" / FILE
    if not _plain(path, Path(within) if within is not None else Path(path).parent) or not master.is_file():
        return
    with open(master.parent / ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        account = _account(master)                  # a copy that names its account keeps to it
        if _newer(path, master) and (account is None or _account(path) == account):
            _replace(path, master)


# ---- the plan: what a ChatGPT login allows, shared by every session on this machine -------------------------------
# A login's sessions draw on its plan's usage windows and its rate limit. One plan directory (PLAN, `plan_dir` to name
# another) steers every session on the machine, whatever login copy a session signs in with (a rollout sandbox's
# included: it is given this directory), through files read again before each session:
#   PAUSE       while it exists, no session starts
#   MAX_USED    a number: no session starts while a window of the plan that has not reset is at or above that percent
#   SLOTS       a number: at most that many sessions run at once on this machine (past some number at once the
#               login's backend answers 429 and a short session spends its clock waiting); 0 or none: no limit
# and the sessions keep the plan's latest report there (plan.json, and every report in plan.jsonl). A session the plan
# refused for its usage limit holds every session back until the window resets, and is run again: it never ran, so it
# is no answer. (Ported from the OfficeQA GPT-5.6-Luna work, examples/officeqa/utils/agent/codex_login.py in PR #54.)
PLAN = ROOT / "plan"
LOCKS = ".slot-locks"                     # not "slots": SLOTS and slots/ are one name on a case-insensitive disk
MAX_WAIT_S = 12 * 3600                    # how long a session waits for the plan before it gives up (unavailable)
LIMIT = re.compile(r"hit your usage limit|usage_limit_reached|usageLimitExceeded", re.I)
OFF = ("apps", "plugins", "remote_plugin", "browser_use", "browser_use_external", "computer_use", "in_app_browser",
       "skill_mcp_dependency_install")      # what reaches past the workspace on a login: connectors, plugins, browsers


def config_lines() -> list[str]:
    """config.toml lines for a session on the login: the file login, nothing that reaches outside the workspace, no
    update check."""
    return ['cli_auth_credentials_store = "file"', "check_for_update_on_startup = false",
            *(f"features.{name} = false" for name in OFF)]


def plan_dir(plan: Path | str | None = None) -> Path:
    return Path(plan) if plan else PLAN


def _number(path: Path) -> float | None:
    try:
        return float(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _windows(report: dict, at: float | None = None) -> list[tuple[float, float]]:
    """(percent used, reset time) of each of the report's windows. A window without `resets_at` resets
    `resets_in_seconds` after the report (`at`: when it was made), or, saying neither, an hour after it: a window
    that never resets would hold every session, and none would run to report again."""
    if at is None:
        try:
            at = datetime.fromisoformat(str(report.get("time")).replace("Z", "+00:00")).timestamp()
        except ValueError:
            at = time.time()
    out = []
    for w in (report.get("primary"), report.get("secondary")):
        if isinstance(w, dict):
            resets = w.get("resets_at") or (at + float(w["resets_in_seconds"]) if w.get("resets_in_seconds") else 0)
            out.append((float(w.get("used_percent") or 0), float(resets or at + 3600)))
    return out


def held(plan: Path | str | None = None) -> str:
    """Why no session may start now ("" when one may). Each window that has not reset counts on its own: a short
    window's reset does not lift a long window that is still over."""
    d = plan_dir(plan)
    if (d / "PAUSE").exists():
        return f"paused ({d / 'PAUSE'} exists)"
    try:
        report = json.loads((d / "plan.json").read_text())
        made = (d / "plan.json").stat().st_mtime
    except (OSError, ValueError):
        return ""
    now, limit = time.time(), _number(d / "MAX_USED")
    try:
        datetime.fromisoformat(str(report.get("time")).replace("Z", "+00:00"))
        made = None                              # the report says when it was made
    except ValueError:
        pass
    live = [(used, resets) for used, resets in _windows(report, made) if resets > now]
    over = [w for w in live if limit is not None and w[0] >= limit]
    refused = report.get("rate_limit_reached_type")
    if refused:              # the plan said no: until the hold's own end, else while a window it fills is live
        until = float(report.get("held_until") or 0)
        over += [(100.0, until)] if until > now else [w for w in live if w[0] >= 100]
    if not over:
        return ""
    used, resets = max(over, key=lambda w: w[1])
    until = time.strftime("%Y-%m-%d %H:%M", time.localtime(resets))
    return (f"plan usage {used:.0f}%{f' (limit {limit:.0f}%)' if limit is not None else ''}"
            f"{', refused' if refused else ''} until {until}")


def wait(plan: Path | str | None = None, cancel=None, log=None, max_s: float | None = MAX_WAIT_S) -> str:
    """Block while `held`; "" once a session may start, "cancelled" when `cancel` was set, "timeout" past `max_s`
    seconds (None: no limit). Says why at the start and every half hour."""
    started, told = time.time(), 0.0
    while (reason := held(plan)):
        if log is not None and time.time() - told >= 1800:
            log(f"sessions on the login held back: {reason} (waiting {(time.time() - started) / 3600:.1f} h)")
            told = time.time()
        if max_s is not None and time.time() - started >= max_s:
            return "timeout"
        if cancel is not None and cancel.wait(30):
            return "cancelled"
        if cancel is None:
            time.sleep(30)
    return ""


@contextmanager
def slot(plan: Path | str | None = None, cancel=None, log=None, deadline: float | None = None):
    """Hold one of the machine's session slots while the block runs (none without a SLOTS limit). Yields True, or
    "cancelled" when `cancel` was set while waiting for one, "timeout" past `deadline` (a time.time())."""
    d, told = plan_dir(plan), False
    while True:
        n = int(_number(d / "SLOTS") or 0)
        if n <= 0:
            yield True
            return
        (d / LOCKS).mkdir(parents=True, exist_ok=True)
        for i in random.sample(range(n), n):
            fd = os.open(d / LOCKS / f"{i}.lock", os.O_RDWR | os.O_CREAT, 0o600)
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
        if deadline is not None and time.time() >= deadline:
            yield "timeout"
            return
        pause = 2 + random.random() * 2
        if deadline is not None:
            pause = max(0.0, min(pause, deadline - time.time()))
        if cancel is not None and cancel.wait(pause):
            yield "cancelled"
            return
        if cancel is None:
            time.sleep(pause)


def usage_of(home: Path) -> dict | None:
    """The plan's usage the latest session log under a Codex home reports (Codex's rate_limits), or None."""
    logs = sorted((p for p in Path(home).glob("sessions/**/*.jsonl") if _plain(p, Path(home))),
                  key=lambda p: p.stat().st_mtime)
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


def note_usage(plan: Path | str | None, home: Path) -> dict | None:
    """Keep a session's report of the plan's usage as plan.json when it is newer, and append it to plan.jsonl. A
    record that cannot be written (a full disk) is skipped: it never fails the session."""
    try:
        return _note_usage(plan, home)
    except OSError:
        return None


def _note_usage(plan: Path | str | None, home: Path) -> dict | None:
    seen = usage_of(home)
    if not seen:
        return None
    d = plan_dir(plan)
    d.mkdir(parents=True, exist_ok=True)
    windows = _windows(seen)
    used, resets = max(windows, key=lambda w: w[0]) if windows else (0.0, 0.0)
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


def hold(plan: Path | str | None = None) -> None:
    """A session was refused for the plan's limit: hold every session back until a report says otherwise (or for an
    hour, when no report says when the window resets). If the hold cannot be written, this session waits five minutes
    itself rather than ask again at once."""
    try:
        _hold(plan)
    except OSError:
        time.sleep(300)


def _hold(plan: Path | str | None = None) -> None:
    d = plan_dir(plan)
    d.mkdir(parents=True, exist_ok=True)
    with open(d / ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            current = json.loads((d / "plan.json").read_text())
        except (OSError, ValueError):
            current = {}
        now = time.time()
        full = [resets for used, resets in _windows(current) if used >= 100 and resets > now]
        current["rate_limit_reached_type"] = current.get("rate_limit_reached_type") or "session_refused"
        current["held_until"] = max(full) if full else now + 3600       # the full window's reset, else an hour
        (d / "plan.json").write_text(json.dumps(current, indent=1))
