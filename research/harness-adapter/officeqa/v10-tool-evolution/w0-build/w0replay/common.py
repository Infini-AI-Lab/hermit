"""Shared pieces of the W0 replay (task R): paths, ledger readers, a conservative shell splitter, argparse
checks through the tools' own parsers, correctness lookup and value masking.

Everything here is read-only on the pilots, the indexes and the tools.  Raw tool output is written only to
the scratchpad (`--out`), never into the work tree; the report shows diffs with numbers masked as <v>.

Vocabulary (for the report's reader): a *ledger* is the per-episode `api/ledger.jsonl` the v9 harness wrote
for controller arms (b = v9-p1-b, b2 = v9-p2-a); the text arms (a = v9-p1-a, c = v9-p1-c) have no ledger, so
their commands come from the Codex `records/events.jsonl` (`command_execution` items).  An *invocation* is
one direct call of a manifest tool (find_tables / show_table / printings) with its argv, after the shell line
the model typed has been split at `;`, `|`, `&&`, `||` and simple `for` loops have been expanded.
"""
from __future__ import annotations

import dataclasses
import importlib.util
import json
import os
import re
import shlex
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]                       # g-p-.../
WORK = Path(__file__).resolve().parents[1]                       # officeqa-pro133-v10-w0work
V9_TREE = ROOT / "officeqa-pro133-v9-plumbing"
V10_TREE = WORK
V9_CLI = V9_TREE / "packs" / "officeqa" / "tools" / "cli.py"
V10_CLI = V10_TREE / "packs" / "officeqa" / "tools" / "cli.py"
V9_INDEX = ROOT / "frameworks-v9" / "officeqa-tables-index" / "tables.sqlite"
V10_INDEX = ROOT / "frameworks-v10" / "officeqa-tables-index" / "tables.sqlite"
CORPUS = ROOT / "v9-p1-a" / "prepared" / "agent" / "corpus"
PYTHON = ROOT / "adapter-comparison-work" / ".venv" / "bin" / "python"
ANALYSIS_INDEX = ROOT / "officeqa-v10-analysis" / "index.json"
SCRATCH = Path(os.environ.get("W0REPLAY_OUT") or
               "/private/tmp/claude-501/-Users-albertzhao--codex--chatgpt-projects-g-p-6aa867f0012881918044d250b055e9dd/"
               "d5f9d16d-6f3c-43aa-ad00-9b12b862fc9b/scratchpad/w0replay")

PILOTS = {"a": "v9-p1-a", "b": "v9-p1-b", "c": "v9-p1-c", "b2": "v9-p2-a"}   # cell letter -> pilot directory
TOOLS = ("find_tables", "show_table", "printings")
SKIP_TOOLS = ("calc", "submit")
OPERATORS = {";", "|", "||", "&&", "&", "(", ")", "<", ">", ">>", "<<", "2>", "&>", "|&"}
SHELL_WORDS = {"do", "done", "then", "else", "fi", "elif", "if", "for", "while", "until", "in"}


@dataclasses.dataclass
class Record:
    """One shell line the model ran, with where it came from."""
    cell: str
    ordinal: int
    task_id: str
    correct: int | None        # 1 right, 0 wrong, None unknown (per officeqa-v10-analysis/index.json)
    call: int
    raw_cmd: str
    exit_code: int | None


@dataclasses.dataclass
class Invocation:
    """One direct manifest-tool call: argv and the records that produced it."""
    tool: str
    args: list
    sources: list = dataclasses.field(default_factory=list)      # [(cell, ordinal, task_id, correct, call, raw_cmd)]
    expanded: bool = False                                        # came out of a for-loop expansion

    @property
    def key(self):
        return (self.tool, tuple(self.args))

    @property
    def right(self) -> bool:
        return any(s[3] == 1 for s in self.sources)


# ---- correctness

def load_correct() -> dict:
    """{(cell, ordinal): (task_id, correct)} from the analysis index ('correct' by cell and ordinal)."""
    out = {}
    for q in json.loads(ANALYSIS_INDEX.read_text()):
        for cell, v in q["correct"].items():
            if cell in PILOTS:
                out[(cell, q["ordinal"])] = (q["task_id"], v)
    return out


# ---- ledger / event readers

def _episodes(cell: str):
    for ep in sorted((ROOT / PILOTS[cell] / "runs" / "episodes").iterdir()):
        if ep.name.isdigit():
            yield int(ep.name), ep


