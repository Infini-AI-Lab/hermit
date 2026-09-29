"""v10 root configuration tests ($0, offline; no real reference answer or grader output is read): the subset,
temperature and top_p keys of `.task-paths.json`, salvaged from the deleted v9 pilot tests
(tests/test_v9_pilot.py of the v9 tree) for the v10 arm names.

- subset: spec parsing and hashing, order = the subset in the repeat's seeded order, a prompt-evolve campaign on
  a subset (blocks of 5, learning, seal, grade, report, audit), refusals on a subset mismatch;
- temperature / top_p: the spec's value reaches the freeze, every invocation and every request body (fake
  broker path on a prompt-static campaign; the real broker against a fake upstream), the audit checks each,
  resume refuses a change; absent top_p = no top_p key anywhere;
- preflight offline (`preflight.py --offline-only --no-suites`) on a synthetic 133-task root with a subset and
  a temperature + top_p cell: the subset hash, the order, the declared sampling, both W0 checks, the ToolRunner
  smoke over the synthetic corpus and the readiness fields.
The campaign helpers (synthetic prepared roots, the fake broker / actor, hooks, the audit summary) come from
tests/test_v10_orchestration.py.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

import arms  # noqa: E402
import broker  # noqa: E402
import campaign as CP  # noqa: E402
import data  # noqa: E402
import fake_upstream as fu  # noqa: E402
from codexsim import CodexSim  # noqa: E402
import test_v10_broker as TB  # noqa: E402   (helpers only; module-qualified so pytest does not re-collect)
import test_v10_orchestration as TO  # noqa: E402

PY = sys.executable


@pytest.fixture(scope='session')
def code(tmp_path_factory):
    return TO.code_copy(tmp_path_factory)


def subset_config(ids, name='pilot-test-7'):
    return {'name': name, 'task_ids': list(ids), 'sha256': arms.subset_sha256(ids)}


def spec_with(arm='prompt-evolve', *, subset=None, temperature=None, root='root', barrier=None, top_p=None):
    config = {'profile': arms.PROFILE, 'arm': arm, 'repeat': 1, 'pack': 'officeqa', 'order_seed': 'test-order-seed'}
    if subset is not None:
        config['subset'] = subset
    if temperature is not None:
        config['temperature'] = temperature
    if top_p is not None:
        config['top_p'] = top_p
    if barrier is not None:
        config['barrier'] = barrier
    return arms.parse_config(config, root_name=root)


def all_ids(n):
    return TO.TASK_IDS + [f'UIDX{i:03}' for i in range(n - len(TO.TASK_IDS))]


def answer_by_id(n):
    ids, questions = all_ids(n), TO.QUESTIONS + [f'What was synthetic amount number {i} in fiscal year 2001?'
                                                for i in range(n - len(TO.TASK_IDS))]
    by_question = dict(zip(questions, ids))
    return lambda question, workspace: 'A-' + by_question[question] if by_question[question] != 'UIDX001' else 'wrong'

# ---------------------------------------------------------------------------------------------
# Spec: subset + temperature + top_p
# ---------------------------------------------------------------------------------------------


def test_subset_and_temperature_parse_hash_and_record():
    ids = ['UIDT005', 'UID0004', 'UIDT002']
    spec = spec_with(subset=subset_config(ids), temperature=0.6)
    assert spec.subset.task_ids == tuple(ids) and spec.subset.sha256 == arms.subset_sha256(ids)
    assert spec.temperature == 0.6 and spec.to_json()['temperature'] == 0.6
    assert spec.to_json()['subset'] == subset_config(ids)
    # a full run at the default temperature records exactly the base spec keys
    full = spec_with()
    assert set(full.to_json()) == {'arm', 'repeat', 'pack', 'order_seed', 'root', 'barrier'}
    assert full.subset is None and full.temperature == 0 and spec_with(temperature=0).to_json() == full.to_json()
    assert arms.subset_sha256(ids) == hashlib.sha256(json.dumps(ids, separators=(',', ':')).encode()).hexdigest()
    bad = [({**subset_config(ids), 'sha256': arms.subset_sha256(ids[::-1])}, 'sha256'),
           ({**subset_config(ids), 'task_ids': ids + [ids[0]]}, 'distinct'),
           ({**subset_config(ids), 'name': 'Bad Name'}, 'subset.name'),
           ({**subset_config(ids), 'extra': 1}, 'exactly'),
           ({'name': 'x', 'task_ids': [], 'sha256': arms.subset_sha256([])}, 'non-empty')]
    for value, message in bad:
        with pytest.raises(ValueError, match=message):
            spec_with(subset=value)
    for value in (-0.1, 2.5, True, '0.7', None, float('nan')):          # an explicit null is refused too
        with pytest.raises(ValueError, match='temperature'):
            arms.parse_config({'arm': 'plain', 'repeat': 1, 'order_seed': 's', 'temperature': value}, root_name='r1')


def test_top_p_parses_records_only_when_declared():
    spec = spec_with(temperature=1.0, top_p=0.95)
    assert (spec.temperature, spec.top_p) == (1.0, 0.95)
    assert spec.to_json()['top_p'] == 0.95 and spec.to_json()['temperature'] == 1.0
    full = spec_with()
    assert full.top_p is None and arms.DEFAULT_TOP_P is None and 'top_p' not in full.to_json()
    assert spec_with(top_p=1).top_p == 1
    for value in (0, -0.1, 1.5, True, '0.95', None, float('nan')):     # absent is the only way to send none
        with pytest.raises(ValueError, match='top_p'):
            arms.parse_config({'arm': 'plain', 'repeat': 1, 'order_seed': 's', 'top_p': value}, root_name='r1')


def test_subset_order_is_the_full_order_without_the_other_questions():
    base = [f'UID{i:04}' for i in range(1, 134)]
    chosen = base[2::6][:20]
    spec = replace(spec_with(), subset=arms.parse_subset(subset_config(chosen, 'pilot-20')))
    order = arms.campaign_order(base, spec)
    full = arms.question_order(base, spec.order_seed, spec.repeat)
    assert order == [t for t in full if t in set(chosen)] and len(order) == 20
    assert [list(b) for b in arms.blocks_of(len(order), 5)] == [[1, 2, 3, 4, 5], [6, 7, 8, 9, 10],
                                                                 [11, 12, 13, 14, 15], [16, 17, 18, 19, 20]]
    with pytest.raises(ValueError, match='not prepared'):
        arms.selected_ids(base[:10], spec.subset)


def test_study_subset_file_is_the_first_50_of_the_canonical_order():
    """run.py roots declares pilot/first50.json: 50 distinct ids whose sha256 is the list's (arms.parse_subset)."""
    import run as RUN
    subset = RUN.load_subset(ROOT / RUN.SUBSET_FILE)
    listing = json.loads((ROOT / RUN.SUBSET_FILE).read_text())
    assert subset.name == 'pilot-first50' and len(subset.task_ids) == 50 == len(set(subset.task_ids))
    assert subset.sha256 == arms.subset_sha256(subset.task_ids) == listing['sha256']
    assert subset.sha256.startswith('c95f9613') and subset.sha256.endswith('a07d')
    assert sum(1 for t in subset.task_ids if t in data.ISSUE_47) == 5          # scored, withheld from the learners

