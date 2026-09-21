"""Reading-assistant tools: retrieve, cut, one reader call, return the report.

Both tools end in the same place: the full text of a few documents, capped per
document, goes to the local reader with the prompt in subagent/report.md; the
reader's text comes back unchanged together with the docids it saw and the
reader's token usage.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _config() -> dict:
    return json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


def _prompt() -> str:
    return (ROOT / "subagent" / "report.md").read_text(encoding="utf-8")


def _need(resources, name):
    if not resources or name not in resources:
        raise RuntimeError(f"resource {name!r} is not bound")
    return resources[name]


def _read(goal: str, query: str, docids: list, resources) -> dict:
    cfg = _config()
    documents = _need(resources, "documents")
    reader = _need(resources, "small_model")
    blocks, seen, chars = [], [], 0
    for docid in docids:
        text = documents.get(str(docid))
        if text is None:
            continue
        text = text[: int(cfg["doc_char_cap"])]
        chars += len(text)
        seen.append(str(docid))
        blocks.append(f"[docid {docid}]\n{text}")
    if not blocks:
        return {"retrieved_docids": [], "report": "NO_RELEVANT_INFO (no documents available)",
                "reader_usage": {"input_tokens": 0, "output_tokens": 0}, "docs_read_chars": 0}
    prompt = _prompt().format(goal=goal or query, query=query, documents="\n\n---\n\n".join(blocks))
    result = reader.chat([{"role": "user", "content": prompt}],
                         max_tokens=int(cfg["reader_max_tokens"]), temperature=0.0)
    return {"retrieved_docids": seen, "report": result["text"] or "NO_RELEVANT_INFO",
            "reader_usage": result.get("usage", {}), "docs_read_chars": chars}


def deep_search(goal: str, query: str, _resources=None) -> dict:
    cfg = _config()
    hits = _need(_resources, "search").search(query, int(cfg["k"]))
    return _read(goal, query, [h["docid"] for h in hits], _resources)


def read_for(goal: str, docids: list, _resources=None) -> dict:
    cfg = _config()
    return _read(goal, "", list(docids)[: int(cfg["k"])], _resources)
