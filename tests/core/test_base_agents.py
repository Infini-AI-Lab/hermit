"""Layer 2's API (docs/framework/agent-layers.md): direct, react and the CLIs behind one `run`, limits enforced or
refused, tools served over MCP."""
import asyncio
import json
import os
from pathlib import Path

import pytest

from fakes import FunctionModel
from hermit.agent.base.interface import Limits, Mount, Unsupported, make
from hermit.agent.base.tool import Tool
from hermit.agent.base.tool_server import ToolServer


def add(a: int, b: int) -> dict:
    return {"sum": a + b}


ADD = Tool("add", "Add two integers.", add, input_schema={"type": "object", "required": ["a", "b"],
                                                          "properties": {"a": {"type": "integer"},
                                                                         "b": {"type": "integer"}}})


def test_direct_offers_tools_and_returns_the_calls_unrun():
    seen = []

    def reply(messages, **kwargs):
        seen.append(kwargs)
        return {"content": "", "finish_reason": "tool_calls"}
    agent = make("direct", {"max_tokens": 10}, FunctionModel(reply))
    result = agent.run("hi", tools=[ADD])
    assert seen[0]["tools"][0]["function"]["name"] == "add" and seen[0]["tool_choice"] == "auto"
    assert seen[0]["max_tokens"] == 10 and result.session[-1]["role"] == "assistant"
    other = agent.configured(max_tokens=99, temperature=None)
    other.run("again")
    assert seen[1]["max_tokens"] == 99 and agent.settings["max_tokens"] == 10      # the agent itself is unchanged
    with pytest.raises(Unsupported):
        agent.run("hi", workspace=Path("."))
    with pytest.raises(Unsupported):
        agent.run("hi", roles=[{"name": "r", "description": "d", "instructions": "i"}])


class Scripted:
    """A chat client that asks for one tool call, then answers with what the tool returned."""

    model = "m"

    def chat(self, messages, **kwargs):
        from hermit.agent.model.usage import Usage
        usage = Usage("m", output_tokens=1, calls=1)                       # this call's usage, as ChatClient's
        if messages[-1]["role"] != "tool":
            call = {"id": "c1", "type": "function", "function": {"name": "add", "arguments": '{"a": 2, "b": 3}'}}
            return {"role": "assistant", "content": None, "tool_calls": [call], "finish_reason": "tool_calls"}, usage
        return {"role": "assistant", "content": f"got {messages[-1]['content']}", "finish_reason": "stop"}, usage


def test_react_runs_the_tools_until_the_model_answers():
    result = make("react", {"max_turns": 4}, Scripted()).run("add 2 and 3", tools=[ADD])
    assert result.reply == 'got {"sum": 5}' and result.extra["turns"] == 2 and result.extra["tool_calls"] == 1
    assert [s.kind for s in result.trajectory] == ["tool_call", "message"]
    assert result.trajectory[0].output == '{"sum": 5}' and result.usage[0].calls == 2


def test_a_tool_error_is_text_the_model_reads():
    assert ADD.answer('{"a": "x", "b": 1}').startswith("Error: ValidationError")
    assert ADD.answer("not json").startswith("Error: JSONDecodeError")
    assert ADD.answer({"a": 1, "b": 2}) == '{"sum": 3}'


@pytest.mark.parametrize("backend", ["codex", "claude"])
def test_a_cli_refuses_limits_it_cannot_keep(tmp_path, backend):
    agent = make(backend, {"web_search": False}, None)
    for limits in (Limits(hidden=[tmp_path / "secret"]), Limits(network=[]), Limits(network=["example.com"])):
        with pytest.raises(Unsupported):
            agent.run("go", workspace=tmp_path, limits=limits)
    with pytest.raises(Unsupported):                                       # sandbox isolation needs the sandbox
        agent.configured(isolation="sandbox").run("go", workspace=tmp_path)
    with pytest.raises(Unsupported):
        agent.run("go")                                                    # a CLI works in a workspace
    with pytest.raises(ValueError, match="web_search"):
        make(backend, {}, None).run("go", workspace=tmp_path)


def test_a_container_hides_nothing_it_is_given(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    for backend in ("codex", "claude"):                  # Claude Code's container is held to the same as Codex's
        with pytest.raises(Unsupported, match="lies in what the container is given"):
            make(backend, {"web_search": False, "isolation": "container"}).run(
                "go", workspace=tmp_path / "work", limits=Limits(readable=[Mount(data, Path("docs"))],
                                                                 hidden=[data / "answers"]))
    home = tmp_path / "home"                                               # the session's home is bound in too
    with pytest.raises(Unsupported, match="lies in what the container is given"):
        make("codex", {"web_search": False, "isolation": "container", "home": home}).run(
            "go", workspace=tmp_path / "work", limits=Limits(hidden=[home / "secret"]))


def test_the_tool_server_serves_mcp_and_json_behind_its_token():
    import urllib.error
    import urllib.request
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async def over_mcp(url):
        async with streamable_http_client(url) as (read, write, *_):
            async with ClientSession(read, write) as session:
                await session.initialize()
                names = [t.name for t in (await session.list_tools()).tools]
                good = await session.call_tool("add", {"a": 1, "b": 2})
                bad = await session.call_tool("add", {"a": "x", "b": 2})
                return names, (good.content[0].text, good.structured_content), bad.is_error

    with ToolServer([ADD]) as server:
        assert asyncio.run(over_mcp(server.mcp_url)) == (["add"], ('{"sum": 3}', None), True)
        tools = json.loads(urllib.request.urlopen(server.url + "/tools").read())
        assert [t["name"] for t in tools] == ["add"]
        request = urllib.request.Request(server.url + "/call/add", data=b'{"a": 4, "b": 5}')
        assert json.loads(urllib.request.urlopen(request).read()) == {"result": {"sum": 9}}
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(server.url.rsplit("/", 1)[0] + "/wrong-token/tools")


def test_direct_and_react_refuse_a_time_limit_they_cannot_keep():
    for name in ("direct", "react"):
        with pytest.raises(Unsupported, match="time limit"):
            make(name, {}, FunctionModel(lambda **_: {"content": "x"})).run("hi", limits=Limits(timeout_s=1))


def test_a_sandboxed_cli_refuses_what_its_sandbox_cannot_keep(tmp_path):
    shown = tmp_path / "shown"
    shown.mkdir()

    class Box:
        enabled, network, allow = True, "isolated", ["example.com"]
        readable, writable, binds = [shown], [], []

        def wrap(self, command, **kw):
            return command, kw["env"]
    agent = make("codex", {"web_search": False, "isolation": "sandbox", "sandbox": Box()})
    with pytest.raises(Unsupported, match="allowed hosts"):
        agent.run("go", workspace=tmp_path, limits=Limits(network=[]))
    with pytest.raises(Unsupported, match="lies in what the sandbox shows"):    # an allowlist shows what it grants
        agent.run("go", workspace=tmp_path, limits=Limits(hidden=[shown / "secret"]))
    home = tmp_path / "home"
    agent = make("codex", {"web_search": False, "isolation": "sandbox", "sandbox": Box(), "home": str(home)})
    for hidden in (home / "private", tmp_path / "work" / "private", Path("/usr/bin/true")):   # its own, the system
        with pytest.raises(Unsupported, match="lies in what the sandbox shows"):
            agent.run("go", workspace=tmp_path / "work", limits=Limits(hidden=[hidden]))
    from hermit.sandbox import PACKAGE, Sandbox
    framework = make("codex", {"web_search": False, "isolation": "sandbox", "sandbox": Sandbox(framework=True)})
    with pytest.raises(Unsupported, match="lies in what the sandbox shows"):    # the framework it shows
        framework.run("go", workspace=tmp_path / "work", limits=Limits(hidden=[PACKAGE / "config.py"]))
    install = tmp_path / "opt" / "private-cli"
    (install / "bin").mkdir(parents=True)
    (install / "bin" / "codex").write_text("#!/bin/sh\n")
    (install / "bin" / "codex").chmod(0o755)
    cli = make("codex", {"web_search": False, "isolation": "sandbox", "sandbox": Sandbox(framework=True),
                         "bin": str(install / "bin" / "codex")})
    with pytest.raises(Unsupported, match="lies in what the sandbox shows"):    # the CLI's own installation
        cli.run("go", workspace=tmp_path / "work", limits=Limits(hidden=[install / "secret.txt"]))


def test_an_interrupted_caller_stops_the_session(tmp_path):
    import os
    import threading
    import time
    from hermit.agent.base.cli import run_process
    pid_file = tmp_path / "pid"
    script = f"import os, time; open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(60)"

    def interrupt():
        while not pid_file.exists():
            time.sleep(0.05)
        import _thread
        _thread.interrupt_main()
    threading.Thread(target=interrupt, daemon=True).start()
    import sys
    with pytest.raises(KeyboardInterrupt):
        run_process([sys.executable, "-c", script], env=dict(os.environ), cwd=tmp_path, stream=tmp_path / "out",
                    timeout=None)
    pid = int(pid_file.read_text())
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_a_resumed_codex_thread_reports_only_its_new_usage(tmp_path):
    """Codex reports a thread's running total; a resumed call's usage is the difference."""
    from hermit.agent.base.cli import _this_call
    from hermit.agent.model.usage import Usage
    first = _this_call(tmp_path, "t1", Usage("m", 300, 200, 30, 1), resumed=False)
    second = _this_call(tmp_path, "t1", Usage("m", 800, 700, 55, 1), resumed=True)
    assert (first.input_tokens, first.output_tokens) == (300, 30)
    assert (second.input_tokens, second.cached_input_tokens, second.output_tokens) == (500, 500, 25)
    fresh = _this_call(tmp_path, "t1", Usage("m", 90, 0, 9, 1), resumed=False)     # a new thread starts at zero
    assert fresh.input_tokens == 90


def test_a_logs_lines_that_are_json_but_not_objects_are_not_events(tmp_path):
    """parse_log reads the session handle and usage past a CLI's own lines that parse as JSON but are no event."""
    from hermit.agent.base.records import parse_log
    noise = ['"UID014"', "42", "[1, 2]", "null", "true", "Reading additional input from stdin..."]
    codex = tmp_path / "agent-codex.jsonl"
    codex.write_text("\n".join([noise[0], json.dumps({"type": "thread.started", "thread_id": "t1"}), *noise[1:],
                                json.dumps({"type": "turn.completed",
                                            "usage": {"input_tokens": 10, "cached_input_tokens": 4, "output_tokens": 3}})]))
    handle, [usage] = parse_log(codex, "codex", "m")
    assert handle == "t1" and (usage.input_tokens, usage.cached_input_tokens, usage.output_tokens, usage.calls) == (10, 4, 3, 1)
    claude = tmp_path / "agent-claude.jsonl"
    claude.write_text("\n".join([*noise, json.dumps({"type": "result", "session_id": "s1", "num_turns": 2,
                                                     "usage": {"input_tokens": 5, "output_tokens": 2}})]))
    handle, [usage] = parse_log(claude, "claude", "m")
    assert handle == "s1" and (usage.input_tokens, usage.output_tokens, usage.calls) == (5, 2, 2)


def test_a_logs_event_whose_text_holds_a_unicode_line_separator_still_gives_its_session_and_usage(tmp_path):
    """A CLI's JSON writer leaves U+0085, U+2028 and U+2029 unescaped in an event's strings (Claude Code's and Pi's
    JSON.stringify, mini-swe-agent's json.dumps(ensure_ascii=False)), where str.splitlines() ends a line: parse_log
    reads such an event whole, so a Claude Code result, a Pi message or a mini-swe-agent response whose text holds one
    still gives the session and its usage."""
    from hermit.agent.base.records import parse_log
    from hermit.agent.model.usage import Usage
    for sep in ("\x85", "\u2028", "\u2029"):
        streams = {"claude": [{"type": "result", "subtype": "success", "session_id": "s1", "num_turns": 1,
                               "result": f"42{sep}done", "usage": {"input_tokens": 5, "output_tokens": 2}}],
                   "pi": [{"type": "session", "id": "s1"},
                          {"type": "message_end", "message": {"role": "assistant", "stopReason": "stop",
                                                              "content": [{"type": "text", "text": f"42{sep}done"}],
                                                              "usage": {"input": 5, "output": 2}}}],
                   "mini-swe-agent": [{"type": "session.started", "session": "s1"},
                                      {"type": "message", "message": {"role": "assistant", "content": f"42{sep}done"},
                                       "usage": {"input_tokens": 5, "output_tokens": 2}}]}
        for backend, stream in streams.items():
            log = tmp_path / f"agent-{backend}.jsonl"
            log.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in stream), encoding="utf-8")
            assert sep in log.read_text(encoding="utf-8")                      # unescaped, as the CLI writes it
            assert parse_log(log, backend, "m") == ("s1", [Usage("m", input_tokens=5, output_tokens=2, calls=1)]), \
                (backend, repr(sep))


