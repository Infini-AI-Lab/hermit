"""Toy pack: a complete contracts.Pack over the generated Widget Bulletin corpus (corpus.py).

Used for end-to-end tests of the core (ledger -> gate -> offers -> replay/counterfactual) without
OfficeQA. Pack-specific pieces:
- observations: `lookup` tags ⟦toy file:line|period|row|raw⟧ -> Observe with attrs toy.file,
  toy.line, toy.table, toy.period, toy.row, toy.printing, toy.revised; other lines -> raw-line
  observations (core helper);
- question_spec: quarters named ('Q2 2020'), decimals, percent/decimal form, 'both'/'two values',
  'as first printed' (pins_date);
- predicates `later_printing` and `period_mismatch`, both offering the core `recompute`.
Items, references and the grader are in items.py.
"""
from __future__ import annotations

from pathlib import Path
import re
from typing import Callable, Mapping, Sequence

from harness_core import contracts as C
from harness_core.numeric import close, numbers_in
from harness_core.observe import raw_line_observations
from harness_core.predicates import facts, from_computes

from . import corpus
from .tools import TAG_OPEN

TOOLS_SCRIPT = Path(__file__).resolve().parent / 'tools.py'
USAGE = '''lookup <file> [<row> ...] [--table W-1|W-2]
    prints the cells of the named rows (all rows when none is named) of one bulletin file, each cell with a
    tag ⟦toy <file>:<line>|<period>|<row>|<value>⟧ (a trailing r marks a revised value)
    files: ''' + ', '.join(corpus.FILES) + '\n'


def tool_wrappers() -> dict[str, str]:
    """kit.build_kit `tool_wrappers` for the toy L1 tool (orchestration adds harness_core/tools/calc.py)."""
    return {'lookup': str(TOOLS_SCRIPT)}

TAG_RE = re.compile(re.escape(TAG_OPEN) + r'(?P<file>[\w.]+):(?P<line>\d+)\|(?P<period>[^|⟧]+)\|'
                    r'(?P<row>[^|⟧]+)\|(?P<raw>[^|⟧]+)⟧')
