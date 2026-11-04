"""The ahe template's hook runner, middleware interface and hosts' wires (hook_runner.py, hermit_hooks.py,
hook_hosts.py, loaded by path): every host event through its wire, the pending-context queue, the per-session state
under lock, the task text from the workspace's task.md or the session's first prompt, where the logs go, the host from
the deployment's record, and the fail-open rules (a crashing or slow middleware produces no output, exit 0 and an audit
line). Payloads are the real Codex 0.164 / Claude Code wire shapes. No bubblewrap: the runner runs where the agent
runs, in its workspace or its container."""
import fcntl
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap
import threading

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "hermit" / "adapter" / "templates" / "ahe"
HOSTS = ("codex", "claude-code")
AUDIT_KEYS = ["ts", "event", "session_id", "tool_name", "command", "decision", "reason", "context_chars", "ms",
              "error"]                                        # the as-run audit line of the Harbor route, field by field


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = _load("ahe_hook_runner", TEMPLATE / "hook_runner.py")
hooks = runner.hooks                                          # the template's hermit_hooks.py, as the runner loaded it
wires = runner.hosts                                          # the template's hook_hosts.py
HookInput, HookResult = hooks.HookInput, hooks.HookResult


def normalise(host, payload, adapter_dir, task_text="", state=None, event=None):
    """The host's wire on a payload, dispatching on `event` (a middleware function) or the payload's own event."""
    return wires.HOSTS[host].normalise(payload.get("hook_event_name") or "", payload, adapter_dir, "", task_text,
                                       state, function=event)


def translate(host, event, result, state, *, stop_hook_active=False):
    return wires.HOSTS[host].translate(event, result, state, stop_hook_active=stop_hook_active)

# A middleware exercising every event: before_tool denies `rm -rf /`, rewrites `ls` to `ls -la`, after_tool appends a
# note after pytest and blocks `boom`, stop blocks until a flag is set, before_model queues a reminder.
MIDDLEWARE = '''\
from hermit_hooks import HookResult


def session_start(inp, state):
    state["started"] = True
    return HookResult(context=f"session {inp.session_id} task={inp.task_text.strip()}")


def before_tool(inp, state):
    state["calls"] = state.get("calls", 0) + 1
    state["task"] = inp.task_text
    if inp.tool_kind == "shell" and "rm -rf /" in (inp.command or ""):
        return HookResult(decision="deny", reason="destructive command", context="use git clean instead")
    if inp.tool_kind == "shell" and inp.command == "ls":
        return {"decision": "allow", "updated_input": {"command": "ls -la"}}
    if inp.tool_kind == "patch":
        return {"context": "patch seen"}
    return None


def after_tool(inp, state):
    if inp.tool_kind == "shell" and "pytest" in (inp.command or ""):
        return HookResult(context="Execution note: pytest ran; exit=" + str(inp.exit_code))
    if inp.output_text == "boom":
        return HookResult(decision="block", reason="output rejected")
    if inp.tool_kind == "patch":
        state["patch_exit"] = inp.exit_code
    return None


def stop(inp, state):
    if not state.get("done"):
        state["done"] = True
        return HookResult(decision="block", reason="verify first: " + (inp.last_message or ""))
    return HookResult(stop_session=True, reason="all good", system_message="bye")


def before_model(inp, state):
    inp.queue_context("queued " + str(state.get("calls", 0)))
    return HookResult(context="reminder")
'''


def payload(event, **extra):
    base = {"hook_event_name": event, "session_id": "s-1", "transcript_path": "/x/t.jsonl", "cwd": "/w",
            "model": "gpt-5", "permission_mode": "default"}
    if event != "SessionStart":
        base["turn_id"] = "turn-1"
    return {**base, **extra}


SHELL_PRE = {"tool_name": "Bash", "tool_input": {"command": "echo hello"}, "tool_use_id": "exec-1"}
PATCH_POST = {"tool_name": "apply_patch", "tool_input": {"command": "*** Begin Patch\n*** End Patch"},
              "tool_response": "Exit code: 3\nWall time: 0.0 seconds\nOutput:\nfailed"}


def adapter_dir(tmp_path: Path, middleware: str = MIDDLEWARE, host: str = "codex") -> Path:
    """An adapter's copy as a deployment for `host` leaves it: the fixed runner, interface and wires beside a
    middleware, in `hermit/ahe/` of the workspace tmp_path/ws, whose `.hermit-deploy.json` names the host."""
    ws = tmp_path / "ws"
    a = ws / "hermit" / "ahe"
    a.mkdir(parents=True, exist_ok=True)
    for name in ("hook_runner.py", "hermit_hooks.py", "hook_hosts.py"):
        shutil.copy2(TEMPLATE / name, a / name)
    (a / "middleware.py").write_text(middleware)
    (ws / ".hermit-deploy.json").write_text(json.dumps({"host": host, "adapter_dir": str(a)}))
    return a


