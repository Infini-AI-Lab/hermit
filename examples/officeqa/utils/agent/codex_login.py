"""Codex on a ChatGPT login (a Codex plan) instead of an API key, for the OfficeQA Codex harnesses and the learner relay.

A model is on the login when its backend's base URL is ChatGPT's Codex endpoint, e.g. in a run config

    backends:
      codexplan: {base_url: "https://chatgpt.com/backend-api/codex", key_required: false, apis: [chat, responses]}
    agent: {params: {model: codexplan/gpt-5.6-luna}}

Codex then uses its built-in OpenAI provider, which on a ChatGPT login talks to chatgpt.com as the machine's own Codex
does; no provider is configured and no key is passed. Each session's fresh CODEX_HOME gets a copy of the login
(OFFICEQA_CODEX_AUTH, default ~/.codex/auth.json), taken when the session starts and removed when it ends. The egress
proxy for these sessions lets through chatgpt.com only, not auth.openai.com: no session can refresh (and so rotate) the
login the machine's own Codex holds, so the login's access token must outlive the run (`python codex_login.py`
prints when it expires). Web search, apps, plugins and the browser and computer tools are off.

The plan's usage is shared with the machine's own Codex. Every session reports the plan's usage in its CODEX_HOME's
session log; `note_usage` keeps the latest report in OFFICEQA_CODEX_PLAN (default
~/albz/officeqa-luna/plan.json). There is no cap of our own by default: `wait_for_plan` holds a new session back
only while the file named by OFFICEQA_CODEX_PAUSE exists (default ~/albz/officeqa-luna/PAUSE), after a session was
refused for the plan's limit (until the plan's window resets), or, if one is set, while the report is at or above
`max_used()` percent of the plan's window (the file MAX_USED beside it, else OFFICEQA_CODEX_MAX_USED). A session
refused for the plan's limit is reported by `limit_hit` and is run again after the wait, never scored.

The login also has a rate limit: past some number of sessions at once its backend answers 429, and Codex retries
with backoff. A session of SkillOpt's harness has 180 s, so those waits become timeouts and wrong answers. `slot()`
holds one of the machine's Codex session slots: SLOTS beside PLAN (a number, read at every try; else
OFFICEQA_CODEX_SLOTS; 0 or none: no limit), shared by every process that takes one (a lock file each under slots/,
released when its holder ends, however it ends). It decides only how many sessions run at once, not what any of them
does.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import random
import re
import sys
import threading
import time

LOGIN_URL = "https://chatgpt.com/backend-api/codex"
AUTH = Path(os.path.expanduser(os.environ.get("OFFICEQA_CODEX_AUTH", "~/.codex/auth.json")))
PLAN = Path(os.path.expanduser(os.environ.get("OFFICEQA_CODEX_PLAN", "~/albz/officeqa-luna/plan.json")))
PAUSE = Path(os.path.expanduser(os.environ.get("OFFICEQA_CODEX_PAUSE", "~/albz/officeqa-luna/PAUSE")))
MAX_USED = float(os.environ.get("OFFICEQA_CODEX_MAX_USED", "inf"))    # a file MAX_USED beside PLAN overrides it
# what reaches data outside the workspace: hosted web search, ChatGPT apps (connectors), plugins and their marketplace,
# the browser and computer tools
OFF = ("apps", "plugins", "remote_plugin", "browser_use", "browser_use_external", "computer_use", "in_app_browser",
       "skill_mcp_dependency_install")
# Codex's own words for a refusal on the plan's limit ("You've hit your usage limit. ..."), and the error's codes
LIMIT = re.compile(r"hit your usage limit|usage_limit_reached|usageLimitExceeded", re.I)
_lock = threading.Lock()


def max_used() -> float:
    """The usage (percent of the plan's window) at which new sessions wait: the file MAX_USED beside PLAN, if any,
    else OFFICEQA_CODEX_MAX_USED (default: none). The file can be changed while runs go on."""
    try:
        return float((PLAN.parent / "MAX_USED").read_text().strip())
    except (OSError, ValueError):
        return MAX_USED


def is_login(binding) -> bool:
    """Whether a backend binding names ChatGPT's Codex endpoint (the Codex plan) rather than an API."""
    return str(getattr(binding, "base_url", "") or "").rstrip("/") == LOGIN_URL


def slot_count() -> int:
    """How many Codex sessions may run at once on this machine: the file SLOTS beside PLAN, else OFFICEQA_CODEX_SLOTS;
    0: no limit."""
    try:
        return int((PLAN.parent / "SLOTS").read_text().strip())
    except (OSError, ValueError):
        return int(os.environ.get("OFFICEQA_CODEX_SLOTS", "0") or 0)


@contextmanager
def slot(log=None):
    """Hold one of the machine's Codex session slots while the block runs (none when there is no limit)."""
    told = False
    while True:
        n = slot_count()
        if n <= 0:
            yield None
            return
        locks = PLAN.parent / "slots"
        locks.mkdir(parents=True, exist_ok=True)
        for i in random.sample(range(n), n):
            fd = os.open(locks / f"{i}.lock", os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                continue
            try:
                yield i
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
            return
        if not told and log is not None:
            log(f"waiting for one of the {n} Codex session slots")
            told = True
        time.sleep(2 + random.random() * 2)


def config_lines() -> list[str]:
    """config.toml lines for a session on the login: the built-in provider on the file login, no web search, nothing
    that reaches outside the workspace, no update check."""
    return ['forced_login_method = "chatgpt"', 'cli_auth_credentials_store = "file"', 'web_search = "disabled"',
            "check_for_update_on_startup = false", *(f"features.{name} = false" for name in OFF)]


def install_auth(home: Path) -> None:
    """A copy of the login into a session's CODEX_HOME (owner-only); retried while the machine's Codex rewrites it."""
    home = Path(home)
    for _ in range(10):
        try:
            data = AUTH.read_bytes()
            if json.loads(data).get("auth_mode") == "chatgpt":
                break
        except (OSError, ValueError):
            pass
        time.sleep(1)
    else:
        raise RuntimeError(f"no ChatGPT login in {AUTH}: `codex login` on this machine first")
    target = home / "auth.json"
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(data)


def remove_auth(home: Path) -> None:
    (Path(home) / "auth.json").unlink(missing_ok=True)


def usage_of(home: Path) -> dict | None:
    """The plan's usage the latest session log under this CODEX_HOME reports (Codex's rate_limits), or None."""
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


def note_usage(home: Path) -> dict | None:
    """Keep the session's report of the plan's usage in PLAN when it is newer than the one there; every report is
    also appended to plan.jsonl beside it (the usage over time)."""
    seen = usage_of(home)
    if not seen:
        return None
    used, resets = _used(seen)
    line = json.dumps({"time": seen.get("time"), "used_percent": used, "resets_at": resets,
                       "reached": seen.get("rate_limit_reached_type"), "home": str(home)}) + "\n"
    PLAN.parent.mkdir(parents=True, exist_ok=True)
    with open(PLAN.with_suffix(".jsonl"), "a") as out:
        out.write(line)
    with _lock:
        try:
            current = json.loads(PLAN.read_text())
        except (OSError, ValueError):
            current = {}
        if str(seen.get("time") or "") >= str(current.get("time") or ""):
            PLAN.parent.mkdir(parents=True, exist_ok=True)
            tmp = PLAN.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
            tmp.write_text(json.dumps(seen, indent=1))
            os.replace(tmp, PLAN)
    return seen


def _used(report: dict) -> tuple[float, float]:
    """(percent used, reset time) of the report's fullest window."""
    windows = [w for w in (report.get("primary"), report.get("secondary")) if isinstance(w, dict)]
    if not windows:
        return 0.0, 0.0
    w = max(windows, key=lambda w: float(w.get("used_percent") or 0))
    return float(w.get("used_percent") or 0), float(w.get("resets_at") or 0)


def wait_for_plan(log=None) -> float:
    """Block while the run is paused or the plan's usage is at or above MAX_USED; returns the seconds waited."""
    started, told = time.time(), False
    while True:
        reason = ""
        if PAUSE.exists():
            reason = f"paused ({PAUSE} exists)"
        else:
            try:
                report = json.loads(PLAN.read_text())
            except (OSError, ValueError):
                report = {}
            used, resets = _used(report)
            limit = max_used()
            if report.get("rate_limit_reached_type") or used >= limit:
                if resets and time.time() >= resets:
                    pass                     # the window has reset: the next session's report will say so
                else:
                    reason = (f"plan usage {used:.0f}% (limit {limit:.0f}%"
                              f"{', refused' if report.get('rate_limit_reached_type') else ''}) until "
                              f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(resets))}")
        if not reason:
            return time.time() - started
        if not told and log is not None:
            log(f"codex sessions held back: {reason}")
            told = True
        time.sleep(60)


def limit_hit(text: str) -> bool:
    """Whether a session's output reports the plan's usage limit (not an answer: run it again after the wait)."""
    return bool(LIMIT.search(text or ""))


def hold_for_limit(log=None) -> None:
    """A session hit the plan's limit: hold every session back until the next one's report says otherwise."""
    with _lock:
        try:
            current = json.loads(PLAN.read_text())
        except (OSError, ValueError):
            current = {}
        current["rate_limit_reached_type"] = current.get("rate_limit_reached_type") or "session_refused"
        current.setdefault("primary", {}).setdefault("resets_at", time.time() + 3600)
        PLAN.parent.mkdir(parents=True, exist_ok=True)
        PLAN.write_text(json.dumps(current, indent=1))
    if log is not None:
        log("a codex session hit the plan's usage limit; holding sessions back")


def auth_status() -> dict:
    """When the login was refreshed and when its access token expires (no token is printed)."""
    import base64
    data = json.loads(AUTH.read_text())
    token = ((data.get("tokens") or {}).get("access_token") or "").split(".")
    claims = {}
    if len(token) == 3:
        claims = json.loads(base64.urlsafe_b64decode(token[1] + "=" * (-len(token[1]) % 4)))
    return {"auth_mode": data.get("auth_mode"), "last_refresh": data.get("last_refresh"),
            "access_token_expires": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(claims["exp"]))
            if "exp" in claims else None}


if __name__ == "__main__":
    print(json.dumps({"auth": auth_status(), "plan": json.loads(PLAN.read_text()) if PLAN.is_file() else None,
                      "paused": PAUSE.exists(), "max_used": max_used()}, indent=1))
    sys.exit(0)
