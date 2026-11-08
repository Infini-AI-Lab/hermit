"""MWM's learner on the Evolver's CLI with `backend: claude` on the subscription's login (docs/guides/claude-subscription.md):
a whole training against a stand-in `claude` that does what an MWM session does. The shared login path is tested in
tests/core/test_claude_subscription.py."""
import json
import shutil

import pytest
import yaml

from hermit.agent.base import login as subscription
from hermit.config import Config

TOKEN = "fake-setup-token-never-real"
NEEDS_BWRAP = pytest.mark.skipif(not shutil.which("bwrap"), reason="needs bubblewrap")


FAKE_MWM_CLAUDE = r'''#!/usr/bin/env python3
"""The MWM learner on Claude Code: it writes a note in its working directory, the world model, and reports one command."""
import json, os, sys
from pathlib import Path
if sys.argv[1:2] == ["--version"]:
    print("2.1.318 (Claude Code)")
    raise SystemExit(0)
prompt = sys.stdin.read()
kind = "bootstrap" if prompt.split("\n\n", 1)[1].startswith("BOOTSTRAP SESSION") else "review"
n = len(list(Path("memory").glob("note*.md")))
Path("memory", f"note{n}.md").write_text(
    f"last_confirmed_t: {n}\nscope: always\nderived_from: none (reasoning only)\nbrief: {kind} note {n}\n\nA fact.\n")
Path("meta", "claude.json").write_text(json.dumps({"args": sys.argv[1:], "cwd": os.getcwd(),
                                                   "token": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"),
                                                   "tools": os.environ.get("MW_TOOLSRV", "")}))
print(json.dumps({"type": "system", "subtype": "init", "session_id": "s", "tools": ["Bash"], "apiKeySource": "none"}))
if not os.environ.get("FAKE_NO_ACTION"):
    print(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]}}))
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "done", "num_turns": 2,
                  "session_id": "s", "usage": {"input_tokens": 100, "cache_read_input_tokens": 40, "output_tokens": 10}}))
'''


@NEEDS_BWRAP
def test_the_mwm_learner_runs_on_the_evolvers_cli(tmp_path, monkeypatch):
    """A whole training of MWM with `backend: claude`, in its native mounts: the sessions take the login, work in /w, and
    are adopted by what the standard steps say they did (Codex's events are Codex's)."""
    import test_mwm_evolver as M
    from hermit.trainer.trainer import Trainer
    monkeypatch.setenv(subscription.TOKEN_ENV, TOKEN)
    monkeypatch.setattr(subscription, "CLAUDE_PLAN", tmp_path / "plan")
    path = M.make(tmp_path, monkeypatch)
    data = yaml.safe_load(path.read_text())
    data["evolver"]["params"].update(backend="claude", login="claude", model="claude-haiku-5-5", effort="low")
    path.write_text(yaml.safe_dump(data))
    (tmp_path / "fakebin" / "claude").write_text(FAKE_MWM_CLAUDE)
    (tmp_path / "fakebin" / "claude").chmod(0o755)
    Trainer(Config.load(path), log=lambda *_: None).train()
    run = tmp_path / "runs" / "mwm-test"
    steps = [json.loads(l) for l in (run / "steps.jsonl").read_text().splitlines()]
    assert [s["evolver"]["status"] for s in steps] == ["done", "done", "done"]
    sessions = json.loads((run / "step_0001" / "output" / "sessions.json").read_text())
    assert sessions[0]["status"] == "adopted" and sessions[0]["tool_calls"] == 1 and sessions[0]["turns"] == 2
    assert (run / "step_0001" / "output" / "review_0001.claude.jsonl").is_file()
    assert steps[1]["evolver"]["usage"][0]["input_tokens"] == 140
    saw = json.loads((run / "checkout" / "meta" / "claude.json").read_text())
    # The session had the token's value; the tree keeps its marker, as it keeps no key a learner held (hermit/redact.py)
    assert saw["cwd"] == "/w" and saw["token"] == f"<{subscription.TOKEN_ENV}>" and "--model" in saw["args"]
    assert saw["args"][saw["args"].index("--effort") + 1] == "low" and saw["args"][-2] == "--settings"


def test_mwm_keeps_an_unlimited_plan_wait(tmp_path, monkeypatch):
    """plan_max_wait_s: null is no limit, as for every other learner: it reaches the session, not the default."""
    from types import SimpleNamespace
    from hermit.agent.evolver.methods.mwm.evolver import MwmEvolver
    evolver = MwmEvolver.__new__(MwmEvolver)
    monkeypatch.setattr(MwmEvolver, "_mcp_tools", lambda self, ctx: [])
    ctx = SimpleNamespace(params={"web_search": False, "login": "claude", "plan_max_wait_s": None}, cancel=None)
    settings = evolver._settings(ctx, tmp_path / "home", tmp_path / "log", None)
    assert "plan_max_wait_s" in settings and settings["plan_max_wait_s"] is None


def test_mwm_sessions_use_the_login_root_its_preflight_checked(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from hermit.agent.evolver.methods.mwm.evolver import MwmEvolver
    monkeypatch.setattr(MwmEvolver, "_mcp_tools", lambda self, ctx: [])
    ctx = SimpleNamespace(params={"web_search": False, "login": "chatgpt", "login_root": str(tmp_path / "root")},
                          cancel=None)
    settings = MwmEvolver.__new__(MwmEvolver)._settings(ctx, tmp_path / "home", tmp_path / "log", None)
    assert settings["login_root"] == str(tmp_path / "root")
