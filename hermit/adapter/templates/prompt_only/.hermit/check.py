"""Template check: the candidate keeps ADAPTER.md, the one file this adapter exists for.

Without it a prompt-only adapter deploys nothing; a candidate that deletes it gets the parent's back.
"""
from __future__ import annotations

from pathlib import Path


def check(adapter_path: str) -> list[dict]:
    if (Path(adapter_path) / "ADAPTER.md").is_file():
        return []
    return [{"path": "ADAPTER.md", "problem": "ADAPTER.md must stay: it is this adapter's only content"}]
