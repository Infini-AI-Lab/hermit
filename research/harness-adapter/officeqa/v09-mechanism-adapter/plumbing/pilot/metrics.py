"""Go/no-go metrics of the v9 pilot (PREREG §7) over sealed pilot roots. $0: no model call, no network.

    python pilot/metrics.py ROOT [ROOT ...] [--json OUT]      exit 0 = GO, 1 = NO-GO, 2 = not computable
    python pilot/metrics.py ../v9-p1-a ../v9-p2-a --json ../v9-p2-metrics.json     re-pilot b2 (addendum P2)

Runs only on roots whose campaign completed and whose predictions are sealed
(private-evaluation/predictions-seal.json). It then grades, controller-side, every candidate the gate
judged with the official scorer (the pinned reward.py of each root, strict tolerance 0; tests inject a
toy grader), and the sealed final answers. It never prints or writes an answer value: the table and the
JSON hold counts, rates, rule ids, ordinals and task ids only.

Definitions (mech-static episodes; one episode = one scored question of one root):
- shown finding: a gate finding the model saw. A `submit` check with status 'findings' (printed by the
  submit CLI), or a `final` check (enforcement) with findings whose re-query was sent (its text is in a
  ledger Note reason 'enforce'). Source: api/checks/NNN.json + api/ledger.jsonl.
- fired episode: at least one shown finding. Pre-finding candidate: the candidate of the first shown
  finding. Judged candidate: the pre-finding candidate of a fired episode; otherwise the last candidate
  the gate checked; otherwise the final answer. right/wrong = strict grade of the judged candidate.
- 1. ledger coverage = answered episodes whose answer (the answer span of the final message, as the gate
  extracts it with packs.officeqa.pack.extract_final, else the last submitted answer) links to the ledger: a number of it equals a calc output after rounding or a value a tool printed
  (harness_core.predicates from_computes/observed, the gate's own relations); an answer without numbers
  links when its text appears in an observed span. Denominator: answered episodes (all-episode rate shown).
- 2. finding rate on right / wrong episodes = fired episodes / episodes, by the judged candidate's grade
  (the rate by the final answer's grade is shown too).
- 3. flips after a finding (fired episodes): wrong->right = pre-finding candidate wrong, final answer
  right; right->wrong the reverse. Both zero = NO DATA (not a GO).
- 4. gate limits: union fire rate on right episodes <= 20%; right->wrong <= 5% of fired episodes whose
  pre-finding candidate was right.
- 5. submit rate (episodes with >= 1 submit) and enforcement re-query rate: reported; submit rate < 50%
  is flagged INVESTIGATE.
Loops (every root): a loop run issued one identical normalised shell command >= 3 times
(records/events.jsonl, the rule of pilot/select_loop_prone.py); recovered = it still completed with a
non-empty answer. mech roots add loop notes (ledger Notes reason 'loop_guard') and whether a noted
command was issued again after its note.

Confirmatory temperature rule (PREREG addendum P1, pre-registered before the pilot): cell a = the text-static
root at temperature 0 without top_p, cell c = the text-static root at temperature 1.0 with top_p 0.95 (both
identified by their frozen settings, on the same question list). The rule's two quantities:
- loop runs per cell (the loop-run definition above); condition: cell c has at least 50% fewer loop runs
  than cell a, i.e. loops_c <= 0.5 x loops_a (when cell a has 0 loop runs there is nothing to reduce: not met);
- strict accuracy difference = strict-correct questions of c minus those of a (official reward.py grades of
  the sealed final answers, tolerance 0); condition: the difference is >= -2 questions.
Both met -> every confirmatory arm runs at temperature 1.0, top_p 0.95; otherwise at temperature 0 (no
top_p). The verdict is printed and written to the JSON; it does not change the go/no-go exit code.

Re-pilot decision rule (PREREG addendum P2, pre-registered before the re-pilot): given the pilot-1 text-static
reference root (T=0, no top_p; `v9-p1-a`, 31 of 50 strict correct, pinned) and one mech-static root that ran the
P2 mech W0 (its frozen W0 revision is P2_W0_REVISION; `v9-p2-a`) at T=0 without top_p on the same questions,
compare b2's strict-correct count with 31: b2 >= 31 PROCEED (plan the confirmatory study with mech = text +
rules); b2 <= 27 STOP (rules add nothing on OfficeQA: stop the OfficeQA mechanism line, move to the second
benchmark); 28-30 INCONCLUSIVE (re-run text-static and mech-static concurrently, paired, before deciding). The
go/no-go table above is computed for b2 as well (the only mech-static root given). Single run, descriptive;
a and b2 ran at different times. The verdict does not change the exit code.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
from typing import Callable

HERE = Path(__file__).resolve().parent
PLUMBING = HERE.parent
if str(PLUMBING) not in sys.path:
    sys.path.insert(0, str(PLUMBING))

from harness_core import contracts as C  # noqa: E402
from harness_core.ledger import load_events  # noqa: E402
from harness_core.numeric import number_tokens  # noqa: E402
from harness_core.predicates import facts, from_computes, observed  # noqa: E402
from packs.officeqa.pack import extract_final  # noqa: E402

try:                                             # run as a script or imported as pilot.metrics
    from pilot.select_loop_prone import THRESHOLD, run_commands  # noqa: E402
except ImportError:                              # pragma: no cover
    from select_loop_prone import THRESHOLD, run_commands  # noqa: E402

# PREREG §7 (and §5 gate limits), copied verbatim as numbers
COVERAGE_MIN = 0.60
FIRE_RATIO_MIN = 2.0          # wrong-episode finding rate >= 2 x right-episode finding rate
FLIP_RATIO_MIN = 3.0          # wrong->right >= 3 x right->wrong
RIGHT_FIRE_MAX = 0.20         # union finding rate on right episodes
RIGHT_TO_WRONG_MAX = 0.05     # right->wrong flips <= 5% of fired right episodes
SUBMIT_INVESTIGATE_BELOW = 0.50
# The confirmatory temperature rule (PREREG addendum P1), as numbers
RULE_BASE_CELL = {'temperature': 0, 'top_p': None}            # cell a
RULE_CELL = {'temperature': 1.0, 'top_p': 0.95}              # cell c (model card, agentic evaluation settings)
RULE_ARM = 'text-static'
RULE_LOOP_REDUCTION_MIN = 0.50    # cell c: at least 50% fewer loop runs than cell a
RULE_ACCURACY_MARGIN = 2          # cell c: strict-correct count not lower than cell a's by more than 2 questions
# The re-pilot decision rule (PREREG addendum P2), as numbers
P2_ARM = 'mech-static'
P2_W0_REVISION = 'd4e9c3a1a574fca83ab8c25a71babe5fd0393f68346f2292d845bdc5a9102398'   # campaign.revision(adapters/mech-w0)
P2_REFERENCE = {'arm': 'text-static', 'temperature': 0, 'top_p': None}                # pilot-1 cell a (v9-p1-a)
P2_REFERENCE_RIGHT = 31          # cell a's strict-correct count in pilot 1 (of 50)
P2_PROCEED_MIN = 31              # b2 >= 31: PROCEED
P2_STOP_MAX = 27                 # b2 <= 27: STOP; 28-30: INCONCLUSIVE
P2_MEANING = {'PROCEED': 'plan the confirmatory study with mech = text + rules',
              'STOP': 'rules add nothing on OfficeQA: stop the OfficeQA mechanism line and move to the second benchmark',
              'INCONCLUSIVE': 're-run text-static and mech-static concurrently (paired, same time) before deciding'}
POLL_TOOLS = ('write_stdin',)
Grader = Callable[..., float]    # score(reference, predicted, tolerance=0.0) -> 1.0 | 0.0 (reward.py shape)


def _read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _rate(k, n):
    return None if not n else k / n

# ---------------------------------------------------------------------------------------------
# Inputs (after the seal only)
# ---------------------------------------------------------------------------------------------


def sealed_root(root: Path) -> dict:
    """freeze + state of a completed, sealed root; refuses anything earlier."""
    root = Path(root)
    seal = root / 'private-evaluation/predictions-seal.json'
    if not seal.is_file():
        raise ValueError(f'{root.name}: predictions are not sealed; metrics run only after the seal')
    state, frozen = _read(root / 'state.json'), _read(root / 'freeze.json')
    if state.get('status') != 'completed':
        raise ValueError(f'{root.name}: campaign status {state.get("status")!r}, not completed')
    return {'root': root, 'state': state, 'freeze': frozen, 'seal': _read(seal)}


def references(root: Path, task_ids) -> dict[str, str]:
    """Controller-side references of exactly these ids (after the seal; answer key hash-checked)."""
    prepared = Path(root) / 'prepared'
    manifest = _read(prepared / 'manifest.json')
    answers = prepared / 'private/answers.jsonl'
    if _sha(answers) != manifest['answers_sha256']:
        raise ValueError('answer key changed after preparation')
    wanted, keys = set(task_ids), {}
    for line in answers.read_text(encoding='utf-8').splitlines():
        if line.strip():
            row = json.loads(line)
            if row['task_id'] in wanted:
                keys[row['task_id']] = row['answer']
    if set(keys) != wanted:
        raise ValueError('missing references for sealed questions')
    return keys


def official_grader(root: Path) -> Grader:
    import data
    return data.official_scorer(root)

# ---------------------------------------------------------------------------------------------
# One episode
# ---------------------------------------------------------------------------------------------


def _checks(api: Path) -> list[dict]:
    return [_read(p) for p in sorted((api / 'checks').glob('[0-9][0-9][0-9].json'))]


def shown_findings(checks: list[dict], events: list) -> list[dict]:
    """Findings the model saw, in order: {n, kind, rule, candidate (answer text; internal only)}."""
    enforce = [e.text for e in events if isinstance(e, C.Note) and e.reason == 'enforce']
    out = []
    for check in checks:
        result = check.get('result') or {}
        findings = result.get('findings') or []
        if result.get('status') != 'findings' or not findings:
            continue
        if check.get('kind') == 'final':
            message = str(result.get('message') or '').strip()
            if not message or not any(message in text for text in enforce):
                continue                           # the re-query carrying it was never sent
        out.append({'n': check.get('n'), 'kind': check.get('kind'), 'rule': findings[0].get('rule'),
                    'candidate': (check.get('candidate') or {}).get('answer') or ''})
    return out


def links_to_ledger(answer: str, events: list) -> bool:
    """A number of `answer` is a calc output (after rounding) or a printed value; a number-free answer
    appears in an observed span."""
    answer = (answer or '').strip()
    if not answer:
        return False
    candidate = C.Candidate(kind='candidate', ref='', call=0, t=0.0, answer=answer)
    ctx = C.RuleContext(question='', spec=C.QuestionSpec(), events=list(events), candidate=candidate,
                        pack=None, runner=None)
    f = facts(ctx)
    tokens = number_tokens(answer)
    if tokens:
        return any(from_computes(f, tok) or observed(f, tok) for tok in tokens)
    needle = answer.casefold()
    return any(needle in (o.text or '').casefold() for o in f.observes)


def loop_stats(events: list) -> dict:
    """Loop notes (ledger), and whether a command that triggered a note was issued again after it."""
    counts, noted, repeated, notes = Counter(), set(), False, 0
    for event in events:
        if isinstance(event, C.Command):
            if event.tool in POLL_TOOLS or event.cmd.startswith(POLL_TOOLS):
                continue
            if event.cmd in noted:
                repeated = True
            counts[event.cmd] += 1
        elif isinstance(event, C.Note) and event.reason == 'loop_guard':
            notes += 1
            noted |= {cmd for cmd, n in counts.items() if n >= THRESHOLD}
    return {'loop_notes': notes, 'repeated_after_note': bool(notes) and repeated}


class Episode:
    def __init__(self, base: Path, result: dict, reference: str, grade: Callable[[str], float], controller: bool):
        self.ordinal, self.task_id = result['ordinal'], result['task_id']
        summary = _read(base / 'summary.json')
        self.status = summary.get('status')
        self.final = summary.get('answer') or ''
        self.final_right = grade(self.final) >= 1.0 if self.final.strip() else False
        commands = run_commands(base / 'records/events.jsonl')
        self.max_repeat = max(commands.values(), default=0)
        self.loop_run = self.max_repeat >= THRESHOLD
        self.recovered = self.loop_run and self.status == 'completed' and bool(self.final.strip())
        self.controller = controller
        if not controller:
            return
        api = base / 'api'
        events = load_events(api / 'ledger.jsonl') if (api / 'ledger.jsonl').is_file() else []
        checks = _checks(api)
        counters = summary.get('controller') or {}
        self.submitted = (counters.get('submits') or 0) >= 1
        self.requeried = (counters.get('enforce_requeries') or 0) >= 1
        submits = [e.answer for e in events if isinstance(e, C.Candidate) and e.via == 'submit' and e.answer]
        # the answer span (the gate's own extraction): prose numbers of a final message (years, table ids)
        # must not count as a linked answer (packs.officeqa.pack.extract_final, S4-review 1)
        linked_answer = extract_final(self.final) if self.final.strip() else (submits[-1] if submits else '')
        self.answered = bool(linked_answer.strip())
        self.linked = self.answered and links_to_ledger(linked_answer, events)
        shown = shown_findings(checks, events)
        self.fired = bool(shown)
        self.first_rule = shown[0]['rule'] if shown else None
        self.shown_rules = sorted({s['rule'] for s in shown})
        if shown:
            judged = shown[0]['candidate']
        elif checks:
            judged = (checks[-1].get('candidate') or {}).get('answer') or ''
        else:
            judged = self.final
        self.judged_right = bool(judged.strip()) and grade(judged) >= 1.0
        self.pre_right = self.judged_right if shown else None
        self.flip = None
        if shown:
            self.flip = ('wrong_to_right' if not self.pre_right and self.final_right else
                         'right_to_wrong' if self.pre_right and not self.final_right else 'unchanged')
        # engine-level fires per checked candidate (for the lowest-precision rule), deduplicated per episode
        self.engine_fired = {'right': set(), 'wrong': set()}
        for check in checks:
            answer = (check.get('candidate') or {}).get('answer') or ''
            side = 'right' if answer.strip() and grade(answer) >= 1.0 else 'wrong'
            self.engine_fired[side] |= {f.get('rule') for f in check.get('fired') or [] if f.get('rule')}
        self.loops = loop_stats(events)

    def row(self) -> dict:
        """Per-episode record without any answer text."""
        value = {'ordinal': self.ordinal, 'task_id': self.task_id, 'status': self.status,
                 'final_right': self.final_right, 'loop_run': self.loop_run, 'max_repeat': self.max_repeat,
                 'recovered': self.recovered}
        if self.controller:
            value.update(answered=self.answered, linked=self.linked, submitted=self.submitted,
                         requeried=self.requeried, fired=self.fired, first_rule=self.first_rule,
                         shown_rules=self.shown_rules, judged_right=self.judged_right, flip=self.flip,
                         loop_notes=self.loops['loop_notes'],
                         repeated_after_note=self.loops['repeated_after_note'])
        return value

# ---------------------------------------------------------------------------------------------
# Roots and the go/no-go table
# ---------------------------------------------------------------------------------------------


def root_metrics(root: Path, grader: Grader | None = None) -> dict:
    info = sealed_root(root)
    root, state, frozen = info['root'], info['state'], info['freeze']
    arm = frozen['arm']
    rows = sorted(state['results'], key=lambda r: r['ordinal'])
    if [r['task_id'] for r in rows] != frozen['order']:
        raise ValueError(f'{root.name}: results do not cover the frozen order')
    keys = references(root, [r['task_id'] for r in rows])
    score = grader if grader is not None else official_grader(root)
    episodes = []
    for result in rows:
        reference = keys[result['task_id']]
        cache: dict[str, float] = {}

        def grade(answer, reference=reference, cache=cache):
            if answer not in cache:
                cache[answer] = float(score(reference, answer, tolerance=0.0))
            return cache[answer]
        episodes.append(Episode(root / 'runs/episodes' / f'{result["ordinal"]:03}', result, reference, grade,
                                bool(arm['controller'])))
    grades_file = root / 'private-evaluation' / f'{arm["name"]}-grades.json'
    if not grades_file.is_file():
        raise ValueError(f'{root.name}: no {grades_file.name}; metrics run only after the root was graded')
    official = {r['task_id']: r['strict_score'] >= 1.0 for r in _read(grades_file)['results']}
    agrees = set(official) == {e.task_id for e in episodes} and all(
        official[e.task_id] == e.final_right for e in episodes)
    if not agrees:
        # the controller-side grading must reproduce the official grades of the sealed answers exactly;
        # otherwise every flip / right-wrong number below is computed with the wrong grader or answer key
        raise ValueError(f'{root.name}: controller-side grades of the sealed answers differ from {grades_file.name}')
    n = len(episodes)
    loops = [e for e in episodes if e.loop_run]
    value = {'root': root.name, 'arm': arm['name'], 'temperature': frozen['settings'].get('temperature'),
             'top_p': frozen['settings'].get('top_p'), 'w0_revision': (frozen.get('w0') or {}).get('revision'),
             'subset': (frozen['campaign'].get('subset') or {}).get('sha256'), 'episodes': n,
             'right': sum(e.final_right for e in episodes), 'accuracy': _rate(sum(e.final_right for e in episodes), n),
             'empty': sum(not e.final.strip() for e in episodes),
             'statuses': dict(Counter(e.status for e in episodes)),
             'loop_runs': len(loops), 'loop_runs_recovered': sum(e.recovered for e in loops),
             'loop_runs_right': sum(e.final_right for e in loops), 'grade_file_agrees': agrees,
             'rows': [e.row() for e in episodes]}
    if arm['controller']:
        value['gate'] = gate_numbers(episodes)
    return value


def gate_numbers(episodes: list[Episode]) -> dict:
    n = len(episodes)
    answered = [e for e in episodes if e.answered]
    right = [e for e in episodes if e.judged_right]
    wrong = [e for e in episodes if not e.judged_right]
    final_right = [e for e in episodes if e.final_right]
    final_wrong = [e for e in episodes if not e.final_right]
    fired = [e for e in episodes if e.fired]
    fired_right = [e for e in fired if e.pre_right]
    flips = Counter(e.flip for e in fired)
    rules: dict[str, Counter] = {}
    for e in episodes:
        if e.first_rule:
            rules.setdefault(e.first_rule, Counter())['first_shown_right' if e.pre_right else 'first_shown_wrong'] += 1
        for side in ('right', 'wrong'):
            for rule in e.engine_fired[side]:
                rules.setdefault(rule, Counter())[f'engine_fired_{side}'] += 1
    per_rule = []
    for rule, counts in rules.items():
        r, w = counts.get('engine_fired_right', 0), counts.get('engine_fired_wrong', 0)
        per_rule.append({'rule': rule, **{k: counts.get(k, 0) for k in ('first_shown_right', 'first_shown_wrong',
                                                                         'engine_fired_right', 'engine_fired_wrong')},
                         'engine_precision': _rate(w, r + w)})
    per_rule.sort(key=lambda x: (x['engine_precision'] if x['engine_precision'] is not None else 2, x['rule']))
    noted = [e for e in episodes if e.loops['loop_notes']]
    return {'episodes': n, 'answered': len(answered), 'linked': sum(e.linked for e in answered),
            'coverage': _rate(sum(e.linked for e in answered), len(answered)),
            'coverage_all_episodes': _rate(sum(e.linked for e in answered), n),
            'right_episodes': len(right), 'wrong_episodes': len(wrong),
            'fired_right': sum(e.fired for e in right), 'fired_wrong': sum(e.fired for e in wrong),
            'fire_rate_right': _rate(sum(e.fired for e in right), len(right)),
            'fire_rate_wrong': _rate(sum(e.fired for e in wrong), len(wrong)),
            'fire_rate_right_by_final': _rate(sum(e.fired for e in final_right), len(final_right)),
            'fire_rate_wrong_by_final': _rate(sum(e.fired for e in final_wrong), len(final_wrong)),
            'fired': len(fired), 'fired_pre_right': len(fired_right),
            'wrong_to_right': flips.get('wrong_to_right', 0), 'right_to_wrong': flips.get('right_to_wrong', 0),
            'unchanged': flips.get('unchanged', 0),
            'right_to_wrong_of_fired_right': _rate(flips.get('right_to_wrong', 0), len(fired_right)) if fired_right else 0.0,
            'submit_rate': _rate(sum(e.submitted for e in episodes), n),
            'requery_rate': _rate(sum(e.requeried for e in episodes), n),
            'loop_notes': sum(e.loops['loop_notes'] for e in episodes), 'loop_note_episodes': len(noted),
            'repeated_after_note': sum(e.loops['repeated_after_note'] for e in noted),
            'noted_recovered': sum(e.status == 'completed' and bool(e.final.strip()) for e in noted),
            'per_rule': per_rule}


def _merge(gates: list[dict]) -> dict:
    """Pool the mech-static roots' episode counts (the pilot has one; several are summed)."""
    if len(gates) == 1:
        return gates[0]
    keys = ('episodes', 'answered', 'linked', 'right_episodes', 'wrong_episodes', 'fired_right', 'fired_wrong',
            'fired', 'fired_pre_right', 'wrong_to_right', 'right_to_wrong', 'unchanged')
    total = {k: sum(g[k] for g in gates) for k in keys}
    total.update(coverage=_rate(total['linked'], total['answered']),
                 coverage_all_episodes=_rate(total['linked'], total['episodes']),
                 fire_rate_right=_rate(total['fired_right'], total['right_episodes']),
                 fire_rate_wrong=_rate(total['fired_wrong'], total['wrong_episodes']),
                 right_to_wrong_of_fired_right=(_rate(total['right_to_wrong'], total['fired_pre_right'])
                                                if total['fired_pre_right'] else 0.0),
                 submit_rate=_rate(sum(g['submit_rate'] * g['episodes'] for g in gates), total['episodes']),
                 requery_rate=_rate(sum(g['requery_rate'] * g['episodes'] for g in gates), total['episodes']))
    return total


