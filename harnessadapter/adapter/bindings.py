"""Generic resource bindings an adapter can receive through `_resources`.

A binding is named on the command line as NAME=SPEC and resolved here:

    search=http://127.0.0.1:8012          -> HttpSearch      .search(query, k) -> [{docid, score}]
    sql=http://127.0.0.1:8013             -> HttpSql         .query(q) -> {columns, rows, truncated} | {error}
    documents=http://127.0.0.1:8012       -> HttpDocuments   .get(docid) -> text | None
    small_model=http://127.0.0.1:8011/v1|Qwen/Qwen3.5-4B
                                          -> ChatModel       .chat(messages, max_tokens, temperature) -> {text, usage}

The transports are plain HTTP with the standard library only, so bindings work
inside the runtime MCP server, the in-process host path, and the evolution
broker alike. Everything after the first `|` in a SPEC is the model name for
chat bindings; other names are passed through as URLs.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request


def _get(url: str, timeout: float = 60.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _post(url: str, payload: dict, timeout: float = 600.0) -> dict:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json",
                                                              "Authorization": "Bearer EMPTY"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


class HttpSearch:
    """Ranked retrieval over a corpus service exposing GET /search?q=&k=."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def search(self, query: str, k: int = 5) -> list[dict]:
        query = (query or "").strip()
        if not query:
            return []
        url = f"{self.base_url}/search?{urllib.parse.urlencode({'q': query, 'k': int(k)})}"
        return list(_get(url).get("results", []))

    def describe(self) -> str:
        return "ranked keyword search over the bound corpus: search(query, k) -> [{docid, score}]"


class HttpDocuments:
    """Full-document fetch over a corpus service exposing GET /document?docid=."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def get(self, docid: str) -> str | None:
        url = f"{self.base_url}/document?{urllib.parse.urlencode({'docid': str(docid)})}"
        try:
            return _get(url).get("text")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise

    def describe(self) -> str:
        return "full text of a corpus document: get(docid) -> str | None"


class ChatModel:
    """Chat completions on an OpenAI-compatible endpoint (the local reader model)."""

    def __init__(self, base_url: str, model: str, retries: int = 4):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.retries = retries

    def chat(self, messages: list[dict], max_tokens: int = 2000, temperature: float = 0.0,
             extra: dict | None = None) -> dict:
        payload = {"model": self.model, "messages": messages, "max_tokens": int(max_tokens),
                   "temperature": float(temperature),
                   "chat_template_kwargs": {"enable_thinking": False}}
        if extra:
            payload.update(extra)
        delay, last = 2.0, None
        for _ in range(self.retries):
            try:
                response = _post(f"{self.base_url}/chat/completions", payload)
                choice = response["choices"][0]
                usage = response.get("usage") or {}
                return {"text": choice["message"].get("content") or "",
                        "finish_reason": choice.get("finish_reason"),
                        "usage": {"input_tokens": usage.get("prompt_tokens", 0),
                                  "output_tokens": usage.get("completion_tokens", 0)}}
            except (urllib.error.URLError, TimeoutError, KeyError, json.JSONDecodeError) as exc:
                last = exc
                time.sleep(delay)
                delay = min(delay * 2, 30.0)
        raise RuntimeError(f"small_model unavailable after {self.retries} attempts: {last!r}")

    def describe(self) -> str:
        return f"chat completions on {self.model}: chat(messages, max_tokens, temperature) -> {{text, usage}}"


class HttpSql:
    """Read-only SQL over an environment service exposing POST /sql {"q": ...}.

    The service owns the read-only guarantees (driver mode, verb screening,
    row and character caps) and logs every statement; this client only
    transports the statement and the reply."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def query(self, q: str) -> dict:
        return _post(f"{self.base_url}/sql", {"q": str(q)}, timeout=60.0)

    def describe(self) -> str:
        return "read-only SQL against the environment database: query(q) -> {columns, rows, truncated} or {error}"


def resolve_binding(name: str, spec: str):
    spec = spec.strip()
    if name == "small_model":
        base_url, _, model = spec.partition("|")
        if not model:
            raise ValueError("small_model binding needs URL|model-name")
        return ChatModel(base_url, model)
    if name == "search":
        return HttpSearch(spec)
    if name == "documents":
        return HttpDocuments(spec)
    if name == "sql":
        return HttpSql(spec)
    raise ValueError(f"Unknown binding name: {name}")


def parse_bindings(items) -> dict:
    """Turn ["search=http://...", ...] into {name: binding object}."""
    out = {}
    for item in items or []:
        name, sep, spec = str(item).partition("=")
        if not sep or not name.strip():
            raise ValueError(f"Binding must be NAME=SPEC: {item}")
        out[name.strip()] = resolve_binding(name.strip(), spec)
    return out


def describe_bindings(resources: dict) -> dict:
    return {name: obj.describe() for name, obj in resources.items() if hasattr(obj, "describe")}


def call_tool_with_bindings(adapter_root, specs, name: str, arguments: dict) -> dict:
    """Load the adapter at `adapter_root` fresh, bind `specs`, and call one tool synchronously."""
    return call_tool_with_resources(adapter_root, parse_bindings(specs), name, arguments)


def call_tool_with_resources(adapter_root, resources: dict, name: str, arguments: dict) -> dict:
    """Load the adapter at `adapter_root` fresh with already-built resource
    objects (real bindings, or the try_tool proxies) and call one tool."""
    import asyncio
    from pathlib import Path
    from harnessadapter.adapter.loader import Adapter

    adapter = Adapter(Path(adapter_root), resources=dict(resources))
    if name not in adapter.tools:
        raise ValueError(f"Unknown tool {name!r}; declared: {sorted(adapter.tools)}")
    coroutine = adapter.call(name, dict(arguments or {}))
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    # Called from inside an event loop (the evolution broker): run the tool on
    # its own loop in a worker thread instead of nesting asyncio.run().
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coroutine).result()
