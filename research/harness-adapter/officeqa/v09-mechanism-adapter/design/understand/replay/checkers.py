"""Candidate v9 commit-gate checkers, replayed offline over v8 trajectories.

(a) format_check(question, answer)
(b) provenance_check(steps, answer, question, visible=None)
(c) loop_detect(steps)
(d) doubt_unresolved(steps)

All checkers are deterministic, use only the question text, the submitted
answer and the trajectory, and never see a reference answer.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np

# ---------------------------------------------------------------------------
# Answer parsing

WORDNUM = {'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7,
           'eight': 8, 'nine': 9, 'ten': 10, 'both': 2}

# Grouping-aware and plain number tokenisers for submitted answers.
TOK_GROUPED = re.compile(r'(?<![\d.])-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][-+]?\d+)?|(?<![\d.])-?\.\d+')
TOK_PLAIN = re.compile(r'(?<![\d.])-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?|(?<![\d.])-?\.\d+')


@dataclass
class Value:
    number: float            # parsed numeric value (never printed in the report)
    decimals: int = 0        # digits after the decimal point, as typed
    sigfigs: int = 0
    grouped: bool = False    # thousands separators used
    percent: bool = False    # followed by a % sign
    currency: bool = False   # preceded by a currency symbol
    sci: bool = False


@dataclass
class Parsed:
    values: list[Value]
    bracketed: bool
    words: list[str]         # alphabetic residue (units, labels, prose)


def _strip_brackets(s: str) -> tuple[str, bool]:
    s = s.strip()
    if len(s) >= 2 and s[0] in '[(' and s[-1] in '])':
        return s[1:-1].strip(), True
    return s, False


def _value(tok: str, before: str, after: str) -> Value:
    clean = tok.replace(',', '')
    mant = re.sub(r'[eE].*$', '', clean).lstrip('-')
    decimals = len(mant.split('.')[1]) if '.' in mant else 0
    digits = mant.replace('.', '').lstrip('0')
    if '.' not in mant:
        digits = digits.rstrip('0') or digits
    return Value(number=float(clean), decimals=decimals, sigfigs=len(digits), grouped=',' in tok,
                 percent=after.lstrip().startswith('%'),
                 currency=bool(re.search(r'[$£¥€]\s*$', before)),
                 sci=bool(re.search(r'[eE][-+]?\d', tok)))


def parse_answer(answer: str, expected: int | None = None) -> Parsed:
    """Every number in the answer is one value. Commas followed by exactly
    three digits are thousands separators, unless that yields the wrong count
    and a plain tokenisation yields the expected count."""
    inner, bracketed = _strip_brackets(answer or '')
    t = inner.replace('−', '-').replace('–', '-')
    toks = list(TOK_GROUPED.finditer(t))
    if expected and len(toks) != expected:
        alt = list(TOK_PLAIN.finditer(t))
        if len(alt) == expected:
            toks = alt
    vals = [_value(m.group(0), t[:m.start()], t[m.end():]) for m in toks]
    residue = TOK_PLAIN.sub(' ', t)
    words = [w for w in re.findall(r'[A-Za-z]+', residue)]
    if any(v.sci for v in vals):
        words = [w for w in words if w.lower() != 'e']
    return Parsed(vals, bracketed, words)


# ---------------------------------------------------------------------------
# Question spec parsing

@dataclass
class Spec:
    n_values: int | None = 1
    list_required: bool = False
    brackets_required: bool = False
    decimals: list[set[int]] | None = None   # per value slot; None = unknown
    sigfigs: int | None = None
    decimal_form: bool = False               # '% sign' forbidden, rate as fraction
    decimal_form_example: bool = False       # explicit 0.1234-vs-12.34% example
    percent_form: bool = False
    no_percent_sign: bool = False
    no_commas: bool = False
    integer_count: bool = False              # "how many" single value
    year_expected: bool = False
    year_slot: bool = False                  # one of several values is a year
    absolute: bool = False
    allowed_words: set[str] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)


_DEC_WORDS = [
    (r'hundred[- ]thousandths?', 5), (r'ten[- ]thousandths?', 4), (r'thousandths?', 3),
    (r'hundredths?', 2), (r'tenths?', 1),
]
_DEC_RE = re.compile(
    r'(?P<n>\d+|one|two|three|four|five|six|seven|eight)\s+(?:nearest\s+)?decimal(?:s\b| places?| points?)'
    r'|to\s+(?P<n2>\d+|one|two|three|four|five|six)\s+decimals\b'
    r'|(?P<w>hundred[- ]thousandths?|ten[- ]thousandths?|thousandths?|hundredths?|tenths?)(?![- ]of\b)'
    r'|(?P<z>nearest\s+whole|whole\s+number|nearest\s+(?:nominal\s+)?dollar\b|nearest\s+million\b|'
    r'single\s+integer|as\s+an\s+integer|nearest\s+integer)'
    r'|(?P<sf>\d+|three|four|five|six)\s+significant\s+(?:digits|figures)',
    re.I)


def _w2n(s: str) -> int:
    s = s.lower()
    return int(s) if s.isdigit() else WORDNUM[s]


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


def rounding_specs(q: str) -> list[tuple[int, str, int]]:
    """(position, kind 'dp'|'sf', n) for rounding phrases that refer to the
    final answer: phrases in a sentence about 'intermediate' values are
    dropped, parenthesised ones are a fallback only."""
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
    # A parenthesised rounding phrase usually qualifies an input ("exchange
    # rate (rounded to ...)"); use it only when nothing outside parentheses does.
    return out or in_paren


def question_spec(q: str) -> Spec:
    s = Spec()
    ql = q.lower()
    # ---- how many values
    n = None
    m = re.search(r'\b(two|three|four|five|2|3|4|5)\s+(?:comma[- ]separated\s+)?(?:values|answers|numbers)\b', ql)
    if m:
        n = _w2n(m.group(1))
    if n is None and re.search(r'\bboth (?:values|answers|final answers)\b|\breturn both\b|\breport both\b', ql):
        n = 2
    if n is None:
        m = re.search(r'in the order of ([^.?]+)', ql)
        if m and 'sub-question' not in m.group(1) and 'subquestion' not in m.group(1):
            items = [x for x in re.split(r',\s*|\s+and\s+', m.group(1)) if x.strip()]
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
        ords = re.findall(r'\b(first|second|third|fourth)\s+value', ql)
        if ords:
            n = {'first': 1, 'second': 2, 'third': 3, 'fourth': 4}[max(ords, key=lambda o: ['first', 'second', 'third', 'fourth'].index(o))]
            n = max(n, 2)
    if n is None and re.search(r'(?:return|output|report)\s+(?:the\s+)?(?:linear regression\s+)?slope and (?:y-)?intercept', ql):
        n = 2
    if n is None:
        m = re.search(r'starting with [^.]*?(\d{4})[^.]*?ending with [^.]*?(\d{4})', ql)
        if m and 'list' in ql:
            n = int(m.group(2)) - int(m.group(1)) + 1
    s.list_required = bool(n and n > 1) or bool(re.search(r'comma[- ]separated|\bbrackets\b|\blist\b', ql))
    if n is None:
        n = None if s.list_required else 1
    s.n_values = n
    s.brackets_required = bool(re.search(r'bracket', ql)) or bool(re.search(r'format \[', ql))
    # ---- rounding
    specs = rounding_specs(q)
    dps = [(p, k) for p, kind, k in specs if kind == 'dp']
    sfs = [k for p, kind, k in specs if kind == 'sf']
    if sfs and not dps:
        s.sigfigs = sfs[0]
    if dps:
        nv = n or 1
        uniq = {k for _, k in dps}
        ords = [(mm.start(), {'first': 0, 'second': 1, 'third': 2, 'fourth': 3}[mm.group(1)])
                for mm in re.finditer(r'\b(first|second|third|fourth)\s+value', ql)]
        if nv > 1 and ords:
            slots: list[set[int]] = [set() for _ in range(nv)]
            for p, k in dps:
                prev = [o for o in ords if o[0] <= p]
                if prev and prev[-1][1] < nv:
                    slots[prev[-1][1]].add(k)
            fallback = uniq
            s.decimals = [sl or set(fallback) for sl in slots]
            s.notes.append('ordinal-slot rounding')
        elif len(uniq) == 1:
            s.decimals = [set(uniq)] * nv
        elif nv > 1 and len(dps) == nv:
            s.decimals = [{k} for _, k in dps]
            s.notes.append('positional rounding')
        else:
            # ambiguous: accept any stated precision for any value
            s.decimals = [set(uniq)] * nv
            s.notes.append('ambiguous rounding (any-of)')
    if s.decimals is None and n == 1 and re.search(r'\bhow many\b', ql):
        s.integer_count = True
        s.decimals = [{0}]
    # ---- percent / decimal form
    s.decimal_form_example = bool(re.search(r'e\.g\.\s*0\.\d+,?\s*not\s*\d+\.\d+%|if\s+\d+\.\d+%?\s+is\s+(?:a\s+)?percent|if percent is \d', ql))
    s.decimal_form = s.decimal_form_example or bool(re.search(
        r'in decimal form|as a decimal value|outputted as a decimal|decimal value\b', ql))
    s.percent_form = bool(re.search(
        r'as a percent(?:age)?(?: value)?\b|expressed as a percent|as a percentage like|percent value|'
        r'in percentage points|percentage points|reported percentage value|percent per year|as a number like \d', ql))
    s.no_percent_sign = bool(re.search(r'no percent sign|without[^.]{0,30}percent signs?|not include[^.]{0,20}percent', ql))
    s.no_commas = bool(re.search(r'no commas|without commas|omitting commas|not include any commas|'
                                 r'without[^.]{0,20}commas|\(no commas\)', ql))
    s.year_expected = bool(n == 1 and re.search(r'\b(?:in )?(?:which|what) year\b', ql)
                           and not re.search(r'multiply|add it', ql))
    s.year_slot = bool((n or 0) > 1 and re.search(r'just the year|the year with|which year|what year', ql))
    s.absolute = bool(re.search(r'\babsolute\b', ql)) and 'signed' not in ql
    s.allowed_words = set(re.findall(r"'([a-z]+)'", ql))
    return s


# ---------------------------------------------------------------------------
# (a) format_check

HARD_FORMAT_FLAGS = ['EMPTY', 'COUNT', 'NO_BRACKETS', 'DEC_LONG', 'DEC_SHORT', 'SIGFIG',
                     'PCT_SIGN_DECIMAL_ASKED', 'PCT_SIGN_FORBIDDEN', 'DECIMAL_FORM_MAGNITUDE',
                     'STRAY_TEXT', 'STRAY_UNIT', 'COMMAS_FORBIDDEN', 'YEAR_EXPECTED',
                     'NEGATIVE_ABSOLUTE', 'NON_NUMERIC']
SOFT_FORMAT_FLAGS = ['THOUSANDS_SEP', 'EXTRA_BRACKETS', 'STRAY_PERCENT']


def _is_year(v: Value) -> bool:
    return v.decimals == 0 and 1900 <= abs(v.number) <= 2035


def format_check(question: str, answer: str) -> dict:
    spec = question_spec(question)
    flags: dict[str, str] = {}
    a = (answer or '').strip()
    if not a:
        return {'flags': {'EMPTY': 'no answer submitted'}, 'spec_notes': spec.notes}
    if len(a.splitlines()) > 1:
        flags['STRAY_TEXT'] = 'multi-line answer'
    p = parse_answer(a, spec.n_values)
    vals = p.values
    allowed_hits = [w for w in p.words if w.lower() in spec.allowed_words]
    stray = [w for w in p.words if w.lower() not in spec.allowed_words]
    n_items = len(vals) + len(allowed_hits)
    if spec.brackets_required and not p.bracketed:
        flags['NO_BRACKETS'] = 'question asks for enclosing brackets; none present'
    if p.bracketed and spec.n_values == 1 and not spec.list_required:
        flags['EXTRA_BRACKETS'] = 'single value wrapped in brackets'
    if not vals and not allowed_hits:
        flags['NON_NUMERIC'] = 'answer contains no number'
    if spec.n_values and n_items != spec.n_values:
        flags['COUNT'] = f'{n_items} value(s) submitted, question asks for {spec.n_values}'
    elif spec.n_values is None and spec.list_required and n_items < 2:
        flags['COUNT'] = f'{n_items} value(s) submitted, question asks for a list'
    if stray:
        flags['STRAY_TEXT'] = f'{len(stray)} stray word(s) (units/labels/prose) around the number(s)'
    for i, v in enumerate(vals):
        slot = f'value {i + 1}/{len(vals)}'
        if v.currency:
            flags.setdefault('STRAY_UNIT', f'{slot} carries a currency symbol')
        if v.percent:
            if spec.decimal_form:
                flags.setdefault('PCT_SIGN_DECIMAL_ASKED', f'{slot} has a % sign; question asks for decimal form')
            elif spec.no_percent_sign:
                flags.setdefault('PCT_SIGN_FORBIDDEN', f'{slot} has a % sign; question forbids it')
            elif not spec.percent_form:
                flags.setdefault('STRAY_PERCENT', f'{slot} has a % sign; question does not ask for percent')
        if v.grouped:
            if spec.no_commas:
                flags.setdefault('COMMAS_FORBIDDEN', f'{slot} uses thousands separators; question forbids commas')
            else:
                flags.setdefault('THOUSANDS_SEP', f'{slot} uses thousands separators')
        if spec.sigfigs and v.sigfigs != spec.sigfigs:
            flags.setdefault('SIGFIG', f'{slot} has {v.sigfigs} significant digits, asked {spec.sigfigs}')
        if spec.decimals and len(vals) == len(spec.decimals) and spec.decimals[i]:
            allowed = spec.decimals[i]
            if not (spec.year_slot and _is_year(v)) and v.decimals not in allowed:
                kind = 'DEC_LONG' if v.decimals > max(allowed) else 'DEC_SHORT'
                flags.setdefault(kind, f'{slot} has {v.decimals} decimals, asked {sorted(allowed)}')
        if spec.decimal_form_example and len(vals) == 1 and abs(v.number) >= 1:
            flags.setdefault('DECIMAL_FORM_MAGNITUDE', f'{slot} magnitude >= 1 where a fraction-form rate is asked')
        if spec.absolute and len(vals) == 1 and v.number < 0:
            flags.setdefault('NEGATIVE_ABSOLUTE', f'{slot} negative; question asks for an absolute value')
    if spec.year_expected and not (len(vals) == 1 and _is_year(vals[0]) and not stray):
        flags.setdefault('YEAR_EXPECTED', 'question asks which year; answer is not a bare 4-digit year')
    return {'flags': flags, 'spec_notes': spec.notes}


def format_fired(res: dict, include_soft: bool = False) -> bool:
    keys = HARD_FORMAT_FLAGS + (SOFT_FORMAT_FLAGS if include_soft else [])
    return any(k in res['flags'] for k in keys)


# ---------------------------------------------------------------------------
# (b) provenance_check

OUT_NUM = re.compile(r'(?<![\d.])-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][-+]?\d{1,3})?|(?<![\d.])-?\.\d+')


def numbers_in(text: str) -> np.ndarray:
    vals = []
    for tok in OUT_NUM.findall(text):
        try:
            vals.append(abs(float(tok.replace(',', ''))))
        except ValueError:
            continue
        if ',' in tok:  # also the comma-separated pieces (CSV-like rows)
            for piece in tok.split(','):
                try:
                    vals.append(abs(float(piece)))
                except ValueError:
                    pass
    return np.array(vals, dtype=float)


def _tol(v: Value) -> float:
    return 0.5 * 10.0 ** (-v.decimals) * (1 + 1e-9) + 1e-12 * max(1.0, abs(v.number))


def _hit(arr: np.ndarray, x: float, tol: float) -> bool:
    if arr.size == 0:
        return False
    i = int(np.searchsorted(arr, x - tol))
    return i < arr.size and arr[i] <= x + tol


SCALES = [1e-9, 1e-6, 1e-3, 1e-2, 1e2, 1e3, 1e6, 1e9]
CALC_TOOLS = re.compile(r'\bpython3?\b|\bawk\b|\bbc\b|\bnode\b')


def _is_calc(cmd: str, out_len: int) -> bool:
    """A computation, not a corpus dump: python/awk/bc with a short output.
    Approximates the v9 'answer = some calc output' rule."""
    return bool(CALC_TOOLS.search(cmd)) and out_len <= 3000 and 'OfficeQACorpus' not in cmd


@dataclass
class Corpus:
    """Sorted |numbers| of a set of tool outputs, plus per-command arrays."""
    all: np.ndarray
    per_cmd: list[tuple[str, np.ndarray, int]]   # (command, |numbers| of its output, output length)

    @classmethod
    def from_steps(cls, steps) -> 'Corpus':
        per = []
        for st in steps:
            if st.kind == 'cmd' and st.output:
                per.append((st.text, np.unique(numbers_in(st.output)), len(st.output)))
        allv = np.unique(np.concatenate([a for _, a, _ in per])) if per else np.array([])
        return cls(allv, per)

    @classmethod
    def from_texts(cls, texts) -> 'Corpus':
        arrs = [np.unique(numbers_in(t)) for t in texts if t]
        allv = np.unique(np.concatenate(arrs)) if arrs else np.array([])
        return cls(allv, [])


def provenance_check(steps, answer: str, question: str, corpus: Corpus | None = None,
                     visible: Corpus | None = None) -> dict:
    spec = question_spec(question)
    nums = parse_answer((answer or '').strip(), spec.n_values).values
    if not nums:
        return {'applicable': False, 'fired': False, 'fired_visible': None, 'fired_scaled': False,
                'fired_echo': False, 'fired_calc': False, 'reasons': ['no numeric value in answer']}
    corpus = corpus or Corpus.from_steps(steps)
    calc = [arr for cmd, arr, out_len in corpus.per_cmd if _is_calc(cmd, out_len)]
    calc_all = np.unique(np.concatenate(calc)) if calc else np.array([])
    unmatched, unmatched_vis, unmatched_scaled, echo_only, not_calc, reasons = [], [], [], [], [], []
    for i, v in enumerate(nums):
        x, tol = abs(v.number), _tol(v)
        if not _hit(corpus.all, x, tol):
            unmatched.append(i)
            scaled = any(_hit(corpus.all, x * s, tol * s) for s in SCALES)
            if not scaled:
                unmatched_scaled.append(i)
            if corpus.all.size:
                j = int(np.searchsorted(corpus.all, x))
                near = [corpus.all[k] for k in (j - 1, j) if 0 <= k < corpus.all.size]
                rel = min(abs(n - x) / max(x, 1e-12) for n in near) if near else float('inf')
            else:
                rel = float('inf')
            reasons.append(f'value {i + 1}/{len(nums)} ({v.decimals} dp) not in any command output; '
                           f'nearest output number off by {rel:.2%}'
                           + ('; matches only after x10^k rescaling (unit/percent conversion typed by hand)' if scaled else ''))
        else:
            # echo test: every output containing it came from a command that already contained it
            producers = [cmd for cmd, arr, _ in corpus.per_cmd if _hit(arr, x, tol)]
            if producers and all(_hit(np.unique(numbers_in(cmd)), x, tol) for cmd in producers):
                echo_only.append(i)
                reasons.append(f'value {i + 1}/{len(nums)} only appears in output of commands that already contain it (echo/print literal)')
        if visible is not None and not _hit(visible.all, x, tol):
            unmatched_vis.append(i)
        if not _hit(calc_all, x, tol):
            not_calc.append(i)
    return {
        'applicable': True,
        'n_values': len(nums),
        'fired': bool(unmatched),
        'fired_scaled': bool(unmatched_scaled),
        'fired_echo': bool(unmatched or echo_only),
        'fired_visible': (bool(unmatched_vis) if visible is not None else None),
        'fired_calc': bool(not_calc),
        'unmatched': len(unmatched),
        'unmatched_visible': len(unmatched_vis),
        'echo_only': len(echo_only),
        'reasons': reasons,
    }


# ---------------------------------------------------------------------------
# (c) loop_detect

def _norm(cmd: str) -> str:
    return ' '.join(cmd.split())


def loop_detect(steps, k: int = 3) -> dict:
    """First command index at which any identical (whitespace-normalised)
    command has been issued for the k-th time, plus repeat statistics and
    whether an automatic compaction happened at or after that point."""
    counts: dict[str, int] = {}
    pair_counts: dict[tuple[str, str], int] = {}
    first_k = first_k_pair = first_consec = None
    prev, consec, max_consec, wasted = None, 0, 0, 0
    for st in steps:
        if st.kind != 'cmd':
            continue
        c = _norm(st.text)
        counts[c] = counts.get(c, 0) + 1
        if counts[c] == k and first_k is None:
            first_k = st.cmd_index
        p = (c, st.output)
        pair_counts[p] = pair_counts.get(p, 0) + 1
        if pair_counts[p] == k and first_k_pair is None:
            first_k_pair = st.cmd_index
        consec = consec + 1 if c == prev else 1
        prev = c
        max_consec = max(max_consec, consec)
        if consec >= k:
            wasted += 1
            if first_consec is None:
                first_consec = st.cmd_index
    comp = [st.cmd_index for st in steps if st.kind == 'compaction']
    n_cmd = sum(1 for st in steps if st.kind == 'cmd')
    return {
        'fired': first_k is not None,
        'fired_same_output': first_k_pair is not None,
        'fired_consecutive': first_consec is not None,
        'first_third_at': first_k,
        'first_third_same_output_at': first_k_pair,
        'first_consecutive_third_at': first_consec,
        'n_cmds': n_cmd,
        'repeats': sum(v - 1 for v in counts.values() if v > 1),
        'distinct_ge3': sum(1 for v in counts.values() if v >= k),
        'max_count': max(counts.values()) if counts else 0,
        'max_consecutive': max_consec,
        'wasted_consecutive': wasted,   # commands issued as 3rd+ identical in a row
        'wasted_all': sum(v - (k - 1) for v in pair_counts.values() if v >= k),  # 3rd+ issues of an identical command+output
        'compactions': len(comp),
        'compacted_after_loop': bool(first_k is not None and any(c >= first_k for c in comp)),
    }


# ---------------------------------------------------------------------------
# (d) doubt_unresolved

DOUBT = re.compile(r'\bmaybe\b|\bunclear\b|\bnot sure\b|\bambiguous\b|\balternatively\b|\bshould we\b', re.I)
SALIENT = re.compile(r'\b(?:1[89]\d\d|20[0-3]\d)\b|\b\d{3,}(?:\.\d+)?\b')


def doubt_unresolved(steps, window: int = 10) -> dict:
    reasons_idx = [i for i, st in enumerate(steps) if st.kind == 'reason']
    last = reasons_idx[-window:]
    doubt_idx = [i for i in last if DOUBT.search(steps[i].text)]
    if not doubt_idx:
        return {'doubt_in_window': False, 'fired': False, 'fired_keyword': False, 'cmds_after': None}
    j = doubt_idx[-1]
    after = [st for st in steps[j + 1:] if st.kind == 'cmd']
    # keyword variant: a later command must mention a year/number from the doubting notes
    toks = set()
    for i in doubt_idx:
        toks |= set(SALIENT.findall(steps[i].text))
    later_cmds = ' '.join(st.text for st in steps[doubt_idx[0] + 1:] if st.kind == 'cmd')
    addressed_kw = (any(t in later_cmds for t in toks) if toks else bool(after))
    words = sorted({m.group(0).lower() for i in doubt_idx for m in DOUBT.finditer(steps[i].text)})
    return {
        'doubt_in_window': True,
        'words': words,
        'fired': not after,
        'fired_keyword': not addressed_kw,
        'cmds_after': len(after),
        'doubt_notes': len(doubt_idx),
    }
