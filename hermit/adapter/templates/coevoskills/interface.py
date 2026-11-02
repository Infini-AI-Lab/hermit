"""Fixed adapter reader. It reads manifests; it never executes learned code."""
from __future__ import annotations

from collections import Counter
import math
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent
K = 3


def words(text):
    return re.findall(r"[a-z0-9]+", text.lower())


def skills(query: str = "", task: dict | None = None) -> dict:
    manifests = sorted((ROOT / "skills").glob("*/*/SKILL.md"))
    out = {"text": "", "retrieved": [], "bank_size": len(manifests), "k": K}
    if not query.strip() or not manifests:
        return out
    q = Counter(words(query))
    candidates = []
    seen = set()
    for manifest in manifests:
        text = manifest.read_text(encoding="utf-8")
        if text in seen:
            continue
        seen.add(text)
        terms = Counter(words(text))
        score = sum(weight * math.log1p(terms[token]) for token, weight in q.items())
        candidates.append((score, str(manifest.relative_to(ROOT)), text))
    selected = sorted(candidates, key=lambda x: (-x[0], x[1]))[:K]
    lines = [
        "Additional reusable Skills are available below. Read the relevant SKILL.md "
        "and use its bundled scripts or references. Paths below are relative to this "
        "adapter folder, as the deployment notes specify. Supply the session corpus "
        "and output paths at runtime; "
        "/root paths in an evolution example are fixture paths. Preserve the Skill "
        "sources; use a separate runner when adapting parameters. Skill import paths "
        "are relative to this adapter folder; use those deployed paths. Return the answer "
        "inside the task's existing <answer>...</answer> format."
    ]
    for score, path, text in selected:
        out["retrieved"].append({"path": path, "score": score})
        lines.append(f"- {path}\n{text[:3000]}")
    out["text"] = "\n\n".join(lines)
    return out
