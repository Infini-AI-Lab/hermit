"""Label-free pilot question list: the first 50 questions of the canonical OfficeQA Pro order. $0, offline.

Rule (the user's pilot decision of 2026-09-28, fixed before any pilot call): the pilot runs the FIRST 50 task
ids of the canonical base order. That is the order of the prepared manifest used by v3-v8 (and rebuilt by
v9's `data.py prepare`): ascending SHA-256(UTF-8(seed NUL uid)), uid tie-break, seed SEED
('metaworld-officeqa-pro-codex70-online-v1', data.SEED). `officeqa_prepare.select_rows` sorts every row by
that key and cuts a prefix, so the first 50 of the 133-question order are exactly the historic 50-question
set of the v4-v7 runs.

What it reads, and nothing else:
- the v8 none root's `prepared/manifest.json`: only its `task_ids` (the base order), `selection.seed` and
  `sample_count` (the manifest holds no answer, only the answer file's hash);
- every scored v8 run's `task.json` (arms none, prompt, cards): only `task_id` and `ordinal`;
- cross-check only: the `task.json` (`task_id`, `ordinal`) of the historic 50-question roots that are on disk.
It never opens predictions, final answers, summaries, grades, evaluation files or reference answers
(tests/test_v9_pilot.py runs it with those files unreadable and an audit hook that records every open).

Checks (a failure raises): the manifest order equals the recomputed seeded order and every v8 arm's
ordinal order; every historic 50-question root on disk ran exactly the first 50 at ordinals 1-50.

Output: pilot/first50.json = {name, task_ids (ascending), ordinals (base-order positions, aligned with
task_ids), sha256 = arms.subset_sha256(task_ids), rule, count, questions_in_base_order, historic_50_matches,
inputs, script_sha256}. Its `name`, `task_ids` and `sha256` are the `.task-paths.json` "subset" of every
pilot root.

    python pilot/select_first50.py [--project DIR] [--out pilot/first50.json] [--check] [--overlap]

`--overlap` prints, as counts only, how many of the 50 are in the loop-prone eligible set of
pilot/select_loop_prone.py (it reads the v8 command strings for that; the list itself does not depend on it).
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
PLUMBING = HERE.parent
if str(PLUMBING) not in sys.path:
    sys.path.insert(0, str(PLUMBING))

NAME = 'pilot-first50'
COUNT = 50
SEED = 'metaworld-officeqa-pro-codex70-online-v1'          # == data.SEED (a test pins the equality)
MANIFEST = 'officeqa-pro133-none-v8-work/prepared/manifest.json'
# v8 arm -> its scored run directory (the same three as pilot/select_loop_prone.py)
V8_RUNS = {'none': 'officeqa-pro133-none-v8-work/runs/baseline',
           'prompt': 'officeqa-pro133-prompt-v8-work/runs/adapted',
           'cards': 'officeqa-pro133-cards-v8-work/runs/adapted'}
# historic 50-question runs (cross-check only; absent directories are skipped)
HISTORIC_50 = {'v4-gold': 'officeqa-codex50-gold-v4-work/runs/baseline',
               'v5-mwm': 'officeqa-codex50-full-mwm-v5-work/runs/baseline',
               'v6f-mwm': 'officeqa-codex50-full-mwm-v6f-work/runs/baseline',
               'v6f-prompt': 'officeqa-codex50-full-prompt-only-v6f-work/runs/baseline',
               'v7-cards': 'officeqa-codex50-cards-v7-work/runs/baseline'}
OUT = HERE / 'first50.json'


def subset_sha256(task_ids) -> str:
    """== arms.subset_sha256 (kept import-free here; a test pins the equality)."""
    return hashlib.sha256(json.dumps(list(task_ids), separators=(',', ':')).encode('utf-8')).hexdigest()


def seeded_order(task_ids, seed: str = SEED) -> list[str]:
    """officeqa_prepare.select_rows' key: ascending SHA-256(seed NUL uid), uid tie-break."""
    return sorted(task_ids, key=lambda uid: (hashlib.sha256((seed + '\0' + uid).encode()).hexdigest(), uid))


def run_ids(directory: Path) -> dict[int, str]:
    """{ordinal: task_id} of one run directory (task.json only)."""
    table: dict[int, str] = {}
    for run in sorted(p for p in Path(directory).iterdir() if p.is_dir() and p.name.isdigit()):
        value = json.loads((run / 'task.json').read_text(encoding='utf-8'))
        task_id, ordinal = value.get('task_id'), value.get('ordinal')
        if not isinstance(task_id, str) or not task_id or isinstance(ordinal, bool) or not isinstance(ordinal, int):
            raise ValueError(f'{run}/task.json: task_id/ordinal missing')
        if ordinal in table:
            raise ValueError(f'{directory}: ordinal {ordinal} twice')
        table[ordinal] = task_id
    return table


