"""OfficeQA pack predicates and offers (DESIGN §2 L4, pack part).

Calling conventions (harness_core/engine.py): `pred(ctx, **args) -> list[Binding]`,
`offer(ctx, binding, **args) -> OfferResult | None`.  Pure and deterministic given the ledger events, the
corpus and the tables index; each predicate is bounded (at most `max_cells` linked cells, bounded printings
scans) to stay well under 2 s.  Only events before the candidate are used (harness_core.predicates.facts).

Linking (`linked_cells`): every number of the candidate answer is traced to
  (a) the latest calc whose printed value rounds to it -> the calc's non-trivial inputs -> the Observe each
      input was linked to by the ledger (or, when the ledger left it unlinked, the latest earlier Observe
      showing exactly that value), following calc-of-calc links up to depth 3; or
  (b) no calc: the latest Observe whose value rounds to it (the answer typed straight from a cell, possibly
      rescaled by 10^k).
An Observe from show_table/printings already carries its cell attrs; a raw corpus line (rg/sed/read) inside
a pipe table is resolved to the one cell of that row with the linked value (ambiguous -> skipped).

Predicates (binding keys; every binding also has `value` = the candidate token (numeric.NumTok.as_dict +
index), `compute` {ref, depth} ('' when typed directly) and, when an offer can use it, `substitutions`):
  cell_flags(flags=[...])      cell{...}, flag{name, why}, printing{...}? — flags from the vocabulary FLAG_NAMES;
                               plain r/p (revised/preliminary) flags never trigger (effect review P6).
  later_printing_differs()     cell{...}, printing{locator, issue, text}; skipped when spec.pins_date.
  period_mismatch()            cell{...}, question{period, periods}, alt{locator, text, period}; FY/CY aware.
  better_table()               table{used, used_ref, other, other_ref, words}.
  row_coverage()               row{locator, period, used, unused_n, unused}.
`cell` = {ref, locator, table, column (short_column, <= 24 chars), column_full, period, text, issue, flags}.
Offers: substitute_printing / substitute_period re-execute the recorded calc with the substituted cell
(harness_core.offers.recompute: sandboxed ComputeRunner, same formatting as the candidate) or, for a
typed answer, replace the number directly.
"""
from __future__ import annotations

import re
from typing import Any

try:
    from harness_core import contracts as C
except ImportError:                                   # pragma: no cover
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from harness_core import contracts as C
from harness_core import offers as core_offers
from harness_core.numeric import replace_number, rounds_to, short
from harness_core.predicates import facts, from_computes, token_dict

from .tools import otables

MAX_DEPTH = 3
MIN_TYPED_SIG = 3
FLAG_NAMES = ("repair", "Y?", "identity_fail", "year_inferred", "seq_break", "label_unreliable", "ocr_suspect",
              "period_unparsed")
NEVER_FLAGS = ("r", "p")                              # revisions are later_printing's domain
FLAG_WHY = {"repair": "a repaired row ({detail})", "Y?": "a month row whose year is not printed",
            "identity_fail": "a row failing the table's total identity (OCR?)",
            "year_inferred": "a row whose year was inferred ({detail})", "seq_break": "a row that breaks the period sequence",
            "label_unreliable": "an unreliable column label ({detail})",
            "ocr_suspect": "a cell with OCR damage ({detail})", "period_unparsed": "a row whose period label did not parse"}
SUM_WORDS = re.compile(r"\b(sum|total|totals|combined|aggregate|altogether|add(?:ing|ed)?\s+up|all\s+(?:listed|individual))\b", re.I)
EXCEPT_RX = re.compile(r"\b(?:except|excluding|exclude|other than|not including|without)\b([^.;?]*)", re.I)
INDIRECT_PERIOD = re.compile(r"\b(?:the|that)\s+(?:calendar\s+|fiscal\s+)?(?:year|month|quarter)\s+(?:that|when|in which|during which|of the|marking)"
                             r"|\byear of the\b|\bduring which\b|\bthe year the\b|\bin which year\b", re.I)
