"""Independent training streams and deterministic harness-bank assembly.

Every upstream loop/probe starts with the same fixed seed and sees one public
training ID. Prior rollout scores and other packages never enter that loop.
Only scheduling and final packaging change; the actor and upstream loop do not.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

from hermit.config import Config
from hermit.spec import check_spec
from hermit.trainer.trainer import Trainer

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]


def atomic_json(path, value):
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, indent=2))
    temp.replace(path)


def fingerprints():
    paths = [*HERE.glob('*.py'), *HERE.glob('*.yaml'),
             *ROOT.joinpath('hermit/agent/evolver/methods/coevoskills').glob('*.py'),
             *ROOT.joinpath('hermit/adapter/templates/coevoskills').rglob('*'),
             ROOT/'hermit/agent/acting/workspace.py', ROOT/'hermit/agent/base/cli.py',
             ROOT/'hermit/agent/base/container.py', ROOT/'hermit/hosts/codex.py', ROOT/'hermit/hosts/inputs.py',
             HERE.parent/'utils/task.py', HERE.parent/'utils/prompt.md', HERE.parent/'utils/judge.py',
             HERE.parent/'utils/tasks.jsonl', *HERE.parent.joinpath('utils/splits').glob('*.txt')]
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(set(paths)) if p.is_file() and '__pycache__' not in p.parts}


def verify_frozen(record):
    for name, digest in record.items():
        if hashlib.sha256((ROOT/name).read_bytes()).hexdigest() != digest:
            raise RuntimeError('run source changed after freeze: ' + name)


def audit_final_deployments(adapter, results):
    from hermit.agent.evolver.methods.coevoskills.integrity import verify_deployment
    issues = []
    rows = [json.loads(line) for line in results.read_text().splitlines()]
    for row in rows:
        if row['status'] != 'ok':
            continue
        log = Path(row['log'])
        try:
            verify_deployment(adapter, log)
        except (OSError, ValueError) as exc:
            issues.append({'task': row['id'], 'issue': str(exc)})
    atomic_json(results.parent/'skill-integrity.json', {'checked_tasks': len(rows), 'issues': issues})
    if issues:
        raise RuntimeError('frozen Skill deployment integrity failed; raw scores retained for audit')

def training_progress(output, ids, shards):
    order = {tid: i+1 for i, tid in enumerate(ids)}
    records = []
    for shard in shards:
        path = output/'training'/shard/'steps.jsonl'
        if not path.is_file():
            continue
        for line in path.read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            tid, = row['tasks']
            records.append({**row, 'step': order[tid], 'shard': shard,
                            'shard_step': row['step'],
                            'note': 'independent task stream; training rollout is not test accuracy'})
    records.sort(key=lambda r: r['step'])
    if len({r['step'] for r in records}) != len(records):
        raise RuntimeError('duplicate training task across shards')
    target = output/'coevoskills/steps.jsonl'
    target.parent.mkdir(exist_ok=True)
    temp = target.with_suffix('.tmp')
    temp.write_text(''.join(json.dumps(r)+'\n' for r in records))
    temp.replace(target)
    return records


def assemble(seed, output, ids, records):
    if len(records) != len(ids):
        raise RuntimeError('training ended before every intended task was recorded')
    frozen = output/'frozen/final'
    shutil.copytree(seed, frozen)
    packages = []
    for row in records:
        tid, = row['tasks']
        source = output/'training'/row['shard']/'workspace/adapter/skills'/tid
        if row['evolver']['status'] == 'done':
            if not source.is_dir():
                raise RuntimeError('successful training task has no exported package: '+tid)
            destination = frozen/'skills'/tid
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, destination)
            packages.append({'task': tid, 'shard': row['shard'], 'commit': row['commit']})
    check = check_spec(frozen, seed)
    if not check.passed:
        raise RuntimeError('assembled adapter violates the fixed contract: '+str(check.issues))
    digest = {str(p.relative_to(frozen)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted(frozen.rglob('*')) if p.is_file() and '__pycache__' not in p.parts}
    atomic_json(output/'frozen-adapter.json', {'path': str(frozen), 'packages': packages, 'files': digest})
    return frozen


def train_parallel(config, output, seed, workers, frozen_sources):
    ids = config.path(config['data']['train']).read_text().split()
    if len(ids) != len(set(ids)):
        raise ValueError('unique training IDs are required')
    workers = min(workers, len(ids))
    jobs = output/'training'
    jobs.mkdir()
    processes, handles, shards = [], [], []
    starts, finishes = [], {}
    budget_s = float(config['trainer']['budget'].get('hours') or 72)*3600
    try:
        for i in range(workers):
            name = f'shard-{i:02}'
            shards.append(name)
            split = jobs/(name+'-ids.txt')
            split.write_text('\n'.join(ids[i::workers])+'\n')
            data = copy.deepcopy(config.data)
            data['data']['train'] = str(split)
            data['trainer'].update({'output_dir': str(jobs), 'tree_dir': str(output/'trees'/name), 'run_name': name})
            data['agent']['concurrency'] = 1
            # This is a wall-clock limit per independent stream. Single-task
            # iteration, episode, inference and actor budgets are unchanged.
            request = jobs/(name+'-config.json')
            atomic_json(request, {'data': data, 'base': str(config.base), 'source_fingerprints': frozen_sources})
            log = (jobs/(name+'.log')).open('w')
            handles.append(log)
            command = [sys.executable, str(Path(__file__).resolve()), str(request)]
            processes.append(subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, env={**os.environ, 'PYTHONUNBUFFERED':'1'}, start_new_session=True))
            starts.append(time.monotonic())
        atomic_json(output/'processes.json', [{'shard': s, 'pid': p.pid} for s,p in zip(shards,processes)])
        while any(p.poll() is None for p in processes):
            now = time.monotonic()
            for index,p in enumerate(processes):
                if p.poll() is not None:
                    finishes.setdefault(index,now)
            if sum(finishes.get(i,now)-start for i,start in enumerate(starts)) > budget_s:
                raise RuntimeError('aggregate training worker-time budget exhausted')
            verify_frozen(frozen_sources)
            training_progress(output, ids, shards)
            failures = [p.returncode for p in processes if p.poll() not in (None,0)]
            if failures:
                raise RuntimeError('a training shard exited before its summary: '+str(failures))
            time.sleep(3)
        verify_frozen(frozen_sources)
        if any(p.returncode for p in processes):
            raise RuntimeError('training worker failed')
        records = training_progress(output, ids, shards)
        frozen = assemble(seed, output, ids, records)
        summary = {'workers': workers, 'recorded_tasks': len(records),
                   'successful_updates': sum(r['evolver']['status']=='done' for r in records),
                   'failed_updates': sum(r['evolver']['status']=='failed' for r in records),
                   'aggregate_worker_time_limit_hours': budget_s/3600,
                   'protocol': 'independent task-local loops; deterministic union; no cross-task feedback',
                   'final_adapter': str(frozen)}
        atomic_json(output/'coevoskills/training-summary.json', summary)
        return summary, frozen
    finally:
        for p in processes:
            if p.poll() is None:
                os.killpg(p.pid, signal.SIGINT)
                try:
                    p.wait(30)
                except subprocess.TimeoutExpired:
                    os.killpg(p.pid, signal.SIGTERM)
        for handle in handles:
            handle.close()


if __name__ == '__main__':
    request = json.loads(Path(sys.argv[1]).read_text())
    verify_frozen(request['source_fingerprints'])
    trainer = Trainer(Config(request['data'], Path(request['base'])))
    try:
        trainer.train()
    finally:
        trainer._close_sandboxes()