def base_order(project: Path) -> tuple[list[str], dict]:
    """The canonical base order, from the v8 manifest, checked against the seed and every v8 arm."""
    project = Path(project)
    manifest = json.loads((project / MANIFEST).read_text(encoding='utf-8'))
    order, seed = manifest.get('task_ids'), (manifest.get('selection') or {}).get('seed')
    if seed != SEED or not isinstance(order, list) or len(order) != manifest.get('sample_count'):
        raise ValueError('the v8 manifest is not the canonical 133-question preparation')
    if seeded_order(order) != order:
        raise ValueError('the v8 manifest order is not the seeded order')
    for arm, relative in V8_RUNS.items():
        table = run_ids(project / relative)
        if [table.get(k) for k in range(1, len(order) + 1)] != order or len(table) != len(order):
            raise ValueError(f'v8 arm {arm}: ordinals disagree with the manifest order')
    return order, {'seed': seed, 'questions': len(order)}


def select(project: Path, *, count: int = COUNT) -> dict:
    project = Path(project)
    order, meta = base_order(project)
    if len(order) < count:
        raise ValueError(f'only {len(order)} questions in the base order')
    first = order[:count]
    matches = []
    for name, relative in HISTORIC_50.items():
        directory = project / relative
        if not directory.is_dir():
            continue
        table = run_ids(directory)
        if [table.get(k) for k in range(1, count + 1)] != first or len(table) != count:
            raise ValueError(f'historic 50-question root {name} did not run the first {count}')
        matches.append(name)
    position = {task_id: k for k, task_id in enumerate(order, start=1)}
    task_ids = sorted(first)
    return {
        'name': NAME, 'task_ids': task_ids, 'ordinals': [position[t] for t in task_ids],
        'sha256': subset_sha256(task_ids), 'count': count,
        'rule': {'text': (f'the first {count} task ids of the canonical base order: ascending '
                          f'sha256("{SEED}" NUL uid), uid tie-break (the v3-v8 prepared manifest order; the '
                          'historic 50-question set of v4-v7)'),
                 'seed': SEED, 'count': count, 'labels_used': False, 'behaviour_used': False},
        'questions_in_base_order': meta['questions'],
        'historic_50_matches': matches,
        'inputs': {'read': [f'{MANIFEST}: task_ids, selection.seed, sample_count',
                            'runs/<v8 arm>/NNN/task.json: task_id, ordinal',
                            'historic 50-question runs/baseline/NNN/task.json: task_id, ordinal (cross-check)'],
                   'never_read': ['prediction.json', 'final.md', 'summary.json', 'records/ (for the list)',
                                  'private-evaluation/', 'prepared/private/', 'reference answers', 'grades'],
                   'v8_runs': dict(V8_RUNS), 'historic_50': dict(HISTORIC_50)},
        'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }


def overlap(project: Path, task_ids) -> dict:
    """Counts only: how many of `task_ids` are loop-prone eligible (pilot/select_loop_prone.py's rule)."""
    try:
        from pilot import select_loop_prone as SL
    except ImportError:                              # pragma: no cover (run as a script)
        import select_loop_prone as SL
    table = SL.scan(Path(project))
    looped = {t: sum(k >= SL.THRESHOLD for k in e['max_repeat'].values()) for t, e in table.items()}
    eligible = {t for t, n in looped.items() if n >= 1}
    chosen = set(task_ids)
    draft = json.loads((HERE / 'loop_prone_20.json').read_text())['task_ids']
    return {'first50_in_eligible': len(chosen & eligible), 'eligible_total': len(eligible),
            'questions_scanned': len(table),
            'first50_by_arms_looped': {str(k): v for k, v in sorted(Counter(looped[t] for t in chosen).items())},
            'loop_prone_20_in_first50': len(chosen & set(draft))}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--project', type=Path, default=PLUMBING.parent, help='directory holding the v8 roots')
    parser.add_argument('--out', type=Path, default=OUT)
    parser.add_argument('--check', action='store_true', help='re-derive and compare with --out; write nothing')
    parser.add_argument('--overlap', action='store_true', help='also print loop-prone overlap counts')
    args = parser.parse_args(argv)
    result = select(args.project)
    extra = {'overlap': overlap(args.project, result['task_ids'])} if args.overlap else {}
    if args.check:
        same = args.out.is_file() and json.loads(args.out.read_text())['sha256'] == result['sha256']
        print(json.dumps({'sha256': result['sha256'], 'matches_file': same,
                          'historic_50_matches': result['historic_50_matches'], **extra}))
        return 0 if same else 1
    if args.out.exists():
        raise SystemExit(f'{args.out} exists; use --check to compare (the list is part of the PREREG addendum)')
    args.out.write_text(json.dumps(result, indent=1) + '\n')
    print(json.dumps({'written': str(args.out), 'sha256': result['sha256'],
                      'historic_50_matches': result['historic_50_matches'], **extra}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
