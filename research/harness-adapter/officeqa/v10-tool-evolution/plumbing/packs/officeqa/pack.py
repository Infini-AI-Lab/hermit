"""OfficeQAPack: the OfficeQA implementation of contracts.Pack (v10 DESIGN §1, §3, §4).

Wiring only: W0 tools -> tools/cli.py + tools/otables.py (copied into the harness W0 adapter's tools/ by
arms.w0_files; the served copy, never this directory, is what an actor reads), question_spec -> spec.py,
observations -> observe.py (re-derived from the tables index with the frozen W0 tools), predicates/offers
-> rules.py, seed rules -> seed_rules.json, leak screen -> leak.py, answer span of a final message ->
extract_final.  Corpus and index paths default to OFFICEQA_CORPUS / TABLES_INDEX and are only touched when
a method needs them (constructing the pack is free).

Seed text (DESIGN §4, W0-PROPOSAL §(a)-(c)): HARNESS_SEED and PROMPT_SEED are the two W0 SEED.md texts,
byte for byte the proposal's; SEED_KNOWLEDGE is the proposal's parity checklist K1..K13, one sentence per
architecture, each present verbatim in its SEED. `seed_text_harness()` names the four harness tools;
`seed_text_prompt()` says the same for a workspace that has only officeqa.py, rg, sed and python, so it
names none of them. arms.check_w0 asserts every sentence is present in its SEED. The W0 adapters ship no
seed notes (notes/ is empty; the table notes are bullets of the SEED), so `seed_notes()` is empty.

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
W0_TOOL_FILES = ("cli.py", "otables.py")              # copied into the harness W0 tools/ (cli.py imports otables)
TOOL_NAMES = ("find_tables", "show_table", "printings")
SEED_TEXT_MAX = C.SEED_MAX
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
# tools/USAGE.md body of the harness W0 (arms.w0_files appends the calc lines): the v9 block plus the one-line
# facts of W0-PROPOSAL §(d) 'USAGE.md'. USAGE line rule (DESIGN scope decision 6): every non-empty, non-heading
# line names a manifest tool or an --option of the tool sources, so USAGE.md cannot become an always-visible
# prompt channel (arms.usage_violations checks it). Lines that describe an option task T adds (--title-only:
# W0-1, --section: W0-3, show_table --period: W0-6) are listed in W0-CONTENT.md so a reviewer can drop them
# when that change is deferred.
USAGE = (
    "Table tools (read-only): find_tables, show_table and printings print pages of at most 8,000 bytes;"
    " one query per call.\n"
    "  find_tables \"<words or table id>\" [--years 2003-2004] [--limit 25] [--all] [--title-only]\n"
    "      find_tables matches every word (prefix for words of 4+ letters) in the title, header or row labels\n"
    "      (--rows is the default; --title-only searches title and header only); on no full match\n"
    "      find_tables lists the best partial matches; each group line says which words matched (match:...)\n"
    "      find_tables prints the group lines first ([FD-1] title | issues | rows | as-of), then the latest 3\n"
    "      parts per group; --all lists every part (12 per group), --years and --limit narrow a cut page\n"
    "  show_table <file> <line> [--stitch] [--match <col words>] [--cols 1-3] [--grep <row>] [--period FY2021]"
    " [--long] [--raw] [--from-row N]\n"
    "      show_table takes <file> <line> (not file:line) and prints one table with filled periods (FY/CY/YYYY-MM),\n"
    "      column labels, repairs and identity checks under show_table's NOTES (read them on every page)\n"
    "      --grep is a case-insensitive substring of the row label, filled period or section ('|' = OR),\n"
    "      not a regex and not a column filter; --match keeps the columns whose label contains the words\n"
    "      --period FY2021 keeps the rows whose filled period falls in that year (12 rows for a fiscal year)\n"
    "      every page starts 'TABLE <id> | <file>:<line> | part k | page rows a-b of N'; --from-row is 0-based:\n"
    "      pass the number the continuation line prints; --stitch joins the parts of a multi-part table\n"
    "  printings --table <id or title words> --row <period or label> [--col <column words or (n)>] [--years A-B]"
    " [--section <words>]\n"
    "      printings lists the same row/column in every issue that prints it, SUMMARY first: revisions, first\n"
    "      and latest printing; printings has no --all\n"
    "      printings --row takes the row's period (2003-01, FY2003) or label and --col the column label or (n);\n"
    "      swap them for a table with periods as columns; printings --section keeps one panel of a stacked table\n"
    "  Quote multi-word values: show_table ... --match \"net receipts\", printings ... --col \"(3)\"\n"
    "  calc: compute every candidate (both readings, percent and decimal) in one run and pick from its printout\n")


def _seed(lines: Sequence[str]) -> str:
    """One SEED.md text from its lines (each bullet is one line)."""
    return "\n".join(lines) + "\n"


# The harness W0 SEED.md (W0-PROPOSAL §(a); the review amendment F8 "retry with fewer words" is folded into the
# proposal since 2026-09-29, so the text is the proposal's byte for byte; 4,053 bytes with the final newline): the
# benchmark-neutral
# advice + the OfficeQA table notes, naming the four harness tools. Each bullet is one line, written as
# concatenated fragments; tests/test_officeqa_pack.py pins the text to the proposal file when it is present.
HARNESS_SEED = _seed((
    "# Working advice",
    "",
    "- Compute every final number with `calc`, not in your head, and answer with the value it printed, "
    "rounded as the question asks.",
    "- Provenance: use numbers a tool printed. Before you rely on a value, print the line it comes from, so "
    "the number in your calculation is the number in the source. For a table printed in year blocks, use the "
    "block's printed Total row.",
    "- Answer count and format: give exactly as many values as the question asks for, in its order and "
    "format (a bracketed list when it asks for one). The final line is a number or a list, never a sentence; "
    "if a value cannot be found, answer your best documented number.",
    "- Answer form: the metric the question names fixes what you report (a normalized difference, share, "
    "rate or change is that metric, not the raw difference). Multiply by 100 only when the answer clause "
    "says percent, percentage or %; a rate or ratio with no percent cue is a decimal. Input units (\"in "
    "percent\") do not set the output form.",
    "- Alternatives: when two candidates fit (tables, sections, rows, printings, readings), name both, "
    "compute both with calc, and answer the one the question's wording selects; never average. Candidates "
    "include other tables and narrative sections, not only neighbouring columns; more decimals is not a "
    "reason.",
    "- Corpus boundary: the corpus is Treasury Bulletins only. A series that is not a Bulletin table (a "
    "price index, a market price, a chart's plotted bars) is not in it: one `find_tables` and one `rg` "
    "settle that; then stop searching, state your assumption and answer.",
    "",
    "OfficeQA table notes:",
    "- Bulletins revise earlier numbers. Unless the question names an issue, publication date, quotation "
    "date or \"as of\" date, use the latest printing of each value: read the `printings` SUMMARY line for "
    "every value that enters your answer. When it names one, use each figure as that issue prints it; do not "
    "replace it with a later revision.",
    "- In `printings`, a value that only the last printing shows while earlier printings agree, or a value "
    "whose column index differs from the other issues, is an OCR variant, not a revision. A table titled "
    "\"Preliminary\" or a one-date snapshot is a first printing; the revised value is in the section's "
    "time-series table.",
    "- Match the period exactly: FY and CY rows with the same year number are different rows; \"est.\" and \"to "
    "date\" rows are not the year's row; a fiscal year has exactly 12 month rows. Check the filled period of "
    "every cell you use.",
    "- Read `show_table`'s NOTES on every page. A row flagged \"identity FAILS\", \"Y?\" or repaired: compare it "
    "with other printings and take the value most printings agree on that restores the row total; for \"Y?\" "
    "rows open the SIBLINGS printing to read the years. A header flagged shifted or spurious, or a column "
    "listed under different indexes across printings: map the numbered labels (1)..(n) onto the n numeric "
    "columns in order and confirm with the identity line before reading a column by its label.",
    "- Finding the table: search `find_tables` with the question's key nouns; on 0 groups or no good match "
    "retry with fewer words. Prefer the table whose row or column label is the question's noun "
    "phrase, then the title. A title naming one component (\"... - General\") is not the total: for \"total X\" "
    "find the summary table with an \"X > Total\" column. Two tables with one title: tell them apart by "
    "`show_table`'s section line. A question naming a report or section may mean narrative text: `rg -n -i` "
    "the phrase and read that section.",
    "- Entity = its own column: an agency, program or item the question names is the ONE column (or row) "
    "whose label names it, not a parent Total beside it and not a neighbouring \"Other\" column, unless the "
    "question says \"total\", \"including\" or lists the parts. Words that repeat the table title or units line "
    "describe the table, not extra columns. A qualifier narrower than a total row (\"subject to duty\" vs "
    "\"dutiable and taxable\") means the footnoted component it excludes is subtracted from every operand: "
    "compute both readings and answer the one that honours every word.",
))

# The prompt W0 SEED.md (W0-PROPOSAL §(b), 4,092 bytes): the same knowledge for a workspace with officeqa.py
# (FTS5 search helper), rg, sed and python, and none of the harness tools (arms.check_w0 refuses a prompt
# SEED that names one).
PROMPT_SEED = _seed((
    "# Working advice",
    "",
    "- Compute every final number with `python`, not in your head, and answer with the value it printed, "
    "rounded as the question asks.",
    "- Provenance: use numbers you have seen in the corpus text. Before you rely on a value, print the line "
    "it comes from with `sed -n`, so the number you compute with is the number in the source. For a table "
    "printed in year blocks, use the block's printed Total row.",
    "- Answer count and format: give exactly as many values as the question asks for, in its order and "
    "format (a bracketed list when it asks for one). The final line is a number or a list, never a sentence; "
    "if a value cannot be found, answer your best documented number.",
    "- Answer form: the metric the question names fixes what you report (a normalized difference, share, "
    "rate or change is that metric, not the raw difference). Multiply by 100 only when the answer clause "
    "says percent, percentage or %; a rate or ratio with no percent cue is a decimal. Input units (\"in "
    "percent\") do not set the output form.",
    "- Alternatives: when two candidates fit (tables, sections, rows, issues, readings), name both, compute "
    "both with python, and answer the one the question's wording selects; never average. Candidates include "
    "other tables and narrative sections, not only neighbouring columns; more decimals is not a reason.",
    "- Corpus boundary: the corpus is Treasury Bulletins only. A series that is not a Bulletin table (a "
    "price index, a market price, a chart's plotted bars) is not in it: one `officeqa.py` search and one "
    "`rg` settle that; then stop searching, state your assumption and answer.",
    "",
    "OfficeQA table notes:",
    "- Bulletins revise earlier numbers. Unless the question names an issue, publication date, quotation "
    "date or \"as of\" date, use the latest printed value: `rg -n` the table title or id across all issues, "
    "open that row in the latest issues printing it, and take the latest value. When it names one, use each "
    "figure as that issue prints it; do not replace it with a later revision.",
    "- A value that only the last issue shows while earlier issues agree, or a value that sits in a "
    "different column position than in the other issues, is an OCR variant, not a revision. A table titled "
    "\"Preliminary\" or a one-date snapshot is a first print; the revised value is in the section's "
    "time-series table.",
    "- Match the period exactly: FY and CY rows with the same year number are different rows; \"est.\" and \"to "
    "date\" rows are not the year's row; a fiscal year has exactly 12 month rows. Check the year banner above "
    "every row you use.",
    "- Check every row you use: if its cells do not sum to the printed total, or its year is not printed, "
    "compare it with the same table in a neighbouring issue and take the value most issues agree on that "
    "restores the row total; read missing years from that issue's header. If the header has more labels than "
    "numeric columns, map the numbered labels (1)..(n) onto the n numeric columns in order and confirm with "
    "a row total before reading a column by its label.",
    "- Finding the table: `rg -n -i` the question's key nouns over the corpus; on no hits retry with fewer "
    "words and with row-label words. Prefer the table whose row or column label is the question's noun "
    "phrase, then the title. A title naming one component (\"... - General\") is not the total: for \"total X\" "
    "find the summary table with an \"X > Total\" column. Two tables with one title: tell them apart by the "
    "section banner above them. A question naming a report or section may mean narrative text: `rg -n -i` "
    "the phrase and read that section.",
    "- Entity = its own column: an agency, program or item the question names is the ONE column (or row) "
    "whose label names it, not a parent Total beside it and not a neighbouring \"Other\" column, unless the "
    "question says \"total\", \"including\" or lists the parts. Words that repeat the table title or units line "
    "describe the table, not extra columns. A qualifier narrower than a total row (\"subject to duty\" vs "
    "\"dutiable and taxable\") means the footnoted component it excludes is subtracted from every operand: "
    "compute both readings and answer the one that honours every word.",
))


def _item(id: str, harness: str, prompt: str | None = None) -> dict[str, str]:
    """One parity item; the prompt sentence defaults to the harness sentence ('same' in the proposal's table)."""
    return {"id": id, "harness": harness, "prompt": harness if prompt is None else prompt}


# The parity checklist (DESIGN §4; W0-PROPOSAL §(c) K1..K13 in order): one sentence per knowledge item for
# each W0 SEED, quoted from the proposal's table. arms.check_w0 asserts every sentence is present
# (whitespace-normalised) in the respective SEED.md. K5 joins the table's two quoted fragments, which are
# adjacent in both SEEDs.
SEED_KNOWLEDGE: list[dict[str, str]] = [
    _item("compute_with_code",                                                                              # K1
          "Compute every final number with `calc`, not in your head",
          "Compute every final number with `python`, not in your head"),
    _item("provenance",                                                                                     # K2
          "print the line it comes from, so the number in your calculation is the number in the source",
          "print the line it comes from with `sed -n`, so the number you compute with is the number in the source"),
    _item("answer_count", "The final line is a number or a list, never a sentence"),                        # K3
    _item("answer_form", "Multiply by 100 only when the answer clause says percent, percentage or %"),      # K4
    _item("alternatives", "never average. Candidates include other tables and narrative sections"),         # K5
    _item("latest_printing",                                                                                # K6
          "Unless the question names an issue, publication date, quotation date or \"as of\" date"),
    _item("ocr_variant", "is an OCR variant, not a revision"),                                              # K7
    _item("period_match", "a fiscal year has exactly 12 month rows"),                                       # K8
    _item("flagged_rows", "map the numbered labels (1)..(n) onto the n numeric columns in order"),          # K9
    _item("table_choice", "for \"total X\" find the summary table with an \"X > Total\" column"),           # K10
    _item("entity_column", "Entity = its own column"),                                                      # K11
    _item("corpus_boundary", "Corpus boundary: the corpus is Treasury Bulletins only"),                     # K12
    _item("narrative_sections", "may mean narrative text"),                                                 # K13
]


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


# The exposure record (DESIGN §4, W0-PROPOSAL §(d), W0-CONTENT §5): every W0 tool change with the development
# items it was designed from. `*` in the proposal marks issue-47 disputed items (DISPUTED below). The harness
# learner's prompt lists these changes so they are not re-proposed (harness_learning.status_section), and the
# analysis reports every contrast on all 50 questions and excluding EXPOSURE_ITEMS (PREREG §2, §5).
DISPUTED = ("UID0007", "UID0154", "UID0155", "UID0213", "UID0237")
W0_CHANGES: tuple[dict, ...] = (
    {"id": "W0-1", "tool": "find_tables",
     "what": "scope title+header+row labels by default, prefix tokens (4+ letters), ranked OR fallback with "
             "'best partial matches', a match: field per group, --title-only",
     "items": ("UID0182", "UID0029", "UID0133", "UID0180", "UID0218", "UID0237", "UID0149", "UID0225", "UID0117",
               "UID0211", "UID0170", "UID0077", "UID0243", "UID0097", "UID0224", "UID0245")},
    {"id": "W0-2", "tool": "find_tables",
     "what": "group-first listing ([FD-1] title | issues | rows | as-of), latest 3 parts per group, --all capped at "
             "12 per group, cut trailer, 'Table N.' title merge, [snapshot] tag",
     "items": ("UID0243", "UID0180", "UID0133", "UID0135", "UID0094", "UID0007", "UID0154", "UID0237", "UID0149",
               "UID0220", "UID0130")},
    {"id": "W0-3", "tool": "printings",
     "what": "SUMMARY keyed by (part, panel stub, column, period, as-of), --section, column-index warning, "
             "snapshot line",
     "items": ("UID0007", "UID0044", "UID0170", "UID0211", "UID0130", "UID0220", "UID0188", "UID0228", "UID0165",
               "UID0214", "UID0218")},
    {"id": "W0-4", "tool": "printings", "what": "exact SUMMARY values (Decimal-keyed groups, raw text, no %g)",
     "items": ("UID0180", "UID0135", "UID0122")},
    {"id": "W0-5", "tool": "printings",
     "what": "no-match diagnostics: printed span and last issue, --col miss with the column list, row window, "
             "periods-as-columns hint, date-keyed rows, intra-word spaces",
     "items": ("UID0114", "UID0218", "UID0057", "UID0097", "UID0122", "UID0148", "UID0109", "UID0180", "UID0225",
               "UID0170", "UID0101")},
    {"id": "W0-6", "tool": "show_table",
     "what": "--grep semantics and 0-row message, '|' = OR, month-name forms, --period FY2021, --from-row past-end "
             "message",
     "items": ("UID0170", "UID0224", "UID0237", "UID0100", "UID0112", "UID0102", "UID0057", "UID0149", "UID0214",
               "UID0220", "UID0182", "UID0007", "UID0245", "UID0109")},
    {"id": "W0-7", "tool": "show_table",
     "what": "non-table line message, part hint, figure caption, --long hint, 0-based --from-row help",
     "items": ("UID0094", "UID0037", "UID0214", "UID0154", "UID0029", "UID0133", "UID0148", "UID0112", "UID0077",
               "UID0211", "UID0225")},
    {"id": "W0-8", "tool": "show_table",
     "what": "header-shift repair with a strict trigger and both mappings printed; date-column label repair",
     "items": ("UID0165", "UID0148", "UID0213", "UID0214", "UID0077", "UID0228", "UID0102", "UID0243", "UID0101",
               "UID0225")},
    {"id": "W0-9", "tool": "show_table", "what": "NOTES printed in --raw",
     "items": ("UID0028", "UID0027", "UID0225", "UID0148")},
    {"id": "W0-10", "tool": "show_table", "what": "sibling year anchoring (YEARS ANCHORED from <file:line>)",
     "items": ("UID0028", "UID0029", "UID0027", "UID0117")},
    {"id": "W0-11", "tool": "printings", "what": "SUMMARY first, month keys exact unless --all-periods",
     "items": ("UID0224", "UID0062", "UID0077", "UID0155", "UID0135", "UID0114", "UID0122", "UID0243", "UID0101")},
    {"id": "W0-12", "tool": "find_tables", "what": "narrative section index ([section] block)",
     "items": ("UID0215", "UID0133", "UID0183", "UID0204", "UID0037")},
)
EXPOSURE_ITEMS: frozenset = frozenset(i for change in W0_CHANGES for i in change["items"])


def w0_changes_text() -> str:
    """The W0 tool changes as the harness learner sees them (one line each; no question ids: the learner
    prompt is model-visible and the exposure items are for the analysis, not for the model)."""
    return "\n".join(f"- {c['id']} {c['tool']}: {c['what']}" for c in W0_CHANGES) + "\n"


def exposure_record() -> dict:
    """The record PREREG copies (W0-PROPOSAL §(d)): per change its items, the disputed ones marked, and the
    union of targeted items every contrast is also reported without."""
    return {"changes": [{"id": c["id"], "tool": c["tool"], "what": c["what"], "items": list(c["items"]),
                         "disputed": [i for i in c["items"] if i in DISPUTED]} for c in W0_CHANGES],
            "targeted_items": sorted(EXPOSURE_ITEMS), "disputed": list(DISPUTED)}


def w0_tools() -> C.W0Tools:
    """The pack's part of the harness W0 `tools/`: copies of tools/cli.py and tools/otables.py and one manifest
    entry per tool ({name: {"script": "cli.py", "args": [name]}}; the multi-command CLI takes the tool name
    first). arms.w0_files adds calc.py, its manifest entry, manifest.json and USAGE.md. Episodes are served
    the adapter's copy (from a neutral code root), never this directory."""
    files = {name: (TOOLS_DIR / name).read_text(encoding="utf-8") for name in W0_TOOL_FILES}
    return C.W0Tools(files=files, manifest={name: {"script": "cli.py", "args": [name]} for name in TOOL_NAMES})


class OfficeQAPack:
    """contracts.Pack for OfficeQA (Treasury Bulletin text corpus)."""
    name = "officeqa"
    answer_kind = "text"
    tools: Mapping[str, str] = {"find_tables": "tools.cli:find_tables", "show_table": "tools.cli:show_table",
                                "printings": "tools.cli:printings"}
    handles_truncation = True
    max_observations = 4000
    SEED_KNOWLEDGE = SEED_KNOWLEDGE
    W0_CHANGES = W0_CHANGES
    EXPOSURE_ITEMS = EXPOSURE_ITEMS

    def w0_changes_text(self) -> str:
        return w0_changes_text()

    def exposure_record(self) -> dict:
        return exposure_record()

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
                     next_ref: Callable[[], str], raw_command: str = "", mode: str = "rerun") -> list[C.Observe]:
        """`mode` (v10 parser mode, harness_core.ledger): 'rerun' | 'header'; see observe.py."""
        return observe.observations(command, output, source=source, call=call, t=t, next_ref=next_ref,
                                    tables=self.tables, raw_command=raw_command, mode=mode)

    # -- rules
    def predicates(self) -> Mapping[str, C.Predicate]:
        return dict(rules.PREDICATES)

    def offers(self) -> Mapping[str, C.Offer]:
        return dict(rules.OFFERS)

    def seed_rules(self) -> list[C.Rule]:
        data = json.loads((HERE / "seed_rules.json").read_text(encoding="utf-8"))
        return [C.Rule(**r) for r in data]

    def seed_notes(self) -> list[C.NoteSpec]:
        """No seed notes: both W0 adapters ship SEED.md and an empty notes/ (DESIGN §4); the OfficeQA table
        notes are bullets of the SEED texts, and learned notes come from the learner, never from the pack."""
        return []

    def seed_text_harness(self) -> str:
        """The harness W0 SEED.md (W0-PROPOSAL §(a), byte for byte)."""
        return _checked_seed(HARNESS_SEED)

    def seed_text_prompt(self) -> str:
        """The prompt W0 SEED.md (W0-PROPOSAL §(b), byte for byte): the same knowledge, no harness tool name."""
        return _checked_seed(PROMPT_SEED)

    def w0_tools(self) -> C.W0Tools:
        return w0_tools()

    def leak_screen(self, text: str, released_answers: Sequence[str]) -> list[str]:
        return leak.leak_screen(text, released_answers)

    def task_conventions(self) -> str:
        return TASK_CONVENTIONS

    def usage(self) -> str:
        """tools/USAGE.md body of the harness W0 (the README tools section); the calc lines are added by arms."""
        return USAGE


def _checked_seed(text: str) -> str:
    if len(text.encode("utf-8")) > SEED_TEXT_MAX:
        raise ValueError(f"seed text exceeds {SEED_TEXT_MAX} bytes")
    return text
