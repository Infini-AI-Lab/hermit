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

    def __init__(self):
        from hermit.agent.model.usage import Usage
        self.total, self.model = Usage("m"), "m"

    def chat(self, messages, **kwargs):
        from hermit.agent.model.usage import Usage
        self.total.calls += 1
        self.total.output_tokens += 1
        if messages[-1]["role"] != "tool":
            call = {"id": "c1", "type": "function", "function": {"name": "add", "arguments": '{"a": 2, "b": 3}'}}
            return {"role": "assistant", "content": None, "tool_calls": [call], "finish_reason": "tool_calls"}, Usage("m")
        return {"role": "assistant", "content": f"got {messages[-1]['content']}", "finish_reason": "stop"}, Usage("m")


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
                return names, good.structured_content, bad.is_error

    with ToolServer([ADD]) as server:
        assert asyncio.run(over_mcp(server.mcp_url)) == (["add"], {"sum": 3}, True)
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


def test_codex_sub_agent_calls_are_steps():
    from hermit.agent.base.trajectory import codex_steps
    spawn = {"type": "item.completed", "item": {"type": "collab_tool_call", "tool": "spawn_agent",
                                                "receiver_thread_ids": ["t2"], "prompt": "verify 507",
                                                "agents_states": {"t2": {"status": "pending"}}, "status": "completed"}}
    [step] = codex_steps([spawn])
    assert step.kind == "tool_call" and json.loads(step.text)["tool"] == "spawn_agent"
    assert json.loads(step.text)["prompt"] == "verify 507" and "pending" in step.output


def test_the_chat_client_waits_as_told_between_retries(monkeypatch):
    import urllib.error
    from hermit.agent.model import llm
    waits = []
    monkeypatch.setattr(llm.time, "sleep", waits.append)

    def unavailable(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 503, "busy", {}, None)
    monkeypatch.setattr(llm.urllib.request, "urlopen", unavailable)
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
