"""The ahe template on each host it supports (hook_hosts.py): the hooks file registers every host's events, each
host's wire turns its real payloads (tests/fixtures/ahe_hosts/, recorded from the hosts' own CLIs) into the one
HookInput and the one HookResult into outputs the host reads, the template check's synthetic events have the real
ones' shape, the runner takes the host and the workspace from the deployment (so a Claude Code `cd` moves neither the
logs nor the task), runs before_model where Claude Code has no event for it and hands its context over when Codex's
would be, keeps a sub-agent's queue apart, the capability table EVOLVER.md shows is the module's own, both deployments
install the hooks that the template check runs, a Pi agent running a hook by hand gets the memory, and the Evolver's
texts name the acting agent's event stream. With the CLIs installed, both hosts run on a scripted local model (no
model, no Docker, no network)."""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

import test_ahe_template as T
from test_ahe_template import tmp_path                                 # noqa: F401 - the fixture outside /tmp

TEMPLATE = T.TEMPLATE
seed, problems = T.seed, T.problems


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = _load("ahe_hosts_runner", TEMPLATE / "hook_runner.py")
wires = runner.hosts
HookResult = runner.hooks.HookResult
CODEX, CLAUDE = wires.CODEX, wires.CLAUDE_CODE


@pytest.fixture(autouse=True)
def own_sys_path(monkeypatch):
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.delenv("HERMIT_HOOK_LOGS", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)


# --- the hooks file and the table -------------------------------------------------------------------------------

FIXTURES = T.ROOT / "tests" / "fixtures" / "ahe_hosts"


def fixtures(host):
    """name -> the real payload (tests/fixtures/ahe_hosts/<host>/<name>.json)."""
    return {f.stem: json.loads(f.read_text()) for f in sorted((FIXTURES / host).glob("*.json"))}


def test_the_hooks_file_registers_every_event_of_every_host_and_nothing_else():
    hooks = json.loads((TEMPLATE / "hooks.json").read_text())["hooks"]
    assert set(hooks) == set(CODEX.events) | set(CLAUDE.events) == set(wires.PI.events)
    assert set(wires.HOSTS) == {"codex", "claude-code", "pi"} and wires.HOOK_HOSTS == ("codex", "claude-code")
    for wire in wires.HOSTS.values():
        assert set(wire.flush) <= set(wire.events) and set(wire.specific) <= set(wire.events)
        assert {f for f in wire.events.values() if f} <= set(runner.hooks.MIDDLEWARE_EVENTS)
    assert "PreModelRequest" in CODEX.events and "PreModelRequest" not in CLAUDE.events
    assert "PostToolUseFailure" in CLAUDE.events and "PostToolUseFailure" not in CODEX.events


def test_evolvers_host_table_is_the_modules_own():
    """EVOLVER.md (what the Evolve Agent reads) and the method page carry hook_hosts.CAPABILITIES as it is."""
    guide = (TEMPLATE / ".hermit" / "EVOLVER.md").read_text()
    begin, end = "<!-- hook_hosts.capability_table: begin -->\n", "\n<!-- hook_hosts.capability_table: end -->"
    for text in (guide, (T.ROOT / "docs" / "methods" / "ahe_adapter.md").read_text()):
        assert text[text.index(begin) + len(begin):text.index(end)] == wires.capability_table()
    rows = wires.capability_table().splitlines()
    assert rows[0] == "| | Codex | Claude Code |" and len(rows) == 2 + len(wires.CAPABILITIES)
    assert {row[0] for row in wires.CAPABILITIES} >= {f"`{f}`" for f in ("session_start", "before_tool", "after_tool",
                                                                         "stop", "before_model")}
    check = T.template_check()
    assert f"slower than {check.DRY_RUN_BUDGET_S} s" in " ".join(guide.split())   # the guide's budget is the check's


# --- each host's wire --------------------------------------------------------------------------------------------

def test_codex_reports_a_failed_command_as_a_post_tool_use_without_an_exit_code():
    """Codex 0.164 (a scripted model): `exit 3` comes back as PostToolUse, its response the plain output string."""
    payload = {"session_id": "s", "turn_id": "t", "cwd": "/w", "hook_event_name": "PostToolUse", "model": "m",
               "permission_mode": "bypassPermissions", "tool_name": "Bash",
               "tool_input": {"command": "echo out-before; echo err-msg >&2; exit 3"},
               "tool_response": "out-before\nerr-msg\n", "tool_use_id": "call_fail"}
    inp = CODEX.normalise("PostToolUse", payload, "/a", "/w", "task", {})
    assert (inp.event, inp.host, inp.tool_kind, inp.command) == ("after_tool", "codex", "shell",
                                                                 "echo out-before; echo err-msg >&2; exit 3")
    assert (inp.output_text, inp.exit_code, inp.failed, inp.workspace) == ("out-before\nerr-msg\n", None, None, "/w")
    patch = CODEX.normalise("PostToolUse", {**payload, "tool_name": "apply_patch", "tool_input": {"command": "x"},
                                            "tool_response": "Exit code: 1\nOutput:\nfailed"}, "/a")
    assert (patch.tool_kind, patch.exit_code, patch.failed) == ("patch", 1, True)


def test_claude_code_reports_a_failed_call_as_post_tool_use_failure():
    base = {"session_id": "s", "transcript_path": "/t.jsonl", "cwd": "/w/sub", "permission_mode": "bypassPermissions",
            "tool_name": "Bash", "tool_input": {"command": "exit 3", "description": "d"}, "tool_use_id": "toolu_1"}
    failed = CLAUDE.normalise("PostToolUseFailure", {**base, "hook_event_name": "PostToolUseFailure",
                                                     "error": "Exit code 3\nboom", "is_interrupt": False}, "/a", "/w")
    assert (failed.event, failed.failed, failed.exit_code, failed.output_text) == ("after_tool", True, 3,
                                                                                   "Exit code 3\nboom")
    assert (failed.cwd, failed.workspace) == ("/w/sub", "/w")
    ok = CLAUDE.normalise("PostToolUse", {**base, "hook_event_name": "PostToolUse", "duration_ms": 12,
                                          "tool_response": {"stdout": "out", "stderr": "err", "interrupted": False,
                                                            "isImage": False, "noOutputExpected": False}}, "/a")
    assert (ok.event, ok.failed, ok.exit_code, ok.output_text, ok.duration_ms) == ("after_tool", False, None,
                                                                                   "out\nerr", 12)
    read = CLAUDE.normalise("PostToolUse", {**base, "hook_event_name": "PostToolUse", "tool_name": "Read",
                                            "tool_input": {"file_path": "/f"},
                                            "tool_response": {"type": "text", "file": {"filePath": "/f",
                                                                                       "content": "body"}}}, "/a")
    assert (read.tool_kind, read.output_text) == ("read", "body")
    assert CLAUDE.tool_kind("Edit") == "write" and CLAUDE.tool_kind("Task") == "other"


