"""Freeze a stratified AgentDojo comparison from one recorded attack family.

Only native ID/vector/no-op checks affect eligibility. Source trajectories and
outcome scores never select groups or payloads and never enter prepared rows.
"""
from collections import Counter, defaultdict
import argparse
import hashlib
from itertools import zip_longest
import json
from pathlib import Path
import random
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from examples.agentdyn.utils.bridge import AGENTDOJO_REVISION, case_fields, validate_case
from examples.agentdyn.utils.prepare_expanded import audit_native

SUITES = ('banking', 'slack', 'travel', 'workspace')
VERSION = 'v1.2.2'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def read_collection(source, attack='tool_knowledge'):
    source = Path(source)
    cases, sources = {}, defaultdict(list)
    for suite in SUITES:
        for path in sorted((source / suite).rglob('*.json')):
            record = json.loads(path.read_text())
            if (not isinstance(record, dict) or record.get('suite_name') != suite
                    or record.get('attack_type') != attack or not record.get('injection_task_id')
                    or not record.get('injections')):
                continue
            case = {'suite': suite, **{k: record[k] for k in ('user_task_id', 'injection_task_id', 'injections')}}
            validate_case(case, benchmark='agentdojo')
            key = digest(case)
            cases[key] = case
            sources[key].append({'path': str(path.relative_to(source)),
                                 'file_sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    if {c['suite'] for c in cases.values()} != set(SUITES):
        raise ValueError('need recorded injected cases for all four suites and the selected attack family')
    return cases, sources


def quotas(total, sizes):
    """Largest-remainder allocation proportional to eligible user groups."""
    count = sum(sizes.values())
    result = {suite: total * n // count for suite, n in sizes.items()}
    remainder = total - sum(result.values())
    for suite in sorted(sizes, key=lambda s: (-(total * sizes[s] % count), s))[:remainder]:
        result[suite] += 1
    return result


def select(cases, audit, seed=20261117, train_groups=19, val_groups=18, goals_per_group=2):
    if min(train_groups, val_groups) < len(SUITES) or goals_per_group < 1:
        raise ValueError('each development split needs at least four groups and a positive goal count')
    groups = {suite: defaultdict(dict) for suite in SUITES}
    for key, case in sorted(cases.items()):
        if audit[key]['eligible']:
            groups[case['suite']][case['user_task_id']].setdefault(case['injection_task_id'], key)
    development = {suite: sorted(task for task, goals in members.items() if len(goals) >= goals_per_group)
                   for suite, members in groups.items()}
    sizes = {suite: len(tasks) for suite, tasks in development.items()}
    if any(n < 2 for n in sizes.values()):
        raise ValueError('each suite needs at least two user groups with enough eligible goals for development')
    train, val = quotas(train_groups, sizes), quotas(val_groups, sizes)
    if any(train[s] < 1 or val[s] < 1 or train[s] + val[s] > sizes[s]
           or train[s] + val[s] >= len(groups[s]) for s in SUITES):
        raise ValueError('split budgets must leave every suite represented in TRAIN, validation and TEST')
    owners = {split: {} for split in ('train', 'val', 'test')}
    for suite, tasks in development.items():
        test_only = sorted(groups[suite].keys() - tasks)
        random.Random(f'{seed}:{suite}').shuffle(tasks)
        a, b = train[suite], train[suite] + val[suite]
        for split, members in zip(owners, (tasks[:a], tasks[a:b], tasks[b:] + test_only)):
            owners[split][suite] = members
    rows, splits, assignments, selected = [], {}, [], []
    for split, by_suite in owners.items():
        split_rows = []
        # The first cascade stage covers all four domains, not just the first suite.
        for batch in zip_longest(*(by_suite[suite] for suite in SUITES)):
            for suite, task in zip(SUITES, batch):
                if task is None:
                    continue
                assignments.append({'suite': suite, 'user_task_id': task, 'split': split})
                split_rows.append({'id': f'{suite}-{task}-clean', 'suite': suite, 'user_task_id': task})
                goals = sorted(groups[suite][task])
                if split != 'test':
                    start = len(assignments) * goals_per_group % len(goals)
                    goals = [goals[(start + n) % len(goals)] for n in range(goals_per_group)]
                for goal in goals:
                    key = groups[suite][task][goal]
                    split_rows.append({'id': f'{suite}-{task}-{goal}-{key[:8]}', **cases[key]})
                    selected.append(key)
        rows.extend(case_fields(row) for row in split_rows)
        splits[split] = [row['id'] for row in split_rows]
    return rows, splits, assignments, selected


def prepare(source, output, *, attack='tool_knowledge', seed=20261117, train_groups=19, val_groups=18):
    output = Path(output)
    if output.exists():
        raise FileExistsError('choose a fresh output directory')
    cases, sources = read_collection(source, attack)
    inventory, audit = audit_native(cases, suite_names=SUITES, benchmark_version=VERSION)
    rows, splits, assignments, selected = select(cases, audit, seed, train_groups, val_groups)
    output.mkdir(parents=True)
    tasks = output / 'tasks.jsonl'
    tasks.write_text(''.join(json.dumps(row, sort_keys=True) + '\n' for row in rows))
    for split, ids in splits.items():
        (output / f'{split}.txt').write_text('\n'.join(ids) + '\n')
    manifest = {'protocol': 'AgentDojo frozen replay comparison v1; local split, not official benchmark score',
                'benchmark': 'agentdojo', 'reference_revision': AGENTDOJO_REVISION,
                'benchmark_version': VERSION, 'source_model': Path(source).name, 'attack_family': attack,
                'seed': seed, 'goals_per_development_group': 2, 'test_goals': 'all eligible recorded goals per group',
                'native_inventory': inventory, 'counts': {s: len(ids) for s, ids in splits.items()},
                'group_counts': dict(Counter(row['split'] for row in assignments)), 'groups': assignments,
                'source_cases': len(cases), 'eligible_cases': sum(row['eligible'] for row in audit.values()),
                'selected_sources': {key: sources[key] for key in selected},
                'audit_scope': 'native IDs, vectors, environment initialization and false no-op verdicts; no model outcomes',
                'tasks_sha256': hashlib.sha256(tasks.read_bytes()).hexdigest(),
                'split_sha256': {s: hashlib.sha256((output / f'{s}.txt').read_bytes()).hexdigest() for s in splits}}
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    (output / 'source-audit.json').write_text(json.dumps(audit, indent=2))
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path, help='existing upstream runs/model directory with all four suites')
    parser.add_argument('output', type=Path)
    parser.add_argument('--attack', default='tool_knowledge')
    parser.add_argument('--seed', type=int, default=20261117)
    args = parser.parse_args()
    manifest = prepare(args.source, args.output, attack=args.attack, seed=args.seed)
    print(json.dumps({k: manifest[k] for k in ('counts', 'group_counts', 'tasks_sha256', 'native_inventory')}, indent=2))