_WORDS = {'zero': 0, 'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5}
CELLS = {(c.file, c.line, c.period): c for c in corpus.all_cells()}


def cell_of(obs: C.Observe) -> corpus.Cell | None:
    a = obs.attrs or {}
    return CELLS.get((a.get('toy.file'), a.get('toy.line'), a.get('toy.period')))


def linked_cells(ctx: C.RuleContext) -> list[tuple[C.Compute, dict, C.Observe, corpus.Cell]]:
    """(compute, input, observation, corpus cell) for the computes that produced the answer."""
    f = facts(ctx)
    out, seen = [], set()
    for tok in f.tokens:
        for k, _ in from_computes(f, tok):
            if k.ref in seen:
                continue
            seen.add(k.ref)
            for inp in k.inputs:
                obs = f.by_ref.get(inp.get('observe') or '')
                if isinstance(obs, C.Observe) and cell_of(obs):
                    out.append((k, inp, obs, cell_of(obs)))
    return out


def later_printing(ctx: C.RuleContext) -> list[C.Binding]:
    """A cell used by the answer's calc has a later printing with a different value, and the
    question does not pin a printing. One binding per compute (all stale cells substituted)."""
    if ctx.spec.pins_date:
        return []
    by_compute: dict[str, dict] = {}
    for k, inp, obs, cell in linked_cells(ctx):
        newest = corpus.latest(cell)
        if newest.printing == cell.printing or close(newest.value, cell.value):
            continue
        b = by_compute.setdefault(k.ref, {'compute': {'ref': k.ref, 'sha': k.code_sha256[:12]},
                                          'substitutions': []})
        b.setdefault('cell', {'ref': obs.ref, 'locator': obs.locator, 'value': cell.value, 'period': cell.period})
        b.setdefault('later', {'locator': newest.locator, 'value': newest.value})
        if all(s['from'] != obs.ref for s in b['substitutions']):
            b['substitutions'].append({'from': obs.ref, 'value': newest.value, 'label': newest.locator})
    return [C.Binding(v) for v in by_compute.values()]


def period_mismatch(ctx: C.RuleContext) -> list[C.Binding]:
    """A cell used by the answer's calc is for a quarter the question does not name, and exactly one
    named quarter is not covered by the other cells of the same row: substitute that quarter."""
    want = set(ctx.spec.periods)
    if not want:
        return []
    links = linked_cells(ctx)
    out = []
    for k, inp, obs, cell in links:
        if cell.period in want:
            continue
        same_row = {c.period for kk, _, _, c in links if kk.ref == k.ref and c.row == cell.row and c is not cell}
        missing = sorted(want - same_row)
        if len(missing) != 1:
            continue
        try:
            target = corpus.find(cell.row, missing[0], table=cell.table)
        except KeyError:
            continue
        out.append(C.Binding({'compute': {'ref': k.ref, 'sha': k.code_sha256[:12]},
                              'cell': {'ref': obs.ref, 'locator': obs.locator, 'value': cell.value, 'period': cell.period},
                              'want': {'period': missing[0], 'locator': target.locator, 'value': target.value},
                              'substitutions': [{'from': obs.ref, 'value': target.value, 'label': target.locator}]}))
    return out


def toy_seed_rules() -> list[C.Rule]:
    return [
        C.Rule(id='period_match', when=[{'pred': 'period_mismatch', 'args': {}}], priority=25, scope='pack',
               offer={'op': 'recompute', 'args': {}},
               finding='calc {compute.sha} uses {cell.locator} ({cell.period}); the question asks for {want.period} '
                       '({want.locator}).'),
        C.Rule(id='later_printing', when=[{'pred': 'later_printing', 'args': {}}], priority=30, scope='pack',
               offer={'op': 'recompute', 'args': {}},
               finding='calc {compute.sha} uses {cell.locator} ({cell.value}); the later printing {later.locator} '
                       'shows {later.value}. Convention: latest printing unless the question names one.'),
    ]


def question_spec(question: str) -> C.QuestionSpec:
    q = question or ''
    low = q.lower()
    periods = tuple(dict.fromkeys(f'Q{a} {b}' for a, b in re.findall(r'\bQ([1-4]) (20\d\d)\b', q)))
    decimals: tuple = ()
    m = re.search(r'round(?:ed)? to (\w+) decimal', low)
    if m:
        w = m.group(1)
        decimals = ((int(w) if w.isdigit() else _WORDS.get(w)),)
    elif 'nearest whole number' in low:
        decimals = (0,)
    n_values = 2 if re.search(r'\b(both|two (comma-separated )?values)\b', low) else 1
    percent = 'percent' if re.search(r'\bas a percentage\b|\bin percent\b', low) else \
        'decimal' if 'as a decimal' in low else 'unspecified'
    keywords = tuple(r.lower() for r in corpus.REGIONS if r.lower() in low)
    return C.QuestionSpec(n_values=n_values, list_format='comma-separated' in low, decimals=decimals,
                          percent_form=percent, value_type='number', periods=periods,
                          pins_date='as first printed' in low or 'first printing' in low, keywords=keywords)


class ToyPack:
    name = 'toy'
    answer_kind = 'text'
    tools: Mapping[str, str] = {'lookup': 'packs.toy.tools:lookup_main'}

    def question_spec(self, question: str) -> C.QuestionSpec:
        return question_spec(question)

    def canonical(self, answer: str) -> str:
        return ''.join(str(answer or '').lower().replace(',', ' ').replace('$', '').split())

    def numbers(self, text: str) -> list[float]:
        return numbers_in(text)

    def observations(self, command: str, output: str, *, source: str, call: int, t: float,
                     next_ref: Callable[[], str]) -> list[C.Observe]:
        out, rest = [], []
        for line in (output or '').split('\n'):
            tags = list(TAG_RE.finditer(line))
            if not tags:
                rest.append(line)
                continue
            for m in tags:
                raw = m.group('raw').strip()
                try:
                    value = float(raw.rstrip('r'))
                except ValueError:
                    continue
                cell = CELLS.get((m.group('file'), int(m.group('line')), m.group('period')))
                attrs = {'toy.file': m.group('file'), 'toy.line': int(m.group('line')),
                         'toy.period': m.group('period'), 'toy.row': m.group('row'), 'toy.revised': raw.endswith('r'),
                         'toy.table': cell.table if cell else '', 'toy.printing': cell.printing if cell else ''}
                out.append(C.Observe(kind='observe', ref=next_ref(), call=call, t=t, source=source,
                                     locator=f"{m.group('file')}:{m.group('line')}", text=m.group(0),
                                     value=value, attrs=attrs))
        out += raw_line_observations('\n'.join(rest), source=source, call=call, t=t, next_ref=next_ref)
        return out

    def predicates(self) -> Mapping[str, C.Predicate]:
        return {'later_printing': later_printing, 'period_mismatch': period_mismatch}

    def offers(self) -> Mapping[str, C.Offer]:
        return {}

    def seed_rules(self) -> list[C.Rule]:
        return toy_seed_rules()

    def seed_text(self) -> str:
        return ('Use the latest printing of a figure unless the question names a printing. '
                'Use the quarter the question names. Give a decimal (not a percent) when asked for a decimal. '
                'Give as many values as the question asks for.')

    def leak_screen(self, text: str, released_answers: Sequence[str]) -> list[str]:
        found = {round(abs(v), 6) for v in numbers_in(text)}
        return [f'released answer #{i} appears in the text' for i, a in enumerate(released_answers)
                if any(round(abs(v), 6) in found for v in numbers_in(a) if abs(v) >= 10)]

    def task_conventions(self) -> str:
        return 'Figures: use the latest printing unless the question names a printing.'

    def usage(self) -> str:
        return USAGE


PACK = ToyPack()
