"""Label-free choice of the pilot's 20 loop-prone questions (PREREG §7, wave 3). $0, offline.

Rule (behaviour only, fixed before any pilot call):
1. A v8 run *looped* when it issued one identical shell command at least THRESHOLD (3) times. "Identical"
   = the Codex `command_execution` item's command string after whitespace collapsing
   (harness_core.ledger.normalize_command, the loop guard's normaliser); each completed item counts once.
2. A question is eligible when its scored run looped in at least one v8 arm (none, prompt, cards).
3. Eligible questions are ranked by the number of v8 arms whose run looped (more first), then by
   sha256(SALT NUL task_id) (ascending; a fixed hash, so no human choice); the first COUNT (20) are taken.

What it reads, and nothing else: for every scored v8 run, `runs/<arm dir>/NNN/task.json` (only its
`task_id` and `ordinal`) and `runs/<arm dir>/NNN/records/events.jsonl` (only the `command` of completed
`command_execution` items). It never opens predictions, final answers, summaries, grades, evaluation files,
reference answers or any index.json (tests/test_v9_pilot.py runs it with those files unreadable and an
audit hook that records every file it opens).

Output: pilot/loop_prone_20.json = {name, task_ids (ascending), ordinals (v8 base-order positions, aligned
with task_ids), sha256 = arms.subset_sha256(task_ids), rule, ranking (behaviour numbers only), inputs}.
The list's `name`, `task_ids` and `sha256` are the `.task-paths.json` "subset" of every pilot root.

    python pilot/select_loop_prone.py [--project DIR] [--out pilot/loop_prone_20.json] [--check]
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
sys.path.insert(0, str(PLUMBING))

from harness_core.ledger import normalize_command  # noqa: E402

NAME = 'pilot-loop-prone-20'
COUNT = 20
THRESHOLD = 3
SALT = 'v9-officeqa-pilot-loop-prone'
# v8 arm -> its scored run directory, relative to the project directory (read-only)
V8_RUNS = {'none': 'officeqa-pro133-none-v8-work/runs/baseline',
           'prompt': 'officeqa-pro133-prompt-v8-work/runs/adapted',
           'cards': 'officeqa-pro133-cards-v8-work/runs/adapted'}
OUT = HERE / 'loop_prone_20.json'


def subset_sha256(task_ids) -> str:
    """== arms.subset_sha256 (kept import-free here; a test pins the equality)."""
    return hashlib.sha256(json.dumps(list(task_ids), separators=(',', ':')).encode('utf-8')).hexdigest()


def tie_hash(task_id: str) -> str:
    return hashlib.sha256(f'{SALT}\0{task_id}'.encode('utf-8')).hexdigest()


def run_commands(events: Path) -> Counter:
    """Normalised command -> times issued in one run (completed command_execution items only)."""
    counts: Counter = Counter()
    if not events.is_file():
        return counts
    with events.open(encoding='utf-8', errors='replace') as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            item = event.get('item') if isinstance(event, dict) else None
            if (event.get('type') == 'item.completed' and isinstance(item, dict)
                    and item.get('type') == 'command_execution' and isinstance(item.get('command'), str)):
                counts[normalize_command(item['command'])] += 1
    return counts


def _task_identity(task_json: Path) -> tuple[str, int]:
    value = json.loads(task_json.read_text(encoding='utf-8'))
    task_id, ordinal = value.get('task_id'), value.get('ordinal')
    if not isinstance(task_id, str) or not task_id or isinstance(ordinal, bool) or not isinstance(ordinal, int):
        raise ValueError(f'{task_json}: task_id/ordinal missing')
    return task_id, ordinal


def scan(project: Path, runs: dict[str, str] | None = None) -> dict[str, dict]:
    """{task_id: {'ordinal': n, 'max_repeat': {arm: k}}} over every scored v8 run."""
    project = Path(project)
    table: dict[str, dict] = {}
    for arm, relative in (runs or V8_RUNS).items():
        directory = project / relative
        if not directory.is_dir():
            raise ValueError(f'v8 run directory missing: {directory}')
        for run in sorted(p for p in directory.iterdir() if p.is_dir() and p.name.isdigit()):
            task_id, ordinal = _task_identity(run / 'task.json')
            counts = run_commands(run / 'records/events.jsonl')
            entry = table.setdefault(task_id, {'ordinal': ordinal, 'max_repeat': {}})
            if entry['ordinal'] != ordinal:
                raise ValueError(f'{task_id}: v8 arms disagree on its base-order ordinal')
            if arm in entry['max_repeat']:
                raise ValueError(f'{task_id}: two scored runs in v8 arm {arm}')
            entry['max_repeat'][arm] = max(counts.values(), default=0)
    return table


def select(table: dict[str, dict], *, count: int = COUNT, threshold: int = THRESHOLD) -> dict:
    ranking = []
    for task_id, entry in table.items():
        looped = sorted(arm for arm, k in entry['max_repeat'].items() if k >= threshold)
        if looped:
            ranking.append({'task_id': task_id, 'ordinal': entry['ordinal'], 'arms_looped': len(looped),
                            'looped_in': looped, 'max_repeat': dict(sorted(entry['max_repeat'].items())),
                            'tie_hash': tie_hash(task_id)})
    ranking.sort(key=lambda r: (-r['arms_looped'], r['tie_hash']))
    if len(ranking) < count:
        raise ValueError(f'only {len(ranking)} eligible (looping) questions; the rule needs {count}')
    chosen = sorted(ranking[:count], key=lambda r: r['task_id'])
    task_ids = [r['task_id'] for r in chosen]
    return {
        'name': NAME, 'task_ids': task_ids, 'ordinals': [r['ordinal'] for r in chosen],
        'sha256': subset_sha256(task_ids),
        'rule': {'text': ('scored v8 run issued one identical (whitespace-normalised) shell command >= '
                          f'{threshold} times in any v8 arm; rank by number of v8 arms that looped (desc), then '
                          f'sha256("{SALT}" NUL task_id) (asc); take the first {count}'),
                 'threshold': threshold, 'count': count, 'salt': SALT, 'v8_arms': sorted(V8_RUNS),
                 'normaliser': 'harness_core.ledger.normalize_command', 'labels_used': False},
        'eligible': len(ranking), 'questions_scanned': len(table),
        'arms_looped_histogram': {str(k): v for k, v in sorted(Counter(r['arms_looped'] for r in ranking).items())},
        'ranking': ranking[:count],
        'inputs': {'read': ['runs/<arm>/NNN/task.json: task_id, ordinal',
                            'runs/<arm>/NNN/records/events.jsonl: command of completed command_execution items'],
                   'never_read': ['prediction.json', 'final.md', 'summary.json', 'private-evaluation/',
                                  'prepared/private/', 'reference answers', 'grades', 'index.json'],
                   'v8_runs': dict(V8_RUNS)},
        'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--project', type=Path, default=PLUMBING.parent, help='directory holding the v8 roots')
    parser.add_argument('--out', type=Path, default=OUT)
    parser.add_argument('--check', action='store_true', help='re-derive and compare with --out; write nothing')
    args = parser.parse_args(argv)
    result = select(scan(args.project))
    text = json.dumps(result, indent=1) + '\n'
    if args.check:
        same = args.out.is_file() and json.loads(args.out.read_text())['sha256'] == result['sha256']
        print(json.dumps({'sha256': result['sha256'], 'matches_file': same}))
        return 0 if same else 1
    if args.out.exists():
        raise SystemExit(f'{args.out} exists; use --check to compare (the list is part of the PREREG addendum)')
    args.out.write_text(text)
    print(json.dumps({'written': str(args.out), 'sha256': result['sha256'], 'eligible': result['eligible'],
                      'arms_looped_histogram': result['arms_looped_histogram']}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
