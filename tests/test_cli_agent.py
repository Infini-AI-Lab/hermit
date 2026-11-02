"""The framework's Codex agent (hermit/agent/acting/workspace.py) against fake CLIs: the task's hooks, what a run records, the
ChatGPT login, retries, mounts, the container's command line; and hermit.agent.base.trajectory, the one reader of the hosts'
logs, with the views the Evolvers build from it."""
import json
import os
from pathlib import Path
import stat
import sys

import pytest

from hermit.agent.base import trajectory
from hermit.agent.acting import workspace as cli
from hermit.agent.base import cli as base_cli
from hermit.agent.base import container, login
from hermit.agent.base.interface import make

ROOT = Path(__file__).resolve().parents[1]
PROMPT_ONLY = ROOT / "hermit" / "adapter" / "templates" / "prompt_only"

EVENTS = [
    {"type": "thread.started", "thread_id": "th1"},
    {"type": "item.completed", "item": {"type": "reasoning", "text": "Find the 1944 bulletin."}},
    {"type": "item.completed", "item": {"type": "command_execution", "command": "cat task.md", "exit_code": 0,
                                        "aggregated_output": "What were the receipts?"}},
    {"type": "item.completed", "item": {"type": "command_execution", "command": "rg receipts docs/", "exit_code": 1,
                                        "aggregated_output": "no match"}},
    {"type": "item.completed", "item": {"type": "agent_message", "text": "<answer>44,463</answer>"}},
    {"type": "turn.completed", "usage": {"input_tokens": 1000, "cached_input_tokens": 200, "output_tokens": 50}},
]

FAKE_CODEX = f'''#!{sys.executable}
"""A stand-in for `codex exec --json ... -o LAST PROMPT`: records its arguments, CODEX_HOME and what it saw; fails
like an unavailable provider while FAKE_UNAVAILABLE counts down; refreshes a login it finds."""
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
last = Path(args[args.index("-o") + 1])
home = Path(os.environ["CODEX_HOME"])
state = Path(os.environ["FAKE_STATE"])
calls = int(state.read_text()) if state.exists() else 0
state.write_text(str(calls + 1))
(home / "seen.json").write_text(json.dumps({{"args": args, "cwd": os.getcwd(), "home": sorted(p.name for p in home.iterdir()),
                                            "task": Path("task.md").read_text() if Path("task.md").exists() else None,
                                            "docs": sorted(p.name for p in Path("docs").iterdir()) if Path("docs").exists() else None}}))
if calls < int(os.environ.get("FAKE_UNAVAILABLE", "0")):
    print("ERROR: unexpected status 429 Too Many Requests")
    sys.exit(1)
if (home / "auth.json").exists():
    auth = json.loads((home / "auth.json").read_text())
    auth["last_refresh"] = "2026-10-30T12:00:00Z"
    (home / "auth.json").write_text(json.dumps(auth))
for e in json.loads({json.dumps(json.dumps(EVENTS))}):
    print(json.dumps(e))
last.write_text("Done.\\n<answer>44,463</answer>")
'''


