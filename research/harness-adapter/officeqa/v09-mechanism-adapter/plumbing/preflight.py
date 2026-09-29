"""Readiness checks for one v9 root: offline checks ($0), then the paid synthetic smoke (cents).

Writes preflight/readiness.json, which run.py requires before a formal campaign. `--offline-only` stops
after the $0 part (no key is asked for). Offline checks (DESIGN §4, §5; engineering review #5):
- the arm spec parses and validates (arms.py); the root name is in its barrier group;
- prepared data verify (hashes, 133 tasks, corpus, FTS index, issue-47 list) and the tables index
  indexes exactly this corpus (its recorded corpus hash equals the prepared corpus manifest's);
- the frozen actor runtime passes the deep manifest check (kit.verify_frozen(deep=True));
- the base order equals the v8 order (read-only v8 manifest, when present); this repeat's permutation is
  registered in the barrier group's order.json (every root of the repeat must agree);
- the arm's W0 equals the generated one and fits the budget; the seed parity numbers of both W0s;
- a real offline workspace for this root's first question (kit, AGENTS.md with its notes, README) and a
  strict Codex config read of its settings (no model call);
- the tier-0/1 test suites: kit, pack, core, broker, learners, orchestration, and the $0 live-Codex tier
  (the pinned binary against a fake upstream on 127.0.0.1);
- learner arms: the learner smoke with the fake model (tests/test_v9_learn*.py, S5);
- PREREG.md hashed; frameworks are the private frameworks-v8 (Codex) / frameworks-v9 (runtime) copies.
- wave 3 (pilot): a declared subset is recorded in the prepared manifest with the same hash, names only
  prepared questions, and sets this root's order (the subset in the repeat's seeded order); the declared
  temperature and top_p (absent = none sent) are the ones the broker sends.
"""
import argparse
from dataclasses import asdict
import getpass
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import arms
import campaign as c
import acting
import data
import experiment
import kit
from paths import ACTOR_PYTHON_PREFIX, CODEX

ROOT = c.ROOT
V8_MANIFEST = ROOT.parent / 'officeqa-pro133-none-v8-work/prepared/manifest.json'
SUITES = {
    'kit': ['tests/test_v9_kit.py'],
    'pack': ['tests/test_officeqa_pack.py', 'tests/test_officeqa_tables.py'],
    'core': ['tests/test_v9_ledger.py', 'tests/test_v9_engine.py', 'tests/test_v9_gate.py', 'tests/test_v9_runner.py',
             'tests/test_v9_replay.py'],
    'broker': ['tests/test_v8_broker.py', 'tests/test_v9_broker.py'],
    'orchestration': ['tests/test_v9_orchestration.py', 'tests/test_v9_pilot.py'],   # wave 3: subset/temperature
}
LEARNER_TESTS = ('tests/test_v9_learn*.py', 'tests/test_v9_evidence*.py', 'tests/test_v9_*learner*.py')
# S8 review: + the $0 end-to-end rehearsal (toy pack, 5 arms, real Codex + broker + learners; ~90 s)
LIVE = ['tests/test_v9_codex_live.py', 'tests/test_v9_kit.py', 'tests/test_v9_orchestration.py', 'tests/test_v9_e2e.py']
PENDING = '[DECISION PENDING'


def source_names():
    return (tuple(p.name for p in sorted(ROOT.glob('*.py'))) + ('PROTOCOL.md', 'PREREG.md', '.task-paths.json')
            + (('cards_domain.md',) if (ROOT / 'cards_domain.md').is_file() else ()))


def source_trees():
    """Revisions of the code trees the freeze covers (harness_core/, packs/, adapters/): a change between
    preflight and the formal run must invalidate readiness, not only a change to a top-level file."""
    return {name: c.revision(ROOT / name) for name in c.CODE_TREES if (ROOT / name).is_dir()}


def pytest(files, *, marker=None):
    files = [f for f in files if (ROOT / f).is_file()]
    if not files:
        return {'passed': False, 'output': 'no test files found'}
    args = [sys.executable, '-B', '-m', 'pytest', '-q', '-p', 'no:cacheprovider', *files]
    if marker:
        args[5:5] = ['-m', marker]
    run = subprocess.run(args, cwd=ROOT, capture_output=True, text=True)
    return {'passed': run.returncode == 0, 'output': run.stdout[-400:], 'files': files, 'marker': marker}