@pytest.mark.parametrize("wire", [CODEX, CLAUDE], ids=["codex", "claude-code"])
def test_every_result_becomes_an_output_the_host_reads(wire):
    results = [None, HookResult(context="c"), HookResult(decision="deny", reason="r", context="c"),
               HookResult(decision="allow", updated_input={"command": "ls"}), HookResult(decision="block", reason="r"),
               HookResult(stop_session=True, reason="r", system_message="m")]
    for event, function in wire.events.items():
        for result in results:
            if function is None and result is not None:          # an event the runner calls no function at
                continue
            if result is not None and result.decision and function not in ("before_tool", "after_tool", "stop"):
                continue
            if result is not None and result.decision == "allow" and function != "before_tool":
                continue
            state = {"_pending": ["queued"]}
            out = wire.translate(event, result, state)
            assert wire.output_problems(event, out) == [], (event, result, out)
            blocked = event == "Stop" and result is not None and result.decision in ("block", "deny")
            if event in wire.flush and (event != "Stop" or blocked):
                assert not state.get("_pending") and "queued" in json.dumps(out), (event, result, out)
            elif event == "Stop":                                       # a stop that goes through: the queue waits
                assert state == {"_pending": ["queued"]} and "additionalContext" not in json.dumps(out)
    assert wire.output_problems("PreToolUse", {"hookSpecificOutput": {"hookEventName": "Stop"}})
    assert wire.output_problems("Stop", {"decision": "block", "bogus": 1})


def test_a_failed_calls_note_is_claude_codes_post_tool_use_failure_output():
    out = CLAUDE.translate("PostToolUseFailure", HookResult(context="exit 3: read the stderr"), {"_pending": ["q"]})
    assert out == {"hookSpecificOutput": {"hookEventName": "PostToolUseFailure",
                                          "additionalContext": "exit 3: read the stderr\n\nq"}}


def test_the_payload_before_model_gets_is_the_sessions():
    """The session's and the agent's fields of the event it follows, the turn included; no tool call."""
    batch = fixtures("claude-code")["PostToolBatch"]
    request = CLAUDE.model_request("PostToolBatch", batch)
    assert request["hook_event_name"] == "PreModelRequest" and request["after"] == "PostToolBatch"
    assert request["turn_id"] == request["prompt_id"] == batch["prompt_id"] and "tool_calls" not in request
    inp = CLAUDE.normalise("PreModelRequest", request, "/a", "/w", "", {}, function="before_model")
    assert (inp.event, inp.tool_name, inp.output_text, inp.turn_id) == ("before_model", None, None, batch["prompt_id"])
    sub = fixtures("claude-code")["PreToolUse-Bash-subagent"]
    inp = CLAUDE.normalise("PreModelRequest", CLAUDE.model_request("PostToolBatch", sub), "/a", function="before_model")
    assert inp.agent_id == sub["agent_id"] and inp.raw["agent_type"] == sub["agent_type"]


# --- the real payloads -------------------------------------------------------------------------------------------

def test_codexs_real_payloads_become_the_hookinput_the_texts_describe():
    got = {name: CODEX.normalise(p["hook_event_name"], p, "/a", "/probe/workspace") for name, p in fixtures("codex").items()}
    assert (got["SessionStart"].event, got["SessionStart"].source) == ("session_start", "startup")
    assert got["SessionStart-resume"].source == "resume"                 # Codex fires it again on `exec resume`
    assert got["UserPromptSubmit"].event == "" and got["PreModelRequest"].event == "before_model"
    bash, failed = got["PostToolUse-Bash"], got["PostToolUse-Bash-failed"]
    assert (bash.tool_kind, bash.failed, bash.exit_code) == ("shell", None, None)
    assert (failed.output_text, failed.exit_code, failed.failed) == ("out-before\nerr-msg\n", None, None)
    patch = got["PostToolUse-apply_patch"]
    assert (patch.tool_kind, patch.exit_code, patch.failed, patch.command) == ("patch", 0, False, None)
    assert got["Stop"].last_message and got["Stop-active"].raw["stop_hook_active"] is True
    assert all(i.turn_id for name, i in got.items() if not name.startswith("SessionStart"))
    assert all(i.model == "gpt-5.6-luna" and i.agent_id is None for i in got.values())


def test_claude_codes_real_payloads_become_the_hookinput_the_texts_describe():
    got = {name: CLAUDE.normalise(p["hook_event_name"], p, "/a", "/probe/ws") for name, p in fixtures("claude-code").items()}
    bash = got["PostToolUseFailure-Bash"]
    assert (bash.event, bash.failed, bash.exit_code) == ("after_tool", True, 3) and bash.output_text.startswith("Exit code 3")
    read = got["PostToolUseFailure-Read"]
    assert (read.tool_kind, read.failed, read.exit_code) == ("read", True, None) and "does not exist" in read.output_text
    assert got["PostToolUse-Read"].output_text and got["PostToolUse-Read"].failed is False
    assert got["PostToolUse-Write"].output_text is None and got["PostToolUse-Edit"].output_text is None
    moved = got["PostToolUse-Bash-after-cd"]
    assert moved.cwd.endswith("/sub") and moved.workspace == "/probe/ws" and moved.duration_ms is not None
    assert got["PreToolUse-Bash-subagent"].agent_id and got["PreToolUse-Bash"].agent_id is None
    assert all(i.turn_id == i.raw["prompt_id"] for name, i in got.items() if name != "SessionStart")
    assert all(i.model is None for i in got.values()) and got["SessionStart"].source == "startup"
    assert got["PostToolBatch"].event == "" and got["Stop-active"].raw["stop_hook_active"] is True


@pytest.mark.parametrize("host", ["codex", "claude-code"])
def test_the_checks_synthetic_events_have_the_real_ones_shape(host):
    """The template check runs a middleware on hook_hosts' samples: each has the keys of the host's real payload of
    that event (a sub-agent's own two aside), so a middleware that reads what the host sends is not refused."""
    wire = wires.HOSTS[host]
    tools = ("Bash", "Read", "Write") if host == "claude-code" else ("Bash", "apply_patch")   # the session's own
    real = {}
    for p in fixtures(host).values():
        tool = p.get("tool_name") if p.get("tool_name") in tools else "Bash"
        real.setdefault((p["hook_event_name"], tool), set()).update(set(p) - {"agent_id", "agent_type"})
    assert {tool for _, tool in real} == set(tools)
    for (event, tool), keys in real.items():
        sample = wire.sample(event, "/w", "s", tool=tool)
        assert set(sample) == keys, (event, tool, sorted(set(sample) ^ keys))
        if "tool_response" in sample and event == "PostToolUse":
            real_response = next(p["tool_response"] for p in fixtures(host).values()
                                 if p["hook_event_name"] == event and (p.get("tool_name") or "Bash") == tool)
            assert type(sample["tool_response"]) is type(real_response), (event, tool)
            if isinstance(real_response, dict):
                assert set(sample["tool_response"]) <= set(real_response), (event, tool)
    session = wire.session("/w", "s")
    assert [e for e, _ in session][:2] == ["SessionStart", "UserPromptSubmit"] and session[-1][0] == "Stop"
    assert session[-1][1]["stop_hook_active"] is True and sum(1 for e, _ in session if e == "PreToolUse") >= 2
    assert {e for e, _ in session} == set(wire.events)                  # every event the host fires, at least once


@pytest.mark.parametrize("host", ["codex", "claude-code"])
def test_every_real_payload_runs_through_a_deployed_copy_without_an_error(tmp_path, capsys, monkeypatch, host):
    work, copy = _deployed(tmp_path, host)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(work))
    for name, payload in fixtures(host).items():
        capsys.readouterr()
        assert runner.run(payload["hook_event_name"], json.dumps(payload), adapter=copy) == 0, name
        text = capsys.readouterr().out
        out = json.loads(text) if text.strip() else None
        assert wires.HOSTS[host].output_problems(payload["hook_event_name"], out) == [], (name, out)
    assert all(line["error"] is None for line in _audit(work))


