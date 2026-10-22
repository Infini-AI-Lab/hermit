"""Insert the adapter context into project instructions as one marked, replaceable block."""
import re

START = "<!-- hermit:"   # every managed block begins with this
PROJECT_CONTEXT = (
    "# Project instructions\n\n"
    "This workspace has a harness adapter deployed.\n"
    "Project-specific instructions belong outside the managed adapter block below.\n"
)


def nested_headings(markdown: str) -> str:
    """Nest adapter headings under the insertion title without changing code blocks."""
    lines = []
    fence = None
    for line in markdown.splitlines():
        match = re.match(r"^\s{0,3}(`{3,}|~{3,})(.*)$", line)
        if match:
            token, suffix = match.groups()
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence) and not suffix.strip():
                fence = None
        elif fence is None:
            line = re.sub(r"^(#{1,5})\s", lambda m: "#" * min(len(m[1]) + 2, 6) + " ", line)
        lines.append(line)
    return "\n".join(lines)


def append_context(existing: str, context: str, *, source="ADAPTER.md", version="", adapter_id="adapter") -> str:
    """Update our marked block; preserve everything outside it exactly."""
    start, end_marker = f"<!-- hermit:{adapter_id}:start -->", f"<!-- hermit:{adapter_id}:end -->"
    block = (
        f"{start}\n\n---\n\n## Adapter: `{adapter_id}`\n\n"
        f"> Managed by Harness Adapter. Source: `{source}` · Version: `{version}`.\n"
        "> Only this marked block is replaced when the adapter context is refreshed.\n\n"
        f"{nested_headings(context).rstrip()}\n\n"
        f"**End of adapter: `{adapter_id}`**\n\n---\n\n" + end_marker
    )
    if start not in existing and end_marker not in existing:
        return existing + ("\n\n" if existing else "") + block + "\n"
    if existing.count(start) != 1 or existing.count(end_marker) != 1:
        raise ValueError("Ambiguous adapter context markers; refusing to overwrite project instructions")
    begin, end = existing.index(start), existing.index(end_marker)
    if end < begin:
        raise ValueError("Reversed adapter context markers")
    return existing[:begin] + block + existing[end + len(end_marker):]