def prereg_pending(path=ROOT / 'PREREG.md'):
    """Decision placeholders still open in PREREG.md (recorded; a confirmatory campaign needs none)."""
    if not path.is_file():
        return ['PREREG.md missing']
    return [line.strip() for line in path.read_text().splitlines() if PENDING in line]


def offline(*, suites=True):
    checks, info = {}, {}
    spec = experiment.CAMPAIGN
    checks['arm_valid'] = spec is not None
    if spec is None:
        return False, checks, {'error': 'no .task-paths.json "arm" in this root'}
    arm = arms.check_arm(spec.arm)
    info['campaign'] = spec.to_json()
    checks['profile_v9'] = experiment.PROFILE == 'v9'
    verification = data.verify_prepared(ROOT, spec=spec)
    c.save(ROOT / 'preflight/data-verification.json', verification)
    checks['data'] = verification['verified'] is True
    checks['tables_index_matches_corpus'] = bool(verification['tables']['corpus_manifest_sha256'])
    tasks = data.load_tasks(ROOT)
    checks['question_count_133'] = len(tasks) == 133
    if V8_MANIFEST.is_file():
        checks['base_order_equals_v8'] = [t['task_id'] for t in tasks] == json.loads(V8_MANIFEST.read_text())['task_ids']
    order = arms.campaign_order([t['task_id'] for t in tasks], spec)
    info['order_sha256'] = arms.order_sha256(order)
    info['question_count'] = len(order)
    if spec.subset is not None:
        # verify_prepared already refused a manifest whose subset differs; recorded here for the operator
        info['subset'] = verification.get('subset')
        checks['subset_hash_matches_manifest'] = (
            (verification.get('subset') or {}).get('sha256') == spec.subset.sha256 == arms.subset_sha256(spec.subset.task_ids)
            and sorted(order) == sorted(spec.subset.task_ids))
    info['temperature'] = spec.temperature
    info['top_p'] = spec.top_p
    if spec.barrier is not None:
        import barrier
        group = barrier.FileBarrier(spec.barrier.dir, spec.barrier.roots, spec.root, timeout_s=spec.barrier.timeout_s)
        group.register_order(spec.repeat, info['order_sha256'], len(order))
        checks['barrier_order_registered'] = True
        checks['barrier_not_aborted'] = group.aborted() is None
    info['frozen_runtime'] = kit.verify_frozen(deep=True)
    checks['frozen_runtime_deep'] = True
    info['seed_parity'] = {name: arms.check_w0(arms.parse_arm(name, pack=spec.pack))['model_visible']
                           for name in ('text-static', 'mech-static')}
    checks['seed_parity_within_budget'] = all(v['within_budget'] for v in info['seed_parity'].values())
    w0 = arms.w0_dir(arm)
    if w0 is not None:
        info['w0'] = arms.check_adapter(w0, arm)
        checks['w0_valid'] = True
    # A real offline workspace for this root's first question.
    directory = ROOT / 'preflight' / 'offline-workspace'
    if directory.exists():
        shutil.rmtree(directory)
    try:
        first = next(t for t in tasks if t['task_id'] == order[0])
        prep = c.prepare_episode(c.Layout(), arm, first, w0, directory / 'episode', ordinal=1, role='preflight')
        workspace = Path(prep['workspace'])
        env_extra, data_roots = c.actor_data(arm, c.Layout())
        settings = acting.build_settings(workspace, directory / 'out', c.CORPUS, c.INDEX, None,
                                         endpoint='http://127.0.0.1:1/v1', arm=arm, env_extra=env_extra, data=data_roots,
                                         permissions_instructions=False)
        info['agents_md_bytes'] = prep['agents_md_bytes']
        info['readme_bytes'] = prep['readme_bytes']
        info['notes_first_question'] = prep['notes']
        (ROOT / 'preflight/agents-md-first-question.md').write_text((workspace / 'AGENTS.md').read_text())
        (ROOT / 'preflight/readme-first-question.md').write_text((workspace / 'README.md').read_text())
        checks['kit_built_for_arm'] = kit.verify_kit(workspace, json.loads(
            (workspace / acting.MARKER).read_text())['kit']) is not None
        checks['codex_config_valid'] = acting.validate_config_offline(settings)['passed']
        checks['settings_v9'] = (settings.get('features.multi_agent') is False and settings.get('agents.enabled') is False
                                 and settings.get('model_auto_compact_token_limit') == 244800
                                 and settings.get('include_permissions_instructions') is False)
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    checks['temperature_sent_equals_declared'] = (c.broker_temperature(spec.temperature) == spec.temperature
                                                  == experiment.TEMPERATURE)
    checks['top_p_sent_equals_declared'] = c.broker_top_p(spec.top_p) == spec.top_p == experiment.TOP_P
    root_fragments = arms.revealing_fragments(ROOT.name, arm)
    checks['root_name_reveals_no_arm'] = not root_fragments
    info['codex_version'] = subprocess.run([str(CODEX), '--version'], capture_output=True, text=True).stdout.strip()
    checks['frameworks_are_private_copies'] = ('frameworks-v8' in str(CODEX) and 'frameworks-v9' in str(ACTOR_PYTHON_PREFIX))
    checks['prereg_present'] = (ROOT / 'PREREG.md').is_file()
    info['prereg_sha256'] = c.sha(ROOT / 'PREREG.md') if checks['prereg_present'] else None
    info['prereg_pending_decisions'] = prereg_pending()
    if suites:
        results = {name: pytest(files) for name, files in SUITES.items()}
        learners = sorted({str(p.relative_to(ROOT)) for pattern in LEARNER_TESTS for p in ROOT.glob(pattern)})
        results['learners'] = pytest(learners)
        results['live_codex'] = pytest(LIVE, marker='live_codex')
        info['suites'] = results
        for name, value in results.items():
            checks[f'suite_{name}'] = value['passed']
        if arm.learner is not None:
            checks['learner_smoke_fake_model'] = results['learners']['passed'] and bool(learners)
    passed = all(v for v in checks.values() if isinstance(v, bool))
    return passed, checks, info


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--offline-only', action='store_true', help='$0: skip the paid smoke; no readiness file')
    parser.add_argument('--no-suites', action='store_true', help='skip the pytest suites (debugging only)')
    args = parser.parse_args()
    passed, checks, info = offline(suites=not args.no_suites)
    print(json.dumps({'offline_checks_passed': passed, **checks}, ensure_ascii=False, indent=2))
    c.save(ROOT / 'preflight/offline-checks.json', {'passed': passed, 'checks': checks, 'info': info, 'utc': c.utc()})
    if not passed or args.offline_only or args.no_suites:
        raise SystemExit(0 if passed else 1)
    key = os.environ.get('FIREWORKS_API_KEY') or getpass.getpass('Fireworks API key (hidden): ')
    import run as runner
    name = f"smoke-{1 + len(list((ROOT / 'preflight').glob('smoke-*'))):02}"
    smoke = runner.smoke(key, name)
    readiness = {'passed': passed and smoke['passed'], 'offline_checks_passed': passed, 'offline_checks': checks,
                 'paid_model_roundtrip_passed': smoke['passed'], 'smoke': name, 'campaign': info['campaign'],
                 'order_sha256': info['order_sha256'], 'seed_parity': info['seed_parity'],
                 'question_count': info['question_count'], 'subset': info.get('subset'),
                 'temperature': info['temperature'], 'top_p': info['top_p'],
                 'prereg_sha256': info['prereg_sha256'], 'prereg_pending_decisions': info['prereg_pending_decisions'],
                 'gold_feedback_use_resolved': True,
                 'gold_feedback_resolution': 'Operator explicitly requested completed-block reference release to the learner arms (same protocol as v4-v8); issue-47 references withheld; not a publisher permission claim.',
                 'source_hashes': {n: c.sha(ROOT / n) for n in source_names() if (ROOT / n).is_file()},
                 'source_trees': source_trees(),
                 'created_utc': c.utc()}
    c.save(ROOT / 'preflight/readiness.json', readiness)
    print(json.dumps({'readiness_passed': readiness['passed']}))
    raise SystemExit(0 if readiness['passed'] else 1)


if __name__ == '__main__':
    main()
