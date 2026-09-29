"""v9 mechanism learner (arm `mech-evolve`; DESIGN §2 L4, §3; effect review P4; validity review P4).

Same skeleton, model, reasoning effort, budget, evidence and domain text as the text learner
(learner_common.run_round); the mechanism adapter additionally carries declarative rules
(rules/<id>.json, contracts.Rule) and a policy (policy.json, contracts.POLICY_DEFAULTS keys). Extra tools:

- propose_rule(rule, targets, rationale): a new rule from the core + pack vocabulary (no Python).
- modify_rule(id, patch, targets, rationale): patch of when / finding / offer / priority / enabled / triggers.
- set_policy(key, value, rationale): one policy knob within contracts.POLICY_RANGES.
- propose_note: as the text learner (paired actor re-run verification after the session).

Rule admission, at zero model cost, during the session (the reply reports the numbers):
1. schema and vocabulary (harness_core.rules.validate_rule over the core + pack predicate/offer names);
2. pack.leak_screen + the 8-word question-copy check on every model-visible string (finding template,
   triggers, predicate and offer arguments), and the shared 16 KB text budget with the seed text counted;
3. counterfactual (harness_core.replay.counterfactual) on the learner's target episodes: on at least one
   wrong released episode the rule fires on the final answer, its offer grades right against the released
   reference (controller-side grader, production: VENDOR reward.py), and it is silent when the candidate
   is set to the reference;
4. precision over all released episodes with a ledger: the Wilson upper bound of the right-episode fire
   rate <= MAX_RIGHT_FIRE with at least MIN_RIGHT right episodes (declared: before that no rule can be
   admitted; with 0 right fires the bound first drops under 0.20 at 16 right episodes);
5. runtime: every episode evaluated in <= RULE_RUNTIME_MS.
A disable (enabled false) is admitted when the rule's point right-episode fire rate exceeds MAX_RIGHT_FIRE
on >= MIN_RIGHT right episodes; a priority-only change after the schema check; any other modification like
a new rule. Policy: validate_policy + replay of the released harness logs (loop guard over the ledgers,
gate decisions over api/checks, enforcement and splice rows of api/controller.jsonl), reported as changed
counts. At most MAX_EDITS rule/policy edits are admitted per block. Admitted edits are written with the
round's new adapter revision (rules/, policy.json) and mechanical-admission.json records the numbers.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import replace
import inspect
import json
from pathlib import Path
import threading
import time
from typing import Any, Mapping, Sequence

from harness_core import contracts as C
from harness_core import offers as core_offers
from harness_core import predicates as core_predicates
from harness_core import replay as RP
from harness_core.engine import RuleEngine
from harness_core.gate import decide
from harness_core.ledger import load_events
from harness_core.rules import effective_policy, rule_to_dict, validate_policy, validate_rule

import evidence as EV
import learner_common as LC

MAX_EDITS = 2
MAX_RIGHT_FIRE = 0.20
MIN_RIGHT = 10
RULE_RUNTIME_MS = 2000.0
RULE_FIELDS = ('id', 'when', 'finding', 'offer', 'priority', 'triggers')
PATCH_FIELDS = ('when', 'finding', 'offer', 'priority', 'enabled', 'triggers')
POLL_TOOLS = ('write_stdin',)
MAX_TARGETS = 10


class NullRunner:
    """A compute runner that never runs anything (fire counts without offer execution)."""

    def run(self, code: str, *, timeout_s: float = 5.0) -> tuple[bool, str]:
        return False, ''


def vocabulary(pack) -> tuple[dict, dict]:
    """Predicates / offers a rule may name: pack ones, then core (a pack cannot shadow a core name)."""
    preds = {**dict(pack.predicates() or {}), **core_predicates.REGISTRY}
    offs = {**dict(pack.offers() or {}), **core_offers.REGISTRY}
    return preds, offs


def _describe(name: str, fn) -> str:
    try:
        params = [f'{p.name}={p.default!r}' for p in inspect.signature(fn).parameters.values()
                  if p.kind == p.KEYWORD_ONLY and p.default is not p.empty]
    except (TypeError, ValueError):
        params = []
    doc = ' '.join((inspect.getdoc(fn) or '').split('\n\n')[0].split())
    return f'- {name}({", ".join(params)}): {EV.one_line(doc, 400) or "(no description)"}'


def vocabulary_text(pack) -> str:
    """Names with their arguments and first doc paragraph, then the reference docs that list the binding
    keys templates and offers can use (core predicates, core offers, the pack's rule modules)."""
    preds, offs = vocabulary(pack)
    lines = ['### Predicates (`when`)'] + [_describe(n, f) for n, f in sorted(preds.items())]
    lines += ['### Offers (`offer.op`)'] + [_describe(n, f) for n, f in sorted(offs.items())]
    modules = [core_predicates, core_offers]
    for fn in list(dict(pack.predicates() or {}).values()) + list(dict(pack.offers() or {}).values()):
        module = inspect.getmodule(fn)
        if module is not None and module not in modules:
            modules.append(module)
    for module in modules:
        doc = inspect.getdoc(module) or ''
        if doc:
            lines += [f'### Reference: {module.__name__}', doc[:3000]]
    return '\n'.join(lines)


def _runner(ctx: LC.RoundContext):
    if ctx.runner is None:
        from harness_core.runner import make_runner
        ctx.runner = make_runner()
    return ctx.runner


def replay_episodes(ctx: LC.RoundContext) -> dict[int, RP.Episode]:
    """Released episodes with a ledger, as harness_core.replay episodes (label = strict grade)."""
    if 'episodes' not in ctx.cache:
        out = {}
        for o in ctx.evidence.ordinals():
            e = ctx.evidence.episodes[o]
            if e.has_ledger:
                out[o] = RP.load_episode(Path(e.run_dir) / 'api', episode_id=f'q{o:03}', question=e.question,
                                         final_answer=e.submitted, label=e.strict)
        ctx.cache['episodes'] = out
    return ctx.cache['episodes']


class _TimedRunner:
    """Pass-through compute runner that measures the time spent inside the sandbox, so the runtime limit
    applies to the trusted engine (predicates, joins, templates; contract: <= 2 s) and not to sandbox process
    start-up, which has its own wall limit and varies with machine load (review fix: admission must not
    depend on how busy the controller host is)."""

    def __init__(self, inner):
        self.inner, self.spent = inner, 0.0

    def run(self, code: str, *, timeout_s: float = 5.0) -> tuple[bool, str]:
        started = time.perf_counter()
        try:
            return self.inner.run(code, timeout_s=timeout_s)
        finally:
            self.spent += time.perf_counter() - started


def rule_stats(rule: C.Rule, episodes: Sequence[RP.Episode], pack, runner) -> dict:
    """RP.fire_stats for one rule, with per-episode evaluation time (ms): `max_ms` = engine time without the
    sandboxed offer re-execution (checked against RULE_RUNTIME_MS), `max_runner_ms` = time in the sandbox."""
    engine = RuleEngine()
    timed = _TimedRunner(runner)
    s = {'n_right': 0, 'n_wrong': 0, 'n_unlabelled': 0, 'fired_right': 0, 'fired_wrong': 0, 'fired_unlabelled': 0,
         'offers': 0, 'n/a': 0, 'errors': 0, 'fired_episodes': [], 'max_ms': 0.0, 'total_ms': 0.0,
         'max_runner_ms': 0.0}
    for ep in episodes:
        started, sandbox = time.perf_counter(), timed.spent
        out = RP.rule_outcome(rule, RP.final_context(ep, pack, timed), engine)
        in_runner = (timed.spent - sandbox) * 1000
        ms = (time.perf_counter() - started) * 1000 - in_runner
        s['max_ms'], s['total_ms'] = max(s['max_ms'], ms), s['total_ms'] + ms
        s['max_runner_ms'] = max(s['max_runner_ms'], in_runner)
        side = 'right' if ep.label == 1 else 'wrong' if ep.label == 0 else 'unlabelled'
        s[f'n_{side}'] += 1
        s['n/a'] += out['status'] == 'n/a'
        s['errors'] += out['status'] == 'error'
        if out['fired']:
            s[f'fired_{side}'] += 1
            s['offers'] += int(out['offer'] is not None)
            s['fired_episodes'].append(ep.episode_id)
    fired = s['fired_right'] + s['fired_wrong']
    s['precision'] = s['fired_wrong'] / fired if fired else None
    s['precision_wilson'] = RP.wilson(s['fired_wrong'], fired)
    s['right_fire_rate'] = s['fired_right'] / s['n_right'] if s['n_right'] else None
    s['right_fire_wilson'] = RP.wilson(s['fired_right'], s['n_right'])
    s['wrong_fire_rate'] = s['fired_wrong'] / s['n_wrong'] if s['n_wrong'] else None
    s['max_ms'], s['total_ms'], s['max_runner_ms'] = (round(s['max_ms'], 2), round(s['total_ms'], 2),
                                                     round(s['max_runner_ms'], 2))
    return s


def _public_stats(s: Mapping) -> dict:
    return {k: s[k] for k in ('n_right', 'n_wrong', 'fired_right', 'fired_wrong', 'offers', 'n/a', 'errors', 'precision',
                              'right_fire_rate', 'right_fire_wilson', 'max_ms', 'max_runner_ms')}


def _scope(rule: C.Rule) -> str:
    core = all(c.get('pred') in core_predicates.REGISTRY for c in rule.when if isinstance(c, dict))
    return 'core' if core and (rule.offer is None or rule.offer.get('op') in core_offers.REGISTRY) else 'pack'


def admit_rule(rule: C.Rule, targets: Sequence[int], ctx: LC.RoundContext) -> dict:
    """DESIGN §3 admission for one new or modified rule. Returns booleans and counts only."""
    verdict: dict[str, Any] = {'admitted': False, 'reasons': []}
    reasons = verdict['reasons']
    preds, offs = vocabulary(ctx.pack)
    errors = validate_rule(rule, predicates=preds, offers=offs)
    if errors:
        reasons += errors
        return verdict
    visible = '\n'.join([rule.finding, ' '.join(rule.triggers), rule.id.replace('_', ' '),
                         json.dumps([c.get('args', {}) for c in rule.when] + [rule.offer], ensure_ascii=False)])
    reasons += ['leak - ' + f for f in LC.leak_findings(ctx.pack, visible, ctx.evidence)]
    budget = ctx.budget_with(rules=[r for r in ctx.pending_rules() if r.id != rule.id] + [rule])
    verdict['text_budget'] = budget['total']
    if not budget['ok']:
        reasons.append(f"text budget: {budget['total']} > {budget['limit']} bytes")
    if reasons:
        return verdict
    episodes = replay_episodes(ctx)
    runner = _runner(ctx)
    results = []
    for o in list(dict.fromkeys(targets))[:MAX_TARGETS]:
        e = ctx.evidence.episodes.get(o)
        if e is None:
            results.append({'ordinal': o, 'admit': False, 'reasons': ['not a released episode']})
            continue
        if o not in episodes:
            results.append({'ordinal': o, 'admit': False, 'reasons': ['episode has no ledger']})
            continue
        cf = RP.counterfactual(rule, episodes[o], e.reference, ctx.grader.grade_fn, pack=ctx.pack, runner=runner)
        results.append({'ordinal': o, **{k: cf[k] for k in ('wrong', 'fires', 'offer', 'offer_right',
                                                               'silent_on_reference', 'admit', 'reasons')}})
    verdict['counterfactual'] = results
    if not any(r['admit'] for r in results):
        reasons.append('counterfactual: on no target does the rule fire, offer the right answer and stay silent on '
                       'the reference')
    stats = rule_stats(rule, list(episodes.values()), ctx.pack, runner)
    ok, why = RP.precision_check(stats, max_right_fire=MAX_RIGHT_FIRE, min_right=MIN_RIGHT)
    verdict['precision'] = {**_public_stats(stats), 'ok': ok}
    reasons += ['precision: ' + w for w in why]
    if stats['max_ms'] > RULE_RUNTIME_MS:
        reasons.append(f"runtime: {stats['max_ms']:.0f} ms > {RULE_RUNTIME_MS:.0f} ms on one episode")
    verdict['admitted'] = not reasons
    return verdict


def _admission_stats(verdict: Mapping, block: int) -> dict:
    cf = verdict.get('counterfactual') or []
    prec = verdict.get('precision') or {}
    return {'admitted_block': block, 'counterfactual_targets': len(cf),
            'counterfactual_admits': [r['ordinal'] for r in cf if r.get('admit')],
            **{k: prec.get(k) for k in ('n_right', 'n_wrong', 'fired_right', 'fired_wrong', 'offers')}}


def _refusal(ctx: LC.RoundContext) -> str | None:
    if len(ctx.edits) >= MAX_EDITS:
        return f'at most {MAX_EDITS} rule/policy edits are admitted per block; not recorded'
    if ctx.attempts > LC.PROPOSAL_ATTEMPTS:
        return 'proposal attempts for this session are exhausted'
    return None


def _log(ctx: LC.RoundContext, kind: str, ident: str | None, verdict: Mapping, args: Mapping, **extra) -> None:
    ctx.screen_log.append({'kind': kind, 'attempt': ctx.attempts, 'id': ident, 'ok': bool(verdict.get('admitted')),
                           'reasons': list(verdict.get('reasons') or []), 'ledgered': True,
                           'rationale': args.get('rationale', ''), 'utc': LC.utc()})
    ctx.row(kind=kind, id=ident, admitted=bool(verdict.get('admitted')), reason='; '.join(verdict.get('reasons') or [])
            or 'admitted', admission={k: v for k, v in verdict.items() if k not in ('admitted', 'reasons')},
            rationale=args.get('rationale', ''), **extra)


def propose_rule(args: dict, ctx: LC.RoundContext) -> tuple[str, bool]:
    ctx.attempts += 1
    refusal = _refusal(ctx)
    if refusal:
        return LC._dumps({'admitted': False, 'reasons': [refusal]}), False
    raw = args['rule']
    reasons = [f'field {k!r} is set by the controller or unknown (allowed: {", ".join(RULE_FIELDS)})'
               for k in raw if k not in RULE_FIELDS]
    rule_id = raw.get('id')
    if rule_id in {r.id for r in ctx.pending_rules()}:
        reasons.append('a rule with this id exists; use modify_rule')
    elif rule_id in ctx.used_ids():
        reasons.append('id already used in this campaign')
    verdict: dict[str, Any] = {'admitted': False, 'reasons': reasons}
    rule = None
    if not reasons:
        rule = C.Rule(id=rule_id, when=raw.get('when'), finding=raw.get('finding', ''), offer=raw.get('offer'),
                      priority=raw.get('priority', 50), enabled=True, triggers=raw.get('triggers', []),
                      origin='learned', admitted_block=ctx.block, stats={})
        if isinstance(rule.when, list):
            rule = replace(rule, scope=_scope(rule))
        verdict = admit_rule(rule, args['targets'], ctx)
    if verdict['admitted']:
        rule = replace(rule, stats=_admission_stats(verdict, ctx.block))
        ctx.edits.append({'kind': 'rule', 'id': rule.id, 'rule': rule, 'action': 'add', 'admission': verdict})
    _log(ctx, 'rule', rule_id if isinstance(rule_id, str) else None, verdict, args, action='add',
         rule=rule_to_dict(rule) if rule else raw)
    return LC._dumps({'id': rule_id, **verdict}), bool(verdict['admitted'])


def modify_rule(args: dict, ctx: LC.RoundContext) -> tuple[str, bool]:
    ctx.attempts += 1
    refusal = _refusal(ctx)
    if refusal:
        return LC._dumps({'admitted': False, 'reasons': [refusal]}), False
    rules = {r.id: r for r in ctx.pending_rules()}
    old, patch = rules.get(args['id']), args['patch']
    reasons = [] if old else [f"no rule {args['id']!r}"]
    reasons += [f'patch field {k!r} is not editable (allowed: {", ".join(PATCH_FIELDS)})' for k in patch if k not in PATCH_FIELDS]
    if not patch:
        reasons.append('empty patch')
    verdict: dict[str, Any] = {'admitted': False, 'reasons': reasons}
    new = None
    mode = 'full'
    if not reasons:
        new = replace(old, **patch)
        if isinstance(new.when, list):
            new = replace(new, scope=_scope(new))
        preds, offs = vocabulary(ctx.pack)
        if set(patch) == {'priority'}:
            mode = 'priority'
            verdict['reasons'] = validate_rule(new, predicates=preds, offers=offs)
            verdict['admitted'] = not verdict['reasons']
        elif patch.get('enabled') is False and set(patch) != {'enabled'}:
            verdict['reasons'].append('disable a rule on its own (patch {"enabled": false}), not together with other changes')
        elif set(patch) == {'enabled'} and patch['enabled'] is False:
            mode = 'disable'
            # the real runner: a finding whose template cites {offer.*} fires only when its offer runs, so
            # counting with NullRunner would understate the right-episode fire rate (review fix)
            stats = rule_stats(old, list(replay_episodes(ctx).values()), ctx.pack, _runner(ctx))
            verdict['precision'] = _public_stats(stats)
            rate = stats['right_fire_rate']
            if stats['n_right'] < MIN_RIGHT:
                verdict['reasons'].append(f"only {stats['n_right']} right episodes (< {MIN_RIGHT})")
            elif rate is None or rate <= MAX_RIGHT_FIRE:
                verdict['reasons'].append(f'right-episode fire rate {rate or 0:.2f} is within {MAX_RIGHT_FIRE:.2f}; '
                                          'a rule is disabled only when it exceeds the limit')
            verdict['admitted'] = not verdict['reasons']
        else:
            verdict = admit_rule(new, args.get('targets') or [], ctx)
    verdict['mode'] = mode
    if verdict['admitted']:
        new = replace(new, stats={**(old.stats or {}), 'modified_block': ctx.block,
                                  **({'modification': _admission_stats(verdict, ctx.block)} if mode == 'full' else {})})
        ctx.edits.append({'kind': 'rule', 'id': new.id, 'rule': new, 'action': 'modify', 'admission': verdict})
    _log(ctx, 'rule', args['id'], verdict, args, action='modify', patch=patch)
    return LC._dumps({'id': args['id'], **verdict}), bool(verdict['admitted'])


def loop_guard_replay(events: Sequence[C.Event], policy: Mapping[str, Any]) -> dict:
    """controller_runtime.MechController._loop_guard over a recorded ledger: loop notes (one per distinct
    normalised command reaching loop_threshold; write_stdin polls skipped) and escalations (a note after
    loop_notes_max notes when a candidate exists after the last new observation)."""
    threshold, notes_max = int(policy['loop_threshold']), int(policy['loop_notes_max'])
    counts: Counter = Counter()
    noted: set[str] = set()
    notes = escalations = 0
    last_obs, last_cand = -1, -1
    for i, e in enumerate(events):
        if isinstance(e, C.Observe):
            last_obs = i
        elif isinstance(e, C.Candidate):
            last_cand = i
        elif isinstance(e, C.Command):
            if e.tool in POLL_TOOLS or e.cmd.startswith(POLL_TOOLS):
                continue
            counts[e.cmd] += 1
            if counts[e.cmd] >= threshold and e.cmd not in noted:
                noted.add(e.cmd)
                notes += 1
                if notes > notes_max and last_cand > last_obs:
                    escalations += 1
    return {'loop_notes': notes, 'escalations': escalations}


def _controller_rows(api: Path) -> list[dict]:
    path = api / 'controller.jsonl'
    rows = []
    if path.is_file():
        for line in path.read_text(encoding='utf-8').splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def policy_replay(ctx: LC.RoundContext, old: Mapping[str, Any], new: Mapping[str, Any]) -> dict:
    """Changed counts of the harness decisions when `new` replaces `old` on the released harness logs."""
    loop = {'episodes': 0, 'notes_old': 0, 'notes_new': 0, 'escalations_old': 0, 'escalations_new': 0,
            'episodes_changed': 0, 'changed_right': 0, 'changed_wrong': 0, 'recorded_loop_notes': 0}
    gate = {'checks': 0, 'status_changed': 0, 'shown_old': 0, 'shown_new': 0}
    enforce = {'recorded': 0, 'skipped_under_new': 0}
    splice = {'recorded': 0, 'shortened_under_new': 0}
    for o in ctx.evidence.ordinals():
        e = ctx.evidence.episodes[o]
        if not e.has_ledger:
            continue
        api = Path(e.run_dir) / 'api'
        events = load_events(api / 'ledger.jsonl')
        a, b = loop_guard_replay(events, old), loop_guard_replay(events, new)
        loop['episodes'] += 1
        loop['notes_old'] += a['loop_notes']
        loop['notes_new'] += b['loop_notes']
        loop['escalations_old'] += a['escalations']
        loop['escalations_new'] += b['escalations']
        if a != b:
            loop['episodes_changed'] += 1
            loop['changed_right' if e.strict else 'changed_wrong'] += 1
        rows = _controller_rows(api)
        loop['recorded_loop_notes'] += sum(r.get('event') == 'loop_note' for r in rows)
        for r in rows:
            if r.get('event') == 'enforce':
                enforce['recorded'] += 1
                enforce['skipped_under_new'] += int(int(r.get('n') or 1) > int(new['enforce_requery_max']))
            elif r.get('event') == 'ledger_splice':
                splice['recorded'] += 1
                splice['shortened_under_new'] += int(int(r.get('bytes') or 0) > int(new['ledger_splice_bytes']))
        for path in sorted((api / 'checks').glob('[0-9]*.json')) if (api / 'checks').is_dir() else []:
            check = EV._load(path)
            if not isinstance(check, dict) or 'state_before' not in check:
                continue
            fired = [C.Finding(rule=f.get('rule', ''), text=f.get('text', '')) for f in check.get('fired') or []]
            before = check['state_before']
            keep = (check.get('candidate') or {}).get('keep', '')
            status_old, pick_old, _, _ = decide(fired, before['shown'], before['kept'], keep, effective_policy(old),
                                                before.get('offered', False))
            status_new, pick_new, _, _ = decide(fired, before['shown'], before['kept'], keep, effective_policy(new),
                                                before.get('offered', False))
            gate['checks'] += 1
            gate['shown_old'] += len(pick_old)
            gate['shown_new'] += len(pick_new)
            gate['status_changed'] += int(status_old != status_new)
    return {'loop_guard': loop, 'gate': gate, 'enforce': enforce, 'ledger_splice': splice,
            'not_replayable': ['enforce_min_remaining_s'] if old.get('enforce_min_remaining_s') != new.get('enforce_min_remaining_s') else []}


def set_policy(args: dict, ctx: LC.RoundContext) -> tuple[str, bool]:
    ctx.attempts += 1
    refusal = _refusal(ctx)
    if refusal:
        return LC._dumps({'admitted': False, 'reasons': [refusal]}), False
    key, value = args['key'], args['value']
    current = effective_policy(ctx.pending_policy())
    proposed = {**ctx.pending_policy(), key: value}
    reasons = validate_policy(proposed)
    if not reasons and current.get(key) == value:
        reasons.append(f'{key} is already {value}')
    verdict: dict[str, Any] = {'admitted': False, 'reasons': reasons}
    if not reasons:
        verdict['replay'] = policy_replay(ctx, current, effective_policy(proposed))
        verdict['admitted'] = True
        ctx.edits.append({'kind': 'policy', 'key': key, 'value': value, 'admission': verdict})
    _log(ctx, 'policy', key, verdict, args, action='set', key=key, value=value, old=current.get(key))
    return LC._dumps({'key': key, 'value': value, **verdict}), bool(verdict['admitted'])


def _schema(required, properties) -> dict:
    return {'type': 'object', 'additionalProperties': False, 'required': list(required), 'properties': properties}


TUNABLE = sorted(k for k, (lo, hi) in C.POLICY_RANGES.items() if lo < hi)
TARGETS = {'type': 'array', 'items': {'type': 'integer', 'minimum': 1}, 'maxItems': MAX_TARGETS,
           'description': 'wrong released episodes (ordinals) the rule should fix; the counterfactual runs on these'}
RATIONALE = {'type': 'string', 'description': 'For the ledger: the failure this edit addresses and the evidence for it.'}


def extra_tools(ctx: LC.RoundContext) -> list[LC.Tool]:
    return [
        LC.Tool('propose_rule', 'Propose one new declarative rule (no code): rule = {id, when: [{pred, args}], finding, '
                'offer: {op, args} | null, priority 0-100, triggers}. Admitted at once, at zero model cost, if valid, '
                'leak-free and within the text budget, the counterfactual passes on at least one target and the '
                'precision over all released episodes holds. The reply gives the numbers.',
                _schema(['rule', 'targets', 'rationale'], {'rule': {'type': 'object'},
                                                          'targets': {**TARGETS, 'minItems': 1}, 'rationale': RATIONALE}),
                propose_rule),
        LC.Tool('modify_rule', 'Change one existing rule: patch = any of when, finding, offer, priority, enabled, triggers. '
                'Disabling is admitted when the rule fires on more than 20% of >= 10 right released episodes; a '
                'priority-only change after the schema check; any other change is admitted like a new rule (targets needed).',
                _schema(['id', 'patch', 'rationale'], {'id': {'type': 'string'}, 'patch': {'type': 'object'},
                                                      'targets': TARGETS, 'rationale': RATIONALE}), modify_rule),
        LC.Tool('set_policy', 'Set one harness policy knob within its range; the reply reports how a replay of the '
                'released harness logs changes (loop notes, gate rounds, enforcement re-queries, ledger splices).',
                _schema(['key', 'value', 'rationale'], {'key': {'type': 'string', 'enum': TUNABLE},
                                                       'value': {'type': 'integer'}, 'rationale': RATIONALE}), set_policy),
    ]


PROMPT_SECTION = f'''## Your task: harness rules, policy and scoped notes (mechanism adapter)
The actor works inside a harness. The harness records a ledger of what the actor saw (observed values with
their locators, calc runs with the inputs they used, candidate answers). When the actor runs `submit` (or ends
with an unsubmitted answer) the harness evaluates declarative rules over that ledger and shows at most one finding
per round; a finding with an offer shows a concrete alternative answer the harness computed itself (e.g. the
actor's own calc re-run with one input substituted), so the actor only has to choose. Rules contain no code:
rule = {{"id": "snake_case", "when": [{{"pred": name, "args": {{...}}}}] (all must match; bindings join on shared keys),
"finding": "<= 200-byte template using {{dotted.fields}} of the bindings", "offer": {{"op": name, "args": {{...}}}} or null,
"priority": 0-100 (lower is shown first), "triggers": [optional question keywords]}}.
Tools: propose_rule, modify_rule, set_policy (admitted at once, zero model cost; at most {MAX_EDITS} rule/policy edits
per block) and propose_note (as for the text adapter: verified by actor re-runs after the session).
Rule admission: (1) schema and vocabulary; (2) leak screen and the shared text budget; (3) counterfactual: on at least
one of your target episodes (wrong released episodes) the rule fires on the final answer, its offer grades right against
the released reference, and it stays silent when the answer is the reference; (4) precision over all released
episodes: the Wilson upper bound of its fire rate on right episodes <= {MAX_RIGHT_FIRE} with at least {MIN_RIGHT}
right episodes (until then no rule can be admitted); (5) at most {RULE_RUNTIME_MS / 1000:.0f} s per episode.
Notes: propose_note(id, triggers, text, support, rationale) - shown on questions containing a trigger, at most
{C.NOTES_PER_EPISODE} per question; support = pool questions it would have fixed; re-run {LC.K} times each plus
{LC.KEEP} control questions.
End with a short report (causes found, edits made, open leads) and no tool call.
'''
NUDGE = ('You ended without an admitted edit or an accepted note. If a rule, a policy change or a note would have '
         'changed the outcome of released wrong episodes, propose it now; otherwise reply "no edit" and explain why.')


def status_section(ctx: LC.RoundContext) -> str:
    rules = ctx.pending_rules()
    episodes = list(replay_episodes(ctx).values())
    stats = RP.fire_stats(rules, episodes, ctx.pack, NullRunner()) if rules and episodes else {}
    policy = effective_policy(ctx.policy)
    lines = ['## Harness rules now (fire counts on the final answers of the released episodes with a ledger; '
             'counted without executing offers, so a rule whose finding cites an offer value can fire more often)']
    for r in sorted(rules, key=lambda r: (r.priority, r.id)):
        s = stats.get(r.id)
        fires = (f"fires on {s['fired_right']}/{s['n_right']} right and {s['fired_wrong']}/{s['n_wrong']} wrong"
                 if s else 'no ledger episodes yet')
        lines.append(f"- {r.id} ({'enabled' if r.enabled else 'DISABLED'}, priority {r.priority}, {r.origin}"
                     f"{', admitted block ' + str(r.admitted_block) if r.origin == 'learned' else ''}); {fires}")
        lines.append('  ' + json.dumps({'when': r.when, 'offer': r.offer, 'triggers': r.triggers, 'finding': r.finding},
                                       ensure_ascii=False))
    lines.append('## Harness policy (value, allowed range)')
    for key in sorted(policy):
        rng = C.POLICY_RANGES.get(key)
        lines.append(f'- {key} = {policy[key]}' + (f' (range {rng[0]}-{rng[1]})' if rng else ''))
    lines.append('## Rule vocabulary')
    lines.append(vocabulary_text(ctx.pack))
    return '\n'.join(lines) + '\n'


SPEC = LC.LearnerSpec(name='mechanism', architecture='mechanism', profile='mech-evolve', extra_tools=extra_tools,
                      prompt_section=lambda ctx: PROMPT_SECTION, status_section=status_section, nudge=NUDGE,
                      admission_mode='counterfactual_precision_replay + paired_actor_rerun (notes)')


def run_learning(key, source, evidence, output, block, stop: threading.Event | None = None, *, actor_runner, **options):
    return LC.run_round(SPEC, key, source, evidence, output, block, stop, actor_runner=actor_runner, **options)
