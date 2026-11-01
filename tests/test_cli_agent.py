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
              "collect": "hooks.py:collect", "deploy": "tree", "effort": "medium", "workspaces": "ws",
              "model": "m1", "flavour": "plain", "corpus": str(corpus), "price": {"input": 1, "output": 2}}
    out = cli.run({"id": "q1", "question": "receipts"}, seed(tmp_path), params=params, base=tmp_path)
    log, work = out.log, Path(out.extra["workspace"])
    seen = json.loads((work.parent / "home" / "seen.json").read_text())
    assert seen["task"] == "Q: receipts (plain)" and seen["docs"] == ["root_1"] and seen["cwd"] == str(work)
    assert "-m" in seen["args"] and 'model_reasoning_effort="medium"' in seen["args"]
    assert seen["args"][-1] == "Read task.md about receipts."                     # the template, its newline gone
    assert (work / "AGENTS.md").read_text() == "# Skill\n\nAnswer with the bare number.\n"   # deploy: tree
    assert out.output == {"answer": "44,463", "id": "q1", "fail_reason": ""}
    assert sorted(p.name for p in log.iterdir()) == ["agent-codex.jsonl", "deployment.json", "last_message.txt",
                                                     "prompt.txt", "task.md"]
    assert not (work / "docs").exists()                                    # the mount's link does not outlive the run
    extra = out.extra
    assert (extra["backend"], extra["deploy"], extra["isolation"], extra["exit_code"], extra["timed_out"],
            extra["attempts"], extra["task"]) == ("codex", "tree", "none", 0, False, 1, {"collected": True})
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
                          ({"isolation": "container", "container": {"img": "x"}}, "unknown keys")]:
        with pytest.raises(ValueError, match=match):
            cli._check("codex", {"web_search": False, **params})
    for value in (None, "no"):                         # web search is set explicitly, never left to a default
        with pytest.raises(ValueError, match="web_search must be set"):
            cli._check("codex", {} if value is None else {"web_search": value})
    with pytest.raises(ValueError, match="takes deploy: host"):
        cli._deploy({}, seed(tmp_path), tmp_path, "claude", {"deploy": "tree"})


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