def go_no_go(gate: dict) -> dict:
    rows = []

    def row(name, value, threshold, verdict):
        rows.append({'criterion': name, 'value': value, 'threshold': threshold, 'verdict': verdict})
    cov = gate['coverage']
    row('1 ledger coverage', f"{_pct(cov)} ({gate['linked']}/{gate['answered']} answered; "
        f"{_pct(gate['coverage_all_episodes'])} of all {gate['episodes']})", '>= 60%',
        'NO DATA' if cov is None else 'PASS' if cov >= COVERAGE_MIN else 'FAIL')
    fr, fw = gate['fire_rate_right'], gate['fire_rate_wrong']
    if fr is None or fw is None:
        verdict = 'NO DATA'
    elif fr == 0:
        verdict = 'PASS' if fw > 0 else 'FAIL'
    else:
        verdict = 'PASS' if fw >= FIRE_RATIO_MIN * fr else 'FAIL'
    row('2 finding rate wrong vs right', f"wrong {_pct(fw)} ({gate['fired_wrong']}/{gate['wrong_episodes']}), "
        f"right {_pct(fr)} ({gate['fired_right']}/{gate['right_episodes']})", 'wrong >= 2 x right', verdict)
    w2r, r2w = gate['wrong_to_right'], gate['right_to_wrong']
    row('3 flips after a finding', f'wrong->right {w2r}, right->wrong {r2w} (of {gate["fired"]} fired)',
        'w->r >= 3 x r->w', 'NO DATA' if w2r == 0 and r2w == 0 else 'PASS' if w2r >= FLIP_RATIO_MIN * r2w else 'FAIL')
    row('4a union finding rate on right episodes', f"{_pct(fr)}", '<= 20%',
        'NO DATA' if fr is None else 'PASS' if fr <= RIGHT_FIRE_MAX else 'FAIL')
    rw = gate['right_to_wrong_of_fired_right']
    row('4b right->wrong of fired right episodes', f"{_pct(rw)} ({r2w}/{gate['fired_pre_right']})", '<= 5%',
        'PASS' if rw <= RIGHT_TO_WRONG_MAX else 'FAIL')
    sr = gate['submit_rate']
    row('5 submit rate', _pct(sr), 'report; < 50% investigate',
        'INVESTIGATE' if sr is not None and sr < SUBMIT_INVESTIGATE_BELOW else 'REPORTED')
    row('5 enforcement re-query rate', _pct(gate['requery_rate']), 'report', 'REPORTED')
    go = all(r['verdict'] == 'PASS' for r in rows if r['criterion'][0] in '1234')
    return {'go': go, 'rows': rows}


