"""Fresh train/none/root/final run with exportable provenance; never reuse prior search state."""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from hermit.adapter.manifest import read_manifest
from hermit.config import Config
from hermit.tree import Tree
from report import summarize
from compare import compare
from experiment_runtime import (ExperimentInterrupted, interruption_handlers, run_owned, snapshot, verify_snapshot)


def read_rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def reported_models(run):
    """Read actual model IDs from Claude events. Codex exec JSONL has no model ID."""
    models = set()
    # Read only provider-owned model fields; never recurse through tool inputs or text.
    for path in run.glob('step_*/output/claude-*-events.jsonl'):
        for line in path.read_text().splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get('type') == 'system' and event.get('subtype') == 'init':
                value = event.get('model')
                if isinstance(value, str) and not value.startswith('<'):
                    models.add(value)
            if event.get('type') == 'assistant':
                value = (event.get('message') or {}).get('model')
                if isinstance(value, str) and not value.startswith('<'):
                    models.add(value)
            if event.get('type') == 'result':
                models.update(k for k in (event.get('modelUsage') or {}) if not k.startswith('<'))
    return sorted(models)



def validate_training(run, expected_proposals=None):
    """A successful CLI exit is insufficient: role failures can preserve the parent."""
    paths = sorted(run.glob('step_*/output/selection.json'))
    if not paths:
        raise RuntimeError('training produced no search selection record')
    summary_path, steps_path = run / 'summary.json', run / 'steps.jsonl'
    if not summary_path.is_file() or not steps_path.is_file():
        raise RuntimeError('training has no completed Trainer summary or step records')
    summary = json.loads(summary_path.read_text())
    steps = [s for s in read_rows(steps_path) if not s.get('bootstrap')]
    if (summary.get('stop_reason') != 'completed all epochs' or summary.get('steps') != len(paths)
            or len(steps) != len(paths)
            or {f"step_{s['step']:04d}" for s in steps} != {p.parents[1].name for p in paths}):
        raise RuntimeError('Trainer did not complete all configured search steps')
    if any(s.get('status') != 'done' or not s.get('check', {}).get('passed') or s.get('repaired')
           for s in steps):
        raise RuntimeError('Trainer failed or repaired a search step; inspect steps.jsonl')
    reviewed = static_rejections = final_revisions_without_approval = 0
    for path in paths:
        selection = json.loads(path.read_text())
        if (selection.get('error') or selection.get('interrupted_error')
                or selection.get('stop_reason') == 'cancelled'):
            raise RuntimeError('search failed; inspect selection.json before spending on test evaluation')
        trials = selection.get('trials', [])
        if expected_proposals is not None and len(trials) != expected_proposals:
            raise RuntimeError('search did not complete the configured proposal budget')
        for trial in trials:
            if trial.get('error') or trial.get('analysis_error') or trial.get('promotion_error'):
                raise RuntimeError('a search role or candidate execution failed; this is an incomplete experiment')
            reviews = trial.get('reviews') or ([trial['review']] if trial.get('review') else [])
            if not reviews:
                raise RuntimeError('a candidate received no independent review')
            independent = [r for r in reviews if r.get('source', 'criticizer') != 'static']
            static_rejections += len(reviews) - len(independent)
            final = trial.get('final_revision') or {}
            bounded_admission = (
                trial.get('evaluation_admission') == 'review_limit_final_revision'
                and final.get('check', {}).get('passed') is True
                and trial.get('check', {}).get('passed') is True
                and final.get('sha256') is not None and final['sha256'] == trial.get('sha256')
                and trial.get('criticizer_calls') == len(independent)
                and 1 <= len(independent) <= 3)
            if trial.get('evaluation_admission') == 'review_limit_final_revision':
                if not bounded_admission:
                    raise RuntimeError('invalid bounded-review final revision record')
                final_revisions_without_approval += 1
            if (trial.get('accepted') or trial.get('promoted')) and not any(
                    r.get('approved') and not r.get('required_changes') for r in independent) and not bounded_admission:
                raise RuntimeError('a promoted candidate has no approving independent review')
            reviewed += len(independent)
    if not reviewed:
        raise RuntimeError('no candidate received independent review')
    return {'reviewed_candidates_or_revisions': reviewed, 'static_rejections': static_rejections,
            'final_revisions_without_approval': final_revisions_without_approval,
            'selection_files': len(paths)}


