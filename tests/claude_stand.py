"""The stand-in `claude` the Claude subscription tests run (tests/core/test_claude_subscription.py, and the methods'
tests of their Claude routes): a script on PATH that replays streams the real CLI printed (tests/fixtures/claude_streams),
and records what each call was given. No test uses a real login, and none reaches a real host."""
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile

import pytest

from hermit.agent.base import login as subscription

ROOT = Path(__file__).resolve().parents[1]
STREAMS = ROOT / "tests" / "fixtures" / "claude_streams"
INDEX = json.loads((STREAMS / "index.json").read_text())
TOKEN = "fake-setup-token-never-real"


def bwrap_works() -> bool:
    """Whether bubblewrap runs here as the sandboxes run it (unprivileged user namespaces, a network of its own), the
    probe of the sandbox tests (tests/core/test_isolate.py)."""
    if not shutil.which("bwrap"):
        return False
    return subprocess.run(["bwrap", "--unshare-user", "--unshare-net", "--ro-bind", "/", "/", "true"],
                          capture_output=True).returncode == 0


FAKE_CLAUDE = f'''#!{sys.executable}
"""A stand-in for `claude -p`: keeps what it was given, then prints a stream the real CLI printed and exits as it did.
It reads what to do from script.json beside it, one entry per call (the last one repeats): {{"case": <a fixture>,
"text": <the reply's text>, "echo": true (the reply says what the session saw), "sleep": seconds, "edit": [path, text],
"resets_in": seconds, "stderr": text printed there first, "probe": a path whose listing it records, where it then tries
to write PAUSE and remove MAX_USED}}. A call it cannot number (its directory is read-only in a
sandbox) takes the first entry."""
import json, os, re, sys, time
from pathlib import Path
here = Path(__file__).resolve().parent
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print(os.environ.get("FAKE_VERSION") or (here / "version").read_text().strip() if (here / "version").exists() else "2.1.318 (Claude Code)")
    raise SystemExit(0)
prompt = sys.stdin.read()
script = json.loads((here / "script.json").read_text())
try:
    number = int((here / "count").read_text()) if (here / "count").exists() else 0
    (here / "count").write_text(str(number + 1))
except OSError:
    number = 0
entry = script[min(number, len(script) - 1)]
home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or ".")
system = None
if "--system-prompt-file" in args:
    system = Path(args[args.index("--system-prompt-file") + 1]).read_text()
elif "--system-prompt" in args:
    system = args[args.index("--system-prompt") + 1]
secret = lambda name: os.environ.get(name) is not None
saw = {{"args": args, "prompt": prompt, "cwd": os.getcwd(), "system": system, "home": sorted(p.name for p in home.iterdir()),
       "work": sorted(p.name for p in Path(".").iterdir()),
       "env": {{k: v for k, v in os.environ.items() if k.startswith(("ANTHROPIC", "CLAUDE"))}},
       "visible": {{p: os.path.exists(os.path.expanduser(p)) for p in ("~/.claude", "~/.ssh")}},
       "user_login": Path((here / "user_login").read_text()).exists() if (here / "user_login").exists() else None,
       "credentials": sorted(str(p) for p in Path(os.environ.get("HOME", ".")).rglob(".credentials.json"))}}
if entry.get("probe"):        # what the session's process finds at a path, whether it can pause or lift a plan there
    place = Path(entry["probe"])
    found = {{"listing": sorted(p.name for p in place.iterdir()) if place.is_dir() else None}}
    for name, act in (("pause", lambda: (place / "PAUSE").write_text("forged")),
                      ("lift", lambda: (place / "MAX_USED").unlink())):
        try:
            act()
            found[name] = "done"
        except OSError as exc:
            found[name] = type(exc).__name__
    saw["probe"] = found
try:
    with open(here / "seen.jsonl", "a") as out:
        out.write(json.dumps(saw) + "\\n")
except OSError:
    pass
(home / "seen.json").write_text(json.dumps(saw))
if entry.get("edit"):
    path = Path(entry["edit"][0])
    path.write_text((path.read_text() if path.exists() else "") + entry["edit"][1])
if entry.get("sleep"):
    time.sleep(entry["sleep"])
if entry.get("leave"):
    for name in entry["leave"]:
        Path(name).write_text("left by the session")
if entry.get("stderr"):
    print(entry["stderr"], file=sys.stderr, flush=True)
case = entry.get("case", "reply")
stream = Path(os.environ["FAKE_STREAMS"]) if os.environ.get("FAKE_STREAMS") else here / "streams"
index = json.loads((stream / "index.json").read_text())
text = json.dumps(saw) if entry.get("echo") else entry.get("text")
for line in (stream / f"{{case}}.jsonl").read_text().splitlines():
    try:
        event = json.loads(line)
    except ValueError:
        print(line)
        continue
    if text is not None and event.get("type") == "assistant":
        for block in event["message"]["content"]:
            if block.get("type") == "text":
                block["text"] = text
    if text is not None and event.get("type") == "result" and not event.get("is_error"):
        event["result"] = text
    if event.get("type") == "system" and event.get("subtype") == "init" and "--restricted" in args:
        event.update(tools=entry.get("tools", []), mcp_servers=[])       # as the real CLI does under the flags
    if "--output-format" in args and args[args.index("--output-format") + 1] == "json" and event.get("type") != "result":
        continue                                                        # one object: the result alone
    out = json.dumps(event)
    if entry.get("resets_in"):
        out = re.sub(r'("(?:resetsAt|overageResetsAt)": )\\d+', lambda m: m.group(1) + str(int(time.time()) + entry["resets_in"]), out)
    print(out, flush=True)
if entry.get("exit_after"):
    raise SystemExit(entry["exit_after"])
raise SystemExit(index[case]["exit"])
'''


