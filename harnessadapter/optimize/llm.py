"""The optimizer-side language model: GEPA's reflection LM, and the model any
text optimizer in this package calls to propose changes.

ChatLM speaks the OpenAI-compatible Chat Completions protocol with the
standard library only (no SDK), keeps the key in memory, records every call
(request, response text, usage, latency) under a ledger directory, and sums
usage so a session can report what its optimizer cost. It is a plain callable
str | messages -> str, which is exactly GEPA's LanguageModel protocol.
"""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import threading
import time
import urllib.error
import urllib.request

RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}


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

    def connection(self) -> dict:
        """The same endpoint for an optimizer that drives its own client
        (SkillOpt's openai_compatible backend). Holds the key: never record it."""
        return {"base_url": self.base_url, "api_key": self._key, "model": self.model,
                "max_tokens": self.max_tokens, "timeout": self.timeout, "temperature": self.temperature}

    def __call__(self, prompt) -> str:
        messages = [{"role": "user", "content": prompt}] if isinstance(prompt, str) else list(prompt)
        body = {"model": self.model, "messages": messages, "max_tokens": self.max_tokens, **self.extra_body}
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
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
                {"model": self.model, "messages": messages, "response": text, "finish_reason": finish,
                 "usage": usage, "seconds": round(elapsed, 3), "error": error if response is None else None},
                ensure_ascii=False, indent=1) + "\n")
        if response is None:
            raise RuntimeError(f"Optimizer model call failed after {self.retries} attempts: {error}")
        if finish == "length":
            raise RuntimeError("Optimizer model output was truncated (finish_reason=length); raise max_tokens")
        return text

    def _post(self, body: dict) -> dict:
        request = urllib.request.Request(
            self.base_url + "/chat/completions", data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=self.timeout) as reply:
            return json.loads(reply.read())


class ScriptedLM:
    """A $0 stand-in: answers from a function of the prompt text. Tests use it
    to drive the real optimizers without any model call."""

    def __init__(self, respond):
        self.respond = respond
        self.prompts: list[str] = []
        self.usage = {"calls": 0, "failed": 0, "prompt_tokens": 0, "completion_tokens": 0,
                      "reasoning_tokens": 0, "cached_tokens": 0, "seconds": 0.0}

    def describe(self) -> dict:
        return {"model": "scripted"}

    def __call__(self, prompt) -> str:
        text = prompt if isinstance(prompt, str) else "\n\n".join(str(m.get("content", "")) for m in prompt)
        self.prompts.append(text)
        self.usage["calls"] += 1
        return self.respond(text)
