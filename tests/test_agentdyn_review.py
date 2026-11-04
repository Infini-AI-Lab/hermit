"""Regression coverage for review findings, with optional real Docker boundary tests."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'examples/agentdyn/utils'))
from isolated_hooks import IsolatedDefense
from hermit.agent.evolver.methods.evosafe.hooks import ToolCallContext, ToolCallResult


def test_real_eval_rows_without_input(tmp_path):
    spec = importlib.util.spec_from_file_location('review_report', ROOT / 'examples/agentdyn/utils/report.py')
    report = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(report)
    tasks = [{'id': 'c', 'kind': 'clean'}, {'id': 'a', 'kind': 'injected'}, {'id': 'e', 'kind': 'injected'}]
    # Generate the file through Hermit's real eval serializer, using a scripted pipeline.
    (tmp_path / 'tasks.jsonl').write_text(''.join(json.dumps(t) + '\n' for t in tasks))
    (tmp_path / 'test.txt').write_text('c\na\ne\n')
    (tmp_path / 'train.txt').write_text('')
    (tmp_path / 'fixture.py').write_text("""
from hermit.task import RunOutput, JudgeResult
def run(task, adapter):
    if task['id'] == 'e': raise RuntimeError('fixture failure')
    return RunOutput(output={'kind': task['kind'], 'utility': True, 'attack_success': False})
def score(task, output):
    return JudgeResult(1, extra={'metrics': output.output})
