"""Explicit paid entry point for one v9 root; importing other modules never starts inference.

    python run.py smoke [--name smoke-01]           paid (~cents): one synthetic marker task through this
                                                     root's full v9 episode path (kit, broker, controller
                                                     and submit for controller arms)
    python run.py run                                paid: the formal campaign (needs passing readiness)
    python run.py resume [--clear-abort]             paid: block-level resume after an ABORT or a crash
    python run.py audit | cost | report              offline
    python run.py pilot-roots [--parent DIR]         $0, no model call: create the three neutral-named pilot
                                                     roots (PREREG §7 + the pilot addendum) next to this
                                                     plumbing; see pilot/README.md
    python run.py pilot2-root [--parent DIR]         $0, no model call: create the one neutral-named re-pilot
                                                     root v9-p2-a (mech-static with the P2 mech W0; PREREG
                                                     addendum P2) next to this plumbing; see pilot/README.md

A formal run requires preflight/readiness.json (preflight.py) with passing offline checks and a passing
smoke, unchanged source hashes, and the PREREG.md hash recorded there.
"""
import argparse
import getpass
import json
import os
from pathlib import Path
import re
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
    `broker_extra={'upstream_url': <local fake upstream>}`."""
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
    # this root's declared temperature and top_p (wave 3), so a root's smoke exercises exactly its broker setting
    temperature = c.CAMPAIGN.temperature if c.CAMPAIGN is not None else arms.DEFAULT_TEMPERATURE
    top_p = c.CAMPAIGN.top_p if c.CAMPAIGN is not None else arms.DEFAULT_TOP_P
    results = []
    for arm in specs:
        base = base_root / arm.name
        question = ('This is a synthetic interface test. Use the shell to read note.txt in your workspace. '
                    'Return only the exact string inside note.txt as your final response.')
        if arm.controller:
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
        controller_ok = (not arm.controller or ((summary.get('controller') or {}).get('submits') or 0) >= 1
                         and summary.get('fallback_answer') == marker)
        # the declared sampling reached every upstream body (a top_p key only when one is declared)
        bodies = [c.read(p) for p in sorted((base / 'api').glob('[0-9][0-9][0-9]/request.json'))]
        sampling_ok = bool(bodies) and all(b.get('temperature') == temperature and ('top_p' in b) == (top_p is not None)
                                           and b.get('top_p') == top_p for b in bodies)
        passed = (summary['status'] == 'completed' and summary['answer'] == marker and summary['kit_intact']
                  and native.get('command_execution', 0) >= 1 and not summary.get('transport_failed')
                  and reasoning_ok and controller_ok and sampling_ok)
        results.append({'arm': arm.name, 'passed': passed, 'summary': summary, 'reasoning_ok': reasoning_ok,
                        'controller_ok': controller_ok, 'sampling_ok': sampling_ok})
    result = {'passed': all(r['passed'] for r in results), 'results': results, 'source_hashes': sources,
              'model': c.MODEL, 'benchmark_questions': 0, 'temperature': temperature, 'top_p': top_p,
              'purpose': 'Actual native shell (+ submit on controller arms) round-trip through Fireworks Responses'}
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
# Pilot roots (wave 3, PREREG §7): $0, creates directories only
# ---------------------------------------------------------------------------------------------

PILOT_ORDER_SEED = 'v9-officeqa-pro133-order'          # PREREG §2: one order seed for the whole study
PILOT_REPEAT = 1
PILOT_BARRIER_TIMEOUT_S = 43200                          # PROTOCOL §3 recommendation (12 h)
# the pilot's question list (user decision 2026-09-28: the first 50 of the canonical order; the earlier
# loop-prone draft pilot/loop_prone_20.json is kept for the record only)
PILOT_LIST_FILE = 'pilot/first50.json'
PILOT_LIST = c.ROOT / PILOT_LIST_FILE
# (root name, arm, temperature, top_p) -- 'cell' = the model-card value recorded in the PREREG addendum;
# top_p None = the key is left out of .task-paths.json (no top_p is sent, as v8)
PILOT_ROOTS = (('v9-p1-a', 'text-static', 0, None), ('v9-p1-b', 'mech-static', 0, None),
               ('v9-p1-c', 'text-static', 'cell', 'cell'))
PILOT_BARRIER = 'v9-p1-barrier'
ADDENDUM_LIST = re.compile(r'^pilot-list-sha256:\s*([0-9a-f]{64})\s*$', re.M)
ADDENDUM_TEMPERATURE = re.compile(r'^pilot-cell-temperature:\s*(\S+)\s*$', re.M)
ADDENDUM_TOP_P = re.compile(r'^pilot-cell-top-p:\s*(\S+)\s*$', re.M)
PLACEHOLDER = '[USER:'
COPY_IGNORE = shutil.ignore_patterns('__pycache__', '.pytest_cache', '*.pyc', '.task-paths.json', 'preflight',
                                     'prepared', 'runs', 'runs-aborted', 'state.json', 'freeze.json')


def pilot_addendum(prereg: Path, subset_sha: str) -> dict:
    """The filled pilot addendum in PREREG.md (pilot/ADDENDUM.md appended below the Addenda line): the list
    hash must be this list's, the cell temperature and top_p numbers, and no [USER: ...] placeholder may
    remain."""
    import arms
    text = prereg.read_text() if prereg.is_file() else ''
    if PLACEHOLDER in text:
        raise ValueError(f'PREREG.md still has {PLACEHOLDER} ...] placeholders: fill pilot/ADDENDUM.md first')
    found = ADDENDUM_LIST.findall(text)
    if found != [subset_sha]:
        raise ValueError('PREREG.md must carry the pilot addendum (pilot/ADDENDUM.md, filled) with '
                         f'"pilot-list-sha256: {subset_sha}" exactly once')
    temperatures = ADDENDUM_TEMPERATURE.findall(text)
    if len(temperatures) != 1:
        raise ValueError('PREREG.md must declare "pilot-cell-temperature: <number>" exactly once')
    try:
        value = float(temperatures[0])
    except ValueError:
        raise ValueError(f'pilot-cell-temperature {temperatures[0]!r} is not a number') from None
    arms.parse_temperature(value)                 # kept as written (1.0 stays 1.0 in every record)
    if value == arms.DEFAULT_TEMPERATURE:
        raise ValueError('the temperature cell must differ from the default temperature 0')
    top_ps = ADDENDUM_TOP_P.findall(text)
    if len(top_ps) != 1:
        raise ValueError('PREREG.md must declare "pilot-cell-top-p: <number>" exactly once')
    try:
        top_p = float(top_ps[0])
    except ValueError:
        raise ValueError(f'pilot-cell-top-p {top_ps[0]!r} is not a number') from None
    arms.parse_top_p(top_p)
    if PILOT_ORDER_SEED not in text:
        raise ValueError(f'PREREG.md does not declare the order seed {PILOT_ORDER_SEED}')
    return {'cell_temperature': value, 'cell_top_p': top_p, 'prereg_sha256': c.sha(prereg)}


def pilot_configs(parent: Path, subset: dict, cell_temperature, cell_top_p=None, *, framework: Path,
                  data_cache: Path) -> dict:
    """{root name: .task-paths.json} of the pilot (parsed and validated with arms.parse_config)."""
    import arms
    names = [name for name, _, _, _ in PILOT_ROOTS]
    barrier = {'dir': str(parent / PILOT_BARRIER), 'roots': names, 'timeout_s': PILOT_BARRIER_TIMEOUT_S}
    configs = {}
    for name, arm, temperature, top_p in PILOT_ROOTS:
        top_p = cell_top_p if top_p == 'cell' else top_p
        config = {'profile': 'v9', 'arm': arm, 'repeat': PILOT_REPEAT, 'pack': 'officeqa',
                  'order_seed': PILOT_ORDER_SEED,
                  'temperature': cell_temperature if temperature == 'cell' else temperature,
                  **({'top_p': top_p} if top_p is not None else {}),
                  'subset': {'name': subset['name'], 'task_ids': list(subset['task_ids']), 'sha256': subset['sha256']},
                  'barrier': barrier, 'framework': str(framework), 'data_cache': str(data_cache)}
        arms.parse_config(config, root_name=name)                  # refuses anything inconsistent
        configs[name] = config
    return configs


def pilot_roots(parent: Path | None = None, *, framework: Path | None = None, data_cache: Path | None = None,
                source: Path | None = None) -> dict:
    """Create the pilot roots as copies of this plumbing next to it (no model call, no data preparation).
    Refuses: inside a campaign root, an existing root or barrier directory, a list whose hash is not the
    one in the filled PREREG addendum, missing framework/data-cache directories."""
    import arms
    source = Path(source or c.ROOT)
    if (source / '.task-paths.json').exists():
        raise ValueError('pilot-roots runs in the plumbing, not inside a campaign root')
    parent = Path(parent or source.parent).resolve()
    framework = Path(framework or parent / 'frameworks-v8/next-a48c0f9')
    data_cache = Path(data_cache or parent / 'baseline-work/data-cache/officeqa-official')
    for what, path in (('framework', framework), ('data_cache', data_cache)):
        if not path.is_dir():
            raise ValueError(f'{what} directory missing: {path}')
    listing = json.loads((source / PILOT_LIST_FILE).read_text())
    subset = arms.parse_subset({k: listing[k] for k in ('name', 'task_ids', 'sha256')})
    addendum = pilot_addendum(source / 'PREREG.md', subset.sha256)
    configs = pilot_configs(parent, subset.to_json(), addendum['cell_temperature'], addendum['cell_top_p'],
                            framework=framework, data_cache=data_cache)
    for name in [*configs, PILOT_BARRIER]:
        if (parent / name).exists():
            raise ValueError(f'{parent / name} exists; pilot roots are created once')
    created = {}
    for name, config in configs.items():
        target = parent / name
        shutil.copytree(source, target, ignore=COPY_IGNORE, symlinks=False)
        (target / '.task-paths.json').write_text(json.dumps(config, indent=2) + '\n')
        if c.sha(target / 'PREREG.md') != addendum['prereg_sha256']:
            raise ValueError(f'{name}: PREREG.md copy differs')
        created[name] = {'arm': config['arm'], 'temperature': config['temperature'], 'top_p': config.get('top_p'),
                         'path': str(target)}
    return {'created': created, 'barrier': str(parent / PILOT_BARRIER), 'subset': {'name': subset.name,
            'sha256': subset.sha256, 'count': len(subset.task_ids)}, 'order_seed': PILOT_ORDER_SEED,
            'prereg_sha256': addendum['prereg_sha256'], 'paid_calls': 0,
            'next': 'per root: data.py prepare, preflight.py, run.py run (pilot/README.md)'}


# ---------------------------------------------------------------------------------------------
# Re-pilot b2 (PREREG addendum P2): $0, creates one directory only
# ---------------------------------------------------------------------------------------------

# One root: mech-static with the P2 mech W0, the pilot-1 list, repeat and order seed, temperature 0 and no top_p
# (as pilot-1 cells a and b), a barrier over this root only.
P2_ROOT = 'v9-p2-a'
P2_ARM = 'mech-static'
P2_TEMPERATURE = 0
P2_BARRIER = 'v9-p2-barrier'
P2_ADDENDUM_FILE = 'pilot/ADDENDUM-P2.md'
P2_SELECTION_FILE = 'pilot/pilot2-rule-selection.json'
ADDENDUM_P2_HEADING = '### Addendum P2:'
ADDENDUM_P2_LINE = re.compile(r'^(p2-[a-z0-9-]+):[ \t]*(\S+)[ \t]*$', re.M)
# addendum lines whose value is fixed by this code
P2_FIXED = {'p2-root': P2_ROOT, 'p2-arm': P2_ARM, 'p2-temperature': str(P2_TEMPERATURE), 'p2-top-p': 'none',
            'p2-repeat': str(PILOT_REPEAT), 'p2-order-seed': PILOT_ORDER_SEED}


def pilot2_pins(source: Path) -> dict:
    """The values addendum P2 pins, computed from the plumbing: the mech W0 revision, its SEED.md and policy.json,
    the rule-selection file and script, the metrics script."""
    source = Path(source)
    return {'p2-mech-w0-revision': c.revision(source / 'adapters/mech-w0'),
            'p2-mech-seed-sha256': c.sha(source / 'adapters/mech-w0/SEED.md'),
            'p2-mech-policy-sha256': c.sha(source / 'adapters/mech-w0/policy.json'),
            'p2-rule-selection-file-sha256': c.sha(source / P2_SELECTION_FILE),
            'p2-rule-selection-script-sha256': c.sha(source / 'pilot/select_rules_p2.py'),
            'p2-metrics-script-sha256': c.sha(source / 'pilot/metrics.py')}


def pilot2_addendum(source: Path, subset_sha: str) -> dict:
    """Addendum P2 in PREREG.md (pilot/ADDENDUM-P2.md appended verbatim after addendum P1): every `p2-...` line
    once; the fixed values of this code; the pins equal to the plumbing's W0, rule selection and scripts; the
    selection file's disabled rules = the W0 policy's. Addendum P1 must still hold (pilot_addendum)."""
    import arms
    source = Path(source)
    prereg = source / 'PREREG.md'
    pilot_addendum(prereg, subset_sha)
    text = prereg.read_text()
    addendum = (source / P2_ADDENDUM_FILE).read_text() if (source / P2_ADDENDUM_FILE).is_file() else ''
    if not addendum or ADDENDUM_P2_HEADING not in addendum or addendum not in text:
        raise ValueError(f'PREREG.md must carry addendum P2 ({P2_ADDENDUM_FILE}) verbatim')
    if text.index(addendum) < text.index('### Addendum P1:'):
        raise ValueError('addendum P2 must follow addendum P1')
    found: dict[str, list[str]] = {}
    for key, value in ADDENDUM_P2_LINE.findall(text):
        found.setdefault(key, []).append(value)
    if any(len(v) != 1 for v in found.values()):
        raise ValueError(f'addendum P2 declares {sorted(k for k, v in found.items() if len(v) != 1)} more than once')
    values = {k: v[0] for k, v in found.items()}
    for key, expected in {**P2_FIXED, **pilot2_pins(source)}.items():
        if values.get(key) != expected:
            raise ValueError(f'addendum P2 "{key}" is {values.get(key)!r}, expected {expected!r}')
    selection = json.loads((source / P2_SELECTION_FILE).read_text())
    policy = arms.load_policy(source / 'adapters/mech-w0')
    if not selection.get('limits_hold') or selection.get('disabled_rules') != sorted(policy.get('disabled_rules') or []):
        raise ValueError('the mech W0 policy.json does not disable exactly the rules of the P2 rule selection')
    arms.check_w0(arms.parse_arm(P2_ARM), source / 'adapters')          # committed W0 = generated W0, in budget
    return {'prereg_sha256': c.sha(prereg), 'pins': values}


def pilot2_config(parent: Path, subset: dict, *, framework: Path, data_cache: Path) -> dict:
    """The re-pilot root's .task-paths.json (parsed and validated with arms.parse_config)."""
    import arms
    config = {'profile': 'v9', 'arm': P2_ARM, 'repeat': PILOT_REPEAT, 'pack': 'officeqa', 'order_seed': PILOT_ORDER_SEED,
              'temperature': P2_TEMPERATURE,
              'subset': {'name': subset['name'], 'task_ids': list(subset['task_ids']), 'sha256': subset['sha256']},
              'barrier': {'dir': str(parent / P2_BARRIER), 'roots': [P2_ROOT], 'timeout_s': PILOT_BARRIER_TIMEOUT_S},
              'framework': str(framework), 'data_cache': str(data_cache)}
    arms.parse_config(config, root_name=P2_ROOT)
    return config


