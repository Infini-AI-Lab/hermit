"""Rule selection for the re-pilot b2 (PREREG §5 gate limits, §7 item 4; addendum P2). $0: no model call, no
network. It replays the gate inputs of a sealed mech-static pilot root and disables seed rules by the
pre-registered procedure. It never prints or writes an answer value: the output holds counts, rates and rule
ids only (no task id, no ordinal).

    python pilot/select_rules_p2.py ROOT [--out pilot/pilot2-rule-selection.json] [--no-recheck]

Inputs, controller-side and only after the seal (pilot.metrics.sealed_root): each episode's
api/checks/NNN.json (the gate's recorded inputs and the engine's `fired` list per check), api/ledger.jsonl,
summary.json (the sealed final answer), the prepared answer key and the root's pinned reward.py (strict,
tolerance 0). The controller-side grades of the sealed final answers must reproduce the root's official
grades file exactly, and the per-rule table must equal the one pilot/metrics.py computes (else it refuses).

Replay. With recheck (the default) every stored check is re-derived from its recorded inputs with
harness_core.replay.recheck (same ledger prefix, rules and policy; offers re-executed in the gate's
sandboxed runner): the stored fired rules, shown finding and message must be identical (online = offline).
Each check is then re-evaluated with only the rules left enabled. The engine evaluates every rule on its own,
so the result must equal the stored `fired` list restricted to those rules (checked, counted).

Definitions (the operational definitions of addendum P1, as pilot/metrics.py computes them):
- a checked candidate is right when its strict official grade is 1;
- right episode: its judged candidate is right (the candidate of the first shown finding; else the last
  checked candidate; else the final answer);
- rule r engine-fires on episode e on the right (wrong) side when r is in the `fired` list of a check of e
  whose candidate is right (wrong); engine precision of r = wrong-side episodes / (right-side + wrong-side
  episodes) (the per-rule table of pilot/metrics.py);
- union right-episode fire rate of an enabled set E: right episodes on which some rule of E engine-fires on
  the right side, divided by the right episodes;
- a right->wrong flip (a fired episode whose pre-finding candidate is right and whose sealed final answer is
  wrong) is attributable to r when r was shown in that episode or r engine-fired on its pre-finding
  candidate.

Procedure (addendum P2): disable competing_candidates first, whatever its numbers (its offer is the model's
own --alt); then, while (a) the union right-episode fire rate of the enabled rules is above 20% or (b) a
right->wrong flip is attributable to an enabled rule, disable the enabled rule with the lowest engine
precision (ties: more right-side episodes first, then rule id; a rule that never fired has no precision, comes
last and is never disabled, since disabling it changes nothing). Rule code and thresholds are not changed; the
result becomes the W0 policy.json `disabled_rules`.

Sensitivity (reported, not used for the selection): the same procedure with (i) `final_grade`: labels from
the official grades file (the sealed final answer's grade), any fire in the episode counts; (ii)
`first_check`: an episode fires when an enabled rule fires on its first checked candidate, labelled by that
candidate's grade (the exact counterfactual: the trajectory up to the first submit does not depend on the
rules).
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Callable, Iterable

HERE = Path(__file__).resolve().parent
PLUMBING = HERE.parent
if str(PLUMBING) not in sys.path:
    sys.path.insert(0, str(PLUMBING))

from harness_core.ledger import load_events  # noqa: E402

try:                                             # run as a script or imported as pilot.select_rules_p2
    from pilot import metrics as PM  # noqa: E402
except ImportError:                              # pragma: no cover
    import metrics as PM  # noqa: E402

FORCED = ('competing_candidates',)               # disabled whatever its numbers: its offer is the model's own --alt
RIGHT_FIRE_MAX = PM.RIGHT_FIRE_MAX               # (a) union right-episode fire rate <= 20% (PREREG §5)
ATTRIBUTABLE_FLIPS_MAX = 0                       # (b) right->wrong flips attributable to an enabled rule
VARIANTS = ('primary', 'final_grade', 'first_check')
OUT = HERE / 'pilot2-rule-selection.json'
ARM = 'mech-static'


def _sha(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

# ---------------------------------------------------------------------------------------------
# Episodes (booleans and rule ids only)
# ---------------------------------------------------------------------------------------------


def episode_view(checks: list[dict], events: list, final: str, official_right: bool,
                 grade: Callable[[str], bool]) -> dict:
    """One episode as booleans and rule ids: per check (kind, right, fired, shown), the judged candidate's
    grade, the pre-finding check's fired rules and the flip (pilot/metrics.py definitions)."""
    views = []
    for check in checks:
        answer = (check.get('candidate') or {}).get('answer') or ''
        result = check.get('result') or {}
        views.append({'n': check.get('n'), 'kind': check.get('kind'), 'right': bool(answer.strip()) and grade(answer),
                      'fired': sorted({f.get('rule') for f in check.get('fired') or [] if f.get('rule')}),
                      'shown': [f.get('rule') for f in result.get('findings') or [] if f.get('rule')]})
    shown = PM.shown_findings(checks, events)
    final_right = bool(final.strip()) and grade(final)
    if shown:
        judged_right = bool(shown[0]['candidate'].strip()) and grade(shown[0]['candidate'])
    elif checks:
        judged_right = views[-1]['right']
    else:
        judged_right = final_right
    pre = next((v for v in views if v['n'] == shown[0]['n']), None) if shown else None
    flip = None
    if shown:
        flip = ('wrong_to_right' if not judged_right and final_right else
                'right_to_wrong' if judged_right and not final_right else 'unchanged')
    return {'checks': views, 'final_right': final_right, 'official_right': official_right,
            'judged_right': judged_right, 'fired': bool(shown), 'shown_rules': sorted({s['rule'] for s in shown}),
            'first_shown': shown[0]['rule'] if shown else None, 'pre_fired': list(pre['fired']) if pre else [],
            'flip': flip}


