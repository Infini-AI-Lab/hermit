#!/usr/bin/env python3
"""Sync the selected design documents and render the dated discussion page.

The renderer supports the headings, paragraphs, lists, fenced code and simple
tables used in this project's authored discussion note. It is not a general
Markdown implementation. No external dependencies are required.
"""
from html import escape
from pathlib import Path
import re
import shutil

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "site" / "dist"
DOCUMENTS = (
    "execution-policy.md",
    "mwm-fidelity-correction.md",
    "mwm-integration-results-2026-09-18.md",
    "core-and-evolution.md",
    "adapter-contract-v0.md",
    "subagent-fields-2026-09-18.md",
    "design-discussion-2026-09-18.md",
)


def inline(value: str) -> str:
    value = escape(value)
    value = re.sub(r"`([^`]+)`", r"<code>\1</code>", value)
    value = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", value)

    def link(match):
        label, target = match.groups()
        if re.match(r"^[a-zA-Z][\w+.-]*:", target) and not target.startswith(("https://", "http://")):
            return label
        if not target.startswith(("https://", "http://", "#")):
            target = "../" + target
        return f'<a href="{target}">{label}</a>'

    return re.sub(r"\[([^\]]+)\]\(([^)]+)\)", link, value)


def render(markdown: str) -> str:
    lines = markdown.splitlines()
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if line.startswith("```"):
            code = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                code.append(lines[i])
                i += 1
            out.append("<pre><code>" + escape("\n".join(code)) + "</code></pre>")
            i += 1
            continue
        heading = re.match(r"^(#{1,6}) (.+)$", line)
        if heading:
            level = len(heading[1])
            out.append(f"<h{level}>{inline(heading[2])}</h{level}>")
            i += 1
            continue
        if line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-+:?", c.replace(" ", "")) for c in cells):
                    rows.append(cells)
                i += 1
            table = ['<div class="journal-table-wrap"><table><thead><tr>']
            table.extend(f"<th scope=\"col\">{inline(c)}</th>" for c in rows[0])
            table.append("</tr></thead><tbody>")
            for row in rows[1:]:
                table.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in row) + "</tr>")
            table.append("</tbody></table></div>")
            out.append("".join(table))
            continue
        bullet = re.match(r"^(- |\d+\. )(.+)$", line)
        if bullet:
            ordered = bullet[1] != "- "
            tag = "ol" if ordered else "ul"
            items = []
            while i < len(lines):
                match = re.match(r"^(\d+\. )(.+)$" if ordered else r"^(- )(.+)$", lines[i])
                if not match:
                    break
                items.append(f"<li>{inline(match[2])}</li>")
                i += 1
            out.append(f"<{tag}>" + "".join(items) + f"</{tag}>")
            continue
        paragraph = [line]
        i += 1
        while i < len(lines) and lines[i].strip() and not re.match(r"^(#|\||```|- |\d+\. )", lines[i]):
            paragraph.append(lines[i])
            i += 1
        out.append("<p>" + inline(" ".join(paragraph)) + "</p>")
    return "\n".join(out)


def main():
    DIST.mkdir(parents=True, exist_ok=True)
    for name in DOCUMENTS:
        shutil.copyfile(ROOT / "docs" / name, DIST / name)
    shutil.copyfile(ROOT / "harnessadapter/evolvers/mwm/METHOD.md", DIST / "mwm-evolver.md")
    import json
    app = DIST / "app.js"
    script = app.read_text()
    start = script.index("const templates=") + len("const templates=")
    templates, length = json.JSONDecoder().raw_decode(script[start:])
    core = ROOT / "harnessadapter/architectures/mwm"
    names = ["adapter.json", "ADAPTER.md", "EVOLVER.md", "interface.py", "memory/_template.md",
             "examples/_template.md", "tools/_template/TOOL.md", "tools/_template/run.py",
             "tools/_template/test.py", "agents/_template/SPEC.md", "consumption/brief.md",
             "meta/cases/README.md", "meta/journal.md"]
    templates["knowledge"]["files"] = {name: (core / name).read_text() for name in names}
    templates["knowledge"]["description"] = "Corrected MWM core: original artifact bundles, empty learned tools and roles, one wm interface, and persistent cases separate from the journal. Generic transport stays in the framework."
    templates["knowledge"]["tags"] = ["original MWM structure", "single wm interface", "sandbox-gated programs"]
    app.write_text(script[:start] + json.dumps(templates, ensure_ascii=False, indent=2) + script[start + length:])
    prompt_core = ROOT / "harnessadapter/architectures/prompt_only"
    templates["skill"] = {
        "title": "prompt-only/", "kind": "IMPLEMENTED ARCHITECTURE",
        "description": "A context-only architecture. No learned instructions initially, no native roles, and no adapter MCP server. Choose the Evolver separately.",
        "tags": ["context only", "no MCP server", "empty learned prompt"],
        "files": {name: (prompt_core/name).read_text() for name in ("adapter.json", "ADAPTER.md", "EVOLVER.md", "README.md")}
    }
    app.write_text(script[:start] + json.dumps(templates, ensure_ascii=False, indent=2) + script[start + length:])
    note = (ROOT / "docs" / DOCUMENTS[-1]).read_text()
    page = '''<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="Harness Adapter design discussion, September 18, 2026: interfaces, role definitions, bootstrap, and open questions.">
<title>2026.09.18 Design Discussion — Harness Adapter</title>
<link rel="stylesheet" href="../styles.css">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 48 48'%3E%3Crect width='48' height='48' rx='12' fill='%23245542'/%3E%3Cpath d='M12 12v24m24-24v24M12 24h24' stroke='%23d9efb8' stroke-width='4'/%3E%3C/svg%3E">
</head><body><div class="journal-shell">
<header class="journal-top"><a href="../index.html#archive">← Harness Adapter / Discussion archive</a><span class="mono">2026.09.18</span></header>
<main class="journal-article">''' + render(note) + '''</main>
<footer class="journal-bottom"><a href="../index.html#archive">Back to the design notebook</a><a href="../design-discussion-2026-09-18.md" download>Download Markdown ↓</a></footer>
</div></body></html>'''
    target = DIST / "discussions" / "2026-09-18.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(page)
    print(f"Synced {len(DOCUMENTS)} documents; rendered {target.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