@pytest.fixture
def fake(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "codex"
    exe.write_text(FAKE_CODEX)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_STATE", str(tmp_path / "calls"))
    return tmp_path


def seed(tmp_path: Path) -> Path:
    import shutil
    adapter = tmp_path / "adapter"
    shutil.copytree(PROMPT_ONLY, adapter, ignore=shutil.ignore_patterns("__pycache__"))
    (adapter / "ADAPTER.md").write_text("# Skill\n\nAnswer with the bare number.\n")
    return adapter


HOOKS = '''
from pathlib import Path
def prepare(task, workspace, params):
    (workspace / "task.md").write_text(f"Q: {task['question']} ({params['flavour']})")
    return [{"source": params["corpus"], "at": "docs/root_1"}]
def answer(last_message, task):
    return {"answer": last_message.rsplit("<answer>", 1)[-1].split("</answer>")[0], "id": task["id"]}
def collect(task, workspace, log):
    (log / "task.md").write_text((workspace / "task.md").read_text())
    return {"collected": True}
'''


def test_a_run_with_the_tasks_hooks_records_the_hosts_log(fake):
    tmp_path = fake
    (tmp_path / "hooks.py").write_text(HOOKS)
    (tmp_path / "prompt.md").write_text("Read task.md about {question}.\n")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "bulletin.txt").write_text("receipts")
    params = {"web_search": False, "prompt": "prompt.md", "prepare": "hooks.py:prepare", "answer": "hooks.py:answer",
              "collect": "hooks.py:collect", "effort": "medium", "workspaces": "ws",
              "model": "m1", "flavour": "plain", "corpus": str(corpus), "price": {"input": 1, "output": 2}}
    out = cli.run({"id": "q1", "question": "receipts"}, seed(tmp_path), params=params, base=tmp_path)
    log, work = out.log, Path(out.extra["workspace"])
    seen = json.loads((work.parent / "home" / "seen.json").read_text())
    assert seen["task"] == "Q: receipts (plain)" and seen["docs"] == ["root_1"] and seen["cwd"] == str(work)
    assert "-m" in seen["args"] and 'model_reasoning_effort="medium"' in seen["args"]
    assert seen["args"][-1] == "Read task.md about receipts."                     # the template, its newline gone
    agents_md = (work / "AGENTS.md").read_text()                             # as `hermit deploy` writes it
    assert "This adapter's files are in `hermit/prompt-only/`." in agents_md and "Answer with the bare number." in agents_md
    assert out.output == {"answer": "44,463", "id": "q1", "fail_reason": ""}
    assert sorted(p.name for p in log.iterdir()) == ["agent-codex.jsonl", "deployment.json", "last_message.txt",
                                                     "prompt.txt", "task.md"]
    assert not (work / "docs").exists()                                    # the mount's link does not outlive the run
    extra = out.extra
    assert (extra["backend"], extra["deploy"], extra["isolation"], extra["exit_code"], extra["timed_out"],
            extra["attempts"], extra["task"]) == ("codex", "host", "none", 0, False, 1, {"collected": True})
    assert out.tokens == 1050 and out.cost_usd == pytest.approx((1000 * 1 + 50 * 2) / 1e6)   # cached at the input price
    steps = trajectory.log_steps(log)
    assert [s.kind for s in steps] == ["reasoning", "command", "command", "message"]


def test_the_answer_pattern_and_why_there_is_none(fake):
    tmp_path = fake
    params = {"web_search": False, "prompt": "go", "answer": "<answer>(.*?)</answer>", "workspaces": "ws"}
    out = cli.run({"id": "q1"}, None, params=params, base=tmp_path)
    assert out.output == {"answer": "44,463", "fail_reason": ""}
    out = cli._output("no tags", {}, {"answer": "<answer>(.*?)</answer>"}, tmp_path, 0, False, 60)
    assert out == {"answer": "", "fail_reason": "no answer in the final message"}
    assert "timed out after 60 s" in cli._output("", {}, {"answer": "x(.)"}, tmp_path, -1, True, 60)["fail_reason"]
    assert cli._output("plain", {}, {}, tmp_path, 0, False, 60) == "plain"           # without `answer`: the message


def test_a_provider_that_could_not_serve_is_run_again(fake, monkeypatch):
    tmp_path = fake
    monkeypatch.setenv("FAKE_UNAVAILABLE", "1")
    out = cli.run({"id": "q1"}, None, params={"web_search": False, "prompt": "go", "workspaces": "ws", "retry_wait_s": 0}, base=tmp_path)
    assert out.extra["attempts"] == 2 and out.output.endswith("<answer>44,463</answer>")
    monkeypatch.setenv("FAKE_UNAVAILABLE", "9")
    (tmp_path / "calls").unlink()
    with pytest.raises(RuntimeError, match="could not be served after 2 attempts"):
        cli.run({"id": "q2"}, None, params={"web_search": False, "prompt": "go", "workspaces": "ws", "retries": 2, "retry_wait_s": 0},
                base=tmp_path)


