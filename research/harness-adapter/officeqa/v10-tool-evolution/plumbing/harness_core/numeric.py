"""Benchmark-neutral number handling shared by the ledger, the core predicates and the offers.

Three jobs, all deterministic and stdlib-only:
- `number_tokens(text)`: numbers as a person or a program printed them ('$1,234.5', '-12%', '1.5e6'),
  with the span of the digits so an offer can swap one number inside an answer and keep the rest.
- `code_literals(code)`: numeric literals in model-authored Python (the `calc` code), via `tokenize`,
  so identifiers such as `x1` or `float64` never count as numbers.
- `ValueIndex`: a sorted multiset of observed |values| answering "which earlier observation does this
  typed literal come from?" exactly, rounded to the literal's precision, or scaled by 10^k.
Signs are ignored for matching (tables print negatives as '-1.2', '(1.2)' or with a word), and every
comparison has an explicit tolerance so float noise never decides a match.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
import io
import math
import re
import tokenize

# A number with optional sign, currency sign, thousands separators, decimals, exponent and percent.
# The look-behind keeps digits glued to letters/underscores/dots out: 'FY1979', 'bulletin_1979', 'c17'.
NUMBER_RE = re.compile(
    r'(?<![\w.])(?P<neg>[-−])?(?P<dollar>\$\s?)?'
    r'(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+)'
    r'(?P<exp>[eE][+-]?\d+)?(?![\w])(?P<pct>\s?%)?')
SCALES = (3, -3, 6, -6, 9, -9, 2, -2)      # 10^k rescalings tried for 'scaled' matches, in this order
MIN_SIG_FOR_SCALED = 3                    # short literals ('50') would match far too much after rescaling


@dataclass(frozen=True)
class NumTok:
    """One number inside a text. `start:end` covers sign, currency and percent; `num_start:num_end`
    only the digits (incl. separators/exponent)."""
    text: str
    value: float                  # signed
    decimals: int                 # digits after the point as written (exponent-adjusted, ≥ 0)
    sig: int                      # significant digits as written
    start: int
    end: int
    num_start: int
    num_end: int
    negative: bool = False
    dollar: bool = False
    percent: bool = False
    commas: bool = False

    def as_dict(self, index: int | None = None) -> dict:
        data = {'text': self.text, 'value': self.value, 'decimals': self.decimals, 'start': self.start,
                'end': self.end, 'num_start': self.num_start, 'num_end': self.num_end,
                'negative': self.negative, 'dollar': self.dollar, 'percent': self.percent, 'commas': self.commas}
        if index is not None:
            data['index'] = index
        return data


def _digits_info(num: str, exp: str | None) -> tuple[float, int, int]:
    plain = num.replace(',', '').replace('_', '')
    value = float(plain + (exp or ''))
    frac = plain.split('.', 1)[1] if '.' in plain else ''
    shift = int(exp[1:]) if exp else 0
    decimals = max(0, len(frac) - shift)
    digits = plain.replace('.', '').lstrip('0')
    sig = len(digits) if digits else 1
    return value, decimals, sig


def number_tokens(text: str) -> list[NumTok]:
    """All numbers in `text`, left to right (see NUMBER_RE)."""
    out: list[NumTok] = []
    for m in NUMBER_RE.finditer(text or ''):
        try:
            value, decimals, sig = _digits_info(m.group('num'), m.group('exp'))
        except ValueError:
            continue
        if not math.isfinite(value):
            continue
        negative = bool(m.group('neg'))
        out.append(NumTok(text=m.group(0), value=-value if negative else value, decimals=decimals, sig=sig,
                          start=m.start(), end=m.end(), num_start=m.start('num'),
                          num_end=m.end('exp') if m.group('exp') else m.end('num'), negative=negative,
                          dollar=bool(m.group('dollar')), percent=bool(m.group('pct')),
                          commas=',' in m.group('num')))
    return out


def numbers_in(text: str) -> list[float]:
    return [t.value for t in number_tokens(text)]


def close(a: float, b: float, rel: float = 1e-9) -> bool:
    return abs(a - b) <= rel * max(1.0, abs(a), abs(b))


def rounds_to(value: float, target: float, decimals: int) -> bool:
    """True when |value| rounded to `decimals` places gives |target| (half-unit window, inclusive)."""
    a, b = abs(value), abs(target)
    return abs(a - b) <= 0.5 * 10.0 ** (-decimals) + 1e-9 * max(1.0, b)


def format_number(value: float, decimals: int, *, commas: bool = False) -> str:
    """Fixed-point text of |value| with `decimals` places (never scientific notation)."""
    decimals = max(0, min(int(decimals), 12))
    return f'{abs(value):,.{decimals}f}' if commas else f'{abs(value):.{decimals}f}'


def short(value: float) -> str:
    """Compact, deterministic text for a float in messages (≤ 12 significant digits, no exponent
    for ordinary magnitudes)."""
    if value is None or not math.isfinite(value):
        return str(value)
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    text = f'{value:.12g}'
    if 'e' in text and 1e-6 <= abs(value) < 1e15:
        text = f'{value:.12f}'.rstrip('0').rstrip('.')
    return text


def replace_number(text: str, tok: NumTok, new_value: float, decimals: int, *,
                   drop_percent: bool = False) -> str:
    """`text` with `tok` replaced by `new_value` (signed), keeping currency, separators and percent."""
    body = format_number(new_value, decimals, commas=tok.commas)
    pct = text[tok.num_end:tok.end] if tok.percent and not drop_percent else ''
    sign = '-' if new_value < 0 and round(abs(new_value), max(0, decimals)) != 0 else ''
    return text[:tok.start] + sign + ('$' if tok.dollar else '') + body + pct + text[tok.end:]


def is_power_of_ten(value: float) -> bool:
    if value <= 0:
        return False
    k = round(math.log10(value))
    return -9 <= k <= 15 and close(value, 10.0 ** k)


def is_trivial(value: float, text: str) -> bool:
    """Constants a program uses without having read them: small integers (indices, round() digits,
    counts), and powers of ten (unit and percent conversions)."""
    a = abs(value)
    integral = a == int(a) and 'e' not in text.lower() and '.' not in text
    return (integral and a <= 12) or is_power_of_ten(a) or a == 0

# ---------------------------------------------------------------------------------------------
# Python literals in model-authored code
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Literal:
    text: str
    value: float
    decimals: int
    sig: int
    start: int                     # character offsets into the code
    end: int
    in_string: bool = False        # a quoted number such as float("1,234.5")
    trivial: bool = False


_FALLBACK_LIT = re.compile(r'(?<![\w.])(\d[\d_]*(?:\.\d*)?|\.\d+)([eE][+-]?\d+)?(?![\w.])')
_STRING_NUMBER = re.compile(r'^\s*[-−]?\$?\s?(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+)\s*%?\s*$')


def _literal(text: str, start: int, end: int, *, in_string: bool = False) -> Literal | None:
    raw = text.replace('_', '')
    if raw.lower().startswith(('0x', '0o', '0b')) or raw.lower().endswith('j'):
        return None
    mant, _, exp = raw.lower().partition('e')
    try:
        value, decimals, sig = _digits_info(mant, ('e' + exp) if exp else None)
    except ValueError:
        return None
    if not math.isfinite(value):
        return None
    return Literal(text=text, value=value, decimals=decimals, sig=sig, start=start, end=end,
                   in_string=in_string, trivial=is_trivial(value, text))


def code_literals(code: str) -> list[Literal]:
    """Numeric literals of `code` with character spans; quoted plain numbers are included with
    `in_string=True`. Falls back to a conservative regex when the code does not tokenize."""
    # Row starts exactly as tokenize reads rows (StringIO.readline splits on '\n' only); str.splitlines
    # also splits on '\r', '\x0c', ' ', ..., which would shift every later span.
    starts = [0]
    for line in io.StringIO(code).readlines():
        starts.append(starts[-1] + len(line))

    def offset(row: int, col: int) -> int:
        return starts[row - 1] + col if row - 1 < len(starts) else len(code)

    out: list[Literal] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(code).readline):
            if tok.type == tokenize.NUMBER:
                s, e = offset(*tok.start), offset(*tok.end)
                lit = _literal(tok.string, s, e)
                if lit:
                    out.append(lit)
            elif tok.type == tokenize.STRING:
                body = tok.string
                q = body.lstrip('rRbBuUfF')
                if q[:3] in ('"""', "'''") or not q or q[0] not in '"\'':
                    continue
                inner = q[1:-1]
                m = _STRING_NUMBER.match(inner)
                if not m:
                    continue
                s = offset(*tok.start) + (len(body) - len(q)) + 1 + m.start(1)
                lit = _literal(m.group(1).replace(',', ''), s, s + len(m.group(1)), in_string=True)
                if lit:
                    out.append(Literal(**{**lit.__dict__, 'text': m.group(1)}))
        return out
    except (tokenize.TokenError, IndentationError, SyntaxError):
        out = []
    for m in _FALLBACK_LIT.finditer(code):
        lit = _literal(m.group(0), m.start(), m.end())
        if lit:
            out.append(lit)
    return out