def role_program(params):
    """The program the roles run, as cli_roles.call_role resolves it from the evolver's settings."""
    from hermit.agent.evolver.methods.evosafe.cli_roles import proposer
    backend = proposer(params)
    return params.get('bin') or params.get(f'{backend}_bin') or backend


def role_model(params):
    """The model the roles run, as cli_roles.call_role resolves it (Codex's default when none is set)."""
    from hermit.agent.evolver.methods.evosafe.cli_roles import proposer
    backend = proposer(params)
    model = params.get('model') or params.get(f'{backend}_model')
    return model if model is not None or backend != 'codex' else 'gpt-6-sol'


def export_training_records(run, artifacts):
    """Include search decisions and metering, without bundling raw CLI logs or prompts."""
    patterns = ('summary.json', 'steps.jsonl', 'step_*/step_result.json',
                'step_*/output/selection.json', 'step_*/output/search-plan.json',
                'step_*/output/evaluation-audit.jsonl', 'step_*/output/parent-results.json',
                'step_*/output/trial-*-results.json', 'learning_material/step_*/summary.json')
    for pattern in patterns:
        for source in run.glob(pattern):
            destination = artifacts / 'training' / source.relative_to(run)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)


def preflight_role(config, directory):
    """Exercise the same shared Base Agent and structured role path before victim spending."""
    from types import SimpleNamespace
    from dataclasses import asdict
    from hermit.agent.evolver.loader import find, instantiate
    from hermit.agent.evolver.methods.evosafe.cli_roles import call_role, proposer
    from hermit.agent.model.backends import registry
    directory.mkdir()
    data = config.data
    evolver = data['evolver']
    spec = find(evolver['name'], config.base)
    _, params = instantiate(spec, evolver['params'],
                            backends=registry(data.get('backends')), default_model=data.get('model'))
    backend = proposer(params)
    if backend not in ('claude', 'codex'):
        raise ValueError('this experiment runner requires a CLI role backend')
    params['timeout_s'] = min(float(params.get('timeout_s', 60)), 60)
    ctx = SimpleNamespace(params=params, sandbox=None, cancel=None,
                          workspace=SimpleNamespace(scratch=directory, output=directory))
    response, usage, error = call_role(ctx, [{'role': 'user', 'content': 'Return {"ready": true}.'}],
        {'type': 'object', 'properties': {'ready': {'type': 'boolean', 'const': True}}, 'required': ['ready'],
         'additionalProperties': False}, 'preflight', backend)
    if error or response is None:
        raise RuntimeError(error or 'role preflight failed')
    return {'status': 'passed', 'usage': [asdict(u) for u in usage]}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--frozen', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    path = args.config.resolve()
    config = Config.load(path)
    root = Path(__file__).resolve().parents[3]
    if args.frozen:
        metadata = verify_snapshot(root)
        if path != Path(metadata['config']):
            raise RuntimeError('configuration is not the frozen effective config')
        with interruption_handlers():
            run_measured(path, config, metadata)
        return
    if config.run_dir().exists() or config.tree_dir('evosafe').exists():
        raise SystemExit('choose fresh run and tree paths')
    metadata = snapshot(root, path, config)
    frozen = Path(metadata['snapshot_root'])
    env = dict(os.environ, PYTHONPATH=str(frozen), PYTHONDONTWRITEBYTECODE='1',
               HERMIT_EPISODE_REGISTRY=str(frozen / '.episode-containers'),
               HA_BACKENDS=metadata['private_backend_file'])
    with interruption_handlers():
        code = run_owned([sys.executable, str(frozen / 'examples/agentdyn/utils/run_experiment.py'),
                          '--config', metadata['config'], '--frozen'], cwd=frozen, stream=None,
                         registry=env['HERMIT_EPISODE_REGISTRY'], env=env)
    if code:
        raise SystemExit(code)