""")
    import yaml
    (tmp_path / 'run.yaml').write_text(yaml.safe_dump({
        'template': 'evosafe@1.0',
        'data': {'tasks': 'tasks.jsonl', 'train': 'train.txt', 'test': 'test.txt'},
        'agent': {'name': 'fixture.py:run', 'concurrency': 1},
        'judge': {'path': 'fixture.py'}, 'evolver': {'name': 'evosafe'}, 'sandbox': {'enabled': False},
        'trainer': {'run_name': 'format-regression', 'output_dir': 'runs'}}))
    from hermit.config import Config
    from hermit.evaluate import evaluate
    config = Config.load(tmp_path / 'run.yaml')
    evaluate(config, 'none', log=lambda *_: None)
    rows = [json.loads(line) for line in
            (config.run_dir() / 'eval/none/results.jsonl').read_text().splitlines()]
    assert all('input' not in row for row in rows)
    result = report.summarize(rows, ['c', 'a', 'e'], tasks=tasks)
    assert result['errors'] == 1
    assert result['defense_metrics']['errors_injected'] == 1
    assert result['defense_metrics']['n_clean'] == 1
    assert result['defense_metrics']['n_injected'] == 2
    assert result['defense_metrics']['asr_completed'] == 0
    assert result['defense_metrics']['objective'] is None
    assert 'defense_metrics_error' not in result
    with pytest.raises(ValueError, match='contradicts'):
        report.summarize(rows, ['c', 'a', 'e'], tasks=[{**t, 'kind': 'clean'} for t in tasks])


def docker_required():
    if os.environ.get('HERMIT_TEST_DOCKER') != '1':
        pytest.skip('set HERMIT_TEST_DOCKER=1 to exercise the real hook container')


def test_isolated_state_json_and_classifier(tmp_path, monkeypatch):
    docker_required()
    marker = tmp_path / 'host-only.txt'
    marker.write_text('benign host marker')
    monkeypatch.setenv('EVOSAFE_TEST_SECRET', 'benign sentinel')
    (tmp_path / 'defense.py').write_text('''
import os
from pathlib import Path
from hermit.agent.evolver.methods.evosafe.hooks import Defense
class Hook(Defense):
    n = 0
    def on_pre_tool_call(self, ctx):
        self.n += 1
        ctx.arguments['n'] = self.n
        ctx.arguments['host_visible'] = Path(HOST_MARKER).exists()
        ctx.arguments['secret_visible'] = os.getenv('EVOSAFE_TEST_SECRET') is not None
        ctx.arguments['classified'] = ctx.classify_output('ordinary content')['ok']
        return ctx
    def on_post_tool_call(self, ctx, result):
        return result
def build(): return Hook()
'''.replace('HOST_MARKER', repr(str(marker))))
    hook = IsolatedDefense(tmp_path)
    try:
        ctx = ToolCallContext('agentdyn', 'test', 'read', {}, 'trace', {'instruction': 'read'})
        ctx.classify_output = lambda text: {'ok': True}
        assert hook.on_pre_tool_call(ctx).arguments == {'n': 1, 'classified': True, 'host_visible': False, 'secret_visible': False}
        assert hook.on_pre_tool_call(ctx).arguments['n'] == 2
        from pydantic import BaseModel
        class Item(BaseModel):
            text: str
        raw = [Item(text='hello')]
        assert hook.on_post_tool_call(ctx, ToolCallResult(raw=raw)).raw is raw
        config = json.loads(subprocess.check_output(['docker', 'inspect', hook.name]))[0]
        assert config['HostConfig']['NetworkMode'] == 'none'
        assert config['HostConfig']['ReadonlyRootfs']
        assert config['Config']['User'] == '65534:65534'
        assert len(config['Mounts']) == 3
    finally:
        hook.close()


def test_hook_timeout_removes_container(tmp_path):
    docker_required()
    (tmp_path / 'defense.py').write_text('''
import time
from hermit.agent.evolver.methods.evosafe.hooks import Defense
class Hook(Defense):
    def system_prompt_transform(self, prompt): time.sleep(20)
def build(): return Hook()
''')
    hook = IsolatedDefense(tmp_path, timeout=5)
    hook.timeout = 0.2
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        hook.system_prompt_transform('hello')
    assert time.monotonic() - started < 5
    assert subprocess.run(['docker', 'inspect', hook.name], capture_output=True).returncode != 0


def test_supervisor_deadline_terminates_worker(tmp_path, monkeypatch):
    import bridge
    # A benign slow trusted worker isolates the supervisor from model/provider behavior.
    worker = tmp_path / 'episode_worker.py'
    worker.write_text('import time; time.sleep(30)')
    monkeypatch.setattr(bridge, '__file__', str(tmp_path / 'bridge.py'))
    result = bridge.run({}, None, params={'episode_timeout_s': 0.2}, base=tmp_path)
    assert result.output == {'episode_error': 'episode deadline exceeded'}
    assert result.tokens is None and result.cost_usd is None
    assert result.wall_s < 3


def completed_training_fixture(run):
    directory = run / 'step_0001/output'
    directory.mkdir(parents=True)
    (run / 'summary.json').write_text(json.dumps({'stop_reason': 'completed all epochs', 'steps': 1}))
    (run / 'steps.jsonl').write_text(json.dumps(
        {'step': 1, 'status': 'done', 'check': {'passed': True}, 'repaired': []}) + '\n')
    return directory


def test_experiment_rejects_role_failures_but_allows_review_rejection(tmp_path):
    from run_experiment import validate_training
    directory = completed_training_fixture(tmp_path)
    selection = directory / 'selection.json'
    selection.write_text(json.dumps({'trials': [{'error': 'authentication failed', 'reviews': []}]}))
    with pytest.raises(RuntimeError, match='incomplete'):
        validate_training(tmp_path)
    selection.write_text(json.dumps({'trials': [{'reviews': [{'approved': False}], 'accepted': False}]}))
    assert validate_training(tmp_path)['reviewed_candidates_or_revisions'] == 1
    selection.write_text(json.dumps({'trials': []}))
    with pytest.raises(RuntimeError, match='no candidate'):
        validate_training(tmp_path)


def test_experiment_counts_static_failures_separately_from_independent_reviews(tmp_path):
    from run_experiment import validate_training
    directory = completed_training_fixture(tmp_path)
    selection = {'trials': [{'accepted': False, 'reviews': [
        {'source': 'static', 'approved': False}, {'source': 'static', 'approved': False}]}]}
    path = directory / 'selection.json'
    path.write_text(json.dumps(selection))
    with pytest.raises(RuntimeError, match='no candidate received independent review'):
        validate_training(tmp_path)
    selection['trials'][0]['reviews'].append({'source': 'criticizer', 'approved': False})
    path.write_text(json.dumps(selection))
    audit = validate_training(tmp_path)
    assert audit['reviewed_candidates_or_revisions'] == 1
    assert audit['static_rejections'] == 2
    selection['trials'][0]['accepted'] = True
    path.write_text(json.dumps(selection))
    with pytest.raises(RuntimeError, match='no approving independent review'):
        validate_training(tmp_path)
    trial = selection['trials'][0]
    trial.update(evaluation_admission='review_limit_final_revision', criticizer_calls=1,
                 sha256='final-source', check={'passed': True},
                 final_revision={'sha256': 'final-source', 'check': {'passed': True}})
    path.write_text(json.dumps(selection))
    assert validate_training(tmp_path)['final_revisions_without_approval'] == 1
    trial['final_revision']['sha256'] = 'different-source'
    path.write_text(json.dumps(selection))
    with pytest.raises(RuntimeError, match='invalid bounded-review'):
        validate_training(tmp_path)


def test_reported_models_excludes_synthetic_and_tool_arguments(tmp_path):
    from run_experiment import reported_models
    directory = tmp_path / 'step_0001/output'
    directory.mkdir(parents=True)
    events = [{'type': 'system', 'subtype': 'init', 'model': 'claude-fixture'},
              'non-event line',
              {'type': 'assistant', 'message': {'model': '<synthetic>', 'content': [{'model': 'fake'}]}}]
    (directory / 'claude-fixture-events.jsonl').write_text('\n'.join(json.dumps(e) for e in events))
    (directory / 'codex-fixture-events.jsonl').write_text(
        json.dumps({'type': 'thread.started', 'thread_id': 'fixture'}) + '\n')
    assert reported_models(tmp_path) == ['claude-fixture']


@pytest.mark.parametrize('remaining', ['', 'container-id\n'])
def test_failed_container_removal_checks_presence_before_clearing_registration(tmp_path, monkeypatch, remaining):
    from types import SimpleNamespace

    defense = IsolatedDefense.__new__(IsolatedDefense)
    defense.name = 'hermit-hook-fixture'
    defense.registration = tmp_path / defense.name
    defense.registration.touch()
    defense.proc = None
    defense.temp = SimpleNamespace(cleanup=lambda: None)
    monkeypatch.setattr(subprocess, 'run', lambda *args, **kwargs: subprocess.CompletedProcess(args, 1))
    monkeypatch.setattr(subprocess, 'check_output', lambda *args, **kwargs: remaining)
    if remaining:
        with pytest.raises(RuntimeError, match='container cleanup incomplete'):
            defense.close()
    else:
        defense.close()
    assert defense.registration.exists() == bool(remaining)


def test_role_preflight_resolves_training_backend(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from hermit.agent.evolver.methods.evosafe import cli_roles
    from run_experiment import preflight_role

    monkeypatch.setenv('HA_BACKENDS', str(tmp_path / 'no-local-settings.yaml'))
    config = SimpleNamespace(base=tmp_path, data={
        'evolver': {'name': 'evosafe', 'params': {
            'backend': 'codex', 'model': 'fixture/role-model', 'timeout_s': 120}},
        'backends': {'fixture': {'base_url': 'http://127.0.0.1:9/v1',
                                'key_required': False, 'apis': ['responses']}},
    })

    def call(ctx, messages, schema, name, backend):
        assert backend == 'codex'
        assert ctx.params['model'] == 'role-model'
        assert ctx.params['binding']['ref'] == 'fixture/role-model'
        assert 'model_provider="fixture"' in ctx.params['args']
        assert ctx.params['timeout_s'] == 60
        return {'content': '{"ready": true}'}, [], None

    monkeypatch.setattr(cli_roles, 'call_role', call)
    assert preflight_role(config, tmp_path / 'preflight')['status'] == 'passed'


@pytest.mark.parametrize('change', [
    {'interrupted_error': 'later role failed'}, {'stop_reason': 'cancelled'},
    {'trials': [{'reviews': [{'approved': True}], 'promotion_error': 'copy failed'}]},
    {'trials': [{'reviews': []}]},
])
def test_experiment_rejects_interrupted_or_unreviewed_search(tmp_path, change):
    from run_experiment import validate_training
    directory = completed_training_fixture(tmp_path)
    selection = {'accepted': True, 'trials': [{'reviews': [{'approved': True}]}], **change}
    (directory / 'selection.json').write_text(json.dumps(selection))
    with pytest.raises(RuntimeError):
        validate_training(tmp_path, expected_proposals=1)


def test_experiment_requires_configured_proposals_and_exports_training_records(tmp_path):
    from run_experiment import validate_training, export_training_records
    run, artifacts = tmp_path / 'run', tmp_path / 'artifacts'
    output = completed_training_fixture(run)
    selection = {'trials': [{'reviews': [{'approved': False}]}]}
    (output / 'selection.json').write_text(json.dumps(selection))
    (run / 'summary.json').write_text(json.dumps({'stop_reason': 'completed all epochs', 'steps': 1,
                                                'spending': {'unknown_cost_calls': 3}}))
    (output / 'codex-trial-001-events.jsonl').write_text('raw CLI log excluded')
    with pytest.raises(RuntimeError, match='proposal budget'):
        validate_training(run, expected_proposals=3)
    assert validate_training(run, expected_proposals=1)['reviewed_candidates_or_revisions'] == 1
    export_training_records(run, artifacts)
    assert json.loads((artifacts / 'training/summary.json').read_text())['spending']['unknown_cost_calls'] == 3
    assert json.loads((artifacts / 'training/step_0001/output/selection.json').read_text()) == selection
    assert not list(artifacts.rglob('*events.jsonl'))


@pytest.mark.parametrize('record', [
    {'status': 'failed'}, {'check': {'passed': False}}, {'repaired': ['defense.py']},
])
def test_experiment_rejects_trainer_failure_even_after_successful_search(tmp_path, record):
    from run_experiment import validate_training
    output = completed_training_fixture(tmp_path)
    (output / 'selection.json').write_text(json.dumps({'trials': [{'reviews': [{'approved': True}]}]}))
    (tmp_path / 'steps.jsonl').write_text(json.dumps(
        {'step': 1, 'status': 'done', 'check': {'passed': True}, 'repaired': [], **record}) + '\n')
    with pytest.raises(RuntimeError, match='Trainer failed or repaired'):
        validate_training(tmp_path, expected_proposals=1)


@pytest.mark.parametrize('failure', ['data', 'docker', 'cli', 'hook', 'entry', 'interrupt'])
def test_experiment_startup_failures_are_recorded_before_role_spend(tmp_path, monkeypatch, failure):
    from types import SimpleNamespace
    import run_experiment as runner
    import isolated_hooks

    config_path = tmp_path / 'run.yaml'
    config_path.write_text('fixture config')
    data = tmp_path / 'tasks.jsonl'
    if failure != 'data':
        data.write_text('{}\n')
    task_info = tmp_path / 'task-info'
    task_info.mkdir()
    run = tmp_path / 'run'
    config = SimpleNamespace(
        data={'agent': {'params': {'model': 'fixture'}}, 'evolver': {'params': {'backend': 'codex'}},
              'eval': {'repeats': 1}, 'data': {k: str(data) for k in ('tasks', 'train', 'val', 'test')},
              'task_info': str(task_info)},
        run_dir=lambda: run, tree_dir=lambda _: tmp_path / 'tree',
        template_dir=lambda: tmp_path / 'template', path=Path,
    )
    monkeypatch.setattr(runner.Config, 'load', lambda _: config)
    monkeypatch.setattr(runner, 'read_manifest', lambda _: {'template': {'id': 'fixture'}})
    monkeypatch.setattr(sys, 'argv', ['run_experiment', '--config', str(config_path)])
    def check_output(command, **kwargs):
        if command[0] == 'git':
            return '' if command[1] == 'status' else 'fixture-commit'
        if (failure == 'docker' and command[0] == 'docker') or (failure == 'cli' and command[0] == 'codex'):
            raise OSError('fixture prerequisite failure')
        return 'fixture-version'
    monkeypatch.setattr(runner.subprocess, 'check_output', check_output)
    def unavailable_hook(*args, **kwargs):
        if failure == 'hook':
            raise OSError('fixture hook startup failure')
        return SimpleNamespace(system_prompt_transform=lambda prompt: prompt, close=lambda: None)
    monkeypatch.setattr(isolated_hooks, 'IsolatedDefense', unavailable_hook)
    def role(*args):
        pytest.fail('role spending must wait until local prerequisites pass')
    monkeypatch.setattr(runner, 'preflight_role', role)

    from experiment_runtime import ExperimentInterrupted
    def stage(*args, **kwargs):
        if failure == 'interrupt':
            raise ExperimentInterrupted(15)
        return 1
    monkeypatch.setattr(runner, 'run_owned', stage)
    expected = ExperimentInterrupted if failure == 'interrupt' else RuntimeError if failure == 'entry' else OSError
    with pytest.raises(expected):
        runner.run_measured(config_path, config)
    manifest = json.loads((run / 'artifacts/manifest.json').read_text())
    assert manifest['status'] == ('cancelled' if failure == 'interrupt' else 'failed')
    assert manifest['failure_type'] in ('OSError', 'FileNotFoundError', 'RuntimeError', 'ExperimentInterrupted')
    assert manifest['framework_commit'] == 'fixture-commit'
    assert [c['label'] for c in manifest['commands']] == (['entry-check'] if failure in ('entry', 'interrupt') else [])


def test_structured_metadata_survives_a_dict_decision_in_the_container(tmp_path):
    docker_required()
    (tmp_path / 'defense.py').write_text('''
from hermit.agent.evolver.methods.evosafe.hooks import Defense
class Hook(Defense):
    def on_pre_tool_call(self, ctx):
        ctx.metadata['checked'] = True
        return {'arguments': {'limit': 1}}
    def on_post_tool_call(self, ctx, result):
        if not ctx.metadata.get('checked'):
            raise PermissionError('metadata lost')
        return None
def build(): return Hook()
''')
    sys.path.insert(0, str(ROOT / 'examples/agentdyn/utils'))
    from bridge import GuardedRuntime
    class Runtime:
        functions = {}
        def run_function(self, env, name, args):
            return 'fact', None
    hook, audit = IsolatedDefense(tmp_path), []
    try:
        text, error = GuardedRuntime(Runtime(), hook, 'read', 'trace', str, audit, 'shopping').run_function(
            {}, 'read', {'limit': 50})
    finally:
        hook.close()
    assert (text, error) == ('fact', None) and audit[0]['arguments'] == {'limit': 1}


@pytest.mark.parametrize('params,program', [({'backend': 'codex'}, 'codex'),
                                            ({'backend': 'codex', 'bin': '/opt/pinned-codex'}, '/opt/pinned-codex'),
                                            ({'proposer': 'claude', 'claude_bin': '/opt/claude'}, '/opt/claude')])
def test_the_recorded_cli_version_is_of_the_program_the_roles_run(params, program):
    from run_experiment import role_program
    assert role_program(params) == program


@pytest.mark.parametrize('params,model', [({'backend': 'codex', 'model': 'm'}, 'm'),
                                          ({'proposer': 'codex', 'codex_model': 'legacy'}, 'legacy'),
                                          ({'proposer': 'codex'}, 'gpt-6-sol'),
                                          ({'proposer': 'claude'}, None)])
def test_the_manifest_records_the_model_the_roles_run(params, model):
    from run_experiment import role_model
    assert role_model(params) == model