def test_a_cli_that_never_started_is_an_error_not_a_wrong_answer(fake, monkeypatch):
    tmp_path = fake
    (tmp_path / "bin" / "codex").write_text(f"#!{sys.executable}\nimport sys\nprint('Error finding codex home')\nsys.exit(1)\n")
    with pytest.raises(RuntimeError, match="before starting a session: Error finding codex home"):
        cli.run({"id": "q1"}, None, params={"web_search": False, "prompt": "go", "workspaces": "ws", "answer": "x(.)"}, base=tmp_path)


def test_the_chatgpt_login_is_copied_refreshed_back_and_removed(fake, monkeypatch):
    tmp_path = fake
    user = tmp_path / "user-codex"
    user.mkdir()
    (user / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt", "last_refresh": "2026-10-01T00:00:00Z"}))
    monkeypatch.setenv("CODEX_HOME", str(user))
    params = {"web_search": False, "prompt": "go", "workspaces": "ws", "login": "chatgpt", "model": "gpt-6-luna"}
    out = cli.run({"id": "q1"}, None, params=params, base=tmp_path)
    home = Path(out.extra["workspace"]).parent / "home"
    seen = json.loads((home / "seen.json").read_text())
    assert "auth.json" in seen["home"] and not (home / "auth.json").exists()      # it does not outlive the run
    config = (home / "config.toml").read_text()
    assert 'forced_login_method = "chatgpt"' in config and 'web_search = "disabled"' in config
    master = json.loads(login.master().read_text())                               # the shared copy
    assert master["last_refresh"] == "2026-10-30T12:00:00Z"                       # the session's refresh came back
    assert json.loads((user / "auth.json").read_text())["last_refresh"] == "2026-10-01T00:00:00Z"   # never written
    assert out.cost_usd is None


def test_web_search_is_set_explicitly_either_way(fake, tmp_path):
    from hermit.agent.base.cli import web_search_args
    assert web_search_args("codex", False) == ["-c", 'web_search="disabled"']
    assert web_search_args("codex", True) == ["-c", 'web_search="live"']
    assert web_search_args("claude", False) == ["--disallowedTools", "WebSearch,WebFetch"]
    assert web_search_args("claude", True) == []
    for enabled, value in ((False, "disabled"), (True, "live")):
        home = tmp_path / f"home-{value}"
        home.mkdir()
        base_cli.write_home(home, {"web_search": enabled}, [])
        assert f'web_search = "{value}"' in (home / "config.toml").read_text()
    out = cli.run({"id": "q1"}, None, params={"web_search": False, "prompt": "go", "workspaces": "ws"}, base=fake)
    assert out.extra["web_search"] is False
    assert "--disallowedTools" in base_cli.Claude({"web_search": False}).command("p", fake, fake / "last")


def test_what_does_not_combine_is_refused(tmp_path):
    for params, match in [({"login": "chatgpt"}, "Codex model name"),
                          ({"login": "chatgpt", "model": "m", "binding": {"ref": "fw/m"}}, "Codex model name"),
                          ({"login": "api"}, "chatgpt or absent"),
                          ({"backend": "claude-code", "login": "chatgpt", "model": "m"}, "Codex's sign-in"),
                          ({"backend": "claude-code", "isolation": "container"}, "runs Codex"),
                          ({"deploy": "adapter"}, "deploy must be one of"),
                          ({"deploy": "tree"}, "deploy: tree is gone"),
                          ({"deploy": "files"}, "deploy: files is gone"),
                          ({"isolation": "container", "container": {"img": "x"}}, "unknown keys")]:
        with pytest.raises(ValueError, match=match):
            cli._check("codex", {"web_search": False, **params})
    for value in (None, "no"):                         # web search is set explicitly, never left to a default
        with pytest.raises(ValueError, match="web_search must be set"):
            cli._check("codex", {} if value is None else {"web_search": value})


def test_login_without_a_login_is_an_error(tmp_path):
    with pytest.raises(RuntimeError, match="no Codex login"):
        login.prepare(tmp_path / "ws", tmp_path, user_file=tmp_path / "missing.json")


FAKE_DOCKER = f'''#!{sys.executable}
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
if args[0] in ("image", "network"):
    sys.exit(0)
Path(os.environ["FAKE_DOCKER_ARGS"]).write_text(json.dumps({{"args": args, "key": os.environ.get("FW_KEY")}}))
script = next(i for i, a in enumerate(args) if a.endswith('exec "$@"'))     # sh -c SCRIPT sh COMMAND...
command = args[script + 2:]
last = command[command.index("-o") + 1]
Path(last).write_text("<answer>7</answer>")
print(json.dumps({{"type": "turn.completed", "usage": {{"input_tokens": 1, "output_tokens": 1}}}}))
'''


def test_a_container_session_binds_the_workspace_the_home_and_the_mounts(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "docker"
    exe.write_text(FAKE_DOCKER)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DOCKER_ARGS", str(tmp_path / "docker.json"))
    monkeypatch.setenv("FW_KEY", "secret-value")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    params = {"web_search": False, "prompt": "go", "workspaces": "ws", "isolation": "container", "answer": "<answer>(.*?)</answer>",
              "container": {"image": "img:1", "network": "net", "proxy": "http://px:3128"},
              "mounts": [{"source": str(corpus), "at": "docs"}, {"source": str(corpus)}],
              "binding": {"backend": "fw", "model": "m", "base_url": "https://x/v1", "api_key_env": "FW_KEY",
                          "apis": ["responses"]}}
    base_cli._ready.clear()
    out = cli.run({"id": "q1"}, None, params=params, base=tmp_path)
    seen = json.loads((tmp_path / "docker.json").read_text())
    args, work = seen["args"], Path(out.extra["workspace"])
    assert out.output == {"answer": "7", "fail_reason": ""} and seen["key"] == "secret-value"
    assert args[args.index("--network") + 1] == "net" and "img:1" in args and "HTTPS_PROXY=http://px:3128" in args
    assert "secret-value" not in " ".join(args) and "FW_KEY" in args                  # the key by name only
    binds = [args[i + 1] for i, a in enumerate(args) if a == "--mount"]
    assert f"type=bind,src={corpus.resolve()},dst={work / 'docs'},readonly" in binds
    assert f"type=bind,src={corpus.resolve()},dst={corpus.resolve()},readonly" in binds
    assert f"type=bind,src={work},dst={work}" in binds
    check = next(a for a in args if a.endswith('exec "$@"'))
    assert str(work / "docs") in check and container.MISSING in check
    assert not (work / "docs").exists()                                            # the empty mount point is gone


def test_without_a_container_a_mount_is_a_link(tmp_path):
    source = tmp_path / "data"
    source.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    mounts = cli._mounts({}, work, {"mounts": [{"source": "data", "at": "in/data"}]}, tmp_path)
    base_cli._link(mounts)
    assert (work / "in" / "data").resolve() == source.resolve()
    (source / "empty").mkdir()
    (source / "link").symlink_to(tmp_path)
    cli._clean(work)
    assert not (work / "in").exists()
    assert (source / "empty").is_dir() and (source / "link").is_symlink()      # nothing behind a link is touched


def test_a_task_id_is_a_safe_directory_name(fake):
    out = cli.run({"id": "a/b c"}, None, params={"web_search": False, "prompt": "go", "workspaces": "ws"}, base=fake)
    assert out.log.parent.name.startswith("a_b_c-") and out.log.parent.parent == (fake / "ws").resolve()


# ---------- hermit.agent.base.trajectory and the Evolvers' views ----------

CLAUDE = [
    {"type": "system", "session_id": "s1"},
    {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "Look first."},
                                                  {"type": "tool_use", "id": "t1", "name": "Bash",
                                                   "input": {"command": "ls docs"}}]}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "a.txt",
                                              "is_error": False}]}},
    {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t2", "name": "Read",
                                                   "input": {"file_path": "a.txt"}},
                                                  {"type": "text", "text": "<answer>3</answer>"}]}},
    {"type": "result", "result": "<answer>3</answer>", "is_error": False},
]


