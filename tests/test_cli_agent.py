"""The framework's Codex agent (hermit/agent/acting/workspace.py) against fake CLIs: the task's hooks, what a run records, the
ChatGPT login, retries, mounts, the container's command line; and hermit.agent.base.trajectory, the one reader of the hosts'
logs, with the views the Evolvers build from it."""
import json
import os
from pathlib import Path
import shutil
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
if os.environ.get("FAKE_REFUSED"):                     # the plan's limit, as Codex reports it: an error event
    print(json.dumps({{"type": "thread.started", "thread_id": "th1"}}))
    print(json.dumps({{"type": "error", "message": "You've hit your usage limit. Try again later."}}))
    sys.exit(1)
if os.environ.get("FAKE_OWN_TEXT"):                    # the session's own work quotes an outage and a limit
    print(json.dumps({{"type": "item.completed", "item": {{"type": "command_execution", "command": "rg error logs/",
                     "exit_code": 0, "aggregated_output": os.environ["FAKE_OWN_TEXT"]}}}}))
if (home / "auth.json").exists():
    auth = json.loads((home / "auth.json").read_text())
    auth["last_refresh"] = "2026-10-30T12:00:00Z"
    (home / "auth.json").write_text(json.dumps(auth))
for e in json.loads({json.dumps(json.dumps(EVENTS))}):
    print(json.dumps(e))
last.write_text("Done.\\n<answer>44,463</answer>")
if os.environ.get("FAKE_LATE_LIMIT"):                  # the plan's limit reported after a turn it served
    print(json.dumps({{"type": "error", "message": "You've hit your usage limit. Try again later."}}))
if os.environ.get("FAKE_LINK_AUTH"):                   # the session leaves links in its home
    (home / "auth.json").unlink()
    (home / "auth.json").symlink_to(os.environ["FAKE_LINK_AUTH"])
if os.environ.get("FAKE_LINK_USAGE"):
    import shutil
    shutil.rmtree(home / "usage", ignore_errors=True)
    (home / "usage").symlink_to(os.environ["FAKE_LINK_USAGE"])
if os.environ.get("FAKE_LEAVE"):                       # links and a pipe left for the next session in this home
    target = Path(os.environ["FAKE_LEAVE"])
    (home / "config.toml").unlink()
    (home / "config.toml").symlink_to(target / "config-target")
    (home / "auth.tmp").symlink_to(target / "auth-target")
    (home / "usage").mkdir(exist_ok=True)
    os.mkfifo(home / "usage" / "th1.json")
if os.environ.get("FAKE_LINK_HOME"):                    # its whole home replaced by a link to another
    import shutil
    shutil.rmtree(home)
    home.symlink_to(os.environ["FAKE_LINK_HOME"])
if os.environ.get("FAKE_LINK_UP"):                      # the directory above its home replaced by a link
    import shutil
    shutil.rmtree(home.parent)
    home.parent.symlink_to(os.environ["FAKE_LINK_UP"])
if os.environ.get("FAKE_LINK_STREAM"):                  # its own stream replaced by a link to another file
    stream = Path(os.readlink("/proc/self/fd/1"))
    stream.unlink()
    stream.symlink_to(os.environ["FAKE_LINK_STREAM"])
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


def test_a_line_of_the_clis_own_output_that_parses_as_json_is_not_an_event(fake):
    """The session's stream carries the CLI's own messages too; a line of a multi-line Codex error that happens to be
    valid JSON but not an object (a quoted word, a number, a list) is not an event: the run still returns the
    session's answer and usage."""
    tmp_path = fake
    exe = tmp_path / "bin" / "codex"
    error = ("ERROR codex_core::tools::router: error=apply_patch verification failed: Failed to find expected lines "
             "in done.json:\n\"UID014\"\n42\n[1, 2]\nnull\n")
    exe.write_text(FAKE_CODEX.replace("for e in json.loads(",
                                      f"sys.stderr.write({error!r}); sys.stderr.flush()\nfor e in json.loads(", 1))
    out = cli.run({"id": "q1"}, None, params={"web_search": False, "prompt": "go", "workspaces": "ws"}, base=tmp_path)
    assert '"UID014"' in (out.log / "agent-codex.jsonl").read_text()       # the error reached the stream
    assert out.output.endswith("<answer>44,463</answer>") and out.extra["exit_code"] == 0   # the session's reply
    assert out.tokens == 1050                                                 # the usage was read past it


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
            result.extra.update(served=False, refused=True,
                                output_tail="ERROR: You've hit your usage limit. Try again later.")
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
    assert (plan / "SLOTS").is_file() and [p.name for p in plan.iterdir() if p.is_dir()] == [L.LOCKS]   # apart


