"""OfficeQAPack: the OfficeQA implementation of contracts.Pack (DESIGN §2 L1 + L4 pack part).

Wiring only: tools -> tools/cli.py (+ tools/otables.py; the only pack files an actor can read or list),
question_spec -> spec.py, observations -> observe.py (re-derived from the tables index), predicates/offers
-> rules.py, seed rules/notes -> seed_rules.json / seed_notes.json, leak screen -> leak.py, answer span of a
final message -> extract_final.  Corpus and index paths default to OFFICEQA_CORPUS / TABLES_INDEX and are
only touched when a method needs them (constructing the pack is free).

Declared to the ledger (contracts.Pack optional attributes): handles_truncation = True (observe.py drops
the fragments beside Codex's truncation markers itself, so it gets the output as the model saw it) and
max_observations = 4000 (a stitched show_table page can show more than 400 cells).
"""
from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Callable, Mapping, Sequence

try:
    from harness_core import contracts as C
except ImportError:                                   # pragma: no cover
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from harness_core import contracts as C
from harness_core.numeric import numbers_in

from . import leak, observe, rules, spec
from .tools import otables

HERE = Path(__file__).resolve().parent
TOOLS_DIR = HERE / "tools"
CLI = TOOLS_DIR / "cli.py"
SEED_TEXT_MAX = 4096
ANSWER_MAX = 1000                                     # harness_core.gate.LIMITS['answer']
# 'Final answer: x', '**Answer:** x', 'ANSWER - x' (label at the start of a line; the value follows it).
# A hyphen right before a digit is a sign, not a separator: 'Answer -5' is not labelled (kept whole: -5).
_ANSWER_LABEL = re.compile(r"^\W*(?:final\s+answer|answer)\W*?\s*(?:[:：=–—]|-(?![\d.]))\s*(.+)$", re.I)
_ANSWER_TAG = re.compile(r"<(final_answer|answer)>\s*(.*?)\s*</\1>", re.I | re.S)
TASK_CONVENTIONS = (
    "Task conventions (published with the OfficeQA benchmark):\n"
    "1. Use maximum precision unless the question specifies otherwise.\n"
    "2. When multiple documents report the same metric, use the most up-to-date revision unless the question "
    "specifies an exact date.\n")
USAGE = (
    "Table tools (read-only; outputs are pages of at most 8,000 bytes):\n"
    "  find_tables \"<words or table id>\" [--years 2003-2004] [--rows] [--all]   find tables by id (FFO-3) or title/header words\n"
    "  show_table <file> <line> [--stitch] [--match <col words>] [--cols 1-3] [--grep <row>] [--long] [--raw] [--from-row N]\n"
    "      one table with filled periods (FY/CY/YYYY-MM), column labels, repairs and identity checks under NOTES;\n"
    "      every page starts 'TABLE <id> | <file>:<line> | part k | page rows a-b of N'; continue with --from-row N\n"
    "  printings --table <id or title words> --row <period or label> [--col <column words or (n)>] [--years A-B]\n"
    "      the same row/column in every issue that prints it: revisions, first and latest printing\n")


def extract_final(final_message: str) -> str:
    """The answer span of a final assistant message (what the gate checks when it was never submitted).
    The task asks for one line holding only the direct answer; when the model adds prose anyway, prose
    numbers must not become part of the candidate (S4-review 1). In order:
    1. an <answer>/<final_answer> tag, or the last line labelled 'Final answer:' / 'Answer:' -> its value;
    2. a one-line message -> that line;
    3. otherwise the last non-empty line that contains a digit, else the last non-empty line.
    Markdown emphasis/code marks around the span are removed; at most ANSWER_MAX characters."""
    text = str(final_message or "").replace("\r\n", "\n").strip()
    if not text:
        return ""
    tags = _ANSWER_TAG.findall(text)
    if tags and tags[-1][1].strip():
        span = tags[-1][1]
    else:
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        labelled = [m.group(1) for m in map(_ANSWER_LABEL.match, lines) if m and m.group(1).strip()]
        if labelled:
            span = labelled[-1]
        elif len(lines) == 1:
            span = lines[0]
        else:
            span = next((ln for ln in reversed(lines) if re.search(r"\d", ln)), lines[-1])
    span = " ".join(span.split())
    span = re.sub(r"^(?:\*\*|__|`)+\s*|\s*(?:\*\*|__|`)+$", "", span)
    return span[:ANSWER_MAX]


def tool_wrappers() -> dict[str, list[str]]:
    """kit.build_kit `tool_wrappers` for the L1 tools: {name: [tools/cli.py, name]} (the multi-command CLI
    takes the tool name first). Orchestration adds harness_core/tools/calc.py itself."""
    return {name: [str(CLI), name] for name in OfficeQAPack.tools}


class OfficeQAPack:
    """contracts.Pack for OfficeQA (Treasury Bulletin text corpus)."""
    name = "officeqa"
    answer_kind = "text"
    tools: Mapping[str, str] = {"find_tables": "tools.cli:find_tables", "show_table": "tools.cli:show_table",
                                "printings": "tools.cli:printings"}
    handles_truncation = True
    max_observations = 4000

    def __init__(self, corpus=None, index=None):
        self._corpus, self._index = corpus, index
        self._tables = None

    @property
    def tables(self) -> "otables.Tables":
        if self._tables is None:
            self._tables = otables.Tables(self._corpus, self._index)
        return self._tables

    def close(self) -> None:
        if self._tables is not None:
            self._tables.close()

    # -- questions and answers
    def question_spec(self, question: str) -> C.QuestionSpec:
        return spec.question_spec(question)

    def canonical(self, answer: str) -> str:
        return spec.canonical(answer)

    def numbers(self, text: str) -> list[float]:
        """Numbers of an answer or message text (harness_core.numeric: thousands separators, signs, %)."""
        return numbers_in(text or "")

    def extract_final(self, final_message: str) -> str:
        return extract_final(final_message)

    # -- ledger
    def observations(self, command: str, output: str, *, source: str, call: int, t: float,
                     next_ref: Callable[[], str], raw_command: str = "") -> list[C.Observe]:
        return observe.observations(command, output, source=source, call=call, t=t, next_ref=next_ref,
                                    tables=self.tables, raw_command=raw_command)

    # -- rules
    def predicates(self) -> Mapping[str, C.Predicate]:
        return dict(rules.PREDICATES)

    def offers(self) -> Mapping[str, C.Offer]:
        return dict(rules.OFFERS)

    def seed_rules(self) -> list[C.Rule]:
        data = json.loads((HERE / "seed_rules.json").read_text(encoding="utf-8"))
        return [C.Rule(**r) for r in data]

    def seed_notes(self) -> list[C.NoteSpec]:
        data = json.loads((HERE / "seed_notes.json").read_text(encoding="utf-8"))
        return [C.NoteSpec(**n) for n in data]

    def seed_text(self) -> str:
        text = "OfficeQA table notes:\n" + "".join(f"- {n.text}\n" for n in self.seed_notes())
        if len(text.encode("utf-8")) > SEED_TEXT_MAX:
            raise ValueError("seed text exceeds 4 KB")
        return text

    def leak_screen(self, text: str, released_answers: Sequence[str]) -> list[str]:
        return leak.leak_screen(text, released_answers)

    def task_conventions(self) -> str:
        return TASK_CONVENTIONS

    def usage(self) -> str:
        """README block describing the L1 tools (arms with l1_tools)."""
        return USAGE
