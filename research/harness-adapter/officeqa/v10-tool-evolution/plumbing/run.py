"""Explicit paid entry point for one v10 root; importing other modules never starts inference.

    python run.py smoke [--name smoke-01]           paid (~cents): one synthetic marker task through this
                                                     root's full episode path (kit, broker, controller on the
                                                     harness arms; submit only when the gate is on)
    python run.py run                                paid: the formal campaign (needs passing readiness)
    python run.py resume [--clear-abort]             paid: block-level resume after an ABORT or a crash
    python run.py audit | cost | report              offline
    python run.py roots [--parent DIR] [--prefix v10-r1] [--no-subset] [--temperature T] [--top-p P]
                                                     $0, no model call: create the five neutral-named roots
                                                     of DESIGN §7 (plain, prompt-evolve, harness-static,
                                                     harness-evolve, harness-evolve-notes) next to this
                                                     plumbing, on the first-50 subset by default; see
                                                     pilot/README.md

A formal run requires preflight/readiness.json (preflight.py) with passing offline checks and a passing
smoke, unchanged source hashes, and the PREREG.md hash recorded there (a root whose PREREG.md differs from
the hashed one is refused, DESIGN §7).
"""
import argparse
import getpass
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import threading

import campaign as c


def fixture_root(root: Path) -> c.Layout:
    """A one-document synthetic corpus with its FTS5 index and tables index (no benchmark data)."""
    corpus = root / 'prepared/agent/corpus'
    corpus.mkdir(parents=True)
    (corpus / 'treasury_bulletin_2000_01.txt').write_text(
        'Synthetic transport test. There are no benchmark questions here.\n\n'
        'Table T-1. - Synthetic fixture table\n\n| Fiscal year | Amount |\n|---|---|\n| 1999 | 1,234 |\n| 2000 | 2,345 |\n')
    c.data._tools.build_index(corpus, root / 'prepared/agent/corpus.sqlite', expected_documents=1)
    from packs.officeqa.tools import otables
    otables.build_index(corpus, root / 'prepared/agent/tables/tables.sqlite')
    return c.Layout(root=root, code=c.ROOT)


def smoke(key, name, arm_names=None, *, base=None, broker_extra=None):
    """Paid (~cents per arm) against Fireworks. Tests run it at $0 with `base` (a scratch directory) and
    `broker_extra={'upstream_url': <local fake upstream>}`. A gate-on arm must submit at least once; a gate-off
    controller arm must not have a submit route at all (DESIGN §3: submit iff gate)."""
    import arms
    sources = {n: c.sha(c.ROOT / n) for n in ('acting.py', 'broker.py', 'campaign.py', 'controller_runtime.py',
                                              'kit.py', 'rate_control.py', 'run.py')}
    base_root = Path(base or c.ROOT / 'preflight') / name
    base_root.mkdir(parents=True, exist_ok=False)
    lay = fixture_root(base_root)
    marker = 'TRANSPORT_' + secrets.token_hex(6)
    specs = [arms.parse_arm(n) for n in arm_names] if arm_names else [c.CAMPAIGN.arm]
    if not specs or any(spec is None for spec in specs):
        raise ValueError('smoke needs arm names or this root\'s campaign arm')
    # this root's declared temperature and top_p, so a root's smoke exercises exactly its broker setting
    temperature = c.CAMPAIGN.temperature if c.CAMPAIGN is not None else arms.DEFAULT_TEMPERATURE
    top_p = c.CAMPAIGN.top_p if c.CAMPAIGN is not None else arms.DEFAULT_TOP_P
    results = []
    for arm in specs:
        base = base_root / arm.name
        question = ('This is a synthetic interface test. Use the shell to read note.txt in your workspace. '
                    'Return only the exact string inside note.txt as your final response.')
        if arm.gate:
            question += ' Before your final response, run: submit --answer "<that string>".'
        task = {'task_id': 'SMOKE', 'question': question}
        adapter = arms.w0_dir(arm, lay.adapters)
        prep = c.prepare_episode(lay, arm, task, adapter, base, ordinal=1, role='smoke')
        (Path(prep['workspace']) / 'note.txt').write_text(marker + '\n')
        summary = c.execute_episode(key, lay, arm, prep, broker_extra=broker_extra, temperature=temperature,
                                    top_p=top_p)
        native = summary.get('native_codex_items') or {}
        calls = [c.read(p) for p in sorted((base / 'api').glob('[0-9][0-9][0-9]/transport.json'))]
        reasoning = [(call.get('changes') or {}).get('reasoning_forwarded') for call in calls
                     if call.get('origin', 'codex') == 'codex']
        reasoning_ok = bool(reasoning) and all((r or {}).get('effort') == 'high' for r in reasoning)
        counters = summary.get('controller') or {}
        if arm.gate:
            controller_ok = (counters.get('submits') or 0) >= 1 and summary.get('fallback_answer') == marker
        elif arm.controller:
            controller_ok = (not (counters.get('submits') or 0) and summary.get('fallback_answer') is None
                             and counters.get('submit_enabled') is not True)
        else:
            controller_ok = summary.get('controller') is None
        # the declared sampling reached every upstream body (a top_p key only when one is declared)
        bodies = [c.read(p) for p in sorted((base / 'api').glob('[0-9][0-9][0-9]/request.json'))]
        sampling_ok = bool(bodies) and all(b.get('temperature') == temperature and ('top_p' in b) == (top_p is not None)
                                           and b.get('top_p') == top_p for b in bodies)
        passed = (summary['status'] == 'completed' and summary['answer'] == marker and summary['kit_intact']
                  and native.get('command_execution', 0) >= 1 and not summary.get('transport_failed')
                  and reasoning_ok and controller_ok and sampling_ok)
        results.append({'arm': arm.name, 'gate': arm.gate, 'passed': passed, 'summary': summary,
                        'reasoning_ok': reasoning_ok, 'controller_ok': controller_ok, 'sampling_ok': sampling_ok})
    result = {'passed': all(r['passed'] for r in results), 'results': results, 'source_hashes': sources,
              'model': c.MODEL, 'benchmark_questions': 0, 'temperature': temperature, 'top_p': top_p,
              'purpose': 'Actual native shell (+ submit on gate arms) round-trip through Fireworks Responses'}
    result['sources_unchanged'] = all(c.sha(c.ROOT / n) == h for n, h in sources.items())
    result['passed'] = result['passed'] and result['sources_unchanged']
    c.save(base_root / 'verification.json', result)
    print(json.dumps({'passed': result['passed'], 'arms': [(r['arm'], r['passed']) for r in results]}, indent=2))
    return result