def _pct(x):
    return 'n/a' if x is None else f'{100 * x:.1f}%'


def temperature_rule(per_root: list[dict]) -> dict:
    """The pre-registered confirmatory temperature rule over the two text-static cells (counts only)."""
    def cell(values):
        return [m for m in per_root if m['arm'] == RULE_ARM and m['temperature'] == values['temperature']
                and m['top_p'] == values['top_p']]
    a, c = cell(RULE_BASE_CELL), cell(RULE_CELL)
    if len(a) != 1 or len(c) != 1:
        return {'computable': False, 'verdict': None,
                'reason': (f'needs exactly one {RULE_ARM} root at T=0 without top_p (found {len(a)}) and one at '
                           f'T={RULE_CELL["temperature"]}, top_p {RULE_CELL["top_p"]} (found {len(c)})')}
    a, c = a[0], c[0]
    if a['episodes'] != c['episodes']:
        raise ValueError('the temperature cells ran different numbers of questions')
    loops_a, loops_c = a['loop_runs'], c['loop_runs']
    loop_met = loops_a > 0 and loops_c <= (1 - RULE_LOOP_REDUCTION_MIN) * loops_a
    difference = c['right'] - a['right']
    accuracy_met = difference >= -RULE_ACCURACY_MARGIN
    switch = loop_met and accuracy_met
    return {'computable': True, 'cell_a': a['root'], 'cell_c': c['root'], 'questions': a['episodes'],
            'loop_runs_a': loops_a, 'loop_runs_c': loops_c,
            'loop_reduction': None if not loops_a else (loops_a - loops_c) / loops_a,
            'loop_condition_met': loop_met, 'right_a': a['right'], 'right_c': c['right'],
            'accuracy_difference_questions': difference, 'accuracy_condition_met': accuracy_met,
            'confirmatory_temperature': RULE_CELL['temperature'] if switch else RULE_BASE_CELL['temperature'],
            'confirmatory_top_p': RULE_CELL['top_p'] if switch else RULE_BASE_CELL['top_p'],
            'verdict': (f'T={RULE_CELL["temperature"]}, top_p {RULE_CELL["top_p"]}' if switch else 'T=0')}


