"""Trusted rule engine (contracts.Engine): evaluates declarative rules, in-process, deterministically.

A rule fires when every `when` predicate returns at least one binding and the bindings join (dicts
merged; keys present on both sides must be equal). Predicates and offers are looked up by name in
the core registries first, then in `ctx.pack.predicates()` / `ctx.pack.offers()`; a rule naming an
unknown predicate or offer is reported 'n/a', never 'silent' or 'passed'. A predicate or offer that
raises makes its rule 'error' and never breaks the evaluation of the others.

Finding templates are rendered without str.format: only `{name.sub.key}` dotted lookups into the
binding (plus `candidate`, `offer`, `rule`) are substituted, each value ≤ 80 chars, the whole text
≤ 200 bytes; anything else in braces stays literal text, so a template cannot reach attributes,
format specs or other objects.
"""
from __future__ import annotations

import re
import time
from typing import Any, Mapping, Sequence

from . import contracts as C
from . import offers as core_offers
from . import predicates as core_predicates
from .numeric import short
from .offers import lookup
from .predicates import binding_key, same_answer

FIELD_RE = re.compile(r'\{([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*)\}')
MAX_FINDING = 200
MAX_FIELD = 80
MAX_JOIN = 64
OFFER_TRIES = 3


def clip_bytes(text: str, max_bytes: int) -> str:
    raw = text.encode('utf-8')
    if len(raw) <= max_bytes:
        return text
    return raw[:max(0, max_bytes - 3)].decode('utf-8', 'ignore') + '…'


def _field_text(value: Any) -> str:
    if value is None or isinstance(value, (dict, list, tuple)):
        return '?'
    if isinstance(value, bool):
        return 'yes' if value else 'no'
    if isinstance(value, (int, float)):
        return short(float(value))
    text = ' '.join(str(value).split())
    return text if len(text) <= MAX_FIELD else text[:MAX_FIELD - 1] + '…'


def render_template(template: str, values: Mapping[str, Any], max_bytes: int = MAX_FINDING) -> str:
    text = FIELD_RE.sub(lambda m: _field_text(lookup(dict(values), m.group(1))), str(template or ''))
    return clip_bytes(' '.join(text.split()), max_bytes)


def template_fields(template: str) -> list[str]:
    return FIELD_RE.findall(str(template or ''))


def _words(text: str) -> list[str]:
    return re.findall(r'[a-z0-9]+', str(text or '').lower())


def trigger_match(question: str, triggers: Sequence[str]) -> bool:
    """Keyword triggers (no regex): empty or '*' = always; otherwise some trigger's word sequence
    occurs contiguously in the question's words."""
    if not triggers or '*' in triggers:
        return True
    words = _words(question)
    joined = ' ' + ' '.join(words) + ' '
    return any(' ' + ' '.join(_words(t)) + ' ' in joined for t in triggers if _words(t))


def join(left: list[dict], right: Sequence[C.Binding]) -> list[dict]:
    out = []
    for a in left:
        for b in right:
            bv = b.values
            if all(binding_key(a[k]) == binding_key(bv[k]) for k in a.keys() & bv.keys()):
                out.append({**a, **bv})
                if len(out) >= MAX_JOIN:
                    return out
    return out