@pytest.mark.parametrize("event, result, active, follows", [
    ("UserPromptSubmit", None, False, True), ("PostToolBatch", None, False, True),
    ("PostToolUse", None, False, False), ("PostToolUseFailure", None, False, False),     # PostToolBatch follows them
    ("PreToolUse", HookResult(decision="deny", reason="r"), False, False),               # and a denied call
    ("Stop", None, False, False), ("Stop", HookResult(decision="block", reason="r"), False, True),
    ("Stop", HookResult(decision="block", reason="r"), True, False),
    ("Stop", HookResult(decision="block", reason="r", stop_session=True), False, False),
    ("SessionStart", None, False, False)])
def test_where_a_model_request_follows_on_claude_code(event, result, active, follows):
    assert CLAUDE.model_follows(event, result, active) is follows
    assert CODEX.model_follows(event, result, active) is False            # Codex fires PreModelRequest itself


def test_the_host_and_the_workspace_are_the_deployments(tmp_path, monkeypatch):
    work = tmp_path / "w"
    copy = work / "hermit" / "ahe"
    copy.mkdir(parents=True)
    assert wires.deployment(copy) is None
    with pytest.raises(LookupError, match="no host"):
        wires.resolve(None, copy, {"cwd": str(work)})
    assert wires.resolve("codex", copy, {"cwd": "/x"}) == (CODEX, "/x")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/project")
    assert wires.resolve("claude-code", copy, {"cwd": "/project/sub"}) == (CLAUDE, "/project")
    (work / ".hermit-deploy.json").write_text(json.dumps({"host": "claude-code", "adapter_dir": str(copy)}))
    assert wires.resolve(None, copy, {"cwd": "/elsewhere"}) == (CLAUDE, str(work.resolve()))
    (work / ".hermit-deploy.json").write_text(json.dumps({"host": "pi", "adapter_dir": str(copy)}))
    assert wires.resolve(None, copy, {}) == (wires.PI, str(work.resolve()))     # a hook the agent runs by hand
    (work / ".hermit-deploy.json").write_text(json.dumps({"host": "goose", "adapter_dir": str(copy)}))
    with pytest.raises(LookupError, match="runs none of this template's hooks"):
        wires.resolve(None, copy, {})
    (work / ".hermit-deploy.json").write_text(json.dumps({"host": "codex", "adapter_dir": str(work / "hermit" / "x")}))
    assert wires.deployment(copy) is None                                 # another adapter's record
    (work / ".hermit-deploy.json").write_text("{not json")
    assert wires.deployment(copy) is None


# --- the runner on a Claude Code deployment ---------------------------------------------------------------------

LOGGING = '''\
from hermit_hooks import HookResult

MEMORY = "MEMORY.md"


def _log(state, *item):
    state.setdefault("log", []).append(list(item))


def session_start(inp, state):
    _log(state, "session_start", inp.host, inp.workspace, inp.task_text)
    return HookResult(context="memory " + open(inp.adapter_dir + "/" + MEMORY).read().strip().splitlines()[-1])


def before_tool(inp, state):
    _log(state, "before_tool", inp.command)
    if "rm -rf" in (inp.command or ""):
        return HookResult(decision="deny", reason="list it first")
    return None


def after_tool(inp, state):
    _log(state, "after_tool", inp.failed, inp.exit_code, inp.output_text, inp.cwd, inp.workspace)
    return HookResult(context=f"noted {inp.output_text}")


def stop(inp, state):
    _log(state, "stop", inp.last_message)
    if not state.get("stopped"):
        state["stopped"] = True
        return HookResult(decision="block", reason="check the units")
    return None


def before_model(inp, state):
    n = state["models"] = state.get("models", 0) + 1
    _log(state, "before_model", inp.raw.get("after"), inp.tool_name)
    inp.queue_context(f"model call {n}")
    return None
'''


def _deployed(tmp_path, host, middleware=LOGGING):
    """The seed with `middleware`, deployed for `host` by the framework: (workspace, the adapter's copy)."""
    module = {"codex": "hermit.hosts.codex", "claude-code": "hermit.hosts.claude_code"}[host]
    prepare = importlib.import_module(module).prepare
    adapter = seed(tmp_path)
    (adapter / "middleware.py").write_text(middleware)
    (adapter / "MEMORY.md").write_text(T.MEMORY_WITH_ENTRIES)
    work = tmp_path / f"work-{host}"
    result = prepare(adapter, work, sys.executable)
    return work.resolve(), Path(result["adapter_dir"])


def _call(work, copy, event, capsys, **fields):
    payload = {"session_id": "s-1", "transcript_path": "/t.jsonl", "cwd": str(work), "hook_event_name": event,
               "permission_mode": "bypassPermissions", **fields}
    capsys.readouterr()
    assert runner.run(event, json.dumps(payload), adapter=copy) == 0
    out = capsys.readouterr().out
    return json.loads(out) if out.strip() else None


def _audit(work):
    return [json.loads(l) for l in (work / ".hermit-hooks" / "hook-calls.jsonl").read_text().splitlines()]


def _state(work):
    """The session's state: the one state file of a workspace with one session."""
    (state,) = [json.loads(f.read_text()) for f in (work / ".hermit-hooks" / "hook-state").glob("*.json")]
    return state


def test_on_claude_code_before_model_runs_where_a_request_follows_and_its_context_goes_out_next(tmp_path, capsys,
                                                                                               monkeypatch):
    """Claude Code's events in the order it fires them (a prompt, a call, its batch, a failed call after a `cd`, a
    denied call, a blocked stop, the stop that goes through): before_model once per model request, its context out
    with the next tool result or blocked stop, as Codex's PreModelRequest's; the workspace stays the project."""
    work, copy = _deployed(tmp_path, "claude-code")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(work))
    (work / "task.md").write_text("the question\n")
    bash = {"tool_name": "Bash", "tool_input": {"command": "echo out", "description": "d"}, "tool_use_id": "t1"}

    def batch(**call):
        return _call(work, copy, "PostToolBatch", capsys, **{k: v for k, v in call.items() if k == "cwd"},
                     tool_calls=[{**bash, "tool_response": "x"}])
    start = _call(work, copy, "SessionStart", capsys, source="startup")
    assert start["hookSpecificOutput"]["additionalContext"].startswith("memory - Freeze the published state")
    assert _call(work, copy, "UserPromptSubmit", capsys, prompt="Read task.md") is None      # queued, not yet out
    assert _call(work, copy, "PreToolUse", capsys, **bash) is None
    post = _call(work, copy, "PostToolUse", capsys, **bash, duration_ms=3,
                 tool_response={"stdout": "out", "stderr": "", "interrupted": False, "isImage": False,
                                "noOutputExpected": False})
    assert post["hookSpecificOutput"] == {"hookEventName": "PostToolUse", "additionalContext": "noted out\n\nmodel call 1"}
    assert batch() is None                                                     # model call 2 queued
    # The agent cds into a subdirectory: Claude Code's cwd follows it; the logs, the state and the task do not.
    sub = work / "sub"
    sub.mkdir()
    failed = _call(work, copy, "PostToolUseFailure", capsys, **{**bash, "cwd": str(sub)}, error="Exit code 2\nno",
                   is_interrupt=False)
    assert failed["hookSpecificOutput"] == {"hookEventName": "PostToolUseFailure",
                                            "additionalContext": "noted Exit code 2\nno\n\nmodel call 2"}
    assert batch(cwd=str(sub)) is None                                         # model call 3 queued
    denied = _call(work, copy, "PreToolUse", capsys, **{**bash, "cwd": str(sub), "tool_input": {"command": "rm -rf b"}})
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert batch(cwd=str(sub)) is None                                         # a denied call's batch: call 4 queued
    stop = _call(work, copy, "Stop", capsys, stop_hook_active=False, last_assistant_message="42")
    assert stop["decision"] == "block"                                         # what the batches queued goes out here
    assert stop["hookSpecificOutput"]["additionalContext"] == "model call 3\n\nmodel call 4"
    assert _call(work, copy, "Stop", capsys, stop_hook_active=True, last_assistant_message="42 m") is None
    assert not (sub / ".hermit-hooks").exists()
    log = _state(work)["log"]
    assert log[0] == ["session_start", "claude-code", str(work), "the question\n"]
    assert [entry[:2] for entry in log if entry[0] == "before_model"] == [
        ["before_model", "UserPromptSubmit"], ["before_model", "PostToolBatch"], ["before_model", "PostToolBatch"],
        ["before_model", "PostToolBatch"], ["before_model", "Stop"]]
    assert all(entry[2] is None for entry in log if entry[0] == "before_model")           # no tool under way
    assert [e for e in log if e[0] == "after_tool"] == [
        ["after_tool", False, None, "out", str(work), str(work)],
        ["after_tool", True, 2, "Exit code 2\nno", str(sub), str(work)]]
    lines = _audit(work)
    assert [(l["event"], l.get("at")) for l in lines if l["event"] == "PreModelRequest"] == [
        ("PreModelRequest", "UserPromptSubmit"), ("PreModelRequest", "PostToolBatch"),
        ("PreModelRequest", "PostToolBatch"), ("PreModelRequest", "PostToolBatch"), ("PreModelRequest", "Stop")]
    assert all(l["error"] is None for l in lines)
    assert _state(work)["_pending"] == ["model call 5"]       # the stop that went through left the queue as it was


