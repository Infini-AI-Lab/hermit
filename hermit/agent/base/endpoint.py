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

            def _send(self, status: int, body: bytes, headers: dict | None = None):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
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