def test_the_plan_holds_sessions_after_a_refusal_and_counts_slots(tmp_path):
    import time
    from hermit.agent.base import login as L
    L.hold(tmp_path)
    assert "refused" in L.held(tmp_path)                                   # held for the window (an hour)
    report = json.loads((L.plan_dir(tmp_path) / "plan.json").read_text())
    report["held_until"] = time.time() - 1
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
            assert second == "cancelled"
        with L.slot(tmp_path, deadline=time.time() + 0.05) as third:       # nor within the session's longest wait
            assert third == "timeout"
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


def test_a_cli_chat_calls_stream_is_kept_with_the_run_or_removed(fake, monkeypatch):
    """Each chat request's session writes an event stream: under the agent's `log_dir` when it has one (the
    Trainer's run records), and otherwise nothing of it is left in the temporary directory."""
    import tempfile
    from hermit.agent.base.endpoint import ChatEndpoint
    from hermit.agent.model.llm import ChatClient
    tmp = fake / "tmp"
    tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp))
    with ChatEndpoint(make("codex", {"model": "gpt-x", "web_search": False})) as endpoint:
        ChatClient(endpoint.url, "gpt-x").chat([{"role": "user", "content": "Sum it."}])
    assert not list(tmp.iterdir())
    from hermit.agent.base.endpoint import cli_answer

    def broken(self, *a, **k):
        raise RuntimeError("the sign-in failed")
    with monkeypatch.context() as m:                            # a call that raises leaves no stream either
        m.setattr(base_cli.CliAgent, "_sign_in", broken)   # after the stream is opened
        with pytest.raises(RuntimeError):
            cli_answer(make("codex", {"model": "gpt-x", "web_search": False}),
                       {"messages": [{"role": "user", "content": "Sum it."}]})
    assert not list(tmp.iterdir())
    logs = fake / "llm_sessions"
    with ChatEndpoint(make("codex", {"model": "gpt-x", "web_search": False, "log_dir": str(logs)})) as endpoint:
        ChatClient(endpoint.url, "gpt-x").chat([{"role": "user", "content": "Sum it."}])
    kept = list(logs.glob("*-codex.jsonl"))
    assert len(kept) == 1 and '"thread.started"' in kept[0].read_text()
    mine = tmp / "agent-codex-mine.jsonl"                       # a log the caller named, in the temporary dir
    with ChatEndpoint(make("codex", {"model": "gpt-x", "web_search": False, "log": str(mine)})) as endpoint:
        ChatClient(endpoint.url, "gpt-x").chat([{"role": "user", "content": "Sum it."}])
    assert mine.is_file()


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
    monkeypatch.setenv("HOME", str(tmp_path / "user"))                    # the default, in a home of the test's own
    home, _ = make("codex", {"web_search": False})._home()
    assert home.parent == (tmp_path / "user" / ".cache" / "hermit" / "homes").resolve()


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
        result.extra.update(served=False, refused=True, output_tail="You've hit your usage limit")
        return result
    monkeypatch.setattr(type(agent), "_session", refused)
    (tmp_path / "w").mkdir()
    result = agent.run("go", workspace=tmp_path / "w")
    assert result.extra["unavailable"] and given[0] <= 1000 and given[1] < given[0] + 1 and given[1] <= 1000


