"""Offline replay over stored episodes: rule statistics, counterfactual admission, check re-derivation.

Inputs per episode (all written by the broker/controller, none by the actor): `api/ledger.jsonl`
(events), `api/checks/NNN.json` (gate inputs/outputs), the question, the final answer, and a 0/1
label from a callback. Zero model cost; the only execution is `recompute` offers in the isolated
runner. Nothing here imports a grader: `grade_fn(answer, reference) -> bool | float` is injected
by the caller, and no function returns or stores answer or reference values (only rule ids,
episode ids, booleans and counts), so results can go straight into Rule.stats and reports.

- `rule_outcome`: one rule on one episode's final (or any) candidate: fired? offer produced?
- `fire_stats`: per rule over labelled episodes: fires on right / wrong, offers, n/a, errors,
  precision = P(wrong | fired) and right-episode fire rate with Wilson bounds.
- `counterfactual`: DESIGN §3 admission step 1: on a wrong episode the rule fires and its offer
  grades right against the released reference; with the candidate set to the reference it is silent.
- `precision_check`: DESIGN §3 admission step 2 (rates with minimum counts, Wilson upper bound).
- `recheck`: re-derive a stored gate check from its recorded inputs and compare (online = offline).
- `ledger_from_api_records`: rebuild a ledger from recorded broker calls (api/NNN/request.json +
  response.json), e.g. for v8 traffic.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
import glob
import json
import math
import os
from pathlib import Path
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import contracts as C
from .engine import RuleEngine
from .gate import decide, finding_message, accepted_message, value_key
from .ledger import Ledger, load_events, ledger_prefix_sha
from .rules import rule_from_dict


@dataclass
class Episode:
    episode_id: str
    question: str
    events: list[C.Event]
    final_answer: str = ''
    label: int | None = None               # 1 right, 0 wrong, None unknown
    checks: list[dict] = field(default_factory=list)
    attrs: dict[str, Any] = field(default_factory=dict)


def load_checks(api_dir: str | os.PathLike) -> list[dict]:
    out = []
    for path in sorted(glob.glob(os.path.join(str(api_dir), 'checks', '[0-9]*.json'))):
        with open(path, encoding='utf-8') as fh:
            out.append(json.load(fh))
    return out


def load_episode(api_dir: str | os.PathLike, *, episode_id: str, question: str, final_answer: str = '',
                 label: int | None = None) -> Episode:
    """Episode from <run>/api (ledger.jsonl + checks/)."""
    path = Path(api_dir) / 'ledger.jsonl'
    events = load_events(path) if path.exists() else []
    return Episode(episode_id=episode_id, question=question, events=events, final_answer=final_answer,
                   label=label, checks=load_checks(api_dir))


def final_context(episode: Episode, pack: C.Pack, runner: C.ComputeRunner, *,
                  answer: str | None = None) -> C.RuleContext:
    """Context for the episode's final answer (or `answer`): the last recorded candidate equal to it
    with the events up to it, else a synthetic Candidate(via='final') after all events."""
    answer = episode.final_answer if answer is None else answer
    from .predicates import same_answer
    events = list(episode.events)
    cands = [e for e in events if isinstance(e, C.Candidate)]
    for cand in reversed(cands):
        if answer and same_answer(pack, cand.answer, answer):
            return C.RuleContext(question=episode.question, spec=pack.question_spec(episode.question),
                                 events=events, candidate=cand, pack=pack, runner=runner)
    n = sum(1 for e in events if isinstance(e, C.Candidate)) + 1
    last_call = max((e.call for e in events), default=0)
    last_t = max((e.t for e in events), default=0.0)
    cand = C.Candidate(kind='candidate', ref=f'a{n}r', call=last_call, t=last_t, answer=answer or '', via='final')
    return C.RuleContext(question=episode.question, spec=pack.question_spec(episode.question),
                         events=events + [cand], candidate=cand, pack=pack, runner=runner)


def with_answer(ctx: C.RuleContext, answer: str) -> C.RuleContext:
    """Same evidence, same candidate position, different answer (counterfactual)."""
    cand = replace(ctx.candidate, answer=answer, alt='', keep='')
    events = [cand if (isinstance(e, C.Candidate) and e.ref == cand.ref) else e for e in ctx.events]
    return C.RuleContext(question=ctx.question, spec=ctx.spec, events=events, candidate=cand,
                         pack=ctx.pack, runner=ctx.runner)


def rule_outcome(rule: C.Rule, ctx: C.RuleContext, engine: RuleEngine | None = None) -> dict:
    """{'status', 'fired', 'offer'} for one rule (enabled or not: replay tests proposals)."""
    engine = engine or RuleEngine()
    findings, trace = engine.evaluate_trace(ctx, [replace(rule, enabled=True)])
    finding = findings[0] if findings else None
    return {'status': trace[0]['status'], 'fired': finding is not None, 'finding': finding,
            'offer': finding.offer if finding else None}


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return 0.0, 1.0
    p = k / n
    den = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, mid - half), min(1.0, mid + half)


def fire_stats(rules: Sequence[C.Rule], episodes: Iterable[Episode], pack: C.Pack, runner: C.ComputeRunner, *,
               label_fn: Callable[[Episode], int | None] | None = None, z: float = 1.96) -> dict[str, dict]:
    """Per rule, evaluated on each episode's final answer. Labels: label_fn(ep) or ep.label (1 = right)."""
    engine = RuleEngine()
    stats = {r.id: {'n_right': 0, 'n_wrong': 0, 'n_unlabelled': 0, 'fired_right': 0, 'fired_wrong': 0,
                    'fired_unlabelled': 0, 'offers': 0, 'n/a': 0, 'errors': 0, 'fired_episodes': []}
             for r in rules}
    for ep in episodes:
        label = label_fn(ep) if label_fn else ep.label
        ctx = final_context(ep, pack, runner)
        for rule in rules:
            s = stats[rule.id]
            side = 'right' if label == 1 else 'wrong' if label == 0 else 'unlabelled'
            s[f'n_{side}'] += 1
            out = rule_outcome(rule, ctx, engine)
            if out['status'] == 'n/a':
                s['n/a'] += 1
            elif out['status'] == 'error':
                s['errors'] += 1
            if out['fired']:
                s[f'fired_{side}'] += 1
                s['offers'] += int(out['offer'] is not None)
                s['fired_episodes'].append(ep.episode_id)
    for s in stats.values():
        fired = s['fired_right'] + s['fired_wrong']
        s['precision'] = s['fired_wrong'] / fired if fired else None
        s['precision_wilson'] = wilson(s['fired_wrong'], fired, z)
        s['right_fire_rate'] = s['fired_right'] / s['n_right'] if s['n_right'] else None
        s['right_fire_wilson'] = wilson(s['fired_right'], s['n_right'], z)
        s['wrong_fire_rate'] = s['fired_wrong'] / s['n_wrong'] if s['n_wrong'] else None
    return stats


