#!/usr/bin/env python3
"""OfficeQA task entry point. Only `run` starts paid model inference."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import getpass
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

TASK = Path(__file__).resolve().parent
FRAMEWORK = TASK.parents[1]


def codex_path():
    bundled = Path('/Applications/ChatGPT.app/Contents/Resources/codex')
    return Path(os.environ.get('OFFICEQA_CODEX') or
                (str(bundled) if bundled.is_file() else shutil.which('codex') or 'codex')).absolute()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fingerprint(root):
    return {p.relative_to(root).as_posix(): sha(p) for p in sorted(root.rglob('*'))
            if p.is_file() and not any(x in {'.git', '__pycache__'} for x in p.relative_to(root).parts)}


def implementation():
    return {name: fingerprint(TASK / name) for name in ('host', 'tests', 'deployments', 'vendor', 'execution')} | {
        'framework': fingerprint(FRAMEWORK / 'harnessadapter'),
        'protocol': sha(TASK / 'PROTOCOL.md'), 'full_protocol':sha(TASK/'PROTOCOL-FULL-MWM.md'), 'launcher': sha(__file__)}


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + '\n')


def runtime_environment(root):
    env = dict(os.environ)
    env['PYTHONPATH'] = os.pathsep.join((str(root), str(FRAMEWORK)))
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    config = root / '.task-paths.json'
    codex = Path(json.loads(config.read_text())['codex']) if config.is_file() else codex_path()
    # Native framework sandbox discovery uses PATH; pin it to the same CLI as actors.
    env['PATH'] = str(codex.parent) + os.pathsep + env.get('PATH', '')
    env['OFFICEQA_CODEX'] = str(codex)
    return env


def execute(root, script, *args, env=None):
    subprocess.run([sys.executable, '-B', str(root / script), *map(str, args)],
                   cwd=root, env=env or runtime_environment(root), check=True)


def check(native=False):
    if native and sys.platform != 'darwin':
        raise ValueError('This historical actor sandbox uses macOS Seatbelt; native checks require macOS')
    receipt = TASK / '.verification/checks.json'
    env = runtime_environment(TASK / 'host')
    env['OFFICEQA_RUN_NATIVE_ACTOR_TEST'] = '1' if native else '0'
    env['OFFICEQA_RUN_NATIVE_LEARNER_TEST'] = '1' if native else '0'
    if native:
        env['OFFICEQA_DOCKER_HOST'] = subprocess.check_output(
            ['docker','context','inspect','--format','{{.Endpoints.docker.Host}}'],text=True).strip()
    command = [sys.executable, '-B', str(TASK / 'tests/run_checks.py'), str(receipt)]
    result = subprocess.run(command, cwd=TASK, env=env)
    data = json.loads(receipt.read_text()) if receipt.exists() else {'passed': False}
    data.update(passed=result.returncode == 0 and data.get('passed', False),
                native=native, implementation=implementation(),
                created_utc=datetime.now(timezone.utc).isoformat())
    if native:
        data['codex_sha256'] = sha(codex_path())
    save(receipt, data)
    if not data['passed']:
        raise RuntimeError('Offline checks failed; see the test output')
    print(json.dumps({k: data[k] for k in ('passed', 'native', 'tests', 'skipped')}, indent=2))


def profile_layout(profile):
    full = str(profile).startswith('full_')
    return {'protocol': 'PROTOCOL-FULL-MWM.md' if full else 'PROTOCOL.md',
            'deployment': {'full_mwm_unbounded': 'officeqa-full',
                           'full_prompt_only_unbounded': 'officeqa-prompt-only'}.get(profile, 'officeqa'),
            'docker': profile == 'full_mwm_unbounded'}


def shelf_hashes(shelf):
    return {p.name: sha(p) for p in sorted(shelf.iterdir()) if p.is_file()}


def prepare(root, cache, codex, profile='memory_bounded', shelf=None):
    root, cache, codex = root.resolve(), cache.resolve(), codex.absolute()
    if root.exists():
        raise ValueError('Use a new run directory; existing trials are never overwritten')
    if not (cache / 'download_receipt.json').is_file():
        raise ValueError('A pinned official download receipt is required; use download or provide your existing cache')
    if not codex.is_file():
        raise ValueError('Codex CLI not found; supply --codex /absolute/path/to/codex')
    if root == cache or root.is_relative_to(cache) or cache.is_relative_to(root):
        raise ValueError('Run directory and private download cache must be separate')
    if shelf is not None and not shelf.is_dir():
        raise ValueError('--shelf must be a directory of extracted *.txt/*.md reference text')
    layout = profile_layout(profile)
    root.mkdir(parents=True)
    for path in (TASK / 'host').glob('*.py'):
        shutil.copy2(path, root / path.name)
    shutil.copy2(TASK / layout['protocol'], root / 'PROTOCOL.md')
    shutil.copytree(TASK / 'vendor', root / 'vendor')
    shutil.copytree(TASK / 'deployments' / layout['deployment'], root / 'deployment')
    config={'framework':str(FRAMEWORK),'data_cache':str(cache),'codex':str(codex),'profile':profile}
    if shelf is not None:
        # Learner-only reference text; actors never receive this directory.
        target = root / 'reference-shelf'
        target.mkdir()
        for path in sorted(shelf.iterdir()):
            if path.is_file() and path.suffix in ('.txt', '.md'):
                shutil.copy2(path, target / path.name)
        config['reference_shelf'] = shelf_hashes(target)
    if layout['docker']:
        config['docker_binary']=shutil.which('docker')
        config['docker_host']=subprocess.check_output(['docker','context','inspect','--format','{{.Endpoints.docker.Host}}'],text=True).strip()
        config['execution_image']=subprocess.check_output(['docker','image','inspect','officeqa-mwm-exec:20260921','--format','{{.Id}}'],text=True).strip()
    save(root / '.task-paths.json', config)
    save(root / 'packaging.json', {'implementation': implementation(),
                                  'note': 'Relocated runtime; new runs use the current framework, not historical v4 scores.'})
    execute(root, 'data.py', 'prepare')
    execute(root, 'data.py', 'verify')
    if shelf is not None:
        check_shelf(root)


def check_shelf(root):
    """The learner-only shelf must not carry the benchmark: no task id or question text."""
    tasks = [json.loads(line) for line in (root / 'prepared/agent/tasks.jsonl').read_text().splitlines() if line.strip()]
    needles = [t['task_id'] for t in tasks] + [t['question'].strip() for t in tasks if t.get('question')]
    for path in sorted((root / 'reference-shelf').iterdir()):
        text = path.read_text(errors='replace')
        hits = [n for n in needles if n and n in text]
        if hits:
            raise ValueError(f'Reference shelf file {path.name} contains benchmark material; remove it before preparing')
    save(root / 'preflight/shelf-check.json', {'checked_utc': datetime.now(timezone.utc).isoformat(),
                                              'files': sorted(p.name for p in (root / 'reference-shelf').iterdir()),
                                              'needles': len(needles), 'passed': True})


def paid_run(root, gold_feedback):
    if not gold_feedback:
        raise ValueError('Specify --gold-feedback to select completed-batch answer-supervised learning')
    if sys.platform != 'darwin':
        raise ValueError('macOS is required; do not silently substitute an unenforced folder sandbox')
    if any((root / name).exists() for name in ('freeze.json', 'state.json', 'process.json')):
        raise ValueError('This campaign has already started; use a new run directory')
    checked = json.loads((TASK / '.verification/checks.json').read_text())
    packaged = json.loads((root / 'packaging.json').read_text())
    if not checked.get('passed') or not checked.get('native') or checked['implementation'] != implementation():
        raise ValueError('Run check --native with these exact sources before paid inference')
    if packaged['implementation'] != implementation():
        raise ValueError('Task/framework changed after preparation; prepare a new run')
    for path in (TASK / 'host').glob('*.py'):
        if sha(root / path.name) != sha(path):
            raise ValueError('Prepared runtime was modified: ' + path.name)
    paths = json.loads((root / '.task-paths.json').read_text())
    layout = profile_layout(paths.get('profile'))
    if sha(root / 'PROTOCOL.md') != sha(TASK / layout['protocol']):
        raise ValueError('Prepared protocol changed')
    if fingerprint(root / 'deployment') != fingerprint(TASK / 'deployments' / layout['deployment']):
        raise ValueError('Prepared deployment changed')
    if fingerprint(root / 'vendor') != fingerprint(TASK / 'vendor'):
        raise ValueError('Pinned evaluator changed')
    if 'reference_shelf' in paths and shelf_hashes(root / 'reference-shelf') != paths['reference_shelf']:
        raise ValueError('Prepared reference shelf changed')
    execute(root, 'data.py', 'verify')
    if Path(paths['framework']).resolve() != FRAMEWORK:
        raise ValueError('Runtime framework differs from the checked checkout')
    if sha(paths['codex']) != checked.get('codex_sha256'):
        raise ValueError('Run check --native with OFFICEQA_CODEX set to this runtime binary')
    env = runtime_environment(root)
    key = os.environ.get('FIREWORKS_API_KEY') or getpass.getpass('Fireworks API key (hidden): ')
    if not key or key.strip() != key:
        raise ValueError('Empty credential or unexpected whitespace')
    env['FIREWORKS_API_KEY'] = key
    readiness = {'passed': False, 'offline_checks_passed': True,
                 'gold_feedback_use_resolved': True,
                 'gold_feedback_resolution': 'Operator explicitly selected --gold-feedback; not a publisher permission claim.',
                 'source_hashes': {p.name: sha(p) for p in sorted(root.glob('*.py'))}}
    readiness['source_hashes'].update({name: sha(root / name) for name in ('PROTOCOL.md', '.task-paths.json')})
    save(root / 'preflight/readiness.json', readiness)
    execute(root, 'run.py', 'smoke', '--name', 'native-roundtrip-01', env=env)
    smoke = json.loads((root / 'preflight/native-roundtrip-01/verification.json').read_text())
    if not smoke['passed']:
        raise RuntimeError('Paid synthetic model/tool smoke failed; no benchmark attempt launched')
    readiness.update(passed=True, paid_model_roundtrip_passed=True)
    save(root / 'preflight/readiness.json', readiness)
    execute(root, 'run.py', 'run', env=env)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    commands = p.add_subparsers(dest='command', required=True)
    c = commands.add_parser('check', help='Synthetic tests only; no model requests')
    c.add_argument('--native', action='store_true', help='Include actual OS sandbox and MCP tests')
    c = commands.add_parser('download', help='Download pinned data with an already authorized HF account')
    c.add_argument('--data-cache', type=Path, required=True)
    c.add_argument('--authorized-auth', action='store_true', required=True)
    c = commands.add_parser('prepare', help='Copy a private runtime and prepare exactly the fixed 50 questions')
    c.add_argument('--run-dir', type=Path, required=True)
    c.add_argument('--data-cache', type=Path, required=True)
    c.add_argument('--codex', type=Path, default=codex_path())
    c.add_argument('--profile', choices=('memory_bounded','full_mwm_unbounded','full_prompt_only_unbounded'), default='memory_bounded')
    c.add_argument('--shelf', type=Path, help='Directory of extracted *.txt/*.md reference text copied for the learner only')
    for name in ('run', 'report', 'audit', 'cost'):
        c = commands.add_parser(name)
        c.add_argument('--run-dir', type=Path, required=True)
        if name == 'run':
            c.add_argument('--gold-feedback', action='store_true')
    c = commands.add_parser('export', help='Export answer-free result tables and HTML from a completed run')
    c.add_argument('--run-dir', type=Path, required=True)
    c.add_argument('--output', type=Path, required=True)
    c.add_argument('--framework-revision', required=True, help='Actual framework commit used by that experiment')
    args = p.parse_args()
    if args.command == 'check':
        return check(args.native)
    if args.command == 'download':
        sys.path.insert(0, str(TASK / 'host'))
        import officeqa_prepare
        print(json.dumps(officeqa_prepare.fetch_official(args.data_cache.resolve(), args.authorized_auth)))
        return
    root = args.run_dir.resolve()
    if args.command == 'prepare':
        return prepare(root, args.data_cache, args.codex, args.profile, args.shelf and args.shelf.resolve())
    if args.command == 'run':
        return paid_run(root, args.gold_feedback)
    if args.command == 'export':
        sys.path.insert(0, str(TASK / 'host'))
        from export_results import export
        return export(root, args.output.resolve(), args.framework_revision)
    name=args.command
    if name=='audit' and str(json.loads((root/'.task-paths.json').read_text()).get('profile')).startswith('full_'):
        name='full_audit'
    execute(root, name + '.py', '--root', root)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        # Never print a request, environment or credential in controller errors.
        print(f'OfficeQA: {exc}', file=sys.stderr)
        raise SystemExit(1)