# ---------------------------------------------------------------------------------------------
# Subset campaign (fake broker/actor, real campaign/seal/grade/report/audit)
# ---------------------------------------------------------------------------------------------

SUBSET_IDS = ['UIDT002', 'UID0004', 'UIDT005', 'UIDX000', 'UIDX001', 'UIDX003', 'UIDX004']   # 1 issue-47 id


def make_subset_root(tmp_path, code, arm='prompt-evolve', *, n=12, ids=SUBSET_IDS, temperature=None, name='root'):
    root = tmp_path / name
    root.mkdir()
    spec = spec_with(arm, subset=subset_config(ids), temperature=temperature, root=name)
    TO.make_prepared(root, spec, n=n)
    return root, spec, CP.Layout(root=root, code=code)


def test_subset_campaign_learns_in_blocks_seals_grades_reports_and_audits(tmp_path, code):
    root, spec, lay = make_subset_root(tmp_path, code)
    manifest = json.loads((root / 'prepared/manifest.json').read_text())
    assert manifest['subset'] == subset_config(SUBSET_IDS) and manifest['sample_count'] == 12
    assert manifest['grading_policy']['subsets'] == {'all': 7, 'undisputed': 6}
    assert data.selected_task_ids(manifest) == [t for t in manifest['task_ids'] if t in SUBSET_IDS]
    record, calls = [], []
    state = TO.run_campaign(lay, spec, TO.hooks(lay, spec.arm, learn=TO.fake_learner(record), calls=calls,
                                                answer_for=answer_by_id(12)))
    order = arms.campaign_order(manifest['task_ids'], spec)
    full = arms.question_order(manifest['task_ids'], spec.order_seed, spec.repeat)
    assert order == [t for t in full if t in SUBSET_IDS]
    assert state['status'] == 'completed' and [r['task_id'] for r in state['results']] == order
    assert state['question_count'] == 7 and state['subset'] == {'name': 'pilot-test-7', 'sha256': spec.subset.sha256,
                                                                 'count': 7}
    # blocks of 5 over the subset: one learning round (after block 1), released = block 1 minus issue-47
    assert [(r['block'], r['status']) for r in state['learning']] == [(1, 'admitted'), (2, 'skipped_unused')]
    assert record[0]['released'] == sorted(t for t in order[:5] if t not in data.ISSUE_47)
    assert {task for _, task, _ in calls} <= set(SUBSET_IDS)
    seal = json.loads((root / 'private-evaluation/predictions-seal.json').read_text())
    assert seal['total_predictions'] == 7 and seal['subset'] == {'name': 'pilot-test-7',
                                                                 'sha256': spec.subset.sha256, 'count': 7}
    metrics = json.loads((root / 'private-evaluation/paired-metrics.json').read_text())
    assert metrics['denominator'] == 7 and metrics['undisputed']['n'] == 6 and metrics['subset']['count'] == 7
    assert sorted(r['task_id'] for r in metrics['results']) == sorted(SUBSET_IDS)
    assert metrics['arms']['prompt-evolve']['correct'] == 6                # UIDX001 answered 'wrong'
    frozen = json.loads((root / 'freeze.json').read_text())
    assert frozen['campaign']['subset'] == subset_config(SUBSET_IDS) and frozen['order'] == order
    assert TO.failed_names(root, code) == set()
    html = (root / 'report.html').read_text()
    assert '7 questions (subset pilot-test-7)' in html and 'Blocks (prequential' in html
    assert '<td>6/7</td>' in html and not any(q in html for q in TO.QUESTIONS)   # cumulative strict, no question text
    # the audit re-checks the subset against the prepared manifest
    path = root / 'prepared/manifest.json'
    changed = {**manifest, 'subset': subset_config(SUBSET_IDS[:-1] + ['UIDX005'])}
    path.write_text(json.dumps(changed, indent=2) + '\n')
    assert 'subset: hash of the declared ids, recorded in the prepared manifest, run exactly' in TO.failed_names(root, code)
    # a completed root without its grade outputs fails the coverage check (it is never skipped)
    graded = root / 'private-evaluation/paired-metrics.json'
    graded.rename(root / 'paired-metrics.moved')
    assert "seal and grades cover exactly this root's questions" in TO.failed_names(root, code)


