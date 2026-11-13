"""Frozen source and interruptible process ownership for the AgentDyn experiment runner."""
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tarfile
import tempfile
import time

import yaml

from hermit.agent.base.cli import _descendants


class ExperimentInterrupted(BaseException):
    def __init__(self, signum):
        self.signum = signum
        super().__init__(f'experiment interrupted by signal {signum}')


@contextmanager
def interruption_handlers():
    previous = {}
    def interrupted(signum, frame):
        # A second signal must not abandon cleanup halfway through.
        for sig in previous:
            signal.signal(sig, signal.SIG_IGN)
        raise ExperimentInterrupted(signum)
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, interrupted)
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


@contextmanager
def finishing_cleanup():
    pending = []
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        for sig in previous:
            signal.signal(sig, lambda signum, frame: pending.append(signum))
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    if pending:
        raise ExperimentInterrupted(pending[0])


def cleanup_containers(registry):
    names = [p.name for p in Path(registry).glob('hermit-hook-*')
             if re.fullmatch(r'hermit-hook-[0-9a-f]{32}', p.name)]
    if names:
        subprocess.run(['docker', 'rm', '-f', *names], capture_output=True, timeout=20)
        # A worker may have removed its own container concurrently. Inspect remaining names.
        remaining = subprocess.check_output(['docker', 'ps', '-aq', '--filter',
                                            'name=^/(' + '|'.join(names) + ')$'], text=True, timeout=20)
        if remaining.strip():
            raise RuntimeError('episode container cleanup incomplete')
        for name in names:
            (Path(registry) / name).unlink(missing_ok=True)


def stop_process(proc):
    # Shared CLI logic enumerates separate-session descendants as well as the process group.
    targets = _descendants(proc.pid)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in reversed(list(dict.fromkeys(targets + _descendants(proc.pid)))):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass
        except PermissionError:  # macOS: only exited processes, not yet reaped, are left
            if proc.poll() is None:
                raise
        if sig == signal.SIGTERM:
            time.sleep(0.3)
    proc.wait(timeout=5)


def run_owned(command, *, cwd, stream, registry, heartbeat=lambda: None, started=lambda pid: None, env=None):
    """The caller owns the child until it is reaped, including startup/save failures."""
    proc = subprocess.Popen(command, cwd=cwd, stdout=stream, stderr=subprocess.STDOUT,
                            start_new_session=True, env=env)
    try:
        started(proc.pid)
        while True:
            try:
                return proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                heartbeat()
    finally:
        # Kill remaining members of the stage process group as well as live descendants.
        with finishing_cleanup():
            try:
                stop_process(proc)
            finally:
                cleanup_containers(registry)