def _ledger_records(cell, ordinal, ep, task_id, correct):
    """Completed `command` rows of a controller ledger: the row with an exit code, one per tool call
    (`call` is the model request number and may carry several commands, so `call_id`/`n` disambiguate)."""
    path = ep / "api" / "ledger.jsonl"
    if not path.exists():
        return
    seen = set()
    for line in path.read_text().splitlines():
        r = json.loads(line)
        key = (r.get("call"), r.get("n"), r.get("call_id"))
        if r.get("kind") != "command" or r.get("exit_code") is None or key in seen:
            continue
        seen.add(key)
        yield Record(cell, ordinal, task_id, correct, r["call"], r["raw_cmd"], r["exit_code"])


def _unwrap_zsh(command: str) -> str | None:
    """`/bin/zsh -lc '<cmd>'` -> `<cmd>` (the Codex CLI wraps every shell line this way)."""
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    if len(parts) >= 3 and parts[0].endswith("zsh") and parts[1] == "-lc":
        return parts[2]
    return command


def _event_records(cell, ordinal, ep, task_id, correct):
    """Completed `command_execution` items of a text arm's Codex event log."""
    path = ep / "records" / "events.jsonl"
    if not path.exists():
        return
    call = 0
    for line in path.read_text().splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        it = r.get("item") or {}
        if r.get("type") != "item.completed" or it.get("type") != "command_execution":
            continue
        call += 1
        cmd = _unwrap_zsh(it.get("command", ""))
        if cmd is not None:
            yield Record(cell, ordinal, task_id, correct, call, cmd, it.get("exit_code"))


def iter_records(cells=("a", "b", "b2", "c")):
    """Every shell line of every episode of the chosen cells, with correctness attached."""
    correct = load_correct()
    for cell in cells:
        for ordinal, ep in _episodes(cell):
            task_id, ok = correct.get((cell, ordinal), (None, None))
            if (ep / "api" / "ledger.jsonl").exists():      # controller arms: the ledger is authoritative
                yield from _ledger_records(cell, ordinal, ep, task_id, ok)
            else:                                            # text arms: only the Codex event log exists
                yield from _event_records(cell, ordinal, ep, task_id, ok)


# ---- shell splitting

FOR_RX = re.compile(r"^\s*for\s+(\w+)\s+in\s+(.+?)\s*;\s*do\s+(.*?)\s*;?\s*done\s*;?\s*$", re.S)
SEQ_RX = re.compile(r"^\$\(\s*seq\s+(\d+)\s+(\d+)\s*\)$")


def _loop_values(list_text: str) -> list | None:
    """Literal words of a `for x in ...` list; `$(seq A B)` is expanded, anything else with `$` is refused."""
    m = SEQ_RX.match(list_text.strip())
    if m:
        return [str(i) for i in range(int(m.group(1)), int(m.group(2)) + 1)]
    try:
        words = shlex.split(list_text)
    except ValueError:
        return None
    return None if any("$" in w or "`" in w for w in words) else words


def expand_loops(raw: str) -> tuple:
    """([shell lines], expanded?): a single-level `for VAR in LIST; do BODY; done` becomes one line per
    value with $VAR / ${VAR} substituted; every other line is returned unchanged."""
    m = FOR_RX.match(raw)
    if not m:
        return [raw], False
    var, values, body = m.group(1), _loop_values(m.group(2)), m.group(3)
    if values is None or "for " in body or "while " in body:
        return [raw], False
    rx = re.compile(r"\$\{%s\}|\$%s\b" % (var, var))
    return [rx.sub(lambda _: v, body) for v in values], True


def split_newlines(text: str) -> list:
    """Physical lines of a shell text, cut only at newlines outside quotes (a newline is a command
    separator for the shell but plain whitespace for shlex)."""
    out, cur, quote = [], [], None
    for ch in text:
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "\n":
            out.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    out.append("".join(cur))
    return [x for x in out if x.strip()]


