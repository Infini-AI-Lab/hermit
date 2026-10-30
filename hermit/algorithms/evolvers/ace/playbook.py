"""ACE's playbook (ace-agent/ace, commit 82709de), reimplemented function by function, and its place in ADAPTER.md.

Upstream keeps these in `playbook_utils.py`, `utils.get_section_slug`, the Reflector's and Curator's reply parsing
(`ace/core/reflector.py`, `ace/core/curator.py`) and the BulletpointAnalyzer (`ace/core/bulletpoint_analyzer.py`).
The upstream package cannot be imported as a library (no build configuration; top-level modules named `utils`, `llm`
and `logger` that pull in its API clients), so the functions are ported here; each names the function it follows.
Behaviour is upstream's, including its quirks, except where a comment says otherwise.

The playbook lives in ADAPTER.md between two markers, after the fixed generator instructions:

    <text the adapter started with, e.g. a task skill>      not touched by ACE
    <!-- ace:playbook -->
    # Playbook ... (GENERATOR_INSTRUCTIONS)                   fixed
    <!-- ace:bullets -->
    ## STRATEGIES & INSIGHTS                                  the playbook proper: headers and bullets
    [sai-00001] helpful=2 harmful=0 :: ...
    <!-- /ace:playbook -->
"""
from __future__ import annotations

import json
import re

from hermit.algorithms.evolvers.ace.prompts import GENERATOR_INSTRUCTIONS

BEGIN = "<!-- ace:playbook -->"
BULLETS = "<!-- ace:bullets -->"
END = "<!-- /ace:playbook -->"

# ACE._initialize_empty_playbook (ace/ace.py)
EMPTY_PLAYBOOK = """## STRATEGIES & INSIGHTS

## FORMULAS & CALCULATIONS

## CODE SNIPPETS & TEMPLATES

## COMMON MISTAKES TO AVOID

## PROBLEM-SOLVING HEURISTICS

## CONTEXT CLUES & INDICATORS

## OTHERS"""

LINE_RX = re.compile(r"\[([^\]]+)\]\s*helpful=(\d+)\s*harmful=(\d+)\s*::\s*(.*)")
# Generator._extract_bullet_ids_regex uses [a-z]{3,}-\d{5}; a section of two words gets a two-letter slug from
# get_section_slug, so any letters are accepted here. Only ids present in the playbook are kept, and an id followed by
# its counters is a copy of the playbook line (the agent printed its context file), not a citation.
CITED_RX = re.compile(r"\[([a-z]+-\d{5})\](?!\s*helpful=)")
ID_RX = re.compile(r"[a-z]+-\d{5}")


# ---------- the playbook inside ADAPTER.md ----------

def has_playbook(text: str) -> bool:
    return BEGIN in text and BULLETS in text and END in text


def with_playbook(base: str, playbook: str = EMPTY_PLAYBOOK) -> str:
    """`base` (the adapter's own text) followed by the generator instructions and the playbook."""
    head = base.rstrip() + "\n\n" if base.strip() else ""
    return f"{head}{BEGIN}\n{GENERATOR_INSTRUCTIONS}\n{BULLETS}\n{playbook.strip()}\n{END}\n"


def split_adapter(text: str) -> tuple[str, str, str]:
    """(everything up to and including the bullets marker, the playbook, everything from the end marker)."""
    if not has_playbook(text):
        raise ValueError("ADAPTER.md has no ACE playbook block")
    start = text.index(BULLETS) + len(BULLETS)
    end = text.index(END, start)
    return text[:start], text[start:end].strip("\n"), text[end:]


def join_adapter(prefix: str, playbook: str, suffix: str) -> str:
    return f"{prefix}\n{playbook.strip()}\n{suffix}"