def load_root(root: Path, grader: PM.Grader | None = None) -> dict:
    """The sealed mech-static root's episodes (views) and the raw check files (for the recheck)."""
    info = PM.sealed_root(root)
    root, state, frozen = info['root'], info['state'], info['freeze']
    arm = frozen['arm']
    if arm['name'] != ARM or not arm['controller']:
        raise ValueError(f'{root.name}: a {ARM} root is needed (found {arm["name"]})')
    rows = sorted(state['results'], key=lambda r: r['ordinal'])
    if [r['task_id'] for r in rows] != frozen['order']:
        raise ValueError(f'{root.name}: results do not cover the frozen order')
    keys = PM.references(root, [r['task_id'] for r in rows])
    grades_file = root / 'private-evaluation' / f'{arm["name"]}-grades.json'
    if not grades_file.is_file():
        raise ValueError(f'{root.name}: no {grades_file.name}; the selection runs only after the root was graded')
    official = {r['task_id']: r['strict_score'] >= 1.0 for r in PM._read(grades_file)['results']}
    if set(official) != {r['task_id'] for r in rows}:
        raise ValueError(f'{root.name}: {grades_file.name} does not cover the sealed questions')
    score = grader if grader is not None else PM.official_grader(root)
    episodes, raw = [], []
    for result in rows:
        reference, cache = keys[result['task_id']], {}

        def grade(answer, reference=reference, cache=cache):
            if answer not in cache:
                cache[answer] = float(score(reference, answer, tolerance=0.0)) >= 1.0
            return cache[answer]
        base = root / 'runs/episodes' / f'{result["ordinal"]:03}'
        final = PM._read(base / 'summary.json').get('answer') or ''
        api = base / 'api'
        events = load_events(api / 'ledger.jsonl') if (api / 'ledger.jsonl').is_file() else []
        checks = PM._checks(api)
        view = episode_view(checks, events, final, official[result['task_id']], grade)
        if view['final_right'] != view['official_right']:
            raise ValueError(f'{root.name}: controller-side grades of the sealed answers differ from {grades_file.name}')
        episodes.append(view)
        raw.append((api, checks))
    return {'root': root, 'freeze': frozen, 'episodes': episodes, 'raw': raw}

# ---------------------------------------------------------------------------------------------
# The procedure (pure: episode views in, rule ids and counts out)
# ---------------------------------------------------------------------------------------------


def _right(e: dict, variant: str) -> bool:
    if variant == 'final_grade':
        return e['official_right']
    if variant == 'first_check':
        return e['checks'][0]['right'] if e['checks'] else e['final_right']
    return e['judged_right']


def _fires(e: dict, rules: set[str], variant: str, side: bool | None) -> bool:
    """Some rule of `rules` engine-fires in e (on a candidate of grade `side`; None = any candidate)."""
    if variant == 'first_check':
        return bool(e['checks']) and bool(set(e['checks'][0]['fired']) & rules)
    if variant == 'final_grade':
        side = None
    return any(set(c['fired']) & rules for c in e['checks'] if side is None or c['right'] == side)