# a question about a change (YoY inflation, growth, month-over-month) needs the base period one step before the
# period it names: such a cell is not a period mismatch
CHANGE_WORDS = re.compile(r"\b(?:change[sd]?|growth|grew|grow|increase[sd]?|decrease[sd]?|decline[sd]?|inflation|rate|"
                          r"prior|previous|preceding|earlier|compared|difference|yoy|mom)\b|-over-|\bover\s+the\b", re.I)
GENERIC_WORDS = {"total", "amount", "value", "values", "data", "reported", "nominal", "public", "federal", "government",
                 "table", "sum", "difference", "absolute", "change", "rate", "growth", "average", "mean", "ratio",
                 "percent", "share", "calculate", "compute", "series", "each", "across", "annual", "monthly", "states",
                 "united", "treasury", "office", "report", "reports", "bulletin", "net", "gross", "department",
                 "agency", "agencies", "administration", "bureau", "service", "services", "fund", "funds",
                 "account", "accounts", "branch", "other", "items", "item", "calculation", "calculations", "entries"}


def _tables(ctx) -> "otables.Tables":
    t = getattr(ctx.pack, "tables", None)
    if t is not None:
        return t
    # a pack without `.tables`: one env-configured Tables per context, not a new sqlite connection per call
    if "_oq_tables" not in ctx.__dict__:
        ctx.__dict__["_oq_tables"] = otables.Tables()
    return ctx.__dict__["_oq_tables"]


def short_column(label: str, n: int = 24) -> str:
    """Compact column name for finding texts (<= n chars): the last ' > ' level (with the printed '(k)'
    number kept), so the decisive values of a <= 200-byte finding are never clipped away by a long label."""
    parts = [x.strip() for x in str(label or "").split(" > ") if x.strip()]
    if not parts:
        return ""
    last = parts[-1]
    if re.fullmatch(r"\(\d{1,2}\)", last) and len(parts) > 1:
        last = f"{parts[-2]} {last}"
    if len(last) <= n:
        return last
    num = re.search(r"\s*\(\d{1,2}\)$", last)
    tail = num.group(0) if num else ""
    return last[:max(4, n - len(tail) - 1)].rstrip() + "…" + tail


def _close(a, b) -> bool:
    return a is not None and b is not None and abs(float(a) - float(b)) <= 1e-9 * max(1.0, abs(float(a)), abs(float(b)))

# ---------------------------------------------------------------------------------------------
# linking the candidate to cells
# ---------------------------------------------------------------------------------------------


