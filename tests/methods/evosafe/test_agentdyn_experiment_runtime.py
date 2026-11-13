"""Real subprocess checks for frozen inputs, runtime overrides, entry loading and cancellation."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
UTILS = ROOT / 'examples/agentdyn/utils'
sys.path.insert(0, str(UTILS))
from experiment_runtime import snapshot, verify_snapshot


@pytest.mark.parametrize('with_validation', [True, False])
@pytest.mark.parametrize('disable_sandbox', [True, False])
def test_snapshot_survives_changes_to_original_code_config_and_data(tmp_path, monkeypatch, with_validation, disable_sandbox):
    root = tmp_path / 'repo'
    root.mkdir()
    (root / 'lazy.py').write_text("VALUE = 'committed'\n")
    (root / 'worker.py').write_text("import lazy; print(lazy.VALUE)\n")
    for name in ['template', 'task-info']:
        (root / name).mkdir()
        (root / name / 'info').write_text('committed context')
    import yaml
    config_data = {'data': {k: 'cases.txt' for k in ('tasks', 'train', 'val', 'test')},
                   'agent': {'name': 'worker.py:run', 'params': {}}, 'judge': {'path': 'worker.py'},
                   'trainer': {}, 'task_info': 'task-info', 'template': 'template', 'sandbox': {'enabled': True},
                   'evolver': {'name': 'she'}}
    if not with_validation:
        config_data['data'].pop('val')
    config_path = root / 'run.yaml'
    config_path.write_text(yaml.safe_dump(config_data))
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(root), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                    'commit', '-qm', 'fixture'], check=True)
    (root / 'cases.txt').write_text('original cases')  # An ignored/untracked prepared input.
    config = SimpleNamespace(data=config_data, run_dir=lambda: tmp_path / 'output/run',
        run_name=lambda: 'run', path=lambda value: (root / value).resolve(),
        template_dir=lambda: root / 'template', tree_dir=lambda method: tmp_path / ('tree-' + method))
    backend = tmp_path / 'backends.yaml'
    backend.write_text('fixture: {base_url: https://original.example.invalid}')
    monkeypatch.setenv('HA_BACKENDS', str(backend))
    meta = snapshot(root, config_path, config, sandbox_enabled=False if disable_sandbox else None)
    assert yaml.safe_load(config_path.read_text())["sandbox"]["enabled"] is True
    assert meta["runtime_overrides"] == ({"sandbox.enabled": False} if disable_sandbox else {})
    backend.write_text('changed backend')
    frozen = Path(meta['snapshot_root'])
    (root / 'lazy.py').write_text("VALUE = 'changed during experiment'\n")
    (root / 'cases.txt').write_text('changed cases')
    config_path.write_text('changed config')
    (root / 'task-info/info').write_text('changed instructions')
    env = dict(os.environ, PYTHONPATH=str(frozen), PYTHONDONTWRITEBYTECODE='1')
    assert subprocess.check_output([sys.executable, str(frozen / 'worker.py')], cwd=frozen,
                                   env=env, text=True).strip() == 'committed'
    effective = yaml.safe_load(Path(meta['config']).read_text())
    assert effective['trainer']['tree_dir'] == str(tmp_path / 'tree-she')
    assert effective['sandbox']['enabled'] is not disable_sandbox
    assert ('val' in effective['data']) == with_validation
    assert Path(effective['data']['tasks']).read_text() == 'original cases'
    assert Path(effective['task_info'], 'info').read_text() == 'committed context'
    assert str(frozen) in effective['agent']['name']
    assert 'original.example.invalid' in Path(meta['private_backend_file']).read_text()
    verify_snapshot(frozen)
    target = frozen / 'lazy.py'
    target.chmod(0o644)
    target.write_text('tampered snapshot')
    with pytest.raises(RuntimeError, match='frozen source/input changed'):
        verify_snapshot(frozen)


def test_formal_entry_check_detects_broken_import_without_model_calls(tmp_path):
    # A fresh interpreter must import the actual configured Evolver, not cached modules.
    shutil.copytree(ROOT / 'hermit', tmp_path / 'hermit', ignore=shutil.ignore_patterns('__pycache__'))
    contract = tmp_path / 'hermit/agent/evolver/methods/evosafe/contract.py'
    contract.write_text(contract.read_text().replace('def aggregate(', 'def missing_aggregate('))
    config = tmp_path / 'run.yaml'
    config.write_text('''template: evosafe@1.0
data: {tasks: tasks.jsonl, train: train.txt}
agent: {name: fixture.py:run}
judge: {path: fixture.py}
evolver: {name: evosafe, params: {search_mode: roles}}
''')
    env = dict(os.environ, PYTHONPATH=str(tmp_path), PYTHONDONTWRITEBYTECODE='1')
    result = subprocess.run([sys.executable, str(UTILS / 'experiment_runtime.py'), str(config)],
                            cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode != 0 and 'aggregate' in result.stderr


def running(pid):
    if not Path('/proc').is_dir():   # macOS: the state from ps
        state = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True).stdout.strip()
        return bool(state) and not state.startswith('Z')
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0] != 'Z'
    except FileNotFoundError:
        return False


@pytest.mark.parametrize('stop_signal', [signal.SIGTERM, signal.SIGINT])
def test_interrupt_reaps_child_and_stops_separate_session_descendant(tmp_path, stop_signal):
    child = tmp_path / 'child.py'
    child.write_text('''import subprocess,sys,time,json,os
from pathlib import Path
p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)'], start_new_session=True)
Path('pids.json').write_text(json.dumps([os.getpid(),p.pid]))
time.sleep(120)
''')
    controller = tmp_path / 'controller.py'
    controller.write_text('''from experiment_runtime import run_owned,interruption_handlers,ExperimentInterrupted
from pathlib import Path
import sys
try:
    with interruption_handlers():
        run_owned([sys.executable,'child.py'],cwd=Path.cwd(),stream=None,registry=Path('registry'))
except ExperimentInterrupted:
    Path('cancelled').write_text('cleaned')
''')
    env = dict(os.environ, PYTHONPATH=str(UTILS) + os.pathsep + str(ROOT))
    proc = subprocess.Popen([sys.executable, str(controller)], cwd=tmp_path, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    ids = []
    try:
        deadline = time.monotonic() + 10
        while not (tmp_path / 'pids.json').exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        ids = json.loads((tmp_path / 'pids.json').read_text())
        proc.send_signal(stop_signal)
        stdout, stderr = proc.communicate(timeout=10)
        assert proc.returncode == 0, stderr
        assert (tmp_path / 'cancelled').is_file()
        assert not any(running(pid) for pid in ids)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.communicate()
        for pid in ids:
            if running(pid):
                os.kill(pid, signal.SIGKILL)


def test_cleanup_removes_only_registered_container(tmp_path):
    if os.environ.get('HERMIT_TEST_DOCKER') != '1':
        pytest.skip('real Docker test opt-in')
    from isolated_hooks import IsolatedDefense
    from experiment_runtime import cleanup_containers
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'defense.py').write_text('''from hermit.agent.evolver.methods.evosafe.hooks import Defense
def build(): return Defense()
''')
    hook = IsolatedDefense(source)
    unrelated = IsolatedDefense(source)
    registry = tmp_path / 'registry'
    registry.mkdir()
    (registry / hook.name).touch()
    try:
        cleanup_containers(registry)
        assert subprocess.run(['docker', 'inspect', hook.name], capture_output=True).returncode != 0
        assert subprocess.run(['docker', 'inspect', unrelated.name], capture_output=True).returncode == 0
        assert not list(registry.iterdir())
    finally:
        hook.close()
        unrelated.close()


@pytest.mark.parametrize('field', ['start_from', 'api_script', 'direct'])
def test_snapshot_refuses_a_file_input_outside_the_repository(tmp_path, monkeypatch, field):
    root = tmp_path / 'repo'
    for name in ['template', 'task-info']:
        (root / name).mkdir(parents=True)
        (root / name / 'info').write_text('committed')
    (root / 'worker.py').write_text('')
    outside = tmp_path / 'outside.py'
    outside.write_text('live')
    import yaml
    config_data = {'data': {k: 'cases.txt' for k in ('tasks', 'train', 'test')},
                   'agent': {'name': 'worker.py:run', 'params': {}}, 'judge': {'path': 'worker.py'},
                   'trainer': {}, 'task_info': 'task-info', 'template': 'template'}
    if field == 'start_from':
        config_data['start_from'] = str(outside)
    elif field == 'direct':
        config_data['agent']['direct'] = {'api_script': str(outside)}
    else:
        config_data['agent']['params']['api_script'] = str(outside)
    config_path = root / 'run.yaml'
    config_path.write_text(yaml.safe_dump(config_data))
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(root), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                    'commit', '-qm', 'fixture'], check=True)
    (root / 'cases.txt').write_text('cases')
    config = SimpleNamespace(data=config_data, run_dir=lambda: tmp_path / 'output/run',
        run_name=lambda: 'run', path=lambda value: (root / value).resolve(),
        template_dir=lambda: root / 'template', tree_dir=lambda _: tmp_path / 'tree')
    monkeypatch.setenv('HA_BACKENDS', str(tmp_path / 'none.yaml'))
    with pytest.raises(ValueError, match='must be committed inside the repository'):
        snapshot(root, config_path, config)


def test_a_frozen_run_cannot_receive_an_unrecorded_sandbox_override(tmp_path):
    result = subprocess.run([sys.executable, str(UTILS / 'run_experiment.py'),
                             '--config', str(tmp_path / 'absent.yaml'), '--frozen', '--no-sandbox'],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 2
    assert '--no-sandbox must be applied before source freezing' in result.stderr
