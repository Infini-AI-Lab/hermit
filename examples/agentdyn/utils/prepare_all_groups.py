"""Extend a frozen protocol to every eligible user group, without moving any group.

Each group's split comes from the full inventory's frozen ownership. Cases of the
base protocol (for example data/thirty-v1) are kept verbatim; every group it does
not use contributes its clean case and `goals` existing frozen injection goals,
chosen by a fixed seed. --full-test adds all frozen variants in TEST-owned groups.
Reads only the inventory and split files, never model outcomes.
"""
import argparse
import hashlib
import json
from pathlib import Path
import random

from bridge import case_fields

SPLITS = ('train', 'val', 'test')


def prepare(inventory, base, output, goals=2, seed=20261102, *, full_test=False):
    inventory, base, output = Path(inventory), Path(base), Path(output)
    if type(goals) is not int or goals < 1:
        raise ValueError('goals must be a positive integer')
    if output.exists():
        raise FileExistsError(output)
    rows = [json.loads(s) for s in (inventory / 'full/tasks.jsonl').read_text().splitlines() if s.strip()]
    by_id = {r['id']: r for r in rows}
    owners = {(g['suite'], g['user_task_id']): g['split']
              for g in json.loads((inventory / 'full/manifest.json').read_text())['groups']}
    group = lambda r: (r['suite'], r['user_task_id'])
    splits = {}
    for split in SPLITS:
        selected = [i for i in (base / f'{split}.txt').read_text().splitlines() if i.strip()]
        chosen = {group(by_id[i]) for i in selected}
        if any(owners[g] != split for g in chosen):
            raise ValueError('base split ownership mismatch')
        for g in sorted(g for g, owner in owners.items() if owner == split and g not in chosen):
            members = [r for r in rows if group(r) == g]
            clean = [r for r in members if not r.get('injection_task_id')]
            by_goal = {}
            for row in sorted(members, key=lambda r: r['id']):
                if row.get('injection_task_id'):
                    by_goal.setdefault(row['injection_task_id'], row)
            keys = sorted(by_goal)
            random.Random(f'{seed}:{split}:{g}').shuffle(keys)
            if len(clean) != 1 or len(keys) < goals:
                raise ValueError(f'group {g} needs one clean case and {goals} existing goals')
            selected.extend(r['id'] for r in clean + [by_goal[k] for k in keys[:goals]])
            chosen.add(g)
        if split == "test" and full_test:
            selected_ids = set(selected)
            selected.extend(r["id"] for r in rows
                            if owners[group(r)] == "test" and r["id"] not in selected_ids)
        splits[split] = selected
    output.mkdir(parents=True)
    used = {i for ids in splits.values() for i in ids}
    (output / 'tasks.jsonl').write_text(''.join(json.dumps(case_fields(by_id[i]), sort_keys=True) + '\n' for i in sorted(used)))
    for split, ids in splits.items():
        (output / f'{split}.txt').write_text('\n'.join(ids) + '\n')
    clean = {s: sum(not by_id[i].get('injection_task_id') for i in ids) for s, ids in splits.items()}
    protocol = f'all eligible user groups: 1 clean + {goals} injected per group'
    if full_test:
        protocol += '; TEST includes all frozen variants in its owned groups'
    report = {'protocol': protocol,
              'inventory': str(inventory.resolve()), 'base': str(base.resolve()), 'seed': seed,
              'selection_uses_model_outcomes': False, 'preserved_prior_split_ownership': True,
              'counts': {s: len(ids) for s, ids in splits.items()}, 'clean': clean,
              'groups': [{'suite': s, 'user_task_id': t, 'split': owners[(s, t)]}
                         for s, t in sorted({group(by_id[i]) for i in used})],
              'tasks_sha256': hashlib.sha256((output / 'tasks.jsonl').read_bytes()).hexdigest()}
    (output / 'manifest.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('inventory', type=Path, help='prepared inventory with full/ (e.g. data/expanded-v2)')
    parser.add_argument('base', type=Path, help='protocol whose cases are kept (e.g. data/thirty-v1)')
    parser.add_argument('output', type=Path)
    parser.add_argument('--goals', type=int, default=2)
    parser.add_argument('--full-test', action='store_true',
                        help='include every frozen case in TEST-owned groups; keep TRAIN and validation unchanged')
    args = parser.parse_args()
    report = prepare(args.inventory, args.base, args.output, args.goals, full_test=args.full_test)
    print(json.dumps({k: report[k] for k in ('counts', 'clean', 'tasks_sha256')}, indent=2))
