"""Re-derive the Observe events of one command from the output the model actually saw.

`observations(command, output, *, source, call, t, next_ref, tables, raw_command='')` (contracts.Pack.observations;
the pack binds `tables`).  `command` is the ledger's normalised command (whitespace collapsed); `raw_command` is
the command as issued: when given, it is parsed instead (`shell_script`: one simple command per line,
here-document bodies removed), so 'cd /corpus<newline>sed -n 10,20p f.txt' is two commands, not one.
`output` may carry Codex truncation markers (the pack declares handles_truncation): cut fragments are dropped here.

1. Pack tools (show_table / printings / find_tables, called directly, as `python .../tools/cli.py <tool>` or
   `python -m packs.officeqa.tools.cli <tool>`): the invocation is parsed with the CLI's own argument parser and
   re-run deterministically (otables.Page); one Observe per displayed cell whose output line is present,
   complete, in what the model saw.  So --from-row/--cols/--match/--grep, pipes such as `| head`, and
   Codex's head+tail truncation are respected without guessing.  Pages printed by loops or unparsable
   invocations are recovered from their self-describing first line ('TABLE ... | file:line | part k | page
   rows a-b of N', 'printings(row=..., table=..., ...)').
2. Other commands whose output shows corpus lines: rg/grep ('file:line:text', or 'line:text' for a file
   named in the command), officeqa.py read results ('N: text' inside printed dicts/JSON), officeqa.py search
   excerpts (matched inside the result's start_line..end_line), cat -n, and unnumbered sed -n 'a,bp' / head /
   tail / cat / awk 'NR>=a && NR<=b' over corpus files (aligned against the corpus).  Every emitted line is
   verified against the corpus text at that line; lines cut by a truncation marker are dropped.  One Observe
   per line (locator 'file:line', text <= 200 chars, attrs {'values': [...]}, 'oq.via', plus
   'oq.table_ref'/'oq.table' when the line lies inside a pipe table).
3. Every other complete output line with a number (custom python prints, loops over files) becomes a raw
   Observe (locator '<source>:L<n>', attrs {'values': [...], 'oq.via': 'raw'}) so grounding checks still see
   it.  Lines of types 2-3 are capped at 400 per command; tool-page lines never become raw lines.

Pure function of (command, output, corpus, index); never raises (a failure yields fewer observations).
Bounded work per command (the broker runs it outside the ledger lock but before relaying the next request):
at most MAX_TOOL_PAGES distinct tool re-runs, MAX_FALLBACK_PAGES recovered pages, MAX_RANGE_FILES aligned
files; printings scans are capped in otables.
"""
from __future__ import annotations

import ast
import bisect
import re
import shlex
from typing import Callable

try:
    from harness_core.contracts import Observe, VALUES_ATTR
except ImportError:                                   # pragma: no cover
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from harness_core.contracts import Observe, VALUES_ATTR

from .tools import cli, otables

LINE_CAP = 400
CELL_CAP = 4000
# The broker re-runs pack tools before it relays the next request: bound the work one command can cause.
MAX_TOOL_PAGES = 6          # distinct parsed tool invocations re-run per command
MAX_FALLBACK_PAGES = 6      # self-describing first lines ('TABLE ...', 'printings(...)') recovered per command
MAX_RANGE_FILES = 8         # corpus files aligned for unnumbered sed/head/tail/cat/awk output
MAX_RANGE_LINES = 200_000   # corpus lines expected by those ranges
TEXT_MAX = 200
TRUNC_RX = re.compile(r"…\s*\d[\d,]*\s+(?:chars|characters|tokens|lines|bytes)\s+truncated\s*…"
                      r"|\.\.\.\s*\d[\d,]*\s+(?:chars|characters|tokens|lines|bytes)\s+truncated\s*\.\.\."
                      r"|\[\.\.\.\s*\d[\d,]*\s+(?:chars|lines|bytes)\s+truncated\s*\.\.\.\]")
CODEX_HEADER_RX = re.compile(r"^(?:Chunk ID: \S*|Wall time: .*|Process exited with code -?\d+|Process running with session ID .*"
                             r"|Original token count: \d+|Output:|Warning: truncated output.*|Total output lines: \d+)\s*$")
