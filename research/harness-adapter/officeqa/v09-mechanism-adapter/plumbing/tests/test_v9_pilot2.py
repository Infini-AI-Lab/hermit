"""Re-pilot b2 (PREREG addendum P2) tests ($0, offline; no real reference answer or grader output is read):

- the P2 mech W0: SEED.md = the text W0's SEED.md + the finishing text; no model-visible text suggests --alt
  (SEED.md, README, gate usage, submit --help) while the submit CLI still accepts it; policy.json disables
  exactly the rules of the committed rule selection;
- pilot/select_rules_p2.py: the pre-registered procedure (forced competing_candidates, then the lowest engine
  precision first; ties by right-side fires, then id; never-fired rules never disabled; stop when (a) and (b)
  hold) on a synthetic sealed root with a toy grader, its cross-check with pilot/metrics.py, the recheck
  counters, refusals, and that no answer value reaches its output; the committed selection file;
- pilot/metrics.py: the re-pilot decision rule (PROCEED / INCONCLUSIVE / STOP boundaries, not computable cases)
  on synthetic roots; the P1 temperature rule untouched;
- addendum P2: appended verbatim after P1, pins equal to the plumbing (W0 revision, SEED.md, policy.json, the
  selection file and script, the metrics script), the numbers of the decision rule;
- run.py pilot2-root: refuses without (or with a stale) addendum P2, then creates the one neutral-named root.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

import arms  # noqa: E402
import campaign as CP  # noqa: E402
from harness_core import contracts as C  # noqa: E402
from harness_core import gate as G  # noqa: E402
from harness_core import rules as R  # noqa: E402
from pilot import metrics as PM  # noqa: E402
from pilot import select_rules_p2 as SR  # noqa: E402
import test_v9_pilot as TP  # noqa: E402   (helpers only; module-qualified so pytest does not re-collect)

PY = sys.executable
REF, TOY, CANDIDATES = TP.REF, TP.TOY, TP.CANDIDATES
W1, W2, W3 = CANDIDATES[:3]
RULES = ('alpha', 'beta', 'competing_candidates', 'delta', 'gamma', 'omega')


def sha(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def no_answer_values(text: str) -> None:
    for value in list(REF.values()) + list(CANDIDATES):
        assert value not in text, value

# ---------------------------------------------------------------------------------------------
# The P2 mech W0 and --alt
# ---------------------------------------------------------------------------------------------


def test_mech_w0_is_the_text_w0_plus_the_finishing_text_and_never_suggests_alt(tmp_path):
    text_seed = (ROOT / 'adapters/text-w0/SEED.md').read_text()
    mech_seed = (ROOT / 'adapters/mech-w0/SEED.md').read_text()
    assert mech_seed.startswith(text_seed) and mech_seed[len(text_seed):] == '\n' + arms.MECH_SEED
    for part in ('# Finishing a question', 'submit --answer', 'Offer:', 'Accept', 'Keep', '[harness:...]'):
        assert part in arms.MECH_SEED, part
    for visible in (mech_seed, CP.SUBMIT_README, G.USAGE):
        assert '--alt' not in visible
    # the toy pack's W0 follows the same composition and keeps every rule enabled (no pilot data there)
    toy = arms.load_pack('toy')
    files = arms.w0_files('rules', toy)
    assert files['SEED.md'] == arms.text_seed(toy) + '\n' + arms.MECH_SEED
    assert json.loads(files['policy.json']) == C.POLICY_DEFAULTS
    # the submit CLI still accepts --alt but does not list it
    tool = ROOT / 'harness_core/tools/submit.py'
    shown = subprocess.run([PY, '-B', str(tool), '--help'], capture_output=True, text=True, timeout=30)
    assert shown.returncode == 0 and '--answer' in shown.stdout and '--alt' not in shown.stdout
    accepted = subprocess.run([PY, '-B', str(tool), '--alt', 'x'], capture_output=True, text=True, timeout=30,
                              env={'PATH': '/usr/bin:/bin', C.SUBMIT_ENV: 'http://127.0.0.1:9/v1/submit'})
    assert accepted.returncode == 2 and 'give --answer' in accepted.stderr         # parsed; refused for no answer
    # the model-visible bytes addendum P2 records
    addendum = (ROOT / 'pilot/ADDENDUM-P2.md').read_text()
    text = arms.check_w0(C.STANDARD_ARMS['text-static'])['model_visible']
    mech = arms.check_w0(C.STANDARD_ARMS['mech-static'])['model_visible']
    for key, value in (('p2-text-w0-model-visible-bytes', text['total']),
                       ('p2-mech-w0-model-visible-bytes', mech['total']),
                       ('p2-mech-seed-bytes', mech['seed_md']), ('p2-mech-gate-frame-bytes', mech['gate_frame'])):
        assert f'{key}: {value}\n' in addendum, key
    assert mech['total'] <= C.TEXT_BUDGET and text['total'] <= C.TEXT_BUDGET


def test_w0_refuses_to_disable_an_unknown_rule(monkeypatch):
    monkeypatch.setitem(arms.W0_DISABLED_RULES, 'officeqa', ('no_such_rule',))
    with pytest.raises(ValueError, match='unknown seed rules'):
        arms.w0_files('rules', arms.load_pack('officeqa'))


def test_gate_skips_the_policy_disabled_rules_of_the_w0():
    from harness_core.gate import Gate
    from harness_core.runner import make_runner
    adapter = ROOT / 'adapters/mech-w0'
    gate = Gate(arms.load_pack('officeqa'), arms.load_adapter_rules(adapter), arms.load_policy(adapter),
                make_runner(), 'How much was spent in fiscal year 1990?')
    assert sorted(r.id for r in gate.rules) == sorted(
        {r.id for r in arms.load_adapter_rules(adapter)} - set(arms.W0_DISABLED_RULES['officeqa']))

# ---------------------------------------------------------------------------------------------
# pilot/select_rules_p2.py
# ---------------------------------------------------------------------------------------------


def check(n, answer, status, fired=(), shown=()):
    return {'n': n, 'kind': 'submit', 'candidate': {'answer': answer, 'via': 'submit', 'ref': f'a{n}'},
            'result': {'status': status, 'findings': [{'rule': r, 'text': 'x'} for r in shown], 'message': 'ok'},
            'fired': [{'rule': r} for r in fired], 'rules': [{'id': r} for r in RULES]}


def episode(task, final, checks):
    return {'task': task, 'final': final, 'controller': {'submits': len(checks), 'enforce_requeries': 0},
            'checks': checks}


# Judged candidate = the first checked candidate everywhere. Right episodes: T1 T2 T3 T4 S2 (5); wrong: T5 T6 S1.
# Engine fires by side: alpha R{T1} W{T5}; beta R{T2} W{T6}; delta R{T4,S2} W{T5,S1}; gamma W{T5,T6,S1};
# competing_candidates R{T3} (and the only right->wrong flip); omega never fires.
SELECTION_EPISODES = [
    episode('T1', REF['T1'], [check(1, REF['T1'], 'findings', ['alpha'], ['alpha']), check(2, REF['T1'], 'accepted')]),
    episode('T2', REF['T2'], [check(1, REF['T2'], 'findings', ['beta'], ['beta']), check(2, REF['T2'], 'kept')]),
    episode('T3', W1, [check(1, REF['T3'], 'findings', ['competing_candidates'], ['competing_candidates']),
                       check(2, W1, 'accepted')]),
    episode('T4', REF['T4'], [check(1, REF['T4'], 'findings', ['delta'], ['delta']), check(2, REF['T4'], 'kept')]),
    episode('T5', W2, [check(1, W2, 'findings', ['alpha', 'gamma', 'delta'], ['alpha']), check(2, W2, 'kept')]),
    episode('T6', W3, [check(1, W3, 'findings', ['beta', 'gamma'], ['beta']), check(2, W3, 'kept')]),
    episode('S1', REF['S1'], [check(1, W1, 'findings', ['gamma', 'delta'], ['gamma']), check(2, REF['S1'], 'accepted')]),
    episode('S2', REF['S2'], [check(1, REF['S2'], 'findings', ['delta'], ['delta']), check(2, REF['S2'], 'kept')]),
]


def test_rule_selection_procedure_on_a_synthetic_sealed_root(tmp_path):
    root = TP.make_sealed_root(tmp_path, 'v9-p1-b', 'mech-static', SELECTION_EPISODES)
    result = SR.compute(root, grader=TOY, do_recheck=False)
    table = {r['rule']: r for r in result['rule_table']}
    assert (table['delta']['engine_fired_right'], table['delta']['engine_fired_wrong']) == (2, 2)
    assert table['gamma']['engine_precision'] == 1.0 and table['omega']['engine_precision'] is None
    assert table['competing_candidates']['right_to_wrong_attributable'] == 1
    # ascending precision; ties at 0.5 by more right-side fires (delta), then id (alpha < beta); never fired: last
    assert result['order'] == ['competing_candidates', 'delta', 'alpha', 'beta', 'gamma', 'omega']
    steps = [(s['disable'], s['fired_right'], s['right_episodes'], s['right_to_wrong_attributable'], s['limits_hold'])
             for s in result['steps']]
    assert steps == [(None, 5, 5, 1, False), ('competing_candidates', 4, 5, 0, False), ('delta', 2, 5, 0, False),
                     ('alpha', 1, 5, 0, True)]                        # 1/5 = 20% holds (<= 20%): stop
    assert result['disabled_rules'] == ['alpha', 'competing_candidates', 'delta'] and result['limits_hold']
    assert result['enabled_rules'] == ['beta', 'gamma', 'omega'] and result['served_rules'] == list(RULES)
    assert result['pilot1_gate']['right_episodes'] == 5 and result['pilot1_gate']['right_to_wrong'] == 1
    assert result['replay_with_enabled']['primary']['fired_wrong'] == 3        # T5 and S1 (gamma), T6 (beta, gamma)
    assert result['recheck'] is None and set(result['sensitivity']) == {'final_grade', 'first_check'}
    out = json.dumps(result) + SR.render(result)
    no_answer_values(out)
    assert not any(t in out for t in ('"T1"', '"S2"', 'ordinal', 'task_id'))    # counts and rule ids only
    # command line: writes the file, exit 0 when the limits hold
    import unittest.mock
    with unittest.mock.patch.object(PM, 'official_grader', lambda r: TOY):
        assert SR.main([str(root), '--out', str(tmp_path / 'sel.json'), '--no-recheck']) == 0
    assert json.loads((tmp_path / 'sel.json').read_text())['disabled_rules'] == result['disabled_rules']


def test_rule_selection_limits_and_never_fired_rules():
    def view(right, fired, shown=(), flip=None):
        checks = [{'n': 1, 'kind': 'submit', 'right': right, 'fired': list(fired), 'shown': list(shown)}]
        return {'checks': checks, 'final_right': right if flip is None else not right, 'official_right': right,
                'judged_right': right, 'fired': bool(shown), 'shown_rules': sorted(shown),
                'first_shown': shown[0] if shown else None, 'pre_fired': list(fired) if shown else [], 'flip': flip}
    # a right->wrong flip after a rule other than competing_candidates keeps (b) failing until that rule is off,
    # even when (a) already holds
    episodes = [view(True, ['beta'], ['beta'], 'right_to_wrong')] + [view(True, []) for _ in range(9)] + \
               [view(False, ['beta', 'gamma'], ['beta']), view(False, ['gamma'], ['gamma'])]
    result = SR.select(episodes, ['beta', 'gamma', 'omega', 'competing_candidates'])
    assert [s['disable'] for s in result['steps']] == [None, 'competing_candidates', 'beta']
    assert result['steps'][1]['limit_a_holds'] and not result['steps'][1]['limit_b_holds']
    assert result['disabled_rules'] == ['beta', 'competing_candidates'] and result['limits_hold']
    # a rule that never fired is never disabled (it would change nothing)
    stubborn = [view(True, ['omega-x'])] * 3
    result = SR.select(stubborn, ['omega-x', 'zeta'])
    assert result['disabled_rules'] == ['omega-x'] and result['limits_hold']
    assert SR.select([view(True, [])], ['zeta'])['disabled_rules'] == []
    # first-check variant: only the first checked candidate counts
    two = {'checks': [{'n': 1, 'right': True, 'fired': [], 'shown': []},
                      {'n': 2, 'right': True, 'fired': ['beta'], 'shown': []}],
           'final_right': True, 'official_right': True, 'judged_right': True, 'fired': False, 'shown_rules': [],
           'first_shown': None, 'pre_fired': [], 'flip': None}
    assert SR.union_fires([two], {'beta'}, 'primary')['fired_right'] == 1
    assert SR.union_fires([two], {'beta'}, 'first_check')['fired_right'] == 0


def test_rule_selection_recheck_counters_and_refusals(tmp_path, monkeypatch):
    root = TP.make_sealed_root(tmp_path, 'v9-p1-b', 'mech-static', SELECTION_EPISODES)
    loaded = SR.load_root(root, TOY)
    from harness_core import replay
    calls = []

    def fake(check, ledger, pack, runner):
        calls.append(len(check['rules']))
        kept = {r['id'] for r in check['rules']}
        return {'n': check['n'], 'identical': True, 'ledger_sha_ok': True,
                'fired': [f['rule'] for f in check['fired'] if f['rule'] in kept], 'status': 'x'}
    monkeypatch.setattr(replay, 'recheck', fake)
    totals = SR.recheck(loaded, {'beta', 'gamma'}, pack=object(), runner=object())
    assert totals == {'checks': 16, 'identical': 16, 'ledger_sha_ok': 16, 'enabled_replay_equal': 16,
                      'all_identical': True, 'all_enabled_replay_equal': True}
    assert sorted(set(calls)) == [2, len(RULES)]                        # full rule set, then the enabled rules only
    # refusals: not a mech-static root; grades that disagree with the official grades file
    text = TP.make_sealed_root(tmp_path, 'v9-p1-a', 'text-static', TP.TEXT_EPISODES)
    with pytest.raises(ValueError, match='mech-static root is needed'):
        SR.load_root(text, TOY)
    grades = root / 'private-evaluation/mech-static-grades.json'
    value = json.loads(grades.read_text())
    value['results'][0]['strict_score'] = 0.0
    grades.write_text(json.dumps(value))
    with pytest.raises(ValueError, match='differ'):
        SR.load_root(root, TOY)


def test_committed_rule_selection_is_the_w0_policy_and_is_pinned():
    selection = json.loads((ROOT / 'pilot/pilot2-rule-selection.json').read_text())
    policy = json.loads((ROOT / 'adapters/mech-w0/policy.json').read_text())
    seed_ids = {r.id for r in R.core_seed_rules()} | {r.id for r in arms.load_pack('officeqa').seed_rules()}
    assert selection['disabled_rules'] == policy['disabled_rules'] == sorted(arms.W0_DISABLED_RULES['officeqa'])
    assert set(selection['disabled_rules']) | set(selection['enabled_rules']) == set(selection['served_rules']) == seed_ids
    assert 'competing_candidates' in selection['disabled_rules'] and selection['forced'] == ['competing_candidates']
    assert selection['limits_hold'] and selection['steps'][-1]['limits_hold']
    assert not any(s['limits_hold'] for s in selection['steps'][:-1])          # stopped at the first step that holds
    final = selection['steps'][-1]
    assert final['fire_rate_right'] <= SR.RIGHT_FIRE_MAX and final['right_to_wrong_attributable'] == 0
    assert selection['recheck']['all_identical'] and selection['recheck']['all_enabled_replay_equal']
    assert selection['source']['root'] == 'v9-p1-b' and selection['source']['episodes'] == 50
    assert selection['script_sha256'] == sha(ROOT / 'pilot/select_rules_p2.py')
    text = json.dumps(selection)
    assert not re.search(r'UID\d', text) and 'ordinal' not in text and 'task_id' not in text
    # the order is the pre-registered one: ascending engine precision, ties by right-side fires, then id
    assert selection['order'] == [r['rule'] for r in sorted(selection['rule_table'], key=SR.order_key)]

# ---------------------------------------------------------------------------------------------
# pilot/metrics.py: the re-pilot decision rule
# ---------------------------------------------------------------------------------------------


def mech_cell(right: int) -> list[dict]:
    """Eight mech-static episodes over the REF tasks, the first `right` right, each submitted once and recorded."""
    out = []
    for i, task in enumerate(sorted(REF)):
        final = REF[task] if i < right else 'no-match'
        out.append({'task': task, 'final': final, 'controller': {'submits': 1, 'enforce_requeries': 0},
                    'ledger': [TP._ev('candidate', ref='a1', answer=final)],
                    'checks': [TP._check(1, 'submit', final, 'accepted')]})
    return out


def with_w0(root: Path, revision: str) -> Path:
    frozen = json.loads((root / 'freeze.json').read_text())
    frozen['w0'] = {'revision': revision}
    (root / 'freeze.json').write_text(json.dumps(frozen))
    return root


@pytest.mark.parametrize('b2, verdict', [(8, 'PROCEED'), (6, 'PROCEED'), (5, 'INCONCLUSIVE'), (3, 'INCONCLUSIVE'),
                                         (2, 'STOP'), (0, 'STOP')])
def test_p2_decision_rule_boundaries(tmp_path, monkeypatch, b2, verdict):
    # the pinned numbers are 31 / 31 / 27 of 50; the synthetic cells have 8 questions: reference 6, proceed >= 6,
    # stop <= 2 (the same shape: proceed at the reference, stop 4 below it)
    monkeypatch.setattr(PM, 'P2_REFERENCE_RIGHT', 6)
    monkeypatch.setattr(PM, 'P2_PROCEED_MIN', 6)
    monkeypatch.setattr(PM, 'P2_STOP_MAX', 2)
    a = TP.make_sealed_root(tmp_path, 'v9-p1-a', 'text-static', TP._text_cell(2, 6), temperature=0)
    b = with_w0(TP.make_sealed_root(tmp_path, 'v9-p2-a', 'mech-static', mech_cell(b2)), PM.P2_W0_REVISION)
    result = PM.compute([a, b], grader=TOY)
    rule = result['p2_rule']
    assert rule['computable'] and (rule['reference'], rule['repilot'], rule['questions']) == ('v9-p1-a', 'v9-p2-a', 8)
    assert (rule['right_reference'], rule['right_repilot'], rule['difference_questions']) == (6, b2, b2 - 6)
    assert rule['verdict'] == verdict and rule['meaning'] == PM.P2_MEANING[verdict]
    assert result['decision'] is not None                                   # the §7 table is computed for b2
    assert result['temperature_rule']['computable'] is False               # no cell c: the P1 rule is not applied
    text = PM.render(result)
    assert f'P2 VERDICT: {verdict}' in text and 'caveat' in text
    no_answer_values(text + json.dumps(result, default=str))


def test_p2_decision_rule_not_computable_cases_and_pinned_numbers(tmp_path, monkeypatch, capsys):
    assert (PM.P2_REFERENCE_RIGHT, PM.P2_PROCEED_MIN, PM.P2_STOP_MAX) == (31, 31, 27)
    assert PM.P2_W0_REVISION == CP.revision(ROOT / 'adapters/mech-w0')
    addendum = (ROOT / 'pilot/ADDENDUM-P2.md').read_text()
    for key, value in (('p2-reference-strict-correct', 31), ('p2-proceed-min', 31), ('p2-stop-max', 27),
                       ('p2-mech-w0-revision', PM.P2_W0_REVISION)):
        assert f'{key}: {value}\n' in addendum, key
    monkeypatch.setattr(PM, 'P2_REFERENCE_RIGHT', 6)
    monkeypatch.setattr(PM, 'P2_PROCEED_MIN', 6)
    monkeypatch.setattr(PM, 'P2_STOP_MAX', 2)
    a = TP.make_sealed_root(tmp_path, 'v9-p1-a', 'text-static', TP._text_cell(2, 6), temperature=0)
    # a mech root that ran another W0 (pilot 1) is not b2
    old = with_w0(TP.make_sealed_root(tmp_path, 'v9-p1-b', 'mech-static', mech_cell(7)), 'f' * 64)
    rule = PM.compute([a, old], grader=TOY)['p2_rule']
    assert not rule['computable'] and 'found 0' in rule['reason']
    b = with_w0(TP.make_sealed_root(tmp_path, 'v9-p2-a', 'mech-static', mech_cell(7)), PM.P2_W0_REVISION)
    # the reference must be the pinned pilot-1 cell (right count), at T=0 without top_p
    other = TP.make_sealed_root(tmp_path, 'v9-p1-x', 'text-static', TP._text_cell(2, 5), temperature=0)
    rule = PM.compute([other, b], grader=TOY)['p2_rule']
    assert not rule['computable'] and 'pins the pilot-1 value 6' in rule['reason']
    hot = TP.make_sealed_root(tmp_path, 'v9-p1-c', 'text-static', TP._text_cell(0, 6), temperature=1.0, top_p=0.95)
    assert not PM.compute([hot, b], grader=TOY)['p2_rule']['computable']
    # b2 must run at T=0 without top_p
    frozen = json.loads((b / 'freeze.json').read_text())
    frozen['settings']['top_p'] = 0.95
    (b / 'freeze.json').write_text(json.dumps(frozen))
    rule = PM.compute([a, b], grader=TOY)['p2_rule']
    assert not rule['computable'] and 'T=0 without top_p' in rule['reason']
    frozen['settings']['top_p'] = None
    (b / 'freeze.json').write_text(json.dumps(frozen))
    # command line: the verdict is printed; the exit code stays the go/no-go of the mech root
    monkeypatch.setattr(PM, 'official_grader', lambda root: TOY)
    code = PM.main([str(a), str(b), '--json', str(tmp_path / 'p2.json')])
    out = capsys.readouterr().out
    assert code in (0, 1) and 'P2 VERDICT: PROCEED' in out and 'DECISION:' in out
    assert json.loads((tmp_path / 'p2.json').read_text())['p2_rule']['verdict'] == 'PROCEED'
    no_answer_values(out + (tmp_path / 'p2.json').read_text())

# ---------------------------------------------------------------------------------------------
# Addendum P2 and run.py pilot2-root
# ---------------------------------------------------------------------------------------------


def test_addendum_p2_is_appended_after_p1_and_pins_the_plumbing():
    import run
    prereg = (ROOT / 'PREREG.md').read_text()
    p1 = (ROOT / 'pilot/ADDENDUM.md').read_text()
    p2 = (ROOT / 'pilot/ADDENDUM-P2.md').read_text()
    assert p1 in prereg and p2 in prereg and prereg.index(p2) > prereg.index(p1)
    assert prereg.rstrip('\n').endswith(p2.rstrip('\n'))
    assert '[USER:' not in p2 and '{' not in p2
    listing = json.loads((ROOT / 'pilot/first50.json').read_text())
    info = run.pilot2_addendum(ROOT, listing['sha256'])
    assert info['pins']['p2-root'] == run.P2_ROOT == 'v9-p2-a' and info['pins']['p2-arm'] == 'mech-static'
    assert {k: info['pins'][k] for k in run.pilot2_pins(ROOT)} == run.pilot2_pins(ROOT)
    for text in ('b2 ≥ 31', 'b2 ≤ 27', '28–30', 'provider drift', 'v9-p1-a', 'text-static'):
        assert text in p2, text
    # P1's own checks still pass on the extended PREREG.md (one list hash, one cell temperature and top_p)
    assert run.pilot_addendum(ROOT / 'PREREG.md', listing['sha256'])['cell_temperature'] == 1.0


def test_pilot2_root_needs_addendum_p2_then_creates_one_root(tmp_path_factory, tmp_path):
    import run
    source = TP.plumbing_copy(tmp_path_factory)
    parent = tmp_path / 'project'
    framework, cache = parent / 'fw', parent / 'cache'
    framework.mkdir(parents=True)
    cache.mkdir()
    options = {'framework': framework, 'data_cache': cache, 'source': source}
    prereg = source / 'PREREG.md'
    full = prereg.read_text()
    p2 = (source / 'pilot/ADDENDUM-P2.md').read_text()
    prereg.write_text(full.replace(p2, ''))                                   # PREREG.md without addendum P2
    with pytest.raises(ValueError, match='addendum P2'):
        run.pilot2_root(parent, **options)
    prereg.write_text(full)
    # a stale pin (the W0 changed after the addendum) is refused
    policy = source / 'adapters/mech-w0/policy.json'
    saved = policy.read_text()
    policy.write_text(saved.replace('"table_alternatives"', '"table_alternatives", "scale"'))
    with pytest.raises(ValueError, match='p2-mech-w0-revision'):
        run.pilot2_root(parent, **options)
    policy.write_text(saved)
    listing = json.loads((source / 'pilot/first50.json').read_text())
    result = run.pilot2_root(parent, **options)
    assert result['paid_calls'] == 0 and list(result['created']) == ['v9-p2-a']
    assert result['subset'] == {'name': 'pilot-first50', 'sha256': listing['sha256'], 'count': 50}
    root = parent / 'v9-p2-a'
    config = json.loads((root / '.task-paths.json').read_text())
    spec = arms.parse_config(config, root_name='v9-p2-a')
    assert spec.arm.name == 'mech-static' and spec.repeat == 1 and spec.temperature == 0 and spec.top_p is None
    assert 'top_p' not in config and spec.order_seed == 'v9-officeqa-pro133-order'
    assert spec.subset.sha256 == listing['sha256'] and spec.subset.task_ids == tuple(listing['task_ids'])
    assert spec.barrier.roots == ('v9-p2-a',) and Path(config['barrier']['dir']) == parent.resolve() / 'v9-p2-barrier'
    assert spec.barrier.timeout_s == 43200 and not (parent / 'v9-p2-barrier').exists()
    assert config['framework'] == str(framework) and config['data_cache'] == str(cache)
    assert not arms.revealing_fragments('v9-p2-a', spec.arm)
    assert CP.sha(root / 'PREREG.md') == CP.sha(prereg) and CP.revision(root / 'adapters/mech-w0') == PM.P2_W0_REVISION
    assert not (root / 'prepared').exists() and not (root / 'preflight').exists()
    probe = subprocess.run([PY, '-B', '-c', 'import json, experiment as e; print(json.dumps([e.ARM.name, e.TEMPERATURE, '
                            'e.TOP_P, e.CAMPAIGN.subset.sha256, e.CAMPAIGN.barrier.roots]))'],
                           cwd=root, capture_output=True, text=True, timeout=60)
    assert json.loads(probe.stdout) == ['mech-static', 0, None, listing['sha256'], ['v9-p2-a']], probe.stderr
    with pytest.raises(ValueError, match='created once'):
        run.pilot2_root(parent, **options)
    with pytest.raises(ValueError, match='not inside a campaign root'):
        run.pilot2_root(tmp_path / 'elsewhere', source=root, framework=framework, data_cache=cache)