def p2_rule(per_root: list[dict]) -> dict:
    """The pre-registered re-pilot decision rule (addendum P2): b2 (mech-static, P2 W0, T=0) vs pilot-1 cell a."""
    refs = [m for m in per_root if m['arm'] == P2_REFERENCE['arm'] and m['temperature'] == P2_REFERENCE['temperature']
            and m['top_p'] is P2_REFERENCE['top_p']]
    mech = [m for m in per_root if m['arm'] == P2_ARM and m.get('w0_revision') == P2_W0_REVISION]
    if len(refs) != 1 or len(mech) != 1:
        return {'computable': False, 'verdict': None,
                'reason': (f'needs exactly one {P2_REFERENCE["arm"]} root at T=0 without top_p (found {len(refs)}) and '
                           f'one {P2_ARM} root that ran the P2 mech W0 (found {len(mech)})')}
    ref, b2 = refs[0], mech[0]
    if b2['temperature'] != 0 or b2['top_p'] is not None:
        return {'computable': False, 'verdict': None, 'reason': f'{b2["root"]} did not run at T=0 without top_p'}
    if ref['right'] != P2_REFERENCE_RIGHT:
        return {'computable': False, 'verdict': None,
                'reason': (f'the reference root {ref["root"]} has {ref["right"]} strict correct; the addendum pins the '
                           f'pilot-1 value {P2_REFERENCE_RIGHT}')}
    if ref['episodes'] != b2['episodes']:
        raise ValueError('the re-pilot and its reference ran different numbers of questions')
    right = b2['right']
    verdict = 'PROCEED' if right >= P2_PROCEED_MIN else 'STOP' if right <= P2_STOP_MAX else 'INCONCLUSIVE'
    return {'computable': True, 'reference': ref['root'], 'repilot': b2['root'], 'questions': b2['episodes'],
            'right_reference': ref['right'], 'right_repilot': right, 'difference_questions': right - ref['right'],
            'proceed_min': P2_PROCEED_MIN, 'stop_max': P2_STOP_MAX, 'verdict': verdict, 'meaning': P2_MEANING[verdict],
            'caveat': 'single run each, at different times (provider drift; ~27% of questions flip between runs)'}


