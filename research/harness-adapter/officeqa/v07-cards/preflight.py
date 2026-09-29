"""Readiness checks for the cards campaign: offline checks, then the paid synthetic smoke.

Writes preflight/readiness.json, which run.py requires before a formal campaign.
The offline part makes no model call; the smoke costs cents.
"""
import getpass
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import campaign as c
import acting
import data
import experiment
from paths import REPOSITORY

ROOT = c.ROOT
SOURCES = tuple(p.name for p in sorted(ROOT.glob('*.py'))) + ('PROTOCOL.md', '.task-paths.json', 'cards_domain.md')


def offline():
    checks = {}
    checks['profile'] = experiment.PROFILE == 'full_cards_unbounded' and experiment.ARCHITECTURE == 'cards' and experiment.FULL
    checks['data'] = data.verify_prepared(ROOT)['verified']
    c.save(ROOT / 'preflight/data-verification.json', data.verify_prepared(ROOT))
    tests = subprocess.run([sys.executable, '-m', 'pytest', 'tests/test_cards.py', '-q'], cwd=REPOSITORY, capture_output=True, text=True)
    checks['cards_unit_tests'] = tests.returncode == 0
    checks['cards_unit_tests_output'] = tests.stdout[-400:]
    from harnessadapter.adapter.loader import Adapter
    context = Adapter(c.W0).context()
    checks['w0_renders_empty_deck'] = context.startswith('# Learned cards') and 'No cards have been admitted yet' in context
    checks['w0_has_no_tools'] = Adapter(c.W0).tools == {}
    import cards_learning
    checks['calc'] = cards_learning.calc('(35810/131973)**(1/8)-1')['value'] < -0.15 and cards_learning.calc('round(2/3, 3)')['value'] == 0.667
    try:
        cards_learning.calc('__import__("os")'); checks['calc_rejects_names'] = False
    except ValueError:
        checks['calc_rejects_names'] = True
    released = {1: {'question': 'What is the total?', 'strict': 0, 'status': 'completed'}, 2: {'question': 'Sum the rows.', 'strict': 0, 'status': 'completed'}}
    screen = cards_learning.block.screen({'id': 'C001', 'scope': 'fiscal year', 'claim': 'Use treasury_bulletin_2016_09.txt', 'support': [1, 2]}, [1, 2], released, [], [])
    checks['leak_screen'] = not screen['ok'] and any('corpus file name' in r for r in screen['reasons'])
    # macOS temp directories sit behind a symlink (/var -> /private/var); the actor
    # runner insists on canonical paths, so use a scratch directory inside the run.
    directory = ROOT / 'preflight' / 'offline-workspace'
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)
    try:
        workspace = directory / 'workspace'
        c.prepare_workspace(workspace, c.W0)
        settings = acting.build_settings(workspace, directory / 'out', c.CORPUS, c.INDEX, c.W0, endpoint='http://127.0.0.1:1/v1')
        checks['agents_md_bytes'] = (workspace / 'AGENTS.md').stat().st_size
        checks['agents_md_has_preamble'] = 'Learned cards' in (workspace / 'AGENTS.md').read_text()
        (ROOT / 'preflight' / 'agents-md-w0.md').write_text((workspace / 'AGENTS.md').read_text())
        checks['codex_config_valid'] = acting.validate_config_offline(settings)['passed']
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    checks['codex_version'] = subprocess.run([str(acting.CODEX), '--version'], capture_output=True, text=True).stdout.strip()
    checks['framework_revision'] = c.framework_revision()
    passed = all(v for k, v in checks.items() if isinstance(v, bool))
    return passed, checks


def main():
    passed, checks = offline()
    print(json.dumps({'offline_checks_passed': passed, **checks}, ensure_ascii=False, indent=2))
    if not passed:
        raise SystemExit(1)
    key = os.environ.get('FIREWORKS_API_KEY') or getpass.getpass('Fireworks API key (hidden): ')
    import run as runner
    name = f"native-roundtrip-{1 + len(list((ROOT / 'preflight').glob('native-roundtrip-*'))):02}"
    smoke = runner.smoke(key, name)
    readiness = {'passed': passed and smoke['passed'], 'offline_checks_passed': passed, 'offline_checks': checks,
                 'paid_model_roundtrip_passed': smoke['passed'], 'smoke': name, 'gold_feedback_use_resolved': True,
                 'gold_feedback_resolution': 'Operator explicitly requested completed-batch reference release to the learner (same protocol as v4-v6); not a publisher permission claim.',
                 'source_hashes': {name: c.sha(ROOT / name) for name in SOURCES}, 'created_utc': c.utc()}
    c.save(ROOT / 'preflight/readiness.json', readiness)
    print(json.dumps({'readiness_passed': readiness['passed']}))
    raise SystemExit(0 if readiness['passed'] else 1)


if __name__ == '__main__':
    main()