def test_a_logs_lines_nested_past_the_parsers_depth_are_not_events(tmp_path):
    """parse_log reads the session handle and usage past lines nested past the JSON parser's depth, as past lines that
    do not parse: never an error that fails the session's or the reader's call."""
    from hermit.agent.base.records import parse_log
    nested = "[" * 100_000 + "]" * 100_000
    codex = tmp_path / "agent-codex.jsonl"
    done = {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 3}}
    codex.write_text("\n".join([json.dumps({"type": "thread.started", "thread_id": "t1"}), nested,
                                '{"type": "turn.completed", "usage": ' + nested + "}", json.dumps(done)]))
    handle, [usage] = parse_log(codex, "codex", "m")
    assert handle == "t1" and (usage.input_tokens, usage.output_tokens, usage.calls) == (10, 3, 1)


def test_codex_sub_agent_calls_are_steps():
    from hermit.agent.base.trajectory import codex_steps
    spawn = {"type": "item.completed", "item": {"type": "collab_tool_call", "tool": "spawn_agent",
                                                "receiver_thread_ids": ["t2"], "prompt": "verify 507",
                                                "agents_states": {"t2": {"status": "pending"}}, "status": "completed"}}
    [step] = codex_steps([spawn])
    assert step.kind == "tool_call" and json.loads(step.text)["tool"] == "spawn_agent"
    assert json.loads(step.text)["prompt"] == "verify 507" and "pending" in step.output


def test_codex_failed_turns_searches_to_do_lists_and_tool_results_are_steps():
    from hermit.agent.base.trajectory import codex_steps, text
    steps = codex_steps([
        {"type": "item.completed", "item": {"type": "mcp_tool_call", "server": "evolver_tools", "tool": "check_spec",
                                            "arguments": {"path": "adapter"}, "result": {"passed": False},
                                            "error": None, "status": "completed"}},
        {"type": "item.completed", "item": {"type": "web_search", "query": "treasury bulletin 1944"}},
        {"type": "item.completed", "item": {"type": "todo_list", "items": [{"text": "find the table",
                                                                            "completed": True}]}},
        {"type": "turn.failed", "error": {"message": "stream disconnected before completion"}},
    ])
    call, search, todo, failed = steps
    assert json.loads(call.text)["arguments"] == {"path": "adapter"} and json.loads(call.output) == {"passed": False}
    assert json.loads(search.text) == {"tool": "web_search", "query": "treasury bulletin 1944"}
    assert json.loads(todo.text)["items"][0]["text"] == "find the table"
    assert failed.kind == "error" and "stream disconnected" in failed.text
    assert "[error] stream disconnected" in text(steps)
    twice = codex_steps([{"type": "error", "message": "quota"}, {"type": "turn.failed", "error": {"message": "quota"}}])
    assert [s.text for s in twice] == ["quota"]                      # Codex reports a failed turn's error twice
    long_call = codex_steps([{"type": "item.completed", "item": {
        "type": "mcp_tool_call", "server": "evolver_tools", "tool": "check_spec", "status": "completed",
        "arguments": {"path": "x" * 2000}}}])
    assert '"tool": "check_spec"' in text(long_call)                  # a long argument does not push the tool out


def test_codex_reports_a_refused_sign_in_from_its_own_notices_only():
    """Codex's 401 for a lost sign-in or a key the provider does not take is `login_failed` (no wait restores it: a
    session on it stops the run) when the CLI itself reported it and the turn did not complete; a command that only
    printed those words is the session's own work."""
    from hermit.agent.base.cli import Codex
    codex = Codex({})
    refused = ('{"type":"thread.started","thread_id":"t"}\n{"type":"turn.started"}\n'
               '{"type":"error","message":"unexpected status 401 Unauthorized: Your authentication token has been '
               'invalidated. Please try signing in again."}\n'
               '{"type":"turn.failed","error":{"message":"unexpected status 401 Unauthorized"}}\n')
    said = codex.outcome(refused, None, None)
    assert said["login_failed"] is True and not said["served"] and not said["unavailable"]
    assert codex.outcome('{"type":"error","message":"Incorrect API key provided: sk-...xyz"}\n', None,
                         None)["login_failed"] is True
    output = "HTTP/1.1 401 Unauthorized\nunexpected status 401 Unauthorized\nInvalid API key"     # the command's own
    printed = ('{"type":"thread.started","thread_id":"t"}\n{"type":"turn.started"}\n'
               + json.dumps({"type": "item.completed", "item": {
                   "id": "i1", "type": "command_execution", "command": "curl -i https://api.example/v1",
                   "aggregated_output": output, "exit_code": 0, "status": "completed"}}) + "\n")
    assert codex.outcome(printed, None, None)["login_failed"] is False          # cut off, its own output alone
    done = printed + '{"type":"turn.completed","usage":{"input_tokens":1,"cached_input_tokens":0,"output_tokens":1}}\n'
    assert codex.outcome(done, None, None)["login_failed"] is False