class Stand:
    """The stand-in on PATH, for one test: `play(...)` says what its calls do, `seen()` what they were given."""

    def __init__(self, directory: Path):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        exe = self.dir / "claude"
        exe.write_text(FAKE_CLAUDE)
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
        shutil.copytree(STREAMS, self.dir / "streams", dirs_exist_ok=True)
        self.play("reply")

    def play(self, *entries):
        """Each call's entry: a fixture's name, or the entry itself."""
        (self.dir / "script.json").write_text(json.dumps([{"case": e} if isinstance(e, str) else e for e in entries]))
        (self.dir / "count").unlink(missing_ok=True)
        (self.dir / "seen.jsonl").unlink(missing_ok=True)

    def seen(self) -> list[dict]:
        path = self.dir / "seen.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.fixture
def stand(tmp_path, monkeypatch):
    """The stand-in first on PATH (never the real CLI: the assert), the user's token set, every other credential a shell
    could hold beside it, and a home with a real-looking login that must stay out of every session."""
    stand = Stand(tmp_path / "standin")
    # a session's stream without a log is kept in the temporary directory: one of the test's own, at a short path (a
    # sandbox's sockets live there too, and a socket's path is short), removed after the test
    short = Path(tempfile.mkdtemp(prefix="hst-"))
    monkeypatch.setattr(tempfile, "tempdir", str(short))
    monkeypatch.setenv("PATH", f"{stand.dir}{os.pathsep}{os.environ['PATH']}")
    assert shutil.which("claude") == str(stand.dir / "claude")
    home = tmp_path / "user-home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / ".credentials.json").write_text('{"claudeAiOauth": {"accessToken": "user-file-token"}}')
    (stand.dir / "user_login").write_text(str(home / ".claude" / ".credentials.json"))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv(subscription.TOKEN_ENV, TOKEN)
    for name, value in (("ANTHROPIC_API_KEY", "sk-would-bill-the-key"), ("ANTHROPIC_AUTH_TOKEN", "would-bill-too"),
                        ("ANTHROPIC_BASE_URL", "https://elsewhere.example"), ("CLAUDE_CODE_USE_BEDROCK", "1"),
                        ("CLAUDECODE", "1"), ("CLAUDE_CODE_SESSION_ID", "parent")):
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(subscription, "PLAN", tmp_path / "codex-plan")
    monkeypatch.setattr(subscription, "CLAUDE_PLAN", tmp_path / "plan")
    yield stand
    shutil.rmtree(short, ignore_errors=True)


def roles_ctx(tmp_path, params=None):
    """A learner's context for a method's roles on the login: the workspace's folders, the login's settings."""
    from types import SimpleNamespace as NS
    ws = NS(scratch=tmp_path / "scratch", output=tmp_path / "out", state=tmp_path / "state", adapter=tmp_path / "adapter",
            learning_material=tmp_path / "lm", history=tmp_path / "hist", task_info=tmp_path / "info")
    for d in (ws.scratch, ws.output, ws.state, ws.adapter, ws.learning_material, ws.history, ws.task_info):
        d.mkdir(parents=True, exist_ok=True)
    return NS(params={"backend": "claude", "login": "claude", "model": "m", "web_search": False, **(params or {})},
              workspace=ws, sandbox=None, cancel=None)
