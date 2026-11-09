"""The user's own subscription on a coding-agent CLI (`agent.params.login`): no API key and no per-token price.

`login: chatgpt` is Codex on a ChatGPT account signed in with `codex login`. One copy of the login is kept for every
session, acting and learning alike (`ROOT/login/auth.json`, outside any run's directory); every session's home starts
from it, and a token a session refreshed is written back to it under a lock, so that no session starts from a token a
refresh has replaced (ChatGPT rotates its refresh tokens). The user's own file is read, never written: it replaces the
copy when it is newer (a later `codex login`). A session in the task sandbox, which cannot write that copy, gets it
from the sandbox's caller and hands its refresh back the same way (hermit.isolate).

`login: claude` is Claude Code on a Claude subscription, signed in with the token `claude setup-token` prints, which
the user exports as CLAUDE_CODE_OAUTH_TOKEN: Anthropic's way for scripts (a year, model requests only, no refresh). The
token passes through the environment to the official CLI and nothing here reads, keeps or copies a credential: no
shared copy, no write-back. The user's `claude` sign-in (~/.claude/.credentials.json, the Keychain on macOS) is left
alone on purpose: a session refreshing a copy of it rotates the refresh token under the user's own Claude Code, and
Anthropic's terms do not let a third-party tool collect, store or intermediate such credentials.

A sandbox that runs a login's sessions allows the hosts they reach (`hosts`); the token passes by name with each
session that runs on it, never to the sandbox's other commands.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import errno
import fcntl
import json
import math
import os
from pathlib import Path
import random
import re
import shutil
import stat
import subprocess
import threading
import time

LOGINS = {"chatgpt": "codex", "claude": "claude"}             # a `login`: the CLI it signs in
NO_LOGIN = ("pi", "opencode")             # the CLIs that sign in only through a <backend>/<model> reference
FILE = "auth.json"
ROOT = Path.home() / ".cache" / "hermit" / "codex-login"      # the shared copy: ROOT/login/auth.json
TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"                         # the token of `claude setup-token`, the user's to export
CLAUDE_HOSTS = ("api.anthropic.com",)                         # what a session on the token reaches (no refresh host)
HOSTS = {"chatgpt": ("chatgpt.com", "auth.openai.com"), "claude": CLAUDE_HOSTS}   # what a login's sessions reach
CLAUDE_MIN_VERSION = (2, 1, 248)                              # --restricted, which a text session needs
NO_TOKEN = (f"login: claude signs in with the token `claude setup-token` prints, in {TOKEN_ENV}, which is not set: run "
            "`claude setup-token` once and export the token (docs/guides/claude-subscription.md)")


def hosts(*logins) -> list[str]:
    """The hosts the sessions on `logins` reach (no key: a login's token passes by name with its session)."""
    return list(dict.fromkeys(h for login in logins for h in HOSTS.get(login, ())))


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


# ---- the plan: what a login allows, shared by every session on this machine ---------------------------------------
# A login's sessions draw on its plan's usage windows and its rate limit. One plan directory per login (PLAN and
# CLAUDE_PLAN, `plan_dir` to name another) steers every session on the machine, whatever login copy a session signs in
# with (a task sandbox's included: it is given this directory), through files read again before each session:
#   PAUSE       while it exists, no session starts
#   MAX_USED    a number: no session starts while a window of the plan that has not reset is at or above that percent
#   SLOTS       a number: at most that many sessions run at once on this machine (past some number at once the
#               login's backend answers 429 and a short session spends its clock waiting); 0 or none: no limit
# and the sessions keep the plan's latest report there (plan.json, and every report in plan.jsonl). A session the plan
# refused for its usage limit holds every session back until the window resets, and is run again: it never ran, so it
# is no answer. (Ported from the OfficeQA GPT-5.6-Luna work, examples/officeqa/utils/agent/codex_login.py in PR #54.)
PLAN = ROOT / "plan"
CLAUDE_PLAN = Path.home() / ".cache" / "hermit" / "claude-login" / "plan"
LOCKS = ".slot-locks"                     # not "slots": SLOTS and slots/ are one name on a case-insensitive disk
MAX_WAIT_S = 12 * 3600                    # how long a session waits for the plan before it gives up (unavailable)
LONGEST_HOLD_S = 31 * 86400               # no window resets later than this: a reset past it is a report's mistake
LOCK_WAIT_S = 120                         # how long a writer waits for the plan's lock before it gives the write up
LIMIT = re.compile(r"hit your usage limit|usage_limit_reached|usageLimitExceeded", re.I)
OFF = ("apps", "plugins", "remote_plugin", "browser_use", "browser_use_external", "computer_use", "in_app_browser",
       "skill_mcp_dependency_install")      # what reaches past the workspace on a login: connectors, plugins, browsers


def config_lines() -> list[str]:
    """config.toml lines for a session on the login: the file login, nothing that reaches outside the workspace, no
    update check."""
    return ['cli_auth_credentials_store = "file"', "check_for_update_on_startup = false",
            *(f"features.{name} = false" for name in OFF)]


def plan_dir(plan: Path | str | None = None, login: str | None = None) -> Path:
    """The plan's directory: the one given (`~` expanded, made absolute where it is resolved: the parent resolves it
    before a worker in another directory gets it), else the machine's one for the login."""
    return Path(os.path.expanduser(str(plan))).absolute() if plan else CLAUDE_PLAN if login == "claude" else PLAN


# The plan's directory is shared with the sessions it steers (a task sandbox binds it, and a session's commands may run
# where it is), while processes outside any sandbox write it too (a learner's sessions, other runs). Its files are read
# and written as regular files of its own, never through a link, a fifo or a directory a session left at a name, and
# what they say is checked before it is used.

def _clear(path: Path) -> None:
    """Remove whatever is at `path` without following it (a link, a fifo, a directory a session left there)."""
    try:
        if stat.S_ISDIR(os.lstat(path).st_mode):
            shutil.rmtree(path)
        else:
            os.unlink(path)
    except FileNotFoundError:
        pass


def _own_fd(path: Path, flags: int) -> int:
    """`path` opened as a regular file, never through what a session left at its name: that is removed first."""
    for _ in range(3):
        try:
            fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600)
        except OSError as exc:
            if exc.errno not in (errno.ELOOP, errno.EISDIR, errno.ENXIO, errno.ENOTDIR, errno.EEXIST):
                raise
            _clear(path)
            continue
        if stat.S_ISREG(os.fstat(fd).st_mode):
            return fd
        os.close(fd)
        _clear(path)
    raise OSError(errno.EEXIST, f"{path} is not a file of the plan's own")


