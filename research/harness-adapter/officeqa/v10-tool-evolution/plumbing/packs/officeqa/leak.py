"""OfficeQA leak screen for learned adapter text (notes, rule templates, rule args).

Port of the cards evolver's numeric screen (frameworks-v8/next-a48c0f9/harnessadapter/evolvers/cards/
block.py:25-128) behind the pack signature `leak_screen(text, released_answers) -> list[str]`:
  - identifiers that tie text to an item: question ids (UID1234), ordinals (q123), issue stamps (2004_03),
    corpus file names;
  - large numbers: > 400 with >= 3 significant digits that are not years (constants a note should not carry);
  - numbers equal to a released reference answer (>= 2 significant digits, outside a small allow-list of
    constants and years), including spelled-out numbers;
  - the normalised text of a non-numeric released answer of >= 4 characters (text answers).
Findings describe the leak without repeating released values (only the answer's position is named).
The cards screen's 8-gram question-copy check needs the released questions and stays with the learner.
"""
from __future__ import annotations

import re
from typing import Sequence

LEAK = (
    (re.compile(r"\bUID\d{4}\b"), "question id"),
    (re.compile(r"\bq\d{3}\b", re.I), "question ordinal"),
    (re.compile(r"\b(19|20)\d{2}_\d{2}\b"), "issue file stamp"),
    (re.compile(r"treasury_bulletin_\S+"), "corpus file name"),
    (re.compile(r"\S+\.txt\b"), "file name"),
)
NUMBER = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?(?!\w|\.\d)")
NUMBER_WORDS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve "
                                           "thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty".split())}
SMALL_CONSTANTS = set(range(0, 13)) | {52, 100, 365, 1000}


def normalize(text: str) -> str:
    """Casefold, drop possessives, punctuation to spaces, collapse whitespace (cards schema.normalize)."""
    text = str(text).casefold().replace("_", " ")
    text = re.sub(r"['’]s\b", "", text)
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _value(token: str):
    try:
        return abs(float(token.replace(",", "").rstrip("%")))
    except ValueError:
        return None


def _significant(token: str) -> int:
    return len(token.replace(",", "").replace("-", "").replace("%", "").replace(".", "").strip("0"))


def numbers_in(text: str) -> list:
    return [(t, v) for t in NUMBER.findall(str(text)) for v in [_value(t)] if v is not None]


def _is_year(v: float) -> bool:
    return 1900 <= v <= 2100 and float(v).is_integer()


def big_numbers(text: str) -> list:
    """Numbers above 400 with three or more significant digits that are not years."""
    return [t for t, v in numbers_in(text) if v > 400 and _significant(t) >= 3 and not _is_year(v)]


def leak_screen(text: str, released_answers: Sequence[str]) -> list:
    text = str(text or "")
    findings = [f"{label}: {m.group(0)}" for pattern, label in LEAK for m in [pattern.search(text)] if m]
    big = big_numbers(text)
    if big:
        findings.append("large number: " + ", ".join(big[:3]))
    values = {round(v, 6) for _, v in numbers_in(text)}
    norm_text = normalize(text)
    values |= {float(NUMBER_WORDS[w]) for w in norm_text.split() if w in NUMBER_WORDS}
    padded = f" {norm_text} "
    for i, ref in enumerate(released_answers or ()):
        ref = str(ref or "")
        for token, value in numbers_in(ref):
            if round(value, 6) in values and _significant(token) >= 2 and value not in SMALL_CONSTANTS \
                    and not _is_year(value):
                findings.append(f"a number equal to released answer #{i}")
                break
        words = normalize(ref)
        if words and not NUMBER.fullmatch(ref.strip()) and len(words) >= 4 and any(c.isalpha() for c in words) \
                and f" {words} " in padded:
            findings.append(f"the text of released answer #{i}")
    return findings