HEADER_RX = re.compile(r"^TABLE (.+?) \| (\S+?\.txt):(\d+) \| part (\d+) \| page rows (\d+)-(\d+) of (\d+)$")
PRINTINGS_RX = re.compile(r"^printings\(row=(.+?), table=(.+?), period=(None|'.*?'|\".*?\"), col=(None|'.*?'|\".*?\")\): ")
CORPUS_FILE_RX = re.compile(r"treasury_bulletin_\d{4}_\d{2}\.txt")
FILE_LINE_RX = re.compile(r"^(?:\S*/)?(treasury_bulletin_\d{4}_\d{2}\.txt)[:-](\d+)[:-](.*)$")
NUM_LINE_RX = re.compile(r"^\s*(\d{1,6})(?::\s?|\t|-)(.*)$")
FILENAME_KEY_RX = re.compile(r"""['"]filename['"]\s*:\s*['"]([^'"\s]+\.txt)['"]""")
SEPARATORS = {"&&", "||", ";", "|", "&", "(", ")", "\n", ";;", "|&"}
REDIRECTS = {">", ">>", "<", ">&", "&>", "<<", "<<<", ">|"}
PREFIXES = {"env", "command", "exec", "time", "nice", "nohup", "builtin"}
# `<<` / `<<-` opens a here-document; `<<<` (a here-string) does not (review fix: it used to swallow the rest)
HEREDOC_RX = re.compile(r"""(?<!<)<<(?!<)-?\s*(?:'([^'\n]+)'|"([^"\n]+)"|\\?([A-Za-z_][A-Za-z0-9_]*))""")


# ---------------------------------------------------------------------------------------------
# output as seen
# ---------------------------------------------------------------------------------------------
def seen_lines(output: str) -> list:
    """[(line, complete, cut_start, cut_end)] of the tool output after any Codex header; the fragments next
    to a truncation marker are incomplete (cut_start: text before it was lost; cut_end: text after it)."""
    lines = (output or "").replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines) and i < 12 and (CODEX_HEADER_RX.match(lines[i]) or
                                         (lines[i] == "" and i and lines[i - 1].startswith("Total output lines"))):
        i += 1
    pieces = TRUNC_RX.split("\n".join(lines[i:]))
    out = []
    for k, piece in enumerate(pieces):
        pl = piece.split("\n")
        for j, line in enumerate(pl):
            cut_start, cut_end = j == 0 and k > 0, j == len(pl) - 1 and k < len(pieces) - 1
            out.append((line, not (cut_start or cut_end), cut_start, cut_end))
    return out


def _complete_set(seen) -> set:
    return {s[0].rstrip() for s in seen if s[1]}


# ---------------------------------------------------------------------------------------------
# command parsing
# ---------------------------------------------------------------------------------------------
def shell_script(raw: str) -> str:
    """The command as issued, as one shell line for `segments`: here-document bodies removed,
    backslash-newline continuations joined, and every other newline outside quotes turned into ' ; ',
    so each script line is its own simple command (the ledger's normalised command has lost them)."""
    lines = (raw or "").replace("\r\n", "\n").split("\n")
    kept, i = [], 0
    while i < len(lines):
        line = lines[i]
        kept.append(line)
        i += 1
        for m in HEREDOC_RX.finditer(line):
            delim = next(g for g in m.groups() if g)
            while i < len(lines) and lines[i].strip() != delim:
                i += 1
            i += 1                                        # the delimiter line itself
    text, out, quote, k = "\n".join(kept), [], None, 0
    while k < len(text):
        ch = text[k]
        if quote == "'":
            out.append(ch)
            quote = None if ch == "'" else quote
        elif ch == "\\" and k + 1 < len(text):
            if text[k + 1] == "\n" and quote is None:     # line continuation
                k += 2
                continue
            out.append(text[k:k + 2])
            k += 2
            continue
        elif quote == '"':
            out.append(ch)
            quote = None if ch == '"' else quote
        elif ch in "'\"":
            out.append(ch)
            quote = ch
        elif ch == "\n":
            out.append(" ; ")
        else:
            out.append(ch)
        k += 1
    return "".join(out)


