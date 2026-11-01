"""Retrieval(x_t, h_t, k) of ReHarness (Eq. 1): the top-k reflections for an instance's input, by query-to-query
comparison, the instance's input against each reflection's source input.

Embedding retrieval (the paper: Qwen3-Embedding-8B, query-to-query; the cosine metric, a dot product of unit vectors,
is this integration's choice, the paper names none) calls an OpenAI-compatible `/embeddings` endpoint with the model
index.json names; BM25 over the same texts is the fallback that needs no endpoint. On the acting side the
endpoint comes from the environment: REHARNESS_EMBED_URL (the base URL, `/embeddings` is appended) and, when a key
must be sent from there, REHARNESS_EMBED_API_KEY_ENV naming the variable that holds it. Inside a run the adapter's
tools run in a sandbox that reaches 127.0.0.1 endpoints only, so the URL is normally a loopback relay in front of the
provider (the examples' wire meter, which also adds the key).
"""
from __future__ import annotations

import json
import math
import os
import re
import time
import urllib.error
import urllib.request

from .bank import Reflection, normalize

READER_URL_ENV = "REHARNESS_EMBED_URL"
READER_KEY_ENV = "REHARNESS_EMBED_API_KEY_ENV"
_TOKEN = re.compile(r"[a-z0-9]+")


class EmbeddingClient:
    """POST /embeddings on an OpenAI-compatible endpoint; unit vectors back, with the usage the server reports."""

    def __init__(self, base_url: str, model: str, *, api_key_env: str | None = None, timeout: float = 120,
                 max_retries: int = 5, batch: int = 32):
        self.base_url, self.model = base_url.rstrip("/"), model
        self.api_key_env, self.timeout, self.max_retries, self.batch = api_key_env, timeout, max_retries, batch

    def embed(self, texts: list[str]) -> tuple[list[list[float]], dict]:
        vectors, usage = [], {"prompt_tokens": 0, "calls": 0}
        for start in range(0, len(texts), self.batch):
            chunk = texts[start:start + self.batch]
            payload = self._post({"model": self.model, "input": chunk})
            data = sorted(payload.get("data") or [], key=lambda d: d.get("index", 0))
            if len(data) != len(chunk):
                raise RuntimeError(f"embeddings: asked for {len(chunk)} vectors, got {len(data)}")
            vectors += [normalize([float(x) for x in d["embedding"]]) for d in data]
            usage["prompt_tokens"] += int((payload.get("usage") or {}).get("prompt_tokens") or 0)
            usage["calls"] += 1
        return vectors, usage

    def _post(self, body: dict) -> dict:
        headers = {"Content-Type": "application/json"}
        key = os.environ.get(self.api_key_env) if self.api_key_env else None
        if key:
            headers["Authorization"] = f"Bearer {key}"
        data = json.dumps(body).encode()
        delay = 2.0
        for attempt in range(self.max_retries + 1):
            request = urllib.request.Request(self.base_url + "/embeddings", data=data, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read())
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode(errors="replace")[:300]
                if exc.code not in (408, 409, 429, 500, 502, 503, 504) or attempt == self.max_retries:
                    raise RuntimeError(f"embeddings HTTP {exc.code} from {self.base_url}: {detail}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                if attempt == self.max_retries:
                    raise RuntimeError(f"embeddings failed at {self.base_url}: {exc}") from None
            time.sleep(delay)
            delay = min(delay * 2, 30)
        raise RuntimeError("unreachable")


def reader_client(index: dict) -> EmbeddingClient:
    """The acting side's client, from the environment and the index's model."""
    url = os.environ.get(READER_URL_ENV)
    if not url:
        raise RuntimeError(f"index.json asks for embedding retrieval, but {READER_URL_ENV} is not set: the reader "
                           "needs the embedding endpoint (a 127.0.0.1 relay inside a run's sandbox)")
    return EmbeddingClient(url, str(index["embedding_model"]), api_key_env=os.environ.get(READER_KEY_ENV) or None)


def tokens(text: str) -> list[str]:
    return _TOKEN.findall((text or "").lower())


def bm25_scores(query: str, documents: list[str], k1: float = 1.5, b: float = 0.75) -> list[float]:
    """Okapi BM25 of one query against each document."""
    docs = [tokens(d) for d in documents]
    n = len(docs)
    if n == 0:
        return []
    avg = sum(len(d) for d in docs) / n or 1.0
    df: dict[str, int] = {}
    for d in docs:
        for term in set(d):
            df[term] = df.get(term, 0) + 1
    scores = []
    q = tokens(query)
    for d in docs:
        counts: dict[str, int] = {}
        for term in d:
            counts[term] = counts.get(term, 0) + 1
        score = 0.0
        for term in q:
            if term not in counts:
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            tf = counts[term]
            score += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * len(d) / avg))
        scores.append(score)
    return scores


def dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def rank(query: str, bank: list[Reflection], k: int, index: dict, *, query_vector: list[float] | None = None,
         embed=None) -> list[dict]:
    """The top-k of the bank for `query`: [{id, task, rank, score}], best first. Embedding retrieval needs the
    query's vector (`query_vector`) or a function `embed(texts) -> (vectors, usage)`; later reflections win ties."""
    if not bank or k < 1 or not (query or "").strip():
        return []
    if index.get("retrieval") == "embedding":
        if query_vector is None:
            if embed is None:
                raise RuntimeError("embedding retrieval needs the query's vector or an embedding function")
            query_vector = embed([query])[0][0]
        scored = [(dot(query_vector, r.embedding) if r.embedding is not None else float("-inf"), r) for r in bank]
    else:
        scored = list(zip(bm25_scores(query, [r.query for r in bank]), bank))
    order = sorted(range(len(scored)), key=lambda i: (-scored[i][0], -scored[i][1].step, scored[i][1].id))
    return [{"id": scored[i][1].id, "task": scored[i][1].task, "rank": n + 1, "score": round(scored[i][0], 6)}
            for n, i in enumerate(order[:k])]