def test_codex_loses_its_sign_in_to_a_403_only_when_the_turn_fails_with_it_and_to_its_own_wordings_as_written():
    """A 403 is a lost sign-in when the turn failed with it, never for Codex's reconnect notices of one (recovered
    from, as Harbor's pattern takes it); a proxy's refused token is one too; the wordings are matched as Codex writes
    them, so another program's lowercase "not logged in" on Codex's stderr is not."""
    from hermit.agent.base.cli import Codex
    codex = Codex({})
    start = '{"type":"thread.started","thread_id":"t"}\n{"type":"turn.started"}\n'
    reconnect = ''.join(f'{{"type":"error","message":"Reconnecting... {n}/5 (unexpected status 403 Forbidden: '
                        f'blocked)"}}\n' for n in (1, 2))
    failed = start + reconnect + '{"type":"turn.failed","error":{"message":"unexpected status 403 Forbidden"}}\n'
    assert codex.outcome(failed, None, None)["login_failed"] is True
    timed_out = start + reconnect + '{"type":"item.completed","item":{"id":"i1","type":"agent_message",' \
                                    '"text":"working"}}\n'                  # recovered, then out of the agent's time
    assert codex.outcome(timed_out, None, None)["login_failed"] is False
    proxy = start + '{"type":"error","message":"Invalid proxy server token passed"}\n'
    assert codex.outcome(proxy, None, None)["login_failed"] is True
    stderr = start + "gh: not logged in to any GitHub hosts\n"                # another program's line, not an event
    assert codex.outcome(stderr, None, None)["login_failed"] is False


def test_codex_never_loses_its_sign_in_to_a_401_it_reconnected_from_only_to_one_that_lasted():
    """Codex retries a 401 it meets, as it does a 403 (its reconnect notice, "Reconnecting... 1/5 (unexpected status
    401 Unauthorized: ...)"): a session that recovered from one and then ran out of its time has not lost its sign-in
    (which would halt the run). A 401 that lasts fails the turn with it, or is the session's last error: a lost
    sign-in, as it was."""
    from hermit.agent.base.cli import Codex
    codex = Codex({})
    start = '{"type":"thread.started","thread_id":"t"}\n{"type":"turn.started"}\n'
    unauthorized = "unexpected status 401 Unauthorized: Missing bearer or basic authentication in header"
    reconnect = f'{{"type":"error","message":"Reconnecting... 1/5 ({unauthorized})"}}\n'
    working = '{"type":"item.completed","item":{"id":"i1","type":"agent_message","text":"working"}}\n'
    assert codex.outcome(start + reconnect + working, None, None)["login_failed"] is False
    failed = start + reconnect + f'{{"type":"turn.failed","error":{{"message":"{unauthorized}"}}}}\n'
    assert codex.outcome(failed, None, None)["login_failed"] is True
    last = start + reconnect + f'{{"type":"error","message":"{unauthorized}"}}\n'
    assert codex.outcome(last, None, None)["login_failed"] is True


def test_codex_never_loses_its_sign_in_to_a_401_it_fell_back_to_https_from():
    """Codex's fall back from a WebSocket the provider refused to HTTPS (an `error` item: "Falling back from WebSockets
    to HTTPS transport. unexpected status 401 ...") is a notice it goes on from, as a reconnect is: a session that then
    worked and ran out of its time has not lost its sign-in. A 401 its turn then failed with has."""
    from hermit.agent.base.cli import Codex
    codex = Codex({})
    start = '{"type":"thread.started","thread_id":"t"}\n{"type":"turn.started"}\n'
    fallback = json.dumps({"type": "item.completed", "item": {
        "id": "item_0", "type": "error", "message": "Falling back from WebSockets to HTTPS transport. unexpected "
                                                    "status 401 Unauthorized: Missing bearer or basic authentication "
                                                    "in header, url: wss://chatgpt.com/backend-api/codex/responses"}})
    working = '{"type":"item.completed","item":{"id":"i1","type":"agent_message","text":"working"}}\n'
    assert codex.outcome(start + fallback + "\n" + working, None, None)["login_failed"] is False
    lasted = '{"type":"turn.failed","error":{"message":"unexpected status 401 Unauthorized"}}\n'
    assert codex.outcome(start + fallback + "\n" + lasted, None, None)["login_failed"] is True


def test_codex_reads_a_provider_that_could_not_serve_from_its_last_failure_never_from_a_notice_it_recovered_from():
    """`unavailable` is read, as Claude Code's, Pi's and OpenCode's readers read it, from the session's last failure:
    the error its turn failed with, else its last error or line (its stderr), never a notice it recovered from (a
    reconnect, a fall back to HTTPS). A session that reconnected after a dropped stream, worked on and failed for its
    own reason (the model's context window) met no provider that could not serve; one whose turn failed on the
    dropped stream did, and so did one whose last line is the provider's 429."""
    from hermit.agent.base.cli import Codex
    codex = Codex({})
    start = '{"type":"thread.started","thread_id":"t"}\n{"type":"turn.started"}\n'
    reconnect = '{"type":"error","message":"Reconnecting... 1/5 (stream disconnected before completion: error ' \
                'sending request for url (https://models.example/v1/responses))"}\n'
    working = '{"type":"item.completed","item":{"id":"i1","type":"agent_message","text":"working"}}\n'
    full = "Codex ran out of room in the model's context window. Start a new thread or clear earlier history."
    own = f'{{"type":"error","message":"{full}"}}\n{{"type":"turn.failed","error":{{"message":"{full}"}}}}\n'
    assert codex.outcome(start + reconnect + working + own, None, None)["unavailable"] is False
    assert codex.outcome(start + reconnect + working, None, None)["unavailable"] is False    # then out of its time
    dropped = '{"type":"turn.failed","error":{"message":"stream disconnected before completion: error sending ' \
              'request"}}\n'
    assert codex.outcome(start + reconnect + dropped, None, None)["unavailable"] is True
    refresh = "2026-11-03T02:33:37Z ERROR codex_models_manager::manager: failed to refresh available models: " \
              "exceeded retry limit, last status: 429 Too Many Requests\n"           # a line beside its events
    assert codex.outcome(start + refresh + working + own, None, None)["unavailable"] is False
    assert codex.outcome(start + refresh, None, None)["unavailable"] is True
    assert codex.outcome("ERROR: unexpected status 429 Too Many Requests\n", None, None)["unavailable"] is True


def test_a_codex_container_docker_could_not_start_is_unavailable_though_dockers_help_line_ends_the_stream():
    """Hermit's own container isolation ({cli: codex, isolation: container}) merges `docker run`'s stderr into the
    session's stream, and docker ends a failed run's error with its help line (newer docker after a blank line). A
    stream with no event (no session began) is read in every line, not by its last: the daemon's error, or a daemon
    docker could not reach, is a container that could not start (`unavailable`: endpoint._no_reply's 503, not a 502).
    A stream with events keeps its last failure (the last-failure test above)."""
    from hermit.agent.base.cli import Codex
    codex = Codex({})
    refused = 'docker: Error response from daemon: Conflict. The container name "/hermit-x-abc" is already in use by ' \
              'container "f00". You have to remove (or rename) that container to be able to reuse that name.'
    unreached = "docker: Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon " \
                "running?"
    hints = ("\n\nRun 'docker run --help' for more information\n",          # newer docker (29.7.1 here)
             "\nSee 'docker run --help'.\n")                               # older docker
    for failed in (refused, unreached):
        for hint in hints:
            assert codex.outcome(failed + hint, None, None)["unavailable"] is True, hint
    for hint in hints:
        assert codex.outcome(hint.lstrip("\n"), None, None)["unavailable"] is False     # a help line alone says none


def test_what_codexs_log_quotes_of_the_sessions_tool_calls_is_the_sessions_work_never_a_lost_sign_in_limit_or_outage():
    """Codex logs a tool call that failed on its stderr, which the stream holds: a command it refused, with the
    command's text, and a patch whose lines it did not find, with those lines after the record, one per line. That is
    the model's own text: a session cut off (no completed turn) whose log quoted the words of a lost sign-in, of the
    plan's limit or of an outage met none of them. Codex's other records stay its notices, a record that starts after
    the quoted lines among them."""
    from hermit.agent.base.cli import Codex
    codex = Codex({})
    start = '{"type":"thread.started","thread_id":"t"}\n{"type":"turn.started"}\n' \
            '{"type":"item.completed","item":{"id":"i1","type":"agent_message","text":"editing the handler"}}\n'
    rejected = ('2026-11-12T01:02:03.456789Z ERROR codex_core::tools::router: error=exec_command failed: CreateProcess '
                '{ message: "Rejected(\\"`/bin/bash -lc \\\\\\"echo Not logged in; echo usage_limit_reached; echo 503 '
                'Service Unavailable; echo Invalid API key\\\\\\"` rejected: blocked by policy\\")" }\n')
    patched = ("2026-11-12T01:02:04.5Z ERROR codex_core::tools::router: error=apply_patch verification failed: Failed "
               "to find expected lines in /app/server/auth.py:\n"
               '        return jsonify(error="Invalid API key"), 401\n'
               "\n"
               '    raise AuthError("Not logged in")\n'
               "# usage_limit_reached: the plan's limit, as the client reports it\n"
               "HTTP/1.1 503 Service Unavailable\n")
    for quoted in (rejected, patched, rejected + patched):
        said = codex.outcome(start + quoted, None, None)
        assert not said["served"], quoted
        assert (said["login_failed"], said["refused"], said["unavailable"]) == (False, False, False), quoted
        assert "Invalid API key" not in codex.notices(start + quoted), quoted
    refresh = "2026-11-03T02:33:37Z ERROR codex_models_manager::manager: failed to refresh available models: " \
              "exceeded retry limit, last status: 429 Too Many Requests\n"           # Codex's own request that failed
    said = codex.outcome(start + patched + refresh, None, None)
    assert said["unavailable"] is True and said["login_failed"] is False and "429 Too Many" in said["failure"]
    assert codex.outcome(start + "Not logged in\n", None, None)["login_failed"] is True   # a line that follows none