def precision_check(stat: Mapping[str, Any], *, max_right_fire: float = 0.20, min_right: int = 10,
                    min_fired_wrong: int = 1, use_upper_bound: bool = True) -> tuple[bool, list[str]]:
    """Admission step 2: enough right episodes to bound the false-fire rate, and the rate (its Wilson
    upper bound by default) within the gate limit; the rule must fire on some wrong episode."""
    reasons = []
    if stat['n_right'] < min_right:
        reasons.append(f"only {stat['n_right']} right episodes (< {min_right})")
    rate = stat['right_fire_wilson'][1] if use_upper_bound else (stat['right_fire_rate'] or 0.0)
    if stat['n_right'] and rate > max_right_fire:
        reasons.append(f'right-episode fire rate bound {rate:.2f} > {max_right_fire:.2f}')
    if stat['fired_wrong'] < min_fired_wrong:
        reasons.append(f"fires on {stat['fired_wrong']} wrong episodes (< {min_fired_wrong})")
    return not reasons, reasons


def _is_right(grade: Any, threshold: float) -> bool:
    if isinstance(grade, bool):
        return grade
    try:
        return float(grade) >= threshold
    except (TypeError, ValueError):
        return False


def counterfactual(rule: C.Rule, episode: Episode, reference_answer: str,
                   grade_fn: Callable[[str, str], Any], *, pack: C.Pack, runner: C.ComputeRunner,
                   label: int | None = None, right_threshold: float = 1.0) -> dict:
    """Admission step 1 on one released episode. Returns booleans only (no answer values):
    {'rule', 'episode', 'wrong', 'fires', 'offer', 'offer_right', 'silent_on_reference', 'admit', 'reasons'}."""
    label = episode.label if label is None else label
    ctx = final_context(episode, pack, runner)
    first = rule_outcome(rule, ctx)
    offer = first['offer']
    offer_right = bool(offer) and _is_right(grade_fn(offer.answer, reference_answer), right_threshold)
    second = rule_outcome(rule, with_answer(ctx, reference_answer))
    result = {'rule': rule.id, 'episode': episode.episode_id, 'wrong': label == 0, 'fires': first['fired'],
              'status': first['status'], 'offer': offer is not None, 'offer_right': offer_right,
              'silent_on_reference': not second['fired'], 'reasons': []}
    if label != 0:
        result['reasons'].append('episode is not labelled wrong')
    if not first['fired']:
        result['reasons'].append('rule does not fire on the episode')
    elif offer is None:
        result['reasons'].append('rule fires but produces no offer')
    elif not offer_right:
        result['reasons'].append('offer does not grade right')
    if second['fired']:
        result['reasons'].append('rule still fires with the reference answer')
    result['admit'] = not result['reasons']
    return result

