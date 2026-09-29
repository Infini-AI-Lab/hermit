"""L4 commit gate (contracts.GateAPI): `submit` records a Candidate, evaluates the rules over the
ledger and answers with at most one finding per round (effect review P3):

    [harness:gate] <finding, ≤ 200 B>
    Offer: <alternative> (<derivation>)            only when the rule's offer produced one
    Accept: submit --answer "<alternative>"        only with an offer
    Keep: submit --answer "<answer>" --keep "<reason>"

Per candidate value (numeric key, so '12.30' and '12.3' are one value) at most
policy['gate_rounds_max'] distinct findings are shown; a finding is never repeated for the same
value; `--keep "<reason>"` suppresses the findings already shown for that value; a value equal to
an alternative the gate offered is accepted as is. Otherwise the
submission is recorded ('[harness:gate] recorded "<answer>". …'). The wording never asks
"are you sure". Every `submit` message is also added to the ledger as a Note (reason 'gate'): the
submit CLI prints it as tool output, so the model sees it, and the ledger strips it from that output
(the harness's own words are never evidence). `check_final` records no Note (contracts v9.1,
S3-review R1): its message reaches the model only if the controller re-queries, and the controller
records the feedback it actually sends. An unusable payload returns status 'rejected' (no candidate,
no check file, not a gate round).

Every check (input and output) is saved to <api>/checks/NNN.json: question, spec, payload,
candidate, ledger position (lines + sha256 of the ledger prefix), rules, policy, gate state before,
engine trace, all fired findings, the shown result. `replay.recheck` re-derives the result from it.
"""
from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import threading
from typing import Any, Mapping, Sequence

from . import contracts as C
from .engine import RuleEngine, clip_bytes
from .numeric import numbers_in, short
from .predicates import ACCEPT_PREFIX, GATE_PREFIX, same_answer
from .rules import effective_policy, rule_to_dict

LIMITS = {'answer': 1000, 'alt': 1000, 'reading': 1000, 'keep': 500, 'artifact_sha256': 64}
# `--alt` is still accepted but not advertised (PREREG addendum P2)
USAGE = 'submit --answer "<answer>" [--reading "<assumption>"] [--keep "<reason>"]'


def validate_payload(payload: Any, answer_kind: str = 'text') -> str | None:
    """Why a submit payload is unusable, or None."""
    if not isinstance(payload, Mapping):
        return 'payload must be a JSON object'
    extra = set(payload) - C.SUBMIT_FIELDS
    if extra:
        return f'unknown fields {sorted(extra)}'
    for key, value in payload.items():
        if not isinstance(value, str):
            return f'{key} must be a string'
        if len(value) > LIMITS[key]:
            return f'{key} is longer than {LIMITS[key]} characters'
    if answer_kind == 'artifact':
        if not re.fullmatch(r'[0-9a-f]{64}', payload.get('artifact_sha256', '')):
            return 'artifact answers need --artifact PATH'
    elif not payload.get('answer', '').strip():
        return 'an --answer is required'
    return None


def shell_quote(text: str) -> str:
    """Text safe inside double quotes for zsh/bash."""
    text = ' '.join(str(text or '').split())
    for a, b in (('\\', '\\\\'), ('"', '\\"'), ('$', '\\$'), ('`', '\\`')):
        text = text.replace(a, b)
    return text


def value_key(pack: C.Pack, cand: C.Candidate) -> str:
    if cand.artifact_sha256 and not cand.answer:
        return 'sha:' + cand.artifact_sha256
    nums = pack.numbers(cand.answer or '')
    if nums:
        return 'num:' + ','.join(short(round(v, 9)) for v in nums)
    return 'txt:' + pack.canonical(cand.answer or '')


def decide(findings: Sequence[C.Finding], shown: Sequence[str], kept: Sequence[str], keep: str,
           policy: Mapping[str, Any], offered: bool = False) -> tuple[str, list[C.Finding], list[str], list[str]]:
    """Pure gate state machine: (status, findings to show, shown after, kept after). A value the
    gate itself offered is accepted as is (the harness never contradicts its own offer)."""
    shown, kept = list(shown), list(kept)
    if offered:
        return 'accepted', [], shown, kept
    if keep.strip():
        kept += [r for r in shown if r not in kept]
    fresh = [f for f in findings if f.rule not in shown and f.rule not in kept]
    if fresh and len(shown) < int(policy['gate_rounds_max']):
        pick = fresh[:int(policy['gate_findings_per_round'])]
        return 'findings', pick, shown + [f.rule for f in pick], kept
    return ('kept' if keep.strip() and kept else 'accepted'), [], shown, kept