def pilot2_root(parent: Path | None = None, *, framework: Path | None = None, data_cache: Path | None = None,
                source: Path | None = None) -> dict:
    """Create the re-pilot root as a copy of this plumbing next to it (no model call, no data preparation).
    Refuses: inside a campaign root, without addendum P2 (or with pins that differ from this plumbing), an
    existing root or barrier directory, missing framework/data-cache directories."""
    import arms
    source = Path(source or c.ROOT)
    if (source / '.task-paths.json').exists():
        raise ValueError('pilot2-root runs in the plumbing, not inside a campaign root')
    parent = Path(parent or source.parent).resolve()
    framework = Path(framework or parent / 'frameworks-v8/next-a48c0f9')
    data_cache = Path(data_cache or parent / 'baseline-work/data-cache/officeqa-official')
    for what, path in (('framework', framework), ('data_cache', data_cache)):
        if not path.is_dir():
            raise ValueError(f'{what} directory missing: {path}')
    listing = json.loads((source / PILOT_LIST_FILE).read_text())
    subset = arms.parse_subset({k: listing[k] for k in ('name', 'task_ids', 'sha256')})
    addendum = pilot2_addendum(source, subset.sha256)
    config = pilot2_config(parent, subset.to_json(), framework=framework, data_cache=data_cache)
    for name in (P2_ROOT, P2_BARRIER):
        if (parent / name).exists():
            raise ValueError(f'{parent / name} exists; the re-pilot root is created once')
    target = parent / P2_ROOT
    shutil.copytree(source, target, ignore=COPY_IGNORE, symlinks=False)
    (target / '.task-paths.json').write_text(json.dumps(config, indent=2) + '\n')
    if c.sha(target / 'PREREG.md') != addendum['prereg_sha256']:
        raise ValueError(f'{P2_ROOT}: PREREG.md copy differs')
    if c.revision(target / 'adapters/mech-w0') != addendum['pins']['p2-mech-w0-revision']:
        raise ValueError(f'{P2_ROOT}: mech W0 copy differs')
    return {'created': {P2_ROOT: {'arm': P2_ARM, 'temperature': P2_TEMPERATURE, 'top_p': None, 'path': str(target)}},
            'barrier': str(parent / P2_BARRIER), 'subset': {'name': subset.name, 'sha256': subset.sha256,
                                                            'count': len(subset.task_ids)},
            'order_seed': PILOT_ORDER_SEED, 'repeat': PILOT_REPEAT, 'prereg_sha256': addendum['prereg_sha256'],
            'mech_w0_revision': addendum['pins']['p2-mech-w0-revision'], 'paid_calls': 0,
            'next': f'in ../{P2_ROOT}: data.py prepare, preflight.py --offline-only, preflight.py, run.py run '
                    '(pilot/README.md, re-pilot b2)'}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('mode', choices=('smoke', 'run', 'resume', 'audit', 'cost', 'report', 'pilot-roots',
                                         'pilot2-root'))
    parser.add_argument('--name', default='smoke-01')
    parser.add_argument('--arms', nargs='*', help='smoke only: standard arm names (default: this root\'s arm)')
    parser.add_argument('--clear-abort', action='store_true', help='resume only: rename the group ABORT file first')
    parser.add_argument('--parent', type=Path, help='pilot-roots / pilot2-root only: where to create them (default: '
                                                    'next to this plumbing)')
    args = parser.parse_args()
    if args.mode == 'pilot-roots':
        print(json.dumps(pilot_roots(args.parent), indent=2))
        return
    if args.mode == 'pilot2-root':
        print(json.dumps(pilot2_root(args.parent), indent=2))
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