def write_stream(path: Path, events: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json\n" + "".join(json.dumps(e) + "\n" for e in events))
    return path


def test_the_hosts_streams_as_steps(tmp_path):
    codex = trajectory.log_steps(write_stream(tmp_path / "c" / "agent-codex.jsonl", EVENTS).parent)
    assert [(s.kind, s.exit_code) for s in codex] == [("reasoning", None), ("command", 0), ("command", 1),
                                                      ("message", None)]
    claude = trajectory.log_steps(write_stream(tmp_path / "k" / "agent-claude.jsonl", CLAUDE).parent)
    assert [s.kind for s in claude] == ["reasoning", "command", "tool_call", "message"]
    assert claude[1].command == "ls docs" and claude[1].output == "a.txt" and claude[1].exit_code == 0
    assert trajectory.log_steps(tmp_path / "nothing") is None
    text = trajectory.text(codex, output=4)
    assert "[command exit 1] rg receipts docs/\n  output: ...atch" in text and "[assistant] <answer>44,463</answer>" in text
    record = {"trajectory": "c"}
    assert trajectory.read(record, tmp_path) == codex and trajectory.read({}, tmp_path) == []


def test_skillopt_and_gepa_read_a_host_agents_log(tmp_path):
    from hermit.agent.evolver.methods.gepa.evolver import trajectory_text
    from hermit.agent.evolver.methods.skillopt.evolver import codex_trace, conversation
    log = write_stream(tmp_path / "trajectories" / "q1" / "agent-codex.jsonl", EVENTS).parent
    (log / "prompt.txt").write_text("Read task.md.")
    record = {"trajectory": "trajectories/q1", "output": {"answer": "44,463"}, "status": "ok", "tokens": 1050,
              "judge": {"score": 1.0}, "pipeline_extra": {"effort": "medium"}}
    entries, system, user = conversation(record, tmp_path)
    assert user == "Read task.md." and system == ""
    assert entries[0] == {"type": "tool_call", "cmd": "cat task.md", "obs": "What were the receipts?"}
    assert {"role": "assistant", "content": "<answer>44,463</answer>"} in entries
    assert entries[-1]["content"].startswith("Run status: ok; score: 1")
    summary = codex_trace(record, tmp_path)
    assert summary.startswith("Codex Trace Summary") and "- read task.md: success" in summary
    assert "rg receipts docs/ (exit 1)" in summary and "- final answer format: tagged_nonlabel" in summary
    assert "- reasoning: medium" in summary and "- tokens used: 1,050" in summary
    (log / "codex_trace_summary.txt").write_text("SkillOpt's own")                  # SkillOpt's harness wrote one
    assert codex_trace(record, tmp_path) == "SkillOpt's own"
    text = trajectory_text({"trajectory": "trajectories/q1"}, tmp_path)
    assert text.startswith("[reasoning] Find the 1944 bulletin.") and '"type"' not in text   # steps, not raw JSON


def test_a_login_file_from_the_future_or_no_login_is_not_taken(tmp_path):
    """The shared copy takes a later refresh only: a file a session could write with a last_refresh ahead of now (it
    would pin the copy against a later `codex login`), or one that is no login, is left out."""
    master = login.master(tmp_path)
    master.parent.mkdir(parents=True)
    master.write_text(json.dumps({"last_refresh": "2026-10-01T00:00:00Z"}))
    for content in ({"last_refresh": "2999-01-01T00:00:00Z"}, {"last_refresh": "soon"}, ["no", "login"]):
        left = tmp_path / "left.json"
        left.write_text(json.dumps(content))
        login.take_back(tmp_path, left)
        assert json.loads(master.read_text())["last_refresh"] == "2026-10-01T00:00:00Z"
    left.write_text(json.dumps({"last_refresh": "2026-10-30T12:00:00Z"}))
    login.take_back(tmp_path, left)
    assert json.loads(master.read_text())["last_refresh"] == "2026-10-30T12:00:00Z"


def test_a_login_session_waits_for_the_plan_and_runs_again_when_refused(fake, monkeypatch):
    """On a ChatGPT login a session first waits for the plan, and one the plan refused is run again (its refusal is
    never the answer) after every session is held back (hermit.agent.base.login)."""
    from hermit.agent.base import login as L
    tmp_path = fake
    root = tmp_path / "login-root"
    (root / "login").mkdir(parents=True)
    (root / "login" / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt",
                                                          "last_refresh": "2026-01-01T00:00:00Z"}))
    seen = {"waits": 0, "sessions": 0, "holds": 0}
    monkeypatch.setattr(L, "wait", lambda plan=None, cancel=None, log=None, max_s=None:
                        seen.__setitem__("waits", seen["waits"] + 1) or "")
    monkeypatch.setattr(L, "hold", lambda plan=None: seen.__setitem__("holds", seen["holds"] + 1))
    agent = make("codex", {"web_search": False, "login": "chatgpt", "model": "gpt-x", "login_root": str(root),
                           "plan_dir": str(tmp_path / "plan")})
    real = type(agent)._session

    def session(self, prompt, **kw):
        seen["sessions"] += 1
        result = real(self, prompt, **kw)
        if seen["sessions"] == 1:                                      # the plan refuses the first one
            result.extra.update(served=False, output_tail="ERROR: You've hit your usage limit. Try again later.")
        return result
    monkeypatch.setattr(type(agent), "_session", session)
    (tmp_path / "w").mkdir()
    result = agent.run("go", workspace=tmp_path / "w")
    assert seen == {"waits": 2, "sessions": 2, "holds": 1} and result.reply.endswith("<answer>44,463</answer>")


