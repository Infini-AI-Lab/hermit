"""Extend a frozen protocol to 30 cases/split without moving any user group.

Reads only the existing task inventory and split manifests, never model outcomes.
Retains old cases verbatim; adds frozen payloads from already-owned groups.
"""
import argparse
import hashlib
import json
from pathlib import Path
import random

from bridge import case_fields

SUITES = ('shopping', 'github', 'dailylife')
SPLITS = ('train', 'val', 'test')


def prepare(source, output, seed=20261102):
    source, output = Path(source), Path(output)
    if output.exists():
        raise FileExistsError(output)
    rows = [json.loads(s) for s in (source / 'full/tasks.jsonl').read_text().splitlines()]
    by_id = {r['id']: r for r in rows}
    manifest = json.loads((source / 'full/manifest.json').read_text())
    owners = {(g['suite'], g['user_task_id']): g['split'] for g in manifest['groups']}
    group = lambda r: (r['suite'], r['user_task_id'])
    splits = {}
    for index, split in enumerate(SPLITS):
        selected = (source / f'{split}.txt').read_text().splitlines()
        chosen = {group(by_id[i]) for i in selected}
        if any(owners[g] != split for g in chosen):
            raise ValueError('source split ownership mismatch')
        for s_index, suite in enumerate(SUITES):
            target = 4 if s_index == index else 3
            available = sorted(g for g, owner in owners.items() if owner == split and g[0] == suite and g not in chosen)
            random.Random(f'{seed}:{split}:{suite}').shuffle(available)
            needed = target - sum(g[0] == suite for g in chosen)
            if needed < 0 or len(available) < needed:
                raise ValueError(f'insufficient untouched groups in {split}/{suite}')
            for g in available[:needed]:
                members = [r for r in rows if group(r) == g]
                clean = [r for r in members if not r.get('injection_task_id')]
                goals = {}
                for row in sorted(members, key=lambda r: r['id']):
                    if row.get('injection_task_id'):
                        goals.setdefault(row['injection_task_id'], row)
                keys = sorted(goals)
                random.Random(f'{seed}:{split}:{g}').shuffle(keys)
                if len(clean) != 1 or len(keys) < 2:
                    raise ValueError('group needs one clean and two existing goals')
                selected.extend(r['id'] for r in clean + [goals[k] for k in keys[:2]])
                chosen.add(g)
        if len(selected) != 30 or len(chosen) != 10:
            raise ValueError('expected 30 cases from 10 independent user groups')
        splits[split] = selected
    output.mkdir(parents=True)
    used = {i for ids in splits.values() for i in ids}
    (output / 'tasks.jsonl').write_text(''.join(json.dumps(case_fields(by_id[i]), sort_keys=True) + '\n' for i in sorted(used)))
    for split, ids in splits.items():
        (output / f'{split}.txt').write_text('\n'.join(ids) + '\n')
    report = {'protocol': '30-case grouped frozen replay: 10 clean + 20 injected per split',
              'source': str(source.resolve()), 'seed': seed, 'selection_uses_model_outcomes': False,
              'counts': {s: len(ids) for s, ids in splits.items()},
              'groups': [{'suite': s, 'user_task_id': t, 'split': owners[(s, t)]}
                         for s, t in sorted({group(by_id[i]) for i in used})],
              'preserved_prior_split_ownership': True,
              'tasks_sha256': hashlib.sha256((output / 'tasks.jsonl').read_bytes()).hexdigest()}
    (output / 'manifest.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    print(json.dumps(prepare(args.source, args.output), indent=2))