def compute(roots, *, grader: Grader | None = None) -> dict:
    per_root = [root_metrics(Path(r), grader) for r in roots]
    subsets = {m['subset'] for m in per_root}
    if len(subsets) != 1:
        raise ValueError(f'the roots declare different question subsets ({len(subsets)} distinct); '
                         'the pilot cells must run the same list')
    mech = [m['gate'] for m in per_root if m['arm'] == 'mech-static' and 'gate' in m]
    result = {'roots': per_root, 'thresholds': {'coverage_min': COVERAGE_MIN, 'fire_ratio_min': FIRE_RATIO_MIN,
                                                 'flip_ratio_min': FLIP_RATIO_MIN, 'right_fire_max': RIGHT_FIRE_MAX,
                                                 'right_to_wrong_max': RIGHT_TO_WRONG_MAX,
                                                 'submit_investigate_below': SUBMIT_INVESTIGATE_BELOW}}
    if mech:
        result['mech_static'] = _merge(mech)
        result['decision'] = go_no_go(result['mech_static'])
    else:
        result['decision'] = None
    result['temperature_rule'] = temperature_rule(per_root)
    result['p2_rule'] = p2_rule(per_root)
    return result


def render_rule(rule: dict) -> list[str]:
    lines = ['', 'confirmatory temperature rule (PREREG addendum P1)']
    if not rule['computable']:
        return lines + [f'  not computable: {rule["reason"]}']
    reduction = 'n/a (cell a has no loop run)' if rule['loop_reduction'] is None else _pct(rule['loop_reduction'])
    lines += [f'  cell a = {rule["cell_a"]} (T=0, no top_p); cell c = {rule["cell_c"]} (T={RULE_CELL["temperature"]}, '
              f'top_p {RULE_CELL["top_p"]}); {rule["questions"]} questions each',
              f'  loop runs: a {rule["loop_runs_a"]}, c {rule["loop_runs_c"]}; c fewer by {reduction} '
              f'(needs >= {_pct(RULE_LOOP_REDUCTION_MIN)}): {"MET" if rule["loop_condition_met"] else "NOT MET"}',
              f'  strict correct: a {rule["right_a"]}, c {rule["right_c"]}; c - a = '
              f'{rule["accuracy_difference_questions"]:+d} questions (needs >= -{RULE_ACCURACY_MARGIN}): '
              f'{"MET" if rule["accuracy_condition_met"] else "NOT MET"}',
              f'  RULE VERDICT: confirmatory arms at {rule["verdict"]}']
    return lines