def test_a_stdin_that_cannot_be_written_is_closed_all_the_same(tmp_path):
    """A prompt the CLI stops reading part-way (its write fails) still closes its stdin, so a CLI that reads stdin to
    the end is not left waiting until its clock runs out."""
    import time
    cli = tmp_path / "cli"
    cli.write_text("#!/bin/sh\nhead -c 10 > /dev/null\nexec 0<&-\nsleep 0.2\necho done\n")
    cli.chmod(0o755)
    started = time.time()
    code, timed_out, _ = base_cli.run_process([str(cli)], env=dict(os.environ), cwd=tmp_path,
                                              stream=tmp_path / "out", timeout=20, stdin="x" * 5_000_000)
    assert code == 0 and not timed_out and time.time() - started < 10


def test_the_plan_settings_reach_the_base_agent(tmp_path):
    agent = cli.base_agent("codex", {"web_search": False, "login": "chatgpt", "model": "gpt-x",
                                     "plan_dir": str(tmp_path / "plan"), "plan_max_wait_s": None})
    assert agent.settings["plan_dir"] == str(tmp_path / "plan") and agent.settings["plan_max_wait_s"] is None


def test_a_refusal_holds_until_its_own_window_resets_not_another(tmp_path):
    import time
    from hermit.agent.base import login as L
    now = time.time()
    (tmp_path / "MAX_USED").write_text("70")
    (tmp_path / "plan.json").write_text(json.dumps({                     # Codex's own report of a refusal
        "rate_limit_reached_type": "primary", "primary": {"used_percent": 100, "resets_at": now - 10},
        "secondary": {"used_percent": 20, "resets_at": now + 5 * 86400}}))
    assert L.held(tmp_path) == ""                                         # the full window reset: the week is fine
    L.hold(tmp_path)                                                      # refused again, no window full: an hour
    held_until = json.loads((tmp_path / "plan.json").read_text())["held_until"]
    assert "refused" in L.held(tmp_path) and now + 3500 < held_until < now + 3700
    (tmp_path / "plan.json").write_text(json.dumps({"secondary": {"used_percent": 100, "resets_at": now + 7200}}))
    L.hold(tmp_path)                                                      # refused with the week full: its reset
    assert json.loads((tmp_path / "plan.json").read_text())["held_until"] == pytest.approx(now + 7200)


def test_only_the_clis_own_reports_say_unavailable_or_refused(fake, monkeypatch):
    """`unavailable` and `refused` read the CLI's own messages and error events, not the session's work: a session
    whose command output quotes an outage and a usage limit (a log it searched) met neither."""
    (fake / "w").mkdir()
    agent = make("codex", {"web_search": False, "model": "gpt-x", "home_files": []})
    monkeypatch.setenv("FAKE_OWN_TEXT", "ERROR: unexpected status 429 Too Many Requests; You've hit your usage limit")
    quoted = agent.run("go", workspace=fake / "w")
    assert quoted.extra["served"] and not quoted.extra["unavailable"] and not quoted.extra["refused"]
    monkeypatch.delenv("FAKE_OWN_TEXT")
    monkeypatch.setenv("FAKE_REFUSED", "1")
    refused = agent.run("go", workspace=fake / "w")
    assert not refused.extra["served"] and refused.extra["refused"] and refused.extra["started"]
    monkeypatch.delenv("FAKE_REFUSED")
    monkeypatch.setenv("FAKE_UNAVAILABLE", "100")                  # the CLI's own line, beside no event
    assert agent.run("go", workspace=fake / "w").extra["unavailable"]
    failed = json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": False,
                         "errors": ["API Error: 503 Service unavailable"]})    # Claude Code's error result
    assert base_cli.UNAVAILABLE.search(base_cli.notices("claude", failed))


def test_a_refused_session_is_returned_when_plan_rerun_is_off(fake, monkeypatch):
    from hermit.agent.base import login as L
    root = fake / "login-root"
    (root / "login").mkdir(parents=True)
    (root / "login" / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt",
                                                          "last_refresh": "2026-01-01T00:00:00Z"}))
    holds = []
    monkeypatch.setattr(L, "wait", lambda plan=None, cancel=None, log=None, max_s=None: "")
    monkeypatch.setattr(L, "hold", lambda plan=None: holds.append(1))
    monkeypatch.setenv("FAKE_REFUSED", "1")
    agent = make("codex", {"web_search": False, "login": "chatgpt", "model": "gpt-x", "login_root": str(root),
                           "plan_dir": str(fake / "plan"), "plan_rerun": False})
    (fake / "w").mkdir()
    result = agent.run("go", workspace=fake / "w")
    assert result.extra["refused"] and not result.extra["served"] and holds == [1]
    assert int((fake / "calls").read_text()) == 1                   # not run again in the same workspace