def union_fires(episodes: list[dict], enabled: Iterable[str], variant: str = 'primary') -> dict:
    """Right / wrong episodes and those on which an enabled rule engine-fires (per variant)."""
    enabled = set(enabled)
    right = [e for e in episodes if _right(e, variant)]
    wrong = [e for e in episodes if not _right(e, variant)]
    fired_right = sum(_fires(e, enabled, variant, True) for e in right)
    fired_wrong = sum(_fires(e, enabled, variant, False) for e in wrong)
    return {'right_episodes': len(right), 'wrong_episodes': len(wrong), 'fired_right': fired_right,
            'fired_wrong': fired_wrong, 'fire_rate_right': PM._rate(fired_right, len(right)),
            'fire_rate_wrong': PM._rate(fired_wrong, len(wrong))}


def attributable_flips(episodes: list[dict], enabled: Iterable[str]) -> int:
    """right->wrong flips whose shown rules or pre-finding fires include an enabled rule."""
    enabled = set(enabled)
    return sum(1 for e in episodes if e['flip'] == 'right_to_wrong' and (set(e['shown_rules']) | set(e['pre_fired'])) & enabled)


def rule_table(episodes: list[dict], rule_ids: Iterable[str], variant: str = 'primary') -> list[dict]:
    """Per rule: right-side / wrong-side episodes, engine precision (None when it never fired), shown counts."""
    table = []
    for rule in sorted(set(rule_ids)):
        one = {rule}
        if variant == 'primary':
            right = sum(_fires(e, one, variant, True) for e in episodes)
            wrong = sum(_fires(e, one, variant, False) for e in episodes)
        else:
            right = sum(_fires(e, one, variant, None) for e in episodes if _right(e, variant))
            wrong = sum(_fires(e, one, variant, None) for e in episodes if not _right(e, variant))
        table.append({'rule': rule, 'engine_fired_right': right, 'engine_fired_wrong': wrong,
                      'engine_precision': PM._rate(wrong, right + wrong),
                      'shown_episodes': sum(rule in e['shown_rules'] for e in episodes),
                      'first_shown_right': sum(e['first_shown'] == rule and e['judged_right'] for e in episodes),
                      'first_shown_wrong': sum(e['first_shown'] == rule and not e['judged_right'] for e in episodes),
                      'right_to_wrong_attributable': attributable_flips(episodes, one)})
    return table


def order_key(row: dict) -> tuple:
    """Ascending engine precision; ties: more right-side episodes first, then rule id; never fired: last."""
    precision = row['engine_precision']
    return (precision is None, precision if precision is not None else 0.0, -row['engine_fired_right'], row['rule'])


def limits(episodes: list[dict], enabled: set[str], variant: str) -> dict:
    union = union_fires(episodes, enabled, variant)
    flips = attributable_flips(episodes, enabled)
    rate = union['fire_rate_right']
    holds_a = union['fired_right'] == 0 or (rate is not None and rate <= RIGHT_FIRE_MAX)
    return {**union, 'right_to_wrong_attributable': flips, 'limit_a_holds': holds_a,
            'limit_b_holds': flips <= ATTRIBUTABLE_FLIPS_MAX, 'limits_hold': holds_a and flips <= ATTRIBUTABLE_FLIPS_MAX}


def select(episodes: list[dict], rule_ids: Iterable[str], variant: str = 'primary') -> dict:
    """The pre-registered procedure: forced disables, then the lowest engine precision first until (a) and (b)."""
    rule_ids = sorted(set(rule_ids))
    table = rule_table(episodes, rule_ids, variant)
    order = sorted(table, key=order_key)
    enabled = set(rule_ids)
    steps = [{'step': 0, 'disable': None, 'reason': 'start (all rules enabled)',
              **limits(episodes, enabled, variant)}]
    for rule in FORCED:
        if rule in enabled:
            enabled.discard(rule)
            steps.append({'step': len(steps), 'disable': rule, 'reason': 'forced: its offer is the model\'s own --alt',
                          **limits(episodes, enabled, variant)})
    for row in order:
        if steps[-1]['limits_hold']:
            break
        if row['rule'] not in enabled:
            continue
        if row['engine_precision'] is None:
            break                                  # never fired: disabling it changes nothing
        enabled.discard(row['rule'])
        steps.append({'step': len(steps), 'disable': row['rule'],
                      'reason': f'lowest engine precision among the enabled rules ({row["engine_fired_wrong"]} wrong / '
                                f'{row["engine_fired_right"] + row["engine_fired_wrong"]} fired episodes)',
                      **limits(episodes, enabled, variant)})
    return {'variant': variant, 'rule_table': table, 'order': [r['rule'] for r in order], 'steps': steps,
            'disabled_rules': sorted(set(rule_ids) - enabled), 'enabled_rules': sorted(enabled),
            'limits_hold': steps[-1]['limits_hold']}