class Runner:
    """Runs the hook runner in process (stdout captured), on an adapter in tmp_path, in a session whose workspace
    (the payload's cwd) is tmp_path/ws: its logs are ws/.hermit-hooks/."""

    def __init__(self, tmp_path, middleware=MIDDLEWARE, host="codex"):
        self.adapter = adapter_dir(tmp_path, middleware, host)
        self.workspace = tmp_path / "ws"
        self.workspace.mkdir(exist_ok=True)
        self.state_dir = self.workspace / ".hermit-hooks" / "hook-state"
        self.audit_log = self.workspace / ".hermit-hooks" / "hook-calls.jsonl"

    def middleware(self, text: str) -> None:
        (self.adapter / "middleware.py").write_text(text)

    def __call__(self, event, data=None, *, capsys, raw=None, strict=False):
        if raw is None:
            data = data if data is not None and "hook_event_name" in data else payload(event, **(data or {}))
            raw = json.dumps({**data, "cwd": str(self.workspace)} if "cwd" in data else data)
        capsys.readouterr()
        code = runner.run(event, raw, adapter=self.adapter, strict=strict)
        out = capsys.readouterr().out
        assert code == 0, out
        return json.loads(out) if out.strip() else None

    def audit(self):
        return [json.loads(line) for line in self.audit_log.read_text().splitlines()]

    def state(self, session_id="s-1"):
        return json.loads((self.state_dir / f"{session_id}.json").read_text())


@pytest.fixture(autouse=True)
def own_sys_path(monkeypatch):
    """The runner puts the adapter it loads a middleware from on sys.path, as it does in its own process."""
    monkeypatch.setattr(sys, "path", list(sys.path))


@pytest.fixture
def run(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("HERMIT_HOOK_LOGS", raising=False)

    def make(**kw):
        r = Runner(tmp_path, **kw)
        return lambda event, data=None, **extra: r(event, data, capsys=capsys, **extra), r
    return make


# --- normalise -------------------------------------------------------------------------------------------------

def test_normalise_the_codex_shell_wire():
    p = payload("PreToolUse", **SHELL_PRE)
    inp = normalise("codex", p, "/a", "task")
    assert (inp.event, inp.host, inp.session_id, inp.cwd, inp.adapter_dir) == ("before_tool", "codex", "s-1", "/w", "/a")
    assert (inp.tool_name, inp.tool_kind, inp.command, inp.task_text) == ("Bash", "shell", "echo hello", "task")
    assert inp.tool_input == {"command": "echo hello"} and inp.tool_output is None and inp.raw is p
    assert (inp.model, inp.turn_id, inp.transcript_path) == ("gpt-5", "turn-1", "/x/t.jsonl")
    post = normalise("codex", payload("PostToolUse", **SHELL_PRE, tool_response="hello\n"), "/a")
    assert post.event == "after_tool" and post.tool_output == "hello\n" and post.output_text == "hello\n"
    assert post.exit_code is None and post.duration_ms is None       # not on the Codex wire


def test_normalise_apply_patch_parses_the_exit_code_and_sets_no_command():
    inp = normalise("codex", payload("PostToolUse", **PATCH_POST), "/a")
    assert inp.tool_kind == "patch" and inp.command is None and inp.exit_code == 3
    assert inp.output_text.startswith("Exit code: 3")


def test_normalise_a_dict_tool_response_and_a_list_command():
    p = payload("PostToolUse", tool_name="Bash", tool_input={"command": ["git", "log", "-n 1"]},
                tool_response={"stdout": "abc", "exit_code": "2", "duration_ms": 15})
    inp = normalise("claude-code", p, "/a")
    assert inp.command == "git log '-n 1'" and inp.output_text == "abc" and inp.exit_code == 2 and inp.duration_ms == 15
    content = normalise("claude-code", payload("PostToolUse", tool_name="Read", tool_input={"file_path": "/f"},
                                               tool_response={"content": [{"type": "text", "text": "a"}, {"text": "b"}]}), "/a")
    assert content.tool_kind == "read" and content.output_text == "ab" and content.command is None


@pytest.mark.parametrize("name, kind", [
    ("Bash", "shell"), ("apply_patch", "patch"), ("Read", "read"), ("Write", "write"), ("Edit", "write"),
    ("mcp__hermit__lookup", "mcp"), ("myserver__search", "mcp"), ("spawn_agent", "other"),
    ("multi_agent_v1wait_agent", "other"), ("WebFetch", "other"), (None, None)])
def test_tool_kinds(name, kind):
    assert wires.CODEX.tool_kind(name) == kind


def test_stop_and_subagent_payloads():
    inp = normalise("codex", payload("Stop", last_assistant_message="done", stop_hook_active=False, agent_id="ag-1",
                                     agent_type="reviewer"), "/a")
    assert inp.event == "stop" and inp.last_message == "done" and inp.raw["agent_id"] == "ag-1"
    assert normalise("codex", payload("PreModelRequest"), "/a").event == "before_model"
    assert normalise("codex", {"hook_event_name": "Bogus"}, "/a").event == ""


def test_queue_context_writes_the_pending_queue():
    state = {}
    inp = HookInput(event="before_model", host="codex", session_id="s", cwd="/", adapter_dir="/a", state=state)
    inp.queue_context("one")
    inp.queue_context("two")
    assert state == {"_pending": ["one", "two"]}


def test_as_result_accepts_only_hook_result_shapes():
    assert hooks.as_result(None) is None
    r = HookResult(decision="deny", reason="use the tests instead")
    assert hooks.as_result(r) is r
    # A deny or a block needs a reason: Codex ignores a deny whose reason is empty, and the reason is what the model reads.
    for empty in [HookResult(decision="deny"), {"decision": "block", "reason": "  "}]:
        with pytest.raises(ValueError, match="needs a non-empty reason"):
            hooks.as_result(empty)
    assert hooks.as_result({"context": "c"}) == HookResult(context="c")
    with pytest.raises(TypeError, match="unknown keys"):
        hooks.as_result({"verdict": "no"})
    with pytest.raises(TypeError, match="must return"):
        hooks.as_result("deny")
    # The fields are typed: a wrong type is refused here, not after the decision inside translate or json.dumps.
    for bad, match in [({"context": 5}, "context must be str or NoneType, not int"),
                       ({"decision": "deny", "reason": 123}, "reason must be str, not int"),
                       ({"updated_input": {1, 2}}, "updated_input must be dict or NoneType, not set"),
                       ({"stop_session": "yes"}, "stop_session must be bool"),
                       (HookResult(system_message=["x"]), "system_message must be str or NoneType, not list")]:
        with pytest.raises(TypeError, match=match):
            hooks.as_result(bad)


def test_translate_validates_the_decision_before_draining_the_queue():
    state = {"_pending": ["kept"]}
    with pytest.raises(ValueError):
        translate("codex", "PostToolUse", HookResult(decision="nope"), state)
    assert state == {"_pending": ["kept"]}
    with pytest.raises(ValueError):
        translate("codex", "Stop", HookResult(decision="allow-ish"), state)
    assert state == {"_pending": ["kept"]}


def test_normalise_takes_the_runner_s_event_over_the_payload_s():
    p = {k: v for k, v in payload("PreToolUse", **SHELL_PRE).items() if k != "hook_event_name"}
    assert normalise("codex", p, "/a").event == ""
    assert normalise("codex", p, "/a", event="before_tool").event == "before_tool"
    assert normalise("codex", payload("PostToolUse"), "/a", event="before_tool").event == "before_tool"
    odd = normalise("codex", payload("PreToolUse", tool_name=5, tool_input={"x": 1}), "/a")
    assert odd.tool_name == "5" and odd.tool_kind == "other"       # a host's bug, not the middleware's error


# --- translate: the table, both hosts ---------------------------------------------------------------------------

@pytest.mark.parametrize("host", HOSTS)
def test_translate_before_tool(host):
    deny = translate(host, "PreToolUse", HookResult(decision="deny", reason="no", context="ctx", updated_input={"x": 1}), {})
    assert deny == {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                           "permissionDecisionReason": "no", "additionalContext": "ctx"}}
    allow = translate(host, "PreToolUse", HookResult(decision="allow", updated_input={"command": "ls -la"}), {})
    assert allow == {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                            "updatedInput": {"command": "ls -la"}}}
    rewritten = translate(host, "PreToolUse", HookResult(context="c", updated_input={"a": 1}), {})
    allow = {"permissionDecision": "allow"} if host == "codex" else {}  # Codex runs a new input only with an allow;
    assert rewritten == {"hookSpecificOutput": {"hookEventName": "PreToolUse", **allow,  # Claude Code's mode decides
                                                "additionalContext": "c", "updatedInput": {"a": 1}}}
    assert translate(host, "PreToolUse", None, {}) is None
    assert translate(host, "PreToolUse", HookResult(), {}) is None
    with pytest.raises(ValueError):
        translate(host, "PreToolUse", HookResult(decision="maybe"), {})