# ---------------------------------------------------------------------------------------------
# Re-deriving stored gate checks
# ---------------------------------------------------------------------------------------------


def recheck(check: Mapping[str, Any], ledger_path: str | os.PathLike, pack: C.Pack, runner: C.ComputeRunner) -> dict:
    """Re-evaluate a stored check with its recorded inputs; report whether the fired rules, the
    shown findings (text + offer answer) and the message are identical."""
    lines = check['ledger']['lines']
    events = load_events(ledger_path, upto_lines=lines)
    sha_ok = lines is None or ledger_prefix_sha(ledger_path, lines) == check['ledger']['sha256']
    cand = C.Candidate(**check['candidate'])
    spec = C.QuestionSpec(**{k: tuple(v) if isinstance(v, list) else v for k, v in check['spec'].items()})
    rules = [rule_from_dict(r) for r in check['rules']]
    ctx = C.RuleContext(question=check['question'], spec=spec, events=events, candidate=cand, pack=pack, runner=runner)
    fired, trace = RuleEngine().evaluate_trace(ctx, rules)
    before = check['state_before']
    status, pick, _, _ = decide(fired, before['shown'], before['kept'], cand.keep, check['policy'],
                                before.get('offered', False))
    message = finding_message(pick[0], cand.answer) if pick else \
        accepted_message(cand.answer or cand.artifact_sha256, status == 'kept')
    stored = check['result']

    def sig(findings: Iterable) -> list:
        return [(f['rule'], f['text'], (f.get('offer') or {}).get('answer')) if isinstance(f, dict)
                else (f.rule, f.text, f.offer.answer if f.offer else None) for f in findings]
    same = (sig(check['fired']) == sig(fired) and stored['status'] == status and stored['message'] == message
            and sig(stored['findings']) == sig(pick))
    return {'n': check['n'], 'ledger_sha_ok': sha_ok, 'identical': same and sha_ok,
            'fired': [f.rule for f in fired], 'status': status}

# ---------------------------------------------------------------------------------------------
# Rebuilding a ledger from recorded broker calls
# ---------------------------------------------------------------------------------------------


def ledger_from_api_records(api_dir: str | os.PathLike, pack: C.Pack, out_path: str | os.PathLike | None = None,
                            *, limit: int | None = None) -> tuple[Ledger, dict]:
    """Feed api/NNN/{request,response}.json through a Ledger in call order (read-only on the
    records). Returns the ledger and {'calls', 'seconds', 'tool_less_requests', 'errors'}."""
    ledger = Ledger(pack, out_path)
    calls = sorted(p for p in glob.glob(os.path.join(str(api_dir), '[0-9][0-9][0-9]*')) if os.path.isdir(p))
    if limit:
        calls = calls[:limit]
    started = time.perf_counter()
    t0 = None
    tool_less = 0
    for path in calls:
        n = int(os.path.basename(path))
        t = float(n)
        try:
            with open(os.path.join(path, 'transport.json'), encoding='utf-8') as fh:
                attempts = json.load(fh).get('attempts') or []
            stamp = attempts[0].get('dispatched_utc') if attempts else None
            if isinstance(stamp, str):
                stamp = datetime.fromisoformat(stamp).timestamp()
            if isinstance(stamp, (int, float)):
                t0 = stamp if t0 is None else t0
                t = round(float(stamp - t0), 3)
        except (OSError, ValueError, TypeError):
            pass
        try:
            with open(os.path.join(path, 'request.json'), encoding='utf-8') as fh:
                request = json.load(fh)
        except (OSError, ValueError) as exc:
            ledger.errors.append(f'call {n}: request unreadable ({type(exc).__name__})')
            continue
        tools = request.get('tools')
        if isinstance(tools, list) and not tools:
            tool_less += 1
        ledger.ingest_request(n, request.get('input') or [], t, tools=tools if isinstance(tools, list) else None)
        try:
            with open(os.path.join(path, 'response.json'), encoding='utf-8') as fh:
                response = json.load(fh)
        except (OSError, ValueError):
            continue
        ledger.ingest_response(n, response, t)
    info = {'calls': len(calls), 'seconds': round(time.perf_counter() - started, 3),
            'tool_less_requests': tool_less, 'errors': list(ledger.errors)}
    return ledger, info