def playbook_problems(text: str) -> list[str]:
    """What makes ADAPTER.md's playbook block unusable: markers missing, repeated or out of order, a line in the
    playbook that is neither a section header nor a bullet, a malformed or repeated bullet id."""
    errs = []
    for marker in (BEGIN, BULLETS, END):
        count = text.count(marker)
        if count != 1:
            errs.append(f"ADAPTER.md must contain the marker {marker} exactly once (found {count})")
    if errs:
        return errs
    if not text.index(BEGIN) < text.index(BULLETS) < text.index(END):
        return [f"the markers must come in the order {BEGIN}, {BULLETS}, {END}"]
    _, playbook, _ = split_adapter(text)
    seen = set()
    for n, line in enumerate(playbook.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parsed = parse_playbook_line(s)
        if not parsed:
            errs.append(f"playbook line {n} is neither a `## section` header nor a bullet "
                        f"`[id] helpful=N harmful=N :: content`: {s[:120]!r}")
        elif not ID_RX.fullmatch(parsed["id"]):
            errs.append(f"playbook line {n}: bullet id {parsed['id']!r} is not of the form slug-00001")
        elif parsed["id"] in seen:
            errs.append(f"playbook line {n}: bullet id {parsed['id']} appears twice")
        else:
            seen.add(parsed["id"])
        if len(errs) >= 10:
            break
    return errs


# ---------- playbook_utils.py ----------

def parse_playbook_line(line: str) -> dict | None:
    """playbook_utils.parse_playbook_line"""
    match = LINE_RX.match(line.strip())
    if match:
        return {"id": match.group(1), "helpful": int(match.group(2)), "harmful": int(match.group(3)),
                "content": match.group(4), "raw_line": line}
    return None


def format_playbook_line(bullet_id: str, helpful: int, harmful: int, content: str) -> str:
    """playbook_utils.format_playbook_line"""
    return f"[{bullet_id}] helpful={helpful} harmful={harmful} :: {content}"


def get_next_global_id(playbook_text: str) -> int:
    """playbook_utils.get_next_global_id"""
    max_id = 0
    for line in playbook_text.strip().split("\n"):
        parsed = parse_playbook_line(line)
        if parsed:
            m = re.search(r"-(\d+)$", parsed["id"])
            if m:
                max_id = max(max_id, int(m.group(1)))
    return max_id + 1


def get_section_slug(section_name: str) -> str:
    """utils.get_section_slug"""
    slug_map = {
        "financial_strategies_and_insights": "fin",
        "formulas_and_calculations": "calc",
        "code_snippets_and_templates": "code",
        "common_mistakes_to_avoid": "err",
        "problem_solving_heuristics": "prob",
        "context_clues_and_indicators": "ctx",
        "others": "misc",
        "meta_strategies": "meta",
    }
    clean_name = section_name.lower().strip().replace(" ", "_").replace("&", "and")
    if clean_name in slug_map:
        return slug_map[clean_name]
    words = clean_name.split("_")
    if len(words) == 1:
        return words[0][:4]
    return "".join(w[0] for w in words[:5] if w)    # upstream w[0] fails on an empty word ("a__b"); skipped here


def _section_key(header: str) -> str:
    return header.lower().replace(" ", "_").replace("&", "and")


def update_bullet_counts(playbook_text: str, bullet_tags) -> str:
    """playbook_utils.update_bullet_counts: helpful +1, harmful +1, neutral (or anything else) unchanged."""
    tag_map = {}
    if isinstance(bullet_tags, list):
        for tag in bullet_tags:
            if isinstance(tag, dict):
                bullet_id = tag.get("id") or tag.get("bullet", "")
                if bullet_id:
                    tag_map[str(bullet_id)] = tag.get("tag", "neutral")
    if not tag_map:
        return playbook_text
    updated = []
    for line in playbook_text.strip().split("\n"):
        if line.strip().startswith("#") or not line.strip():
            updated.append(line)
            continue
        parsed = parse_playbook_line(line)
        if parsed and parsed["id"] in tag_map:
            tag = tag_map[parsed["id"]]
            if tag == "helpful":
                parsed["helpful"] += 1
            elif tag == "harmful":
                parsed["harmful"] += 1
            updated.append(format_playbook_line(parsed["id"], parsed["helpful"], parsed["harmful"], parsed["content"]))
        else:
            updated.append(line)
    return "\n".join(updated)


def clean_content(content) -> str:
    """A bullet is one line. Upstream writes the Curator's content as given, so a newline in it breaks the playbook's
    line format (the rest of the bullet becomes a line no parser reads); here whitespace runs become one space."""
    return " ".join(str(content or "").split())


def apply_curator_operations(playbook_text: str, operations: list, next_id: int) -> tuple[str, int, list[dict]]:
    """playbook_utils.apply_curator_operations: only ADD is applied (upstream's UPDATE, MERGE, DELETE and CREATE_META
    are unimplemented TODOs); a section that does not exist goes to OTHERS. Also returns the bullets added, for the
    record (upstream prints them)."""
    lines = playbook_text.strip().split("\n")
    sections: dict[str, list] = {}
    current = "general"
    for i, line in enumerate(lines):
        if line.strip().startswith("##"):
            current = _section_key(line.strip()[2:].strip())
            sections.setdefault(current, [])
        elif line.strip():
            sections.setdefault(current, []).append((i, line))

    to_add, added = [], []
    for op in operations:
        if op.get("type") != "ADD":
            continue
        section_raw = str(op.get("section", "general"))
        section = _section_key(section_raw)
        if section not in sections and section != "general":
            section = "others"
        new_id = f"{get_section_slug(section)}-{next_id:05d}"
        next_id += 1
        content = clean_content(op.get("content", ""))
        to_add.append((section, format_playbook_line(new_id, 0, 0, content)))
        added.append({"id": new_id, "section": section, "requested_section": section_raw, "content": content})

    final, current = [], None
    for line in lines:
        if line.strip().startswith("##"):
            if current:
                final += [b for s, b in to_add if s == current]
                to_add = [(s, b) for s, b in to_add if s != current]
            current = _section_key(line.strip()[2:].strip())
        final.append(line)
    if current:
        final += [b for s, b in to_add if s == current]
        to_add = [(s, b) for s, b in to_add if s != current]
    if to_add:
        others = [b for _, b in to_add]
        idx = next((i for i, line in enumerate(final) if line.strip() == "## OTHERS"), -1)
        if idx >= 0:
            for k, bullet in enumerate(others):
                final.insert(idx + 1 + k, bullet)
        else:
            final += others
    return "\n".join(final), next_id, added


def get_playbook_stats(playbook_text: str) -> dict:
    """playbook_utils.get_playbook_stats"""
    stats = {"total_bullets": 0, "high_performing": 0, "problematic": 0, "unused": 0, "by_section": {}}
    current = "general"
    for line in playbook_text.strip().split("\n"):
        if line.strip().startswith("##"):
            current = line.strip()[2:].strip()
            continue
        parsed = parse_playbook_line(line)
        if parsed:
            stats["total_bullets"] += 1
            if parsed["helpful"] > 5 and parsed["harmful"] < 2:
                stats["high_performing"] += 1
            elif parsed["harmful"] >= parsed["helpful"] and parsed["harmful"] > 0:
                stats["problematic"] += 1
            elif parsed["helpful"] + parsed["harmful"] == 0:
                stats["unused"] += 1
            sec = stats["by_section"].setdefault(current, {"count": 0, "helpful": 0, "harmful": 0})
            sec["count"] += 1
            sec["helpful"] += parsed["helpful"]
            sec["harmful"] += parsed["harmful"]
    return stats


def extract_playbook_bullets(playbook_text: str, bullet_ids: list[str]) -> str:
    """playbook_utils.extract_playbook_bullets: the bullets the generator used, for the Reflector."""
    if not bullet_ids:
        return "(No bullets used by generator)"
    found = []
    for line in playbook_text.strip().split("\n"):
        parsed = parse_playbook_line(line) if line.strip() else None
        if parsed and parsed["id"] in bullet_ids:
            found.append(format_playbook_line(parsed["id"], parsed["helpful"], parsed["harmful"], parsed["content"]))
    if not found:
        return "(Generator referenced bullet IDs but none were found in playbook)"
    return "\n".join(found)


def bullet_ids_of(playbook_text: str) -> list[str]:
    return [p["id"] for p in map(parse_playbook_line, playbook_text.split("\n")) if p]


def cited_bullet_ids(text: str, playbook_text: str) -> list[str]:
    """Generator._extract_bullet_ids_regex over what the acting agent wrote, kept to ids in the playbook, in order of
    first mention."""
    known = set(bullet_ids_of(playbook_text))
    out: list[str] = []
    for bid in CITED_RX.findall(text or ""):
        if bid in known and bid not in out:
            out.append(bid)
    return out


def extract_json_from_text(text: str) -> dict | None:
    """playbook_utils.extract_json_from_text: the whole reply, else a ```json block, else the first balanced {...}
    that parses."""
    text = text or ""
    try:
        result = json.loads(text.strip())
        if isinstance(result, dict):
            return result
    except ValueError:
        pass
    for match in re.findall(r"```json\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE):
        try:
            result = json.loads(match.strip())
            if isinstance(result, dict):
                return result
        except ValueError:
            continue
    i = 0
    while i < len(text):
        if text[i] != "{":
            i += 1
            continue
        depth, start = 1, i
        i += 1
        while i < len(text) and depth > 0:
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            elif text[i] == '"':
                i += 1
                while i < len(text) and text[i] != '"':
                    if text[i] == "\\":
                        i += 1
                    i += 1
            i += 1
        if depth == 0:
            try:
                result = json.loads(text[start:i])
                if isinstance(result, dict):
                    return result
            except ValueError:
                pass
            i = start + 1      # upstream scans on from the end of the candidate; a nested object is tried too here
    return None


# ---------- the Reflector's and the Curator's replies ----------

def extract_bullet_tags(response: str) -> list[dict]:
    """Reflector._extract_bullet_tags (non-JSON mode): the array after "bullet_tags", by bracket matching. JSON mode
    is tried first, which upstream does only when json_mode is set."""
    whole = extract_json_from_text(response)
    if isinstance(whole, dict) and isinstance(whole.get("bullet_tags"), list):
        return whole["bullet_tags"]
    start = (response or "").find('"bullet_tags"')
    if start == -1:
        return []
    bracket = response.find("[", start)
    if bracket == -1:
        return []
    depth, end = 0, bracket
    for i in range(bracket, len(response)):
        if response[i] == "[":
            depth += 1
        elif response[i] == "]":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    try:
        tags = json.loads(response[bracket:end])
    except ValueError:
        return []
    return tags if isinstance(tags, list) else []


def extract_and_validate_operations(response: str) -> dict:
    """Curator._extract_and_validate_operations: raises ValueError for a reply the Curator's caller skips."""
    info = extract_json_from_text(response)
    if not info:
        raise ValueError("Failed to extract valid JSON from curator response")
    if "reasoning" not in info:
        raise ValueError("JSON missing required 'reasoning' field")
    if "operations" not in info:
        raise ValueError("JSON missing required 'operations' field")
    if not isinstance(info["reasoning"], str):
        raise ValueError("'reasoning' field must be a string")
    if not isinstance(info["operations"], list):
        raise ValueError("'operations' field must be a list")
    for i, op in enumerate(info["operations"]):
        if not isinstance(op, dict):
            raise ValueError(f"Operation {i} must be a dictionary")
        if "type" not in op:
            raise ValueError(f"Operation {i} missing required 'type' field")
        if op["type"] == "ADD":
            missing = {"type", "section", "content"} - set(op)
            if missing:
                raise ValueError(f"ADD operation {i} missing fields: {sorted(missing)}")
    return info


# ---------- BulletpointAnalyzer ----------

def find_similar_groups(embeddings, threshold: float) -> list[list[int]]:
    """BulletpointAnalyzer._find_similar_groups over L2-normalised embeddings (a list of equal-length vectors): each
    unvisited i with every later j whose cosine similarity to i is at least `threshold`."""
    n = len(embeddings)
    dot = lambda a, b: sum(x * y for x, y in zip(a, b))
    groups, visited = [], set()
    for i in range(n):
        if i in visited:
            continue
        similar = [j for j in range(i + 1, n) if dot(embeddings[i], embeddings[j]) >= threshold]
        if similar:
            groups.append([i] + similar)
            visited.update([i] + similar)
    return groups


MERGED_RX = re.compile(r"\[([^\]]+)\]\s+helpful=(\d+)\s+harmful=(\d+)\s+::\s+(.+)")


def merge_groups(playbook_text: str, groups: list[list[int]], merged: dict[int, str | None]) -> str:
    """BulletpointAnalyzer.analyze's reconstruction: each group's first bullet becomes its merged line (or stays as it
    was when the merge failed), the group's other bullets are removed. `groups` index the playbook's bullets in order;
    `merged` maps a group's first index to the merged line, None for a failed merge."""
    drop, replace, k = set(), {}, 0
    for g in groups:
        drop.update(g[1:])
        if merged.get(g[0]):
            replace[g[0]] = merged[g[0]]
    out = []
    for line in playbook_text.strip().split("\n"):
        if parse_playbook_line(line):
            if k not in drop:
                out.append(replace.get(k, line))
            k += 1
        else:
            out.append(line)
    return "\n".join(out)