# ---------------------------------------------------------------------------------------------
# Recheck (online = offline) and the enabled-only replay
# ---------------------------------------------------------------------------------------------


def recheck(loaded: dict, enabled: Iterable[str], *, pack=None, runner=None) -> dict:
    """Re-derive every stored check (harness_core.replay.recheck) and replay it with the enabled rules only."""
    from harness_core import replay
    enabled = set(enabled)
    root, frozen = loaded['root'], loaded['freeze']
    close = pack is None
    if pack is None or runner is None:
        import arms
        import paths
        from harness_core.runner import make_runner
        corpus = (root / 'prepared/agent/corpus').resolve()
        index = (root / 'prepared/agent/tables/tables.sqlite').resolve()
        if frozen.get('tables_index_sha256') and _sha(index) != frozen['tables_index_sha256']:
            raise ValueError('the tables index differs from the frozen one')
        pack = pack or arms.load_pack('officeqa', corpus, index)
        runner = runner or make_runner(paths.ACTOR_PYTHON, extra_read=[str(corpus)])
    totals = {'checks': 0, 'identical': 0, 'ledger_sha_ok': 0, 'enabled_replay_equal': 0}
    try:
        for api, checks in loaded['raw']:
            for check in checks:
                ledger = api / 'ledger.jsonl'
                full = replay.recheck(check, ledger, pack, runner)
                sub = replay.recheck({**check, 'rules': [r for r in check['rules'] if r['id'] in enabled]}, ledger,
                                     pack, runner)
                stored = [f.get('rule') for f in check.get('fired') or []]
                totals['checks'] += 1
                totals['identical'] += bool(full['identical'])
                totals['ledger_sha_ok'] += bool(full['ledger_sha_ok'])
                totals['enabled_replay_equal'] += sub['fired'] == [r for r in stored if r in enabled]
    finally:
        if close and hasattr(pack, 'close'):
            pack.close()
    totals['all_identical'] = totals['identical'] == totals['checks'] == totals['ledger_sha_ok']
    totals['all_enabled_replay_equal'] = totals['enabled_replay_equal'] == totals['checks']
    return totals

# ---------------------------------------------------------------------------------------------
# Whole selection
# ---------------------------------------------------------------------------------------------


def served_rules(loaded: dict) -> list[str]:
    ids = {tuple(sorted(r['id'] for r in check.get('rules') or [])) for _, checks in loaded['raw'] for check in checks}
    if len(ids) != 1:
        raise ValueError(f'the checks served {len(ids)} different rule sets')
    return list(next(iter(ids)))


