"""Readiness checks for one v8 root (no adapter, prompt-only or cards): offline checks,
then the paid synthetic smoke.

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
SOURCES = (tuple(p.name for p in sorted(ROOT.glob('*.py'))) + ('PROTOCOL.md', '.task-paths.json')
           + (('cards_domain.md',) if (ROOT / 'cards_domain.md').is_file() else ()))
V7 = ROOT.parent / 'officeqa-codex50-cards-v7-work'
V6F_PROMPT = ROOT.parent / 'officeqa-codex50-full-prompt-only-v6f-work'


def offline():
    checks = {}
    adapted = 'adapted' in experiment.ARMS
    kind = 'none' if not adapted else experiment.ARCHITECTURE
    checks['kind'] = kind
    checks['profile'] = experiment.FULL and kind in ('none', 'prompt_only', 'cards')
    checks['single_arm'] = len(experiment.ARMS) == 1 and not experiment.LOCKSTEP
    checks['data'] = data.verify_prepared(ROOT)['verified']
    c.save(ROOT / 'preflight/data-verification.json', data.verify_prepared(ROOT))
    tasks = data.load_tasks(ROOT)
    checks['question_count_133'] = len(tasks) == 133
    # The first 50 questions are the v4-v7 questions in the same order.
    earlier = [json.loads(line)['task_id'] for line in (V7 / 'baseline-predictions.jsonl').read_text().splitlines()]
    checks['first_50_match_v7_order'] = [t['task_id'] for t in tasks[:50]] == earlier
    tests = subprocess.run([sys.executable, '-m', 'pytest', 'tests/test_v8_broker.py', '-q', '-p', 'no:cacheprovider'],
                           cwd=ROOT, capture_output=True, text=True)
    checks['broker_unit_tests'] = tests.returncode == 0
    checks['broker_unit_tests_output'] = tests.stdout[-300:]
    if kind == 'cards':
        tests = subprocess.run([sys.executable, '-m', 'pytest', 'tests/test_cards.py', '-q', '-p', 'no:cacheprovider'],
                               cwd=REPOSITORY, capture_output=True, text=True)
        checks['cards_unit_tests'] = tests.returncode == 0
        from harnessadapter.adapter.loader import Adapter
        context = Adapter(c.W0).context()
        checks['w0_renders_empty_deck'] = context.startswith('# Learned cards') and 'No cards have been admitted yet' in context
        checks['w0_has_no_tools'] = Adapter(c.W0).tools == {}
        # Same frozen framework as the v7 cards campaign.
        frozen = c.read(V7 / 'freeze.json')
        checks['framework_equals_v7'] = (c.framework_revision() == frozen['framework_revision']
                                         and c.snapshot(REPOSITORY / 'harnessadapter') == frozen['framework_files'])
        import cards_learning
        checks['calc'] = cards_learning.calc('round(2/3, 3)')['value'] == 0.667
    if kind == 'prompt_only':
        # The prompt-only adapter, learner and reference shelf of v6f, byte for byte.
        frozen = c.read(V6F_PROMPT / 'freeze.json')
        checks['framework_equals_v6f_prompt_only'] = (c.framework_revision() == frozen['framework_revision']
                                                      and c.snapshot(REPOSITORY / 'harnessadapter') == frozen['framework_files'])
        checks['w0_equals_v6f_prompt_only'] = c.snapshot(c.W0) == frozen['generic_W0']
        checks['shelf_equals_v6f_prompt_only'] = c.shelf_snapshot() == frozen['reference_shelf']
        checks['learner_equals_v6f_prompt_only'] = all(
            c.sha(ROOT / name) == frozen['files'][name] for name in ('full_learning.py', 'full_sandbox.py', 'learning.py', 'postmortem.py'))
        import full_learning  # noqa: F401  (import must succeed)
        checks['full_learning_imports'] = True
    if kind == 'none':
        checks['no_shelf'] = c.shelf_snapshot() is None
    directory = ROOT / 'preflight' / 'offline-workspace'
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)
    try:
        adapter = c.W0 if adapted else None
        workspace = directory / 'workspace'
        c.prepare_workspace(workspace, adapter)
        settings = acting.build_settings(workspace, directory / 'out', c.CORPUS, c.INDEX, adapter, endpoint='http://127.0.0.1:1/v1')
        checks['agents_md_bytes'] = (workspace / 'AGENTS.md').stat().st_size
        checks['no_mwm_runtime_in_workspace'] = not (workspace / '.codex/mwm-runtime.py').exists()
        (ROOT / 'preflight' / 'agents-md-first-question.md').write_text((workspace / 'AGENTS.md').read_text())
        checks['codex_config_valid'] = acting.validate_config_offline(settings)['passed']
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    checks['codex_version'] = subprocess.run([str(acting.CODEX), '--version'], capture_output=True, text=True).stdout.strip()
    # The learner's PolicySandbox resolves `codex` through PATH; campaign.py pins it to the actors' binary.
    checks['learner_finds_pinned_codex'] = Path(shutil.which('codex') or '').resolve() == Path(acting.CODEX).resolve()
    checks['codex_sandbox_has_P'] = ' -P' in subprocess.run([str(acting.CODEX), 'sandbox', '--help'], capture_output=True, text=True).stdout
    checks['frameworks_are_private_copies'] = 'frameworks-v8' in str(REPOSITORY) and 'frameworks-v8' in str(acting.CODEX)
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
                 'gold_feedback_resolution': 'Operator explicitly requested completed-batch reference release to the learner (same protocol as v4-v7); not a publisher permission claim.',
                 'source_hashes': {name: c.sha(ROOT / name) for name in SOURCES}, 'created_utc': c.utc()}
    c.save(ROOT / 'preflight/readiness.json', readiness)
    print(json.dumps({'readiness_passed': readiness['passed']}))
    raise SystemExit(0 if readiness['passed'] else 1)


if __name__ == '__main__':
    main()