def test_links_a_session_leaves_in_its_home_are_never_followed(fake, monkeypatch):
    """After the session the Base Agent reads its home (a refreshed login, the plan's usage) and writes the usage
    ledger there: a link the session left is not read as its own login, and the ledger is not written through one."""
    from hermit.agent.base import login as L
    root = fake / "login-root"
    (root / "login").mkdir(parents=True)
    master = root / "login" / "auth.json"
    master.write_text(json.dumps({"auth_mode": "chatgpt", "last_refresh": "2026-01-01T00:00:00Z"}))
    forged = fake / "forged.json"
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")      # a later, plausible refresh
    forged.write_text(json.dumps({"auth_mode": "chatgpt", "last_refresh": now, "who": "else"}))
    outside = fake / "outside"
    outside.mkdir()
    monkeypatch.setattr(L, "wait", lambda plan=None, cancel=None, log=None, max_s=None: "")
    monkeypatch.setenv("FAKE_LINK_AUTH", str(forged))
    monkeypatch.setenv("FAKE_LINK_USAGE", str(outside))
    home = fake / "home"
    agent = make("codex", {"web_search": False, "login": "chatgpt", "model": "gpt-x", "login_root": str(root),
                           "plan_dir": str(fake / "plan"), "home": str(home)})
    (fake / "w").mkdir()
    result = agent.run("go", workspace=fake / "w")
    assert result.reply.endswith("<answer>44,463</answer>") and result.usage
    assert "else" not in master.read_text()                          # the shared login is not the linked file
    assert not list(outside.iterdir())                               # nothing written through the link
    assert (home / "usage").is_dir() and not (home / "usage").is_symlink() and (home / "usage" / "th1.json").is_file()


def test_a_kept_home_is_not_written_through_what_the_last_session_left(fake, monkeypatch):
    """A home kept between calls: the next call's config, sign-in copy and usage ledger are written into files of
    its own, never through a link (or into a pipe) the last session left there."""
    import threading
    from hermit.agent.base import login as L
    root = fake / "login-root"
    (root / "login").mkdir(parents=True)
    (root / "login" / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt",
                                                          "last_refresh": "2026-01-01T00:00:00Z"}))
    monkeypatch.setattr(L, "wait", lambda plan=None, cancel=None, log=None, max_s=None: "")
    outside = fake / "outside"
    outside.mkdir()
    home = fake / "home"
    agent = make("codex", {"web_search": False, "login": "chatgpt", "model": "gpt-x", "login_root": str(root),
                           "plan_dir": str(fake / "plan"), "home": str(home)})
    (fake / "w").mkdir()
    monkeypatch.setenv("FAKE_LEAVE", str(outside))
    done = []
    worker = threading.Thread(target=lambda: done.append(agent.run("go", workspace=fake / "w")), daemon=True)
    worker.start()
    worker.join(60)
    assert done, "the usage ledger blocked on the pipe the session left"
    monkeypatch.delenv("FAKE_LEAVE")
    result = agent.run("go", workspace=fake / "w", session="th1")
    assert result.reply.endswith("<answer>44,463</answer>")
    assert not list(outside.iterdir())                               # nothing written through the links
    assert not (home / "config.toml").is_symlink() and (home / "usage" / "th1.json").is_file()


def test_a_stream_replaced_by_a_link_is_not_read_as_the_sessions(fake, monkeypatch):
    other = fake / "other.jsonl"
    other.write_text(json.dumps({"type": "item.completed", "item": {"type": "agent_message",
                                                                    "text": "<answer>forged</answer>"}}) + "\n")
    monkeypatch.setenv("FAKE_LINK_STREAM", str(other))
    (fake / "w").mkdir()
    result = make("codex", {"web_search": False, "model": "gpt-x", "home_files": [],
                            "log": str(fake / "logs" / "agent-codex.jsonl")}).run("go", workspace=fake / "w")
    assert "forged" not in result.reply and not result.trajectory and not result.extra["served"]


