"""OfficeQA question spec: what the answer must look like and which periods it is about.

`question_spec(question) -> contracts.QuestionSpec` generalises the replay parser
(officeqa-v9-design/understand/replay/checkers.py:174, replay.md §4):
  - n_values / list_format: stated counts ('3 comma-separated values', 'both values'), 'in the order of A, B
    and C', 'format [a, b]', ordinal slots ('first value ... second value'), 'slope and intercept',
    'starting with <year> ... ending with <year>' lists; 'a single value' forces 1.
  - decimals per value: 'N decimal places', 'nearest hundredths', 'nearest whole number', 'as an integer';
    phrases in a sentence about intermediate values are ignored, parenthesised ones are a fallback only;
    slots by ordinal words, else by position.  Significant-digit requests give None (not a decimals count).
  - percent_form: from the answer-form phrases outside parentheses ('as a decimal value' / 'in decimal form'
    -> decimal; 'as a percent(age) value' / 'percentage points' / 'percent per year' -> percent).
  - value_type: 'year' (which/what year), 'integer' (how many, single integer), else 'number'.
  - periods: normalised 'FY1955', 'CY2010', '1970-03', '1970' (bare year = basis not stated), ranges expanded
    (years <= 60, months <= 120), in order of appearance, without the dates of a pinned publication.
  - pins_date: the question names a publication/issue ('the June 1970 bulletin', 'published on ...',
    'reported by the U.S. Treasury in March 1941', 'as of reported values on the end of the 1982 FY').
    'most recently published' / 'latest' is not a pin (it states the default convention).
  - keywords: normalised content words (for note triggers and table matching).
  - allowed_words: single words the question quotes ("'surplus' or 'deficit'"), which the answer may hold
    as values (replay parser, understand/replay/checkers.py:259); core count_mismatch lets them fill slots.
Deterministic, stdlib only (plus the contracts dataclass).
"""
from __future__ import annotations

import re

try:
    from harness_core.contracts import QuestionSpec
except ImportError:                                   # pragma: no cover - the plumbing root is not on sys.path
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from harness_core.contracts import QuestionSpec

WORDNUM = {'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7, 'eight': 8, 'nine': 9,
           'ten': 10, 'both': 2}
MONTHS = {'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6, 'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10,
          'nov': 11, 'dec': 12}
MON = (r'(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?'
       r'|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?')
YEAR = r'(?:19|20)\d\d'
MONTHYEAR = MON + r'\s*(?:\d{1,2}(?:st|nd|rd|th)?,?\s*)?' + YEAR
MAX_YEARS, MAX_MONTHS = 60, 120

# ---------------------------------------------------------------------------------------------
# text helpers
# ---------------------------------------------------------------------------------------------


def _clean(q: str) -> str:
    q = (q or '').replace('’', "'").replace('‘', "'").replace('“', '"').replace('”', '"')
    q = re.sub(r'[‐-―−]', '-', q)
    return re.sub(r'\s+', ' ', q).strip()


def _w2n(s: str) -> int:
    s = s.lower()
    return int(s) if s.isdigit() else WORDNUM[s]


def _month(s: str) -> int:
    return MONTHS[s.lower().rstrip('.')[:3]]


def _sentences(q: str) -> list[tuple[int, str]]:
    out, start = [], 0
    for m in re.finditer(r'(?<=[.?!])\s+(?=[A-Z(])', q):
        out.append((start, q[start:m.start()]))
        start = m.end()
    out.append((start, q[start:]))
    return out


def _paren_spans(q: str) -> list[tuple[int, int]]:
    spans, stack = [], []
    for i, ch in enumerate(q):
        if ch == '(':
            stack.append(i)
        elif ch == ')' and stack:
            spans.append((stack.pop(), i))
    return spans


def _strip_parens(q: str) -> str:
    out, depth = [], 0
    for ch in q:
        if ch == '(':
            depth += 1
        elif ch == ')' and depth:
            depth -= 1
        elif not depth:
            out.append(ch)
    return re.sub(r'\s+', ' ', ''.join(out))

# ---------------------------------------------------------------------------------------------
# count of values
# ---------------------------------------------------------------------------------------------

ORDINALS = {'first': 0, 'second': 1, 'third': 2, 'fourth': 3, 'fifth': 4}