def compute(root: Path, *, grader: PM.Grader | None = None, do_recheck: bool = True, pack=None, runner=None) -> dict:
    loaded = load_root(Path(root), grader)
    episodes, rule_ids = loaded['episodes'], served_rules(loaded)
    primary = select(episodes, rule_ids, 'primary')
    # the per-rule table and the gate numbers must be pilot/metrics.py's own
    metrics = PM.root_metrics(Path(root), grader)['gate']
    theirs = {r['rule']: (r['engine_fired_right'], r['engine_fired_wrong']) for r in metrics['per_rule']}
    mine = {r['rule']: (r['engine_fired_right'], r['engine_fired_wrong']) for r in primary['rule_table']
            if r['engine_fired_right'] or r['engine_fired_wrong'] or r['rule'] in theirs}
    start = primary['steps'][0]
    if (mine != theirs or start['right_episodes'] != metrics['right_episodes']
            or sum(e['flip'] == 'right_to_wrong' for e in episodes) != metrics['right_to_wrong']):
        raise ValueError('the replay does not reproduce the per-rule table of pilot/metrics.py')
    sensitivity = {v: select(episodes, rule_ids, v) for v in VARIANTS if v != 'primary'}
    final = set(primary['enabled_rules'])
    result = {
        'procedure': 'PREREG addendum P2: disable competing_candidates, then the lowest engine precision first '
                     '(ties: more right-side episodes, then rule id) until (a) union right-episode fire rate <= 20% '
                     'and (b) 0 right->wrong flips attributable to an enabled rule',
        'source': {'root': loaded['root'].name, 'arm': ARM, 'episodes': len(episodes),
                   'checks': sum(len(c) for _, c in loaded['raw']),
                   'subset_sha256': ((loaded['freeze'].get('campaign') or {}).get('subset') or {}).get('sha256'),
                   'w0_revision': (loaded['freeze'].get('w0') or {}).get('revision'),
                   'labels': 'strict official reward.py grades (tolerance 0), controller-side after the seal; the '
                             'sealed final answers reproduce the root\'s official grades file'},
        'limits': {'right_fire_max': RIGHT_FIRE_MAX, 'right_to_wrong_attributable_max': ATTRIBUTABLE_FLIPS_MAX},
        'served_rules': rule_ids, 'forced': list(FORCED),
        'disabled_rules': primary['disabled_rules'], 'enabled_rules': primary['enabled_rules'],
        'limits_hold': primary['limits_hold'], 'rule_table': primary['rule_table'], 'order': primary['order'],
        'steps': primary['steps'],
        'pilot1_gate': {k: metrics[k] for k in ('right_episodes', 'wrong_episodes', 'fired_right', 'fired_wrong',
                                                 'fire_rate_right', 'fire_rate_wrong', 'wrong_to_right',
                                                 'right_to_wrong', 'fired_pre_right')},
        'replay_with_enabled': {v: {**union_fires(episodes, final, v),
                                    'right_to_wrong_attributable': attributable_flips(episodes, final)}
                                for v in VARIANTS},
        'sensitivity': {v: {'disabled_rules': s['disabled_rules'], 'limits_hold': s['limits_hold'],
                            'same_as_primary': s['disabled_rules'] == primary['disabled_rules'], 'order': s['order']}
                        for v, s in sensitivity.items()},
        'script_sha256': _sha(Path(__file__)),
        'not_replayable': 'criterion 3 (flips after a finding) depends on the model\'s reply to a finding and is '
                          'measured again in b2',
    }
    result['recheck'] = recheck(loaded, final, pack=pack, runner=runner) if do_recheck else None
    return result


def render(result: dict) -> str:
    lines = [f'rule selection (PREREG addendum P2) on {result["source"]["root"]}: {result["source"]["episodes"]} '
             f'episodes, {result["source"]["checks"]} checks -- no answer values are shown', '',
             f'{"rule":<22} {"right":>5} {"wrong":>5} {"precision":>9} {"shown":>5} {"r->w":>4}']
    for row in sorted(result['rule_table'], key=order_key):
        precision = 'n/a' if row['engine_precision'] is None else f'{row["engine_precision"]:.3f}'
        lines.append(f'{row["rule"]:<22} {row["engine_fired_right"]:>5} {row["engine_fired_wrong"]:>5} {precision:>9} '
                     f'{row["shown_episodes"]:>5} {row["right_to_wrong_attributable"]:>4}')
    lines += ['', 'steps: disable -> union right-episode fires / right episodes; attributable right->wrong']
    for s in result['steps']:
        lines.append(f'  {s["step"]}. {s["disable"] or "-":<22} {s["fired_right"]}/{s["right_episodes"]} '
                     f'({PM._pct(s["fire_rate_right"])}); r->w {s["right_to_wrong_attributable"]}; '
                     f'limits {"HOLD" if s["limits_hold"] else "fail"}')
    lines += ['', f'disabled: {", ".join(result["disabled_rules"])}', f'enabled:  {", ".join(result["enabled_rules"])}']
    for v, s in result['sensitivity'].items():
        lines.append(f'sensitivity {v}: disabled {", ".join(s["disabled_rules"])} '
                     f'({"same as primary" if s["same_as_primary"] else "DIFFERS"})')
    if result.get('recheck'):
        r = result['recheck']
        lines.append(f'recheck: {r["identical"]}/{r["checks"]} checks re-derived identically; enabled-only replay equal '
                     f'on {r["enabled_replay_equal"]}/{r["checks"]}')
    return '\n'.join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('root', type=Path)
    parser.add_argument('--out', type=Path, default=OUT)
    parser.add_argument('--no-recheck', action='store_true', help='skip the re-derivation of the stored checks')
    args = parser.parse_args(argv)
    result = compute(args.root, do_recheck=not args.no_recheck)
    print(render(result))
    args.out.write_text(json.dumps(result, indent=1, sort_keys=True) + '\n')
    ok = result['limits_hold'] and (result['recheck'] is None or (result['recheck']['all_identical']
                                                                    and result['recheck']['all_enabled_replay_equal']))
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