def segments(command: str) -> list:
    """Simple commands of a shell command line as token lists (quotes honoured, redirections removed)."""
    try:
        lex = shlex.shlex(command or "", posix=True, punctuation_chars=";&|()<>")
        lex.whitespace_split = True
        lex.commenters = ""
        toks = list(lex)
    except ValueError:
        return []
    segs, cur, skip = [], [], False
    for tok in toks:
        if skip:
            skip = False
            continue
        if tok in SEPARATORS:
            if cur:
                segs.append(cur)
            cur = []
            continue
        if tok in REDIRECTS or re.fullmatch(r"\d?[<>]{1,2}&?\d?", tok):
            skip = not re.fullmatch(r"\d?>&\d", tok)
            if cur and cur[-1] in ("0", "1", "2"):        # '2>&1' tokenises as '2', '>&', '1'
                cur.pop()
            continue
        cur.append(tok)
    if cur:
        segs.append(cur)
    return segs


def _strip_prefixes(seg: list) -> list:
    i = 0
    while i < len(seg):
        tok = seg[i]
        if tok in PREFIXES or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tok):
            i += 1
        elif tok == "timeout" and i + 1 < len(seg):
            i += 2
        else:
            break
    return seg[i:]


def tool_invocation(seg: list):
    """(tool, args) when the simple command runs one of the pack tools, else None."""
    seg = _strip_prefixes(seg)
    if not seg:
        return None
    base = seg[0].rsplit("/", 1)[-1]
    if base in cli.TOOLS:
        return base, seg[1:]
    if re.fullmatch(r"python(?:3(?:\.\d+)?)?", base) and len(seg) >= 3:
        if seg[1] == "-m" and re.search(r"officeqa(?:\.tools)?\.cli$", seg[2]) and len(seg) >= 4 and seg[3] in cli.TOOLS:
            return seg[3], seg[4:]
        if seg[1].endswith("cli.py") and seg[2] in cli.TOOLS:
            return seg[2], seg[3:]
        if seg[1].rsplit("/", 1)[-1] in cli.TOOLS:            # python .harness/bin/show_table ...
            return seg[1].rsplit("/", 1)[-1], seg[2:]
    return None


def invocations(command: str) -> list:
    """[(tool, args)] of the pack tool calls in a command line.  Without the raw command the ledger's
    normalised command has its whitespace collapsed, so 'show_table a 1<newline>show_table b 2' arrives as
    one simple command: args are split again at a later token naming a pack tool."""
    out = []
    for seg in segments(command):
        inv = tool_invocation(seg)
        if not inv:
            continue
        tool, args = inv
        cuts = [i for i, x in enumerate(args) if x.rsplit("/", 1)[-1] in cli.TOOLS]
        if not cuts:
            out.append((tool, args))
            continue
        out.append((tool, args[:cuts[0]]))
        for a, b in zip(cuts, cuts[1:] + [len(args)]):
            nxt = tool_invocation(args[a:b])
            if nxt:
                out.append(nxt)
    return out


# ---------------------------------------------------------------------------------------------
# tool pages
# ---------------------------------------------------------------------------------------------
def _page(tables, tool, args):
    try:
        return cli.page(tool, cli.parser(tool).parse_args(args), tables)
    except Exception:                                 # noqa: BLE001 - a re-derivation failure only loses observations
        return None


def _page_cells(page, complete: set):
    if page is None:
        return [], 0
    return ([c for c in page.cells if page.lines[c.line].rstrip() in complete],
            sum(1 for x in page.lines if x.rstrip() in complete))


def _fallback_pages(tables, seen, explained: set):
    """Pages recovered from self-describing first lines that no parsed invocation explained."""
    out = []
    complete = _complete_set(seen)
    tried = 0
    for line, ok, *_ in seen:
        line = line.rstrip()
        if not ok or line in explained:
            continue
        m = HEADER_RX.match(line)
        if m or PRINTINGS_RX.match(line):
            if tried >= MAX_FALLBACK_PAGES:
                explained.add(line)           # a page header is never a raw value line
                continue
            tried += 1
        if m:
            fname, hdr, a = m.group(2), int(m.group(3)), int(m.group(5))
            best = None
            for kw in ({}, {"stitch": True}, {"long": True}, {"long": True, "stitch": True}):
                try:
                    pg = tables.show_page(fname, hdr, from_row=max(0, a - 1), **kw)
                except Exception:                     # noqa: BLE001
                    continue
                if not pg.lines or pg.lines[0] != line:
                    continue
                cells, n = _page_cells(pg, complete)
                if best is None or n > best[1]:
                    best = (cells, n, pg)
            if best:
                out.append(best[0])
                explained.update(x.rstrip() for x in best[2].lines if x.rstrip() in complete)
            continue
        m = PRINTINGS_RX.match(line)
        if m:
            try:
                row, table = ast.literal_eval(m.group(1)), ast.literal_eval(m.group(2))
                period = None if m.group(3) == "None" else ast.literal_eval(m.group(3))
                col = None if m.group(4) == "None" else ast.literal_eval(m.group(4))
                pg = tables.printings_page(row, table, period, col)
            except Exception:                         # noqa: BLE001
                continue
            out.append(_page_cells(pg, complete)[0])
            explained.update(x.rstrip() for x in pg.lines if x.rstrip() in complete)
    return out