def n_values(q: str) -> tuple[int | None, bool]:
    """(number of values or None when a list of unknown length, list_format)."""
    ql = q.lower()
    n = None
    m = re.search(r'\b(two|three|four|five|six|2|3|4|5|6)\s+(?:comma[- ]separated\s+|final\s+|numeric\s+)?'
                  r'(?:values|answers|numbers)\b', ql)
    if m:
        n = _w2n(m.group(1))
    if n is None and re.search(r'\bboth (?:values|answers|final answers|numbers)\b|\breturn both\b|\breport both\b', ql):
        n = 2
    if n is None:
        m = re.search(r'in the order of ([^.?]+)', ql)
        if m and not re.search(r'sub-?questions?', m.group(1)):
            items = [x for x in re.split(r',\s*(?:and\s+)?|\s+and\s+', m.group(1).split(' rounded')[0]) if x.strip()]
            if len(items) >= 2:
                n = len(items)
    if n is None:
        m = re.search(r'format \[([^\]]+)\]', ql)
        if m:
            n = m.group(1).count(',') + 1
    if n is None and re.search(r'order of the sub-?questions', ql):
        k = len(re.findall(r'\bwhat\b', ql))
        n = k if k >= 2 else None
    if n is None:
        ords = re.findall(r'\b(first|second|third|fourth|fifth)\s+value', ql)
        if ords:
            n = max(2, max(ORDINALS[o] for o in ords) + 1)
    if n is None and re.search(r'(?:return|output|report)\s+(?:the\s+)?(?:linear regression\s+)?slope and (?:the\s+)?(?:y-)?intercept', ql):
        n = 2
    if n is None:
        m = re.search(r'starting with [^.]*?(\d{4})[^.]*?ending with [^.]*?(\d{4})', ql)
        if m and 'list' in ql:
            n = int(m.group(2)) - int(m.group(1)) + 1
    single = bool(re.search(r'\b(?:a|one|as a) single (?:numeric |final )?(?:value|number|integer|answer)\b|'
                            r'returned as a single value|as the single final', ql))
    if single and (n is None or n <= 1):
        n = 1
    list_format = bool(n and n > 1) or bool(re.search(r'comma[- ]separated|\bbrackets?\b|\bcomma separated list\b|'
                                                      r'\breturn(?:ed)? as a (?:comma separated )?list\b', ql))
    if single:
        list_format = False
    if n is None:
        n = None if list_format else 1
    return n, list_format

# ---------------------------------------------------------------------------------------------
# rounding
# ---------------------------------------------------------------------------------------------

_DEC_WORDS = [(r'hundred[- ]thousandths?', 5), (r'ten[- ]thousandths?', 4), (r'thousandths?', 3),
              (r'hundredths?', 2), (r'tenths?', 1)]
_DEC_RE = re.compile(
    r'(?P<n>\d+|one|two|three|four|five|six|seven|eight)\s+(?:nearest\s+)?decimal(?:s\b| places?| points?)'
    r'|to\s+(?P<n2>\d+|one|two|three|four|five|six)\s+decimals\b'
    r'|(?P<w>hundred[- ]thousandths?|ten[- ]thousandths?|thousandths?|hundredths?|tenths?)(?![- ]of\b)'
    r'|(?P<z>nearest\s+whole|whole\s+number|nearest\s+(?:nominal\s+)?dollar\b|nearest\s+million\b|'
    r'nearest\s+billion\b|single\s+integer|as\s+an\s+integer|nearest\s+integer)'
    r'|(?P<sf>\d+|three|four|five|six)\s+significant\s+(?:digits|figures)',
    re.I)


def rounding_specs(q: str) -> list[tuple[int, str, int]]:
    """(position, 'dp'|'sf', n) for rounding phrases about the final answer (replay.md §4 rules)."""
    parens = _paren_spans(q)
    sents = _sentences(q)
    out, in_paren = [], []
    for m in _DEC_RE.finditer(q):
        pos = m.start()
        sent = next(s for st, s in reversed(sents) if st <= pos)
        if re.search(r'intermediate', sent, re.I):
            continue
        target = in_paren if any(a < pos < b for a, b in parens) else out
        if m.group('sf'):
            target.append((pos, 'sf', _w2n(m.group('sf'))))
        elif m.group('n') or m.group('n2'):
            target.append((pos, 'dp', _w2n(m.group('n') or m.group('n2'))))
        elif m.group('w'):
            w = m.group('w').lower()
            target.append((pos, 'dp', next(n for pat, n in _DEC_WORDS if re.fullmatch(pat, w))))
        else:
            target.append((pos, 'dp', 0))
    return out or in_paren