def test_subset_mismatch_and_bad_manifest_subset_are_refused(tmp_path, code):
    root, spec, lay = make_subset_root(tmp_path, code)
    other = replace(spec, subset=arms.parse_subset(subset_config(SUBSET_IDS[:5])))
    with pytest.raises(ValueError, match='subset differs'):
        CP.Campaign(TO.KEY, other, lay, hooks=TO.hooks(lay, other.arm)).load(resume=False)
    with pytest.raises(ValueError, match='subset differs'):
        data.check_subset(root, replace(spec, subset=None))
    assert data.check_subset(root, spec) == {'name': 'pilot-test-7', 'sha256': spec.subset.sha256, 'count': 7}
    manifest = json.loads((root / 'prepared/manifest.json').read_text())
    with pytest.raises(ValueError, match='sha256'):
        data.selected_task_ids({**manifest, 'subset': {**manifest['subset'], 'sha256': '0' * 64}})
    with pytest.raises(ValueError, match='not prepared'):
        data.selected_task_ids({**manifest, 'subset': subset_config(['UIDT002', 'NOPE'])})
    # a full root (no subset) seals and grades exactly as before: the full list
    full_root = tmp_path / 'full'
    full_root.mkdir()
    TO.make_prepared(full_root, spec_with('plain', root='full'), n=7)
    assert data.check_subset(full_root, spec_with('plain', root='full')) is None
    assert 'subset' not in json.loads((full_root / 'prepared/manifest.json').read_text())