def snapshot(root, config_path, config, *, sandbox_enabled=None):
    """Archive the committed tree; never bind live repository files into a measured run."""
    root = Path(root).resolve()
    if subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=root, text=True).strip():
        raise RuntimeError('commit tracked changes before a measured run')
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
    relative = Path(config_path).resolve().relative_to(root)
    # Require a committed config, not an ignored or untracked replacement.
    committed_config = subprocess.check_output(['git', 'show', f'{commit}:{relative}'], cwd=root)
    if committed_config != Path(config_path).read_bytes():
        raise RuntimeError('config changed while creating source snapshot')
    parent = config.run_dir().parent / '.source-snapshots'
    parent.mkdir(parents=True, exist_ok=True)
    frozen = Path(tempfile.mkdtemp(prefix=config.run_name() + '-', dir=parent))
    archive = subprocess.check_output(['git', 'archive', commit], cwd=root)
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        if any(m.issym() or m.islnk() for m in source.getmembers()):
            raise ValueError('source snapshots require regular files, not symlinks')
        source.extractall(frozen, filter='data')
    effective = yaml.safe_load(committed_config)
    overrides = {}
    if sandbox_enabled is not None:
        effective.setdefault("sandbox", {})["enabled"] = sandbox_enabled
        overrides["sandbox.enabled"] = sandbox_enabled
    inputs = frozen / '.experiment-inputs'
    inputs.mkdir()
    original_config = inputs / 'original-run.yaml'
    original_config.write_bytes(committed_config)
    for key in ('tasks', 'train', 'val', 'test'):
        if key == 'val' and not config.data['data'].get(key):
            effective['data'].pop(key, None)
            continue
        source = config.path(config.data['data'][key])
        target = inputs / (key + source.suffix)
        target.write_bytes(source.read_bytes())
        effective['data'][key] = str(target)
    # Public instructions and the template must come from the committed tree.
    for key, source in [('task_info', config.path(config.data['task_info'])),
                        ('template', config.template_dir())]:
        target = frozen / source.resolve().relative_to(root)
        if not target.is_dir():
            raise ValueError(f'{key} must be committed inside the repository')
        effective[key] = str(target)
    # Other file inputs too: a path outside the snapshot would stay live and unhashed.
    params, direct = effective['agent'].get('params') or {}, effective['agent'].get('direct') or {}
    for name, holder, key in [('start_from', effective, 'start_from'),
                              ('agent.params.api_script', params, 'api_script'),
                              ('agent.direct.api_script', direct, 'api_script')]:
        if holder.get(key):
            source = config.path(holder[key]).resolve()
            target = frozen / source.relative_to(root) if source.is_relative_to(root) else None
            if target is None or not target.exists():
                raise ValueError(f'{name} must be committed inside the repository')
            holder[key] = str(target)
    # File-backed application entry points must resolve inside the archived source.
    for section, key in [('agent', 'name'), ('judge', 'path')]:
        value = effective[section][key]
        filename, separator, function = value.partition(':')
        if filename.endswith('.py'):
            target = frozen / config.path(filename).relative_to(root)
            if not target.is_file():
                raise ValueError(f'{section}.{key} must be a committed entry point')
            effective[section][key] = str(target) + (separator + function if separator else '')
    effective['trainer']['output_dir'] = str(config.run_dir().parent)
    effective['trainer']['tree_dir'] = str(config.tree_dir(config.data.get('evolver', {}).get('name', 'evosafe')))
    effective['agent'].setdefault('params', {})['logs'] = str(config.path(
        config.data['agent']['params'].get('logs', 'runs/agentdyn-episodes')))
    destination = frozen / relative
    destination.write_text(yaml.safe_dump(effective, sort_keys=False))
    backend = Path(os.environ.get('HA_BACKENDS') or Path.home() / '.config/hermit/backends.yaml')
    backend_target = inputs / 'private-backends.yaml'
    backend_target.write_bytes(backend.read_bytes() if backend.exists() else b'{}\n')
    hashes = {str(p.relative_to(frozen)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in frozen.rglob('*') if p.is_file()}
    metadata = {'commit': commit, 'source_root': str(root), 'snapshot_root': str(frozen),
                'config': str(destination), 'original_config': str(original_config), 'private_backend_file': str(backend_target), 'file_sha256': hashes,
                'runtime_overrides': overrides}
    (frozen / '.experiment-snapshot.json').write_text(json.dumps(metadata, indent=2) + '\n')
    for p in frozen.rglob('*'):
        if p.is_file():
            p.chmod(0o555 if os.access(p, os.X_OK) else 0o444)
    return metadata


def verify_snapshot(root):
    root = Path(root).resolve()
    metadata = json.loads((root / '.experiment-snapshot.json').read_text())
    if Path(metadata['snapshot_root']) != root:
        raise RuntimeError('snapshot root does not match its metadata')
    for name, digest in metadata['file_sha256'].items():
        p = root / name
        if p.is_symlink() or hashlib.sha256(p.read_bytes()).hexdigest() != digest:
            raise RuntimeError(f'frozen source/input changed: {name}')
    return metadata


def check_entry(config_path):
    """Fresh-process entry check; no provider calls, tools or rollouts."""
    from hermit.config import Config
    from hermit.agent.evolver.loader import find, instantiate
    config = Config.load(Path(config_path))
    spec = find(config.data['evolver']['name'], config.base)
    evolver, params = instantiate(spec, config.data['evolver']['params'])
    if spec.name not in ('evosafe', 'she'):
        raise ValueError('this experiment runner requires an AgentDyn defense Evolver')
    if params.get('search_mode', 'single') == 'roles':
        from hermit.agent.evolver.methods.evosafe.roles import run, proposal_schema, REVIEW_SCHEMA, ANALYSIS_SCHEMA
        assert callable(run) and proposal_schema() and REVIEW_SCHEMA and ANALYSIS_SCHEMA
    return {'status': 'passed', 'class': type(evolver).__name__, 'search_mode': params.get('search_mode', 'single')}


if __name__ == "__main__":
    import sys
    print(json.dumps(check_entry(sys.argv[1])))