def decimals_per_value(q: str, n: int | None) -> tuple[int | None, ...]:
    """Decimals per answer value (len == n when n is known, else 1 = 'every value'); None = not stated or
    ambiguous (e.g. significant digits)."""
    specs = rounding_specs(q)
    dps = [(p, k) for p, kind, k in specs if kind == 'dp']
    sfs = [k for p, kind, k in specs if kind == 'sf']
    nv = n or 1
    if not dps:
        if sfs:
            return (None,) * nv
        if nv == 1 and re.search(r'\bhow many\b', q, re.I):
            return (0,)
        return ()
    uniq = sorted({k for _, k in dps})
    ords = [(m.start(), ORDINALS[m.group(1)]) for m in re.finditer(r'\b(first|second|third|fourth|fifth)\s+value', q, re.I)]
    if nv > 1 and ords:
        slots: list[set] = [set() for _ in range(nv)]
        for p, k in dps:
            prev = [o for o in ords if o[0] <= p]
            if prev and prev[-1][1] < nv:
                slots[prev[-1][1]].add(k)
        out = []
        for sl in slots:
            sl = sl or set(uniq)
            out.append(next(iter(sl)) if len(sl) == 1 else None)
        return tuple(out)
    if len(uniq) == 1:
        return (uniq[0],) * nv
    if nv > 1 and len(dps) == nv:
        return tuple(k for _, k in dps)
    if nv > 1 and 1 < len(dps) < nv:
        # 'A rounded to x ... B and C rounded to y': earlier phrases take the first values, the last the rest
        ks = [k for _, k in dps]
        return tuple(ks[:-1] + [ks[-1]] * (nv - len(ks) + 1))
    return (None,) * nv

# ---------------------------------------------------------------------------------------------
# answer form
# ---------------------------------------------------------------------------------------------


def percent_form(q: str) -> str:
    t = _strip_parens(q.lower())
    dec = re.search(r'\bas a decimal value|in decimal form|\bdecimal form\b|outputted as a decimal|reported as a decimal'
                    r'|\bas a decimal\b(?! places?)', t)
    strong_dec = re.search(r'decimal value|decimal form', t)
    pct = re.search(r'as a percent(?:age)?(?! of)(?: value)?\b|expressed as a percent|as a percent value|in percentage points'
                    r'|percentage points|reported percentage value|percent per year|answer as percent|in percent\b(?! of)', t)
    if strong_dec:
        return 'decimal'
    if pct:
        return 'percent'
    if dec:
        return 'decimal'
    return 'unspecified'


_MEASURE = r'(?:\w+\s+){0,2}(?:standard deviations?|percentage points?|percent|basis points?|dollars?|millions?|billions?|units?)\b'


def value_type(q: str, n: int | None, decimals: tuple = ()) -> str:
    ql = q.lower()
    if n == 1 and re.search(r'\b(?:in\s+)?(?:which|what)\s+(?:calendar\s+|fiscal\s+)?year\b', ql) \
            and not re.search(r'multiply|add it|\bsum\b', ql):
        return 'year'
    if n == 1 and any(d for d in decimals if d):
        return 'number'
    if n == 1 and (re.search(rf'\bhow many\b(?!\s+{_MEASURE})', ql) or re.search(r'\bsingle integer\b|\bnumber of times\b', ql)):
        return 'integer'
    return 'number'

# ---------------------------------------------------------------------------------------------
# publication pins
# ---------------------------------------------------------------------------------------------