def test_a_long_window_still_over_holds_after_the_short_one_resets(tmp_path):
    import time
    from hermit.agent.base import login as L
    (tmp_path / "MAX_USED").write_text("70")
    (tmp_path / "plan.json").write_text(json.dumps({"primary": {"used_percent": 90, "resets_at": time.time() - 5},
                                                    "secondary": {"used_percent": 80, "resets_at": time.time() + 900}}))
    assert "80%" in L.held(tmp_path)
    assert L.wait(tmp_path, max_s=0) == "timeout"                        # past the longest wait: unavailable


def test_a_session_waiting_for_a_slot_waits_again_if_paused_meanwhile(fake, monkeypatch):
    from hermit.agent.base import login as L
    tmp_path = fake
    root, plan = tmp_path / "login-root", tmp_path / "plan"
    (root / "login").mkdir(parents=True)
    (root / "login" / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt",
                                                          "last_refresh": "2026-01-01T00:00:00Z"}))
    plan.mkdir()
    (plan / "SLOTS").write_text("1")
    calls = {"held": 0}
    real_held = L.held

    def held(p=None):                       # PAUSE appears while the session waits for its slot, then goes again
        calls["held"] += 1
        return "paused" if calls["held"] == 1 else real_held(p)
    monkeypatch.setattr(L, "held", held)
    monkeypatch.setattr(L, "wait", lambda plan=None, cancel=None, log=None, max_s=None: "")
    (tmp_path / "w").mkdir()
    agent = make("codex", {"web_search": False, "login": "chatgpt", "model": "gpt-x", "login_root": str(root),
                           "plan_dir": str(plan)})
    result = agent.run("go", workspace=tmp_path / "w")
    assert calls["held"] == 2 and result.reply.endswith("<answer>44,463</answer>")
    assert (plan / L.LOCKS).is_dir() and not (plan / "slots").exists()      # SLOTS and its locks never share a name