def test_a_quoted_line_shaped_like_a_log_record_is_the_quoted_text_only_a_rust_path_target_starts_codexs_records():
    """A patch whose lines Codex did not find may be of a log in the format of Codex's own (a fixture's "<time> ERROR
    auth: Invalid API key"), each quoted line on a line of its own after the tools record. Codex's records name a Rust
    path as their target (codex_core::tools::router, rmcp::transport::worker): a quoted line whose target is a plain
    word starts none, it is the text the record quoted, so a session cut off whose patch quoted the words of a lost
    sign-in, of an outage or of the plan's limit met none of them. Codex's own record after the quote is its notice."""
    from hermit.agent.base.cli import Codex
    codex = Codex({})
    start = '{"type":"thread.started","thread_id":"t"}\n{"type":"turn.started"}\n' \
            '{"type":"item.completed","item":{"id":"i1","type":"agent_message","text":"fixing the log fixture"}}\n'
    patched = ("2026-11-12T01:02:04.5Z ERROR codex_core::tools::router: error=apply_patch verification failed: Failed "
               "to find expected lines in /app/tests/fixtures/server.log:\n"
               "2024-03-01T12:00:00Z INFO server: listening on :8080\n"
               "2024-03-01T12:00:05Z ERROR auth: Invalid API key\n"
               "2024-03-01T12:00:06Z WARN upstream: unexpected status 503 Service Unavailable\n")
    billing = "2024-03-01T12:00:07Z ERROR billing{org=o1}: usage_limit_reached\n"
    for quoted in (patched, patched + billing):                         # the 503 line last, or the limit's after it
        said = codex.outcome(start + quoted, None, None)
        assert not said["served"], quoted
        assert (said["login_failed"], said["unavailable"], said["refused"]) == (False, False, False), quoted
        assert codex.notices(start + quoted) == "", quoted
    refresh = "2026-11-03T02:33:37Z ERROR codex_models_manager::manager: failed to refresh available models: " \
              "exceeded retry limit, last status: 429 Too Many Requests\n"           # Codex's own request that failed
    said = codex.outcome(start + patched + billing + refresh, None, None)
    assert said["unavailable"] is True and said["login_failed"] is False and said["failure"] == refresh.strip()


def test_codexs_record_of_a_tool_call_inside_its_spans_quotes_the_sessions_work_never_a_lost_sign_in_limit_or_outage():
    """Where the level of Codex's spans is on (RUST_LOG=codex_core=info, Codex 0.164.0), a record names the spans it
    was logged in before its target ("session_loop{...}:submission_dispatch{...}:turn{...}: codex_core::tools::router:
    ..."). The target is the Rust path after them: the record of a patch whose lines Codex did not find is the
    session's work and so are the lines it quotes, so a session cut off whose patch quoted the words of a lost sign-in,
    of the plan's limit or of an outage met none of them. Codex's own record after the quote, inside spans whose names
    hold dots or that have no fields, is its notice."""
    from hermit.agent.base.cli import Codex
    codex = Codex({})
    start = '{"type":"thread.started","thread_id":"t"}\n{"type":"turn.started"}\n' \
            '{"type":"item.completed","item":{"id":"i1","type":"agent_message","text":"I will patch auth.py."}}\n'
    turn = ('session_loop{thread_id=t}:submission_dispatch{otel.name="op.dispatch.turn_input" submission.id="s" '
            'codex.op="turn_input"}:turn{otel.name="session_task.turn" thread.id=t turn.id=s model=gpt-5}')
    patched = (f"2026-11-12T15:15:48.093896Z ERROR {turn}: codex_core::tools::router: error=apply_patch verification "
               "failed: Failed to find expected lines in /app/server/auth.py:\n"
               '    raise AuthError("Not logged in")\n')
    for quoted in (patched, patched + '    if code == "usage_limit_reached":\n',
                   patched + '    return "503 Service Unavailable"\n'):
        said = codex.outcome(start + quoted, None, None)
        assert not said["served"], quoted
        assert (said["login_failed"], said["refused"], said["unavailable"]) == (False, False, False), quoted
        assert codex.notices(start + quoted) == "", quoted
    own = ('2026-11-12T15:15:49.409742Z  WARN app_server.request{otel.name="turn/start" rpc.method="turn/start"}:'
           'thread_spawn{otel.name="thread_spawn"}:session_init:get_model_info{model="gpt-5"}: '
           "codex_models_manager::model_info: Unknown model gpt-5 is used. This will use fallback model metadata.\n")
    retry = (f"2026-11-12T15:15:53.501662Z  WARN {turn}: codex_core::responses_retry: stream connection failed; "
             "waiting to retry turn_id=s error=Connection failed: error sending request retry_delay=5s\n")
    assert codex.notices(start + patched + own + retry) == (own + retry).strip()
    said = codex.outcome(start + patched + own + retry, None, None)
    assert said["unavailable"] is True and said["login_failed"] is False


def test_an_event_whose_text_holds_a_unicode_line_separator_is_one_event_of_the_sessions_work():
    """A CLI's JSON writer leaves U+0085, U+2028 and U+2029 unescaped in an event's strings (Codex's serde_json, Claude
    Code's JSON.stringify), where str.splitlines() ends a line: a message or a command's output that holds one is still
    one event, the session's own work. So a session cut off (no completed turn) whose command printed the words of a
    lost sign-in, of the plan's limit or of an outage met none of them, and none of its words is the CLI's notice."""
    from hermit.agent.base.cli import Claude, Codex
    codex, claude = Codex({}), Claude({"login": "claude"})
    start = '{"type":"thread.started","thread_id":"t"}\n{"type":"turn.started"}\n'
    for sep in ("\x85", "\u2028", "\u2029"):
        message = {"type": "item.completed", "item": {"id": "i1", "type": "agent_message",
                                                      "text": f"The server answers{sep}Invalid API key"}}
        command = {"type": "item.completed", "item": {
            "id": "i2", "type": "command_execution", "command": "curl -s https://api.example/v1",
            "aggregated_output": f"Not logged in{sep}usage_limit_reached{sep}503 Service Unavailable",
            "exit_code": 0, "status": "completed"}}
        stream = start + "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in (message, command))
        assert sep in stream                                                   # unescaped, as Codex writes it
        said = codex.outcome(stream, None, None)
        assert not said["served"]
        assert (said["login_failed"], said["refused"], said["unavailable"]) == (False, False, False), repr(sep)
        assert codex.notices(stream) == "" and [event for event, _ in codex.lines(stream)] == [
            {"type": "thread.started", "thread_id": "t"}, {"type": "turn.started"}, message, command]
        working = [{"type": "system", "subtype": "init", "session_id": "s", "apiKeySource": "none"},
                   {"type": "assistant", "message": {"role": "assistant", "content": [
                       {"type": "text", "text": f"The server answers{sep}Not logged in"}]}}]     # then cut off
        stream = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in working)
        said = claude.outcome(stream, None, None)
        assert not said["served"] and said["login_failed"] is False and claude.notices(stream) == "", repr(sep)


def test_a_link_in_place_of_the_last_message_file_is_never_followed_one_left_there_or_put_there_after_a_check(
        tmp_path, monkeypatch):
    """Codex's `-o` file and mini-swe-agent's last-message file lie where the session's processes may still write (a
    Harbor stream's environment outlives its step): a link in the file's place, left there or put there right after
    the reader looked at the file by its name (stat, lstat), is never followed, so a file of this machine is never the
    reply (the stream's last message is, or the file's own text)."""
    from hermit.agent.base.cli import Codex, MiniSweAgent
    host = tmp_path / "host.txt"
    host.write_text("a file of this machine\n")
    streams = {Codex: '{"type":"thread.started","thread_id":"t"}\n'
                      '{"type":"item.completed","item":{"id":"i1","type":"agent_message","text":"from the stream"}}\n',
               MiniSweAgent: '{"type":"message","message":{"role":"assistant","content":"from the stream"}}\n'}
    looks = {name: getattr(os, name) for name in ("stat", "lstat")}

    def swapped_after_a_look(path: Path) -> None:
        """A link to `host` in `path`'s place right after the first stat or lstat of it (a check before it is opened
        by its name), as a process of the environment may put it there."""
        done = []

        def look(name):
            def call(p, *args, **kwargs):
                seen = looks[name](p, *args, **kwargs)
                if not done and isinstance(p, (str, os.PathLike)) and os.fspath(p) == str(path):
                    done.append(name)
                    os.unlink(path)
                    os.symlink(host, path)
                return seen
            return call
        for name in looks:
            monkeypatch.setattr(os, name, look(name))
    for reader, stream in streams.items():
        last = tmp_path / reader.name / "last_message.txt"
        last.parent.mkdir()
        last.symlink_to(host)                                              # left there
        assert reader.last_message(None, last, raw=stream) == "from the stream", reader.name
        last.unlink()
        last.write_text("the session's own\n")
        swapped_after_a_look(last)                                         # put there after a check
        reply = reader.last_message(None, last, raw=stream)
        monkeypatch.undo()
        assert reply in ("the session's own\n", "from the stream"), (reader.name, reply)