# ---------------------------------------------------------------------------------------------
# Temperature and top_p through the campaign, the records and the audit
# ---------------------------------------------------------------------------------------------


def test_temperature_campaign_freezes_records_and_audits_it(tmp_path, code):
    root = tmp_path / 'root'
    root.mkdir()
    spec = spec_with('prompt-static', temperature=0.6)
    TO.make_prepared(root, spec)
    lay = CP.Layout(root=root, code=code)
    state = TO.run_campaign(lay, spec, TO.hooks(lay, spec.arm))
    assert state['status'] == 'completed' and state['temperature'] == 0.6
    frozen = json.loads((root / 'freeze.json').read_text())
    assert frozen['campaign']['temperature'] == 0.6
    assert frozen['settings']['temperature'] == frozen['settings']['broker_temperature'] == 0.6
    for run in sorted((root / 'runs/episodes').iterdir()):
        assert json.loads((run / 'records/invocation.json').read_text())['temperature'] == 0.6
        assert json.loads((run / 'api/001/request.json').read_text())['temperature'] == 0.6
    assert TO.failed_names(root, code) == set()
    # a resume under another temperature is refused (spec and settings are frozen)
    with pytest.raises(ValueError, match='changed after the freeze'):
        CP.verify_freeze(lay, frozen, replace(spec, temperature=0), frozen['order'])
    invocation = root / 'runs/episodes/002/records/invocation.json'
    invocation.write_text(json.dumps({**json.loads(invocation.read_text()), 'temperature': 0}))
    request = root / 'runs/episodes/003/api/001/request.json'
    request.write_text(json.dumps({**json.loads(request.read_text()), 'temperature': 0}))
    failed = TO.failed_names(root, code)
    assert 'prompt-static/002 invocation temperature as frozen' in failed
    assert 'prompt-static/003 api/001 temperature as frozen' in failed


def test_real_broker_sends_the_declared_temperature_and_top_p_in_every_body(tmp_path):
    with TB.relay(tmp_path / 'a', lambda body, index: TB.DRAFT, temperature=0.6) as (relay_, up):
        CodexSim(relay_.endpoint).send()
    assert up.bodies[0]['temperature'] == 0.6 and 'top_p' not in up.bodies[0]
    assert TB.api(tmp_path / 'a', 1, 'transport.json')['changes']['temperature'] == 0.6
    with TB.relay(tmp_path / 'b', lambda body, index: TB.DRAFT, temperature=1.0, top_p=0.95) as (relay_, up):
        CodexSim(relay_.endpoint).send()
    assert (up.bodies[0]['temperature'], up.bodies[0]['top_p']) == (1.0, 0.95)
    assert json.loads((tmp_path / 'b/api/001/request.json').read_text())['top_p'] == 0.95
    changes = TB.api(tmp_path / 'b', 1, 'transport.json')['changes']
    assert (changes['temperature'], changes['top_p']) == (1.0, 0.95)
    # default: v8's 0 and no top_p in the body nor in the record
    with TB.relay(tmp_path / 'c', lambda body, index: TB.DRAFT) as (relay_, up):
        CodexSim(relay_.endpoint).send()
    assert up.bodies[0]['temperature'] == 0 and 'top_p' not in up.bodies[0]
    assert 'top_p' not in TB.api(tmp_path / 'c', 1, 'transport.json')['changes']
    assert broker.normalized({'model': broker.MODEL, 'input': []}, None, None) == {
        'model': broker.MODEL, 'input': [], 'store': False, 'temperature': 0}
    for value in (-1, 3, True, '0.5'):
        with pytest.raises(ValueError, match='temperature'):
            broker.Broker(TB.KEY, tmp_path / 'x', temperature=value, **TB.UNBOUNDED)
    for value in (0, -0.5, 1.5, True, '0.9'):
        with pytest.raises(ValueError, match='top_p'):
            broker.Broker(TB.KEY, tmp_path / 'y', top_p=value, **TB.UNBOUNDED)