# ---------------------------------------------------------------------------------------------
# raw corpus lines
# ---------------------------------------------------------------------------------------------
def _files_in(command: str) -> list:
    seen = []
    for f in CORPUS_FILE_RX.findall(command or ""):
        if f not in seen:
            seen.append(f)
    return seen


def _ranges_for(seg: list, nlines: Callable[[str], int]):
    """[(file, first, last)] for sed -n / head / tail / cat / awk NR ranges over corpus files, else []."""
    seg = _strip_prefixes(seg)
    if not seg:
        return []
    prog = seg[0].rsplit("/", 1)[-1]
    names = [CORPUS_FILE_RX.search(x).group(0) for x in seg[1:] if CORPUS_FILE_RX.search(x)]
    if not names:
        return []
    out = []
    if prog == "sed" and "-n" in seg:
        scripts = [x for x in seg[1:] if not x.startswith("-") and not CORPUS_FILE_RX.search(x)]
        if len(names) != 1 or not scripts:
            return []
        for part in re.split(r"[;\n]", scripts[0]):
            part = part.strip()
            m = re.fullmatch(r"(\d+)(?:,(\d+|\$|\+\d+))?\s*\{?\s*p\s*;?\s*\}?", part)
            if not m:
                if part:
                    return []
                continue
            a, b = int(m.group(1)), m.group(2)
            last = a if b is None else (nlines(names[0]) if b == "$" else (a + int(b[1:]) if b.startswith("+") else int(b)))
            out.append((names[0], a, last))
        return out
    if prog == "head" and len(names) == 1:
        n = 10
        for i, x in enumerate(seg[1:], 1):
            if x == "-n" and i + 1 < len(seg) and seg[i + 1].isdigit():
                n = int(seg[i + 1])
            elif re.fullmatch(r"-n?\d+", x):
                n = int(x.lstrip("-n"))
        return [(names[0], 1, n)]
    if prog == "tail" and len(names) == 1:
        total = nlines(names[0])
        n, from_ = 10, None
        for i, x in enumerate(seg[1:], 1):
            v = seg[i + 1] if x == "-n" and i + 1 < len(seg) else (x[2:] if x.startswith("-n") and len(x) > 2 else
                                                                   (x[1:] if re.fullmatch(r"-\d+", x) else None))
            if v and v.startswith("+") and v[1:].isdigit():
                from_ = int(v[1:])
            elif v and v.isdigit():
                n = int(v)
        return [(names[0], from_ if from_ else max(1, total - n + 1), total)]
    if prog == "cat" and not any(x.startswith("-") for x in seg[1:]):
        return [(f, 1, nlines(f)) for f in names[:MAX_RANGE_FILES]]
    if prog == "awk" and len(names) == 1:
        script = next((x for x in seg[1:] if "NR" in x), "")
        m = re.search(r"NR\s*(>=?|==)\s*(\d+)\s*(?:&&|,)\s*NR\s*(<=?|==)\s*(\d+)", script)
        if m:
            a = int(m.group(2)) + (1 if m.group(1) == ">" else 0)
            b = int(m.group(4)) - (1 if m.group(3) == "<" else 0)
            return [(names[0], a, b)]
    return []


def _unescape(s: str) -> str:
    return (s.replace("\\\\", "\x00").replace("\\n", "\n").replace("\\t", "\t").replace("\\'", "'")
            .replace('\\"', '"').replace("\x00", "\\"))


