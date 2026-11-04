"""A `direct` Base Agent behind an OpenAI-compatible URL, for code that only takes a base URL
(docs/framework/agent-layers.md#layer-3-agent): a benchmark's own client, in this process or another.

    with ChatEndpoint(make("direct", {}, ChatClient(upstream, model, max_retries=0))) as endpoint:
        client(base_url=endpoint.url)          # POST <url>/chat/completions

Each request is one `direct` call. The endpoint is a relay, so that the benchmark's requests reach the model as they
did before: every field of the request body (the model, the sampling, the tools, the provider's own fields) is sent
on as it is, and so is its Authorization header; the reply is the upstream's response body. An upstream error comes
back with its status, its whole body and its Retry-After; an upstream that gave no answer (no connection, a reply cut
off) leaves the client's connection closed without one, as the upstream left the relay's; the endpoint retries
nothing (the client does, as it did). A client that hangs up has its call given up upstream too.

A request that asks for a stream (ServeLearnBench's client streams) is sent upstream whole (`stream` and
`stream_options` dropped: the one difference from the client's own request). The status is held back while the
model works, up to HEADER_WAIT_S, so that an error (a context overflow, a refused key) comes back with its status, as
from the upstream itself; the reply is then streamed in the chat-completions chunk format: the message as one delta
(content, reasoning, tool calls with their index), the finish reason, the usage when `stream_options.include_usage`
asked for it, `[DONE]`. A model still working at HEADER_WAIT_S gets the stream started and an SSE comment every
KEEPALIVE_S, so that the client's read timeout does not fire; an error after that arrives as an `error` event.

The URL carries a random token, so that only whoever was handed it can call the model; it listens on loopback
unless told otherwise.
"""
from __future__ import annotations

import json
import secrets
import select
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from hermit.agent.base.interface import BaseAgent

HEADER_WAIT_S = 120.0
KEEPALIVE_S = 15.0
_DROP = object()                                  # no answer: close the client's connection without one