# ---------------------------------------------------------------------------------------------
# Value index: which earlier observation/compute a literal was typed from
# ---------------------------------------------------------------------------------------------


class ValueIndex:
    """Sorted |values| with the ref that showed them. `add` is O(1); queries merge pending adds
    lazily (Timsort on two sorted runs), so interleaved add/query stays cheap."""

    def __init__(self) -> None:
        self._keys: list[float] = []
        self._items: list[tuple[float, int, str]] = []   # (|value|, seq, ref)
        self._pending: list[tuple[float, int, str]] = []
        self._seq = 0

    def __len__(self) -> int:
        return len(self._items) + len(self._pending)

    def add(self, value: float | None, ref: str) -> None:
        if value is None or not isinstance(value, (int, float)) or not math.isfinite(value) or value == 0:
            return
        self._pending.append((abs(float(value)), self._seq, ref))
        self._seq += 1

    def _flush(self) -> None:
        if self._pending:
            self._items = sorted(self._items + self._pending)
            self._keys = [item[0] for item in self._items]
            self._pending = []

    def window(self, lo: float, hi: float) -> list[tuple[float, int, str]]:
        self._flush()
        return self._items[bisect_left(self._keys, lo):bisect_right(self._keys, hi)]

    def link(self, value: float, decimals: int, sig: int) -> dict | None:
        """Best source for a typed number: exact > rounded (at the literal's precision) > scaled by
        10^k (exact, then rounded; only for ≥ 3 significant digits). Ties go to the latest source.
        Returns {'ref', 'match', 'scale', 'source_value'} or None."""
        target = abs(value)
        if target == 0:
            return None
        eps = 1e-9 * max(1.0, target)
        half = 0.5 * 10.0 ** (-decimals)

        def best(k: int) -> tuple[str, tuple] | None:
            f = 10.0 ** k
            found = self.window((target - half - eps) / f, (target + half + eps) / f)
            if not found:
                return None
            exact = [it for it in found if abs(it[0] * f - target) <= eps]
            pick = max(exact or found, key=lambda it: it[1])
            return ('exact' if exact else 'rounded'), pick

        hit = best(0)
        if hit:
            kind, (v, _, ref) = hit
            return {'ref': ref, 'match': kind, 'scale': 0, 'source_value': v}
        if sig < MIN_SIG_FOR_SCALED:
            return None
        rounded = None
        for k in SCALES:
            hit = best(k)
            if hit and hit[0] == 'exact':
                return {'ref': hit[1][2], 'match': 'scaled', 'scale': k, 'source_value': hit[1][0]}
            if hit and rounded is None:
                rounded = (k, hit[1])
        if rounded:
            k, (v, _, ref) = rounded
            return {'ref': ref, 'match': 'scaled', 'scale': k, 'source_value': v}
        return None