def _tokens(line: str) -> list | None:
    """shlex tokens of one line with operators kept as tokens (None when the line does not tokenize)."""
    lex = shlex.shlex(line, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    try:
        return list(lex)
    except ValueError:
        return None


def split_segments(text: str) -> list | None:
    """Simple commands of one shell text as token lists (None when it does not tokenize).  The text is cut
    at newlines and at every operator token; a redirection ends its segment.  Control words (`do`,
    `then` ...) at the start of a segment are dropped."""
    toks = []
    for line in split_newlines(text):
        lt = _tokens(line)
        if lt is None:
            return None
        toks += lt + [";"]
    segs, cur, redirected = [], [], False
    for t in toks:
        if t in OPERATORS or set(t) <= set("|&;()<>"):
            if t in ("<", ">", ">>", "<<", "2>", "&>"):
                if cur and cur[-1] in ("1", "2"):      # the fd of `2>/dev/null` tokenizes as a lone digit
                    cur.pop()
                redirected = True
                continue
            segs.append(cur)
            cur, redirected = [], False
            continue
        if redirected:
            continue
        cur.append(t)
    segs.append(cur)
    out = []
    for s in segs:
        while s and s[0] in SHELL_WORDS:
            s = s[1:]
        if s:
            out.append(s)
    return out


def tool_argvs(raw: str) -> tuple:
    """(list of (tool, args, expanded), reason) for the direct manifest-tool calls in one shell line;
    segments with unresolved `$`/backticks are refused (conservative)."""
    lines, expanded = expand_loops(raw)
    found, reason = [], None
    for line in lines:
        segs = split_segments(line)
        if segs is None:
            return [], "untokenizable"
        for s in segs:
            if s[0] not in TOOLS:
                continue
            if any("$" in t or "`" in t for t in s):
                reason = "unresolved variable"
                continue
            found.append((s[0], s[1:], expanded))
    return found, reason


# ---- the tools' own argparse

def load_cli(path: Path):
    """Import one tree's cli.py under a private module name; its `otables` sibling is purged first so the
    two trees never share a cached module (cli.py falls back to `import otables` from its own directory)."""
    for name in ("otables", "cli"):
        sys.modules.pop(name, None)
    sys.path.insert(0, str(path.parent))
    try:
        spec = importlib.util.spec_from_file_location(f"cli_{path.parent.parent.parent.parent.name}", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path.pop(0)
    return mod


def parses(cli, tool: str, args: list) -> bool:
    """Does `tool args` parse with this tree's argparse?  (`-h` counts as parsing.)"""
    if args[:1] in (["-h"], ["--help"]):
        return True
    try:
        cli.parser(tool).parse_args(list(args))
        return True
    except Exception:                                  # noqa: BLE001 - ToolError from _Parser.error, or a type error
        return False


# ---- masking for the report

KEEP_RX = re.compile(
    r"treasury_bulletin_\d{4}_\d{2}\.txt(?::\d+)?"                          # file and file:line
    r"|\b\d{4}_\d{2}\b"                                                     # issue id
    r"|\b[A-Z]{1,5}(?:-[IVX]+)?-\d+[A-Za-z]?\b"                             # table ids: FFO-3, GA-I-1, CM-I-2
    r"|\bTable \d+\b|\bpart ?\d+\b|\bp\d\b|\bc\d{1,3}\b|\bv\d{1,3}\b|\(\d{1,3}\)"
    r"|\b(?:FY|CY)?(?:18|19|20)\d{2}(?:-\d{2}|-[A-Z][a-z]{2,4}\.?)?\b"        # years, FY2005, 2005-06, 2005-Nov
    r"|\b[A-Z][a-z]{2,8}\.? \d{1,2}, (?:18|19|20)\d{2}\b|\b\d{1,2}/\d{1,2}/\d{4}\b"   # Sept. 30, 2000; 03/16/2016
    r"|\b\d+ (?:issues|parts|printings|rows|groups|lines|bytes|distinct|words|tokens|more|table parts|cells|labels|"
    r"numeric|narrative|months|continuation|repeats)\b"
    r"|\brows \d+-\d+ of \d+|\blines? \d+(?:-\d+)?|\b\d+c x \d+r\b|\bmatched \d+ of \d+|\b(?:latest|all|first) \d+(?: of \d+)?"
    r"|\bat \d+ bytes|\bcolumns? \d+|\brow \d+|\b\d+ of \d+ groups|\bcut at \d+")
NUM_RX = re.compile(r"(?<![A-Za-z_])[-+]?\d[\d,]*(?:\.\d+)?(?:[eE][-+]?\d+)?")
ROW_RX = re.compile(r"^(\d+\s+\d+\s+\S+(?: est| to date)?\s+)(.*)$")        # show_table ROWS line: keep line/panel/period
LISTING_RX = re.compile(r"^(\s+\d{4}_\d{2}\s+treasury_bulletin_\S+\s+p\d+\s+\S+.*? \| c\d+ '[^']*' = )(.*)$")
SUMMARY_VALUE_RX = re.compile(r"^(\s{6})(\S.*?)(: \d{4}_\d{2}\.\.\d{4}_\d{2} \(\d+\).*)$")


def _placeholder(i: int) -> str:
    """A digit-free token for the i-th kept span (letters only, so NUM_RX leaves it alone)."""
    letters = ""
    while True:
        letters = chr(0x41 + i % 26) + letters
        i //= 26
        if i == 0:
            return "\x00" + letters + "\x00"


def _mask_structural(text: str) -> str:
    """Mask every number except the structural ones KEEP_RX names."""
    keep = []

    def _hold(m):
        keep.append(m.group(0))
        return _placeholder(len(keep) - 1)
    masked = NUM_RX.sub("<v>", KEEP_RX.sub(_hold, text))
    for i, v in enumerate(keep):
        masked = masked.replace(_placeholder(i), v, 1)
    return masked


def mask_line(line: str) -> str:
    """One page line with its values masked: table rows keep line/panel/period and the label, printings
    listing lines keep everything up to `= `, SUMMARY value lines keep the issue span; the rest keeps the
    structural numbers of KEEP_RX."""
    m = ROW_RX.match(line)
    if m:
        head, rest = m.groups()
        label = re.split(r"\s{2,}", rest, 1)[0]
        return _mask_structural(head) + label + "  " + " ".join("<v>" for _ in re.split(r"\s{2,}", rest)[1:])
    m = LISTING_RX.match(line)
    if m:
        return _mask_structural(m.group(1)) + "<v>" + (" [flags]" if "[" in m.group(2) else "")
    m = SUMMARY_VALUE_RX.match(line)
    if m:
        return m.group(1) + "<v>" + _mask_structural(m.group(3))
    return _mask_structural(line)


def mask_values(text: str) -> str:
    """Whole page or argv with numbers masked as <v> (see mask_line)."""
    return "\n".join(mask_line(l) for l in text.split("\n"))


# ---- find_tables page structure (shared by replay.py and harm.py)

GROUP_RX = re.compile(r"^\[(?!section\])([^\]]*)\]\s*(.*?)\s*\|")
ISSUES_RX = re.compile(r"\|\s*(\d+) issues\b")
PART_RX = re.compile(r"^\s+(treasury_bulletin_\d{4}_\d{2}\.txt:\d+) part")
BLOCK_RX = re.compile(r"^  \[[^\]]*\] parts \(")
TABLE_N_RX = re.compile(r"^\s*table\s+\d+[a-z]?\s*[.:-]?\s*", re.I)


def parse_find_page(page: str) -> list:
    """Groups of a find_tables page in rank order: [{id, title, issues, parts}], for both layouts (v9: part
    lines follow their group line; v10 W0-2: all group lines first, then one `[id] parts (...)` block per
    group in the same order)."""
    groups, cur, blocks_seen = [], -1, 0
    for line in page.splitlines():
        m = GROUP_RX.match(line)
        if m:
            n = ISSUES_RX.search(line)
            groups.append({"id": m.group(1), "title": m.group(2), "issues": int(n.group(1)) if n else 0, "parts": []})
            cur = len(groups) - 1
            continue
        if BLOCK_RX.match(line):                    # v10: the k-th block belongs to the k-th group
            cur = min(blocks_seen, len(groups) - 1)
            blocks_seen += 1
            continue
        pm = PART_RX.match(line)
        if pm and groups:
            groups[cur]["parts"].append(pm.group(1))
    return groups



LEAK_RX = re.compile(r"(?<![\w:_./-])(?:\d{1,3}(?:,\d{3})+|\d+\.\d+)(?![\w/])")


def mask_argv(tool: str, args: list) -> str:
    """One shell-like line for a tool argv; only value-like tokens (thousands groups, decimals) are masked,
    file names, line numbers, years and option values are structural and stay."""
    toks = [tool] + [json.dumps(a) if " " in a or not a else a for a in args]
    return " ".join(LEAK_RX.sub("<v>", t) for t in toks)


def leak_scan(text: str) -> list:
    """Value-like tokens (thousands groups or decimals) that survived masking; the report must show none."""
    return sorted({m.group(0) for m in LEAK_RX.finditer(text)})


def run_env(index: Path) -> dict:
    """Environment for one tool subprocess (corpus + the index that tree was built for)."""
    return {**os.environ, "OFFICEQA_CORPUS": str(CORPUS), "TABLES_INDEX": str(index), "PYTHONDONTWRITEBYTECODE": "1"}