_PIN_RES = [
    re.compile(rf'(?:{MONTHYEAR}|{YEAR}|\d{{1,2}}/{YEAR})(?:\s+(?:and|,)\s+{YEAR})*\s+(?:u\.?\s?s\.?\s+)?(?:treasury\s+)?'
               rf'(?:monthly\s+)?(?:bulletins?|edition|issues?)\b', re.I),
    re.compile(rf'\bbulletins?\s+(?:published\s+)?(?:in|from|of|dated)\s+(?:the\s+)?{MONTHYEAR}(?:\s*,\s*(?:and\s+)?{YEAR})*', re.I),
    re.compile(rf'(?<!recently )(?<!latest )\bpublished\s+(?:(?:in|on|at|during|by)\s+)?(?:the\s+)?(?:{MONTHYEAR}|{YEAR}|'
               rf'last\s+day[^.;?]{{0,30}}|end\s+of[^.;?]{{0,30}}|this\s+date|specifically[^.;?]{{0,40}}|[^.;?]{{0,12}}\bsubsequent\b[^.;?]{{0,20}})', re.I),
    re.compile(rf'\b(?:reported|released|issued)\b[^.;?]{{0,60}}?\bby\s+the\s+u\.?\s?s\.?\s+(?:department of the\s+)?'
               rf'treasury\s+in\s+{MONTHYEAR}', re.I),
    re.compile(rf'\b(?:as of|with)\s+reported\s+(?:values|estimates|figures)\s+on\s+(?:the\s+end\s+of\s+)?(?:the\s+)?'
               rf'(?:{MONTHYEAR}|{YEAR}\s+fy|fy\s?{YEAR}|fiscal\s+year\s+{YEAR})', re.I),
    re.compile(rf'\breported\s+at\s+the\s+end\s+of\s+(?:the\s+)?(?:fy|fiscal\s+year)\s?{YEAR}', re.I),
    re.compile(r'\b(?:exclusively|only|specifically)\s+from\s+[^.;?]{0,40}?\bbulletins?\b', re.I),
]
_NOT_PIN = re.compile(r'most recently published|latest (?:published|printing|revision|treasury bulletin)', re.I)


def pin_spans(q: str) -> list[tuple[int, int]]:
    spans = []
    for rx in _PIN_RES:
        for m in rx.finditer(q):
            if _NOT_PIN.search(q[max(0, m.start() - 20):m.end()]):
                continue
            spans.append((m.start(), m.end()))
    return sorted(spans)

# ---------------------------------------------------------------------------------------------
# periods
# ---------------------------------------------------------------------------------------------


def _years_between(a: int, b: int) -> list[int]:
    if b < a or b - a + 1 > MAX_YEARS:
        return [a, b] if b != a else [a]
    return list(range(a, b + 1))


def _months_between(y1: int, m1: int, y2: int, m2: int) -> list[str]:
    a, b = y1 * 12 + m1 - 1, y2 * 12 + m2 - 1
    if b < a:
        return [f'{y1}-{m1:02d}', f'{y2}-{m2:02d}']
    if b - a + 1 > MAX_MONTHS:
        return [f'{y1}-{m1:02d}'] + [str(y) for y in range(y1 + 1, y2)] + [f'{y2}-{m2:02d}']
    return [f'{i // 12}-{i % 12 + 1:02d}' for i in range(a, b + 1)]