def test_take_back_reads_no_login_through_a_linked_directory(tmp_path):
    """A rollout's logins are taken back from below its work directory (hermit/isolate.py): a directory there
    replaced by a link to another login is not followed."""
    import datetime
    from hermit.agent.base import login as L
    root = tmp_path / "root"
    (root / "login").mkdir(parents=True)
    master = root / "login" / "auth.json"
    master.write_text(json.dumps({"last_refresh": "2026-01-01T00:00:00Z"}))
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "auth.json").write_text(json.dumps({"last_refresh": now, "who": "else"}))
    work = tmp_path / "work"
    (work / "workspaces").mkdir(parents=True)
    (work / "workspaces" / "login").symlink_to(elsewhere)
    L.take_back(root, work / "workspaces" / "login" / "auth.json", within=work)
    assert "else" not in master.read_text()
    (work / "workspaces" / "login").unlink()
    (work / "workspaces" / "login").mkdir()
    (work / "workspaces" / "login" / "auth.json").write_text(json.dumps({"last_refresh": now, "who": "refreshed"}))
    L.take_back(root, work / "workspaces" / "login" / "auth.json", within=work)
    assert "refreshed" in master.read_text()                          # a real refresh is still taken back


def test_take_back_keeps_the_shared_login_to_its_account(tmp_path):
    """A session can write its home's login: a later one for another ChatGPT account is not taken back."""
    import datetime
    from hermit.agent.base import login as L
    root = tmp_path / "root"
    (root / "login").mkdir(parents=True)
    master = root / "login" / "auth.json"
    master.write_text(json.dumps({"last_refresh": "2026-01-01T00:00:00Z", "tokens": {"account_id": "mine"}}))
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    home = tmp_path / "home"
    home.mkdir()
    (home / "auth.json").write_text(json.dumps({"last_refresh": now, "tokens": {"account_id": "theirs"}}))
    L.take_back(root, home / "auth.json", within=home)
    assert "theirs" not in master.read_text()
    (home / "auth.json").write_text(json.dumps({"last_refresh": now, "tokens": {"account_id": "mine"}, "n": 2}))
    L.take_back(root, home / "auth.json", within=home)
    assert '"n": 2' in master.read_text()                             # the same account's refresh is taken back
    master.write_text(json.dumps({"last_refresh": "2026-01-01T00:00:00Z"}))   # a copy that names no account
    (home / "auth.json").write_text(json.dumps({"last_refresh": now, "tokens": {"account_id": "mine"}, "n": 3}))
    L.take_back(root, home / "auth.json", within=home)
    assert '"n": 3' in master.read_text()


def test_no_link_on_the_way_to_a_sessions_output_is_followed(tmp_path):
    """A session can replace a directory between its home and its stream with a link: the Base Agent removes the
    link and makes the directory, and writes or removes nothing where it led."""
    from hermit.agent.base.cli import _session_output
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    (outside / "logs" / "agent.jsonl").mkdir(parents=True)          # a directory where the stream goes
    (outside / "logs" / "agent.jsonl" / "keep.txt").write_text("k")
    (home / "a").symlink_to(outside)
    _session_output(home / "a" / "logs" / "agent.jsonl", [home])
    assert (outside / "logs" / "agent.jsonl" / "keep.txt").read_text() == "k"
    assert not (home / "a").is_symlink() and (home / "a" / "logs").is_dir()


def test_a_login_copy_below_a_rollouts_directory_is_removed_without_following_links(tmp_path):
    from hermit.agent.base import login as L
    target = tmp_path / "users-codex"
    target.mkdir()
    (target / "auth.json").write_text("{}")
    work = tmp_path / "work"
    (work / "workspaces").mkdir(parents=True)
    (work / "workspaces" / "login").symlink_to(target)
    L.remove(work / "workspaces" / "login" / "auth.json", within=work)
    assert (target / "auth.json").is_file() and not (work / "workspaces" / "login").exists()
    (work / "workspaces" / "login").mkdir()
    (work / "workspaces" / "login" / "auth.json").write_text("{}")
    L.remove(work / "workspaces" / "login" / "auth.json", within=work)
    assert not (work / "workspaces" / "login" / "auth.json").exists()


