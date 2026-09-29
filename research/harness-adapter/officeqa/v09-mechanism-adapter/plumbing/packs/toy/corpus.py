"""Toy corpus: three tiny 'Widget Bulletin' text files with two tables each, generated in code.

The 2020 figures exist in two printings: `widgets_2020_a.txt` (first printing) and
`widgets_2020_b.txt` (revised printing; revised cells carry an 'r' suffix). This gives the toy the
same shapes as OfficeQA's hard cases (first vs latest printing, wrong period, percent vs decimal)
at a size where every number is known. Deterministic; `make_corpus(root)` writes the files.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

REGIONS = ('North', 'South', 'East', 'West')
SHIPMENTS = {   # thousands of units, by quarter
    2020: {'North': (120.5, 130.25, 128.0, 140.75), 'South': (98.4, 101.2, 97.6, 110.3),
           'East': (75.0, 80.5, 82.25, 90.0), 'West': (60.1, 62.3, 65.7, 70.2)},
    2021: {'North': (135.0, 142.5, 139.75, 150.0), 'South': (104.0, 108.6, 103.9, 115.5),
           'East': (88.0, 91.5, 95.25, 101.0), 'West': (66.4, 68.0, 71.9, 75.3)},
}
REVISIONS_2020 = {('North', 1): 131.5, ('East', 3): 91.5, ('South', 0): 98.9}   # (region, quarter index)
PRICES = {2020: (2.5, 2.55, 2.6, 2.62), 2021: (2.7, 2.75, 2.8, 2.85)}           # dollars per widget
FILES = {'widgets_2020_a.txt': (2020, 'a', '2021-01'), 'widgets_2020_b.txt': (2020, 'b', '2021-07'),
         'widgets_2021_a.txt': (2021, 'a', '2022-01')}


@dataclass(frozen=True)
class Cell:
    file: str
    line: int
    table: str
    period: str          # 'Q2 2020'
    row: str             # region, 'Total' or 'Price'
    raw: str             # as printed ('131.5r')
    value: float
    year: int
    printing: str        # 'a' first, 'b' revised
    revised: bool

    @property
    def locator(self) -> str:
        return f'{self.file}:{self.line}'


def _fmt(value: float) -> str:
    return f'{value:g}'


def render(file: str) -> tuple[str, list[Cell]]:
    year, printing, published = FILES[file]
    label = 'first printing' if printing == 'a' else 'revised printing'
    lines = [f'Widget Bulletin: {year} annual, {label} (published {published})', '']
    cells: list[Cell] = []
    periods = [f'Q{q} {year}' for q in range(1, 5)]

    def table(name: str, title: str, rows: list[tuple[str, list[tuple[float, bool]]]]) -> None:
        lines.append(f'Table {name}. {title}')
        lines.append('| Row | ' + ' | '.join(periods) + ' |')
        for row, values in rows:
            raws = [_fmt(v) + ('r' if rev else '') for v, rev in values]
            lines.append(f'| {row} | ' + ' | '.join(raws) + ' |')
            for q, ((v, rev), raw) in enumerate(zip(values, raws)):
                cells.append(Cell(file, len(lines), name, periods[q], row, raw, v, year, printing, rev))
        lines.append('')

    rows = []
    totals = [0.0] * 4
    for region in REGIONS:
        vals = []
        for q, v in enumerate(SHIPMENTS[year][region]):
            rev = printing == 'b' and (region, q) in REVISIONS_2020
            v = REVISIONS_2020[(region, q)] if rev else v
            totals[q] += v
            vals.append((v, rev))
        rows.append((region, vals))
    rows.append(('Total', [(round(t, 2), False) for t in totals]))
    table('W-1', f'Widgets shipped by region, {year} (thousands of units)', rows)
    table('W-2', f'Average price per widget, {year} (dollars)', [('Price', [(p, False) for p in PRICES[year]])])
    return '\n'.join(lines) + '\n', cells


def all_cells() -> list[Cell]:
    return [c for f in FILES for c in render(f)[1]]


def make_corpus(root: str | Path) -> Path:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    for f in FILES:
        (root / f).write_text(render(f)[0], encoding='utf-8')
    return root


def latest(cell: Cell, cells: list[Cell] | None = None) -> Cell:
    """The latest printing of the same table/row/period."""
    same = [c for c in (cells or all_cells()) if (c.table, c.row, c.period) == (cell.table, cell.row, cell.period)]
    return max(same, key=lambda c: c.printing)


def find(row: str, period: str, *, printing: str | None = None, table: str = 'W-1') -> Cell:
    """The cell for row/period (latest printing unless `printing` is given)."""
    same = [c for c in all_cells() if c.table == table and c.row == row and c.period == period
            and (printing is None or c.printing == printing)]
    if not same:
        raise KeyError((row, period, printing))
    return max(same, key=lambda c: c.printing)
