"""Toy items: 10 synthetic questions over the toy corpus, synthetic references, strict grader.

Toy only (never real benchmark data). References follow the toy convention: latest printing
unless the question names one; ROUND_HALF_UP at the stated precision.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from harness_core import contracts as C
from harness_core.numeric import number_tokens

from . import corpus


def _round(value: float, decimals: int) -> str:
    q = Decimal(1).scaleb(-decimals)
    return str(Decimal(repr(value)).quantize(q, rounding=ROUND_HALF_UP))


def _v(row: str, period: str, printing: str | None = None, table: str = 'W-1') -> float:
    return corpus.find(row, period, printing=printing, table=table).value


QUESTIONS = [
    'What is the combined number of widgets shipped by the North region in Q1 2020 and Q2 2020, '
    'in thousands of units? Round to two decimal places.',
    'By what percentage did South region shipments change from Q1 2020 to Q4 2020? '
    'Answer as a percentage rounded to two decimal places.',
    'What was the growth rate of East region shipments from Q1 2021 to Q4 2021, as a decimal? '
    'Round to four decimal places.',
    'What was the average quarterly number of widgets shipped by the East region in Q1 2020, Q2 2020, '
    'Q3 2020 and Q4 2020, in thousands of units? Round to one decimal place.',
    "Give both the West region's shipments in Q3 2020 and in Q3 2021, in thousands of units, as two "
    'comma-separated values.',
    'What was the ratio of North to South shipments in Q1 2020 as first printed? Round to three decimal places.',
    'What was the change in total widgets shipped from Q2 2020 to Q2 2021, in thousands of units? '
    'Round to two decimal places.',
    'What was the average price per widget in Q3 2021, in dollars? Round to two decimal places.',
    "What was the East region's revenue in Q1 2021, in thousands of dollars (shipments in thousands of units "
    'times the average price)? Round to the nearest whole number.',
    'What was the growth rate of West region shipments from Q1 2021 to Q4 2021, as a decimal? '
    'Round to four decimal places.',
]
ITEMS: list[C.Item] = [C.Item(f'toy{i:02d}', i, q) for i, q in enumerate(QUESTIONS, 1)]


def _references() -> dict[str, str]:
    return {
        'toy01': _round(_v('North', 'Q1 2020') + _v('North', 'Q2 2020'), 2),
        'toy02': _round((_v('South', 'Q4 2020') - _v('South', 'Q1 2020')) / _v('South', 'Q1 2020') * 100, 2),
        'toy03': _round((_v('East', 'Q4 2021') - _v('East', 'Q1 2021')) / _v('East', 'Q1 2021'), 4),
        'toy04': _round(sum(_v('East', f'Q{q} 2020') for q in range(1, 5)) / 4, 1),
        'toy05': f"{_round(_v('West', 'Q3 2020'), 1)}, {_round(_v('West', 'Q3 2021'), 1)}",
        'toy06': _round(_v('North', 'Q1 2020', 'a') / _v('South', 'Q1 2020', 'a'), 3),
        'toy07': _round(_v('Total', 'Q2 2021') - _v('Total', 'Q2 2020'), 2),
        'toy08': _round(_v('Price', 'Q3 2021', table='W-2'), 2),
        'toy09': _round(_v('East', 'Q1 2021') * _v('Price', 'Q1 2021', table='W-2'), 0),
        'toy10': _round((_v('West', 'Q4 2021') - _v('West', 'Q1 2021')) / _v('West', 'Q1 2021'), 4),
    }


REFERENCES = _references()


def grade(answer: str, reference: str) -> bool:
    """Strict: same count of numbers, each equal to the reference at the reference's precision."""
    got, want = number_tokens(answer or ''), number_tokens(reference or '')
    if not want or len(got) != len(want):
        return False
    return all(round(g.value, w.decimals) == round(w.value, w.decimals)
               and abs(g.value - w.value) <= 0.5 * 10.0 ** (-w.decimals) + 1e-9 for g, w in zip(got, want))