class ChatEndpoint:
    def __init__(self, agent: BaseAgent, host: str = "127.0.0.1"):
        self.agent, self.host = agent, host
        self.token = secrets.token_hex(16)
        endpoint = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _closing(self):
                """A connection this handler closes after its reply says so (`Connection: close`): an HTTP/1.1
                client would otherwise keep it for its next request and find it closed under that request."""
                if self.close_connection:
                    self.send_header("Connection", "close")

            def _send(self, status: int, body: bytes, headers: dict | None = None):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self._closing()
                self.end_headers()
                self.wfile.write(body)

            def _gone(self) -> bool:
                """Whether the client hung up (its socket reads the end)."""
                try:
                    ready, _, _ = select.select([self.connection], [], [], 0)
                    return bool(ready) and self.connection.recv(1, socket.MSG_PEEK) == b""
                except OSError:
                    return True

            def _wait(self, worker: threading.Thread, abort: threading.Event, seconds: float) -> bool:
                """Wait for the call up to `seconds`; False when the client hung up (the call is given up)."""
                end = time.time() + seconds
                while worker.is_alive() and time.time() < end:
                    worker.join(0.5)
                    if worker.is_alive() and self._gone():
                        abort.set()
                        return False
                return True

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.path.rstrip("/") not in (f"/{endpoint.token}/chat/completions",
                                                 f"/{endpoint.token}/v1/chat/completions"):
                    return self._send(404, b'{"error": {"message": "not found"}}')
                try:
                    request = json.loads(body or b"{}")
                    if not isinstance(request, dict):
                        raise ValueError("the request body is no JSON object")
                except ValueError as exc:
                    return self._send(400, json.dumps({"error": {"message": str(exc)}}).encode())
                stream = bool(request.pop("stream", False))
                include_usage = bool((request.pop("stream_options", None) or {}).get("include_usage")) \
                    if stream else False
                abort, box = threading.Event(), {}
                worker = threading.Thread(target=lambda: box.update(
                    reply=endpoint.answer(request, self.headers.get("Authorization"), abort)), daemon=True)
                worker.start()
                self.close_connection = True
                if not self._wait(worker, abort, HEADER_WAIT_S if stream else float("inf")):
                    return
                if not worker.is_alive():
                    reply = box["reply"]
                    if reply is _DROP:
                        return                          # as the upstream left the relay: no answer
                    status, data, headers = reply
                    if status != 200 or not stream:
                        return self._send(status, data, headers)
                self._stream(worker, abort, box, request.get("model"), include_usage)

            def _stream(self, worker, abort, box, model, include_usage: bool):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self._closing()
                self.end_headers()
                try:
                    while worker.is_alive():
                        if not self._wait(worker, abort, KEEPALIVE_S):
                            return
                        if worker.is_alive():
                            self.wfile.write(b": the model is working\n\n")
                            self.wfile.flush()
                    reply = box["reply"]
                    if reply is _DROP:
                        return                          # a stream cut off, as the upstream's was
                    status, data, _ = reply
                    for event in _chunks(status, data, model, include_usage):
                        self.wfile.write(b"data: " + event + b"\n\n")
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    abort.set()                         # the client hung up: give the call up

        self._server = ThreadingHTTPServer((host, 0), Handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self.url = f"http://{host}:{self.port}/{self.token}"
        self._thread: threading.Thread | None = None

    def answer(self, request: dict, authorization: str | None, abort: threading.Event | None = None):
        """One request (its JSON body, not streamed): (status, response body, headers to pass on), or _DROP when the
        upstream gave no answer."""
        if self.agent.name in ("codex", "claude"):
            from hermit.agent.base.interface import Unsupported
            try:
                return cli_answer(self.agent, request, abort)
            except (ValueError, Unsupported) as exc:            # the session's settings: the client reads why
                return 400, json.dumps({"error": {"message": f"{type(exc).__name__}: {exc}"}}).encode(), {}
            except Exception as exc:                            # noqa: BLE001 - the session failed to run
                return 502, json.dumps({"error": {"message": f"{type(exc).__name__}: {exc}"}}).encode(), {}
        from hermit.agent.model.llm import ChatAborted, ChatHTTPError
        try:
            request = dict(request)
            messages = request.pop("messages")
            tools = request.pop("tools", None) or ()
            choice = request.pop("tool_choice", None)
            fields = {**request, "return_response": True,
                      **({"extra_headers": {"Authorization": authorization}} if authorization else {}),
                      **({"abort": abort} if abort is not None else {})}
            agent = self.agent.configured(request=fields)
            agent.settings["tool_choice"] = choice          # as the request had it: none when it gave none
            result = agent.run(messages, tools=tools)
            return 200, json.dumps(result.extra["message"]["_response"]).encode(), {}
        except ChatHTTPError as exc:
            keep = {k: v for k, v in exc.headers.items() if k.lower() in ("retry-after", "x-request-id")}
            return exc.code, exc.body, keep
        except ChatAborted:
            return _DROP
        except RuntimeError as exc:
            if str(exc).startswith("chat/completions failed"):
                return _DROP                                # no answer upstream: none here either
            return 502, json.dumps({"error": {"message": str(exc)}}).encode(), {}
        except Exception as exc:                            # noqa: BLE001 - the client reads an error, as from a server
            return 400, json.dumps({"error": {"message": f"{type(exc).__name__}: {exc}"}}).encode(), {}

    def start(self) -> "ChatEndpoint":
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True, name="hermit-chat-endpoint")
        self._thread.start()
        return self

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self) -> "ChatEndpoint":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.close()