def test_a_ledger_the_session_rewrote_is_no_earlier_total(tmp_path):
    from hermit.agent.base.cli import _this_call
    from hermit.agent.model.usage import Usage
    reported = Usage("m", input_tokens=1000, cached_input_tokens=0, output_tokens=50, calls=1)
    (tmp_path / "usage").mkdir()
    for text in ("not json", json.dumps({"input_tokens": 10 ** 9, "output_tokens": 10 ** 9}), json.dumps([1]),
                 '{"input_tokens": 1e1000}'):
        (tmp_path / "usage" / "th1.json").write_text(text)
        u = _this_call(tmp_path, "th1", reported, resumed=True)
        assert (u.input_tokens, u.output_tokens) == (1000, 50)
    u = _this_call(tmp_path, "th1", Usage("m", input_tokens=1500, cached_input_tokens=0, output_tokens=80, calls=1),
                   resumed=True)
    assert (u.input_tokens, u.output_tokens) == (500, 30)            # an honest earlier total still counts


def test_what_a_session_left_on_the_way_to_its_log_is_never_followed_by_the_next_call(fake, tmp_path):
    """A log below a place the session writes: what it left on the way (a dangling link, a link out, a file) is
    removed before the call makes the log's directory, never followed nor in the way."""
    ws = fake / "w"
    outside = tmp_path / "outside"
    outside.mkdir()
    for leave, log in ((lambda: (ws / "logs").symlink_to(tmp_path / "nowhere"), ws / "logs" / "agent-codex.jsonl"),
                       (lambda: (ws / "a").symlink_to(outside), ws / "a" / "b" / "agent-codex.jsonl"),
                       (lambda: (ws / "f").write_text("x"), ws / "f" / "agent-codex.jsonl")):
        shutil.rmtree(ws, ignore_errors=True)
        ws.mkdir()
        leave()
        result = make("codex", {"web_search": False, "model": "gpt-x", "home_files": [],
                                "log": str(log)}).run("go", workspace=ws)
        assert result.extra["served"] and log.is_file()
    assert list(outside.iterdir()) == []                    # nothing made outside through the link
    alias = tmp_path / "ws-alias"                               # a workspace given by a link's name
    alias.symlink_to(ws)
    shutil.rmtree(ws)
    ws.mkdir()
    (ws / "a").symlink_to(outside)
    result = make("codex", {"web_search": False, "model": "gpt-x", "home_files": [],
                            "log": str(alias / "a" / "b" / "agent-codex.jsonl")}).run("go", workspace=alias)
    assert result.extra["served"] and list(outside.iterdir()) == []
    extra = tmp_path / "extra"                                  # a writable path given by a link's name, the log
    extra.mkdir()                                               # below its real one
    (tmp_path / "extra-alias").symlink_to(extra)
    (extra / "a").symlink_to(outside)
    result = make("codex", {"web_search": False, "model": "gpt-x", "home_files": [],
                            "writable": [str(tmp_path / "extra-alias")],
                            "log": str(extra / "a" / "b" / "agent-codex.jsonl")}).run("go", workspace=ws)
    assert result.extra["served"] and list(outside.iterdir()) == []
    real_home = tmp_path / "real-home"                          # a home given by a link's name (macOS's /var and
    real_home.mkdir()                                           # /private/var), the log below that name
    (tmp_path / "home-alias").symlink_to(real_home)
    (real_home / "logs").write_text("left by the last session")
    (real_home / "deep").mkdir()
    (real_home / "deep" / "a").symlink_to(outside)
    for log in ("logs/agent-codex.jsonl", "deep/a/b/agent-codex.jsonl"):
        result = make("codex", {"web_search": False, "model": "gpt-x", "home_files": [],
                                "home": str(tmp_path / "home-alias"),
                                "log": str(tmp_path / "home-alias" / log)}).run("go", workspace=ws)
        assert result.extra["served"] and (real_home / log).is_file()
    assert list(outside.iterdir()) == []