def test_a_codex_deployment_runs_no_before_model_of_the_runners(tmp_path, capsys):
    work, copy = _deployed(tmp_path, "codex")
    bash = {"tool_name": "Bash", "tool_input": {"command": "echo out"}, "tool_use_id": "t1", "turn_id": "u"}
    _call(work, copy, "UserPromptSubmit", capsys, prompt="p", turn_id="u")
    post = _call(work, copy, "PostToolUse", capsys, **bash, tool_response="out\n")
    assert post == {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "noted out\n"}}
    assert _call(work, copy, "PreModelRequest", capsys, turn_id="u", model="m") is None      # Codex's own: queued
    stop = _call(work, copy, "Stop", capsys, turn_id="u", stop_hook_active=False, last_assistant_message="x")
    assert stop == {"decision": "block", "reason": "check the units\n\nmodel call 1"}   # Codex reads the reason
    assert _audit(work)[-1]["context_chars"] == len("model call 1")    # the queue it carried, in the audit
    assert [l["event"] for l in _audit(work)] == ["UserPromptSubmit", "PostToolUse", "PreModelRequest", "Stop"]
    assert not any("at" in l for l in _audit(work))


def test_a_slow_before_model_of_the_runners_costs_only_its_own_context(tmp_path, capsys, monkeypatch):
    """The call has one deadline; when it runs out in the before_model the runner called, the event's own output
    still goes out and only that before_model's line carries the error."""
    slow = LOGGING.replace("def before_model(inp, state):\n", "def before_model(inp, state):\n    import time\n"
                                                              "    time.sleep(5)\n")
    work, copy = _deployed(tmp_path, "claude-code", slow)
    monkeypatch.setattr(runner, "TIMEOUT_S", 4)                         # the alarm at 2 s
    prompt = _call(work, copy, "UserPromptSubmit", capsys, prompt="p")        # its own output: none
    assert prompt is None
    own, follow = _audit(work)
    assert own["event"] == "UserPromptSubmit" and own["error"] is None
    assert follow["event"] == "PreModelRequest" and follow["at"] == "UserPromptSubmit"
    assert follow["error"] == "MiddlewareTimeout: middleware timed out"
    stop = LOGGING.replace("def before_model(inp, state):\n", "def before_model(inp, state):\n    import time\n"
                                                               "    time.sleep(5)\n")
    (copy / "middleware.py").write_text(stop)
    blocked = _call(work, copy, "Stop", capsys, stop_hook_active=False, last_assistant_message="x")
    assert blocked == {"decision": "block", "reason": "check the units"}       # the event's output still goes out
    assert _audit(work)[-1]["at"] == "Stop" and _audit(work)[-1]["error"].startswith("MiddlewareTimeout")


def test_a_slow_after_tool_is_cut_by_the_alarm_and_leaves_one_audit_line(tmp_path, capsys, monkeypatch):
    slow = LOGGING.replace("def after_tool(inp, state):\n", "def after_tool(inp, state):\n    import time\n"
                                                            "    time.sleep(5)\n")
    work, copy = _deployed(tmp_path, "claude-code", slow)
    monkeypatch.setattr(runner, "TIMEOUT_S", 4)
    bash = {"tool_name": "Bash", "tool_input": {"command": "echo out"}, "tool_use_id": "t1"}
    assert _call(work, copy, "PostToolUse", capsys, **bash, tool_response={"stdout": "out"}) is None
    (line,) = _audit(work)
    assert line["error"] == "MiddlewareTimeout: middleware timed out"


def test_a_sub_agents_queue_is_its_own(tmp_path, capsys, monkeypatch):
    """Claude Code's sub-agents share the session: what the main agent queued goes out with the main agent's next tool
    result, not the sub-agent's, and the other way round."""
    work, copy = _deployed(tmp_path, "claude-code")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(work))
    sub = {"agent_id": "a1", "agent_type": "general-purpose"}
    bash = {"tool_name": "Bash", "tool_input": {"command": "echo out", "description": "d"}, "tool_use_id": "t1",
            "tool_response": {"stdout": "out"}, "prompt_id": "p1"}
    assert _call(work, copy, "UserPromptSubmit", capsys, prompt="p", prompt_id="p1") is None   # main: model call 1
    inner = _call(work, copy, "PostToolUse", capsys, **bash, **sub)
    assert inner["hookSpecificOutput"]["additionalContext"] == "noted out"                      # the main's stays
    assert _call(work, copy, "PostToolBatch", capsys, tool_calls=[], prompt_id="p1", **sub) is None   # sub: call 2
    outer = _call(work, copy, "PostToolUse", capsys, **{**bash, "tool_name": "Agent", "tool_response": {"status": "x"}})
    assert outer["hookSpecificOutput"]["additionalContext"] == "noted None\n\nmodel call 1"
    assert _state(work)["_pending:a1"] == ["model call 2"]
    lines = [l for l in _audit(work) if l["event"] == "PreModelRequest"]
    assert [l["at"] for l in lines] == ["UserPromptSubmit", "PostToolBatch"]


def test_codex_drops_a_context_given_with_allow_and_the_check_says_so():
    out = CODEX.translate("PreToolUse", HookResult(decision="allow", context="note"), {})
    assert CODEX.output_problems("PreToolUse", out) and CODEX.delivered("PreToolUse", out, None) == ""
    kept = CODEX.translate("PreToolUse", HookResult(decision="allow", context="note", updated_input={"command": "ls"}),
                           {})
    assert CODEX.output_problems("PreToolUse", kept) == [] and CODEX.delivered("PreToolUse", kept, None) == "note"
    alone = CODEX.translate("PreToolUse", HookResult(context="note"), {})
    assert CODEX.output_problems("PreToolUse", alone) == []
    claude = CLAUDE.translate("PreToolUse", HookResult(decision="allow", context="note"), {})
    assert CLAUDE.output_problems("PreToolUse", claude) == [] and CLAUDE.delivered("PreToolUse", claude, None) == "note"


