"""The model an optimizer Evolver calls: GEPA's reflection model, SkillOpt's
optimizer model, or whatever a plugin asks to propose changes.

ChatLM speaks the OpenAI-compatible Chat Completions protocol with the
standard library only (no SDK), keeps the key in memory, records every call
(request, response text, usage, latency) under a ledger directory, and sums
usage so a session can report what its model cost. It is a plain callable
str | messages -> str, which is exactly GEPA's LanguageModel protocol.

A plugin whose library drives its own client (SkillOpt's openai_compatible
backend) is given ChatLM.serve(): a localhost endpoint that forwards to the
same model with the same settings, key and ledger. Every optimizer in a
comparison then calls the model identically, and the key never leaves this
object.
"""
from __future__ import annotations

from contextlib import contextmanager
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import time
import urllib.error
import urllib.request

RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}


class LMText(str):
    """A completion that keeps the provider's finish_reason (GEPA's LMOutput
    contract): a proposer can reject a truncated output after one call
    instead of the caller raising and the proposer asking again."""

    def __new__(cls, value: str, *, finish_reason: str | None = None):
        instance = super().__new__(cls, value)
        instance.finish_reason = finish_reason
        return instance

    def strip(self, chars: str | None = None) -> "LMText":
        return type(self)(super().strip(chars), finish_reason=self.finish_reason)


class ChatLM:
    def __init__(self, model: str, *, base_url: str, api_key_env: str, temperature: float | None = None,
                 max_tokens: int = 32768, timeout: float = 900, retries: int = 5, reasoning_effort: str | None = None,
                 extra_body: dict | None = None, ledger: Path | None = None):
        key = os.environ.get(api_key_env, "")
        if not key:
            raise ValueError(f"Set {api_key_env} to the optimizer model's API key")
        self._key = key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.temperature = temperature
        self.max_tokens = int(max_tokens)
        self.timeout = timeout
        self.retries = max(1, int(retries))
        self.reasoning_effort = reasoning_effort
        self.extra_body = dict(extra_body or {})
        self.ledger = Path(ledger) if ledger else None   # a session sets it to its control/ when unset
        self._lock = threading.Lock()
        self._calls = 0
        self.usage = {"calls": 0, "failed": 0, "prompt_tokens": 0, "completion_tokens": 0,
                      "reasoning_tokens": 0, "cached_tokens": 0, "seconds": 0.0}

    def describe(self) -> dict:
        return {"model": self.model, "base_url": self.base_url, "api_key_env": self.api_key_env,
                "temperature": self.temperature, "max_tokens": self.max_tokens,
                "reasoning_effort": self.reasoning_effort}

    def settle(self, body: dict) -> dict:
        """This model's settings over a request body: the model, the token cap,
        and the declared temperature, reasoning effort and extra fields."""
        body = {**body, **self.extra_body, "model": self.model}
        body["max_tokens"] = min(int(body.get("max_tokens") or self.max_tokens), self.max_tokens)
        body.pop("max_completion_tokens", None)
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        return body

    def complete(self, body: dict) -> dict:
        """One recorded Chat Completions call with retries; the provider's JSON."""
        body = self.settle(body)
        with self._lock:
            self._calls += 1
            number = self._calls
        started = time.monotonic()
        response, error = None, None
        for attempt in range(self.retries):
            try:
                response = self._post(body)
                break
            except urllib.error.HTTPError as exc:
                error = f"HTTP {exc.code}: {exc.read()[:500].decode(errors='replace')}"
                if exc.code not in RETRY_STATUS:
                    break
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError,
                    http.client.HTTPException, ValueError) as exc:
                # ValueError: a 200 carrying a gateway page instead of JSON.
                error = f"{type(exc).__name__}: {exc}"
            if attempt + 1 < self.retries:
                time.sleep(min(120, 10 * 2 ** attempt))
        elapsed = time.monotonic() - started
        text, usage, finish = "", {}, None
        if response is not None:
            choice = (response.get("choices") or [{}])[0]
            text = ((choice.get("message") or {}).get("content") or "").strip()
            finish = choice.get("finish_reason")
            usage = response.get("usage") or {}
        with self._lock:
            self.usage["calls"] += 1
            self.usage["seconds"] += elapsed
            if response is None:
                self.usage["failed"] += 1
            self.usage["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
            self.usage["completion_tokens"] += int(usage.get("completion_tokens") or 0)
            details = usage.get("completion_tokens_details") or {}
            self.usage["reasoning_tokens"] += int(details.get("reasoning_tokens") or 0)
            cached = usage.get("prompt_tokens_details") or {}
            self.usage["cached_tokens"] += int(cached.get("cached_tokens") or 0)
        if self.ledger:
            self.ledger.mkdir(parents=True, exist_ok=True)
            (self.ledger / f"{number:05d}.json").write_text(json.dumps(
                {"model": self.model, "request": {k: v for k, v in body.items() if k != "messages"},
                 "messages": body.get("messages"), "response": text, "finish_reason": finish,
                 "usage": usage, "seconds": round(elapsed, 3), "error": error if response is None else None},
                ensure_ascii=False, indent=1) + "\n")
        if response is None:
            raise RuntimeError(f"Optimizer model call failed after {self.retries} attempts: {error}")
        return response

    def __call__(self, prompt) -> str:
        messages = [{"role": "user", "content": prompt}] if isinstance(prompt, str) else list(prompt)
        response = self.complete({"messages": messages})
        choice = (response.get("choices") or [{}])[0]
        finish = choice.get("finish_reason")
        if finish in ("length", "max_tokens"):
            # A truncated completion is never a proposal, even when the cut
            # leaves a balanced code fence behind. The text is in the ledger.
            return LMText("", finish_reason=finish)
        return LMText(((choice.get("message") or {}).get("content") or "").strip(), finish_reason=finish)

    def _post(self, body: dict) -> dict:
        request = urllib.request.Request(
            self.base_url + "/chat/completions", data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=self.timeout) as reply:
            return json.loads(reply.read())

    @contextmanager
    def serve(self):
        """A localhost /v1/chat/completions that forwards every request to
        this model with this object's settings, key and ledger. Yields its
        base URL; the client may use any key."""
        model = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                if not self.path.rstrip("/").endswith("/chat/completions"):
                    self.send_error(404)
                    return
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                    reply, status = json.dumps(model.complete(body)).encode(), 200
                except Exception as exc:   # reported to the client as a server error it may retry
                    reply, status = json.dumps({"error": {"message": f"{type(exc).__name__}: {exc}"}}).encode(), 502
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_address[1]}/v1"
        finally:
            server.shutdown()
            server.server_close()