def test_top_p_campaign_freezes_records_audits_and_refuses_a_mismatch(tmp_path, code, monkeypatch):
    root = tmp_path / 'root'
    root.mkdir()
    spec = spec_with('prompt-static', temperature=1.0, top_p=0.95)
    TO.make_prepared(root, spec)
    lay = CP.Layout(root=root, code=code)
    state = TO.run_campaign(lay, spec, TO.hooks(lay, spec.arm))
    assert state['status'] == 'completed' and (state['temperature'], state['top_p']) == (1.0, 0.95)
    frozen = json.loads((root / 'freeze.json').read_text())
    assert (frozen['campaign']['temperature'], frozen['campaign']['top_p']) == (1.0, 0.95)
    assert frozen['settings']['top_p'] == frozen['settings']['broker_top_p'] == 0.95
    runs = sorted((root / 'runs/episodes').iterdir())
    for run in runs:
        assert json.loads((run / 'records/invocation.json').read_text())['top_p'] == 0.95
        assert json.loads((run / 'api/001/request.json').read_text())['top_p'] == 0.95
    assert TO.failed_names(root, code) == set()
    # a resume under another (or no) top_p is refused: spec and settings are frozen
    for other in (None, 0.9):
        with pytest.raises(ValueError, match='changed after the freeze'):
            CP.verify_freeze(lay, frozen, replace(spec, top_p=other), frozen['order'])
    # the audit checks every invocation and every request body
    invocation = runs[1] / 'records/invocation.json'
    invocation.write_text(json.dumps({**json.loads(invocation.read_text()), 'top_p': None}))
    request = runs[2] / 'api/001/request.json'
    request.write_text(json.dumps({k: v for k, v in json.loads(request.read_text()).items() if k != 'top_p'}))
    failed = TO.failed_names(root, code)
    assert f'prompt-static/{runs[1].name} invocation top_p as frozen' in failed
    assert f'prompt-static/{runs[2].name} api/001 top_p as frozen' in failed
    # a call still in flight at the actor kill (request.json, no transport.json) is checked as well
    unfinished = runs[3] / 'api/001'
    (unfinished / 'transport.json').unlink()
    body = json.loads((unfinished / 'request.json').read_text())
    (unfinished / 'request.json').write_text(json.dumps({**body, 'top_p': 0.9, 'temperature': 0}))
    failed = TO.failed_names(root, code)
    assert {f'prompt-static/{runs[3].name} api/001 top_p as frozen',
            f'prompt-static/{runs[3].name} api/001 temperature as frozen'} <= failed
    # a root without top_p: frozen null, and a body that carries a top_p fails the audit
    plain_root = tmp_path / 'bare'
    plain_root.mkdir()
    plain = spec_with('plain', root='bare')
    TO.make_prepared(plain_root, plain)
    plain_lay = CP.Layout(root=plain_root, code=code)
    TO.run_campaign(plain_lay, plain, TO.hooks(plain_lay, plain.arm))
    frozen = json.loads((plain_root / 'freeze.json').read_text())
    assert 'top_p' not in frozen['campaign'] and frozen['settings']['top_p'] is frozen['settings']['broker_top_p'] is None
    assert all(json.loads((r / 'records/invocation.json').read_text())['top_p'] is None
               for r in (plain_root / 'runs/episodes').iterdir())
    assert TO.failed_names(plain_root, code) == set()
    request = plain_root / 'runs/episodes/001/api/001/request.json'
    request.write_text(json.dumps({**json.loads(request.read_text()), 'top_p': 0.95}))
    assert 'plain/001 api/001 top_p as frozen' in TO.failed_names(plain_root, code)
    # the campaign refuses to start when the broker would not send the declared top_p
    fresh = tmp_path / 'fresh'
    fresh.mkdir()
    TO.make_prepared(fresh, spec)
    fresh_lay = CP.Layout(root=fresh, code=code)
    real = broker.normalized
    monkeypatch.setattr(broker, 'normalized', lambda payload, remaining, max_response=65536, temperature=0, top_p=None:
                        real(payload, remaining, max_response, temperature))          # ignores its top_p option
    with pytest.raises(ValueError, match='top_p 0.95 but the broker sends None'):
        CP.Campaign(TO.KEY, spec, fresh_lay, hooks=TO.hooks(fresh_lay, spec.arm)).load(resume=False)
    assert not (fresh / 'freeze.json').exists()