def test_the_plan_holds_sessions_after_a_refusal_and_counts_slots(tmp_path):
    import time
    from hermit.agent.base import login as L
    L.hold(tmp_path)
    assert "refused" in L.held(tmp_path)                                   # held for the window (an hour)
    report = json.loads((L.plan_dir(tmp_path) / "plan.json").read_text())
    report["primary"]["resets_at"] = time.time() - 1
    (L.plan_dir(tmp_path) / "plan.json").write_text(json.dumps(report))
    assert L.held(tmp_path) == ""                                          # the window reset
    (L.plan_dir(tmp_path) / "MAX_USED").write_text("50")
    (L.plan_dir(tmp_path) / "plan.json").write_text(json.dumps(
        {"primary": {"used_percent": 70, "resets_at": time.time() + 600}}))
    assert "70%" in L.held(tmp_path)
    (L.plan_dir(tmp_path) / "SLOTS").write_text("1")
    with L.slot(tmp_path) as first:
        assert first
        import threading
        stop = threading.Event()
        threading.Timer(0.3, stop.set).start()
        with L.slot(tmp_path, cancel=stop) as second:                     # the only slot is taken
            assert second is False
    home = tmp_path / "home" / "sessions" / "2026"
    home.mkdir(parents=True)
    (home / "s.jsonl").write_text(json.dumps({"timestamp": "2026-11-02T00:00:00Z", "payload": {
        "rate_limits": {"primary": {"used_percent": 12, "resets_at": 1}}}}) + "\n")
    assert L.note_usage(tmp_path, tmp_path / "home")["primary"]["used_percent"] == 12
    assert json.loads((L.plan_dir(tmp_path) / "plan.json").read_text())["primary"]["used_percent"] == 12