def require_readiness():
    checks = c.ROOT / 'preflight/readiness.json'
    if not checks.is_file() or not c.read(checks).get('passed'):
        raise ValueError('Formal campaign requires recorded passing readiness checks (preflight.py)')
    readiness = c.read(checks)
    if not readiness.get('source_hashes') or any(c.sha(c.ROOT / name) != digest
                                                 for name, digest in readiness['source_hashes'].items()):
        raise ValueError('Experiment source changed after readiness checks')
    import preflight
    if readiness.get('source_trees') != preflight.source_trees():
        raise ValueError('harness_core/, packs/ or adapters/ changed after readiness checks')
    prereg = c.ROOT / 'PREREG.md'
    if not prereg.is_file() or readiness.get('prereg_sha256') != c.sha(prereg):
        raise ValueError('PREREG.md must be hashed by preflight before the first paid campaign call')
    return readiness


def install_stop():
    stop = threading.Event()
    def handler(signum, _frame):
        print(json.dumps({'stop_requested': signal.Signals(signum).name}), flush=True)
        stop.set()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, handler)
    return stop


def formal(resume=False, clear_abort=False):
    require_readiness()
    if clear_abort and c.CAMPAIGN.barrier is not None:
        import barrier
        moved = barrier.clear_abort(c.CAMPAIGN.barrier.dir)
        print(json.dumps({'abort_cleared': str(moved) if moved else None}), flush=True)
    key = os.environ.get('FIREWORKS_API_KEY') or getpass.getpass('Fireworks API key (hidden): ')
    c.save(c.ROOT / ('process-resume.json' if resume else 'process.json'),
           {'pid': os.getpid(), 'pgid': os.getpgrp(), 'started_utc': c.utc(), 'root': str(c.ROOT), 'resume': resume})
    # Keep this authorized long run awake; the guard exits with this process.
    subprocess.Popen(['/usr/bin/caffeinate', '-i', '-w', str(os.getpid())],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    stop = install_stop()
    c.run(key, resume=resume, stop=stop)
    offline_outputs()


def offline_outputs():
    import cost, full_audit, report
    print(cost.build(c.ROOT))
    print(json.dumps(full_audit.build(c.ROOT)))
    print(report.build(c.ROOT))


# ---------------------------------------------------------------------------------------------
# Study roots (DESIGN §7): $0, creates directories only
# ---------------------------------------------------------------------------------------------

STUDY_ORDER_SEED = 'v10-officeqa-pro133-order'         # one order seed for the whole study
STUDY_PREFIX = 'v10-r1'                                 # roots <prefix>-a … <prefix>-e, barrier <prefix>-barrier
STUDY_REPEAT = 1
STUDY_BARRIER_TIMEOUT_S = 43200                         # PROTOCOL §3 recommendation (12 h)
# (root letter, arm) of DESIGN §7; the mapping root -> arm lives only in .task-paths.json, freeze.json and
# the barrier's roots list (the names are neutral: arms.parse_config refuses a revealing one)
STUDY_ARMS = (('a', 'plain'), ('b', 'prompt-evolve'), ('c', 'harness-static'), ('d', 'harness-evolve'),
              ('e', 'harness-evolve-notes'))
# the development subset: the first 50 of the canonical order (pilot/first50.json, sha c95f9613…a07d)
SUBSET_FILE = 'pilot/first50.json'
# PREREG-v10.md is the pre-registration's own name (DESIGN §7); the code reads it as PREREG.md, a symlink to it in
# the plumbing. copytree(symlinks=False) materialises PREREG.md as a regular file in every root, and the -v10 name is
# not copied, so a root holds one copy of the document under the name preflight, freeze and the audit hash.
COPY_IGNORE = shutil.ignore_patterns('__pycache__', '.pytest_cache', '*.pyc', '.task-paths.json', 'preflight',
                                     'prepared', 'runs', 'runs-aborted', 'evidence', 'evolution', 'state.json',
                                     'freeze.json', 'report.html', 'cost-estimate.json', 'PREREG-v10.md')


def load_subset(path: Path):
    """A declared subset file ({name, task_ids, sha256, ...}) parsed by arms.parse_subset."""
    import arms
    listing = json.loads(Path(path).read_text())
    return arms.parse_subset({k: listing[k] for k in ('name', 'task_ids', 'sha256')})


# The v10 tables index (otables INDEX_VERSION 'otables-index-v10', with the narrative section tables of W0-12). Every
# root records it as `tables_index` in .task-paths.json: data.py's default is the frameworks-v9 copy (v9.1, no
# section tables), which the v10 tools open without error, so W0-12 would silently be absent (integration finding).
STUDY_TABLES_INDEX = 'frameworks-v10/officeqa-tables-index/tables.sqlite'


def study_configs(parent: Path, *, prefix: str = STUDY_PREFIX, subset=None, temperature=0, top_p=None,
                  repeat: int = STUDY_REPEAT, framework: Path, data_cache: Path, tables_index: Path,
                  arm_names=None) -> dict:
    """{root name: .task-paths.json} of the study (parsed and validated with arms.parse_config). `arm_names`
    (default: the five DESIGN §7 arms) are assigned to <prefix>-a, -b, … in order."""
    import arms
    pairs = list(STUDY_ARMS) if arm_names is None else [(chr(ord('a') + i), name) for i, name in enumerate(arm_names)]
    names = [f'{prefix}-{letter}' for letter, _ in pairs]
    barrier = {'dir': str(parent / f'{prefix}-barrier'), 'roots': names, 'timeout_s': STUDY_BARRIER_TIMEOUT_S}
    configs = {}
    for (letter, arm), name in zip(pairs, names):
        config = {'profile': arms.PROFILE, 'arm': arm, 'repeat': repeat, 'pack': 'officeqa',
                  'order_seed': STUDY_ORDER_SEED, 'temperature': temperature,
                  **({'top_p': top_p} if top_p is not None else {}),
                  **({'subset': subset.to_json()} if subset is not None else {}),
                  'barrier': barrier, 'framework': str(framework), 'data_cache': str(data_cache),
                  'tables_index': str(tables_index)}
        arms.parse_config(config, root_name=name)                  # refuses anything inconsistent or revealing
        configs[name] = config
    return configs


def study_roots(parent: Path | None = None, *, prefix: str = STUDY_PREFIX, subset_file: str | None = SUBSET_FILE,
                temperature=0, top_p=None, repeat: int = STUDY_REPEAT, framework: Path | None = None,
                data_cache: Path | None = None, tables_index: Path | None = None, source: Path | None = None,
                arm_names=None) -> dict:
    """Create the study roots as copies of this plumbing next to it (no model call, no data preparation).
    Refuses: inside a campaign root, an existing root or barrier directory, missing framework/data-cache
    directories, a missing tables index or one whose version is not the tools' (`data.tables_index_version`),
    a subset file whose hash is not the hash of its list."""
    import data
    source = Path(source or c.ROOT)
    if (source / '.task-paths.json').exists():
        raise ValueError('roots runs in the plumbing, not inside a campaign root')
    parent = Path(parent or source.parent).resolve()
    framework = Path(framework or parent / 'frameworks-v8/next-a48c0f9')
    data_cache = Path(data_cache or parent / 'baseline-work/data-cache/officeqa-official')
    tables_index = Path(tables_index or parent / STUDY_TABLES_INDEX)
    for what, path in (('framework', framework), ('data_cache', data_cache)):
        if not path.is_dir():
            raise ValueError(f'{what} directory missing: {path}')
    if not tables_index.is_file():
        raise ValueError(f'tables_index file missing: {tables_index}')
    data.tables_index_version(tables_index)                      # refuses an index the v10 tools cannot fully use
    subset = load_subset(source / subset_file) if subset_file else None
    configs = study_configs(parent, prefix=prefix, subset=subset, temperature=temperature, top_p=top_p, repeat=repeat,
                            framework=framework, data_cache=data_cache, tables_index=tables_index, arm_names=arm_names)
    for name in [*configs, f'{prefix}-barrier']:
        if (parent / name).exists():
            raise ValueError(f'{parent / name} exists; study roots are created once')
    created = {}
    prereg = c.sha(source / 'PREREG.md') if (source / 'PREREG.md').is_file() else None
    for name, config in configs.items():
        target = parent / name
        shutil.copytree(source, target, ignore=COPY_IGNORE, symlinks=False)
        (target / '.task-paths.json').write_text(json.dumps(config, indent=2) + '\n')
        if prereg is not None and c.sha(target / 'PREREG.md') != prereg:
            raise ValueError(f'{name}: PREREG.md copy differs')
        created[name] = {'arm': config['arm'], 'temperature': config['temperature'], 'top_p': config.get('top_p'),
                         'path': str(target)}
    return {'created': created, 'barrier': str(parent / f'{prefix}-barrier'),
            'subset': ({'name': subset.name, 'sha256': subset.sha256, 'count': len(subset.task_ids)} if subset else None),
            'order_seed': STUDY_ORDER_SEED, 'repeat': repeat, 'prereg_sha256': prereg, 'paid_calls': 0,
            'tables_index': str(tables_index), 'tables_index_version': data.tables_index_version(tables_index),
            'next': 'per root: data.py prepare, preflight.py --offline-only, preflight.py (paid smoke), run.py run '
                    '(pilot/README.md)'}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('mode', choices=('smoke', 'run', 'resume', 'audit', 'cost', 'report', 'roots'))
    parser.add_argument('--name', default='smoke-01')
    parser.add_argument('--arms', nargs='*', help='smoke: standard arm names (default: this root\'s arm); '
                                                  'roots: the arms of <prefix>-a, -b, … (default: DESIGN §7)')
    parser.add_argument('--clear-abort', action='store_true', help='resume only: rename the group ABORT file first')
    parser.add_argument('--parent', type=Path, help='roots only: where to create them (default: next to this plumbing)')
    parser.add_argument('--prefix', default=STUDY_PREFIX, help='roots only: root name prefix (default v10-r1)')
    parser.add_argument('--no-subset', action='store_true', help='roots only: all 133 questions instead of the first 50')
    parser.add_argument('--temperature', type=float, default=0, help='roots only: actor temperature (default 0)')
    parser.add_argument('--top-p', type=float, default=None, help='roots only: actor top_p (default: none sent)')
    parser.add_argument('--repeat', type=int, default=STUDY_REPEAT, help='roots only: the repeat number')
    parser.add_argument('--tables-index', type=Path, default=None,
                        help=f'roots only: the v10 tables index every root installs (default <parent>/{STUDY_TABLES_INDEX})')
    args = parser.parse_args()
    if args.mode == 'roots':
        temperature = int(args.temperature) if float(args.temperature).is_integer() else args.temperature
        print(json.dumps(study_roots(args.parent, prefix=args.prefix, subset_file=None if args.no_subset else SUBSET_FILE,
                                     temperature=temperature, top_p=args.top_p, repeat=args.repeat,
                                     tables_index=args.tables_index, arm_names=args.arms or None), indent=2))
        return
    if args.mode == 'smoke':
        key = os.environ.get('FIREWORKS_API_KEY') or getpass.getpass('Fireworks API key (hidden): ')
        raise SystemExit(0 if smoke(key, args.name, args.arms)['passed'] else 1)
    if args.mode in ('run', 'resume'):
        formal(resume=args.mode == 'resume', clear_abort=args.clear_abort)
    elif args.mode == 'audit':
        import full_audit
        result = full_audit.build(c.ROOT)
        print(json.dumps(result))
        raise SystemExit(0 if result['passed'] else 1)
    elif args.mode == 'cost':
        import cost
        print(cost.build(c.ROOT))
    else:
        import report
        print(report.build(c.ROOT))


if __name__ == '__main__':
    main()