def run_measured(path, config, snapshot_metadata=None):
    run = config.run_dir()
    if run.exists():
        raise SystemExit('run directory already exists: choose a fresh run_name')
    tree_path = config.tree_dir(read_manifest(config.template_dir())['template']['id'])
    if tree_path.exists():
        raise SystemExit('tree directory already exists: choose a fresh tree_dir')
    root = Path(__file__).resolve().parents[3]
    if snapshot_metadata is None and subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=root, text=True).strip():
        raise SystemExit('commit tracked changes before a measured run')
    run.mkdir(parents=True)
    artifacts = run / 'artifacts'
    artifacts.mkdir()
    manifest = {'status': 'running', 'framework_commit': (snapshot_metadata['commit'] if snapshot_metadata else subprocess.check_output(
        ['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()), 'started_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'python': sys.version, 'packages': {}, 'data_sha256': {}, 'task_info_sha256': {},
        'hook_image': config.data['agent']['params'].get('hook_image', 'python:3.12-slim'),
        'protocol': ('fresh train/validation selection, then frozen none/root/final test'
                     if config.data['data'].get('val') else
                     'training-only selection, then frozen none/root/final test after all proposals'),
        'supervisor_pid': os.getpid(),
        'repeats': config.data['eval']['repeats'], 'commands': []}
    started = time.monotonic()
    def save():
        manifest['elapsed_s'] = time.monotonic() - started
        manifest['reported_role_model_ids'] = reported_models(run)
        manifest['heartbeat_utc'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        pending = artifacts / 'manifest.pending.json'
        pending.write_text(json.dumps(manifest, indent=2) + '\n')
        pending.replace(artifacts / 'manifest.json')
    registry = Path(os.environ.get('HERMIT_EPISODE_REGISTRY', str(run / '.episode-containers')))
    def execute(command, label):
        if snapshot_metadata:
            verify_snapshot(root)
        record = {'label': label, 'argv': command, 'status': 'running'}
        manifest['commands'].append(record)
        save()
        def started(pid):
            record['pid'] = pid
            save()
        try:
            with (run / (label + '.log')).open('w') as stream:
                code = run_owned(command, cwd=root, stream=stream, registry=registry, heartbeat=save, started=started)
        except BaseException as exc:
            record['status'] = 'cancelled' if isinstance(exc, (ExperimentInterrupted, KeyboardInterrupt)) else 'failed'
            save()
            raise
        if snapshot_metadata:
            verify_snapshot(root)
        record.update(status='complete' if code == 0 else 'failed', exit_code=code)
        save()
        if code:
            raise RuntimeError(label + ' failed; inspect its local log')
    try:
        if snapshot_metadata:
            manifest['source_snapshot'] = snapshot_metadata
        save()
        shutil.copy2(path, artifacts / 'effective-config.yaml')
        shutil.copy2(snapshot_metadata['original_config'] if snapshot_metadata else path, artifacts / 'run.yaml')
        for package in ('agentdojo', 'hermit-harness', 'mcp', 'pydantic'):
            try:
                manifest['packages'][package] = importlib.metadata.version(package)
            except importlib.metadata.PackageNotFoundError:
                manifest['packages'][package] = None
        for key in ('tasks', 'train', 'val', 'test'):
            if key == 'val' and not config.data['data'].get(key):
                continue
            manifest['data_sha256'][key] = hashlib.sha256(config.path(config.data['data'][key]).read_bytes()).hexdigest()
        for source in config.path(config.data['task_info']).iterdir():
            if source.is_file():
                manifest['task_info_sha256'][source.name] = hashlib.sha256(source.read_bytes()).hexdigest()
        from victim import model_params
        from bridge import AGENTDYN_REVISION
        manifest['victim_model_id'] = model_params(config.data['agent']['params'])['model']
        manifest['dataset_revision'] = AGENTDYN_REVISION
        manifest['docker_version'] = subprocess.check_output(['docker', 'version', '--format', '{{.Server.Version}}'], text=True).strip()
        from hermit.agent.evolver.methods.evosafe.cli_roles import proposer
        manifest['learner_backend'] = proposer(config.data['evolver']['params'])
        manifest['configured_role_model_id'] = role_model(config.data['evolver']['params'])
        manifest['reported_role_model_ids_source'] = (
            'claude_events' if manifest['learner_backend'] == 'claude'
            else 'unavailable_in_codex_exec_jsonl')
        manifest['learner_cli_version'] = subprocess.check_output(
            [role_program(config.data['evolver']['params']), '--version'], text=True).strip()
        save()
        # Fail before provider spend if the execution boundary is unavailable.
        from isolated_hooks import IsolatedDefense
        hook = IsolatedDefense(config.template_dir(), image=manifest['hook_image'])
        try:
            assert hook.system_prompt_transform('preflight') == 'preflight'
        finally:
            hook.close()
        execute([sys.executable, str(Path(__file__).with_name('experiment_runtime.py')), str(path)], 'entry-check')
        manifest['entry_preflight'] = json.loads((run / 'entry-check.log').read_text())
        manifest['role_preflight'] = preflight_role(config, run / 'role-preflight')
        save()
        # Match Trainer's sandbox-disabled handling for the trusted template checker.
        # Core check currently ignores sandbox.enabled for this call; keep this workaround task-side.
        check = "from hermit import spec; spec.SANDBOX_ENFORCE=None; from hermit.cli import main; raise SystemExit(main())"
        execute([sys.executable, '-c', check, 'check', '--config', str(path), '--no-probe'], 'check')
        execute([sys.executable, '-m', 'hermit', 'train', '--config', str(path)], 'train')
        export_training_records(run, artifacts)
        manifest['training_audit'] = validate_training(
            run, expected_proposals=int(config.data['evolver']['params'].get('max_proposals', 2)))
        save()
        runs = {}
        tasks = read_rows(config.path(config.data['data']['tasks']))
        ids = config.path(config.data['data']['test']).read_text().splitlines()
        ids = [i.strip() for i in ids if i.strip() and not i.startswith('#')]
        for adapter in ('none', 'root', 'final'):
            execute([sys.executable, '-m', 'hermit', 'eval', '--config', str(path), '--adapter', adapter], 'eval-' + adapter)
            rows = read_rows(run / 'eval' / adapter / 'results.jsonl')
            runs[adapter] = rows
            shutil.copy2(run / 'eval' / adapter / 'results.jsonl', artifacts / (adapter + '-results.jsonl'))
            shutil.copy2(run / 'eval' / adapter / 'summary.json', artifacts / (adapter + '-summary.json'))
            report = summarize(rows, ids, config.data['eval']['repeats'], tasks=tasks)
            (artifacts / (adapter + '-report.json')).write_text(json.dumps(report, indent=2))
        comparison = compare(tasks, ids, runs, config.data['eval']['repeats'])
        (artifacts / 'comparison.json').write_text(json.dumps(comparison, indent=2))
        tree = Tree(tree_path)
        from hermit.tree import ROOT_TAG
        for label, ref in [('root', ROOT_TAG), ('final', config.run_name() + '/final')]:
            tree.export(ref, artifacts / label)
        manifest['status'] = ('complete_with_episode_errors' if any(not v['complete'] for v in comparison['runs'].values())
                              else 'complete')
    except BaseException as exc:
        manifest['status'] = 'cancelled' if isinstance(exc, (ExperimentInterrupted, KeyboardInterrupt)) else 'failed'
        manifest['failure_type'] = type(exc).__name__
        raise
    finally:
        save()
    # Bundle only reviewed structured reports and adapters; raw logs remain local.
    manifest['artifact_sha256'] = {str(p.relative_to(artifacts)): hashlib.sha256(p.read_bytes()).hexdigest()
                                   for p in artifacts.rglob('*') if p.is_file() and p.name != 'manifest.json'}
    save()
    shutil.make_archive(str(run / 'review-artifacts'), 'gztar', artifacts)
    print(json.dumps({'run': str(run), 'status': manifest['status']}, indent=2))


if __name__ == '__main__':
    main()