_LIST_SEP = r'\s*(?:,\s*(?:and\s+)?|and\s+|&\s*)\s*'
_RANGE_SEP = r'\s*(?:-|to|through|thru|until)\s*(?:the\s+(?:end|start|beginning)\s+of\s+(?:the\s+)?)?'
_P_RULES = [
    # FY / CY year ranges and lists ('fiscal years 1991-2010', 'FY 2007 - 2013', 'CY 1945 to 1955', 'FY1939 - 1941')
    ('range', re.compile(rf'\b(?:federal\s+)?(?P<b>fiscal\s+years?|fy|calendar\s+years?|cy)\s*(?:from\s+|of\s+|between\s+(?:the\s+)?)?'
                         rf'(?P<a>{YEAR})(?:{_RANGE_SEP}|\s+and\s+(?=(?:fy|cy)?\s*{YEAR}\s+inclusive))'
                         rf'(?:(?:fy|cy|fiscal\s+years?|calendar\s+years?)\s*)?(?P<z>{YEAR})\b', re.I)),
    ('list', re.compile(rf'\b(?:federal\s+)?(?P<b>fiscal\s+years?|fy|calendar\s+years?|cy)\s*(?:of\s+)?'
                        rf'(?P<l>{YEAR}(?:{_LIST_SEP}(?:fy\s*|cy\s*)?{YEAR})*)', re.I)),
    ('list_after', re.compile(rf'\b(?P<l>{YEAR}(?:{_LIST_SEP}{YEAR})*)\s+(?P<b>fiscal|calendar)\s+years?\b', re.I)),
    ('range_after', re.compile(rf'\b(?P<a>{YEAR}){_RANGE_SEP}(?P<z>{YEAR})\s+(?P<b>fiscal|calendar)\s+years?\b', re.I)),
    # calendar quarters ('third calendar year quarter of 1995')
    ('quarter', re.compile(rf'\b(?P<q>first|second|third|fourth)\s+(?:calendar[- ](?:year\s+)?)?quarter\s+(?:of\s+)?(?P<y>{YEAR})', re.I)),
    # month ranges ('January 1984 to March 1987', 'May through August 1939', 'February-May 1940')
    ('mrange', re.compile(rf'\b(?P<m1>{MON})\s*(?P<y1>{YEAR})?{_RANGE_SEP}(?P<m2>{MON})\s*(?:\d{{1,2}},?\s*)?(?P<y2>{YEAR})', re.I)),
    # one month over a year range ('July across 2010-2013', 'fiscal month January from 1969 to 1980')
    ('myrange', re.compile(rf'\b(?P<m>{MON})\s+(?:across|from|of|for|in|over)\s+(?:each\s+of\s+|the\s+years\s+|years\s+)?'
                           rf'(?P<a>{YEAR}){_RANGE_SEP}(?P<z>{YEAR})', re.I)),
    # month lists ('January, February, and March of 1941', 'October in 1938')
    ('mlist', re.compile(rf'\b(?P<l>{MON}(?:{_LIST_SEP}{MON})*)\s+(?:of\s+|in\s+)?(?:\d{{1,2}}(?:st|nd|rd|th)?,?\s*)?(?P<y>{YEAR})', re.I)),
    # bare year ranges ('1960-1969', 'between 1934 and 1946', '2009 to 2011')
    ('yrange', re.compile(rf'\b(?:between\s+(?:the\s+years\s+(?:of\s+)?)?)?(?P<a>{YEAR})(?:{_RANGE_SEP}|\s+and\s+)(?P<z>{YEAR})\b', re.I)),
    ('year', re.compile(rf'(?<![\d/.-])(?P<y>{YEAR})(?![\d%])', re.I)),
]
_CPI_BASE = re.compile(r'\b(?:19|20)\d\d\s*-\s*\d\d\s*=\s*100\b')


def _basis(word: str) -> str:
    w = word.lower()
    return 'FY' if w.startswith(('fiscal', 'fy')) else 'CY'


def periods(q: str, exclude: list[tuple[int, int]] = ()) -> tuple[str, ...]:
    """Normalised periods named by the question, in order of appearance (see module docstring)."""
    text = q
    taken = [False] * len(text)
    for a, b in list(exclude) + [(m.start(), m.end()) for m in _CPI_BASE.finditer(text)]:
        for i in range(a, min(b, len(text))):
            taken[i] = True
    found: list[tuple[int, list[str]]] = []
    for kind, rx in _P_RULES:
        for m in rx.finditer(text):
            if any(taken[m.start():m.end()]):
                continue
            g = m.groupdict()
            out: list[str] = []
            if kind in ('range', 'range_after'):
                a, z = int(g['a']), int(g['z'])
                out = [f"{_basis(g['b'])}{y}" for y in _years_between(a, z)]
            elif kind in ('list', 'list_after'):
                out = [f"{_basis(g['b'])}{y}" for y in re.findall(YEAR, g['l'])]
            elif kind == 'quarter':
                qn = ORDINALS[g['q'].lower()]
                out = [f"{g['y']}-{m_:02d}" for m_ in range(3 * qn + 1, 3 * qn + 4)]
            elif kind == 'mrange':
                y2 = int(g['y2'])
                m1, m2 = _month(g['m1']), _month(g['m2'])
                y1 = int(g['y1']) if g['y1'] else (y2 if m1 <= m2 else y2 - 1)
                out = _months_between(y1, m1, y2, m2)
            elif kind == 'myrange':
                mm = _month(g['m'])
                out = [f'{y}-{mm:02d}' for y in _years_between(int(g['a']), int(g['z']))]
            elif kind == 'mlist':
                mons = re.findall(MON, g['l'], re.I)
                out = [f"{g['y']}-{_month(x):02d}" for x in mons]
            elif kind == 'yrange':
                out = [str(y) for y in _years_between(int(g['a']), int(g['z']))]
            elif kind == 'year':
                out = [g['y']]
            if not out:
                continue
            for i in range(m.start(), m.end()):
                taken[i] = True
            found.append((m.start(), out))
    seen, result = set(), []
    for _, ps in sorted(found, key=lambda x: x[0]):
        for p in ps:
            if p not in seen:
                seen.add(p)
                result.append(p)
    return tuple(result)

