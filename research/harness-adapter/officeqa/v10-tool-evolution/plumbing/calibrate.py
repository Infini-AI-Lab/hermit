"""The calibration round before the paid run (DESIGN §5 "Calibration before the paid run"; PREREG §4-§5).

    python calibrate.py --source-root ../v9-p1-a [--actor-runs 20] [--dry-run] [--source-tools w0|v9]

One `harness` learner session on the released evidence of a finished root (the v9-a pilot: 50 scored
episodes, a static arm, so no evidence blocks exist there) treated as ONE block: does the learner stage a
tool change, do the screens, the replay and the paired re-runs admit it? The record answers PREREG's go/no-go
question "d admits no revision in 10 blocks: pipeline or learner?" and is required by preflight.offline
before `prereg_pending` clears (preflight/calibration.json).

How it works (all controller-side; the actor and the learner never see the source root's private trees):
1. `build_scratch_root(source_root, scratch)` makes a fresh campaign-shaped root: `prepared` is a symlink to
   the source's (read-only: corpus, FTS index, tables index, manifest); `runs/episodes/NNN` are real
   directories whose `api`, `records` and `workspace` entries link to the source episode (the evidence builder
   resolves a run dir inside the root and reads the broker records through it); `evidence/01` and
   `private-learning-seals/01.json` are written as `campaign.release_block` writes them (task, sealed
   prediction, summary, public trajectory, failure fields, the official reference through
   `data.release_references`, never for a withheld issue-47 item); every learner-visible file passes
   evidence.validate_block.
2. `source_adapter(scratch, source_tools)`: the harness W0 of this tree ('w0'), or the same adapter with the v9
   tools/cli.py + otables.py and the v9 USAGE text ('v9': the tools the recorded commands ran with; the replay
   then measures a candidate against them). Either passes arms.check_adapter for harness-evolve.
3. `calibrate(...)` runs harness_learning.run_learning on that evidence with the campaign's ToolRunner factory
   (campaign.Campaign.tool_runner over the scratch layout: neutral copies under runs/code/) and a verification
   actor runner that runs real episodes through campaign.prepare_episode / execute_episode (role 'verify', run
   dirs under runs/verify/01/<token>/<NN>) at most `actor_runs` times (a run beyond the cap is recorded as
   transport-lost, so the judge marks the proposal inconclusive instead of failing the round), then writes
   `preflight/calibration.json`: proposals by kind, every screen outcome, replay status and counts, tiers,
   the verdicts, the admission record's outcome, the actor runs used and the cost (cost.estimate on the
   scratch root). No answer value enters the record.
`--dry-run` ($0): the same path with a scripted model that proposes nothing (the evidence build, the source
adapter check, the ToolRunner and the record are exercised; no actor runs, no model calls). Tests inject a
scripted model and a fake actor runner.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import getpass
import json
import os
from pathlib import Path
import shutil
import threading
import time
from typing import Callable, Mapping, Sequence

import arms
import broker
import campaign as c
import cost
import data
import evidence as EV
import harness_learning
import learner_common as LC
from harness_core import contracts as C

ROOT = c.ROOT
V9_PLUMBING = ROOT.parent / 'officeqa-pro133-v9-plumbing'
V9_TOOL_FILES = ('cli.py', 'otables.py')
ACTOR_RUNS = 20
BLOCK = 1
RECORD = 'preflight/calibration.json'


class NoProposalModel:
    """The $0 dry-run learner: ends the session without a proposal (and again after the nudge)."""
    name = LC.MODEL_NAME
    reasoning_effort = LC.REASONING_EFFORT

    def __call__(self, messages, *, tools, tool_choice=None, max_tokens=65_536):
        message = {'role': 'assistant', 'content': 'no proposal (calibration dry run)'}
        return ({'model': self.name, 'choices': [{'message': message, 'finish_reason': 'stop'}],
                 'usage': {'prompt_tokens': 0, 'completion_tokens': 0}},
                {'usage': {'prompt_tokens': 0, 'completion_tokens': 0}, 'http_status': 200, 'response_model': self.name,
                 'usage_complete': True, 'wire_attempts': 0, 'attempts': []})


def _link(target: Path, link: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(str(target.resolve()), str(link))


# The tables index the campaign roots install (run.STUDY_TABLES_INDEX, version otables.INDEX_VERSION). A finished
# v9 root's own index is v9.1 (no narrative section tables), so a calibration over it would never exercise W0-12;
# the scratch root links the source's prepared/ entry by entry and this index in place of agent/tables/.
STUDY_TABLES_INDEX = ROOT.parent / 'frameworks-v10/officeqa-tables-index/tables.sqlite'


def default_tables_index() -> Path | None:
    """The study index when it exists (the campaign's), else None (the source root's own index)."""
    return STUDY_TABLES_INDEX if STUDY_TABLES_INDEX.is_file() else None


def link_prepared(source: Path, target: Path, tables_index: Path | None = None) -> dict:
    """<scratch>/prepared: one symlink to the source's prepared/ when `tables_index` is None; otherwise a real
    directory whose entries link to the source's, except agent/tables/tables.sqlite, which links to
    `tables_index` (checked with data.tables_index_version). Returns what the record keeps."""
    if tables_index is None:
        _link(source, target)
        own = source / 'agent/tables/tables.sqlite'
        return {'tables_index': str(own.resolve()) if own.is_file() else None,
                'tables_index_version': data.tables_meta(own).get('version') if own.is_file() else None}
    tables_index = Path(tables_index).resolve()
    version = data.tables_index_version(tables_index)
    target.mkdir(parents=True)
    for child in source.iterdir():
        if child.name != 'agent':
            _link(child, target / child.name)
    agent = target / 'agent'
    agent.mkdir()
    if (source / 'agent').is_dir():
        for child in (source / 'agent').iterdir():
            if child.name != 'tables':
                _link(child, agent / child.name)
    _link(tables_index, agent / 'tables' / 'tables.sqlite')
    return {'tables_index': str(tables_index), 'tables_index_version': version}


def scored_episodes(source_root: Path) -> list[dict]:
    """The scored episodes of a finished root: runs/episodes/NNN with task, prediction and summary."""
    out = []
    runs = next((source_root / 'runs' / name for name in (c.RUNS_DIR, 'adapted') if (source_root / 'runs' / name).is_dir()),
                source_root / 'runs' / c.RUNS_DIR)
    for base in sorted(runs.glob('[0-9][0-9][0-9]')):
        if not all((base / n).is_file() for n in ('task.json', 'prediction.json', 'summary.json')):
            continue
        task = c.read(base / 'task.json')
        out.append({'ordinal': int(base.name), 'task_id': task['task_id'], 'question': task['question'], 'base': base})
    if not out:
        raise ValueError(f'{source_root} holds no scored episodes under runs/{c.RUNS_DIR}/')
    return out


def build_scratch_root(source_root: Path, scratch: Path, *, references: Mapping[str, str | None] | None = None,
                       withheld: Sequence[str] | None = None, block: int = BLOCK,
                       tables_index: Path | None = None) -> dict:
    """A campaign-shaped root over a finished root's records (docstring step 1). `references` and
    `withheld` default to the source root's prepared data (data.release_references / data.withheld_ids);
    `tables_index` (link_prepared) replaces the source's tables index by the campaign's."""
    source_root, scratch = Path(source_root).resolve(), Path(scratch)
    if scratch.exists() and any(scratch.iterdir()):
        raise ValueError(f'calibration scratch root is not empty: {scratch}')
    scratch.mkdir(parents=True, exist_ok=True)
    episodes = scored_episodes(source_root)
    ids = [e['task_id'] for e in episodes]
    if withheld is None:
        withheld = data.withheld_ids(source_root)
    withheld_set = set(withheld)
    if references is None:
        keys = data.release_references(source_root, [i for i in ids if i not in withheld_set])
    else:
        keys = {i: references.get(i) for i in ids}
    index_info = {'tables_index': None, 'tables_index_version': None}
    if (source_root / 'prepared').is_dir():
        index_info = link_prepared(source_root / 'prepared', scratch / 'prepared', tables_index)
    runs = scratch / 'runs' / c.RUNS_DIR
    out = scratch / 'evidence' / f'{block:02}'
    out.mkdir(parents=True)
    sealed, held, rows, files = [], [], [], []
    for e in episodes:
        base = runs / f'{e["ordinal"]:03}'
        base.mkdir(parents=True)
        for name in ('task.json', 'prediction.json', 'summary.json'):
            shutil.copyfile(e['base'] / name, base / name)
        for name in ('api', 'records', 'workspace'):
            if (e['base'] / name).is_dir():
                _link(e['base'] / name, base / name)
        row = {'id': e['task_id'], 'ordinal': e['ordinal'], 'task_sha256': c.sha(base / 'task.json'),
               'prediction_sha256': c.sha(base / 'prediction.json')}
        if e['task_id'] in withheld_set:
            held.append(row)
            continue
        if keys.get(e['task_id']) is None:
            raise ValueError(f'no reference for {e["task_id"]} (withheld ids must be declared)')
        sealed.append(row)
    seal_path = scratch / 'private-learning-seals' / f'{block:02}.json'
    seal = {'block': block, 'source_arm': 'calibration', 'predictions_sealed': True, 'reference_scope': 'completed_batch_only',
            'sealed_utc': c.utc(), 'episodes': sealed, 'withheld_episodes': held,
            'withheld_reason': 'databricks/officeqa#47: reference never released'}
    c.save(seal_path, seal)
    entries = []
    for row in sealed:
        base = runs / f'{row["ordinal"]:03}'
        destination = out / f'{row["ordinal"]:03}'
        destination.mkdir()
        for name in ('task.json', 'prediction.json', 'summary.json'):
            shutil.copyfile(base / name, destination / name)
        reference = destination / 'reference_answer.json'
        c.save(reference, {'task_id': row['id'], 'official_answer': keys[row['id']]})
        row['reference_answer_sha256'] = c.sha(reference)
        events = base / 'records' / 'events.jsonl'
        public_rows = []
        for line in (events.read_text().splitlines() if events.is_file() else []):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            item = event.get('item', {})
            if event.get('type') in ('turn.completed', 'turn.failed') or (
                    event.get('type') == 'item.completed' and item.get('type') in (
                    'agent_message', 'command_execution', 'mcp_tool_call', 'file_change')):
                public_rows.append(broker.public(event))
        (destination / 'trajectory.jsonl').write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in public_rows))
        summary = c.read(base / 'summary.json')
        c.save(destination / 'failure.json', {k: summary.get(k) for k in (
            'status', 'failure_kind', 'terminal_event', 'error_text', 'requests', 'wire_attempts', 'call_failures',
            'elapsed_seconds', 'agents_md_bytes', 'project_doc_max_bytes')})
        entries.append({'id': row['id'], 'ordinal': row['ordinal'], 'directory': f'{row["ordinal"]:03}',
                        'run_dir': str(base.relative_to(scratch))})
        files.append({'path': f'{row["ordinal"]:03}/', 'source': str(e['base'])})
    seal['reference_released_utc'] = c.utc()
    c.save(seal_path, seal)
    c.save(out / 'manifest.json', {'source_arm': 'calibration', 'block': block, 'episodes': entries,
                                   'withheld': [{'id': r['id'], 'ordinal': r['ordinal']} for r in held],
                                   'includes_gold': True, 'includes_grades': False, 'includes_future_questions': False,
                                   'includes_baseline_traces': False, 'includes_source_hints': False, 'files': files,
                                   'batch_seal': {'file': seal_path.name, 'sha256': c.sha(seal_path)}})
    (out / 'README.md').write_text('# Calibration evidence: the released episodes of a finished root as one block\n')
    return {'scratch': str(scratch), 'source_root': str(source_root), 'episodes': len(episodes), 'released': len(sealed),
            'withheld': len(held), 'block': block, **index_info}


def source_adapter(scratch: Path, source_tools: str = 'w0', *, pack='officeqa') -> Path:
    """The source revision of the calibration session (docstring step 2), checked for harness-evolve."""
    arm = arms.parse_arm('harness-evolve', pack=pack)
    w0 = arms.w0_dir(arm)
    target = Path(scratch) / 'calibration' / 'source'
    arms.copy_adapter(w0, target)
    if source_tools == 'v9':
        v9_tools = V9_PLUMBING / 'packs/officeqa/tools'
        for name in V9_TOOL_FILES:
            shutil.copyfile(v9_tools / name, target / arms.TOOLS_DIR / name)
        v9_pack = V9_PLUMBING / 'packs/officeqa/pack.py'
        namespace: dict = {}
        source = v9_pack.read_text(encoding='utf-8')
        start = source.index('USAGE = (')
        end = source.index('\n\n', start)
        exec(compile(source[start:end], str(v9_pack), 'exec'), namespace)     # the v9 USAGE string literal only
        (target / arms.TOOLS_DIR / C.USAGE_FILE).write_text(namespace['USAGE'].rstrip() + '\n' + arms.CALC_USAGE, encoding='utf-8')
        info = json.loads((target / 'adapter.json').read_text())
        info['revision'] = arms.revision(target)
        (target / 'adapter.json').write_text(json.dumps(info, indent=2, sort_keys=True) + '\n')
    elif source_tools != 'w0':
        raise ValueError("source_tools is 'w0' or 'v9'")
    arms.check_adapter(target, arm)
    return target


def capped_actor_runner(lay: c.Layout, arm: C.ArmSpec, key: str, *, actor_runs: int, tasks: Mapping[str, dict],
                        temperature: float = 0) -> Callable:
    """learner_common.ActorRunner over the scratch layout: real verification episodes through the scored
    episode path, at most `actor_runs` of them (beyond the cap: transport-lost, never a model call)."""
    count = {'started': 0}
    lock = threading.Lock()

    def actor_runner(adapter, task, run_dir):
        with lock:
            if count['started'] >= actor_runs:
                return {'status': 'calibration_budget_exhausted', 'answer': '', 'transport_failed': True}
            count['started'] += 1
        task_id = task['task_id']
        if task_id not in tasks:
            raise ValueError('verification is limited to the released questions of the calibration block')
        arms.check_adapter(Path(adapter), arm)
        base = Path(run_dir)
        arms.check_neutral(base.resolve().relative_to(lay.root.resolve().parent), 'verification run path', arm)
        return c.episode(key, lay, arm, tasks[task_id], Path(adapter), base, ordinal=task['ordinal'], role='verify',
                         temperature=temperature, top_p=None)
    actor_runner.count = count
    return actor_runner


def calibrate(source_root: Path, scratch: Path, *, key: str = '', actor_runs: int = ACTOR_RUNS, model=None,
              actor_runner=None, tool_runner=None, corpus=None, grader=None, pack=None, exclude=None,
              references=None, withheld=None, source_tools: str = 'w0', out: Path | None = None,
              stop: threading.Event | None = None, tables_index: Path | None = None) -> dict:
    """Docstring step 3. Returns the calibration record (also written to `out`). `tables_index`: the index the
    scratch root serves instead of the source's own (link_prepared; the CLI defaults to the study index)."""
    scratch = Path(scratch).resolve()
    started = time.monotonic()
    built = build_scratch_root(source_root, scratch, references=references, withheld=withheld, tables_index=tables_index)
    pack_name = getattr(pack, 'name', 'officeqa')
    arm = arms.parse_arm('harness-evolve', pack=pack_name)
    source = source_adapter(scratch, source_tools, pack=pack_name) if pack is None or pack_name == 'officeqa' \
        else arms.build_w0('harness', pack, scratch / 'calibration' / 'source')
    lay = c.Layout(root=scratch, code=ROOT)
    spec = arms.CampaignSpec(arm=arm, repeat=1, pack=pack_name, order_seed='calibration', root=scratch.name, barrier=None)
    campaign_ = c.Campaign(key, spec, lay)
    tasks = {e['task_id']: {'task_id': e['task_id'], 'question': e['question']} for e in scored_episodes(source_root)}
    runner = actor_runner or capped_actor_runner(lay, arm, key, actor_runs=actor_runs, tasks=tasks)
    # the source adapter lives under <scratch>/calibration/source (step 2), which a campaign's runner factory does
    # not know; the paid calibration of 2026-09-29 failed every replay on exactly this refusal
    factory = tool_runner or campaign_.tool_runner(BLOCK, extra_roots=(source.parent,))
    if pack is None:
        pack = arms.load_pack('officeqa', lay.corpus.resolve(), lay.tables_index.resolve())
    exclude = tuple(data.withheld_ids(source_root) if exclude is None else exclude)
    output = scratch / 'evolution' / f'{BLOCK:02}'          # the campaign layout, so cost.estimate and the audit find it
    result = harness_learning.run_learning(key, source, scratch / 'evidence' / f'{BLOCK:02}', output, BLOCK, stop,
                                           actor_runner=runner, tool_runner=factory, pack=pack,
                                           grader=grader or source_grader(source_root), model=model or NoProposalModel(),
                                           corpus=corpus, root=scratch, w0=source, exclude=exclude, arm=arm,
                                           verify_dir=lay.verify_runs(BLOCK))
    control = output / 'sessions' / 'learner' / 'control'
    screens = json.loads((output / 'screen.json').read_text()) if (output / 'screen.json').is_file() else []
    admission = c.read(control / 'mechanical-admission.json') if (control / 'mechanical-admission.json').is_file() else None
    verification = c.read(output / 'verification.json') if (output / 'verification.json').is_file() else None
    record = {
        'kind': 'calibration', 'created_utc': c.utc(), 'source_root': str(Path(source_root).resolve()),
        'scratch': str(scratch), 'block': built, 'source_tools': source_tools,
        'source_revision': arms.revision(source), 'arm': asdict(arm), 'dry_run': model is None,
        'model': getattr(model or NoProposalModel(), 'name', None), 'status': result.get('status'),
        'learner_status': result.get('learner_status'), 'error': result.get('error'),
        'proposals': {'by_kind': _count_by(screens, 'kind'), 'accepted': [s for s in screens if s.get('ok') and s.get('kind') != 'stage'],
                      'screen_rejections': [{'kind': s.get('kind'), 'id': s.get('id'), 'reasons': s.get('reasons')}
                                            for s in screens if not s.get('ok')],
                      'stage_calls': sum(1 for s in screens if s.get('kind') == 'stage'),
                      'staged_ok': sum(1 for s in screens if s.get('kind') == 'stage' and s.get('ok'))},
        'replays': [{'id': s.get('id'), 'status': s.get('replay_status'), 'counts': s.get('replay_counts'),
                     'tier': s.get('tier'), 'seconds': s.get('replay_seconds')} for s in screens if s.get('replay_status')],
        'admission': ({'admitted': admission.get('admitted'), 'admitted_notes': admission.get('admitted_notes'),
                       'admitted_revision': admission.get('admitted_revision'),
                       'verification': admission.get('verification'), 'replay_seconds': admission.get('replay_seconds'),
                       'try_tool_calls': admission.get('try_tool_calls'),
                       'revision': ({k: (admission['revision'] or {}).get(k) for k in ('id', 'kind', 'tier', 'files', 'controls',
                                                                                     'admitted', 'verification')}
                                    if admission.get('revision') else None)} if admission else None),
        'actor_runs': {'cap': actor_runs, 'used': getattr(runner, 'count', {}).get('started', None),
                       'recorded': (verification or {}).get('done')},
        'tokens': {'learner': result.get('usage')},
        'cost': _calibration_cost(scratch),
        'elapsed_seconds': round(time.monotonic() - started, 3),
    }
    target = Path(out) if out is not None else ROOT / RECORD
    c.save(target, record)
    record['path'] = str(target)
    return record


