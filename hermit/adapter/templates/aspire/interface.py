"""The ASPIRE adapter's reader: the skill library as the actor's context (ASPIRE §2.2: files as context, no
retrieval ranking). The harness calls `library()` once per task (Session.context) and appends its `text` to
ADAPTER.md: skills/README.md, then each skill file in the order the index links them, then any file it does not."""
from __future__ import annotations

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent
LINK = re.compile(r"\]\(([A-Za-z0-9_\-]+\.md)\)")


def _body(text: str) -> str:
    """A skill file without its frontmatter."""
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            return text[end + 4:].lstrip("\n")
    return text


def library(query: str = "", task: dict | None = None) -> dict:
    skills = ROOT / "skills"
    index = (skills / "README.md").read_text(encoding="utf-8") if (skills / "README.md").is_file() else ""
    # skills/proposed.md holds proposals under verification (the verifier's proposed_skills.md): not the library
    present = sorted(p.name for p in skills.glob("*.md") if p.name not in ("README.md", "proposed.md"))
    order = [n for n in dict.fromkeys(LINK.findall(index)) if n in present]
    order += [n for n in present if n not in order]
    parts, entries = [index.strip()] if index.strip() else [], 0
    for name in order:
        body = _body((skills / name).read_text(encoding="utf-8")).strip()
        entries += len(re.findall(r"^## ", body, re.M))
        parts.append(f"<!-- skills/{name} -->\n{body}")
    return {"text": "\n\n---\n\n".join(parts), "files": order, "entries": entries}