# ---------------------------------------------------------------------------------------------
# keywords
# ---------------------------------------------------------------------------------------------

STOP = set("""
a an the and or of to in on at by for from with without into over under between across per as is are was were be been
being this that these those it its their there here which what who whom whose when where why how many much each every
all any both either neither same other than then also only just not no nor so such very more most less least
using use used based according report reported reporting return returned output outputted provide give find compute
calculate calculated determine estimate express expressed round rounded rounding nearest place places decimal decimals
tenths tenth hundredths hundredth thousandths thousandth whole number numbers value values answer answers final
single numeric comma separated commas brackets bracket enclosed square list order first second third fourth fifth
percent percentage points point sign form like e.g i.e if would should does do did can could will please note
treasury bulletin bulletins department u.s us usa united states federal government nominal dollars dollar millions
million billions billion thousands thousand trillions year years calendar fiscal month months monthly annual yearly
quarter quarterly cy fy inclusive specifically data official officially published end beginning day days date dates
january february march april may june july august september october november december jan feb mar apr jun jul aug sep
sept oct nov dec total values mean average
""".split())


def keywords(q: str, limit: int = 40) -> tuple[str, ...]:
    words = re.findall(r"[a-z][a-z'-]*[a-z]", _clean(q).lower())
    out, seen = [], set()
    for w in words:
        w = w.strip("'-").replace("'s", '')
        if len(w) < 3 or w in STOP or w in seen:
            continue
        seen.add(w)
        out.append(w)
        if len(out) >= limit:
            break
    return tuple(out)

# ---------------------------------------------------------------------------------------------


_QUOTED_WORD = re.compile(r"""(?<![a-z0-9])(['"])([a-z][a-z-]*[a-z]|[a-z])\1(?![a-z0-9])""")


def allowed_words(q: str) -> tuple[str, ...]:
    """Single lower-case words the question puts in quotes, in order of appearance (the replay parser's
    rule, extended to double quotes; apostrophes inside words such as "Treasury's" never open a quote)."""
    out: list[str] = []
    for m in _QUOTED_WORD.finditer(_clean(q).lower()):
        if m.group(2) not in out:
            out.append(m.group(2))
    return tuple(out)


def question_spec(question: str) -> QuestionSpec:
    q = _clean(question)
    n, lst = n_values(q)
    pins = pin_spans(q)
    dec = decimals_per_value(q, n)
    return QuestionSpec(n_values=n, list_format=lst, decimals=dec,
                        percent_form=percent_form(q), value_type=value_type(q, n, dec),
                        periods=periods(q, pins), pins_date=bool(pins), allowed_words=allowed_words(q),
                        keywords=keywords(q))


_ANSWER_TOKEN = re.compile(r'[-−]?\$?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?|[-−]?\.\d+%?|[a-z]+')


def canonical(answer: str) -> str:
    """Comparable form of a text answer: lower case, brackets/currency/thousands separators dropped,
    numbers as typed (decimals kept), '%' kept, words kept; tokens joined by single spaces."""
    t = _clean(answer).lower().replace('−', '-')
    toks = []
    for m in _ANSWER_TOKEN.finditer(t):
        tok = m.group(0).replace('$', '')
        if tok[:1].isdigit() or tok[:1] in '-.':
            tok = tok.replace(',', '')
        toks.append(tok)
    return ' '.join(toks)
