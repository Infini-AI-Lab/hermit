"""The reflection bank: the harness state h_t = {R_i}_{i<t} as files.

    index.json                 {"k": 5, "retrieval": "embedding" | "bm25", "embedding_model": ..., "dim": ...}
    reflections/<id>.json      one reflection per past instance

A reflection file:

    {"id": "<memory id>", "task": "<task id>", "step": <the step that wrote it>, "created": "<time>",
     "query": "<the instance's input x_i, what retrieval compares>",
     "reflection": "<R_i, the text the decision agent reads>",
     "embedding": {"model": "...", "dim": 4096, "dtype": "f16", "data": "<base64>"} | null,
     "revisions": [{"step": 9, "by": "<task id>", "rank": 2, "indel_distance": 123, "chars_before": 800,
                    "chars_after": 950}, ...]}

Embeddings are unit vectors stored as base64 IEEE half floats (8 KB of floats, about 11 KB of base64, for 4096
dimensions), so a bank of a few
hundred reflections stays small in the adapter's git history.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import struct

INDEX = "index.json"
REFLECTIONS = "reflections"
DEFAULT_INDEX = {"k": 5, "retrieval": "bm25", "embedding_model": None, "dim": None, "memory": "reflection"}
RETRIEVALS = ("embedding", "bm25")
MEMORIES = ("reflection", "trajectory")      # what an entry is: a reflection, or a stored trajectory (Top-K Raw)


@dataclass
class Reflection:
    id: str
    task: str
    step: int
    query: str
    reflection: str
    embedding: list[float] | None = None
    model: str | None = None
    created: str = ""
    revisions: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"id": self.id, "task": self.task, "step": self.step, "created": self.created, "query": self.query,
                "reflection": self.reflection,
                "embedding": ({"model": self.model, "dim": len(self.embedding), "dtype": "f16",
                               "data": encode_embedding(self.embedding)} if self.embedding is not None else None),
                "revisions": list(self.revisions)}

    @classmethod
    def from_dict(cls, data: dict) -> "Reflection":
        emb = data.get("embedding")
        vector, model = None, None
        if isinstance(emb, dict) and emb.get("data"):
            vector, model = decode_embedding(emb["data"]), emb.get("model")
        return cls(id=str(data["id"]), task=str(data.get("task", data["id"])), step=int(data.get("step", 0)),
                   query=str(data.get("query", "")), reflection=str(data.get("reflection", "")), embedding=vector,
                   model=model, created=str(data.get("created", "")), revisions=list(data.get("revisions") or []))


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_id(text: str) -> str:
    """A filename-safe memory id: the id itself when it is short and plain (a task id, normally); otherwise a prefix
    plus a digest of the whole id, so two ids never share one file."""
    text = str(text)
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", text)
    if slug == text and 0 < len(slug) <= 60:
        return slug
    return f"{slug[:48].strip('_') or 'memory'}-{hashlib.sha1(text.encode()).hexdigest()[:10]}"


def normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector))
    return [x / norm for x in vector] if norm > 0 else list(vector)


def encode_embedding(vector: list[float]) -> str:
    return base64.b64encode(struct.pack(f"<{len(vector)}e", *vector)).decode("ascii")


def decode_embedding(data: str) -> list[float]:
    raw = base64.b64decode(data)
    return list(struct.unpack(f"<{len(raw) // 2}e", raw))


def read_index(root: Path) -> dict:
    path = Path(root) / INDEX
    index = dict(DEFAULT_INDEX)
    if path.is_file():
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError(f"{INDEX} must be a JSON object")
        index.update(data)
    if index.get("retrieval") not in RETRIEVALS:
        raise ValueError(f"{INDEX}: retrieval must be one of {list(RETRIEVALS)}, not {index.get('retrieval')!r}")
    if not isinstance(index.get("k"), int) or index["k"] < 1:
        raise ValueError(f"{INDEX}: k must be a positive integer")
    if index["retrieval"] == "embedding" and not index.get("embedding_model"):
        raise ValueError(f"{INDEX}: embedding retrieval needs embedding_model")
    if index.get("memory", "reflection") not in MEMORIES:
        raise ValueError(f"{INDEX}: memory must be one of {list(MEMORIES)}, not {index.get('memory')!r}")
    return index


def write_index(root: Path, index: dict) -> None:
    (Path(root) / INDEX).write_text(json.dumps(index, indent=1) + "\n")


def reflection_path(root: Path, memory_id: str) -> Path:
    return Path(root) / REFLECTIONS / f"{safe_id(memory_id)}.json"


def load_bank(root: Path) -> list[Reflection]:
    """Every reflection of the adapter, oldest first (by step, then id)."""
    directory = Path(root) / REFLECTIONS
    if not directory.is_dir():
        return []
    out = []
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except ValueError as exc:
            raise ValueError(f"{REFLECTIONS}/{path.name}: not valid JSON: {exc}") from None
        if not isinstance(data, dict) or "id" not in data:
            raise ValueError(f"{path.name}: a reflection file is a JSON object with an id")
        out.append(Reflection.from_dict(data))
    out.sort(key=lambda r: (r.step, r.id))
    return out


def save_reflection(root: Path, reflection: Reflection) -> Path:
    path = reflection_path(root, reflection.id)
    if path.is_file():
        held = json.loads(path.read_text()).get("id")
        if held != reflection.id:
            raise ValueError(f"{REFLECTIONS}/{path.name} holds memory {held!r}, not {reflection.id!r}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(reflection.to_dict(), ensure_ascii=False, indent=1) + "\n")
    return path


def validate(root: Path) -> list[dict]:
    """The template check: index.json and every reflection parse and agree; issues as {path, problem}."""
    issues = []
    try:
        index = read_index(root)
    except (ValueError, OSError) as exc:
        return [{"path": INDEX, "problem": str(exc)}]
    directory = Path(root) / REFLECTIONS
    for path in sorted(directory.glob("*")) if directory.is_dir() else []:
        rel = f"{REFLECTIONS}/{path.name}"
        if path.is_dir() or path.suffix != ".json":
            issues.append({"path": rel, "problem": "only reflection JSON files belong under reflections/"})
            continue
        try:
            data = json.loads(path.read_text())
            reflection = Reflection.from_dict(data)
        except (ValueError, KeyError, TypeError, struct.error) as exc:
            issues.append({"path": rel, "problem": f"does not parse as a reflection: {exc}"})
            continue
        if path.name != f"{safe_id(reflection.id)}.json":
            issues.append({"path": rel, "problem": f"file name does not match the memory id {reflection.id!r}"})
        if not reflection.reflection.strip():
            issues.append({"path": rel, "problem": "empty reflection"})
        if index["retrieval"] == "embedding":
            if reflection.embedding is None:
                issues.append({"path": rel, "problem": "no embedding, but index.json asks for embedding retrieval"})
            elif reflection.model and reflection.model != index["embedding_model"]:
                issues.append({"path": rel, "problem": f"embedded with {reflection.model!r}, the index names "
                                                       f"{index['embedding_model']!r}"})
            elif index.get("dim") and len(reflection.embedding) != int(index["dim"]):
                issues.append({"path": rel, "problem": f"{len(reflection.embedding)} dimensions, the index says "
                                                       f"{index['dim']}"})
    return issues
