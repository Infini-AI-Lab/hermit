"""Core offers: the engine derives a concrete alternative answer itself, so the actor only chooses.

Calling convention (also for pack offers): `offer(ctx: RuleContext, binding: Binding, **args)
-> OfferResult | None`. None = no usable alternative (the finding may still be shown without one).
Answers keep the candidate's text and formatting; only the affected number changes, rounded like
the candidate (or to spec.decimals when the question states them).

  unscale     for scaled_100: the calc output the answer is 100× (or 1/100) of.
  use_alt     for competing_candidates: the model's own alternative.
  recompute   re-executes a recorded calc with substituted inputs in the isolated runner.
              Substitutions come from rule args {'from': '<dotted binding path>', 'to': '<path>'}
              (each resolving to a ref or a dict with 'ref'), or from binding key 'substitutions':
              [{'from': '<ref>', 'to': '<ref>'} | {'from': '<ref>', 'value': float, 'label': str}].
              The compute is binding['compute']['ref'] if present, else the latest earlier compute
              that has an input linked to a 'from' ref (preferring one that produced the answer).
"""
from __future__ import annotations

from typing import Any

from . import contracts as C
from .numeric import NumTok, number_tokens, numbers_in, rounds_to, replace_number, short
from .predicates import facts, from_computes, same_answer

MAX_DERIVATION = 200


def lookup(values: Any, path: str) -> Any:
    """Dotted lookup in dicts/lists only (no attribute access)."""
    cur = values
    for part in str(path).split('.'):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


def answer_decimals(ctx: C.RuleContext, index: int, default: int) -> int:
    d = tuple(ctx.spec.decimals or ())
    if index < len(d) and d[index] is not None:
        return int(d[index])
    if len(d) == 1 and d[0] is not None:
        return int(d[0])
    return max(0, int(default))


def _clip(text: str) -> str:
    raw = text.encode('utf-8')
    return text if len(raw) <= MAX_DERIVATION else raw[:MAX_DERIVATION - 3].decode('utf-8', 'ignore') + '…'


def _token(ctx: C.RuleContext, binding: C.Binding, key: str = 'value') -> tuple[NumTok, int] | None:
    v = binding.values.get(key)
    toks = number_tokens(ctx.candidate.answer or '')
    if isinstance(v, dict) and isinstance(v.get('index'), int) and 0 <= v['index'] < len(toks):
        return toks[v['index']], v['index']
    return None


def _result(ctx: C.RuleContext, answer: str, derivation: str, sha: str = '') -> C.OfferResult | None:
    if not answer.strip() or same_answer(ctx.pack, answer, ctx.candidate.answer or ''):
        return None
    return C.OfferResult(answer=answer, derivation=_clip(derivation), compute_sha256=sha)


def unscale(ctx: C.RuleContext, binding: C.Binding, **_: Any) -> C.OfferResult | None:
    got = _token(ctx, binding)
    comp = binding.values.get('compute') or {}
    if not got or not isinstance(comp.get('value'), (int, float)):
        return None
    tok, i = got
    v = float(comp['value'])
    x100 = binding.values.get('direction', 'x100') == 'x100'
    d = answer_decimals(ctx, i, tok.decimals + 2 if x100 else tok.decimals - 2)
    new = abs(v) * (-1 if tok.value < 0 else 1)
    answer = replace_number(ctx.candidate.answer, tok, new, d, drop_percent=x100)
    how = 'is 100x' if x100 else 'is 1/100 of'
    name = f"calc {comp['sha']}" if comp.get('sha') else comp.get('ref', '?')
    return _result(ctx, answer, f'{name} printed {short(v)}; your answer {how} it')


def use_alt(ctx: C.RuleContext, binding: C.Binding, **_: Any) -> C.OfferResult | None:
    alt = binding.values.get('alt') or {}
    if not isinstance(alt.get('answer'), str):
        return None
    source = alt.get('source') or '?'
    how = 'your --alt' if source == '--alt' else f'your earlier candidate {source}'
    return _result(ctx, alt['answer'], how)


def _event(ctx: C.RuleContext, ref: str) -> C.Event | None:
    for e in ctx.events:
        if e.ref == ref:
            return e
    return None


def _event_value(e: C.Event | None) -> float | None:
    if isinstance(e, C.Observe):
        if e.value is not None:
            return float(e.value)
        vals = (e.attrs or {}).get(C.VALUES_ATTR) or []
        return float(vals[0]) if len(vals) == 1 else None
    if isinstance(e, C.Compute) and len(e.values) == 1:
        return float(e.values[0])
    return None


def _ref_of(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get('ref'), str):
        return value['ref']
    return None


