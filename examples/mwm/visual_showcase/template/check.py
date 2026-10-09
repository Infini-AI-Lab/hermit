"""Template check: knowledge is prose (no complete-HTML marker), the brief stays within 6000 characters, and no
knowledge path has a hidden component. Each issue names its file."""
from pathlib import Path
import re

MAX_BRIEF = 6000                                                   # characters
HTML_MARKER = re.compile(r'<!doctype\s+html|<html(?:\s|>)', re.I)
KNOWLEDGE = ("memory", "examples", "consumption", "meta")          # top directories of the learnable paths


def check(adapter: str) -> list[dict]:
    root = Path(adapter)
    issues = []
    for path in sorted(root.rglob("*.md")):
        rel = path.relative_to(root).as_posix()
        if rel.split("/")[0] not in KNOWLEDGE or not path.is_file():
            continue
        if any(part.startswith(".") for part in rel.split("/")):
            issues.append({"path": rel, "problem": "hidden path component in a knowledge path"})
            continue
        text = path.read_text(errors="replace")
        if HTML_MARKER.search(text):
            issues.append({"path": rel, "problem": "complete-HTML marker found: knowledge is prose, not a page copy"})
        if rel == "consumption/brief.md" and len(text) > MAX_BRIEF:
            issues.append({"path": rel, "problem": f"brief has {len(text)} characters; maximum is {MAX_BRIEF}"})
    return issues
