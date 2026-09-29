"""Card files: one atomic rule each.

Parsed, validated and matched here, by the renderer inside the adapter copy the
actor reads, by the evolver's block algorithm and by the tests, so all three
agree on what a card is.

File format (`key: value` lines; a value of `|` continues on the two-space
indented lines that follow):

    id: C001
    scope: fiscal year, annual row, revised
    claim: |
      When a question asks for an annual figure and the same row is printed in
      more than one issue, use the latest issue that prints the row.
    unless: |
      The question names a specific issue or date.
    support: 3, 8
    admitted: 2

Only id, scope, claim and unless reach the acting agent. support lists the
ordinals of the released questions the card was written for (one or more); it
is evidence for the verifier, which re-runs them. admitted is the block number
the controller writes when the card passes verification; a card without it is
never rendered.
"""
from __future__ import annotations

import re

FIELDS = ("id", "scope", "claim", "unless", "support", "admitted")
CLAIM_MAX = 600      # UTF-8 bytes the actor reads per card
UNLESS_MAX = 300
SCOPE_MIN, SCOPE_MAX = 1, 8
KEYWORD_MIN = 3
ID_PATTERN = re.compile(r"C\d{3}")
STOPWORDS = frozenset("""
the and for was are did has how its not but per any all one two use may can what who why
about above after again against also amount answer around because been before being below between both
could does doing down during each either every from further have having here into just least less more most much must
number only other over question report same should since some such than that their them then there these they this those
through under until using value values very want were what when where which while whose will with within without would
year years your
""".split())
# Lines that would restructure the document the actor reads (headings, fences, comment markers).
STRUCTURE = re.compile(r"^\s{0,3}(#{1,6}(\s|$)|`{3,}|~{3,}|<!--)")


def normalize(text: str) -> str:
    """Casefold, drop possessives, turn punctuation into spaces, collapse whitespace."""
    text = str(text).casefold().replace("_", " ")
    text = re.sub(r"['’]s\b", "", text)
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def keywords(scope: str) -> list[str]:
    return [normalize(k) for k in str(scope).split(",") if normalize(k)]


def matches(question: str, scope: str) -> list[str]:
    """Scope keywords that occur in the question as whole words or phrases."""
    haystack = " " + normalize(question) + " "
    return [k for k in keywords(scope) if " " + k + " " in haystack]


def parse(text: str) -> dict:
    card, key, block = {}, None, None
    for raw in text.splitlines():
        if block is not None:
            if raw.startswith("  ") or not raw.strip():
                block.append(raw[2:] if raw.startswith("  ") else "")
                continue
            card[key] = "\n".join(block).strip()
            block = None
        match = re.match(r"^([a-z]+):\s*(.*)$", raw)
        if not match:
            if raw.strip():
                raise ValueError(f"Unparseable card line: {raw!r}")
            continue
        key, value = match.group(1), match.group(2).strip()
        if key in card:
            raise ValueError(f"Duplicate field: {key}")
        if value == "|":
            block = []
        else:
            card[key] = value
    if block is not None:
        card[key] = "\n".join(block).strip()
    return card


def support_list(value) -> list[int]:
    if isinstance(value, (list, tuple)):
        items = list(value)
    elif isinstance(value, str):
        items = [v for v in re.split(r"[,\s]+", value.strip()) if v]
    elif value is None:
        items = []
    else:
        raise ValueError("support must be a list of question ordinals")
    result = []
    for item in items:
        try:
            ordinal = int(str(item).lstrip("q"))
        except ValueError:
            raise ValueError(f"support entry is not an ordinal: {item!r}") from None
        if ordinal < 1:
            raise ValueError("support ordinals are positive")
        if ordinal not in result:
            result.append(ordinal)
    return result


def validate(card) -> list[str]:
    """Every reason the card is not a card; an empty list means it is one."""
    if not isinstance(card, dict):
        return ["card must be a mapping"]
    errors = []
    unknown = set(card) - set(FIELDS)
    if unknown:
        errors.append("unknown fields: " + ", ".join(sorted(unknown)))
    if not ID_PATTERN.fullmatch(str(card.get("id", ""))):
        errors.append("id must look like C001")
    words = keywords(card.get("scope", ""))
    if not SCOPE_MIN <= len(words) <= SCOPE_MAX:
        errors.append(f"scope needs {SCOPE_MIN}-{SCOPE_MAX} comma-separated keywords or short phrases")
    for word in words:
        if min(len(token) for token in word.split()) < KEYWORD_MIN:
            errors.append(f"scope keyword too short: {word!r}")
        elif word in STOPWORDS:
            errors.append(f"scope keyword is a stopword: {word!r}")
    if len(set(words)) != len(words):
        errors.append("duplicate scope keywords")
    claim = str(card.get("claim", "") or "").strip()
    if not claim:
        errors.append("claim is empty")
    elif len(claim.encode()) > CLAIM_MAX:
        errors.append(f"claim exceeds {CLAIM_MAX} bytes")
    unless = str(card.get("unless", "") or "").strip()
    if len(unless.encode()) > UNLESS_MAX:
        errors.append(f"unless exceeds {UNLESS_MAX} bytes")
    for field, text in (("claim", claim), ("unless", unless)):
        for line in text.splitlines():
            if STRUCTURE.match(line):
                errors.append(f"{field} line looks like Markdown structure: {line.strip()[:40]!r}")
    try:
        if len(support_list(card.get("support"))) < 1:
            errors.append("support needs at least one question ordinal")
    except ValueError as exc:
        errors.append(str(exc))
    if card.get("admitted") is not None:
        try:
            if int(card["admitted"]) < 1:
                raise ValueError
        except (TypeError, ValueError):
            errors.append("admitted must be a positive block number")
    return errors


def dumps(card: dict) -> str:
    lines = [f"id: {card['id']}", "scope: " + ", ".join(keywords(card["scope"])), "claim: |"]
    lines += ["  " + line for line in str(card["claim"]).strip().splitlines()]
    unless = str(card.get("unless", "") or "").strip()
    if unless:
        lines.append("unless: |")
        lines += ["  " + line for line in unless.splitlines()]
    lines.append("support: " + ", ".join(str(n) for n in support_list(card.get("support"))))
    if card.get("admitted") is not None:
        lines.append(f"admitted: {int(card['admitted'])}")
    return "\n".join(lines) + "\n"


def render(card: dict) -> str:
    """What the actor reads for one card."""
    text = f"### {card['id']} — applies when: {', '.join(keywords(card['scope']))}\n{str(card['claim']).strip()}\n"
    unless = str(card.get("unless", "") or "").strip()
    if unless:
        text += f"Not when: {unless}\n"
    return text


def public_text(card: dict) -> str:
    """The text of a card that reaches the actor; what leak screening inspects."""
    return " ".join([str(card.get("scope", "")), str(card.get("claim", "")), str(card.get("unless", "") or "")])