def _obs_values(o: C.Observe) -> list:
    vals = [o.value] if o.value is not None else []
    vals += [v for v in (o.attrs or {}).get(C.VALUES_ATTR) or [] if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return vals


def _source_value(inp: dict) -> float | None:
    v = inp.get("value")
    if not isinstance(v, (int, float)):
        return None
    return float(v) / 10.0 ** int(inp.get("scale") or 0)


def resolve_cell(ctx, o: C.Observe, target: float | None, decimals: int | None = None) -> dict | None:
    """Cell record for an Observe: its own attrs when it is a displayed cell, else the one cell of a raw
    table line whose |value| matches |target| (exactly, or rounded to `decimals`)."""
    at = o.attrs or {}
    if at.get("oq.col") and at.get("oq.table_ref"):
        return {"ref": o.ref, "locator": o.locator, "text": o.text, "value": o.value, "attrs": dict(at)}
    if not at.get("oq.table_ref") or target is None:
        return None
    tables = _tables(ctx)
    try:
        fname, hdr = at["oq.table_ref"].rsplit(":", 1)
        t = tables.table_at(fname, int(hdr))
        line = int(o.locator.rsplit(":", 1)[1])
        cells = tables.row_cells(t, line)
    except Exception:                                 # noqa: BLE001
        return None
    exact = [c for c in cells if c["value"] is not None and _close(abs(c["value"]), abs(target))]
    hits = exact or ([c for c in cells if c["value"] is not None and rounds_to(c["value"], target, decimals)]
                     if decimals is not None else [])
    if len({c["attrs"]["oq.col"] for c in hits}) != 1:
        return None
    rec = dict(hits[0])
    rec["ref"] = o.ref
    return rec


def _latest_observe(f, value: float, decimals: int | None, before: int) -> tuple | None:
    """Latest Observe before position `before` showing `value` (exactly, else rounded to `decimals`),
    also after a 10^k rescaling (k in 0, ±3, ±6, ±2).  -> (observe, source value, scale)"""
    obs = [e for e in f.observes if f.pos.get(e.ref, 0) < before][::-1]
    for k in (0, 3, -3, 6, -6, 2, -2):
        target = value / 10.0 ** k
        for o in obs:
            for v in _obs_values(o):
                if _close(abs(v), abs(target)):
                    return o, v, k
        if decimals is not None:
            for o in obs:
                for v in _obs_values(o):
                    if rounds_to(v * 10.0 ** k, value, decimals) and v != 0:
                        return o, v, k
    return None


def linked_cells(ctx, max_links: int = 40) -> list:
    """[{tok, tok_index, compute, depth, input, observe, cell, source_value, scale}] (see module doc)."""
    f = facts(ctx)
    cache = ctx.__dict__.setdefault("_oq_links", {})
    key = (len(f.prior), ctx.candidate.ref, ctx.candidate.answer)
    if key in cache:
        return cache[key]
    out = []
    for i, tok in enumerate(f.tokens):
        comps = from_computes(f, tok)
        if comps:
            k0, _ = comps[0]
            todo, seen = [(k0, 1)], set()
            while todo and len(out) < max_links:
                k, depth = todo.pop(0)
                if k.ref in seen:
                    continue
                seen.add(k.ref)
                for inp in k.inputs:
                    if inp.get("trivial"):
                        continue
                    src = f.by_ref.get(inp.get("observe") or "")
                    sv = _source_value(inp)
                    if isinstance(src, C.Compute):
                        if depth < MAX_DEPTH:
                            todo.append((src, depth + 1))
                        continue
                    if src is None and sv is not None:
                        got = _latest_observe(f, sv, None, f.pos.get(k.ref, len(f.prior)))
                        if got and got[2] == 0:
                            src = got[0]
                    if not isinstance(src, C.Observe):
                        continue
                    cell = resolve_cell(ctx, src, sv)
                    out.append({"tok": tok, "tok_index": i, "compute": k, "depth": depth, "input": inp,
                                "observe": src, "cell": cell, "source_value": sv, "scale": int(inp.get("scale") or 0)})
        elif tok.sig >= MIN_TYPED_SIG:
            # typed straight from a cell: exact value (after 10^k), >= 3 significant digits, so a computed
            # answer does not link to an unrelated cell that happens to round to it
            got = _latest_observe(f, tok.value, None, len(f.prior))
            if got:
                o, v, scale = got
                cell = resolve_cell(ctx, o, v, None)
                out.append({"tok": tok, "tok_index": i, "compute": None, "depth": 0, "input": None, "observe": o,
                            "cell": cell, "source_value": v, "scale": scale})
    cache[key] = out[:max_links]
    return cache[key]


def _cell_view(link: dict) -> dict:
    rec = link["cell"]
    at = rec["attrs"]
    return {"ref": rec["ref"], "locator": rec["locator"], "table": at.get("oq.table", ""),
            "column": short_column(at.get("oq.column", "")), "column_full": str(at.get("oq.column", ""))[:80],
            "period": str(at.get("oq.period", "") or at.get("oq.row", ""))[:16],
            "text": rec["text"], "issue": at.get("oq.issue", ""),
            "flags": ",".join(x for x in at.get("oq.flags", []) if x not in NEVER_FLAGS)[:80]}


def _base(link: dict) -> dict:
    k = link["compute"]
    return {"value": token_dict(link["tok"], link["tok_index"]),
            "compute": {"ref": k.ref if k else "", "depth": link["depth"]},
            "cell": _cell_view(link)}


def _subs(link: dict, new_value: float, label: str) -> list:
    return [{"from": link["observe"].ref, "value": float(new_value), "label": label}]


def _cell_links(ctx, max_cells: int) -> list:
    out, seen = [], set()
    for link in linked_cells(ctx):
        if not link["cell"] or link["cell"]["value"] is None:
            continue
        key = (link["cell"]["locator"], link["cell"]["attrs"].get("oq.col"), link["tok_index"])
        if key in seen:
            continue
        seen.add(key)
        out.append(link)
        if len(out) >= max_cells:
            break
    return out

# ---------------------------------------------------------------------------------------------
# flags
# ---------------------------------------------------------------------------------------------


def flag_hits(attrs: dict) -> dict:
    """{flag name: detail} for one cell record's attrs (vocabulary FLAG_NAMES)."""
    fl = [str(x) for x in attrs.get("oq.flags") or []]
    per = str(attrs.get("oq.period") or "")
    out = {}
    rep = [x for x in fl if x.startswith(otables.REPAIR_PREFIXES)]
    if rep or attrs.get("oq.repair"):
        out["repair"] = attrs.get("oq.repair") or " ".join(rep)
    if "YEAR_UNKNOWN" in fl or "Y?" in per:             # a 'Y?-MM' column resolved by the row year is fine
        out["Y?"] = per
    if attrs.get("oq.identity_fail") or "IDENTITY_FAIL" in fl:
        out["identity_fail"] = "identity"
    inf = [x for x in fl if "inferred" in x or x.startswith("year from")]
    if inf:
        out["year_inferred"] = inf[0]
    if any(x.startswith("SEQ") or x == "YEAR_BACKWARDS" for x in fl):
        out["seq_break"] = next(x for x in fl if x.startswith("SEQ") or x == "YEAR_BACKWARDS")
    lab = [x for x in fl if x in ("label_unreliable", "label_realigned", "header_shift")]
    if lab:
        out["label_unreliable"] = lab[0]
    ocr = [x for x in fl if x in ("trail_digit?", "text") or x.startswith("multi:") or x.endswith("/?")]
    if ocr:
        out["ocr_suspect"] = ocr[0]
    if "PERIOD?" in fl:
        out["period_unparsed"] = "period label"
    return out


def cell_flags(ctx, *, flags=("repair", "Y?", "identity_fail"), with_printing: bool = True, max_cells: int = 6) -> list:
    """Linked cells carrying one of `flags` (never r/p).  With `with_printing`, a later printing of the
    same cell whose value differs is attached as `printing` + `substitutions` (offer substitute_printing)."""
    wanted = [x for x in (flags or ()) if x in FLAG_NAMES and x not in NEVER_FLAGS]
    if not wanted:
        return []
    out = []
    for link in _cell_links(ctx, max_cells):
        hits = flag_hits(link["cell"]["attrs"])
        name = next((w for w in wanted if w in hits), None)
        if not name:
            continue
        b = _base(link)
        b["flag"] = {"name": name, "why": FLAG_WHY[name].format(detail=str(hits[name])[:18])[:48],
                     "check": _check_phrase(link, name)}
        if with_printing and not ctx.spec.pins_date:
            pr = _later_printing(ctx, link)
            if pr:
                b["printing"] = pr
                b["substitutions"] = _subs(link, pr["value"], f"{pr['locator']} = {pr['text']}")
                b["flag"]["check"] = f"The latest printing {pr['locator']} shows {pr['text']}."[:80]
        out.append(C.Binding(b))
    return out


def _check_phrase(link: dict, name: str) -> str:
    """A concrete next step (<= 80 chars) for a flagged cell - never 'are you sure'."""
    at = link["cell"]["attrs"]
    per = str(at.get("oq.row_period") or "")
    num, _ = otables.column_key(at.get("oq.column") or "")
    col = f"({num})" if num else short_column(at.get("oq.column") or "", 12)
    if name == "Y?" or per.startswith("Y?"):
        return "Anchor the year with a SIBLINGS printing listed under show_table NOTES."
    if _PRINTABLE_PERIOD.match(per) and at.get("oq.table"):
        return f'Compare: printings --table {at["oq.table"][:12]} --row {per} --col "{col}"'[:80]
    return "Compare it with another printing of this table (find_tables, printings)."

# ---------------------------------------------------------------------------------------------
# later printing
# ---------------------------------------------------------------------------------------------
_PRINTABLE_PERIOD = re.compile(r"^(?:(?:FY|CY)?\d{4}|\d{4}-\d\d)$")


def _later_printing(ctx, link) -> dict | None:
    rec = link["cell"]
    at = rec["attrs"]
    if not _PRINTABLE_PERIOD.match(str(at.get("oq.row_period") or "")) or rec["value"] is None:
        return None
    cache = ctx.__dict__.setdefault("_oq_printings", {})
    key = (rec["locator"], at.get("oq.table_ref"), at.get("oq.col"))
    if key not in cache:
        try:
            cache[key] = _tables(ctx).latest_printing(rec)
        except Exception:                             # noqa: BLE001
            cache[key] = None
    lp = cache[key]
    if not lp or lp.get("ambiguous") or lp.get("value") is None or _close(lp["value"], rec["value"]):
        return None
    return {"locator": lp["locator"], "issue": lp["issue"], "text": lp["text"], "value": lp["value"]}


def later_printing_differs(ctx, *, max_cells: int = 3) -> list:
    """A linked cell (row period FY/CY year or YYYY-MM) has a later printing whose value differs.
    Silent when the question pins a publication date (task convention: latest printing otherwise)."""
    if ctx.spec.pins_date:
        return []
    out = []
    for link in _cell_links(ctx, max_cells):
        pr = _later_printing(ctx, link)
        if not pr:
            continue
        b = _base(link)
        b["printing"] = pr
        b["substitutions"] = _subs(link, pr["value"], f"{pr['locator']} = {pr['text']}")
        out.append(C.Binding(b))
    return out

# ---------------------------------------------------------------------------------------------
# periods (FY / CY aware)
# ---------------------------------------------------------------------------------------------


def parse_period(p: str):
    """('FY'|'CY'|'Y', year) for annual periods, ('M', year, month) for months/days, else None
    (estimates, fiscal-year-to-date, unknown years and ranges are not compared)."""
    p = str(p or "").strip()
    if not p or "Y?" in p or " est" in p or "to date" in p or "range" in p or p.endswith("?"):
        return None
    m = re.fullmatch(r"(FY|CY)?(\d{4})", p)
    if m:
        return (m.group(1) or "Y", int(m.group(2)))
    m = re.fullmatch(r"(\d{4})-(\d\d)(?:-\d\d)?~?", p)
    if m and 1 <= int(m.group(2)) <= 12:
        return ("M", int(m.group(1)), int(m.group(2)))
    return None


def fy_months(year: int) -> tuple:
    """(start, end) month index (year*12+month-1) of federal fiscal year `year`: July-June through FY1976,
    October-September from FY1977."""
    if year <= 1976:
        return (year - 1) * 12 + 6, year * 12 + 5
    return (year - 1) * 12 + 9, year * 12 + 8


def _span(p) -> tuple:
    kind = p[0]
    if kind == "M":
        i = p[1] * 12 + p[2] - 1
        return i, i
    if kind == "FY":
        return fy_months(p[1])
    return p[1] * 12, p[1] * 12 + 11


def consistent(cell_p, q_p) -> bool:
    """Is a cell period compatible with a question period?  FY vs CY of the same number is a mismatch;
    an unstated basis ('Y') matches either; months must fall inside the question's year/fiscal year, and a
    month question accepts only that month or an annual cell covering it."""
    if cell_p is None or q_p is None:
        return True
    ck, qk = cell_p[0], q_p[0]
    if ck in ("FY", "CY") and qk in ("FY", "CY"):
        return ck == qk and cell_p[1] == q_p[1]
    if ck == "M" and qk == "M":
        return cell_p[1:] == q_p[1:]
    a0, a1 = _span(cell_p)
    b0, b1 = _span(q_p)
    if qk == "M":
        return a0 <= b0 <= a1
    if ck == "M":
        return b0 <= a0 <= b1
    return not (a1 < b0 or b1 < a0)                   # 'Y' against FY/CY/Y: overlapping years


def _alt_period(cell_p, q_ps: list):
    """The question period to substitute: same year with the question's basis (FY<->CY), else the only
    question period of the cell's granularity."""
    same_year = [q for q in q_ps if q[0] in ("FY", "CY") and cell_p[0] in ("FY", "CY") and q[1] == cell_p[1] and q[0] != cell_p[0]]
    if len(same_year) == 1:
        return same_year[0]
    gran = [q for q in q_ps if (q[0] == "M") == (cell_p[0] == "M")]
    if len(gran) == 1:
        return gran[0]
    return None


def _fmt_period(p) -> str:
    if p is None:
        return ""
    return f"{p[1]}-{p[2]:02d}" if p[0] == "M" else (f"{p[0]}{p[1]}" if p[0] != "Y" else str(p[1]))


def step_before(cell_p, q_ps: list) -> bool:
    """Is the cell period the base of a change to some question period: the month 1 or 12 months earlier, or
    the year 1 earlier (same basis, or basis unstated on either side)?"""
    for q in q_ps:
        if cell_p[0] == "M" and q[0] == "M":
            if (q[1] * 12 + q[2]) - (cell_p[1] * 12 + cell_p[2]) in (1, 12):
                return True
        elif cell_p[0] != "M" and q[0] != "M" and (cell_p[0] == q[0] or "Y" in (cell_p[0], q[0])) \
                and q[1] - cell_p[1] == 1:
            return True
    return False


def period_mismatch(ctx, *, require_alt: bool = True, max_cells: int = 6) -> list:
    """A linked cell's period is compatible with none of the question's periods (spec.periods).  The
    alternative is the cell of the same table part and column labelled with the question's period."""
    q_ps = [p for p in (parse_period(x) for x in ctx.spec.periods or ()) if p]
    if not q_ps or INDIRECT_PERIOD.search(ctx.question or ""):
        return []
    change = bool(CHANGE_WORDS.search(ctx.question or ""))
    tables = _tables(ctx)
    out = []
    for link in _cell_links(ctx, max_cells):
        at = link["cell"]["attrs"]
        cp = parse_period(at.get("oq.period"))
        if cp is None or any(consistent(cp, q) for q in q_ps) or (change and step_before(cp, q_ps)):
            continue
        qa = _alt_period(cp, q_ps)
        alt = None
        if qa is not None:
            try:
                fname, hdr = at["oq.table_ref"].rsplit(":", 1)
                t = tables.table_at(fname, int(hdr))
                col = int(str(at["oq.col"]).lstrip("c"))
                want = _fmt_period(qa)
                cands = [c for c in tables.column_cells(t, col)
                         if parse_period(c["attrs"]["oq.period"]) == parse_period(want) and c["value"] is not None]
                if cands and len({c["value"] for c in cands}) == 1:
                    alt = cands[0]
            except Exception:                         # noqa: BLE001
                alt = None
        if alt is None and require_alt:
            continue
        b = _base(link)
        b["question"] = {"period": _fmt_period(qa) or ", ".join(ctx.spec.periods[:3]),
                         "periods": ", ".join(ctx.spec.periods[:4]) + (" …" if len(ctx.spec.periods) > 4 else "")}
        if alt is not None:
            b["alt"] = {"locator": alt["locator"], "text": alt["text"], "period": alt["attrs"]["oq.period"]}
            b["substitutions"] = _subs(link, alt["value"], f"{alt['locator']} = {alt['text']}")
        out.append(C.Binding(b))
    return out

# ---------------------------------------------------------------------------------------------
# better table / row coverage
# ---------------------------------------------------------------------------------------------


def _stem(w: str) -> str:
    return w[:-1] if len(w) > 4 and w.endswith("s") and not w.endswith("ss") else w


def better_table(ctx, *, min_gain: int = 2, min_ratio: float = 1.5, min_score: int = 3) -> list:
    """All linked cells come from one table, and another data table of the same issue matches clearly more
    of the question's content words in its title/subtitle/section/header (conservative thresholds)."""
    refs = {link["cell"]["attrs"]["oq.table_ref"] for link in linked_cells(ctx) if link["cell"]}
    if len(refs) != 1:
        return []
    ref = next(iter(refs))
    fname, hdr = ref.rsplit(":", 1)
    words = [_stem(w) for w in ctx.spec.keywords if w not in GENERIC_WORDS and len(w) > 3]
    words = list(dict.fromkeys(words))
    if len(words) < min_score:
        return []
    try:
        rows = _tables(ctx).con().execute(
            "SELECT hdr_line, tid, tid_raw, title, subtitle, section, header, squash, kind, part FROM t WHERE file=? "
            "ORDER BY hdr_line", (fname,)).fetchall()
    except Exception:                                 # noqa: BLE001
        return []

    def score(r):
        text = " " + " ".join(_stem(x) for x in otables.norm(" ".join(str(v or "") for v in r[1:7])).split()) + " "
        return [w for w in words if f" {w} " in text or w in (r[7] or "")]
    used = next((r for r in rows if r[0] == int(hdr)), None)
    if used is None:
        return []
    used_hits = score(used)
    best = None
    for r in rows:
        if r[0] == int(hdr) or r[8] != "data" or (r[1] and r[1] == used[1]) or (r[3] and r[3] == used[3]):
            continue
        hits = score(r)
        if best is None or len(hits) > len(best[1]):
            best = (r, hits)
    if not best:
        return []
    r, hits = best
    if len(hits) < min_score or len(hits) < len(used_hits) + min_gain or len(hits) < min_ratio * len(used_hits):
        return []

    def name(x):
        text = (x[1] or (f"{x[2]}. {x[3] or ''}" if x[2] else (x[3] or "(untitled)")))
        return text if len(text) <= 32 else text[:31] + "…"
    extra = [w for w in hits if w not in used_hits]
    return [C.Binding({"table": {"used": name(used), "used_ref": ref, "other": name(r), "other_ref": f"{fname}:{r[0]}",
                                 "words": ", ".join(extra[:5])[:26], "score": len(hits), "used_score": len(used_hits)}})]


def row_coverage(ctx, *, min_used: int = 2, max_list: int = 3) -> list:
    """Sum/total questions: >= min_used linked cells of one table row feed one calc, and other numeric cells
    of that row (not total columns, not columns the question excludes) are unused."""
    if not SUM_WORDS.search(ctx.question or ""):
        return []
    items = []                                        # 'except A, B, and C': each item's content words
    for m in EXCEPT_RX.finditer(ctx.question or ""):
        for part in re.split(r",|\band\b|\bor\b", m.group(1)):
            ws = {_stem(w) for w in otables.norm(part).split() if len(w) > 3 and w not in GENERIC_WORDS}
            if ws:
                items.append(ws)
    groups: dict = {}
    for link in linked_cells(ctx):
        if not link["cell"] or not link["compute"]:
            continue
        at = link["cell"]["attrs"]
        groups.setdefault((link["compute"].ref, at["oq.table_ref"], link["cell"]["locator"], at.get("oq.row_period")), []).append(link)
    out = []
    tables = _tables(ctx)
    for (kref, tref, loc, rper), links in groups.items():
        used_cols = {x["cell"]["attrs"]["oq.col"] for x in links}
        if len(used_cols) < min_used:
            continue
        try:
            fname, hdr = tref.rsplit(":", 1)
            t = tables.table_at(fname, int(hdr))
            a = tables.analysis(t)
            cells = [c for c in tables.row_cells(t, int(loc.rsplit(":", 1)[1])) if c["attrs"].get("oq.row_period") == rper]
        except Exception:                             # noqa: BLE001
            continue
        totals = {f"c{idn['total']}" for idn in a.get("identities", [])}
        unused = []
        for c in cells:
            cat = c["attrs"]
            lab = otables.norm(cat.get("oq.column", ""))
            if cat["oq.col"] in used_cols or cat["oq.col"] in totals or c["value"] in (None, 0.0) or "total" in lab.split():
                continue
            lab_words = {_stem(w) for w in lab.split()}
            if any(ws <= lab_words for ws in items):
                continue
            unused.append(c)
        if not unused:
            continue
        b = _base(links[0])
        b["row"] = {"locator": loc, "period": rper or links[0]["cell"]["attrs"].get("oq.row", ""), "used": len(used_cols),
                    "unused_n": len(unused),
                    "unused": "; ".join(f"{short_column(c['attrs']['oq.column'])} {c['text']}" for c in unused[:max_list])[:70]}
        out.append(C.Binding(b))
    return out

# ---------------------------------------------------------------------------------------------
# offers
# ---------------------------------------------------------------------------------------------


def substitute(ctx, binding, **args: Any):
    """Offer for bindings with `substitutions`: re-run the recorded calc (core recompute) or, when the answer
    was typed straight from the cell, replace that number (same decimals/format as the candidate)."""
    v = binding.values
    subs = v.get("substitutions") or []
    if not subs:
        return None
    comp = v.get("compute") or {}
    if comp.get("ref"):
        if int(comp.get("depth") or 1) != 1:
            return None                               # the calc that produced the answer did not read the cell itself
        return core_offers.recompute(ctx, binding, **args)
    tokd = v.get("value") or {}
    toks = facts(ctx).tokens
    i = tokd.get("index")
    if not isinstance(i, int) or not 0 <= i < len(toks):
        return None
    tok = toks[i]
    link = next((x for x in linked_cells(ctx) if x["tok_index"] == i and x["observe"].ref == subs[0]["from"]), None)
    if link is None or link["source_value"] in (None, 0):
        return None
    new = float(subs[0]["value"]) * 10.0 ** link["scale"]
    sign = -1 if (tok.value < 0) != (link["source_value"] < 0) else 1
    answer = replace_number(ctx.candidate.answer, tok, sign * new, core_offers.answer_decimals(ctx, i, tok.decimals))
    if not answer.strip() or answer == ctx.candidate.answer:
        return None
    label = subs[0].get("label") or short(new)
    return C.OfferResult(answer=answer, derivation=f"typed value replaced by {label}"[:200])


def substitute_printing(ctx, binding, **args):
    return substitute(ctx, binding, **args)


def substitute_period(ctx, binding, **args):
    return substitute(ctx, binding, **args)


PREDICATES = {"cell_flags": cell_flags, "later_printing_differs": later_printing_differs,
              "period_mismatch": period_mismatch, "better_table": better_table, "row_coverage": row_coverage}
OFFERS = {"substitute_printing": substitute_printing, "substitute_period": substitute_period}