def test_dshs_failure_past_the_context_window_keeps_its_whole_error_line_though_it_holds_a_line_separator():
    """dsh's failure for a model past its server's context window is dsh's error line, then what to declare: the
    line read to its "\\n", though the provider's message in it holds a U+2028 (which dsh, on Node, leaves unescaped
    in its events too)."""
    from hermit.agent.base.cli import CLIS
    recorded = Path(__file__).resolve().parents[1] / "fixtures" / "dsh" / "context_window.jsonl"
    raw = recorded.read_text().replace(" Please reduce the length", "\u2028Please reduce the length")
    failure = CLIS["dsh"]({"binding": {"ref": "local/m"}}).outcome(raw, None, None)["failure"]
    assert failure.startswith("dsh: CONTEXT_WINDOW_EXCEEDED: 400: ") and "\u2028Please reduce the length" in failure
    assert failure.endswith("declare the backend's context_window")


def test_opencodes_failure_names_the_http_status_its_error_gives_after_the_errors_own_words():
    """OpenCode's error event gives the provider's status beside its type and message, which may not name it: the
    failure ends with it ("... (HTTP 401)"), so a reader of the failure's words (a Harbor trial's verdict) finds a 401
    or a 402 whose message says nothing of a key or a bill; an error with no status (no request answered) has none."""
    from hermit.agent.base.cli import OpenCode
    streams = Path(__file__).resolve().parents[1] / "fixtures" / "opencode"
    said = OpenCode({}).outcome((streams / "failure_401.jsonl").read_text(), None, None)
    assert said["rejected"] and said["failure"] == "provider.auth: the model is down (HTTP 401)"
    bland = '{"type":"error","error":{"type":"provider.unknown","message":"the model is down","status":403}}\n'
    assert OpenCode({}).outcome(bland, None, None)["failure"] == "provider.unknown: the model is down (HTTP 403)"
    unreached = OpenCode({}).outcome((streams / "failure_refused.jsonl").read_text(), None, None)
    assert unreached["unavailable"] and unreached["failure"].startswith("provider.transport: ECONNREFUSED") and \
        "(HTTP" not in unreached["failure"]
    assert OpenCode({}).outcome((streams / "success.jsonl").read_text(), None, None)["failure"] == ""


def test_a_logs_stream_under_another_name_is_found(tmp_path):
    """A Harbor trial keeps Codex's events as agent/codex.txt; a file named otherwise is read by its events."""
    from hermit.agent.base.trajectory import log_steps
    trial = tmp_path / "trial"
    (trial / "agent").mkdir(parents=True)
    events = [{"type": "thread.started", "thread_id": "t"},
              {"type": "item.completed", "item": {"type": "agent_message", "text": "<answer>7</answer>"}}]
    (trial / "agent" / "codex.txt").write_text("".join(json.dumps(e) + "\n" for e in events))
    assert [s.text for s in log_steps(trial)] == ["<answer>7</answer>"]
    assert [s.text for s in log_steps(trial / "agent" / "codex.txt")] == ["<answer>7</answer>"]
    (tmp_path / "notes.txt").write_text("no events here\n")
    assert log_steps(tmp_path / "notes.txt") is None and log_steps(tmp_path / "missing") is None
    (trial / "agent" / "codex.txt").write_text(json.dumps({"type": "error", "message": "stream disconnected"}) + "\n")
    assert [(s.kind, s.text) for s in log_steps(trial)] == [("error", "stream disconnected")]   # by its name


def test_the_chat_client_waits_as_told_between_retries(monkeypatch):
    import urllib.error
    from hermit.agent.model import llm
    waits = []
    monkeypatch.setattr(llm.time, "sleep", waits.append)

    def unavailable(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 503, "busy", {}, None)
    import types
    monkeypatch.setattr(llm.urllib.request, "build_opener", lambda *a: types.SimpleNamespace(open=unavailable))
    client = llm.ChatClient("http://x/v1", "m", max_retries=4, retry_wait=4.0, retry_wait_max=60.0)
    with pytest.raises(RuntimeError, match="HTTP 503"):
        client.chat([{"role": "user", "content": "hi"}])
    assert waits == [4.0, 8.0, 16.0, 32.0]                 # SkillOpt's own client: 4 * 2**attempt, at most 60


def test_a_client_of_an_endpoint_over_a_cli_counts_what_each_failed_session_used(monkeypatch):
    """Hermit's endpoint over a CLI answers a session that failed with an error whose body says what the session used
    (endpoint._no_reply). A client made from that endpoint's spec (endpoint_spec's `count_error_usage`) counts it in its
    total, each failed attempt's (a retried one's too) priced as a reply's; an error that reports no token, a body that
    is no JSON and a count that is no number are not counted; a client of any other endpoint counts replies only, as
    before."""
    from types import SimpleNamespace
    from hermit.agent.base.endpoint import endpoint_spec
    from hermit.agent.model import llm
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    price = {"input": 1.0, "cached_input": 0.5, "output": 2.0}

    def failed(prompt, completion, cached=0):
        return json.dumps({"error": {"message": "the codex session gave no reply (exit 1): refused"},
                           "usage": {"prompt_tokens": prompt, "completion_tokens": completion,
                                     "total_tokens": prompt + completion,
                                     "prompt_tokens_details": {"cached_tokens": cached}}}).encode()
    ok = json.dumps({"choices": [{"index": 0, "finish_reason": "stop",
                                  "message": {"role": "assistant", "content": "42"}}],
                     "usage": {"prompt_tokens": 3, "completion_tokens": 1}}).encode()
    script = {"retried": [(503, failed(100, 10, 40)), (200, ok)], "refused": [(400, failed(30, 0))],
              "silent": [(503, failed(0, 0)), (503, b"busy"),
                         (400, b'{"error": {"message": "no"}, "usage": {"prompt_tokens": "many"}}')]}

    def reply(handler, body, upstream):
        status, out = script[body["model"]].pop(0)
        _answer(handler, status, out)
    up = _Upstream(reply)
    served = SimpleNamespace(url=up.url, agent=SimpleNamespace(settings={"model": "m"}))
    assert endpoint_spec(served, {"cli": "codex"})["count_error_usage"] is True
    ask = [{"role": "user", "content": "q"}]
    try:
        def client(model, counting=True, retries=0):
            return llm.ChatClient.from_spec({"base_url": up.url, "model": model, "price": price,
                                             "count_error_usage": counting}, max_retries=retries)
        retried = client("retried", retries=1)
        assert retried.chat(ask)[1].input_tokens == 3                      # the reply's own usage
        t = retried.total
        assert (t.input_tokens, t.cached_input_tokens, t.output_tokens, t.calls) == (103, 40, 11, 2)
        assert t.cost_usd == pytest.approx((60 * 1.0 + 40 * 0.5 + 10 * 2.0 + 3 * 1.0 + 1 * 2.0) / 1e6)
        refused = client("refused")
        with pytest.raises(llm.ChatHTTPError):
            refused.chat(ask)
        t = refused.total
        assert (t.input_tokens, t.output_tokens, t.calls) == (30, 0, 1) and t.cost_usd == pytest.approx(30 / 1e6)
        silent = client("silent", retries=2)
        with pytest.raises(llm.ChatHTTPError):
            silent.chat(ask)
        assert (silent.total.input_tokens, silent.total.calls) == (0, 0) and not script["silent"]
        script["retried"] = [(503, failed(100, 10, 40)), (200, ok)]
        plain = client("retried", counting=False, retries=1)
        plain.chat(ask)
        assert (plain.total.input_tokens, plain.total.output_tokens, plain.total.calls) == (3, 1, 1)
    finally:
        up.server.shutdown()