def _read_own(path: Path, limit: int = 1 << 20) -> str | None:
    """A regular file's text (at most `limit` bytes), or None: missing, a link, a fifo or a directory."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        chunks, size = [], 0
        while size < limit and (chunk := os.read(fd, min(65536, limit - size))):
            chunks.append(chunk)
            size += len(chunk)
        return b"".join(chunks).decode(errors="replace")
    except OSError:
        return None
    finally:
        os.close(fd)


def _report(d: Path) -> dict:
    """plan.json as a mapping ({} when missing or not one)."""
    try:
        report = json.loads(_read_own(d / "plan.json") or "{}")
    except ValueError:
        return {}
    return report if isinstance(report, dict) else {}


def _write_report(d: Path, report: dict) -> None:
    """plan.json replaced whole (a reader without the lock never sees half of it), by a file of its own."""
    tmp = d / f"plan.{os.getpid()}.{threading.get_ident()}.tmp"
    fd = _own_fd(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    try:
        os.write(fd, json.dumps(report, indent=1).encode())
    finally:
        os.close(fd)
    if os.path.isdir(d / "plan.json") and not os.path.islink(d / "plan.json"):
        _clear(d / "plan.json")
    os.replace(tmp, d / "plan.json")             # replaces a link at the name, never what it points to


@contextmanager
def _plan_lock(d: Path, wait_s: float | None = None):
    """The plan's lock (its writers one at a time), given up past `wait_s` (LOCK_WAIT_S): a session's command could
    hold it."""
    wait_s = LOCK_WAIT_S if wait_s is None else wait_s
    fd = _own_fd(d / ".lock", os.O_RDWR | os.O_CREAT)
    try:
        deadline = time.time() + wait_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() >= deadline:
                    raise OSError(errno.EWOULDBLOCK, f"the plan's lock ({d / '.lock'}) is held past {wait_s:.0f} s")
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def _number(path: Path) -> float | None:
    try:
        value = float((_read_own(path, 64) or "").strip())
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _windows(report: dict, at: float | None = None) -> list[tuple[float, float]]:
    """(percent used, reset time) of each of the report's windows. A window without `resets_at` resets
    `resets_in_seconds` after the report (`at`: when it was made), or, saying neither, an hour after it: a window
    that never resets would hold every session, and none would run to report again."""
    if at is None:
        try:
            at = datetime.fromisoformat(str(report.get("time")).replace("Z", "+00:00")).timestamp()
        except ValueError:
            at = time.time()
    named = report.get("windows")
    out = []
    for w in (report.get("primary"), report.get("secondary"), *(named.values() if isinstance(named, dict) else ())):
        if isinstance(w, dict):
            resets = w.get("resets_at") or (at + float(w["resets_in_seconds"]) if w.get("resets_in_seconds") else 0)
            out.append((float(w.get("used_percent") or 0), float(resets or at + 3600)))
    return out


def held(plan: Path | str | None = None, credits: bool = False) -> str:
    """Why no session may start now ("" when one may). Each window that has not reset counts on its own: a short
    window's reset does not lift a long window that is still over. `credits`: the session may draw usage credits
    (`usage_credits: true`), so a hold another session wrote because it drew them does not hold it."""
    d = plan_dir(plan)
    if os.path.lexists(d / "PAUSE"):
        return f"paused ({d / 'PAUSE'} exists)"
    try:
        return _held(d, credits)
    except (TypeError, ValueError, OverflowError, OSError):   # a report that is not one: none to hold by
        return ""


def _held(d: Path, credits: bool) -> str:
    report = _report(d)
    if not report:
        return ""
    made = os.lstat(d / "plan.json").st_mtime
    now, limit = time.time(), _number(d / "MAX_USED")
    try:
        datetime.fromisoformat(str(report.get("time")).replace("Z", "+00:00"))
        made = None                              # the report says when it was made
    except ValueError:
        pass
    live = [(used, min(resets, now + LONGEST_HOLD_S)) for used, resets in _windows(report, made) if resets > now]
    over = [w for w in live if limit is not None and w[0] >= limit]
    refused = report.get("rate_limit_reached_type")
    if refused == "usage_credits" and credits:
        refused = None       # the hold is for the sessions that may not draw credits; MAX_USED still holds this one
    if refused:              # the plan said no: until the hold's own end, else while a window it fills is live
        until = min(float(report.get("held_until") or 0), now + LONGEST_HOLD_S)
        over += [(100.0, until)] if until > now else [w for w in live if w[0] >= 100]
    if not over:
        return ""
    used, resets = max(over, key=lambda w: w[1])
    until = time.strftime("%Y-%m-%d %H:%M", time.localtime(resets))
    return (f"plan usage {used:.0f}%{f' (limit {limit:.0f}%)' if limit is not None else ''}"
            f"{', refused' if refused else ''} until {until}")


def wait(plan: Path | str | None = None, cancel=None, log=None, max_s: float | None = MAX_WAIT_S,
         credits: bool = False) -> str:
    """Block while `held` (`credits` as there); "" once a session may start, "cancelled" when `cancel` was set,
    "timeout" past `max_s` seconds (None: no limit). Says why at the start and every half hour."""
    started, told = time.time(), 0.0
    while (reason := held(plan, credits)):
        if log is not None and time.time() - told >= 1800:
            log(f"sessions on the login held back: {reason} (waiting {(time.time() - started) / 3600:.1f} h)")
            told = time.time()
        if max_s is not None and time.time() - started >= max_s:
            return "timeout"
        poll = 30.0 if max_s is None else min(30.0, max(0.0, max_s - (time.time() - started)))   # never past max_s
        if cancel is not None and cancel.wait(poll):
            return "cancelled"
        if cancel is None:
            time.sleep(poll)
    return ""


@contextmanager
def slot(plan: Path | str | None = None, cancel=None, log=None, deadline: float | None = None):
    """Hold one of the machine's session slots while the block runs (none without a SLOTS limit). Yields True, or
    "cancelled" when `cancel` was set while waiting for one, "timeout" past `deadline` (a time.time())."""
    d, told = plan_dir(plan), False
    while True:
        n = int(min(max(_number(d / "SLOTS") or 0, 0), 1024))
        if n <= 0:
            yield True
            return
        locks = d / LOCKS
        if not stat.S_ISDIR(os.lstat(locks).st_mode if os.path.lexists(locks) else stat.S_IFDIR):
            _clear(locks)                            # a link or a file a session left: the locks are made here
        locks.mkdir(parents=True, exist_ok=True)
        for i in random.sample(range(n), n):
            fd = _own_fd(locks / f"{i}.lock", os.O_RDWR | os.O_CREAT)
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


def opted_in(value) -> bool:
    """A yes/no setting that opts into something billed apart (usage_credits): true, or its spelling as text."""
    return value is True or str(value).strip().lower() in ("true", "1", "yes", "on")


def claude_report(events: list[dict]) -> dict | None:
    """The plan's usage a Claude Code session reports (its stream's last `rate_limit_event`), in the form `held` reads:
    each window's percent used and reset time. `status` and the top-level window say only how this one request went (a
    throttled request is `rejected` too): they are no refusal; `hermit.agent.base.cli` reads that from the failed
    result. `using_credits`: the session drew the account's usage credits, billed apart from the plan."""
    info = credits = None
    for event in events:
        if event.get("type") == "rate_limit_event" and isinstance(event.get("rate_limit_info"), dict):
            info = event["rate_limit_info"]
            if info.get("isUsingOverage") is True:     # a later request on the plan does not undo a drawn credit
                credits = info
    if info is None:
        return None
    windows = {}
    named = info.get("unifiedWindows")
    for name, window in (named.items() if isinstance(named, dict) else ()):
        try:
            windows[str(name)] = {"used_percent": float(window["utilization"]) * 100,
                                  "resets_at": window.get("resetsAt")}
        except (KeyError, TypeError, ValueError):       # a window that says nothing of its use: no limit to hold at
            continue
    return {"time": datetime.now(timezone.utc).isoformat(), "windows": windows, "status": info.get("status"),
            "resets_at": info.get("resetsAt"), "using_credits": credits is not None,
            **({"credits_resets_at": credits.get("resetsAt")} if credits is not None else {})}


