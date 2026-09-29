"""Core predicates: benchmark-neutral `when` vocabulary for rules (DESIGN §2 L4).

Calling convention (also for pack predicates): `pred(ctx: RuleContext, **args) -> list[Binding]`.
Pure and deterministic; an empty list means "no match". Every predicate looks only at the events
*before* the candidate being checked (so a counterfactual candidate sees the same evidence) and at
`ctx.spec`. Binding keys each predicate produces (templates and offers read them):

  count_mismatch        count{expected, got}               (got = numbers + allowed words the answer holds)
  not_from_compute      value{text, value, decimals, index, ...}
  scaled_100            value{...}, compute{ref, sha, value}, factor (100 | 0.01), direction ('x100' | 'div100'),
                        relation, why (ready-made phrases for templates)
  ungrounded_inputs     compute{ref, sha}, input{literal, value}   (numbers stated in the question count as seen)
  competing_candidates  alt{answer, source}          (source: '--alt' or the earlier candidate's ref)

`value` dicts are numeric tokens of the candidate answer (numeric.NumTok.as_dict + 'index').
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from typing import Any

from . import contracts as C
from .numeric import NumTok, ValueIndex, close, number_tokens, rounds_to

GATE_PREFIX = '[harness:gate] '
ACCEPT_PREFIX = GATE_PREFIX + 'recorded'
NON_NUMERIC_TYPES = ('text', 'artifact', 'date', 'year')


@dataclass
class Facts:
    """Per-evaluation view of the evidence, built once per RuleContext."""
    prior: list[C.Event]
    computes: list[C.Compute]
    observes: list[C.Observe]
    candidates: list[C.Candidate]
    notes: list[C.Note]
    tokens: list[NumTok]
    obs_index: ValueIndex = field(default_factory=ValueIndex)
    by_ref: dict[str, C.Event] = field(default_factory=dict)
    pos: dict[str, int] = field(default_factory=dict)


def prior_events(ctx: C.RuleContext) -> list[C.Event]:
    events = list(ctx.events)
    for i, e in enumerate(events):
        if isinstance(e, C.Candidate) and e.ref and e.ref == ctx.candidate.ref:
            return events[:i]
    return events


def facts(ctx: C.RuleContext) -> Facts:
    key = (id(ctx.events), len(ctx.events), ctx.candidate.ref, ctx.candidate.answer)
    cached = ctx.__dict__.get('_facts')
    if cached and cached[0] == key:
        return cached[1]
    prior = prior_events(ctx)
    f = Facts(prior=prior, computes=[e for e in prior if isinstance(e, C.Compute)],
              observes=[e for e in prior if isinstance(e, C.Observe)],
              candidates=[e for e in prior if isinstance(e, C.Candidate)],
              notes=[e for e in prior if isinstance(e, C.Note)],
              tokens=number_tokens(ctx.candidate.answer or ''), by_ref={e.ref: e for e in prior},
              pos={e.ref: i for i, e in enumerate(prior)})
    for o in f.observes:
        f.obs_index.add(o.value, o.ref)
        for v in (o.attrs or {}).get(C.VALUES_ATTR) or []:
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                f.obs_index.add(float(v), o.ref)
    ctx.__dict__['_facts'] = (key, f)
    return f


def numeric_answer(ctx: C.RuleContext) -> bool:
    return ctx.spec.value_type not in NON_NUMERIC_TYPES and bool((ctx.candidate.answer or '').strip())


def token_dict(tok: NumTok, index: int) -> dict[str, Any]:
    return tok.as_dict(index)


def from_computes(f: Facts, tok: NumTok) -> list[tuple[C.Compute, int]]:
    """Computes (latest first) and the index of the printed value `tok` equals after rounding."""
    out = []
    for k in reversed(f.computes):
        for j, v in enumerate(k.values):
            if rounds_to(v, tok.value, tok.decimals):
                out.append((k, j))
                break
    return out


def observed(f: Facts, tok: NumTok) -> bool:
    x, h = abs(tok.value), 0.5 * 10.0 ** (-tok.decimals)
    return bool(f.obs_index.window(x - h - 1e-9 * max(1.0, x), x + h + 1e-9 * max(1.0, x)))


def same_answer(pack: C.Pack, a: str, b: str) -> bool:
    """Equal after the pack's canonical form, or equal as number lists (formatting-only change)."""
    if pack.canonical(a or '') == pack.canonical(b or ''):
        return True
    na, nb = pack.numbers(a or ''), pack.numbers(b or '')
    return bool(na) and len(na) == len(nb) and all(abs(x - y) <= 1e-9 * max(1.0, abs(x)) for x, y in zip(na, nb))