def test_a_home_the_session_replaced_by_a_link_is_not_read_or_written_through(fake, tmp_path, monkeypatch):
    """A kept home inside a place the session writes (Harness²'s `.codex-home`): a session that replaced the whole
    home by a link has nothing done through it after the call (the sign-in copy removed, the login taken back)."""
    user = tmp_path / "user-codex"
    user.mkdir()
    (user / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt", "last_refresh": "2026-01-01T00:00:00Z"}))
    monkeypatch.setenv("CODEX_HOME", str(user))
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "auth.json").write_text("{}")
    (victim / "last_message.txt").write_text("<answer>forged</answer>")
    ws = fake / "w"
    ws.mkdir()
    monkeypatch.setenv("FAKE_LINK_HOME", str(victim))
    result = make("codex", {"web_search": False, "model": "gpt-x", "home": str(ws / ".codex-home"),
                            "home_files": ["auth.json"]}).run("go", workspace=ws)
    assert (victim / "auth.json").read_text() == "{}" and "forged" not in result.reply
    assert not (ws / ".codex-home").is_symlink()
    monkeypatch.delenv("FAKE_LINK_HOME")
    (victim / "home").mkdir()                                     # a link above the home, in a place it writes
    (victim / "home" / "auth.json").write_text("{}")
    monkeypatch.setenv("FAKE_LINK_UP", str(victim))
    result = make("codex", {"web_search": False, "model": "gpt-x", "home": str(ws / "session" / "home"),
                            "home_files": ["auth.json"]}).run("go", workspace=ws)
    assert (victim / "home" / "auth.json").read_text() == "{}" and not (ws / "session").is_symlink()
    monkeypatch.delenv("FAKE_LINK_UP")
    (tmp_path / "real-home").mkdir()
    (tmp_path / "home-link").symlink_to(tmp_path / "real-home")   # a home the caller gives by a link stays one
    make("codex", {"web_search": False, "model": "gpt-x", "home": str(tmp_path / "home-link"),
                   "home_files": []}).run("go", workspace=ws)
    assert (tmp_path / "home-link").is_symlink()


def test_a_callers_own_output_paths_are_left_as_they_are(fake):
    """The caller's `stderr_log` and `log` (a device, a link the caller made) are opened as they are, never
    removed; in the session's own home, whatever it left at the stream's path (a directory) is replaced."""
    (fake / "w").mkdir()
    for path in (os.devnull, "/dev/stderr"):
        result = make("codex", {"web_search": False, "model": "gpt-x", "home_files": [],
                                "stderr_log": path}).run("go", workspace=fake / "w")
        assert result.reply.endswith("<answer>44,463</answer>") and os.path.exists(path)
    (fake / "logs").mkdir()
    (fake / "logs" / "kept.jsonl").write_text("")
    (fake / "logs" / "agent-codex.jsonl").symlink_to(fake / "logs" / "kept.jsonl")
    result = make("codex", {"web_search": False, "model": "gpt-x", "home_files": [],
                            "log": str(fake / "logs" / "agent-codex.jsonl")}).run("go", workspace=fake / "w")
    assert (fake / "logs" / "agent-codex.jsonl").is_symlink() and result.trajectory and result.extra["served"]
    home = fake / "kept-home"
    (home / "agent-codex.jsonl").mkdir(parents=True)        # what a session left at its stream's path
    result = make("codex", {"web_search": False, "model": "gpt-x", "home_files": [],
                            "home": str(home)}).run("go", workspace=fake / "w")
    assert result.extra["served"] and (home / "agent-codex.jsonl").is_file()


def test_a_session_the_plan_served_is_not_refused(fake, monkeypatch):
    monkeypatch.setenv("FAKE_LATE_LIMIT", "1")
    (fake / "w").mkdir()
    result = make("codex", {"web_search": False, "model": "gpt-x", "home_files": []}).run("go", workspace=fake / "w")
    assert result.extra["served"] and not result.extra["refused"] and result.reply.endswith("<answer>44,463</answer>")