def render_p2(rule: dict) -> list[str]:
    lines = ['', 're-pilot decision rule (PREREG addendum P2)']
    if not rule['computable']:
        return lines + [f'  not computable: {rule["reason"]}']
    return lines + [f'  reference = {rule["reference"]} (text-static, T=0); re-pilot b2 = {rule["repilot"]} (mech-static, '
                    f'P2 W0, T=0); {rule["questions"]} questions each',
                    f'  strict correct: reference {rule["right_reference"]}, b2 {rule["right_repilot"]} '
                    f'({rule["difference_questions"]:+d}); b2 >= {rule["proceed_min"]} PROCEED, <= {rule["stop_max"]} STOP, '
                    f'between INCONCLUSIVE',
                    f'  P2 VERDICT: {rule["verdict"]} -- {rule["meaning"]}',
                    f'  caveat: {rule["caveat"]}']


def render(result: dict) -> str:
    lines = ['v9 pilot go/no-go (PREREG §7) -- no answer values are shown',
             f'subset sha256: {result["roots"][0]["subset"] if result["roots"] else None}; '
             'controller-side grades agree with every root\'s official grades', '']
    lines.append(f'{"root":<12} {"arm":<12} {"T":>5} {"top_p":>5} {"n":>3} {"right":>5} {"empty":>5} {"loops":>5} '
                 f'{"recov":>5} {"loopR":>5}  statuses')
    for m in result['roots']:
        top_p = '-' if m.get('top_p') is None else str(m['top_p'])
        lines.append(f'{m["root"]:<12} {m["arm"]:<12} {str(m["temperature"]):>5} {top_p:>5} {m["episodes"]:>3} '
                     f'{m["right"]:>5} {m["empty"]:>5} {m["loop_runs"]:>5} {m["loop_runs_recovered"]:>5} '
                     f'{m["loop_runs_right"]:>5}  {json.dumps(m["statuses"], sort_keys=True)}')
    rule_lines = render_rule(result['temperature_rule']) if 'temperature_rule' in result else []
    rule_lines += render_p2(result['p2_rule']) if 'p2_rule' in result else []
    decision = result.get('decision')
    if decision is None:
        lines += rule_lines + ['', 'No mech-static root given: the go/no-go table needs one.']
        return '\n'.join(lines)
    gate = result['mech_static']
    lines += ['', f'{"criterion":<42} {"value":<58} {"threshold":<26} verdict']
    for r in decision['rows']:
        lines.append(f'{r["criterion"]:<42} {r["value"]:<58} {r["threshold"]:<26} {r["verdict"]}')
    lines += ['', f'loop notes {gate.get("loop_notes", "-")} in {gate.get("loop_note_episodes", "-")} episodes; '
                  f'noted command repeated after its note in {gate.get("repeated_after_note", "-")}; '
                  f'noted episodes completed {gate.get("noted_recovered", "-")}']
    if gate.get('per_rule'):
        lines += ['', 'rules (lowest engine precision first): rule  first-shown right/wrong  engine-fired right/wrong']
        for r in gate['per_rule']:
            lines.append(f'  {r["rule"]:<22} {r["first_shown_right"]:>3}/{r["first_shown_wrong"]:<3} '
                         f'{r["engine_fired_right"]:>6}/{r["engine_fired_wrong"]:<3}')
    lines += rule_lines
    lines += ['', 'DECISION: ' + ('GO' if decision['go'] else 'NO-GO')]
    return '\n'.join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('roots', nargs='+', type=Path)
    parser.add_argument('--json', type=Path, help='also write the numbers (no answer values) to this file')
    args = parser.parse_args(argv)
    result = compute(args.roots)
    print(render(result))
    if args.json:
        args.json.write_text(json.dumps(result, indent=1, default=str) + '\n')
    if result['decision'] is None:
        return 2
    return 0 if result['decision']['go'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