def note_usage(plan: Path | str | None, home: Path | None, seen: dict | None = None) -> dict | None:
    """Keep a session's report of the plan's usage as plan.json when it is newer, and append it to plan.jsonl. The
    report is `seen` (one the caller read from the session's stream), else the one in the Codex home's session logs. A
    record that cannot be written (a full disk) is skipped: it never fails the session."""
    try:
        return _note_usage(plan, home, seen)
    except OSError:
        return None


def _note_usage(plan: Path | str | None, home: Path | None, seen: dict | None = None) -> dict | None:
    seen = seen or (usage_of(home) if home is not None else None)
    if not seen:
        return None
    d = plan_dir(plan)
    d.mkdir(parents=True, exist_ok=True)
    try:
        windows = _windows(seen)
    except (TypeError, ValueError, OverflowError):            # a report the session's stream made up
        return None
    used, resets = max(windows, key=lambda w: w[0]) if windows else (0.0, 0.0)
    fd = _own_fd(d / "plan.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    try:
        os.write(fd, (json.dumps({"time": seen.get("time"), "used_percent": used, "resets_at": resets,
                                  "reached": seen.get("rate_limit_reached_type")}) + "\n").encode())
    finally:
        os.close(fd)
    with _plan_lock(d):
        current = _report(d)
        if str(seen.get("time") or "") >= str(current.get("time") or ""):
            report = dict(seen)
            try:
                in_force = float(current.get("held_until") or 0) > time.time()
            except (TypeError, ValueError):
                in_force = False
            if in_force:
                # A hold lasts to its end: a later report comes from a session that started before it (none starts
                # while it holds), and says nothing of the refusal or the credits behind it.
                report.update(held_until=current["held_until"],
                              rate_limit_reached_type=current.get("rate_limit_reached_type") or "session_refused")
            _write_report(d, report)
    return seen


def refused(output: str) -> bool:
    """Whether a session's output reports the plan's usage limit (no answer: it is run again after the wait)."""
    return bool(LIMIT.search(output or ""))


def hold(plan: Path | str | None = None, until: float | None = None, why: str = "session_refused", *, cancel=None,
         max_s: float | None = None) -> None:
    """A session was refused for the plan's limit: hold every session back until `until` (a reset time the refusal
    named), else until a report says otherwise, or for an hour when no report says when the window resets. `why` is the
    hold's reason when the plan names none yet: usage_credits (a session drew the account's usage credits) holds only
    the sessions that may not draw them. If the hold cannot be written, this session waits five minutes itself rather
    than ask again at once (at most `max_s`, until `cancel` is set)."""
    try:
        _hold(plan, until, why)
    except OSError:
        pause = 300.0 if max_s is None else min(300.0, max(0.0, max_s))
        if cancel is not None:
            cancel.wait(pause)
        else:
            time.sleep(pause)


def _hold(plan: Path | str | None = None, until: float | None = None, why: str = "session_refused") -> None:
    d = plan_dir(plan)
    d.mkdir(parents=True, exist_ok=True)
    with _plan_lock(d):
        current = _report(d)
        now = time.time()
        try:
            full = [resets for used, resets in _windows(current) if used >= 100 and resets > now]
        except (TypeError, ValueError, OverflowError):
            full = []
        try:
            until = min(float(until), now + LONGEST_HOLD_S) if until and float(until) > now else None
        except (TypeError, ValueError):             # read from a stream: not a time
            until = None
        # A refusal outranks a credits hold: the credits hold lets the sessions that may draw credits through, a refusal
        # holds every session (a session allowed credits refused while a credits hold stands must wait, not rerun).
        prior = current.get("rate_limit_reached_type")
        current["rate_limit_reached_type"] = why if not prior or (prior == "usage_credits" and why != "usage_credits") \
            else prior
        # The refusal's own reset, else the full window's, else an hour; never sooner than a hold already in force (a
        # session that started before a long hold may end on a short window's refusal).
        try:
            in_force = float(current.get("held_until") or 0)
        except (TypeError, ValueError):
            in_force = 0.0
        in_force = min(in_force, now + LONGEST_HOLD_S) if math.isfinite(in_force) else 0.0
        current["held_until"] = max(until or (max(full) if full else now + 3600), in_force)
        _write_report(d, current)


# ---- what stops a run before its first session --------------------------------------------------------------------

def logins_of(data: dict, evolver_params: dict | None = None) -> list[tuple[str, str, str | None, bool, str | None]]:
    """Every subscription a run signs in to, from the normalized config: (who, login, the CLI's program if named,
    whether its sessions run in a container, with the image's CLI: the acting agent's with environment.image and the
    sandbox on, a model's through a CLI with isolation: container; never the learner's or its roles' (RRSI's, on the
    learner's CLI), which run in the evolver sandbox on this machine's CLI; the Codex login's shared copy if named,
    `login_root`). The program and the copy are the ones the sessions use: `bin`, or Harness²'s `claude_bin`."""
    params = (data.get("agent") or {}).get("params") or {}
    image = bool((data.get("environment") or {}).get("image")) and \
        bool((data.get("sandbox") or {}).get("enabled", True))          # agent_pipeline's container (ContainerAgent)
    learner = evolver_params or {}
    roles = learner.get("roles") if isinstance(learner.get("roles"), dict) else {}
    out = []
    for who, spec, contained in (("agent", params, image), ("agent's model", params.get("model"), None),
                                 ("llm", data.get("llm"), None), ("evolver", evolver_params, False),
                                 ("evolver's roles", {"bin": learner.get("bin"), "login_root": learner.get("login_root"),
                                                      **roles}, False)):
        if isinstance(spec, dict) and spec.get("login"):
            out.append((who, spec["login"], spec.get("bin") or spec.get("claude_bin"),
                        spec.get("isolation") == "container" if contained is None else contained,
                        spec.get("login_root")))
    return out


def evolver_problems(params: dict | None) -> list[str]:
    """What an agent Evolver's own sign-in cannot run, found before the first step rather than at each step's session
    (after its rollouts): a login that is not its CLI's (`backend`), a Claude login with no model name to run."""
    params = params or {}
    login = params.get("login")
    if not login:
        return []
    own = LOGINS.get(login)
    backend = {"claude-code": "claude"}.get(params.get("backend"), params.get("backend"))
    out = []
    if backend in NO_LOGIN:
        out.append(f"login: {login} is a Codex or Claude Code sign-in; the {backend} Evolver signs in through its "
                   "model reference (evolver.model: <backend>/<model>)")
    if own is not None and backend in ("codex", "claude") and own != backend:
        out.append(f"login: {login} is {'Codex' if own == 'codex' else 'Claude Code'}'s sign-in; the Evolver's "
                   f"sessions run {'Codex' if backend == 'codex' else 'Claude Code'}, which takes login: "
                   f"{'chatgpt' if backend == 'codex' else 'claude'}")
    if own == "claude" and not params.get("model"):
        out.append("login: claude runs the model it is given by name (a Claude Code model name): give "
                   "evolver.model.name")
    if own == "claude" and params.get("binding"):
        out.append("login: claude is the subscription, and a <backend>/<model> reference (evolver.model.ref) pays "
                   "with that backend's key, which every session on the login refuses: give evolver.model.name")
    return out


def problems_of(login: str, program: str | None = None, contained: bool = False,
                root: Path | str | None = None) -> list[str]:
    """What stops a session on `login` before any request: no sign-in (Codex's: the user's file or the shared copy under
    `root`, the session's `login_root`, as `prepare` reads them), no CLI, a CLI too old (the machine's; a session in a
    container runs the image's, which the agent checks before its first session). Nothing here calls the model, or
    reads a credential beyond whether it is there."""
    cli = LOGINS.get(login)
    if cli is None:
        return [f"login must be one of {sorted(LOGINS)}, not {login!r}"]
    if cli == "codex":
        return [] if source().is_file() or master(root).is_file() else \
            [f"login: chatgpt, but there is no Codex login at {source()} or {master(root)} (run `codex login`)"]
    out = [] if os.environ.get(TOKEN_ENV) else [NO_TOKEN]
    if contained:
        return out
    found = shutil.which(program or "claude")
    if found is None:
        return out + [f"login: claude needs the Claude Code CLI, and {program or 'claude'} is not on PATH"]
    try:
        text = subprocess.run([found, "--version"], capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.TimeoutExpired):
        text = ""
    version = re.match(r"\s*(\d+)\.(\d+)\.(\d+)", text)
    if version and tuple(map(int, version.groups())) < CLAUDE_MIN_VERSION:
        out.append(f"login: claude needs Claude Code {'.'.join(map(str, CLAUDE_MIN_VERSION))} or later, not "
                   f"{'.'.join(version.groups())} (`claude update`)")
    return out