def test_a_before_model_that_leaves_the_state_unsaveable_costs_only_itself(tmp_path, capsys, monkeypatch):
    bad = LOGGING.replace("def before_model(inp, state):\n", "def before_model(inp, state):\n"
                                                              "    state[(1, 2)] = 'tuple key'\n")
    work, copy = _deployed(tmp_path, "claude-code", bad)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(work))
    _call(work, copy, "UserPromptSubmit", capsys, prompt="p")
    own, follow = _audit(work)
    assert own["error"] is None and follow["error"].startswith("state not saved: TypeError")
    assert _state(work)["_prompt"] == "p"                     # the prompt event's own change is kept


def test_text_that_cannot_be_encoded_is_escaped_not_a_crash(tmp_path, capsys):
    odd = LOGGING.replace('    return HookResult(context=f"noted {inp.output_text}")',
                          '    return HookResult(context="lone \\ud800 surrogate")')
    work, copy = _deployed(tmp_path, "codex", odd)
    payload = {"session_id": "s-1", "cwd": str(work), "hook_event_name": "PostToolUse", "tool_name": "Bash",
               "tool_input": {"command": "x"}, "tool_response": "y"}
    proc = subprocess.run([sys.executable, "-B", "hook_runner.py", "PostToolUse"], cwd=copy, input=json.dumps(payload),
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "surrogate" in json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert _audit(work)[-1]["error"] is None


def test_on_pi_a_hook_run_by_hand_hands_the_memory_over(tmp_path):
    """Pi runs no hooks: the deployment tells the agent to run each hook command itself, without a payload. The
    SessionStart one prints the memory; the others have nothing to act on and print nothing."""
    from hermit.hosts.pi import prepare
    adapter = seed(tmp_path)
    (adapter / "MEMORY.md").write_text(T.MEMORY_WITH_ENTRIES)
    work = tmp_path / "work-pi"
    result = prepare(adapter, work, sys.executable)
    agents_md = (work / "AGENTS.md").read_text()
    line = next(l for l in agents_md.splitlines() if "On `SessionStart`" in l)
    command = line.split("run `", 1)[1].rstrip("`")
    proc = subprocess.run(["sh", "-c", command], cwd=work, input="", capture_output=True, text=True,
                          env={k: v for k, v in os.environ.items() if k != "HERMIT_HOOK_LOGS"})
    assert proc.returncode == 0, proc.stderr
    assert "Freeze the published state" in json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    stop = subprocess.run(["sh", "-c", command.replace("SessionStart", "Stop")], cwd=work, input="",
                          capture_output=True, text=True)
    assert stop.returncode == 0 and stop.stdout == ""
    assert json.loads((work / ".hermit-deploy.json").read_text())["host"] == "pi" and result["host"] == "pi"
    assert [l["event"] for l in _audit(work)] == ["SessionStart", "Stop"] and not any(l["error"] for l in _audit(work))


# --- the deployments and the check ---------------------------------------------------------------------------------

@pytest.mark.parametrize("host, hooks_file", [("codex", ".codex/hooks.json"), ("claude-code", ".claude/settings.json")])
def test_each_deployment_installs_every_hook_and_runs_it_from_the_copy(tmp_path, host, hooks_file):
    work, copy = _deployed(tmp_path, host)
    record = json.loads((work / ".hermit-deploy.json").read_text())
    assert record["host"] == host and Path(record["adapter_dir"]) == copy == work / "hermit" / "ahe"
    hooks = json.loads((work / hooks_file).read_text())["hooks"]
    events = json.loads((TEMPLATE / "hooks.json").read_text())["hooks"]
    assert list(hooks) == list(events)
    for event in events:
        assert hooks[event] == [{"hooks": [{"type": "command", "timeout": 30,
                                            "command": f"cd {shlex.quote(str(copy))} || exit 1\n"
                                                       f"python3 -B hook_runner.py {event}"}]}]
    wire = wires.HOSTS[host]
    env = {k: v for k, v in os.environ.items() if k not in ("HERMIT_HOOK_LOGS", "CLAUDE_PROJECT_DIR")}
    proc = subprocess.run(["sh", "-c", hooks["SessionStart"][0]["hooks"][0]["command"]], cwd=work, env=env,
                          input=json.dumps(wire.sample("SessionStart", str(work), "s-1")), capture_output=True,
                          text=True)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert wire.output_problems("SessionStart", out) == [] and "Freeze the published state" in json.dumps(out)
    assert _audit(work)[-1]["event"] == "SessionStart" and _audit(work)[-1]["error"] is None


def test_a_claude_deployment_names_claude_codes_places(tmp_path):
    work, copy = _deployed(tmp_path, "claude-code")
    claude_md = (work / "CLAUDE.md").read_text()
    assert "Prefer short replies" in claude_md and "This adapter's files are in `hermit/ahe/`" in claude_md
    assert (work / ".claude" / "skills" / "tables" / "scripts" / "sum.py").is_file()
    assert (work / ".claude" / "agents" / "reader.md").is_file() and not (copy / "CLAUDE.md").exists()
    assert not (work / "AGENTS.md").exists()


def test_a_hook_that_fails_on_one_host_alone_fails_the_check_naming_the_host(tmp_path, monkeypatch):
    adapter = seed(tmp_path)
    T.rewrite(adapter, "middleware.py", "def after_tool(inp: HookInput, state: dict) -> HookResult | None:\n"
              "    return None\n", "def after_tool(inp: HookInput, state: dict) -> HookResult | None:\n"
              "    if inp.failed:\n        raise RuntimeError('failed calls')\n    return None\n")
    assert problems(adapter) == [("middleware.py", "middleware.py: `after_tool` fails in a dry run through "
                                  "hook_runner.py on Claude Code (PostToolUseFailure of a synthetic session, the "
                                  "state carried over from the events before it): RuntimeError: failed calls; on "
                                  "Claude Code this hook would fail open, silently")]
    import hermit.hosts.claude_code as claude_code

    def refuse(copy, workspace, python, **_):
        raise ValueError(f"cannot install {copy}/agents/reader.md")
    monkeypatch.setattr(claude_code, "prepare", refuse)
    found = [(p, t) for p, t in problems(adapter) if "does not deploy" in t]
    assert found == [("agents/reader.md", "the adapter does not deploy to Claude Code, so AGENTS.md cannot be "
                                          "measured as the agent reads it: ValueError: cannot install "
                                          "agents/reader.md")]


def test_hooks_slower_than_the_budget_fail_on_the_middleware(tmp_path, monkeypatch):
    adapter = seed(tmp_path)
    T.rewrite(adapter, "middleware.py", "def before_tool(inp: HookInput, state: dict) -> HookResult | None:\n"
              "    return None\n", "def before_tool(inp: HookInput, state: dict) -> HookResult | None:\n"
              "    import time\n    time.sleep(2)\n    return None\n")
    check = T.template_check()
    monkeypatch.setattr(check, "DRY_RUN_BUDGET_S", 3)
    found = [(i["path"], i["problem"]) for i in check.check(str(adapter))]
    assert found == [("middleware.py", "middleware.py: the dry runs of its hooks took more than 3 s together: a hook "
                                       "runs inside every tool call of every trial, so keep each one fast (no "
                                       "sleeping, no scanning of large files)")]


@pytest.mark.parametrize("name, text", [("hook_runner.py", "raise SystemExit(3)\n"),
                                        ("exports.yaml", "context: ADAPTER.md\n")])     # no hooks exported
def test_a_broken_fixed_file_is_not_the_middlewares_fault(tmp_path, name, text):
    """The dry runs deploy and run the template root's exports, runner, interface and wires: a copy whose fixed file
    is broken gets no middleware issue from the template check (check 1, the fixed files' own rule, names it)."""
    adapter = seed(tmp_path)
    (adapter / name).write_text(text)
    assert problems(adapter) == []
    from hermit.spec import check_spec
    issues = check_spec(adapter, TEMPLATE, deploy=True).issues
    assert [(i.path, i.check) for i in issues] == [(name, 1)]


def test_a_failure_of_the_runners_before_model_names_before_model(tmp_path):
    adapter = seed(tmp_path)
    T.rewrite(adapter, "middleware.py", "def before_model(inp: HookInput, state: dict) -> HookResult | None:\n"
              "    return None\n", "def before_model(inp: HookInput, state: dict) -> HookResult | None:\n"
              "    if inp.host == 'claude-code':\n        raise RuntimeError('claude only')\n    return None\n")
    assert problems(adapter) == [("middleware.py", "middleware.py: `before_model` fails in a dry run through "
                                  "hook_runner.py on Claude Code, where the runner calls it after UserPromptSubmit (a "
                                  "synthetic session): RuntimeError: claude only; on Claude Code only its context "
                                  "would be lost, silently")]


def test_a_hook_that_fails_only_at_its_second_call_fails_the_check(tmp_path):
    """The dry run is one session: a state that does not survive its round trip (a set becomes its str) breaks the
    second call, as it would every later event of a trial."""
    adapter = seed(tmp_path)
    T.rewrite(adapter, "middleware.py", "def before_tool(inp: HookInput, state: dict) -> HookResult | None:\n"
              "    return None\n", "def before_tool(inp: HookInput, state: dict) -> HookResult | None:\n"
              "    state.setdefault('seen', set()).add(inp.command)\n    return None\n")
    found = problems(adapter)
    assert len(found) == 1 and found[0][0] == "middleware.py", found
    assert "`before_tool` fails in a dry run through hook_runner.py on Codex (the second PreToolUse of a synthetic " \
           "session" in found[0][1] and "AttributeError" in found[0][1]


def test_a_long_deploy_failure_still_names_its_file(tmp_path, monkeypatch):
    import hermit.hosts.codex as codex
    adapter = seed(tmp_path)

    def refuse(copy, workspace, python, **_):
        raise RuntimeError("Traceback (most recent call last):\n" + "  frame\n" * 200
                           + f"ValueError: cannot install the role {copy}/agents/reader.md")
    monkeypatch.setattr(codex, "prepare", refuse)
    found = [(p, t) for p, t in problems(adapter) if "does not deploy to Codex" in t]
    assert len(found) == 1 and found[0][0] == "agents/reader.md" and found[0][1].endswith("agents/reader.md")


@pytest.mark.parametrize("body, said", [
    ("    import os as _o\n    _o.system('true')\n", "calls `os.system`"),
    ("    from os import popen as _p\n    _p('true')\n", "calls `os.popen`"),
    ("    from builtins import __import__\n    __import__('subprocess')\n", "calls `__import__`"),
    ("    import builtins\n    builtins.__import__('subprocess')\n", "calls `builtins.__import__`"),
    ("    import posix\n    posix.system('true')\n", "imports `posix`"),
    ("    from os import *\n    system('true')\n", "`from os import *`"),
], ids=["alias", "from-alias", "builtins-from", "builtins", "posix", "star"])
def test_a_forbidden_call_by_another_name_is_still_forbidden(tmp_path, body, said):
    adapter = seed(tmp_path)
    T.rewrite(adapter, "middleware.py", "def stop(inp: HookInput, state: dict) -> HookResult | None:\n"
              "    return None\n", "def stop(inp: HookInput, state: dict) -> HookResult | None:\n" + body
              + "    return None\n")
    found = [t for p, t in problems(adapter) if p == "middleware.py"]
    assert any(said in t for t in found), found


def test_an_output_codex_drops_part_of_fails_the_check_as_such(tmp_path):
    adapter = seed(tmp_path)
    T.rewrite(adapter, "middleware.py", "def before_tool(inp: HookInput, state: dict) -> HookResult | None:\n"
              "    return None\n", "def before_tool(inp: HookInput, state: dict) -> HookResult | None:\n"
              "    return HookResult(decision='allow', context='a note')\n")
    assert problems(adapter) == [("middleware.py", "middleware.py: `before_tool` returns an output Codex does not read "
                                  "in full, in a dry run through hook_runner.py (PreToolUse of a synthetic session, the "
                                  "state carried over from the events before it): Codex drops a PreToolUse context "
                                  "given with `allow` and no updated_input: return the context without a decision; on "
                                  "Codex that part would be dropped, silently")]


def test_a_state_with_an_unencodable_character_is_saved(tmp_path, capsys, monkeypatch):
    """A lone surrogate (a file name os.listdir gave back undecoded) in what before_model queues: the state is saved
    escaped and read back as it was, on Claude Code's runner-called before_model as on any event."""
    odd = LOGGING.replace("    inp.queue_context(f\"model call {n}\")", "    inp.queue_context(\"files: data-\\udcff.csv\")")
    assert odd != LOGGING
    work, copy = _deployed(tmp_path, "claude-code", odd)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(work))
    _call(work, copy, "UserPromptSubmit", capsys, prompt="p")
    assert _state(work)["_pending"] == ["files: data-\udcff.csv"] and _state(work)["_prompt"] == "p"
    out = _call(work, copy, "PostToolUse", capsys, tool_name="Bash", tool_input={"command": "x"},
                tool_response={"stdout": "y"})
    assert "data-" in out["hookSpecificOutput"]["additionalContext"]
    assert all(l["error"] is None for l in _audit(work))