def _substitutions(ctx: C.RuleContext, binding: C.Binding, args: dict) -> list[dict]:
    raw: list[dict] = []
    if 'from' in args and 'to' in args:
        raw.append({'from': lookup(binding.values, args['from']), 'to': lookup(binding.values, args['to'])})
    else:
        raw = [s for s in binding.values.get('substitutions') or [] if isinstance(s, dict)]
    subs = []
    for s in raw:
        src = _ref_of(s.get('from'))
        if not src:
            continue
        if isinstance(s.get('value'), (int, float)) and not isinstance(s.get('value'), bool):
            subs.append({'from': src, 'value': float(s['value']), 'label': str(s.get('label') or s.get('locator') or '')})
            continue
        to = s.get('to')
        dst = _ref_of(to)
        value = to.get('value') if isinstance(to, dict) and isinstance(to.get('value'), (int, float)) else None
        event = _event(ctx, dst) if dst else None
        value = value if value is not None else _event_value(event)
        if value is None:
            continue
        label = event.locator if isinstance(event, C.Observe) and event.locator else (dst or '')
        subs.append({'from': src, 'value': float(value), 'label': label})
    return subs


def _source_sign(ctx: C.RuleContext, ref: str, literal_value: float, scale: int) -> int:
    """Sign of the value the literal was typed from (the code carries its own sign)."""
    e = _event(ctx, ref)
    vals: list = []
    if isinstance(e, C.Observe):
        vals = ([e.value] if e.value is not None else []) + list((e.attrs or {}).get(C.VALUES_ATTR) or [])
    elif isinstance(e, C.Compute):
        vals = list(e.values)
    vals = [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if not vals:
        return 1
    target = abs(literal_value) / 10.0 ** scale
    best = min(vals, key=lambda v: abs(abs(v) - target))
    return -1 if best < 0 else 1


def recompute(ctx: C.RuleContext, binding: C.Binding, **args: Any) -> C.OfferResult | None:
    subs = _substitutions(ctx, binding, dict(args))
    if not subs:
        return None
    f = facts(ctx)
    froms = {s['from'] for s in subs}
    comp = None
    want = _ref_of(binding.values.get('compute'))
    if want:
        comp = next((k for k in f.computes if k.ref == want), None)
    else:
        linked = [k for k in reversed(f.computes) if any(inp.get('observe') in froms for inp in k.inputs)]
        made = {c.ref for t in f.tokens for c, _ in from_computes(f, t)}
        producing = [k for k in linked if k.ref in made]
        comp = (producing or linked or [None])[0]
    if comp is None or len(comp.code_sha256) != 64 or C.sha256_text(comp.code) != comp.code_sha256:
        return None                     # unknown or truncated code cannot be re-executed faithfully
    edits = []
    for s in subs:
        for inp in comp.inputs:
            if inp.get('observe') != s['from'] or inp.get('trivial') or not inp.get('span'):
                continue
            scale = int(inp.get('scale') or 0)
            text = short(abs(s['value']) * 10.0 ** scale)
            if (s['value'] < 0) != (_source_sign(ctx, s['from'], float(inp.get('value') or 0), scale) < 0):
                text = f'-{text}' if inp.get('in_string') else f'(-{text})'
            edits.append((int(inp['span'][0]), int(inp['span'][1]), text))
    if not edits:
        return None
    code = comp.code
    for start, end, text in sorted(edits, reverse=True):
        code = code[:start] + text + code[end:]
    if code == comp.code:
        return None
    ok, out = ctx.runner.run(code)
    if not ok:
        return None
    # The gate records the run of the offer it shows as a Compute, so an accepted offer is itself
    # a calc output (recompute_final stays silent on it).
    ctx.__dict__.setdefault('_offer_runs', {})[C.sha256_text(code)] = (code, out)
    new_values = numbers_in(out)
    tokens = f.tokens
    pick = None
    for i, tok in enumerate(tokens):
        for j, v in enumerate(comp.values):
            if rounds_to(v, tok.value, tok.decimals):
                pick = (i, j)
                break
        if pick:
            break
    if pick is None and len(tokens) == 1 and len(comp.values) == 1:
        pick = (0, 0)
    if pick is None:
        return None
    i, j = pick
    if len(new_values) == len(comp.values):
        nv = new_values[j]
    elif len(new_values) == 1:
        nv = new_values[0]
    else:
        return None
    tok = tokens[i]
    rel = -1 if (tok.value < 0) != (comp.values[j] < 0) else 1
    answer = replace_number(ctx.candidate.answer, tok, rel * nv, answer_decimals(ctx, i, tok.decimals))
    labels = '; '.join(f"{s['label'] or short(s['value'])} in place of {_label(ctx, s['from'])}" for s in subs)
    return _result(ctx, answer, f'calc {comp.code_sha256[:12]} re-run with {labels}', C.sha256_text(code))


def _label(ctx: C.RuleContext, ref: str) -> str:
    e = _event(ctx, ref)
    return e.locator if isinstance(e, C.Observe) and e.locator else ref


REGISTRY: dict[str, C.Offer] = {'unscale': unscale, 'use_alt': use_alt, 'recompute': recompute}
