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

from harnessadapter.evolver.base import Usage


class ChatClient:
    @classmethod
    def from_spec(cls, spec: dict, **kwargs) -> "ChatClient":
        """A client from a model spec: a resolved backend reference (see harnessadapter.backends) or the older
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
                 timeout: float = 600, max_retries: int = 4, extra_body: dict | None = None):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.extra_body = dict(extra_body or {})     # sent with every request, e.g. {"reasoning_effort": "medium"}
        self.api_key_env = api_key_env
        self.price = price
        self.timeout = timeout
        self.max_retries = max_retries
        self._lock = threading.Lock()
        self.total = Usage(model=model, cost_usd=0.0 if price else None)

    def cost(self, prompt: int, cached: int, completion: int) -> float | None:
        if not self.price:
            return None
        p = self.price
        return ((prompt - cached) * p.get("input", 0) + cached * p.get("cached_input", p.get("input", 0))
                + completion * p.get("output", 0)) / 1e6

    def chat(self, messages: list[dict], **kwargs) -> tuple[dict, Usage]:
        """POST /chat/completions; returns the response message and this call's usage."""
        body = {"model": self.model, "messages": messages, **self.extra_body, **kwargs}
        headers = {"Content-Type": "application/json"}
        if self.api_key_env:
            key = os.environ.get(self.api_key_env)
            if not key:
                raise RuntimeError(f"Environment variable {self.api_key_env} is not set")
            headers["Authorization"] = f"Bearer {key}"
        data = json.dumps(body).encode()
        delay = 2.0
        for attempt in range(self.max_retries + 1):
            request = urllib.request.Request(self.base_url + "/chat/completions", data=data, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    payload = json.loads(response.read())
                break
            except urllib.error.HTTPError as exc:
                retriable = exc.code in (408, 409, 429, 500, 502, 503, 504)
                detail = exc.read().decode(errors="replace")[:500]
                if not retriable or attempt == self.max_retries:
                    raise RuntimeError(f"chat/completions HTTP {exc.code}: {detail}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                if attempt == self.max_retries:
                    raise RuntimeError(f"chat/completions failed: {exc}") from None
            time.sleep(delay)
            delay *= 2
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
        return message, usage
