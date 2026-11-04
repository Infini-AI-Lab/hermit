"""Layer 2's API (docs/framework/agent-layers.md): direct, react and the CLIs behind one `run`, limits enforced or
refused, tools served over MCP."""
import asyncio
import json
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


def test_claude_takes_no_container_and_a_container_hides_nothing_it_is_given(tmp_path):
    with pytest.raises(Unsupported, match="container runs Codex"):
        make("claude", {"web_search": False, "isolation": "container"}).run("go", workspace=tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    with pytest.raises(Unsupported, match="lies in what the container is given"):
        make("codex", {"web_search": False, "isolation": "container"}).run(
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


def test_a_sandboxed_cli_refuses_data_it_cannot_place_or_hosts_it_cannot_limit(tmp_path):
    class Box:
        enabled, network, allow = True, "isolated", ["example.com"]

        def wrap(self, command, **kw):
            return command, kw["env"]
    agent = make("codex", {"web_search": False, "isolation": "sandbox", "sandbox": Box()})
    data = tmp_path / "data"
    data.mkdir()
    with pytest.raises(Unsupported, match="its own path"):
        agent.run("go", workspace=tmp_path, limits=Limits(readable=[Mount(data, Path("docs"))]))
    with pytest.raises(Unsupported, match="sandbox.allow"):
        agent.run("go", workspace=tmp_path, limits=Limits(network=[]))
    with pytest.raises(Unsupported, match="did not isolate"):        # a wrap without bubblewrap hides nothing
        agent.run("go", workspace=tmp_path, limits=Limits(hidden=[tmp_path / "secret"]))


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


def test_the_chat_endpoint_relays_a_request_as_it_is():
    """A benchmark's own client reaches the model through `direct` at a local URL: body, Authorization header and the
    upstream's response pass unchanged; an upstream error keeps its status; streaming is refused."""
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
    import openai
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
    import openai
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


def test_react_counts_its_own_calls_on_any_model_with_chat():
    def reply(messages, **_):
        return {"content": "hi", "finish_reason": "stop"}
    result = make("react", {}, FunctionModel(reply)).run("say hi")          # a model with chat() and no `total`
    assert result.reply == "hi" and result.usage[0].calls == 1


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


def test_the_sandbox_places_a_relative_target_under_the_workspace(tmp_path, monkeypatch):
    class Box:
        enabled, network, allow = True, "isolated", []

        def wrap(self, command, **kw):
            return command, kw["env"]
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.chdir(tmp_path)                                           # "data" from here is the source itself
    with pytest.raises(Unsupported, match="its own path"):
        make("codex", {"web_search": False, "isolation": "sandbox", "sandbox": Box()}).run(
            "go", workspace=tmp_path / "work", limits=Limits(readable=[Mount(data, Path("data"))]))


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
