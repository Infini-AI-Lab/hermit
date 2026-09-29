"""Generic observation helpers for packs, and `RawPack`, the smallest valid contracts.Pack.

`raw_line_observations` turns any untagged tool output (rg/sed/cat/python prints) into one Observe
per line that contains a number: `locator` '<source>:L<line>', `text` the line (≤ 200 chars),
`attrs['values']` every number on it. Grounding predicates then see values the model read with raw
tools, which is what the design requires ("untagged outputs become observe events with raw text").
Packs call it for the lines their own tag parser did not claim.
"""
from __future__ import annotations

import re
from typing import Callable, Mapping, Sequence

from . import contracts as C
from .numeric import numbers_in


def raw_line_observations(output: str, *, source: str, call: int, t: float, next_ref: Callable[[], str],
                          attrs: Mapping | None = None, max_lines: int = 400,
                          numbers: Callable[[str], list[float]] = numbers_in) -> list[C.Observe]:
    out: list[C.Observe] = []
    for i, line in enumerate((output or '').split('\n'), 1):
        values = numbers(line)
        if not values:
            continue
        text = line.strip()
        out.append(C.Observe(kind='observe', ref=next_ref(), call=call, t=t, source=source,
                             locator=f'{source}:L{i}', text=text[:200],
                             value=values[0] if len(values) == 1 else None,
                             attrs={**(attrs or {}), C.VALUES_ATTR: values[:64]}))
        if len(out) >= max_lines:
            break
    return out


_DECIMALS = re.compile(r'(?:round(?:ed)?|to)\s+(?:the\s+)?(?:nearest\s+)?(\w+)\s+decimal', re.I)
_WORDS = {'zero': 0, 'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6}


def generic_question_spec(question: str) -> C.QuestionSpec:
    """A deliberately small, benchmark-neutral spec parser (packs override it)."""
    q = question or ''
    decimals: tuple = ()
    m = _DECIMALS.search(q)
    if m:
        word = m.group(1).lower()
        d = int(word) if word.isdigit() else _WORDS.get(word)
        if d is not None:
            decimals = (d,)
    low = q.lower()
    percent = ('percent' if re.search(r'\bpercent(age)?\b|%', low) and 'decimal form' not in low
               else 'decimal' if re.search(r'\b(decimal form|as a decimal|as a fraction)\b', low)
               else 'unspecified')
    value_type = 'year' if re.search(r'\b(which|what) year\b', low) else 'number'
    keywords = tuple(sorted({w for w in re.findall(r'[a-z]{4,}', low)}))
    return C.QuestionSpec(decimals=decimals, percent_form=percent, value_type=value_type, keywords=keywords)


class RawPack:
    """contracts.Pack with raw-line observations only: no tools, no pack predicates or rules.
    Used for offline ledger checks on recorded traffic and as the neutral default in tests."""
    name = 'raw'
    answer_kind = 'text'
    tools: Mapping[str, str] = {}

    def question_spec(self, question: str) -> C.QuestionSpec:
        return generic_question_spec(question)

    def canonical(self, answer: str) -> str:
        return ' '.join(str(answer or '').lower().replace(',', '').split())

    def numbers(self, text: str) -> list[float]:
        return numbers_in(text)

    def observations(self, command: str, output: str, *, source: str, call: int, t: float,
                     next_ref: Callable[[], str]) -> list[C.Observe]:
        return raw_line_observations(output, source=source, call=call, t=t, next_ref=next_ref)

    def predicates(self) -> Mapping[str, C.Predicate]:
        return {}

    def offers(self) -> Mapping[str, C.Offer]:
        return {}

    def seed_rules(self) -> list[C.Rule]:
        return []

    def seed_text(self) -> str:
        return ''

    def leak_screen(self, text: str, released_answers: Sequence[str]) -> list[str]:
        """Descriptions of leaks (never the leaked values themselves)."""
        low = self.canonical(text)
        return [f'released answer #{i} appears in the text' for i, a in enumerate(released_answers)
                if len(self.canonical(a)) >= 4 and self.canonical(a) in low]

    def task_conventions(self) -> str:
        return ''