def source_grader(source_root: Path) -> EV.Grader:
    """The controller-side grader of the source root: its pinned prepared/private/reward.py (campaign.grader)."""
    return EV.Grader(score=data.official_scorer(Path(source_root)), name='prepared/private/reward.py (pinned sha)')


def _calibration_cost(scratch: Path) -> dict:
    """The paid part of a calibration: its verification actors (runs/verify/) and the learner session; the
    source root's own episodes (linked under runs/episodes/) are not this round's spend."""
    estimate = cost.estimate(scratch)
    groups = estimate['groups']
    return {'kind': estimate['kind'], 'verification': estimate['verification'], 'learners': groups['learners'],
            'replay_seconds': estimate['replay_seconds'],
            'note': 'verification actors + learner calls of this calibration only (the linked source episodes are excluded)'}


def _count_by(rows: Sequence[Mapping], key: str) -> dict:
    counts: dict = {}
    for row in rows:
        counts[str(row.get(key))] = counts.get(str(row.get(key)), 0) + 1
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--source-root', type=Path, required=True, help='a finished root (the v9-a pilot)')
    parser.add_argument('--scratch', type=Path, default=None, help='scratch root (default preflight/calibration-<stamp>)')
    parser.add_argument('--actor-runs', type=int, default=ACTOR_RUNS)
    parser.add_argument('--source-tools', choices=('w0', 'v9'), default='w0')
    parser.add_argument('--dry-run', action='store_true', help='$0: a scripted learner that proposes nothing')
    parser.add_argument('--tables-index', type=Path, default=default_tables_index(),
                        help=f'the tables index the scratch root serves (default: the study index {STUDY_TABLES_INDEX} '
                             'when it exists, else the source root\'s own); "none" keeps the source root\'s own')
    args = parser.parse_args()
    scratch = args.scratch or ROOT / 'preflight' / f'calibration-{c.stamp()}'
    key = '' if args.dry_run else (os.environ.get('FIREWORKS_API_KEY') or getpass.getpass('Fireworks API key (hidden): '))
    tables_index = None if args.tables_index is None or str(args.tables_index) == 'none' else args.tables_index
    record = calibrate(args.source_root, scratch, key=key, actor_runs=args.actor_runs,
                       model=None if args.dry_run else LC.FireworksModel(key), source_tools=args.source_tools,
                       tables_index=tables_index)
    print(json.dumps({k: record[k] for k in ('status', 'proposals', 'replays', 'admission', 'actor_runs', 'path')}
                     | {'tables_index': record['block'].get('tables_index'),
                        'tables_index_version': record['block'].get('tables_index_version')},
                     ensure_ascii=False, indent=2))
    raise SystemExit(0 if record['status'] not in (None, 'failed') else 1)


if __name__ == '__main__':
    main()