# --- the Evolver's texts -----------------------------------------------------------------------------------------

def test_the_evolvers_texts_name_the_acting_agents_stream(tmp_path):
    """START.md, the evolution query and the Debugger's question name Claude Code's stream when the rollouts are its
    sessions, and are the files' own for Codex's."""
    from hermit.agent.evolver.methods.ahe_adapter import analysis as A, debugger as D
    methods = T.ROOT / "hermit" / "agent" / "evolver" / "methods" / "ahe_adapter"
    step = tmp_path / "current"
    for name, stream in (("a_1", "agent-codex.jsonl"), ("b_1", "agent-claude.jsonl")):
        (step / "trajectories" / name).mkdir(parents=True)
        (step / "trajectories" / name / stream).write_text("")
    codex = [{"trajectory": "trajectories/a_1", "_dir": str(step)}]
    claude = [{"trajectory": "trajectories/b_1", "_dir": str(step)}]
    assert D.rollout_stream(codex) == "agent-codex.jsonl" and D.rollout_stream([]) == "agent-codex.jsonl"
    assert D.rollout_stream(claude + codex) == "agent-claude.jsonl"
    start = (methods / "START.md").read_text()
    assert D.for_stream(start, "agent-codex.jsonl") == start
    words = D.for_stream(start, "agent-claude.jsonl")
    assert "agent-codex.jsonl" not in words and words.count("agent-claude.jsonl") == 3
    assert "the Claude Code event stream" in words and "hermit/ahe/" in words and "ahe-codex" not in start
    import test_ahe_analysis as AN
    stats = A.compute_stats([AN.rec("a", 1, **AN.PASS), AN.rec("b", 1, **AN.FAIL)])
    query = A.build_evolution_query({"learning_material": "lm", "state": "st", "adapter": "ad"}, stats, step=2)
    assert "agent-codex.jsonl" in query
    assert "agent-codex.jsonl" not in D.for_stream(query, "agent-claude.jsonl")
    trial_log = tmp_path / "trial"
    trial_log.mkdir()
    (trial_log / "agent-claude.jsonl").write_text(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "hi"}]}}) + "\n")
    job = D.Job(task="t", trials=[D.Trial(id="t#1", verdict="fail", reward=0.0, log=trial_log, verifier_output="x")],
                n_fail=1)
    question = (D.prepare_job_dir(job, tmp_path / "scratch", D.settings({})) / "question.md").read_text()
    assert "`claude -p --output-format stream-json` event stream" in question and "codex exec --json" not in question