# ---------------------------------------------------------------------------------------------


def allowed_word_hits(answer: str, words) -> int:
    """How many whole-word occurrences of the question's quoted words (spec.allowed_words) the answer holds."""
    wanted = {str(w).lower() for w in words or () if str(w).strip()}
    if not wanted:
        return 0
    return sum(1 for w in re.findall(r"[a-z]+(?:-[a-z]+)*", (answer or '').lower()) if w in wanted)


def count_mismatch(ctx: C.RuleContext, *, n: int | None = None) -> list[C.Binding]:
    """The answer holds a different number of values than the question asks for (spec.n_values).
    Words the question quotes as possible values (spec.allowed_words, e.g. 'surplus'/'deficit') may fill
    slots: the answer passes when its numbers alone, or its numbers plus those words, match the count."""
    expected = n if n is not None else ctx.spec.n_values
    if expected is None or ctx.spec.value_type in ('text', 'artifact') or not (ctx.candidate.answer or '').strip():
        return []
    numbers = len(ctx.pack.numbers(ctx.candidate.answer))
    got = numbers + allowed_word_hits(ctx.candidate.answer, getattr(ctx.spec, 'allowed_words', ()))
    if expected in (numbers, got):
        return []
    return [C.Binding({'count': {'expected': int(expected), 'got': got}})]


def not_from_compute(ctx: C.RuleContext, *, allow_observed: bool = True) -> list[C.Binding]:
    """A number of the answer equals no calc output after rounding to the answer's own precision
    (and, with allow_observed, is not a value some tool printed either: plain look-ups pass)."""
    if not numeric_answer(ctx):
        return []
    f = facts(ctx)
    out = []
    for i, tok in enumerate(f.tokens):
        if from_computes(f, tok) or (allow_observed and observed(f, tok)):
            continue
        out.append(C.Binding({'value': token_dict(tok, i)}))
    return out


def scaled_100(ctx: C.RuleContext, *, forms: tuple | list = ('decimal', 'unspecified')) -> list[C.Binding]:
    """The answer is 100× a calc output while the question does not ask for a percent ('x100', for
    spec.percent_form in `forms`), or 1/100 of one while it does ('div100')."""
    if not numeric_answer(ctx):
        return []
    f = facts(ctx)
    form = ctx.spec.percent_form
    out = []
    for i, tok in enumerate(f.tokens):
        if tok.value == 0:
            continue
        for k in reversed(f.computes):
            hit = None
            for v in k.values:
                if v == 0:
                    continue
                if form in forms and rounds_to(100.0 * v, tok.value, tok.decimals) \
                        and not rounds_to(v, tok.value, tok.decimals):
                    hit = ('x100', 100, v)
                elif form == 'percent' and rounds_to(v / 100.0, tok.value, tok.decimals) \
                        and not rounds_to(v, tok.value, tok.decimals):
                    hit = ('div100', 0.01, v)
                if hit:
                    break
            if hit:
                x100 = hit[0] == 'x100'
                out.append(C.Binding({'value': token_dict(tok, i), 'compute': {'ref': k.ref, 'sha': k.code_sha256[:12], 'value': hit[2]},
                                      'factor': hit[1], 'direction': hit[0],
                                      'relation': '100 times' if x100 else 'one hundredth of',
                                      'why': ('the question does not ask for a percent' if x100
                                              else 'the question asks for a percent')}))
                break
    return out