def line_records(tables, seen, command: str, consumed: set | None = None, files_from: str | None = None) -> list:
    """[(file, line, displayed text, how)] for verified corpus lines in the output.  `consumed` receives the
    indices of the seen lines that produced a record.  Corpus file names are taken from `files_from`
    (default `command`; the raw command keeps names that only a here-document body mentions)."""
    recs, lines_of = [], {}
    consumed = consumed if consumed is not None else set()

    def corpus(f):
        if f not in lines_of:
            try:
                lines_of[f] = tables.lines(f)
            except Exception:                         # noqa: BLE001
                lines_of[f] = None
        return lines_of[f]

    def verify(f, n, text):
        ls = corpus(f)
        if not ls or not 1 <= n <= len(ls):
            return False
        want, got = ls[n - 1].rstrip(), text.rstrip()
        if got == want:
            return True
        # a fragment (rg -o, cut columns) counts only when every number it shows is a number of that line,
        # so '45' cut out of '1,245.3' (or a forged 'file:line:' prefix) never becomes a corpus value
        return len(got.strip()) >= 3 and got.strip() in want and \
            set(otables.numbers(got)) <= set(otables.numbers(want))

    cmd_files = _files_in(command if files_from is None else files_from)
    # printed dicts/JSON keep their content on one physical line: mark cut ends so only the cut logical
    # lines are dropped
    body = "\n".join(("\x01" if cs else "") + line + ("\x01" if ce else "") for line, _, cs, ce in seen)
    keys = list(FILENAME_KEY_RX.finditer(body))
    regions = [(m.group(1), m.end(), keys[i + 1].start() if i + 1 < len(keys) else len(body))
               for i, m in enumerate(keys)]
    for i, (line, ok, *_) in enumerate(seen):
        if not ok:
            continue
        m = FILE_LINE_RX.match(line)
        if m and verify(m.group(1), int(m.group(2)), m.group(3)):
            recs.append((m.group(1), int(m.group(2)), m.group(3), "grep"))
            consumed.add(i)
            continue
        m = NUM_LINE_RX.match(line)
        if m and cmd_files:
            n, text = int(m.group(1)), m.group(2)
            hits = [f for f in cmd_files[:8] if verify(f, n, text)]
            if len(hits) == 1:
                recs.append((hits[0], n, text, "numbered"))
                consumed.add(i)
    phys = body.split("\n")
    offsets, pos = [], 0
    for x in phys:
        offsets.append(pos)
        pos += len(x) + 1
    for fname, a, b in regions:
        region = body[a:b]
        ms = re.search(r"""['"]start_line['"]\s*:\s*(\d+)""", region)
        me = re.search(r"""['"]end_line['"]\s*:\s*(\d+)""", region)
        lo, hi = (int(ms.group(1)), int(me.group(1))) if ms and me else (None, None)
        window = {}
        if lo is not None and corpus(fname) and 0 < hi - lo < 400:
            for n in range(max(1, lo), min(hi, len(corpus(fname))) + 1):
                window.setdefault(corpus(fname)[n - 1].rstrip(), n)
        got = False
        parts = TRUNC_RX.split(region)
        for k, part in enumerate(parts):
            pl = _unescape(part).split("\n")
            for j, piece in enumerate(pl):
                if (j == 0 and k > 0) or (j == len(pl) - 1 and k < len(parts) - 1) or "\x01" in piece:
                    continue
                piece = re.sub(r"""^.*?['"]content['"]\s*:\s*['"]""", "", piece)
                piece = re.sub(r"""['"]\s*,\s*['"](?:truncated|total_lines|end_line)['"].*$|['"]\s*}\s*,?\s*$""", "", piece)
                m = re.match(r"^\s*(\d{1,6}): (.*)$", piece)
                if m and verify(fname, int(m.group(1)), m.group(2)):
                    recs.append((fname, int(m.group(1)), m.group(2), "read"))
                    got = True
                elif len(piece.strip()) >= 8 and piece.rstrip() in window:
                    recs.append((fname, window[piece.rstrip()], piece, "search"))
                    got = True
        if got:
            first = max(i for i, o in enumerate(offsets) if o <= a)
            last = max(i for i, o in enumerate(offsets) if o <= max(a, b - 1))
            consumed.update(range(first, last + 1))
    expected, files = [], []
    for seg in segments(command):
        for f, a, b in _ranges_for(seg, lambda f: len(corpus(f) or ())):
            if f not in files and len(files) >= MAX_RANGE_FILES:
                continue
            ls = corpus(f)
            if ls:
                files.append(f)
                expected += [(f, n) for n in range(max(1, a), min(b, len(ls)) + 1)][:MAX_RANGE_LINES - len(expected)]
    if expected:
        # in-order alignment: each seen line takes the first expected line at or after the previous match with
        # the same text (a text -> positions index keeps it O(lines log lines) instead of O(seen x expected))
        where: dict = {}
        for q, (f, n) in enumerate(expected):
            where.setdefault(corpus(f)[n - 1].rstrip(), []).append(q)
        taken = {(f, n) for f, n, _, _ in recs}
        p = 0
        for i, (line, ok, *_) in enumerate(seen):
            if not ok or not line.strip() or p >= len(expected):
                continue
            qs = where.get(line.rstrip())
            j = bisect.bisect_left(qs, p) if qs else 0
            if not qs or j >= len(qs):
                continue
            q = qs[j]
            f, n = expected[q]
            if (f, n) not in taken:
                recs.append((f, n, line, "range"))
                taken.add((f, n))
                consumed.add(i)
            p = q + 1
    return recs