def test_a_cli_answers_chat_requests_for_code_that_calls_a_chat_api(fake):
    """`llm: {cli: codex, ...}`: each chat request is one session; the system messages are its developer instructions,
    the rest its prompt on stdin, the effort the request's; the reply is the session's last message with its usage.
    The learner is an agent that can run commands: only a container of its own is accepted."""
    from hermit.agent.base.endpoint import ChatEndpoint, cli_model, split_messages
    from hermit.agent.model.llm import ChatClient
    for spec in ({"cli": "codex", "model": "gpt-x", "web_search": False},
                 {"cli": "codex", "isolation": "sandbox", "web_search": False}):
        with pytest.raises(ValueError, match="isolation: container"):
            cli_model(spec)
    endpoint = ChatEndpoint(make("codex", {"model": "gpt-x", "web_search": False})).start()   # the answering path
    client = ChatClient(endpoint.url, "gpt-x")
    message, usage = client.chat([{"role": "system", "content": "Be terse."}, {"role": "user", "content": "Sum it."}],
                                 reasoning_effort="high")
    assert message["content"].endswith("<answer>44,463</answer>") and usage.input_tokens == 1000
    assert split_messages([{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"},
                           {"role": "user", "content": "c"}])[1].endswith("[assistant]\nb\n\n[user]\nc")


def test_a_cli_llm_is_left_to_the_trainer_and_bound_when_its_model_is_a_reference():
    from hermit.agent.model.backends import normalize_config
    data = {"llm": {"cli": "codex", "model": "gpt-5.6-luna", "login": "chatgpt", "web_search": False}, "agent": {}}
    normalize_config(data)
    assert data["llm"] == {"cli": "codex", "model": "gpt-5.6-luna", "login": "chatgpt", "web_search": False}
    data = {"llm": {"cli": "codex", "model": "fireworks/accounts/fireworks/models/glm-5p3-flash",
                    "web_search": False}, "agent": {}}
    normalize_config(data)
    assert data["llm"]["binding"] and any("model_provider" in a for a in data["llm"]["args"])
    with pytest.raises(ValueError, match="llm.cli must be codex"):
        normalize_config({"llm": {"cli": "pi"}, "agent": {}})


def test_a_fresh_home_is_not_in_the_temporary_directory(tmp_path, monkeypatch):
    """Codex makes no helper programs for its sandbox in a home under the temporary directory, and then no command of
    the session runs: a fresh home is made under $HERMIT_HOMES (default ~/.cache/hermit/homes)."""
    monkeypatch.setenv("HERMIT_HOMES", str(tmp_path / "homes"))
    home, fresh = make("codex", {"web_search": False})._home()
    assert fresh and home.parent == (tmp_path / "homes").resolve()
    monkeypatch.delenv("HERMIT_HOMES")
    home, _ = make("codex", {"web_search": False})._home()
    assert home.parent == Path.home().joinpath(".cache", "hermit", "homes").resolve()
    home.rmdir()


def test_a_cli_session_that_cannot_run_answers_the_client_with_why(tmp_path):
    """A CLI session that raises (a missing setting, a container not ready) is an error response with its reason, not
    a dropped connection the client retries blind."""
    from hermit.agent.base.endpoint import ChatEndpoint
    from hermit.agent.model.llm import ChatClient, ChatHTTPError
    endpoint = ChatEndpoint(make("codex", {"model": "gpt-x"})).start()           # web_search not set: refused
    with pytest.raises(ChatHTTPError) as err:
        ChatClient(endpoint.url, "gpt-x", max_retries=0).chat([{"role": "user", "content": "hi"}])
    assert err.value.code == 400 and b"web_search" in err.value.body


def test_long_instructions_go_on_stdin_not_in_one_argument():
    from hermit.agent.base.cli import ARG_MAX, codex_instructions
    args, prompt = codex_instructions("be brief", "go")
    assert args == ["-c", 'developer_instructions="be brief"'] and prompt == "go"
    args, prompt = codex_instructions("x" * (ARG_MAX + 1), "go")
    assert len(args[1].encode()) < 200 and prompt.startswith("# SYSTEM INSTRUCTIONS") and prompt.endswith("go")


def test_a_window_without_a_reset_time_does_not_hold_forever(tmp_path):
    import time
    from datetime import datetime, timezone
    from hermit.agent.base import login as L
    (tmp_path / "MAX_USED").write_text("50")
    old = datetime.fromtimestamp(time.time() - 7200, timezone.utc).isoformat()
    (tmp_path / "plan.json").write_text(json.dumps({"time": old, "primary": {"used_percent": 90}}))
    assert L.held(tmp_path) == ""                            # an hour after the report it counts as reset
    (tmp_path / "plan.json").write_text(json.dumps({"time": old, "primary": {"used_percent": 90,
                                                                             "resets_in_seconds": 10800}}))
    assert "90%" in L.held(tmp_path)                         # resets three hours after the report: one to go


def test_the_longest_wait_counts_over_every_wait_of_a_session(fake, monkeypatch):
    from hermit.agent.base import login as L
    tmp_path = fake
    root = tmp_path / "login-root"
    (root / "login").mkdir(parents=True)
    (root / "login" / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt",
                                                          "last_refresh": "2026-01-01T00:00:00Z"}))
    given = []

    def wait(plan=None, cancel=None, log=None, max_s=None):
        given.append(max_s)
        return "" if len(given) == 1 else "timeout"
    monkeypatch.setattr(L, "wait", wait)
    monkeypatch.setattr(L, "hold", lambda plan=None: None)
    agent = make("codex", {"web_search": False, "login": "chatgpt", "model": "gpt-x", "login_root": str(root),
                           "plan_dir": str(tmp_path / "plan"), "plan_max_wait_s": 1000})
    real = type(agent)._session

    def refused(self, prompt, **kw):
        result = real(self, prompt, **kw)
        result.extra.update(served=False, output_tail="You've hit your usage limit")
        return result
    monkeypatch.setattr(type(agent), "_session", refused)
    (tmp_path / "w").mkdir()
    result = agent.run("go", workspace=tmp_path / "w")
    assert result.extra["unavailable"] and given[0] <= 1000 and given[1] < given[0] + 1 and given[1] <= 1000