def test_the_chat_endpoint_relays_a_request_as_it_is():
    """A benchmark's own client reaches the model through `direct` at a local URL: body, Authorization header and the
    upstream's response pass unchanged; an upstream error keeps its status and is not retried."""
    import http.server
    import threading
    import urllib.error
    import urllib.request
    from hermit.agent.base.endpoint import ChatEndpoint
    from hermit.agent.model.llm import ChatClient
    seen = []

    class Upstream(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append((body, self.headers.get("Authorization")))
            if body.get("model") == "busy":
                out, status = b'{"error": "busy"}', 429
            else:
                out, status = json.dumps({"id": "r1", "object": "chat.completion", "model": body["model"],
                                          "choices": [{"index": 0, "finish_reason": "stop",
                                                       "message": {"role": "assistant", "content": "hi",
                                                                   "reasoning_content": "r"}}],
                                          "usage": {"prompt_tokens": 3, "completion_tokens": 1}}).encode(), 200
            self.send_response(status)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
    up = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    client = ChatClient(f"http://127.0.0.1:{up.server_address[1]}/v1", "ignored", max_retries=0)

    def post(url, body, auth="Bearer k1"):
        request = urllib.request.Request(url + "/v1/chat/completions", data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json", "Authorization": auth})
        return json.loads(urllib.request.urlopen(request).read())
    with ChatEndpoint(make("direct", {}, client)) as endpoint:
        body = {"model": "fw/glm", "messages": [{"role": "user", "content": "q"}], "max_tokens": 7,
                "reasoning_effort": "high", "tools": [{"type": "function", "function": {"name": "f", "parameters": {}}}]}
        reply = post(endpoint.url, body)
        assert reply["choices"][0]["message"]["content"] == "hi" and reply["id"] == "r1"
        assert seen[0] == (body, "Bearer k1")                       # the same body, no tool_choice added
        with pytest.raises(urllib.error.HTTPError) as err:
            post(endpoint.url, {**body, "model": "busy"})
        assert err.value.code == 429 and len(seen) == 2             # passed back, not retried
    up.shutdown()


def test_the_chat_endpoint_streams_a_whole_reply_as_chunks(monkeypatch):
    """ServeLearnBench's client streams: the relay asks upstream for the whole reply (keeping the client's read
    timeout alive meanwhile) and streams it back; the openai SDK reassembles content, tool calls and usage."""
    import http.server
    import threading
    import time
    openai = pytest.importorskip("openai")                # the SDK a client of the endpoint uses
    from hermit.agent.base import endpoint as module
    from hermit.agent.model.llm import ChatClient
    monkeypatch.setattr(module, "KEEPALIVE_S", 0.1)
    monkeypatch.setattr(module, "HEADER_WAIT_S", 0.1)               # the stream starts while the model works
    seen = []

    class Upstream(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(body)
            time.sleep(0.5)                                          # several keep-alives meanwhile
            if body["model"] == "down":
                out, status = b'{"error": {"message": "overloaded"}}', 503
            else:
                call = {"id": "c1", "type": "function", "function": {"name": "lookup", "arguments": '{"q": 1}'}}
                out, status = json.dumps({"id": "r2", "model": body["model"], "choices": [
                    {"index": 0, "finish_reason": "tool_calls", "message": {
                        "role": "assistant", "content": "thinking aloud", "reasoning_content": "r",
                        "tool_calls": [call]}}], "usage": {"prompt_tokens": 9, "completion_tokens": 4,
                                                           "total_tokens": 13}}).encode(), 200
            self.send_response(status)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
    up = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    client = ChatClient(f"http://127.0.0.1:{up.server_address[1]}/v1", "ignored", max_retries=0)
    with module.ChatEndpoint(make("direct", {}, client)) as endpoint:
        sdk = openai.OpenAI(base_url=endpoint.url + "/v1", api_key="k", max_retries=0)
        tools = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]
        stream = sdk.chat.completions.create(model="fw/glm", messages=[{"role": "user", "content": "q"}], stream=True,
                                             stream_options={"include_usage": True}, tools=tools,
                                             parallel_tool_calls=False, extra_body={"reasoning_effort": "high"})
        content, calls, finish, usage = [], {}, None, None
        for chunk in stream:
            usage = chunk.usage or usage
            for choice in chunk.choices:
                finish = choice.finish_reason or finish
                content.append(choice.delta.content or "")
                for tc in choice.delta.tool_calls or []:
                    calls[tc.index] = (tc.id, tc.function.name, tc.function.arguments)
        assert "".join(content) == "thinking aloud" and calls == {0: ("c1", "lookup", '{"q": 1}')}
        assert finish == "tool_calls" and usage.total_tokens == 13
        assert "stream" not in seen[0] and "stream_options" not in seen[0]           # asked upstream whole
        assert seen[0]["reasoning_effort"] == "high" and seen[0]["parallel_tool_calls"] is False
        with pytest.raises(openai.APIError, match="overloaded"):
            for _ in sdk.chat.completions.create(model="down", messages=[{"role": "user", "content": "q"}],
                                                 stream=True):
                pass
    up.shutdown()



class _Upstream:
    """A local chat-completions server whose replies a test scripts by the request's model name."""

    def __init__(self, reply):
        import http.server
        import threading
        self.seen, self.closed = [], threading.Event()
        upstream = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                upstream.seen.append(body)
                reply(self, body, upstream)
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"


def _answer(handler, status: int, body: bytes, headers: dict | None = None):
    handler.send_response(status)
    for k, v in (headers or {}).items():
        handler.send_header(k, v)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def test_the_chat_endpoint_passes_errors_on_as_the_upstream_gave_them():
    """A quick error keeps its status even for a streamed request (a client tells a context overflow from an outage
    by it), its whole body and its Retry-After; a reply cut off upstream is no answer here either."""
    openai = pytest.importorskip("openai")                # the SDK a client of the endpoint uses
    from hermit.agent.base.endpoint import ChatEndpoint
    from hermit.agent.model.llm import ChatClient
    long_message = "This model's maximum context length is 131072 tokens. " + "x" * 900

    def reply(handler, body, upstream):
        if body["model"] == "overflow":
            _answer(handler, 400, json.dumps({"error": {"message": long_message}}).encode())
        elif body["model"] == "busy":
            _answer(handler, 429, b'{"error": {"message": "slow down"}}', {"Retry-After": "7"})
        else:                                                         # a reply cut off mid-body
            handler.send_response(200)
            handler.send_header("Content-Length", "1000")
            handler.end_headers()
            handler.wfile.write(b'{"id": "r"')
            handler.wfile.flush()
            handler.connection.shutdown(2)
    up = _Upstream(reply)
    with ChatEndpoint(make("direct", {}, ChatClient(up.url, "x", max_retries=0))) as endpoint:
        sdk = openai.OpenAI(base_url=endpoint.url + "/v1", api_key="k", max_retries=0)
        messages = [{"role": "user", "content": "q"}]
        for stream in (False, True):
            with pytest.raises(openai.BadRequestError) as err:
                out = sdk.chat.completions.create(model="overflow", messages=messages, stream=stream)
                list(out) if stream else None
            assert err.value.status_code == 400 and long_message in str(err.value.body)
        with pytest.raises(openai.RateLimitError) as err:
            sdk.chat.completions.create(model="busy", messages=messages)
        assert err.value.response.headers.get("retry-after") == "7"
        with pytest.raises(openai.APIConnectionError):                # no answer, as from the upstream itself
            sdk.chat.completions.create(model="cut", messages=messages)
    up.server.shutdown()


def test_what_a_cli_session_cannot_take_is_refused_with_hermits_own_error_code():
    """A request a CLI session cannot take (here a figure; tool calls and another model are the runner tests') is
    refused before any session: a 400 whose code, endpoint.UNSUPPORTED, says it is the caller's settings, which no
    retry or other input changes (ahead of its message: a client's error text keeps only the start of the body).
    Hermit's own client reads it as any 400: no lost sign-in."""
    from hermit.agent.base import endpoint
    from hermit.agent.base.interface import login_failure
    from hermit.agent.model.llm import ChatHTTPError
    figure = [{"role": "user", "content": [{"type": "text", "text": "What does the figure show?"},
                                           {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}]}]
    status, body, headers = endpoint.cli_answer(make("codex", {"model": "gpt-x", "web_search": False}),
                                                {"messages": figure})
    error = json.loads(body)["error"]
    assert (status, list(error)) == (400, ["code", "message"]) and error["code"] == endpoint.UNSUPPORTED
    assert "not text" in error["message"] and endpoint.UNSUPPORTED in str(ChatHTTPError(status, body, headers))
    assert login_failure(ChatHTTPError(status, body, headers)) is None


def test_a_cli_session_its_own_settings_keep_from_starting_is_refused_with_hermits_own_error_code():
    """A session the CLI Base Agent will not start for its settings (here web search left unset; an isolation or an
    effort the CLI does not take is another) is the caller's settings as well, which no retry or other input changes:
    the endpoint's 400 carries endpoint.UNSUPPORTED ahead of its message, as a request a session cannot take does."""
    from hermit.agent.base import endpoint
    text = {"model": "gpt-x", "messages": [{"role": "user", "content": "What is 6 x 7?"}]}
    with endpoint.ChatEndpoint(make("codex", {"model": "gpt-x"})) as served:
        status, body, _ = served.answer(text, None)
    error = json.loads(body)["error"]
    assert (status, list(error), error["code"]) == (400, ["code", "message"], endpoint.UNSUPPORTED)
    assert error["message"].startswith("ValueError: ") and "web_search" in error["message"]


def test_the_chat_endpoint_says_it_closes_each_connection():
    """The endpoint closes a connection after its reply; the reply says so, or an HTTP/1.1 client keeps the
    connection for its next request and finds it closed under it (a dropped connection, not the answer)."""
    import http.client
    from hermit.agent.base.endpoint import ChatEndpoint
    from hermit.agent.model.llm import ChatClient

    def reply(handler, body, upstream):
        if body["messages"][-1]["content"] == "bad":
            _answer(handler, 400, b'{"error": {"message": "no"}}')
        else:
            _answer(handler, 200, json.dumps({"id": "c", "object": "chat.completion", "model": "x", "choices": [
                {"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}]}).encode(),
                {"Content-Type": "application/json"})
    up = _Upstream(reply)
    with ChatEndpoint(make("direct", {}, ChatClient(up.url, "x", max_retries=0))) as endpoint:
        host, port = "127.0.0.1", endpoint.port
        token = endpoint.url.rsplit("/", 1)[-1]
        for content, status in (("bad", 400), ("q", 200)):           # an error, and an answer (streamed: SSE)
            for stream in (False, True):
                conn = http.client.HTTPConnection(host, port, timeout=10)
                conn.request("POST", f"/{token}/v1/chat/completions",
                             json.dumps({"model": "m", "messages": [{"role": "user", "content": content}],
                                         "stream": stream}), {"Content-Type": "application/json"})
                response = conn.getresponse()
                response.read()
                assert response.status == status and response.getheader("Connection") == "close"
                conn.close()
    up.server.shutdown()


def test_a_call_given_up_after_the_headers_stops_reading_the_body():
    import threading
    import time
    from hermit.agent.model.llm import ChatAborted, ChatClient

    def reply(handler, body, upstream):                              # the headers now, the body much later
        payload = json.dumps({"choices": [{"message": {"role": "assistant", "content": "late"}}]}).encode()
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.flush()
        upstream.closed.wait(3)
        try:
            handler.wfile.write(payload)
        except OSError:
            pass
    up = _Upstream(reply)
    abort, box = threading.Event(), {}

    def call():
        try:
            box["result"] = ChatClient(up.url, "x", max_retries=0).chat([{"role": "user", "content": "q"}],
                                                                        abort=abort)
        except Exception as exc:                                     # noqa: BLE001
            box["error"] = exc
    worker = threading.Thread(target=call, daemon=True)
    worker.start()
    deadline = time.time() + 5
    while not up.seen and time.time() < deadline:
        time.sleep(0.05)
    time.sleep(0.3)                                                  # the headers are in; the body is awaited
    started = time.time()
    abort.set()
    worker.join(2)
    up.closed.set()
    assert not worker.is_alive() and time.time() - started < 2
    assert isinstance(box.get("error"), ChatAborted), box
    up.server.shutdown()


def test_a_client_that_hangs_up_gives_the_call_up_upstream():
    import socket
    import time
    from hermit.agent.base.endpoint import ChatEndpoint
    from hermit.agent.model.llm import ChatClient

    def reply(handler, body, upstream):
        handler.connection.settimeout(10)
        try:
            data = handler.connection.recv(1)                        # waits: b"" once the relay closes its side
        except OSError:
            data = b""
        if data == b"":
            upstream.closed.set()
    up = _Upstream(reply)
    with ChatEndpoint(make("direct", {}, ChatClient(up.url, "x", max_retries=0))) as endpoint:
        body = json.dumps({"model": "slow", "messages": [{"role": "user", "content": "q"}], "stream": True}).encode()
        client = socket.create_connection(("127.0.0.1", endpoint.port))
        client.sendall(f"POST /{endpoint.token}/v1/chat/completions HTTP/1.1\r\nHost: x\r\n"
                       f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
        deadline = time.time() + 5
        while not up.seen and time.time() < deadline:
            time.sleep(0.05)
        client.close()                                                # the client's own watchdog gives up
        assert up.closed.wait(5)                                      # and the relay hangs up upstream
    up.server.shutdown()


def test_a_tool_server_that_cannot_serve_fails_at_start(monkeypatch):
    import uvicorn

    async def broken(self, sockets=None):
        raise OSError("no socket")
    monkeypatch.setattr(uvicorn.Server, "serve", broken)
    with pytest.raises(RuntimeError, match="did not start.*no socket"):
        ToolServer([ADD]).start()


def test_a_declared_tool_with_an_empty_output_schema_returns_an_object(tmp_path):
    from hermit.tools import declared
    (tmp_path / "t.py").write_text("def f():\n    return 'text'\n")
    tool = declared({"name": "f", "description": "d", "entrypoint": "t.py:f", "output_schema": {}}, tmp_path, "task", {})
    assert tool.output_schema == {"type": "object"} and tool.answer("{}").startswith("Error: ToolError")


def test_the_relay_waits_as_long_as_the_upstream_and_keeps_each_timeout_apart():
    from hermit.agent.base.endpoint import _relays, relay
    url = "http://127.0.0.1:9/v1"
    plain, short = relay(url), relay(url, timeout=5)
    assert plain != short and relay(url) == plain
    assert _relays[f"{url} None"].agent.model.timeout is None and _relays[f"{url} 5"].agent.model.timeout == 5


def test_a_call_given_up_before_it_connects_sends_nothing():
    import threading
    from hermit.agent.model.llm import ChatAborted, ChatClient
    up = _Upstream(lambda handler, body, upstream: _answer(handler, 200, b"{}"))
    abort = threading.Event()
    abort.set()
    with pytest.raises(ChatAborted):
        ChatClient(up.url, "x", max_retries=0).chat([{"role": "user", "content": "q"}], abort=abort)
    assert up.seen == []
    up.server.shutdown()


def test_without_isolation_data_is_linked_inside_the_workspace_only(tmp_path):
    from hermit.agent.base.cli import _link
    old, new, work = tmp_path / "old", tmp_path / "new", tmp_path / "work"
    for d in (old, new, work):
        d.mkdir()
    agent = make("codex", {"web_search": False})
    with pytest.raises(Unsupported, match="links data into the workspace"):
        agent.run("go", workspace=work, limits=Limits(readable=[Mount(new, tmp_path / "elsewhere")]))
    (work / "docs").symlink_to(old)                                        # left by an earlier call
    _link([Mount(new, work / "docs")])
    assert (work / "docs").resolve() == new


def test_without_isolation_claude_code_gets_data_bound_inside_the_workspace(tmp_path, monkeypatch):
    """A search that walks the tree (`grep -r`, `find`, Claude Code's Glob and Grep) does not follow a link: without
    isolation a mount inside a Claude Code workspace is bound there, a real read-only directory that `find` descends
    into, as in a container (Codex keeps the link)."""
    import os
    from hermit.agent.base import cli as base_cli
    if base_cli._bwrap_binds() is None:
        pytest.skip("needs a working bubblewrap")
    data, work, bin_dir, user = tmp_path / "data", tmp_path / "work", tmp_path / "bin", tmp_path / "user"
    for d in (data / "deep", work, bin_dir, user):
        d.mkdir(parents=True)
    (data / "deep" / "a.txt").write_text("1934 557\n")
    (work / "docs").symlink_to(tmp_path)                                   # a link an earlier call left
    fake = bin_dir / "claude"
    fake.write_text('#!/bin/sh\ncat > /dev/null\n{ find docs -name "*.txt"; [ -L docs ] && echo link; '
                    'touch docs/new 2>/dev/null && echo writable; } > found.txt\n'
                    'echo \'{"type":"result","subtype":"success","is_error":false,"result":"ok","num_turns":1}\'\n')
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("HOME", str(user))                                  # no sign-in of the user's to copy
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    result = make("claude", {"web_search": False}).run(
        "go", workspace=work, limits=Limits(readable=[Mount(data, Path("docs"))]))
    assert result.reply == "ok" and result.exit_code == 0 and result.extra["mounts"] == {str(work / "docs"): "bound"}
    assert (work / "found.txt").read_text().split() == ["docs/deep/a.txt"]      # descended into; no link; read-only
    assert (work / "docs").is_dir() and not (work / "docs").is_symlink() and not list((work / "docs").iterdir())


def test_without_bubblewrap_claude_codes_data_is_linked_and_said_so(tmp_path, monkeypatch, capsys):
    """Where bubblewrap cannot bind (no bwrap, no user namespaces), the mount is linked as for Codex, the result says so
    and the process warns once: Claude Code's own Glob and Grep will not see inside it."""
    import os
    from hermit.agent.base import cli as base_cli
    data, work, bin_dir, user = tmp_path / "data", tmp_path / "work", tmp_path / "bin", tmp_path / "user"
    for d in (data, work, bin_dir, user):
        d.mkdir(parents=True)
    fake = bin_dir / "claude"
    fake.write_text('#!/bin/sh\ncat > /dev/null\necho \'{"type":"result","subtype":"success","is_error":false,'
                    '"result":"ok","num_turns":1}\'\n')
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("HOME", str(user))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(base_cli, "_bwrap_binds", lambda: None)
    monkeypatch.setattr(base_cli, "_WARNED", set())
    for _ in range(2):
        result = make("claude", {"web_search": False}).run(
            "go", workspace=work, limits=Limits(readable=[Mount(data, Path("docs"))]))
        assert result.extra["mounts"] == {str(work / "docs"): "linked"} and (work / "docs").is_symlink()
    assert capsys.readouterr().err.count("linked, not bound") == 1


def test_the_sandbox_shows_a_mount_at_its_target(tmp_path):
    """A mount is a bind in the run's sandbox: shown under its target (a relative one under the workspace), not at
    its own path."""
    seen = {}

    class Box:
        enabled, network, allow = True, "isolated", []

        def wrap(self, command, **kw):
            seen.update(kw)
            return command, kw["env"]
    data, work = tmp_path / "data", tmp_path / "work"
    data.mkdir()
    work.mkdir()
    make("codex", {"web_search": False, "isolation": "sandbox", "sandbox": Box(), "bin": "true"}).run(
        "go", workspace=work, limits=Limits(readable=[Mount(data, Path("docs")), Mount(data, Path("/data"))]))
    assert (data, str(work.resolve() / "docs"), False) in seen["binds"] and (data, "/data", False) in seen["binds"]
    assert seen["chdir"] == work.resolve()


def test_a_call_given_up_while_a_proxy_holds_its_connect_stops(monkeypatch):
    import socket
    import threading
    import time
    from hermit.agent.model.llm import ChatAborted, ChatClient
    proxy = socket.socket()
    proxy.bind(("127.0.0.1", 0))
    proxy.listen()
    held = []
    threading.Thread(target=lambda: held.append(proxy.accept()), daemon=True).start()   # accepts, never answers
    for name in ("https_proxy", "HTTPS_PROXY"):
        monkeypatch.setenv(name, f"http://127.0.0.1:{proxy.getsockname()[1]}")
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    abort = threading.Event()
    threading.Timer(0.5, abort.set).start()
    started = time.time()
    with pytest.raises(ChatAborted):
        ChatClient("https://api.example.com/v1", "x", max_retries=0, timeout=10).chat(
            [{"role": "user", "content": "q"}], abort=abort)
    assert time.time() - started < 3
    proxy.close()


def test_without_isolation_a_target_cannot_climb_out_of_the_workspace(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "work").mkdir()
    with pytest.raises(Unsupported, match="links data into the workspace"):
        make("codex", {"web_search": False}).run(
            "go", workspace=tmp_path / "work", limits=Limits(readable=[Mount(tmp_path / "data", Path("../escaped"))]))
    assert not (tmp_path / "escaped").exists()


def test_a_model_only_network_limit_holds_where_the_sandbox_allows_just_that_model(tmp_path):
    """The run's sandbox allows the session's provider (its key and host are given to it): limiting the session to
    its model is kept; another allowed host still refuses it."""
    from hermit.sandbox import Sandbox
    binding = {"base_url": "https://api.example.com/v1", "model": "m", "api_key_env": "EXAMPLE_KEY"}
    cases = ((["api.example.com"], [], {}, False), (["api.example.com", "other.example.com"], [], {}, True),
             (["api.example.com"], [5432], {}, True),               # a database forwarded besides
             (["api.example.com"], [], {3130: tmp_path / "svc.sock"}, True))     # a service through a socket
    for allow, forward, sockets, refused in cases:
        box = Sandbox(network="allowlist", allow=allow, forward=forward, sockets=sockets)
        agent = make("codex", {"web_search": False, "isolation": "sandbox", "sandbox": box, "binding": binding}, None)
        if refused:
            with pytest.raises(Unsupported, match="more than the model"):
                agent._isolation(tmp_path, Limits(network=[]))
        else:
            assert agent._isolation(tmp_path, Limits(network=[]))[0] == "sandbox"


def test_a_login_sessions_plan_waits_come_off_its_callers_deadline(tmp_path, monkeypatch):
    """A container session's caller is stopped at its deadline: time spent waiting for the login's plan (and a
    refused session's rerun) is not given back to the session's timeout; past the deadline no session starts."""
    import contextlib
    import time as _time
    from hermit.agent.base import cli as base_cli
    given = []
    monkeypatch.setattr(base_cli.codex_login, "wait", lambda *a: _time.sleep(2) or None)
    monkeypatch.setattr(base_cli.codex_login, "slot", lambda *a: contextlib.nullcontext(True))
    monkeypatch.setattr(base_cli.codex_login, "held", lambda plan, *_: False)
    agent = make("codex", {"web_search": False, "login": "chatgpt", "plan_dir": str(tmp_path / "plan"),
                           "deadline": _time.time() + 10}, None)
    monkeypatch.setattr(type(agent), "_session", lambda self, prompt, **kw: given.append(kw["limits"].timeout_s)
                        or base_cli.Result(reply="ok", extra={"served": True}))
    agent.run("go", workspace=tmp_path, limits=Limits(timeout_s=3600))
    assert given and given[0] <= 8.1
    far = agent.configured(deadline=_time.time() + 7200)            # a distant deadline keeps the default hour
    assert far._within(None).timeout_s == 3600
    late = agent.configured(deadline=_time.time() + 1)
    result = late.run("go", workspace=tmp_path, limits=Limits(timeout_s=3600))
    assert result.extra["unavailable"] and "deadline passed" in result.extra["output_tail"] and len(given) == 1


def test_a_session_without_limits_or_deadline_runs(tmp_path, monkeypatch):
    """No deadline and no limits: the session runs (a caller's None limits are no expired deadline)."""
    from hermit.agent.base import cli as base_cli
    ran = []
    agent = make("codex", {"web_search": False}, None)
    monkeypatch.setattr(type(agent), "_session", lambda self, prompt, **kw: ran.append(kw["limits"])
                        or base_cli.Result(reply="ok", extra={"served": True}))
    assert agent.run("go", workspace=tmp_path).reply == "ok" and ran == [None]


def test_the_sessions_preparation_comes_off_its_callers_deadline(tmp_path, monkeypatch):
    """The sign-in copy, the tool server and the container checks take time before the CLI starts: the process is
    given what is left of the deadline, and none is started past it."""
    import time as _time
    from hermit.agent.base import cli as base_cli
    given = []
    monkeypatch.setattr(base_cli.CliAgent, "_sign_in", lambda self, home: _time.sleep(1.5) or [])
    monkeypatch.setattr(base_cli, "run_process", lambda command, **kw: given.append(kw["timeout"]) or (0, False, False))
    agent = make("codex", {"web_search": False, "deadline": _time.time() + 5}, None)
    agent.run("go", workspace=tmp_path, limits=Limits(timeout_s=3600))
    assert given and given[0] <= 3.6
    late = agent.configured(deadline=_time.time() + 1)
    assert late.run("go", workspace=tmp_path, limits=Limits(timeout_s=3600)).extra["unavailable"] and len(given) == 1


def test_a_slow_sandbox_wrap_comes_off_the_deadline_too(tmp_path, monkeypatch):
    """The run's sandbox prepares the command (packages, binds, the relay) after the sign-in: what that takes comes
    off the deadline as well, checked just before the CLI starts."""
    import time as _time
    from hermit.agent.base import cli as base_cli
    from hermit.sandbox import Sandbox
    given = []
    box = Sandbox(enabled=False)
    monkeypatch.setattr(Sandbox, "wrap", lambda self, command, **kw: (_time.sleep(1.5), (command, kw["env"]))[1])
    monkeypatch.setattr(base_cli, "run_process", lambda command, **kw: given.append(kw["timeout"]) or (0, False, False))
    agent = make("codex", {"web_search": False, "isolation": "sandbox", "sandbox": box,
                           "deadline": _time.time() + 5}, None)
    agent.run("go", workspace=tmp_path, limits=Limits(timeout_s=3600))
    assert given and given[0] <= 3.6


def test_the_logins_waits_never_sleep_past_what_the_caller_has_left(tmp_path, monkeypatch):
    """A paused plan with a second left: the wait returns at that second, not after its 30 s poll; a hold that cannot
    be written waits at most what is left, not five minutes."""
    import threading
    import time as _time
    from hermit.agent.base import login as L
    (tmp_path / "PAUSE").write_text("")
    started = _time.time()
    assert L.wait(tmp_path, None, None, max_s=0.5) == "timeout"
    assert _time.time() - started < 5
    def unwritable(*_, **__):
        raise OSError("read-only")
    monkeypatch.setattr(L, "_hold", unwritable)
    started = _time.time()
    L.hold(tmp_path, cancel=threading.Event(), max_s=0.3)
    assert _time.time() - started < 5


def test_hiding_a_path_under_the_interpreters_prefix_that_no_grant_shows_is_kept(tmp_path):
    """The check compares a path to hide with what the sandbox really shows, not with the whole interpreter
    prefix: a file under sys.prefix that no grant holds can be hidden."""
    import sys
    from hermit.sandbox import Sandbox
    if Path(sys.prefix).resolve().is_relative_to("/usr"):
        pytest.skip("a system Python: its prefix lies in /usr, which every sandbox shows")
    box = Sandbox(network="isolated")
    agent = make("codex", {"web_search": False, "isolation": "sandbox", "sandbox": box}, None)
    assert agent._isolation(tmp_path, Limits(hidden=[Path(sys.prefix) / "private-data" / "answers.json"]))[0] == \
        "sandbox"


def test_a_system_file_every_sandbox_shows_cannot_be_hidden(tmp_path):
    from hermit.sandbox import Sandbox
    agent = make("codex", {"web_search": False, "isolation": "sandbox", "sandbox": Sandbox(network="isolated")}, None)
    with pytest.raises(Unsupported, match="lies in what the sandbox shows"):
        agent._isolation(tmp_path, Limits(hidden=[Path("/etc/passwd")]))