# --- the real CLIs, each on a scripted local model ----------------------------------------------------------------
# A session of each host in a workspace the framework deployed, its model a script served on 127.0.0.1 (every proxy
# pointed at a closed port): which text reaches a request is what each channel delivers. Skipped without the CLI.

def _serve(answer):
    """A local HTTP model: `answer(path, body) -> (content type, bytes)` for every POST, each body kept."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(404)
            self.send_header("content-length", "0")
            self.end_headers()

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
            requests.append(body)
            kind, data = answer(self.path, body)
            self.send_response(200)
            self.send_header("content-type", kind)
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}", requests


def _offline(home, url_env):
    dead = "http://127.0.0.1:9"
    return {"PATH": os.environ["PATH"], "HOME": str(home), "LANG": "C.UTF-8", "SHELL": "/bin/bash",
            "HTTPS_PROXY": dead, "HTTP_PROXY": dead, "https_proxy": dead, "http_proxy": dead, "ALL_PROXY": dead,
            "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost", **url_env}


def _seen(requests, *texts):
    blob = [json.dumps(r) for r in requests]
    return {t: next((i for i, b in enumerate(blob) if t in b), None) for t in texts}


CODEX_CLI = pytest.mark.skipif(not __import__("shutil").which("codex"), reason="needs the codex CLI")
CLAUDE_CLI = pytest.mark.skipif(not __import__("shutil").which("claude"), reason="needs the claude CLI")


def _js(command):
    """A code-mode model's `exec` script that runs one command and prints its result, or the error it raised."""
    return ("try {\n  const r = await tools.exec_command({\"cmd\": " + json.dumps(command) + "});\n  text(r);\n"
            "} catch (e) {\n  text(\"CAUGHT: \" + String(e));\n}")