def _chunks(status: int, reply: bytes, model, include_usage: bool):
    """A whole response as the chunks of a streamed one (JSON-encoded events)."""
    if status != 200:
        try:
            error = json.loads(reply)
        except ValueError:
            error = {"message": reply.decode(errors="replace")}
        error = error.get("error", error) if isinstance(error, dict) else {"message": str(error)}
        yield json.dumps({"error": {**(error if isinstance(error, dict) else {"message": str(error)}),
                                    "code": status}}).encode()
        return
    response = json.loads(reply)
    head = {"id": response.get("id") or "chatcmpl-relay", "object": "chat.completion.chunk",
            "created": response.get("created") or int(time.time()), "model": response.get("model") or model}
    choice = (response.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    delta = {"role": "assistant"}
    for key in ("content", "reasoning_content", "reasoning"):
        if message.get(key):
            delta[key] = message[key]
    if message.get("tool_calls"):
        delta["tool_calls"] = [{"index": i, **call} for i, call in enumerate(message["tool_calls"])]
    yield json.dumps({**head, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}).encode()
    yield json.dumps({**head, "choices": [{"index": 0, "delta": {},
                                           "finish_reason": choice.get("finish_reason")}]}).encode()
    if include_usage and response.get("usage"):
        yield json.dumps({**head, "choices": [], "usage": response["usage"]}).encode()


def _text(content) -> str:
    """A message's text: a string, or the text parts of a list of parts."""
    if isinstance(content, list):
        return "\n".join(str(p.get("text") or "") for p in content
                         if isinstance(p, dict) and p.get("type") in ("text", "input_text", "output_text"))
    return "" if content is None else str(content)


def split_messages(messages: list[dict]) -> tuple[str, str]:
    """(instructions, prompt): the system and developer messages, and the rest: the one user message as it is, or
    several turns written out as a transcript that ends with the turn to answer."""
    system = "\n\n".join(_text(m.get("content")) for m in messages if m.get("role") in ("system", "developer"))
    rest = [m for m in messages if m.get("role") not in ("system", "developer")]
    if len(rest) == 1 and rest[0].get("role") == "user":
        return system, _text(rest[0].get("content"))
    turns = [f"[{m.get('role')}]\n{_text(m.get('content'))}" for m in rest]
    return system, ("The conversation so far, oldest first; answer its last user turn as the assistant.\n\n"
                    + "\n\n".join(turns))


def cli_answer(agent: BaseAgent, request: dict, abort: threading.Event | None = None):
    """A chat-completions request answered by one session of a CLI Base Agent (`codex`, `claude`): a model reachable
    only through the CLI (a ChatGPT login) for code that calls a chat API (an Evolver's `llm`). The system messages
    are the session's instructions, the rest its prompt (split_messages), given on stdin; the session works in an
    empty workspace of its own, and its last message is the reply, with its token usage. `reasoning_effort` is the
    session's effort; sampling settings a CLI has none for (max_tokens, temperature) are not applied. A session the
    provider could not serve answers 503 (the client retries), one past its clock 504, one without a reply 502.
    The session sees no files: a caller that wants a session to read files runs one itself (`make(...).run`). Its
    event stream is kept under the agent's `log_dir` setting when there is one (the Trainer gives the run's
    `llm_sessions/`), and is removed after the call otherwise."""
    import shutil
    import tempfile
    from pathlib import Path
    messages = request.get("messages")
    if not isinstance(messages, list) or not messages:
        return 400, json.dumps({"error": {"message": "messages: a non-empty list"}}).encode(), {}
    system, prompt = split_messages(messages)
    own = {"stdin": True, **({"cancel": abort} if abort is not None else {})}
    if request.get("reasoning_effort"):
        own["effort"] = str(request["reasoning_effort"])
    if system and agent.name == "codex":
        from hermit.agent.base.cli import codex_instructions
        extra, prompt = codex_instructions(system, prompt)
        own["args"] = [*(agent.settings.get("args") or []), *extra]
    elif system:
        own["append_system_prompt"] = system
    log_dir, scratch = agent.settings.get("log_dir"), None
    if log_dir:
        own["log"] = Path(log_dir) / f"{time.strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(4)}-{agent.name}.jsonl"
    elif not agent.settings.get("log"):                       # no record asked for: a stream removed with the call
        scratch = Path(tempfile.mkdtemp(prefix=f"hermit-chat-log-{agent.name}-"))
        own["log"] = scratch / f"agent-{agent.name}.jsonl"
    work = Path(tempfile.mkdtemp(prefix=f"hermit-chat-{agent.name}-"))
    try:
        result = agent.configured(**own).run(prompt, workspace=work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if scratch is not None:                                # also when the call raised
            shutil.rmtree(scratch, ignore_errors=True)
    if result.extra.get("cancelled"):
        return _DROP
    if not result.extra.get("served") or not result.reply.strip():
        why = "timed out" if result.timed_out else f"exit {result.exit_code}"
        status = 504 if result.timed_out else 503 if result.extra.get("unavailable") else 502
        return status, json.dumps({"error": {"message": f"the {agent.name} session gave no reply ({why}): "
                                             f"{result.extra.get('output_tail', '')[-600:]}"}}).encode(), {}
    u = result.usage[0] if result.usage else None
    prompt_tokens, completion = (u.input_tokens, u.output_tokens) if u else (0, 0)
    body = {"id": f"chatcmpl-{secrets.token_hex(6)}", "object": "chat.completion", "created": int(time.time()),
            "model": request.get("model") or agent.settings.get("model"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": result.reply},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion,
                      "total_tokens": prompt_tokens + completion,
                      "prompt_tokens_details": {"cached_tokens": u.cached_input_tokens if u else 0}}}
    return 200, json.dumps(body, ensure_ascii=False).encode(), {}


_relays: dict[str, ChatEndpoint] = {}
_relays_lock = threading.Lock()


def relay(upstream: str, *, timeout: float | None = None) -> str:
    """The base URL to give a client in place of `upstream`: a relay endpoint over the `direct` Base Agent, one per
    upstream and timeout for the process (started on first use). The relay retries nothing and, by default, waits
    as long as the upstream takes: the client's own timeout and retries hold, as they did (a client that gives up
    hangs up, and the relay gives the call up)."""
    key = f"{upstream} {timeout}"
    with _relays_lock:
        if key not in _relays:
            from hermit.agent.base.interface import make
            from hermit.agent.model.llm import ChatClient
            client = ChatClient(upstream.rstrip("/"), "relay", max_retries=0, timeout=timeout)
            _relays[key] = ChatEndpoint(make("direct", {}, client)).start()
        return _relays[key].url


def cli_model(spec: dict, log_dir=None):
    """The run's `llm` when it names a CLI (`llm: {cli: codex, model: ..., login: chatgpt, ...}`): a chat client over
    a ChatEndpoint that answers each request with one session of that CLI Base Agent (cli_answer), for a model only
    the CLI reaches. Every key but `cli` and `timeout` is the Base Agent's setting (hermit.agent.base.cli); `web_search`
    must be given. The model is an agent that can run commands, and Codex's own read-only sandbox still reads the whole
    machine (the test answers, the keys), so the session must run in a container of its own (`isolation:
    container`), where its commands may only read (codex_sandbox read-only) unless the spec says otherwise."""
    from hermit.agent.base.interface import make
    from hermit.agent.model.llm import ChatClient
    if spec["cli"] != "codex" or spec.get("isolation") != "container":
        raise ValueError("llm.cli: the learner's model is a Codex agent that can run commands; it needs cli: codex "
                         "and isolation: container (with container: {image, network, proxy}), which shows it only "
                         "its empty workspace")
    settings = {k: v for k, v in spec.items() if k not in ("cli", "timeout")}
    settings.setdefault("codex_sandbox", "read-only")
    if log_dir is not None:
        settings.setdefault("log_dir", str(log_dir))           # each call's event stream, with the run's records
    endpoint = ChatEndpoint(make(spec["cli"], settings)).start()
    return ChatClient(endpoint.url, str(settings.get("model") or spec["cli"]), price=settings.get("price"),
                      timeout=float(spec.get("timeout", 3600)))