# ---------------------------------------------------------------------------------------------
def observations(command: str, output: str, *, source: str, call: int, t: float, next_ref: Callable[[], str],
                 tables, raw_command: str = "") -> list:
    try:
        return _observations(command, output, source=source, call=call, t=t, next_ref=next_ref, tables=tables,
                             raw_command=raw_command)
    except Exception:                                 # noqa: BLE001 - never break the ledger
        return []


def _observations(command, output, *, source, call, t, next_ref, tables, raw_command=""):
    shell = shell_script(raw_command) if (raw_command or "").strip() else command
    files_from = raw_command if (raw_command or "").strip() else command
    seen = seen_lines(output)
    complete = _complete_set(seen)
    groups, explained, done = [], set(), set()
    for inv in invocations(shell):
        key = (inv[0], tuple(inv[1]))
        if key in done or len(done) >= MAX_TOOL_PAGES:
            continue
        done.add(key)
        pg = _page(tables, *inv)
        if pg is None:
            continue
        groups.append(_page_cells(pg, complete)[0])
        explained.update(x.rstrip() for x in pg.lines if x.rstrip() in complete)
    groups += _fallback_pages(tables, seen, explained)
    events, keys = [], set()
    for cells in groups:
        for c in cells:
            key = (c.locator, c.attrs.get("oq.table_ref"), c.attrs.get("oq.col"), c.text, c.attrs.get("oq.kind"))
            if key in keys or len(events) >= CELL_CAP:
                continue
            keys.add(key)
            attrs = dict(c.attrs)
            if c.value is None and "oq.kind" not in attrs and VALUES_ATTR not in attrs:
                attrs[VALUES_ATTR] = otables.numbers(c.text)
            events.append(Observe(kind="observe", ref=next_ref(), call=call, t=t, source=source, locator=c.locator,
                                  text=c.text[:TEXT_MAX], value=c.value, attrs=attrs))
    rest = [(x, ok and x.rstrip() not in explained, cs, ce) for x, ok, cs, ce in seen]
    n_lines = 0
    consumed: set = set()
    for fname, n, text, how in line_records(tables, rest, shell, consumed, files_from=files_from):
        if n_lines >= LINE_CAP:
            break
        loc = f"{fname}:{n}"
        key = (loc, None, None, text.strip(), "line")
        if key in keys:
            continue
        keys.add(key)
        attrs = {VALUES_ATTR: otables.numbers(text), "oq.file": fname, "oq.issue": otables.issue_of(fname), "oq.via": how}
        try:
            tb = tables.table_containing(fname, n)
        except Exception:                             # noqa: BLE001
            tb = None
        if tb is not None:
            attrs["oq.table_ref"] = f"{tb['file']}:{tb['hdr_line']}"
            attrs["oq.table"] = otables.display_name(tb)
        events.append(Observe(kind="observe", ref=next_ref(), call=call, t=t, source=source, locator=loc,
                              text=text.strip()[:TEXT_MAX], value=None, attrs=attrs))
        n_lines += 1
    # anything else with numbers the model saw (custom prints, loops over files): raw lines, so grounding
    # checks still see those values; locator '<source>:L<line of the output>'
    for i, (line, ok, *_) in enumerate(rest):
        if n_lines >= LINE_CAP:
            break
        if not ok or i in consumed or not line.strip():
            continue
        vals = otables.numbers(line, limit=64)
        if not vals:
            continue
        events.append(Observe(kind="observe", ref=next_ref(), call=call, t=t, source=source, locator=f"{source}:L{i + 1}",
                              text=line.strip()[:TEXT_MAX], value=None, attrs={VALUES_ATTR: vals, "oq.via": "raw"}))
        n_lines += 1
    return events
