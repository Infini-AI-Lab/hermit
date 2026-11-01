"""A small OpenAI-compatible chat client with usage accounting, used by the `llm` run tool and by examples.

Prices are given per million tokens: {input, cached_input, output}. Without prices, cost is None (unknown), never 0.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request

from hermit.agent.model.usage import Usage


class ChatHTTPError(RuntimeError):
    """An error status from the endpoint, with its whole body and headers (a relay passes them on as they are)."""

    def __init__(self, code: int, body: bytes, headers: dict):
        self.code, self.body, self.headers = code, body, headers
        super().__init__(f"chat/completions HTTP {code}: {body.decode(errors='replace')[:500]}")


class ChatAborted(RuntimeError):
    """The caller gave the call up (`abort`) while it was in flight; its connection was closed."""


def _opener(sockets: list, abort):
    """A urllib opener (proxies from the environment as usual) that keeps a duplicate of every TCP socket its
    connections open, from the moment it is made, so that a call can be given up by shutting it: shutting a duplicate
    ends the connection itself, during a proxy's CONNECT, the TLS handshake (which takes the socket over) or the body
    (which urllib reads from the response, having cleared the connection's socket). The caller closes the
    duplicates. A call given up before its connection is made sends nothing."""
    import http.client
    import socket

    def tracked(cls):
        class Tracked(cls):
            def connect(self):
                if abort.is_set():
                    raise ConnectionAbortedError("the call was given up")
                create = self._create_connection

                def recorded(*args, **kwargs):
                    sock = create(*args, **kwargs)
                    sockets.append(sock.dup())
                    if abort.is_set():                  # given up while connecting: the watcher may have missed it
                        sock.close()
                        raise ConnectionAbortedError("the call was given up")
                    return sock
                self._create_connection = recorded
                super().connect()
        return Tracked

    class Plain(urllib.request.HTTPHandler):
        def http_open(self, req):
            return self.do_open(tracked(http.client.HTTPConnection), req)

    class Secure(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(tracked(http.client.HTTPSConnection), req, context=self._context)
    return urllib.request.build_opener(Plain, Secure)


class ChatClient:
    @classmethod
    def from_spec(cls, spec: dict, **kwargs) -> "ChatClient":
        """A client from a model spec: a resolved backend reference (see hermit.agent.model.backends) or the older
        {base_url, model, api_key_env, price, extra_body, timeout} mapping. A key that is optional for the backend
        and not set sends no Authorization header."""
        if not isinstance(spec, dict) or not spec.get("base_url") or not spec.get("model"):
            raise ValueError("a model spec needs base_url and model (a `<backend>/<model>` reference resolves to one)")
        key_env = spec.get("api_key_env")
        if key_env and not spec.get("key_required", True) and not os.environ.get(key_env):
            key_env = None
        kwargs.setdefault("timeout", float(spec.get("timeout", 600)))
        return cls(spec["base_url"], spec["model"], api_key_env=key_env, price=spec.get("price"),
                   extra_body=spec.get("extra_body"), **kwargs)

    def __init__(self, base_url: str, model: str, *, api_key_env: str | None = None, price: dict | None = None,
                 timeout: float | None = 600, max_retries: int = 4, extra_body: dict | None = None,
                 retry_wait: float = 2.0, retry_wait_max: float | None = None):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.extra_body = dict(extra_body or {})     # sent with every request, e.g. {"reasoning_effort": "medium"}
        self.api_key_env = api_key_env
        self.price = price
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_wait, self.retry_wait_max = retry_wait, retry_wait_max   # the first wait, doubled up to the cap
        self._lock = threading.Lock()
        self.total = Usage(model=model, cost_usd=0.0 if price else None)

    def cost(self, prompt: int, cached: int, completion: int) -> float | None:
        if not self.price:
            return None
        p = self.price
        return ((prompt - cached) * p.get("input", 0) + cached * p.get("cached_input", p.get("input", 0))
                + completion * p.get("output", 0)) / 1e6

    def chat(self, messages: list[dict], **kwargs) -> tuple[dict, Usage]:
        """POST /chat/completions; returns the response message and this call's usage. Three keyword arguments are not
        request fields: `extra_headers` (sent as they are; an Authorization among them replaces the key),
        `return_response` (the whole response body is put in the message as `_response`) and `abort` (a
        threading.Event: once set, the call in flight is given up, its connection closed, and ChatAborted raised).
        An error status the endpoint gave last raises ChatHTTPError; no answer at all, RuntimeError."""
        extra_headers = dict(kwargs.pop("extra_headers", None) or {})
        return_response = bool(kwargs.pop("return_response", False))
        abort = kwargs.pop("abort", None)
        body = {"model": self.model, "messages": messages, **self.extra_body, **kwargs}
        headers = {"Content-Type": "application/json", **extra_headers}
        if self.api_key_env and not any(k.lower() == "authorization" for k in extra_headers):
            key = os.environ.get(self.api_key_env)
            if not key:
                raise RuntimeError(f"Environment variable {self.api_key_env} is not set")
            headers["Authorization"] = f"Bearer {key}"
        data = json.dumps(body).encode()
        delay = self.retry_wait
        import http.client
        for attempt in range(self.max_retries + 1):
            request = urllib.request.Request(self.base_url + "/chat/completions", data=data, headers=headers)
            sockets: list = []
            watcher = _abort_watcher(abort, sockets) if abort is not None else None
            try:
                opener = _opener(sockets, abort) if abort is not None else urllib.request.build_opener()
                with opener.open(request, timeout=self.timeout) as response:
                    raw = response.read()
                if abort is not None and abort.is_set():
                    raise ChatAborted("the call was given up")
                payload = json.loads(raw)
                break
            except urllib.error.HTTPError as exc:
                if abort is not None and abort.is_set():
                    raise ChatAborted("the call was given up") from None
                retriable = exc.code in (408, 409, 429, 500, 502, 503, 504)
                body = exc.read()
                if not retriable or attempt == self.max_retries:
                    raise ChatHTTPError(exc.code, body, dict(exc.headers or {})) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException, OSError) as exc:
                if abort is not None and abort.is_set():
                    raise ChatAborted("the call was given up") from None
                if attempt == self.max_retries:
                    raise RuntimeError(f"chat/completions failed: {type(exc).__name__}: {exc}") from None
            finally:
                if watcher is not None:
                    watcher.set()
                for sock in sockets:
                    sock.close()
            time.sleep(delay)
            delay = delay * 2 if self.retry_wait_max is None else min(delay * 2, self.retry_wait_max)
        u = payload.get("usage") or {}
        prompt = int(u.get("prompt_tokens", 0))
        cached = int((u.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0)
        completion = int(u.get("completion_tokens", 0))
        usage = Usage(self.model, prompt, cached, completion, 1, self.cost(prompt, cached, completion))
        with self._lock:
            t = self.total
            t.input_tokens += prompt; t.cached_input_tokens += cached; t.output_tokens += completion; t.calls += 1
            if t.cost_usd is not None and usage.cost_usd is not None:
                t.cost_usd += usage.cost_usd
        choice = (payload.get("choices") or [{}])[0]
        message = dict(choice.get("message") or {})
        message["finish_reason"] = choice.get("finish_reason")
        if return_response:
            message["_response"] = payload
        return message, usage


def _abort_watcher(abort, sockets: list):
    """A thread that shuts the call's sockets once `abort` is set (a read blocked on one returns); set the returned
    event when the call ends."""
    import socket
    done = threading.Event()

    def watch():
        while not done.is_set():
            if abort.wait(0.2):
                for sock in list(sockets):
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                return
    threading.Thread(target=watch, daemon=True).start()
    return done