def finding_message(finding: C.Finding, answer: str) -> str:
    lines = [GATE_PREFIX + finding.text]
    if finding.offer:
        offer = ' '.join(f'{finding.offer.answer} ({finding.offer.derivation})'.split())   # one line, always
        lines.append(clip_bytes(f'Offer: {offer}', 400))
        lines.append(f'Accept: submit --answer "{shell_quote(finding.offer.answer)}"')
    lines.append(f'Keep: submit --answer "{shell_quote(answer)}" --keep "<reason>"')
    return '\n'.join(lines)


def accepted_message(answer: str, kept: bool) -> str:
    return (f'{ACCEPT_PREFIX} "{shell_quote(answer)}"{" (kept)" if kept else ""}. '
            'End your turn with exactly this answer as your final message.')


class Gate:
    """contracts.GateAPI for one episode. `rules` = the adapter's rule set (seed + learned);
    rules disabled in the rule or by policy['disabled_rules'] are not evaluated."""

    def __init__(self, pack: C.Pack, rules: Sequence[C.Rule], policy: Mapping[str, Any] | None,
                 runner: C.ComputeRunner, question: str, *, api_dir: str | os.PathLike | None = None,
                 engine: RuleEngine | None = None) -> None:
        self.pack, self.runner, self.question = pack, runner, question
        self.policy = effective_policy(policy)
        disabled = set(self.policy['disabled_rules'])
        self.rules = [r for r in rules if r.enabled and r.id not in disabled]
        self.spec = pack.question_spec(question)
        self.engine = engine or RuleEngine()
        self.checks_dir = Path(api_dir) / 'checks' if api_dir is not None else None
        self._lock = threading.Lock()
        self._shown: dict[str, list[str]] = {}
        self._kept: dict[str, list[str]] = {}
        self._offered: set[str] = set()          # value keys of alternatives the gate has shown
        self.checks = 0
        self.summary: list[dict] = []            # per check: kind, status, rule shown (no answer values)

    # -- GateAPI ---------------------------------------------------------------------------------

    def submit(self, payload: Mapping[str, Any], ledger: C.LedgerAPI, t: float, call: int) -> C.GateResult:
        problem = validate_payload(payload, self.pack.answer_kind)
        if problem:
            return C.GateResult(status='rejected', findings=[],
                                message=f'{GATE_PREFIX}rejected: {problem}. Usage: {USAGE}')
        cand = C.Candidate(kind='candidate', ref='', call=call, t=t, answer=payload.get('answer', '').strip(),
                           alt=payload.get('alt', '').strip(), reading=payload.get('reading', '').strip(),
                           keep=payload.get('keep', '').strip(),
                           artifact_sha256=payload.get('artifact_sha256', ''), via='submit')
        return self._check('submit', cand, dict(payload), ledger, t, call)

    def check_final(self, final_text: str, ledger: C.LedgerAPI, t: float, call: int) -> C.GateResult:
        """`final_text` is the answer span of the final message (the controller extracts it with
        pack.extract_final). Records Candidate(via='final') and evaluates the rules; records no Note."""
        cand = C.Candidate(kind='candidate', ref='', call=call, t=t, answer=(final_text or '').strip()[:LIMITS['answer']],
                           via='final')
        return self._check('final', cand, {}, ledger, t, call)

    # -- helpers for the controller ----------------------------------------------------------------

    def needs_enforcement(self, final_text: str, ledger: C.LedgerAPI) -> bool:
        """True when nothing was submitted, or the final message differs (after canonical/numeric
        normalisation) from the last submission (effect review P6, engineering #3)."""
        subs = [c for c in ledger.candidates() if c.via == 'submit']
        return not subs or not same_answer(self.pack, final_text or '', subs[-1].answer)

    # -- internals ---------------------------------------------------------------------------------

    def _check(self, kind: str, cand: C.Candidate, payload: dict, ledger: C.LedgerAPI,
               t: float, call: int) -> C.GateResult:
        with self._lock:
            cand = ledger.add(cand)
            if hasattr(ledger, 'snapshot'):
                events, lines, sha = ledger.snapshot()
            else:
                events, lines, sha = list(ledger.events), None, ''
            key = value_key(self.pack, cand)
            before = {'shown': list(self._shown.get(key, [])), 'kept': list(self._kept.get(key, [])),
                      'offered': key in self._offered}
            ctx = C.RuleContext(question=self.question, spec=self.spec, events=events, candidate=cand,
                                pack=self.pack, runner=self.runner)
            fired, trace = self.engine.evaluate_trace(ctx, self.rules)
            status, pick, self._shown[key], self._kept[key] = decide(fired, before['shown'], before['kept'],
                                                                     cand.keep, self.policy, before['offered'])
            if pick and pick[0].offer:
                self._offered.add(value_key(self.pack, C.Candidate(kind='candidate', ref='', call=call, t=t,
                                                                   answer=pick[0].offer.answer)))
            message = finding_message(pick[0], cand.answer) if pick else \
                accepted_message(cand.answer or cand.artifact_sha256, status == 'kept')
            self._record_offer_run(ctx, pick, ledger, t, call)
            if kind == 'submit':        # printed by the submit CLI: shown. A final check's message is shown only
                ledger.add(C.Note(kind='note', ref='', call=call, t=t, reason='gate',   # if the controller re-queries
                                  text=message, sticky=False))                        # (it records that Note)
            result = C.GateResult(status=status, findings=pick, message=message, candidate_ref=cand.ref)
            self.checks += 1
            self.summary.append({'n': self.checks, 'kind': kind, 'call': call, 'status': status,
                                 'shown': [f.rule for f in pick], 'fired': [f.rule for f in fired]})
            self._save(kind, call, t, payload, cand, lines, sha, before, trace, fired, result)
            return result

    def _record_offer_run(self, ctx: C.RuleContext, pick: list[C.Finding], ledger: C.LedgerAPI,
                          t: float, call: int) -> None:
        """The shown offer's re-execution becomes a Compute (source 'gate:<rule>'), so accepting it
        submits a calc output and the ledger shows where the number came from."""
        offer = pick[0].offer if pick else None
        run = (ctx.__dict__.get('_offer_runs') or {}).get(offer.compute_sha256) if offer else None
        if not run:
            return
        code, out = run
        ledger.add(C.Compute(kind='compute', ref='', call=call, t=t, source=f'gate:{pick[0].rule}', code=code,
                             code_sha256=offer.compute_sha256, output=out[:2048], values=numbers_in(out[:2048]),
                             inputs=[]))

    def _save(self, kind: str, call: int, t: float, payload: dict, cand: C.Candidate, lines: int | None,
              sha: str, before: dict, trace: list, fired: list, result: C.GateResult) -> None:
        if self.checks_dir is None:
            return
        record = {'n': self.checks, 'kind': kind, 'call': call, 't': t, 'contract': C.CONTRACT_VERSION,
                  'pack': self.pack.name, 'question': self.question, 'spec': asdict(self.spec),
                  'payload': payload, 'candidate': asdict(cand),
                  'ledger': {'lines': lines, 'sha256': sha},
                  'rules': [rule_to_dict(r) for r in self.rules], 'policy': self.policy,
                  'state_before': before, 'trace': trace, 'fired': [asdict(f) for f in fired],
                  'result': asdict(result)}
        self.checks_dir.mkdir(parents=True, exist_ok=True)
        path = self.checks_dir / f'{self.checks:03d}.json'
        tmp = path.with_suffix('.json.tmp')
        try:
            data = (json.dumps(record, ensure_ascii=False, indent=1, sort_keys=True) + '\n').encode('utf-8')
        except UnicodeEncodeError:                  # lone surrogates in model text: ASCII-escaped JSON
            data = (json.dumps(record, ensure_ascii=True, indent=1, sort_keys=True) + '\n').encode('ascii')
        tmp.write_bytes(data)
        os.replace(tmp, path)
