"""The retrieved reflections as the decision agent reads them: the paper's layout (Figure 5),

    [RANKED REFLECTIONS]
    [RANK 1 OF K REFLECTION]
    {reflection}
    ...

A bank of stored trajectories (index.json `memory: trajectory`, the Top-K Raw baseline) is rendered as
[RANKED TRAJECTORIES] / [RANK n OF K TRAJECTORY], so the block says what its entries are.
"""
from __future__ import annotations

from .bank import Reflection

HEADER = "[RANKED REFLECTIONS]"
WORDS = {"reflection": ("REFLECTIONS", "REFLECTION"), "trajectory": ("TRAJECTORIES", "TRAJECTORY")}


def ranked(retrieved: list[dict], by_id: dict[str, Reflection], kind: str = "reflection") -> str:
    """The block for `retrieved` (rank order), or "" when nothing was retrieved."""
    if not retrieved:
        return ""
    plural, singular = WORDS.get(kind, WORDS["reflection"])
    total = len(retrieved)
    parts = [f"[RANKED {plural}]"]
    for item in retrieved:
        reflection = by_id[item["id"]]
        parts.append(f"[RANK {item['rank']} OF {total} {singular}]\n{reflection.reflection.strip()}")
    return "\n".join(parts[:2]) + "".join("\n\n" + p for p in parts[2:]) + "\n"