def _codex_turn(item):
    if isinstance(item, str):
        return [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": item}]}]
    return [{"type": "custom_tool_call", "name": "exec", "call_id": f"c{abs(hash(item['cmd'])) % 10_000}",
             "input": _js(item["cmd"])}]


def _run_codex(tmp_path, work, turns):
    """`codex exec` as the framework's Codex agent runs it, on gpt-5.6-luna (code mode), its model `turns` in order
    (a str: a message; {"cmd": ...}: an exec script running it): (the run, the requests the model got)."""
    home = tmp_path / "home"
    (home / "codex").mkdir(parents=True)
    served = []

    def answer(path, body):
        n = len(served)
        served.append(path)
        items = [dict(item, id=f"it{n}_{k}", status="completed")
                 for k, item in enumerate(_codex_turn(turns[min(n, len(turns) - 1)]))]
        events = [{"type": "response.created", "response": {"id": f"r{n}", "status": "in_progress", "output": []}}]
        events += [{"type": "response.output_item.done", "output_index": k, "item": item} for k, item in enumerate(items)]
        events.append({"type": "response.completed", "response": {
            "id": f"r{n}", "status": "completed", "output": items,
            "usage": {"input_tokens": 1, "input_tokens_details": {"cached_tokens": 0}, "output_tokens": 1,
                      "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": 2}}})
        return "text/event-stream", b"".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in events)
    server, url, requests = _serve(answer)
    env = _offline(home, {"CODEX_HOME": str(home / "codex"), "MOCK_API_KEY": "dummy"})
    try:
        run = subprocess.run(
            ["codex", "exec", "--json", "--skip-git-repo-check", "-C", str(work), "-s", "workspace-write",
             "-o", str(tmp_path / "last.txt"), "-m", "gpt-5.6-luna", "--dangerously-bypass-hook-trust",
             "-c", "model_provider=mock",
             "-c", f'model_providers.mock={{name="mock", base_url="{url}/v1", wire_api="responses", env_key="MOCK_API_KEY"}}',
             "Answer the question."],
            cwd=work, env=env, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
    finally:
        server.shutdown()
    assert run.returncode == 0, run.stderr[-2000:]
    return run, requests


def _run_claude(tmp_path, work, turns):
    """`claude -p` as the framework's Claude agent runs it, its model `turns` by the assistant messages so far (a str:
    a text; {"bash": ...}: a Bash call): (the run, the requests with tools the model got)."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    requests = []

    def answer(path, body):
        if "count_tokens" in path:
            return "application/json", json.dumps({"input_tokens": 1}).encode()
        n = sum(1 for m in body.get("messages") or [] if m.get("role") == "assistant")
        turn = turns[min(n, len(turns) - 1)] if body.get("tools") else "ok"
        block = ({"type": "tool_use", "id": f"toolu_{n}", "name": "Bash",
                  "input": {"command": turn["bash"], "description": "scripted"}} if isinstance(turn, dict)
                 else {"type": "text", "text": turn})
        stop = "tool_use" if isinstance(turn, dict) else "end_turn"
        message = {"id": f"msg_{len(served)}", "type": "message", "role": "assistant",
                   "model": body.get("model", "m"), "content": [block], "stop_reason": stop, "stop_sequence": None,
                   "usage": {"input_tokens": 1, "output_tokens": 1}}
        served.append(path)
        if body.get("tools"):
            requests.append(body)
        if not body.get("stream"):
            return "application/json", json.dumps(message).encode()
        empty = {**block, "input": {}} if block["type"] == "tool_use" else {"type": "text", "text": ""}
        delta = ({"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
                 if block["type"] == "tool_use" else {"type": "text_delta", "text": block["text"]})
        events = [("message_start", {"type": "message_start", "message": {**message, "content": [], "stop_reason": None}}),
                  ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": empty}),
                  ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": delta}),
                  ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                  ("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                                     "usage": {"output_tokens": 1}}),
                  ("message_stop", {"type": "message_stop"})]
        return "text/event-stream", b"".join(f"event: {k}\ndata: {json.dumps(v)}\n\n".encode() for k, v in events)
    served = []
    server, url, _ = _serve(answer)
    env = _offline(home, {"CLAUDE_CONFIG_DIR": str(home / ".claude"), "ANTHROPIC_BASE_URL": url,
                          "ANTHROPIC_API_KEY": "sk-ant-test", "DISABLE_TELEMETRY": "1", "DISABLE_AUTOUPDATER": "1",
                          "DISABLE_ERROR_REPORTING": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"})
    try:
        run = subprocess.run(["claude", "-p", "Answer the question.", "--output-format", "stream-json", "--verbose",
                              "--permission-mode", "bypassPermissions"],
                             cwd=work, env=env, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
    finally:
        server.shutdown()
    assert run.returncode == 0, run.stderr[-2000:]
    return run, requests


@CODEX_CLI
def test_with_the_codex_cli_every_channel_reaches_the_model(tmp_path):
    """`codex exec` as the framework's Codex agent runs it, on gpt-5.6-luna's code mode (one `exec` tool whose script
    calls the shell): the memory, an after_tool note, the queued before_model context, a deny and a blocked stop's
    reason (with the queue) reach the model; nothing leaves the machine."""
    work, copy = _deployed(tmp_path, "codex")
    run, requests = _run_codex(tmp_path, work, [{"cmd": "echo out; echo err >&2"}, {"cmd": "rm -rf build"}, "42",
                                                "42 again"])
    # Codex may hand a command's PostToolUse its last output chunk only (an empty one, at times): the note's
    # wording is checked, not the output it quotes.
    seen = _seen(requests, "Freeze the published state", "noted ", "model call 1", "list it first",
                 "check the units\\n\\nmodel call 2\\n\\nmodel call 3")
    assert all(i is not None for i in seen.values()), seen
    assert seen["Freeze the published state"] == 0 and seen["noted "] == seen["model call 1"] == 1
    lines = _audit(work)
    assert [l["event"] for l in lines] == ["SessionStart", "UserPromptSubmit", "PreModelRequest", "PreToolUse",
                                           "PostToolUse", "PreModelRequest", "PreToolUse", "PreModelRequest", "Stop",
                                           "PreModelRequest", "Stop"]
    assert all(l["error"] is None for l in lines) and [l["decision"] for l in lines if l["decision"]] == ["deny", "block"]
    assert [e[:2] for e in _state(work)["log"] if e[0] == "after_tool"] == [["after_tool", None]]   # no exit code


@CLAUDE_CLI
def test_with_the_claude_cli_every_channel_reaches_the_model(tmp_path):
    """`claude -p` as the framework's Claude agent runs it: the memory, after_tool's note on a call and on a failed one
    (after a `cd`), the queued before_model context, a deny and a blocked stop's reason reach the model; the logs stay
    in the project; nothing leaves the machine."""
    work, copy = _deployed(tmp_path, "claude-code")
    (work / "sub").mkdir()
    run, model = _run_claude(tmp_path, work, [{"bash": "echo out; echo err >&2"}, {"bash": "cd sub && echo in"},
                                              {"bash": "false"}, {"bash": "rm -rf build"}, "42", "42 again"])
    seen = _seen(model, "Freeze the published state", "noted out", "model call 1", "noted Exit code 1",
                 "model call 2", "list it first", "check the units", "model call 3")
    assert all(i is not None for i in seen.values()), seen
    lines = _audit(work)
    assert [(l["event"], l.get("at")) for l in lines if l["event"] == "PreModelRequest"] == [
        ("PreModelRequest", "UserPromptSubmit")] + [("PreModelRequest", "PostToolBatch")] * 4 + [
        ("PreModelRequest", "Stop")]
    assert all(l["error"] is None for l in lines) and not (work / "sub" / ".hermit-hooks").exists()
    after = [e for e in _state(work)["log"] if e[0] == "after_tool"]
    assert [(e[1], e[2], e[3].strip()) for e in after] == [(False, None, "out\nerr"), (False, None, "in"),
                                                           (True, 1, "Exit code 1")]
    assert [e[4] for e in after] == [str(work), str(work / "sub"), str(work / "sub")]   # the cwd follows the cd,
    assert {e[5] for e in after} == {str(work)}                                          # the workspace does not


# More of the table: allow with a context, a new input, an after_tool block with a system message, stop_session after a
# call that succeeded (the other cells rest on the probe's measurements and tests/fixtures/ahe_hosts/).
OUTPUTS = '''\
from hermit_hooks import HookResult

MEMORY = "MEMORY.md"


def session_start(inp, state):
    open(inp.adapter_dir + "/" + MEMORY).read()
    return None


def before_tool(inp, state):
    n = state["calls"] = state.get("calls", 0) + 1
    state.setdefault("commands", []).append(inp.command)
    if n == 1:
        return HookResult(decision="allow", context="ALLOW-CONTEXT-MARK")
    if n == 2:
        return HookResult(updated_input=dict(inp.tool_input or {}, command="echo REWRITTEN-$((2+3))"))
    return None


def after_tool(inp, state):
    m = state["after"] = state.get("after", 0) + 1
    state.setdefault("ran", []).append(inp.command)
    if m == 1:
        return HookResult(decision="block", reason="BLOCK-REASON-MARK", system_message="SYSTEM-MESSAGE-MARK")
    if m == 3:
        return HookResult(stop_session=True, reason="STOP-SESSION-MARK")
    return None


def stop(inp, state):
    state["stops"] = state.get("stops", 0) + 1
    return None


def before_model(inp, state):
    return None
'''
# The outputs are computed (FIRST-42, ORIGINAL-2, REWRITTEN-5, THIRD-6): the model's own call keeps the command's
# text in every later request, its output only where the host hands it over.
OUTPUT_TURNS = [{"cmd": "echo FIRST-$((40+2))"}, {"cmd": "echo ORIGINAL-$((1+1))"}, {"cmd": "echo THIRD-$((3+3))"},
                "done", "done"]
RAN = ["echo FIRST-$((40+2))", "echo REWRITTEN-$((2+3))", "echo THIRD-$((3+3))"]


@CODEX_CLI
def test_with_the_codex_cli_the_rest_of_the_table_holds(tmp_path):
    """On Codex 0.164 (code mode): a context given with `allow` is dropped; a block replaces the output with its reason
    (an error inside the script); a system message is shown nowhere; a new input runs instead; a stop_session at a
    tool result does not end the session."""
    work, copy = _deployed(tmp_path, "codex", OUTPUTS)
    run, requests = _run_codex(tmp_path, work, OUTPUT_TURNS)
    seen = _seen(requests, "ALLOW-CONTEXT-MARK", "BLOCK-REASON-MARK", "FIRST-42", "SYSTEM-MESSAGE-MARK",
                 "REWRITTEN-5", "ORIGINAL-2")
    assert seen["ALLOW-CONTEXT-MARK"] is None and seen["BLOCK-REASON-MARK"] == 1 and seen["FIRST-42"] is None
    assert seen["SYSTEM-MESSAGE-MARK"] is None and "SYSTEM-MESSAGE-MARK" not in run.stdout + run.stderr
    assert seen["REWRITTEN-5"] == 2 and seen["ORIGINAL-2"] is None
    state = _state(work)
    assert state["ran"] == RAN and state["stops"] == 1
    assert len(requests) == 4                                            # the session went on to its answer
    lines = _audit(work)
    assert [l["context_chars"] for l in lines if l["event"] == "PreToolUse"][0] == 0   # what Codex dropped: none


@CLAUDE_CLI
def test_with_the_claude_cli_the_rest_of_the_table_holds(tmp_path):
    """On Claude Code: a context given with `allow` arrives; a block keeps the output and adds its reason; a system
    message goes to the person watching, not the model; a new input runs instead; a stop_session at a tool result
    ends the session there."""
    work, copy = _deployed(tmp_path, "claude-code", OUTPUTS)
    run, model = _run_claude(tmp_path, work, [{"bash": t["cmd"]} if isinstance(t, dict) else t for t in OUTPUT_TURNS])
    seen = _seen(model, "ALLOW-CONTEXT-MARK", "BLOCK-REASON-MARK", "FIRST-42", "SYSTEM-MESSAGE-MARK",
                 "REWRITTEN-5", "ORIGINAL-2")
    assert seen["ALLOW-CONTEXT-MARK"] == seen["BLOCK-REASON-MARK"] == seen["FIRST-42"] == 1
    assert seen["SYSTEM-MESSAGE-MARK"] is None and "SYSTEM-MESSAGE-MARK" in run.stdout
    assert seen["REWRITTEN-5"] == 2 and seen["ORIGINAL-2"] is None
    state = _state(work)
    assert state["ran"] == RAN and "stops" not in state
    assert len(model) == 3                                               # no request after the third call: ended