def question_values(ctx: C.RuleContext) -> list[float]:
    """Numbers the question itself states (the model saw them; they need no tool output)."""
    try:
        return [float(v) for v in ctx.pack.numbers(ctx.question or '')]
    except Exception:
        return []


def _from_question(value: Any, given: list[float]) -> bool:
    """A calc literal typed from the question: the stated number, its percent as a fraction
    ('3.5%' -> 0.035) or the growth factor (1.035 / 0.965)."""
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not given:
        return False
    v = abs(float(value))
    return any(close(v, abs(q)) or (abs(q) < 100 and (close(v * 100.0, abs(q)) or close(v, 1.0 + abs(q) / 100.0)
                                                      or close(v, 1.0 - abs(q) / 100.0))) for q in given)


def ungrounded_inputs(ctx: C.RuleContext, *, scope: str = 'candidate') -> list[C.Binding]:
    """Non-trivial literals in calc code that match no earlier observation, no earlier compute and no
    number the question states.
    scope='candidate': only the computes that produced a number of the answer, and the computes
    those consume (transitively); scope='all': every compute."""
    f = facts(ctx)
    if scope == 'all':
        chosen = list(reversed(f.computes))
    else:
        if not numeric_answer(ctx):
            return []
        seen: dict[str, C.Compute] = {}
        todo = [k for tok in f.tokens for k, _ in from_computes(f, tok)]
        while todo:
            k = todo.pop(0)
            if k.ref in seen:
                continue
            seen[k.ref] = k
            for inp in k.inputs:
                src = f.by_ref.get(inp.get('observe') or '')
                if isinstance(src, C.Compute):
                    todo.append(src)
        chosen = sorted(seen.values(), key=lambda k: -f.pos[k.ref])
    given = question_values(ctx)
    out = []
    for k in chosen:
        for inp in k.inputs:
            if inp.get('trivial') or inp.get('observe') or _from_question(inp.get('value'), given):
                continue
            out.append(C.Binding({'compute': {'ref': k.ref, 'sha': k.code_sha256[:12]},
                                  'input': {'literal': inp.get('literal'), 'value': inp.get('value')}}))
    return out


def _gate_finding_after(f: Facts, cand: C.Candidate) -> bool:
    """A gate finding (not an 'accepted' notice) was shown after `cand` and before the next candidate.
    Enforcement notes wrap the gate message in the controller's own text, so an 'accepted' notice is
    recognised anywhere in the note."""
    start = f.pos[cand.ref]
    for e in f.prior[start + 1:]:
        if isinstance(e, C.Candidate):
            return False
        if isinstance(e, C.Note) and e.reason in ('gate', 'enforce') and ACCEPT_PREFIX not in e.text:
            return True
    return False


def competing_candidates(ctx: C.RuleContext) -> list[C.Binding]:
    """The model's own --alt, or an earlier candidate it moved away from without being told to
    (a candidate followed by a gate finding is excluded, so accepting an offer never ping-pongs)."""
    f = facts(ctx)
    cur = ctx.candidate.answer or ''
    out, seen = [], []

    def push(answer: str, source: str) -> None:
        if not answer.strip() or same_answer(ctx.pack, answer, cur):
            return
        if any(same_answer(ctx.pack, answer, s) for s in seen):
            return
        seen.append(answer)
        out.append(C.Binding({'alt': {'answer': answer, 'source': source}}))

    push(ctx.candidate.alt or '', '--alt')
    for c in reversed(f.candidates):
        if c.answer and not _gate_finding_after(f, c):
            push(c.answer, c.ref)
    return out


REGISTRY: dict[str, C.Predicate] = {
    'count_mismatch': count_mismatch,
    'not_from_compute': not_from_compute,
    'scaled_100': scaled_100,
    'ungrounded_inputs': ungrounded_inputs,
    'competing_candidates': competing_candidates,
}


def binding_key(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)