@pytest.mark.parametrize("host", HOSTS)
def test_translate_after_tool_session_start_and_stop(host):
    assert translate(host, "PostToolUse", HookResult(decision="block", reason="bad"), {}) == {"decision": "block", "reason": "bad"}
    both = translate(host, "PostToolUse", HookResult(decision="block", reason="bad", context="note"), {})
    assert both == {"decision": "block", "reason": "bad",
                    "hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "note"}}
    assert translate(host, "PostToolUse", HookResult(context="note"), {}) == {
        "hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "note"}}
    assert translate(host, "SessionStart", HookResult(context="hello"), {}) == {
        "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "hello"}}
    assert translate(host, "Stop", HookResult(decision="block", reason="more"), {}) == {"decision": "block", "reason": "more"}
    assert translate(host, "Stop", HookResult(decision="block", reason="more"), {}, stop_hook_active=True) is None
    assert translate(host, "Stop", HookResult(stop_session=True, reason="enough", system_message="msg"), {}) == {
        "continue": False, "stopReason": "enough", "systemMessage": "msg"}
    with pytest.raises(ValueError):
        translate(host, "SessionStart", HookResult(decision="deny"), {})
    assert translate(host, "Bogus", HookResult(context="x"), {}) is None


@pytest.mark.parametrize("host", ["codex"])          # Claude Code has no PreModelRequest: the runner's own call
def test_translate_before_model_queues_and_flush_points_drain(host):
    state = {}
    assert translate(host, "PreModelRequest", HookResult(context="remember", decision=None), state) is None
    assert state == {"_pending": ["remember"]}
    assert translate(host, "PreToolUse", None, state) is None            # not a flush point
    assert state == {"_pending": ["remember"]}
    assert translate(host, "UserPromptSubmit", None, state) == {
        "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "remember"}}
    assert state == {}
    state = {"_pending": ["a", "b"]}
    assert translate(host, "PostToolUse", HookResult(context="note"), state) == {
        "hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "note\n\na\n\nb"}}
    state = {"_pending": ["a"]}                                         # Codex reads no Stop context: the reason
    assert translate(host, "Stop", HookResult(decision="block", reason="r", context="c"), state) == {
        "decision": "block", "reason": "r\n\nc\n\na"}
    assert state == {}
    state = {"_pending": ["a"]}                                         # a stop that goes through: the queue waits
    assert translate(host, "Stop", HookResult(context="c"), state) is None and state == {"_pending": ["a"]}
    assert translate(host, "UserPromptSubmit", None, {}) is None


def test_claude_codes_stop_sends_a_context_only_with_a_block():
    """Claude Code reads a Stop's additionalContext, and alone it makes the model go on: so only beside a block."""
    state = {"_pending": ["a"]}
    assert translate("claude-code", "Stop", HookResult(decision="block", reason="r", context="c"), state) == {
        "decision": "block", "reason": "r", "hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": "c\n\na"}}
    state = {"_pending": ["a"]}
    assert translate("claude-code", "Stop", HookResult(context="c"), state) is None and state == {"_pending": ["a"]}
    assert translate("claude-code", "Stop", HookResult(decision="block", reason="r", context="c"), state,
                     stop_hook_active=True) is None and state == {"_pending": ["a"]}


# --- the runner, event by event ---------------------------------------------------------------------------------

def test_every_event_through_the_runner(run):
    call, r = run()
    # Codex fires SessionStart before the prompt is submitted: no task text yet.
    assert call("SessionStart") == {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                           "additionalContext": "session s-1 task="}}
    assert r.state()["started"] is True
    assert call("UserPromptSubmit", {"prompt": "fix the bug"}) is None
    assert call("PreToolUse", SHELL_PRE) is None
    assert r.state()["task"] == "fix the bug" and r.state()["_prompt"] == "fix the bug"      # no task.md in ws/
    assert call("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "rm -rf / --no-preserve-root"}}) == {
        "hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                               "permissionDecisionReason": "destructive command", "additionalContext": "use git clean instead"}}
    assert call("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "ls"}}) == {
        "hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow", "updatedInput": {"command": "ls -la"}}}
    assert call("PreToolUse", {"tool_name": "apply_patch", "tool_input": {"command": "*** Begin Patch"}}) == {
        "hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "patch seen"}}
    assert call("PostToolUse", {**SHELL_PRE, "tool_response": "hello\n"}) is None
    assert call("PostToolUse", {"tool_name": "Bash", "tool_input": {"command": "pytest -q"}, "tool_response": "1 passed"}) == {
        "hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "Execution note: pytest ran; exit=None"}}
    assert call("PostToolUse", {**SHELL_PRE, "tool_response": "boom"}) == {"decision": "block", "reason": "output rejected"}
    assert call("PostToolUse", PATCH_POST) is None
    assert r.state()["patch_exit"] == 3
    assert call("Stop", {"last_assistant_message": "done", "stop_hook_active": False}) == {
        "decision": "block", "reason": "verify first: done"}
    assert call("Stop", {"last_assistant_message": "done", "stop_hook_active": False}) == {
        "continue": False, "stopReason": "all good", "systemMessage": "bye"}
    assert call("PreModelRequest") is None
    assert r.state()["_pending"] == ["queued 4", "reminder"]
    events = [a["event"] for a in r.audit()]
    assert events == ["SessionStart", "UserPromptSubmit", "PreToolUse", "PreToolUse", "PreToolUse", "PreToolUse",
                      "PostToolUse", "PostToolUse", "PostToolUse", "PostToolUse", "Stop", "Stop", "PreModelRequest"]
    deny = r.audit()[3]
    assert deny["decision"] == "deny" and deny["reason"] == "destructive command" and deny["command"].startswith("rm -rf /")
    assert deny["context_chars"] == len("use git clean instead") and deny["error"] is None and deny["ms"] >= 0
    assert r.audit()[-1]["context_chars"] == len("reminder")
    assert all(list(a) == AUDIT_KEYS and a["session_id"] == "s-1" for a in r.audit())


def test_the_task_text_is_the_sessions_first_prompt(run):
    call, r = run()
    call("SessionStart")
    assert call("PreToolUse", SHELL_PRE) is None and r.state()["task"] == ""        # before any prompt
    call("UserPromptSubmit", {"prompt": "   "})                                         # a blank prompt is no task
    assert "_prompt" not in r.state()
    call("UserPromptSubmit", {"prompt": "answer the question in task.md"})
    call("UserPromptSubmit", {"prompt": "a later message"})                             # the first one stays
    call("PreToolUse", SHELL_PRE)
    assert r.state()["task"] == "answer the question in task.md"
    call("PreToolUse", payload("PreToolUse", session_id="s-2", **SHELL_PRE))            # another session has its own
    assert r.state("s-2")["task"] == ""


def test_the_task_text_is_the_workspaces_task_md(tmp_path, run, monkeypatch):
    """task.md in the session's workspace, as the earlier run's --task-file: read at every call, from SessionStart on,
    over the prompt that only names it; without a regular file there, the session's first prompt."""
    call, r = run()
    task = r.workspace / "task.md"
    task.write_text("## Question\nWhat share of outlays, in percent?\n")
    assert call("SessionStart")["hookSpecificOutput"]["additionalContext"] == (
        "session s-1 task=## Question\nWhat share of outlays, in percent?")
    call("UserPromptSubmit", {"prompt": "Read `task.md` and answer."})
    call("PreToolUse", SHELL_PRE)
    assert r.state()["task"] == task.read_text() and r.state()["_prompt"] == "Read `task.md` and answer."
    task.write_bytes(b"## Question\nrevised \xff\n")                       # read anew; a stray byte is no error
    call("PreToolUse", SHELL_PRE)
    assert r.state()["task"] == "## Question\nrevised �\n" and r.audit()[-1]["error"] is None
    task.unlink()
    for other in (task.mkdir, lambda: os.mkfifo(task)):                    # a directory; a pipe, which would block
        other()
        call("PreToolUse", SHELL_PRE)
        assert r.state()["task"] == "Read `task.md` and answer." and r.audit()[-1]["error"] is None
        task.rmdir() if task.is_dir() else task.unlink()
    task.write_text("q?\n")
    monkeypatch.setenv("HERMIT_HOOK_LOGS", str(tmp_path / "logs"))         # the logs elsewhere: still the workspace's
    call("PreToolUse", SHELL_PRE)
    assert json.loads((tmp_path / "logs" / "hook-state" / "s-1.json").read_text())["task"] == "q?\n"


def test_the_logs_go_under_the_workspace_or_where_the_environment_says(tmp_path, run, capsys, monkeypatch):
    call, r = run()
    call("PreToolUse", SHELL_PRE)
    assert r.audit_log.is_file() and r.state()["calls"] == 1                # <payload cwd>/.hermit-hooks/
    elsewhere = tmp_path / "logs"
    monkeypatch.setenv("HERMIT_HOOK_LOGS", str(elsewhere))
    call("PreToolUse", SHELL_PRE)
    assert len(r.audit()) == 1 and r.state()["calls"] == 1                  # nothing more under the workspace
    assert json.loads((elsewhere / "hook-calls.jsonl").read_text())["event"] == "PreToolUse"
    assert json.loads((elsewhere / "hook-state" / "s-1.json").read_text())["calls"] == 1
    monkeypatch.delenv("HERMIT_HOOK_LOGS")
    # The workspace is the deployment's (the record's directory), whatever the payload's cwd says.
    for cwd in (None, "relative/dir", str(tmp_path)):
        data = {k: v for k, v in payload("SessionStart").items() if k != "cwd"}
        if cwd:
            data["cwd"] = cwd
        assert call("SessionStart", raw=json.dumps(data))["hookSpecificOutput"]["additionalContext"] == "session s-1 task="
    assert len(r.audit()) == 4 and not (r.adapter / ".hermit-hooks").exists()
    assert not (Path.cwd() / "relative").exists() and not (tmp_path / ".hermit-hooks").exists()


def test_a_copy_no_deployment_made_needs_the_host_and_logs_under_the_payload_s_cwd(tmp_path, capsys, monkeypatch):
    """Without a deployment record the host is --host's, and the workspace the host's own notion of it: Codex's
    payload cwd. Without either host, nothing runs: an audited error and no output."""
    monkeypatch.delenv("HERMIT_HOOK_LOGS", raising=False)
    r = Runner(tmp_path)
    (r.workspace / ".hermit-deploy.json").unlink()
    data = json.dumps({**payload("SessionStart"), "cwd": str(r.workspace)})
    assert runner.run("SessionStart", data, adapter=r.adapter) == 0
    assert capsys.readouterr().out == "" and r.audit()[-1]["error"].startswith("LookupError: no host")
    assert runner.run("SessionStart", data, adapter=r.adapter, host="codex") == 0
    assert json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"] == "session s-1 task="
    assert r.audit()[-1]["error"] is None
    assert runner.run("SessionStart", data, adapter=r.adapter, host="goose") == 0
    assert capsys.readouterr().out == "" and "runs none of this template's hooks" in r.audit()[-1]["error"]
    # Without a cwd either, the call runs and leaves no log.
    bare = json.dumps({k: v for k, v in payload("SessionStart").items() if k != "cwd"})
    assert runner.run("SessionStart", bare, adapter=r.adapter, host="codex") == 0
    assert json.loads(capsys.readouterr().out) and len(r.audit()) == 3


def test_the_pending_queue_is_flushed_at_the_next_flush_point(run):
    call, r = run()
    assert call("PreModelRequest") is None
    assert call("PreModelRequest") is None
    assert r.state()["_pending"] == ["queued 0", "reminder", "queued 0", "reminder"]
    assert call("PreToolUse", SHELL_PRE) is None                         # PreToolUse does not flush
    assert len(r.state()["_pending"]) == 4
    assert call("PostToolUse", {**SHELL_PRE, "tool_response": "hello\n"}) == {
        "hookSpecificOutput": {"hookEventName": "PostToolUse",
                               "additionalContext": "queued 0\n\nreminder\n\nqueued 0\n\nreminder"}}
    assert "_pending" not in r.state()
    call("PreModelRequest")
    assert call("Stop", {"last_assistant_message": "m"}) == {                # Codex: the queue goes with the reason
        "decision": "block", "reason": "verify first: m\n\nqueued 1\n\nreminder"}
    call("PreModelRequest")
    assert call("UserPromptSubmit", {"prompt": "go"}) == {
        "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "queued 1\n\nreminder"}}
    assert r.audit()[-1]["context_chars"] == len("queued 1\n\nreminder")


def test_a_stop_block_is_ignored_when_stop_hook_active(run):
    call, r = run()
    assert call("Stop", {"last_assistant_message": "x", "stop_hook_active": True}) is None
    assert r.state()["done"] is True
    assert r.audit()[-1]["reason"] == "stop_hook_active" and r.audit()[-1]["decision"] == "block"


def test_sessions_keep_separate_state_and_odd_ids_are_file_safe(run):
    call, r = run()
    call("SessionStart", payload("SessionStart", session_id="a/b c"))
    assert (r.state_dir / "a_b_c.json").is_file() and not (r.state_dir / "s-1.json").exists()
    call("SessionStart")
    assert sorted(p.name for p in r.state_dir.iterdir()) == ["a_b_c.json", "s-1.json"]


# --- failures: fail-open, audited -------------------------------------------------------------------------------

def test_logs_that_cannot_be_written_lose_the_line_not_the_call(tmp_path, run, monkeypatch):
    call, r = run()
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)                                                      # read-only: no audit, no state
    try:
        monkeypatch.setenv("HERMIT_HOOK_LOGS", str(locked / "logs"))
        # The state cannot be kept either: the call fails open as the as-run runner did (no output), and it no
        # longer dies on the audit line (exit 0: the helper asserts it).
        assert call("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "rm -rf / --no-preserve-root"}}) is None
        assert not (locked / "logs").exists()
    finally:
        locked.chmod(0o700)


def test_a_crashing_middleware_gives_no_output_exit_0_and_an_error_line(run):
    call, r = run(middleware="def before_tool(inp, state):\n    state['touched'] = 1\n    raise RuntimeError('kaboom')\n")
    assert call("PreToolUse", SHELL_PRE) is None
    line = r.audit()[-1]
    assert line["error"] == "RuntimeError: kaboom" and line["decision"] is None
    assert r.state()["touched"] == 1


def test_a_module_that_fails_to_import_or_returns_junk_is_an_error(run):
    call, r = run(middleware="import no_such_module_xyz\n")
    assert call("PreToolUse", SHELL_PRE) is None
    assert r.audit()[-1]["error"].startswith("ModuleNotFoundError")
    r.middleware("def before_tool(inp, state):\n    return {'verdict': 'deny'}\n")
    assert call("PreToolUse", SHELL_PRE) is None
    assert "unknown keys" in r.audit()[-1]["error"]
    r.middleware("def before_tool(inp, state):\n    return {'decision': 'block-ish'}\n")
    assert call("PreToolUse", SHELL_PRE) is None
    assert r.audit()[-1]["error"].startswith("ValueError: before_tool decision")
    r.middleware("import sys\ndef before_tool(inp, state):\n    sys.exit(2)\n")
    assert call("PreToolUse", SHELL_PRE) is None
    assert r.audit()[-1]["error"] == "SystemExit: 2"
    (r.adapter / "middleware.py").unlink()                                 # no middleware at all
    assert call("PreToolUse", SHELL_PRE) is None and r.audit()[-1]["error"].startswith("FileNotFoundError")


def test_a_slow_middleware_is_cut_by_the_runner_alarm(run, monkeypatch):
    monkeypatch.setattr(runner, "TIMEOUT_S", 3)                            # alarm at 1 s, well before the host's 3 s
    call, r = run(middleware="import time\ndef before_tool(inp, state):\n    time.sleep(30)\n    return {'decision': 'deny'}\n")
    assert call("PreToolUse", SHELL_PRE) is None
    line = r.audit()[-1]
    assert line["error"] == "MiddlewareTimeout: middleware timed out" and 900 <= line["ms"] < 2500


def test_an_except_exception_in_the_middleware_does_not_swallow_the_alarm(run, monkeypatch):
    monkeypatch.setattr(runner, "TIMEOUT_S", 3)
    call, r = run(middleware="import time\ndef before_tool(inp, state):\n    while True:\n        try:\n"
                             "            time.sleep(10)\n        except Exception:\n            pass\n")
    assert call("PreToolUse", SHELL_PRE) is None
    line = r.audit()[-1]
    assert line["error"] == "MiddlewareTimeout: middleware timed out" and 900 <= line["ms"] < 2500


def test_the_alarm_covers_the_wait_for_the_session_lock(run, monkeypatch):
    monkeypatch.setattr(runner, "TIMEOUT_S", 3)
    call, r = run()
    r.state_dir.mkdir(parents=True)
    held = r.state_dir / "s-1.json"
    held.write_text('{"held": true}')
    fd = os.open(held, os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)                    # another call on the session, stuck
    try:
        assert call("PreToolUse", SHELL_PRE) is None
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    line = r.audit()[-1]
    assert line["error"] == "MiddlewareTimeout: middleware timed out" and 900 <= line["ms"] < 2500
    assert held.read_text() == '{"held": true}'      # never loaded, never written
    assert call("PreToolUse", SHELL_PRE) is None and r.state()["calls"] == 1     # released: the next call runs


def test_junk_from_the_middleware_is_an_audited_error_with_exit_0(run):
    call, r = run()
    for source, error in [
            ("def before_tool(inp, state):\n    return {'context': 5}\n", "TypeError: HookResult.context must be str"),
            ("def before_tool(inp, state):\n    return {'decision': 'deny', 'reason': 123}\n", "TypeError: HookResult.reason must be str"),
            ("def before_tool(inp, state):\n    return {'updated_input': {'a': {1, 2}}}\n", "TypeError: Object of type set"),
            ("def before_tool(inp, state):\n    return {'stop_session': 'yes'}\n", "TypeError: HookResult.stop_session must be bool")]:
        r.middleware(source)
        assert call("PreToolUse", SHELL_PRE) is None
        assert r.audit()[-1]["error"].startswith(error), r.audit()[-1]
    # A state the runner cannot save leaves the file as it was; the decision returned alongside is still printed.
    r.middleware("def session_start(inp, state):\n    state['n'] = 1\n\n"
                 "def before_tool(inp, state):\n    state[('a', 'b')] = 1\n    return {'decision': 'deny', 'reason': 'no'}\n")
    call("SessionStart")
    assert call("PreToolUse", SHELL_PRE)["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert r.audit()[-1]["error"].startswith("state not saved: TypeError") and r.audit()[-1]["decision"] == "deny"
    assert r.state() == {"n": 1}
    # A rejected decision at a flush point does not lose the queued context.
    r.middleware("def after_tool(inp, state):\n    return {'decision': 'nope'}\n")
    (r.state_dir / "s-1.json").write_text('{"_pending": ["kept"]}')
    assert call("PostToolUse", {**SHELL_PRE, "tool_response": "x"}) is None
    assert r.audit()[-1]["error"].startswith("ValueError: after_tool decision") and r.state() == {"_pending": ["kept"]}
    # A malformed payload and an overlong session id are not the middleware's problem either.
    r.middleware("def before_tool(inp, state):\n    return {'context': inp.tool_kind}\n")
    assert call("PreToolUse", {"tool_name": 5, "tool_input": {}}) == {
        "hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "other"}}
    assert call("PreToolUse", payload("PreToolUse", session_id="s" * 300, **SHELL_PRE)) is not None
    assert r.audit()[-1]["error"] is None
    (name,) = [p.name for p in r.state_dir.iterdir() if p.name.startswith("sss")]
    assert len(name) < 120 and name.endswith(".json")


def test_the_event_comes_from_the_runner_and_a_disagreeing_payload_is_an_error(run):
    call, r = run(middleware="def before_tool(inp, state):\n    return {'context': 'event=' + inp.event}\n")
    without = {k: v for k, v in payload("PreToolUse", **SHELL_PRE).items() if k != "hook_event_name"}
    assert call("PreToolUse", raw=json.dumps({**without, "cwd": str(r.workspace)}))["hookSpecificOutput"][
        "additionalContext"] == "event=before_tool"
    assert call("PreToolUse", raw=json.dumps({**payload("PostToolUse", **SHELL_PRE), "cwd": str(r.workspace)})) is None
    assert r.audit()[-1]["error"] == "hook_event_name 'PostToolUse' does not match the runner's event 'PreToolUse'"


def test_the_error_field_is_capped_like_the_command(run):
    call, r = run(middleware="def before_tool(inp, state):\n    raise ValueError('unexpected input ' + repr(inp.tool_input))\n")
    secret = "curl -H 'Authorization: Bearer " + "t" * 600 + "'"
    assert call("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": secret}}) is None
    line = r.audit()[-1]
    assert line["error"].startswith("ValueError: unexpected input") and len(line["error"]) == 500
    assert len(line["command"]) == 200


def test_strict_exits_non_zero_on_an_error(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("HERMIT_HOOK_LOGS", raising=False)
    r = Runner(tmp_path, "def before_tool(inp, state):\n    raise RuntimeError('kaboom')\n")
    data = json.dumps({**payload("PreToolUse", **SHELL_PRE), "cwd": str(r.workspace)})
    assert runner.run("PreToolUse", data, adapter=r.adapter, strict=True) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "RuntimeError: kaboom" in captured.err and "Traceback" in captured.err
    assert r.audit()[-1]["error"] == "RuntimeError: kaboom"
    # The same call without --strict, and a healthy one with it, exit 0; main() reads the flag from argv.
    assert runner.run("PreToolUse", data, adapter=r.adapter) == 0
    r.middleware("def before_tool(inp, state):\n    return None\n")
    assert runner.run("PreToolUse", data, adapter=r.adapter, strict=True) == 0
    monkeypatch.setattr(sys, "stdin", io.StringIO(data))
    with pytest.raises(SystemExit) as info:                           # the template's own middleware: the seed's;
        runner.main(["PreToolUse", "--strict", "--host", "codex"])      # the template was not deployed: --host
    assert info.value.code == 0
    monkeypatch.setattr(sys, "stdin", io.StringIO(data))
    with pytest.raises(SystemExit) as info:
        runner.main(["PreToolUse", "--strict"])                         # no record and no --host: an error
    assert info.value.code == 1


def test_unknown_events_and_bad_stdin_give_no_output(run, tmp_path, monkeypatch):
    call, r = run()
    assert call("PreCompact", payload("PreCompact")) is None
    assert r.audit()[-1]["error"] == "unknown event 'PreCompact'"
    assert call("SubagentStart", payload("SubagentStart", agent_id="ag-1")) is None
    assert r.audit()[-1]["error"] == "unknown event 'SubagentStart'"
    assert call("PreToolUse", {"hook_event_name": "PreToolUse", "session_id": "s-1", "cwd": "/w"}) is None   # no tool
    assert r.audit()[-1]["error"] is None and r.audit()[-1]["tool_name"] is None
    # stdin that is no JSON object has no cwd: the error is audited in the deployment's workspace all the same.
    assert call("PreToolUse", raw="not json") is None
    assert len(r.audit()) == 4 and r.audit()[-1]["error"].startswith("JSONDecodeError")
    monkeypatch.setenv("HERMIT_HOOK_LOGS", str(tmp_path / "logs"))
    assert call("PreToolUse", raw="not json") is None
    assert call("PreToolUse", raw="[1, 2]") is None
    errors = [json.loads(l)["error"] for l in (tmp_path / "logs" / "hook-calls.jsonl").read_text().splitlines()]
    assert errors[0].startswith("JSONDecodeError") and errors[1] == "TypeError: hook payload must be a JSON object"
    assert call("SessionStart", raw="")["hookSpecificOutput"]["additionalContext"] == "session  task="


# --- the runner as Codex runs it: a process in the adapter's copy ------------------------------------------------

def _invoke(adapter: Path, event: str, data, env=None, python=(sys.executable, "-B")):
    return subprocess.run([*python, "hook_runner.py", event], input=json.dumps(data), capture_output=True,
                          text=True, cwd=adapter, env=env if env is not None else
                          {k: v for k, v in os.environ.items() if k != "HERMIT_HOOK_LOGS"})


COUNTER = '''\
import time

def before_tool(inp, state):
    n = state.get("n", 0)
    time.sleep(1.0)                      # longer than the interpreters' startup stagger: unlocked, a later process reads the stale value
    state["n"] = n + 1
    return None
'''


def test_state_is_locked_across_concurrent_calls(tmp_path):
    r = Runner(tmp_path, COUNTER)
    results = []

    def one():
        results.append(_invoke(r.adapter, "PreToolUse", {**payload("PreToolUse", **SHELL_PRE), "cwd": str(r.workspace)}))

    threads = [threading.Thread(target=one) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(p.returncode == 0 and p.stdout == "" for p in results), [p.stderr for p in results]
    assert r.state()["n"] == 3
    assert len(r.audit()) == 3 and all(a["error"] is None for a in r.audit())


PRINTER = '''\
import subprocess
print("imported")

def before_tool(inp, state):
    print("debug: checking", inp.command)
    subprocess.run(["echo", "child noise"])        # a child with the inherited descriptor 1
    return {"decision": "deny", "reason": "no"}
'''


def test_what_the_middleware_prints_never_reaches_the_wire(tmp_path):
    r = Runner(tmp_path, PRINTER)
    proc = _invoke(r.adapter, "PreToolUse", {**payload("PreToolUse", **SHELL_PRE), "cwd": str(r.workspace)})
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"   # one JSON document
    assert "imported" in proc.stderr and "debug: checking echo hello" in proc.stderr and "child noise" in proc.stderr
    assert r.audit()[-1]["error"] is None and not (r.adapter / "__pycache__").exists()


def test_the_runner_works_from_the_copy_with_the_stdlib_only(tmp_path):
    """-I -S, no PYTHON* variables, no site-packages, and no -B: the runner imports its own hermit_hooks.py, names its
    directory as the adapter, and writes no byte code there."""
    r = Runner(tmp_path, textwrap.dedent('''\
        import hermit_hooks
        from hermit_hooks import HookResult

        def before_tool(inp, state):
            return HookResult(decision="deny", reason=hermit_hooks.__file__ + "|" + inp.adapter_dir)
    '''))
    proc = _invoke(r.adapter, "PreToolUse", {**payload("PreToolUse", **SHELL_PRE), "cwd": str(r.workspace)},
                   env={"PATH": os.environ.get("PATH", "")}, python=(sys.executable, "-I", "-S"))
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)["hookSpecificOutput"]
    interface, adapter = out["permissionDecisionReason"].split("|")
    assert out["permissionDecision"] == "deny" and Path(adapter) == r.adapter.resolve()
    assert Path(interface).resolve() == (r.adapter / "hermit_hooks.py").resolve()
    assert r.audit()[-1]["decision"] == "deny" and not list(r.adapter.rglob("__pycache__"))
    # main() with no event, or two, is an unknown event: audited, exit 0, no output
    unnamed = {k: v for k, v in payload("PreToolUse", **SHELL_PRE).items() if k != "hook_event_name"}
    proc = _invoke(r.adapter, "", {**unnamed, "cwd": str(r.workspace)})
    assert proc.returncode == 0 and proc.stdout == "" and r.audit()[-1]["error"] == "unknown event ''"


def test_a_middleware_with_a_dataclass_loads(tmp_path):
    """The middleware is imported as a module of its own, registered as an import would register it: a dataclass
    under postponed annotations resolves its module by name. A module that fails to import is not left behind."""
    adapter = tmp_path / "adapter"
    adapter.mkdir()
    (adapter / "middleware.py").write_text("from __future__ import annotations\nfrom dataclasses import dataclass\n\n\n"
                                           "@dataclass\nclass _Rule:\n    pattern: str\n\n\n"
                                           "RULES = [_Rule('rm -rf')]\n")
    module = runner.load_middleware(adapter / "middleware.py", adapter)
    assert module.RULES[0].pattern == "rm -rf"
    (adapter / "middleware.py").write_text("raise RuntimeError('broken')\n")
    with pytest.raises(RuntimeError):
        runner.load_middleware(adapter / "middleware.py", adapter)
    assert "adapter_middleware" not in sys.modules