# ---------------------------------------------------------------------------------------------
# preflight --offline-only on a subset root with a temperature + top_p cell
# ---------------------------------------------------------------------------------------------


def test_preflight_offline_on_a_subset_root_with_a_temperature_cell(tmp_path_factory, tmp_path):
    import paths
    copy = TO.code_copy(tmp_path_factory)
    ids = [f'UIDX{i:03}' for i in range(3, 126, 6)][:20]
    config = {'profile': arms.PROFILE, 'arm': 'prompt-static', 'repeat': 1, 'pack': 'officeqa', 'order_seed': 'pf-seed',
              'temperature': 1.0, 'top_p': 0.95, 'subset': subset_config(ids, 'pilot-first50'),
              'barrier': {'dir': str(tmp_path / 'barrier'), 'roots': [copy.name], 'timeout_s': 60},
              'codex': str(paths.CODEX), 'frozen_runtime': str(paths.FROZEN)}
    (copy / '.task-paths.json').write_text(json.dumps(config))
    spec = arms.parse_config(config, root_name=copy.name)
    manifest = TO.make_prepared(copy, spec, n=133)
    shutil.copyfile(ROOT / 'vendor/officeqa/reward.py', copy / 'prepared/private/reward.py')
    run = subprocess.run([PY, '-B', 'preflight.py', '--offline-only', '--no-suites'], cwd=copy,
                         capture_output=True, text=True, timeout=600)
    assert run.returncode == 0, run.stdout[-3000:] + run.stderr[-3000:]
    result = json.loads((copy / 'preflight/offline-checks.json').read_text())
    checks, info = result['checks'], result['info']
    assert result['passed'] and checks['profile_v10'] and checks['subset_hash_matches_manifest']
    assert checks['temperature_sent_equals_declared'] and checks['top_p_sent_equals_declared']
    assert (info['temperature'], info['top_p']) == (1.0, 0.95)
    assert checks['question_count_133'] and info['question_count'] == 20
    order = arms.campaign_order(manifest['task_ids'], spec)
    assert info['order_sha256'] == arms.order_sha256(order) and info['subset']['count'] == 20
    assert json.loads((tmp_path / 'barrier/order.json').read_text())['order_sha256'] == info['order_sha256']
    # v10 readiness fields: both W0s, the parity numbers, the ToolRunner smoke, W0 idempotence, submit iff gate
    assert checks['w0_prompt_valid'] and checks['w0_harness_valid'] and checks['seed_parity_within_budget'] and checks['w0_valid']
    assert set(info['seed_parity']['bytes']) == {'harness', 'prompt'} and set(info['model_visible']) == {'text', 'harness'}
    assert checks['tool_runner_smoke'] and info['tool_runner_smoke']['runner'] == 'ToolRunner'
    assert set(info['tool_runner_smoke']['tools']) == {'find_tables', 'printings', 'show_table'}
    assert set(info['tool_runner_smoke']['skipped']) == {'calc'}
    assert checks['w0_idempotence'] is True and info['w0_idempotence']['status'] == 'passed'
    assert info['w0_idempotence']['screens'] == ['screen_staged'] and info['w0_idempotence']['findings'] == {}
    assert set(info['w0_idempotence']['files']) == {'tools/USAGE.md', 'tools/cli.py', 'tools/manifest.json', 'tools/otables.py'}
    assert checks['prereg_present'] and checks['prereg_is_v10'] and info['prereg_sha256']
    assert checks['submit_iff_gate'] and checks['kit_built_for_arm'] and 'code_root_neutral' not in checks   # prompt arm: no tools
    assert (copy / 'preflight/readme-first-question.md').read_text().count('# Submitting your answer') == 0
    # a manifest prepared for another subset fails the data verification
    (copy / 'prepared/manifest.json').chmod(0o644)
    path = copy / 'prepared/manifest.json'
    path.write_text(json.dumps({**manifest, 'subset': subset_config(ids[:19], 'pilot-first50')}))
    run = subprocess.run([PY, '-B', 'preflight.py', '--offline-only', '--no-suites'], cwd=copy,
                         capture_output=True, text=True, timeout=600)
    assert run.returncode != 0 and 'subset differs' in run.stderr


