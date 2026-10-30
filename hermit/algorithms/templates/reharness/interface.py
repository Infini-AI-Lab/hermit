"""The ReHarness adapter's reader: the forward read of Eq. 1, Retrieval(x_t, h_t, k), as the manifest's context tool.

The harness calls `reflections(query=x_t, task=row)` once per instance, before the decision agent's first model
call (Session.context(query=..., task=...)), and appends the `text` to ADAPTER.md. The result also names what was
retrieved (`retrieved`: id, task, rank, score) so the pipeline can record it (RunOutput.extra["context_tool"]) and
the Evolver revises exactly the reflections the agent saw. An empty query, or an empty bank, retrieves nothing and
calls no endpoint (the deploy check calls it that way).
"""
from __future__ import annotations

from pathlib import Path

from .reharness_core import bank as _bank, render as _render, retrieval as _retrieval

ROOT = Path(__file__).resolve().parent


def reflections(query: str = "", task: dict | None = None) -> dict:
    index = _bank.read_index(ROOT)
    reflections_ = _bank.load_bank(ROOT)
    k = int(index["k"])
    out = {"text": "", "retrieved": [], "query": query or "", "k": k, "bank_size": len(reflections_),
           "retrieval": "none"}
    if not reflections_ or not (query or "").strip():
        return out
    if index["retrieval"] == "embedding":
        client = _retrieval.reader_client(index)
        retrieved = _retrieval.rank(query, reflections_, k, index, embed=client.embed)
        out["retrieval"] = f"embedding:{index['embedding_model']}"
    else:
        retrieved = _retrieval.rank(query, reflections_, k, index)
        out["retrieval"] = "bm25"
    out["retrieved"] = retrieved
    out["text"] = _render.ranked(retrieved, {r.id: r for r in reflections_}, index.get("memory", "reflection"))
    return out