class RuleEngine:
    """contracts.Engine. `predicates` / `offers` add registries (tests); pack ones come from ctx.pack."""

    def __init__(self, *, predicates: Mapping[str, C.Predicate] | None = None,
                 offers: Mapping[str, C.Offer] | None = None) -> None:
        self.extra_predicates = dict(predicates or {})
        self.extra_offers = dict(offers or {})

    def _registry(self, ctx: C.RuleContext, kind: str) -> dict[str, Any]:
        core = core_predicates.REGISTRY if kind == 'pred' else core_offers.REGISTRY
        extra = self.extra_predicates if kind == 'pred' else self.extra_offers
        try:
            pack = dict((ctx.pack.predicates() if kind == 'pred' else ctx.pack.offers()) or {})
        except Exception:
            pack = {}
        return {**pack, **extra, **core}              # core names cannot be shadowed

    def evaluate(self, ctx: C.RuleContext, rules: Sequence[C.Rule]) -> list[C.Finding]:
        return self.evaluate_trace(ctx, rules)[0]

    def evaluate_trace(self, ctx: C.RuleContext, rules: Sequence[C.Rule]) -> tuple[list[C.Finding], list[dict]]:
        preds, offs = self._registry(ctx, 'pred'), self._registry(ctx, 'offer')
        findings: list[C.Finding] = []
        trace: list[dict] = []
        for rule in sorted(rules, key=_order):
            started = time.perf_counter()
            status, detail, finding = self._one(ctx, rule, preds, offs)
            entry = {'rule': rule.id, 'status': status, 'ms': round((time.perf_counter() - started) * 1000, 2)}
            if detail:
                entry['detail'] = detail[:200]
            trace.append(entry)
            if finding:
                findings.append(finding)
        return findings, trace

    def _one(self, ctx: C.RuleContext, rule: C.Rule, preds: dict, offs: dict) -> tuple[str, str, C.Finding | None]:
        if not rule.enabled:
            return 'disabled', '', None
        if not trigger_match(ctx.question, rule.triggers):
            return 'no_trigger', '', None
        if not rule.when:
            return 'n/a', 'rule has no predicates', None
        calls = []
        for clause in rule.when:
            name = clause.get('pred') if isinstance(clause, dict) else None
            if name not in preds:
                return 'n/a', f'unknown predicate {name}', None
            calls.append((name, preds[name], dict(clause.get('args') or {})))
        offer_fn, offer_args = None, {}
        if rule.offer:
            op = rule.offer.get('op')
            if op not in offs:
                return 'n/a', f'unknown offer {op}', None
            offer_fn, offer_args = offs[op], dict(rule.offer.get('args') or {})
        bindings: list[dict] = [{}]
        for name, fn, args in calls:
            try:
                got = fn(ctx, **args)
            except Exception as exc:
                return 'error', f'{name}: {type(exc).__name__}: {exc}', None
            if not isinstance(got, list) or not all(isinstance(b, C.Binding) for b in got):
                return 'error', f'{name}: predicates must return list[Binding]', None
            bindings = join(bindings, got)
            if not bindings:
                return 'silent', '', None
        chosen, offer = bindings[0], None
        if offer_fn:
            for b in bindings[:OFFER_TRIES]:
                try:
                    res = offer_fn(ctx, C.Binding(dict(b)), **offer_args)
                except Exception as exc:
                    return 'error', f"offer {rule.offer.get('op')}: {type(exc).__name__}: {exc}", None
                if isinstance(res, C.OfferResult) and str(res.answer).strip() \
                        and not same_answer(ctx.pack, res.answer, ctx.candidate.answer or ''):
                    chosen, offer = b, res                     # an "alternative" equal to the answer is none
                    break
            if offer is None and any(f.startswith('offer.') for f in template_fields(rule.finding)):
                return 'offer_failed', '', None
        values = {**chosen, 'candidate': {'answer': ctx.candidate.answer, 'ref': ctx.candidate.ref},
                  'rule': {'id': rule.id}}
        if offer:
            values['offer'] = {'answer': offer.answer, 'derivation': offer.derivation}
        text = render_template(rule.finding, values)
        return ('fired' if offer or not offer_fn else 'fired_no_offer'), '', \
            C.Finding(rule=rule.id, text=text, offer=offer, bindings=_json_safe(chosen))


def _order(rule: C.Rule) -> tuple[int, str]:
    """Priority order; a malformed priority (unvalidated learned rule) sorts last instead of making
    the sort raise and taking the whole gate down."""
    p = rule.priority
    return (p if isinstance(p, int) and not isinstance(p, bool) else 1_000, str(rule.id))


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def evaluate(ctx: C.RuleContext, rules: Sequence[C.Rule]) -> list[C.Finding]:
    return RuleEngine().evaluate(ctx, rules)