def test_preflight_offline_on_a_harness_root_records_the_neutral_code_root(tmp_path_factory, tmp_path):
    """The offline workspace of a harness arm's first question serves the W0 tools from
    <root>/runs/code/<sha12>/tools (no submit wrapper, no submit README section: the gate is off)."""
    import paths
    copy = TO.code_copy(tmp_path_factory)
    config = {'profile': arms.PROFILE, 'arm': 'harness-static', 'repeat': 1, 'pack': 'officeqa', 'order_seed': 'pf-seed',
              'codex': str(paths.CODEX), 'frozen_runtime': str(paths.FROZEN)}
    (copy / '.task-paths.json').write_text(json.dumps(config))
    spec = arms.parse_config(config, root_name=copy.name)
    TO.make_prepared(copy, spec, n=133)
    shutil.copyfile(ROOT / 'vendor/officeqa/reward.py', copy / 'prepared/private/reward.py')
    run = subprocess.run([PY, '-B', 'preflight.py', '--offline-only', '--no-suites'], cwd=copy,
                         capture_output=True, text=True, timeout=600)
    assert run.returncode == 0, run.stdout[-3000:] + run.stderr[-3000:]
    result = json.loads((copy / 'preflight/offline-checks.json').read_text())
    checks = result['checks']
    assert result['passed'] and checks['code_root_neutral'] and checks['submit_iff_gate'] and checks['tool_runner_smoke']
    readme = (copy / 'preflight/readme-first-question.md').read_text()
    assert CP.TOOLS_HEADING + '\n' in readme and '# Submitting your answer' not in readme
    staged = sorted((copy / 'runs/code').iterdir())
    assert len(staged) == 1 and staged[0].name == arms.revision(copy / 'adapters/harness-w0')
    assert sorted(p.name for p in (staged[0] / 'tools').iterdir()) == ['USAGE.md', 'calc.py', 'cli.py', 'manifest.json', 'otables.py']


def test_prereg_must_be_a_v10_pre_registration(tmp_path):
    """A v10 root is never hashed against the v9 tree's PREREG.md: the file must exist and its first heading
    must name v10 (the v9 copy is not in this tree; a stray copy fails readiness)."""
    import preflight
    v9 = tmp_path / 'v9.md'
    v9.write_text('# v9 pre-registration — OfficeQA Pro 133 (development set)\n\nStatus: draft\n')
    v10 = tmp_path / 'v10.md'
    v10.write_text('\n# v10 pre-registration — round 1\n')
    assert not preflight.prereg_is_v10(v9) and preflight.prereg_is_v10(v10) and not preflight.prereg_is_v10(tmp_path / 'none.md')
    assert not (ROOT / 'PREREG.md').is_file() or preflight.prereg_is_v10(ROOT / 'PREREG.md')
