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

Over a CLI Base Agent (`codex`, `claude`, `pi`) the endpoint is no relay: each request is one session of it, for code that
calls a chat API and a model only the CLI reaches (cli_answer, cli_model). A session is not a model API, and what it
cannot do is refused rather than dropped: docs/guides/claude-subscription.md has the contract.
"""
from __future__ import annotations

import json
import os
import secrets
import select
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from hermit.agent.base.interface import LOGIN_FAILED, BaseAgent

HEADER_WAIT_S = 120.0
KEEPALIVE_S = 15.0
_DROP = object()                                  # no answer: close the client's connection without one
UNAPPLIED = ("max_tokens", "max_completion_tokens")   # a cap, no requirement on the answer: accepted, said not applied


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
        self.closing: list = []                         # what goes with it (a session sandbox and its proxy)

    def answer(self, request: dict, authorization: str | None, abort: threading.Event | None = None):
        """One request (its JSON body, not streamed): (status, response body, headers to pass on), or _DROP when the
        upstream gave no answer."""
        from hermit.agent.base.cli import CliAgent
        if isinstance(self.agent, CliAgent):
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
        for thing in self.closing:
            thing.close()

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
        error = error if isinstance(error, dict) else {"message": str(error)}
        # the error's own code stays (a lost sign-in's ends the run), the status beside it
        yield json.dumps({"error": {"code": status, **error, "status": status}}).encode()
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


def contract_problems(agent: BaseAgent, request: dict) -> tuple[list[str], list[str]]:
    """(what a session of `agent` cannot do of this request, the fields it accepts and does not apply). A session takes
    text messages and gives one text reply, on the model it was started with; the CLI has no setting for the rest, so a
    request that asks for it is told, not answered as if it had got it. `unapplied` in the spec names sampling fields
    the user accepts going unapplied. Fields sent as null ask for nothing."""
    problems, unapplied, accepted = [], [], set(agent.settings.get("unapplied") or ())
    messages = request.get("messages")
    if not isinstance(messages, list) or not messages or not all(isinstance(m, dict) for m in messages):
        problems.append("messages: a non-empty list of messages")
        messages = []
    for m in messages:
        if m.get("role") not in ("system", "developer", "user", "assistant"):
            problems.append(f"a message of role {m.get('role')!r}: a session takes text turns, not tool results")
        if m.get("tool_calls") or m.get("function_call"):
            problems.append("a message with tool calls: a session takes text turns")
        if isinstance(m.get("content"), list) and any(
                not isinstance(p, dict) or p.get("type") not in ("text", "input_text", "output_text")
                for p in m["content"]):
            problems.append("a message part that is not text: a session takes text")
    applied = set(agent.applied_fields()) if hasattr(agent, "applied_fields") else set()   # the session sets them
    session = agent.settings.get("model")
    if session and request.get("model") and request["model"] != session:
        problems.append(f"model {request['model']!r}: this endpoint's sessions run {session!r}")
    effort, efforts = request.get("reasoning_effort"), getattr(agent, "efforts", None)
    if effort and efforts and effort not in efforts:
        problems.append(f"reasoning_effort {effort!r}: {agent.name} takes {list(efforts)}")
    for key, value in request.items():
        if value is None or key in ("model", "messages", "reasoning_effort", "parallel_tool_calls") or key in applied:
            continue
        if key in UNAPPLIED:
            unapplied.append(key)
        elif key in ("tools", "functions"):
            if value:
                problems.append(f"{key}: a session cannot return tool calls to its caller; a role that needs tools "
                                "runs a Base Agent session with them")
        elif key == "tool_choice":
            if value not in ("none", "auto"):
                problems.append("tool_choice: a session cannot return tool calls to its caller")
        elif key == "n":
            if value != 1:
                problems.append("n: a session gives one reply")
        elif key == "response_format":
            if not (isinstance(value, dict) and value.get("type") == "text"):
                problems.append("response_format: a session gives plain text; ask for the form in the prompt")
        elif key in accepted:
            unapplied.append(key)
        else:
            problems.append(f"{key}: a {agent.name} session has no such setting (llm.unapplied: [{key}] accepts it "
                            "going unapplied)")
    return problems, unapplied


def _no_reply(agent: BaseAgent, result) -> tuple:
    """A session that gave no reply, as the client is to read it: past its clock 504, one the provider could not serve
    503 and one the plan refused 429 (the client retries), a lost sign-in 401, a request no retry can help 400, anything
    else 502. What it spent comes with it: a failed session can have used tokens (a refused answer)."""
    extra = result.extra
    status = 504 if result.timed_out else 401 if extra.get("login_failed") else 503 if extra.get("unavailable") \
        else 429 if extra.get("refused") else 400 if extra.get("rejected") else 502
    why = "timed out" if result.timed_out else f"exit {result.exit_code}"
    # the code first: a client's error text keeps only the start of the body
    error = {"code": LOGIN_FAILED} if extra.get("login_failed") else {}
    error["message"] = (f"the {agent.name} session gave no reply ({why}): "
                        f"{(extra.get('failure') or extra.get('output_tail', ''))[-600:]}")
    # a session that ran out its clock would run it out again: a client is told not to retry it (OpenAI's header)
    headers = {"x-should-retry": "false"} if result.timed_out else {}
    return status, json.dumps({"error": error, **_usage_of(result)}).encode(), headers


def _usage_of(result) -> dict:
    u = result.usage[0] if result.usage else None
    prompt_tokens, completion = (u.input_tokens, u.output_tokens) if u else (0, 0)
    return {"usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion,
                      "total_tokens": prompt_tokens + completion,
                      "prompt_tokens_details": {"cached_tokens": u.cached_input_tokens if u else 0}}}


def cli_answer(agent: BaseAgent, request: dict, abort: threading.Event | None = None):
    """A chat-completions request answered by one session of a CLI Base Agent (`codex`, `claude`, `pi`): a model reached
    through the CLI (a subscription, or the Evolver's own agent) for code that calls a chat API (an Evolver's `llm`, an
    acting pipeline's model). The system messages are the session's instructions, the rest its prompt (split_messages),
    given on stdin; the session works in an empty workspace of its own and only replies (`text_only`: a Claude Code or
    Pi session has no tools; a Codex session's commands are held by its sandbox or container), and its reply is the content, with its
    token usage. `reasoning_effort` is the session's effort; max_tokens is not applied and the reply says so
    (`unapplied`); what else a session cannot do is refused (contract_problems). A session that gave no reply is never
    an answer (_no_reply). The session sees no files: a caller that wants a session to read files runs one itself
    (`make(...).run`). Its event stream is kept under the agent's `log_dir` setting when there is one (the Trainer
    gives the run's `llm_sessions/`), and is removed after the call otherwise."""
    import shutil
    import tempfile
    from pathlib import Path
    problems, unapplied = contract_problems(agent, request)
    if problems:
        return 400, json.dumps({"error": {"message": "; ".join(problems)}}).encode(), {}
    system, prompt = split_messages(request["messages"])
    own = {"stdin": True, "text_only": True, **({"cancel": abort} if abort is not None else {})}
    sampling = {k: request[k] for k in (agent.applied_fields() if hasattr(agent, "applied_fields") else ())
                if request.get(k) is not None}
    if sampling:                                              # Pi's: sent with the request, as an API would
        own["sampling"] = sampling
    if request.get("reasoning_effort"):
        own["effort"] = str(request["reasoning_effort"])
    log_dir, scratch = agent.settings.get("log_dir"), None
    if log_dir:
        own["log"] = Path(log_dir) / f"{time.strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(4)}-{agent.name}.jsonl"
    elif not agent.settings.get("log"):                       # no record asked for: a stream removed with the call
        scratch = Path(tempfile.mkdtemp(prefix=f"hermit-chat-log-{agent.name}-"))
        own["log"] = scratch / f"agent-{agent.name}.jsonl"
    work = Path(tempfile.mkdtemp(prefix=f"hermit-chat-{agent.name}-"))
    own["writable"] = [*(agent.settings.get("writable") or ()), str(work)]   # a sandbox shows it no other way
    try:
        session, prompt = agent.configured(**own).instructed(system, prompt)
        result = session.run(prompt, workspace=work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if scratch is not None:                                # also when the call raised
            shutil.rmtree(scratch, ignore_errors=True)
        from hermit.redact import redact_file                 # what the caller keeps: no model key in it
        for kept in (own.get("log") if scratch is None else None, agent.settings.get("log"),
                     agent.settings.get("stderr_log")):
            if kept:
                redact_file(Path(os.path.realpath(kept)))     # through a link the caller made: its target
    if result.extra.get("cancelled"):
        return _DROP
    if not result.extra.get("served") or not (result.reply.strip() or result.finish_reason == "length"):
        return _no_reply(agent, result)                       # a reply cut at the output limit is one, marked so
    body = {"id": f"chatcmpl-{secrets.token_hex(6)}", "object": "chat.completion", "created": int(time.time()),
            "model": result.extra.get("served_model") or request.get("model") or agent.settings.get("model"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": result.reply},
                         "finish_reason": result.finish_reason or "stop"}],
            **_usage_of(result), **({"unapplied": unapplied} if unapplied else {})}
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


CODEX_BOUNDARY = ("cli: codex: the model is a Codex agent that can run commands; it needs isolation: sandbox (the "
                  "default: a bubblewrap sandbox of its own) or isolation: container (with container: {image, network, "
                  "proxy}), which show it only its empty workspace")


def cli_problems(spec: dict) -> list[str]:
    """What is wrong with a model spec that names a CLI, before any session. Each request is one session in a sandbox
    of its own (`isolation: sandbox`, the default; the run makes it, hermit.isolate.cli_sandbox) or in a container of
    its own (`isolation: container`, with container: {image, network, proxy}); a machine without bubblewrap runs it
    unconfined only as the run says (sandbox.enforce: best_effort, sandbox.enabled: false), which the run's records
    keep. Codex's model can run commands, and Codex's own read-only sandbox still reads the whole machine (the test
    answers, the keys); Claude Code's and Pi's have no tools at all (the Base Agent's `text_only`), and the sandbox holds
    what the CLI might do besides. A login is the CLI's own: chatgpt for codex, claude for claude; Pi has none, and
    reaches its model through a `<backend>/<model>` reference."""
    isolation = spec.get("isolation") or "sandbox"
    if spec.get("cli") == "codex":
        out = [] if isolation in ("sandbox", "container") else [CODEX_BOUNDARY]
        if spec.get("login") and spec["login"] != "chatgpt":
            out.append(f"cli: codex signs in with login: chatgpt (Codex's sign-in), not login: {spec['login']}")
        return out
    if spec.get("cli") == "pi":
        from hermit.agent.base.cli import Pi
        out = [] if isolation == "sandbox" else ["cli: pi needs isolation: sandbox (the default: a bubblewrap sandbox of "
                                                 "its own); without bubblewrap, sandbox.enforce: best_effort runs it "
                                                 "unconfined and records that"]
        if spec.get("login"):
            out.append(f"cli: pi signs in through a <backend>/<model> reference, not login: {spec['login']}")
        elif not spec.get("binding"):
            out.append("cli: pi needs a <backend>/<model> reference: without one, Pi would pick a provider itself")
        if spec.get("web_search") is True:
            out.append("cli: pi: a text session has no tools, so web_search cannot be on")
        if spec.get("effort") is not None and spec["effort"] not in Pi.efforts:    # the CLI would ignore it
            out.append(f"cli: pi takes effort {', '.join(Pi.efforts)}, not {spec['effort']!r}")
        return out
    if spec.get("cli") != "claude":
        return [f"cli must be codex, claude or pi, not {spec.get('cli')!r}"]
    out = []
    if not spec.get("binding") and spec.get("login") != "claude":
        out.append("cli: claude needs login: claude (your subscription) or a <backend>/<model> reference (that "
                   "backend's key): without one, whatever the CLI finds in the environment would pay")
    if spec.get("binding") and spec.get("login"):
        out.append("cli: claude takes login or a <backend>/<model> reference, not both")
    if spec.get("login") and not spec.get("model"):
        out.append("with login: claude, model is required: Claude Code's default differs by sign-in")
    if isolation not in ("sandbox", "container"):
        out.append("cli: claude needs isolation: sandbox (the default: a bubblewrap sandbox of its own) or container "
                   "(a Docker container of its own, with container: {image, network, proxy}); without bubblewrap, "
                   "sandbox.enforce: best_effort runs it unconfined and records that")
    elif isolation == "container":
        from hermit.agent.base import container
        try:
            container.settings(spec, "claude")
        except ValueError as e:
            out.append(f"cli: claude: {e}")
    if spec.get("web_search") is True:
        out.append("cli: claude: a text session has no tools, so web_search cannot be on")
    from hermit.agent.base.cli import Claude
    if spec.get("effort") is not None and spec["effort"] not in Claude.efforts:   # the CLI would ignore it
        out.append(f"cli: claude takes effort {', '.join(Claude.efforts)}, not {spec['effort']!r}")
    return out


def cli_endpoint(spec: dict, log_dir=None, *, sandbox=None) -> ChatEndpoint:
    """A chat endpoint over the CLI a spec names (`{cli: codex | claude | pi, model: ..., login: ..., ...}`), started: each
    request is one session of that Base Agent (cli_answer). Every key but `cli` and `timeout` is the Base Agent's
    setting (hermit.agent.base.cli). With `isolation: sandbox` (the default) every session runs in `sandbox`, the one
    the run made for them (hermit.isolate.cli_sandbox: its empty workspace and home, its model's or its login's
    hosts, nothing of the run); with `isolation: container` each in a container of its own. What the caller hands
    over to close with the endpoint goes in its `closing`. Close it after use."""
    from hermit.agent.base.interface import make
    problems = cli_problems(spec)
    if problems:
        raise ValueError("; ".join(problems))
    settings = {k: v for k, v in spec.items() if k not in ("cli", "timeout")}
    isolation = settings["isolation"] = spec.get("isolation") or "sandbox"   # the Base Agent's own default is none
    if isolation == "sandbox":
        if sandbox is None:
            raise ValueError("llm.cli runs in a sandbox of its own, which the Trainer makes (settings.sandbox)")
        settings["sandbox"] = sandbox
        if spec["cli"] == "codex":
            settings.setdefault("codex_sandbox", "danger-full-access")   # the bubblewrap sandbox is the boundary
    if spec["cli"] == "codex":
        settings.setdefault("codex_sandbox", "read-only")
    else:
        settings.setdefault("web_search", False)
    if log_dir is not None:
        settings.setdefault("log_dir", str(log_dir))           # each call's event stream, with the run's records
    return ChatEndpoint(make(spec["cli"], settings)).start()


def endpoint_spec(endpoint: ChatEndpoint, spec: dict) -> dict:
    """The plain model spec of an endpoint over a CLI, for code that takes a model spec or a base URL (an acting
    pipeline): it needs no key, and its URL holds the endpoint's token (never written to a run's records)."""
    settings = endpoint.agent.settings
    model = str(settings.get("model") or spec["cli"])
    timeout = float(spec.get("timeout", 3600))
    if settings.get("login"):
        # On a login the session waits for the plan inside the request (CliAgent.run), as long as plan_max_wait_s: the
        # client waits as long, or a hold would make it hang up and try again until it gives up, a failed call instead
        # of one served after the hold. None: no limit (30 days, as a socket takes a number).
        from hermit.agent.base.login import MAX_WAIT_S
        wait = settings.get("plan_max_wait_s", MAX_WAIT_S)
        timeout += 30 * 86400 if wait is None else float(wait)
    return {"base_url": endpoint.url, "model": model, "api_key_env": None, "key_required": False,
            "price": spec.get("price"), "timeout": timeout}


def cli_model(spec: dict, log_dir=None, sandbox=None):
    """The run's `llm` when it names a CLI (`llm: {cli: codex | claude | pi, model: ..., login: chatgpt | claude, ...}`): a
    chat client over a ChatEndpoint over that CLI (cli_endpoint), for a model only the CLI reaches; its sessions run
    in `sandbox`, which the Trainer makes (isolation: sandbox, the default), or each in a container of its own.
    `close()` closes the endpoint."""
    from hermit.agent.model.llm import ChatClient
    endpoint = cli_endpoint(spec, log_dir, sandbox=sandbox)
    try:
        client = ChatClient.from_spec(endpoint_spec(endpoint, spec))
    except BaseException:
        endpoint.close()                                  # a client that could not be made: nothing is left serving
        raise
    client.close = endpoint.close
    return client
