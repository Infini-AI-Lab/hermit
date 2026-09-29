"""Readiness checks for one v10 root: offline checks ($0), then the paid synthetic smoke (cents).

Writes preflight/readiness.json, which run.py requires before a formal campaign. `--offline-only` stops
after the $0 part (no key is asked for). Offline checks (DESIGN §3, §4, §6, §8 S3):
- the arm spec parses and validates (arms.py, profile v10); the root name is in its barrier group and
  reveals no arm;
- prepared data verify (hashes, 133 tasks, corpus, FTS index, issue-47 list) and the tables index
  indexes exactly this corpus; a declared subset is the prepared manifest's and sets this root's order; the
  declared temperature and top_p (absent = none sent) are the ones the broker sends;
- the frozen actor runtime passes the deep manifest check (kit.verify_frozen(deep=True));
- the base order equals the v8 order (read-only v8 manifest, when present); this repeat's permutation is
  registered in the barrier group's order.json;
- both W0 adapters of the pack (prompt-w0, harness-w0) equal the generated ones (drift), satisfy the seed
  parity checklist (`arms.check_w0`: every SEED_KNOWLEDGE item on each side, both SEEDs ≤ SEED_MAX and within
  ±25 % in bytes, the prompt SEED naming no harness tool) and pass `check_adapter` (manifest, calc pinned,
  caps, USAGE line rule, controls, dry-run kit); the byte counts PREREG records (SEED bytes and hashes, the
  model-visible total per architecture) are written to readiness.json;
- W0 idempotence through the learner's screens: each W0 tool file, USAGE.md and manifest.json restaged
  unchanged must pass `learner_common.screen_staged` (path, caps, functional python screen, code-aware
  diff leak screen, USAGE budget, manifest shape; source = candidate = W0, no released evidence) with zero
  findings (DESIGN §5 screen 3 c); tools/calc.py is skipped (pinned: restaging it is refused by design).
  Further screens can be registered in `W0_SCREENS`;
- the ToolRunner smoke: every manifest tool of the harness W0 runs once through the sandboxed ToolRunner
  (`harness_core.runner.ToolRunner`, DESIGN §5; read roots checked by `campaign.check_tool_runner_roots`)
  over this root's corpus and tables index: exit 0 and non-empty output (calc is skipped: it is sha-pinned and
  its runner forks, which the sandbox denies; the learners' replay skips it for the same reason);
- a real offline workspace for this root's first question (kit, AGENTS.md with its notes, README) and a
  strict Codex config read of its settings (no model call);
- the test suites (kit, arms, pack, core, broker, orchestration/config, learners) and the $0 live-Codex tier
  (the pinned binary against a fake upstream on 127.0.0.1);
- PREREG.md present, a v10 pre-registration (its first heading names v10: the v9 tree's PREREG.md is not
  copied into this tree, so a root cannot be hashed against the wrong protocol) and hashed; frameworks are
  the private frameworks-v8 (Codex) / frameworks-v9 (runtime) copies;
- the calibration record (calibrate.py -> preflight/calibration.json; DESIGN §5 "Calibration before the paid
  run"): required for a harness-learner root, else listed as a pending decision.

Separate, optional, never part of readiness: `--replay-v9a <root>` replays the recorded tool commands of a
v9 evidence root (read-only; `runs/episodes/*/api/ledger.jsonl` command events when a ledger exists, else
the `command_execution` items of `runs/episodes/*/records/events.jsonl`) against the unchanged W0 tools of
this tree: each rebuilt invocation (`learner_common.tool_invocations`, the one definition the learners'
replay uses) runs twice (source tools = candidate tools = W0) through the ToolRunner; it must rebuild
≥ REPLAY_MIN_JOBS invocations and see 0 changed outputs (DESIGN §5 ToolRunner). It writes
preflight/replay-v9a.json (counts, wall time, per-tool numbers; never tool output).
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import getpass
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Callable, Sequence

import arms
import campaign as c
import acting
import data
import experiment
import kit
from harness_core import contracts as C
from paths import ACTOR_PYTHON, ACTOR_PYTHON_PREFIX, CODEX

ROOT = c.ROOT
V8_MANIFEST = ROOT.parent / 'officeqa-pro133-none-v8-work/prepared/manifest.json'
# Test suites by stem: tests/test_v10_<stem>.py (a missing file is reported by pytest() and fails the check).
SUITES = {
    'kit': ['kit'], 'arms': ['arms'],
    'pack': ['tests/test_officeqa_pack.py', 'tests/test_officeqa_tables.py'],
    'core': ['ledger', 'engine', 'gate', 'runner', 'replay'],
    'broker': ['tests/test_v8_broker.py', 'broker'],
    'orchestration': ['orchestration', 'config'],
}
LEARNER_TESTS = ('tests/test_v10_learn*.py', 'tests/test_v10_evidence*.py', 'tests/test_v10_*learner*.py')
LIVE = ['codex_live', 'kit', 'orchestration', 'e2e']
PENDING = '[DECISION PENDING'
# ToolRunner smoke arguments per manifest tool (default: --help, which every argparse tool answers with exit 0).
SMOKE_ARGV: dict[str, list[str]] = {'find_tables': ['receipts', '--limit', '3']}
# calc is pinned to the W0 copy (arms.load_manifest checks its sha) and skipped by the replay: its RUNNER forks a
# python child, which the ToolRunner profile denies (DESIGN review §4-7), so the smoke skips it too.
SMOKE_SKIP: dict[str, str] = {C.CALC_TOOL: 'pinned to the W0 copy (sha checked by arms.load_manifest); its runner forks, '
                                           'which the ToolRunner denies; the replay skips it as well'}
TOOL_CPU_S = 20
TOOL_TIMEOUT_S = 20
TOOL_OUTPUT_CAP = 16_384
REPLAY_MIN_JOBS = 200
REPLAY_WORKERS = 4
# Further screens for the W0 idempotence check, beside the learner's own (learner_common.w0_idempotence_screen,
# always run): (name, screen(relative path, text) -> findings).
W0_SCREENS: list[tuple[str, Callable[[str, str], list[str]]]] = []
# The pinned tool file the learner may never restage (learner_common.screen_staged refuses it by design).
IDEMPOTENCE_SKIP = {f'{arms.TOOLS_DIR}/calc.py': 'pinned to the W0 copy: restaging it is refused by the learner screen'}
PREREG_HEADING = '# v10'      # the first heading of a v10 pre-registration (a v9 PREREG.md copy is refused)


def source_names():
    return (tuple(p.name for p in sorted(ROOT.glob('*.py'))) + ('PROTOCOL.md', 'PREREG.md', '.task-paths.json')
            + (('cards_domain.md',) if (ROOT / 'cards_domain.md').is_file() else ()))


def source_trees():
    """Revisions of the code trees the freeze covers (harness_core/, packs/, adapters/): a change between
    preflight and the formal run must invalidate readiness, not only a change to a top-level file."""
    return {name: c.revision(ROOT / name) for name in c.CODE_TREES if (ROOT / name).is_dir()}


def suite_files(entries: Sequence[str]) -> list[str]:
    """Resolve suite entries: an explicit tests/ path, or a stem -> tests/test_v10_<stem>.py."""
    return [entry if entry.startswith('tests/') else f'tests/test_v10_{entry}.py' for entry in entries]


def pytest(files, *, marker=None):
    files = [f for f in files if (ROOT / f).is_file()]
    if not files:
        return {'passed': False, 'output': 'no test files found'}
    args = [sys.executable, '-B', '-m', 'pytest', '-q', '-p', 'no:cacheprovider', *files]
    if marker:
        args[5:5] = ['-m', marker]
    run = subprocess.run(args, cwd=ROOT, capture_output=True, text=True)
    return {'passed': run.returncode == 0, 'output': run.stdout[-400:], 'files': files, 'marker': marker}


def prereg_pending(path=ROOT / 'PREREG.md'):
    """Decision placeholders still open in PREREG.md (recorded; a confirmatory campaign needs none)."""
    if not path.is_file():
        return ['PREREG.md missing']
    return [line.strip() for line in path.read_text().splitlines() if PENDING in line]


CALIBRATION_RECORD = 'preflight/calibration.json'     # written by calibrate.py


def calibration_record(path=None) -> dict | None:
    """The calibration outcome (calibrate.py) as PREREG cites it: status, proposals by kind, replays, admission,
    actor runs, source revision; None when no calibration has run."""
    path = Path(path) if path is not None else ROOT / CALIBRATION_RECORD
    if not path.is_file():
        return None
    record = json.loads(path.read_text(encoding='utf-8'))
    return {k: record.get(k) for k in ('created_utc', 'source_root', 'source_tools', 'source_revision', 'dry_run', 'status',
                                       'proposals', 'replays', 'admission', 'actor_runs', 'elapsed_seconds')}


def prereg_is_v10(path=ROOT / 'PREREG.md') -> bool:
    """PREREG.md exists and its first non-empty line is a v10 heading (`# v10 ...`): the v9 tree's PREREG.md
    must never be the one a v10 root is hashed against."""
    if not path.is_file():
        return False
    first = next((line.strip() for line in path.read_text(encoding='utf-8').splitlines() if line.strip()), '')
    return first.startswith(PREREG_HEADING)

# ---------------------------------------------------------------------------------------------
# W0 checks (DESIGN §4): both architectures, parity numbers, idempotence through the learner screens
# ---------------------------------------------------------------------------------------------


def w0_checks(pack: str, adapters: Path | None = None) -> dict:
    """arms.check_w0 for both W0s of the pack; the PREREG numbers (SEED bytes/hashes, model-visible bytes per
    architecture, checklist items) and per-W0 pass/fail with the error text."""
    out = {'ok': True, 'w0': {}, 'seed_parity': None, 'model_visible': {}}
    for arm_name in ('prompt-static', 'harness-static'):
        arm = arms.parse_arm(arm_name, pack=pack)
        try:
            checked = arms.check_w0(arm, adapters)
            out['w0'][arm.seed] = {'ok': True, 'adapter': checked['adapter'], 'files': checked['files'],
                                   'revision': checked['revision']}
            out['seed_parity'] = checked['parity']
            out['model_visible'][arm.seed] = checked['model_visible']
        except (OSError, ValueError) as exc:
            out['ok'] = False
            out['w0'][arm.seed] = {'ok': False, 'error': str(exc)[:500]}
    return out


def w0_idempotence(pack: str, adapters: Path | None = None) -> dict:
    """Every W0 tool file, USAGE.md and manifest.json (calc.py skipped: pinned) restaged unchanged passes
    the learner's staged-file screen (learner_common.w0_idempotence_screen: source = candidate = W0) and
    every screen registered in W0_SCREENS with zero findings.
    {'status': 'passed' | 'failed', 'screens': [...], 'files': [...], 'skipped': {...}, 'findings': {...}}."""
    import tempfile
    import learner_common as LC
    directory = arms.w0_dir(arms.parse_arm('harness-static', pack=pack), adapters)
    files = {rel: text for rel, text in arms.tree_files(directory).items()
             if rel.startswith(arms.TOOLS_DIR + '/') and rel not in IDEMPOTENCE_SKIP}
    findings = {}
    with tempfile.TemporaryDirectory(prefix='w0-idempotence-') as scratch:
        screens = [('screen_staged', LC.w0_idempotence_screen(arms.load_pack(pack), directory, Path(scratch)))]
        screens += list(W0_SCREENS)
        for name, screen in screens:
            for rel, text in sorted(files.items()):
                try:
                    found = list(screen(rel, text))
                except Exception as exc:              # a screen that raises on W0 is a failed screen
                    found = [f'{type(exc).__name__}: {exc}']
                if found:
                    findings[f'{name}:{rel}'] = found[:5]
    return {'status': 'failed' if findings else 'passed', 'screens': [n for n, _ in screens],
            'files': sorted(files), 'skipped': dict(IDEMPOTENCE_SKIP), 'findings': findings}

# ---------------------------------------------------------------------------------------------
# ToolRunner (DESIGN §5): harness_core.runner.ToolRunner over the harness W0 (readiness) or the v9 evidence replay
# ---------------------------------------------------------------------------------------------


def tool_runner(tools_dir: Path, lay: 'c.Layout', *, pack: str, tmp_root: Path, root: Path | None = None):
    """The ToolRunner for `tools_dir` over the root's corpus and, for a pack with a tables index, its index:
    campaign.make_tool_runner, the one constructor (review fix: preflight no longer builds its own), which
    checks the read roots against the private trees of `root` (default: the layout root) and lets the runner
    re-check them."""
    return c.make_tool_runner(lay, tools_dir, tmp_root, pack=pack, root=root)


def run_result(value) -> dict:
    """One shape for a ToolRunner.run result: exit_code, stdout, stderr, timed_out, seconds."""
    return {'exit_code': value.get('exit'), 'stdout': str(value.get('stdout') or ''),
            'stderr': str(value.get('stderr_tail') or value.get('stderr') or ''),
            'timed_out': bool(value.get('timed_out')), 'seconds': value.get('seconds')}


def run_manifest_tool(runner, manifest: dict, name: str, args: Sequence[str], stdin: bytes = b'') -> dict:
    """Run manifest tool `name` through `runner` (its tools dir): [script, *manifest args, *args]."""
    entry = manifest[name]
    return run_result(runner.run([entry['script'], *entry['args'], *args], stdin=stdin, cpu_s=TOOL_CPU_S,
                                 timeout_s=TOOL_TIMEOUT_S, output_cap=TOOL_OUTPUT_CAP))


def tool_runner_smoke(pack: str, lay: 'c.Layout', *, adapters: Path | None = None, tmp_root: Path | None = None) -> dict:
    """Every manifest tool of the harness W0 (calc excepted: SMOKE_SKIP) once through the sandboxed ToolRunner:
    exit 0 and non-empty stdout."""
    tools_dir = arms.w0_dir(arms.parse_arm('harness-static', pack=pack), adapters) / arms.TOOLS_DIR
    manifest = arms.load_manifest(tools_dir)
    tmp_root = Path(tmp_root or lay.root / 'preflight' / 'tool-tmp')
    runner = tool_runner(tools_dir, lay, pack=pack, tmp_root=tmp_root)
    tools, skipped, ok = {}, {}, True
    for name in sorted(manifest):
        if name in SMOKE_SKIP:
            skipped[name] = SMOKE_SKIP[name]
            continue
        result = run_manifest_tool(runner, manifest, name, SMOKE_ARGV.get(name, ['--help']))
        passed = result['exit_code'] == 0 and bool(result['stdout'].strip())
        ok = ok and passed
        tools[name] = {'passed': passed, 'exit_code': result['exit_code'], 'stdout_bytes': len(result['stdout']),
                       'stderr_tail': result['stderr'][-200:] if not passed else '', 'seconds': result['seconds']}
    shutil.rmtree(tmp_root, ignore_errors=True)
    return {'passed': ok and bool(tools), 'runner': type(runner).__name__, 'read_roots': list(runner.read_roots),
            'tools': tools, 'skipped': skipped, 'tools_dir': str(tools_dir)}

# ---------------------------------------------------------------------------------------------
# Replay of recorded tool commands (the optional --replay-v9a command)
# ---------------------------------------------------------------------------------------------



def recorded_commands(evidence_root: Path) -> list[dict]:
    """The recorded commands of a root's scored episodes: ledger command events (raw_cmd) when the episode has
    a ledger, else the command_execution items of records/events.jsonl. Only command text and exit codes
    are read; outputs, answers and references are never opened."""
    rows = []
    episodes = sorted(p for p in (Path(evidence_root) / 'runs' / c.RUNS_DIR).glob('[0-9][0-9][0-9]') if p.is_dir())
    for episode in episodes:
        ledger = episode / 'api' / 'ledger.jsonl'
        events = episode / 'records' / 'events.jsonl'
        step = 0
        if ledger.is_file():
            seen = {}
            for line in ledger.read_text(encoding='utf-8', errors='replace').splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get('kind') == 'command':
                    seen[event.get('ref')] = event            # folded by ref (the last line carries the exit code)
            for ref, event in seen.items():
                step += 1
                rows.append({'episode': episode.name, 'step': step, 'source': 'ledger', 'ref': ref,
                             'raw': event.get('raw_cmd') or event.get('cmd') or '', 'exit_code': event.get('exit_code')})
        elif events.is_file():
            for line in events.read_text(encoding='utf-8', errors='replace').splitlines():
                if not line.startswith('{'):
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                item = event.get('item') or {}
                if event.get('type') == 'item.completed' and item.get('type') == 'command_execution':
                    step += 1
                    rows.append({'episode': episode.name, 'step': step, 'source': 'events',
                                 'raw': str(item.get('command') or ''), 'exit_code': item.get('exit_code')})
    return rows


def replay_jobs(commands: Sequence[dict], manifest: dict, *, skip: Sequence[str] = (C.CALC_TOOL,),
                limit: int | None = None) -> list[dict]:
    """Structured replay jobs {tool, argv, episode, step, exit_code} rebuilt from recorded commands with
    learner_common.tool_invocations (the one definition: zsh -lc wrappers, pipelines, redirections, loop
    bodies and unexpanded variables handled there; calc skipped: pinned, its output cannot change),
    de-duplicated on (tool, argv), most recent first when `limit` cuts the list."""
    import learner_common as LC
    jobs, seen = [], set()
    for row in commands:
        for tool, argv in LC.tool_invocations(row['raw'], manifest):
            if tool in skip:
                continue
            key = (tool, tuple(argv))
            if key in seen:
                continue
            seen.add(key)
            jobs.append({'tool': tool, 'argv': list(argv), 'episode': row['episode'], 'step': row['step'],
                         'exit_code': row.get('exit_code')})
    if limit is not None and len(jobs) > limit:
        jobs = jobs[-limit:]
    return jobs


def replay(jobs: Sequence[dict], runner, manifest: dict, *, candidate=None, workers: int = REPLAY_WORKERS) -> dict:
    """Run every job with the source tools (`runner`) and with the candidate tools (`candidate`, a runner over
    the candidate tools dir; default: the source runner again, the determinism check) and compare stdout
    and exit codes. Returns counts and the per-job table without outputs."""
    candidate = candidate or runner
    started = time.perf_counter()

    def one(job):
        before = run_manifest_tool(runner, manifest, job['tool'], job['argv'])
        after = run_manifest_tool(candidate, manifest, job['tool'], job['argv'])
        changed = before['stdout'] != after['stdout'] or before['exit_code'] != after['exit_code']
        return {'tool': job['tool'], 'episode': job['episode'], 'step': job['step'], 'exit_before': before['exit_code'],
                'exit_after': after['exit_code'], 'timed_out': before['timed_out'] or after['timed_out'],
                'changed': changed, 'stdout_bytes': len(before['stdout'].encode('utf-8'))}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        table = list(pool.map(one, jobs))
    per_tool = {}
    for row in table:
        entry = per_tool.setdefault(row['tool'], {'jobs': 0, 'ok': 0, 'changed': 0, 'timed_out': 0})
        entry['jobs'] += 1
        entry['ok'] += row['exit_before'] == 0
        entry['changed'] += row['changed']
        entry['timed_out'] += row['timed_out']
    return {'jobs': len(table), 'ok': sum(r['exit_before'] == 0 for r in table), 'changed': sum(r['changed'] for r in table),
            'timed_out': sum(r['timed_out'] for r in table), 'wall_seconds': round(time.perf_counter() - started, 3),
            'per_tool': per_tool, 'table': table}


class _IndexLayout(c.Layout):
    """A layout over an evidence root whose tables index is another file (the v10 index for a v9 root whose own
    index is v9.1: the section tables of W0-12 exist only in the v10 build)."""

    def __init__(self, root: Path, code: Path, tables_index: Path):
        super().__init__(root=root, code=code)
        object.__setattr__(self, '_tables_index', Path(tables_index).resolve())   # Layout is a frozen dataclass

    @property
    def tables_dir(self):
        return self._tables_index.parent

    @property
    def tables_index(self):
        return self._tables_index


def replay_v9a(evidence_root: Path, *, lay: 'c.Layout | None' = None, pack: str = 'officeqa', adapters: Path | None = None,
               min_jobs: int = REPLAY_MIN_JOBS, limit: int | None = None, out: Path | None = None,
               tmp_root: Path | None = None, tables_index: Path | None = None) -> dict:
    """The optional replay command: rebuild the recorded invocations of `evidence_root` (read-only) and run
    them twice against this tree's unchanged harness W0 tools over that root's corpus and tables index (or
    `tables_index`, e.g. the v10 index the campaign roots install: a v9 root's own index has no section tables).
    Passes when ≥ min_jobs invocations were rebuilt and 0 outputs changed."""
    evidence_root = Path(evidence_root).resolve()
    if lay is None:
        lay = (_IndexLayout(evidence_root, c.ROOT, tables_index) if tables_index is not None
               else c.Layout(root=evidence_root, code=c.ROOT))
    tools_dir = arms.w0_dir(arms.parse_arm('harness-static', pack=pack), adapters) / arms.TOOLS_DIR
    manifest = arms.load_manifest(tools_dir)
    commands = recorded_commands(evidence_root)
    jobs = replay_jobs(commands, manifest, limit=limit)
    tmp_root = Path(tmp_root or ROOT / 'preflight' / 'replay-tmp')
    runner = tool_runner(tools_dir, lay, pack=pack, tmp_root=tmp_root, root=evidence_root)
    result = replay(jobs, runner, manifest)
    shutil.rmtree(tmp_root, ignore_errors=True)
    # a pack without a tables index (toy) records none; OfficeQA records the index the replay ran against
    has_index = c.uses_tables_index(pack) and Path(lay.tables_index).is_file()
    record = {'evidence_root': str(evidence_root), 'tools_dir': str(tools_dir), 'commands': len(commands),
              'tables_index': str(lay.tables_index.resolve()) if has_index else None,
              'tables_index_version': data.tables_meta(lay.tables_index).get('version') if has_index else None,
              'sources': sorted({r['source'] for r in commands}), 'rebuilt': result['jobs'], 'ok': result['ok'],
              'changed': result['changed'], 'timed_out': result['timed_out'], 'wall_seconds': result['wall_seconds'],
              'per_tool': result['per_tool'], 'min_jobs': min_jobs, 'runner': type(runner).__name__,
              'read_roots': list(runner.read_roots), 'passed': result['jobs'] >= min_jobs and result['changed'] == 0,
              'table': result['table'], 'utc': c.utc()}
    if out is not None:
        c.save(out, record)
    return record

# ---------------------------------------------------------------------------------------------
# Offline readiness
# ---------------------------------------------------------------------------------------------


def offline(*, suites=True):
    checks, info = {}, {}
    spec = experiment.CAMPAIGN
    checks['arm_valid'] = spec is not None
    if spec is None:
        return False, checks, {'error': 'no .task-paths.json "arm" in this root'}
    arm = arms.check_arm(spec.arm)
    info['campaign'] = spec.to_json()
    checks['profile_v10'] = experiment.PROFILE == arms.PROFILE == 'v10'
    verification = data.verify_prepared(ROOT, spec=spec)
    c.save(ROOT / 'preflight/data-verification.json', verification)
    checks['data'] = verification['verified'] is True
    checks['tables_index_matches_corpus'] = bool(verification['tables']['corpus_manifest_sha256'])
    # the installed index carries the tools' version (a v9.1 index opens without error but has no section tables)
    from packs.officeqa.tools import otables
    info['tables_index_version'] = verification['tables'].get('version')
    checks['tables_index_version_current'] = info['tables_index_version'] == otables.INDEX_VERSION
    tasks = data.load_tasks(ROOT)
    checks['question_count_133'] = len(tasks) == 133
    if V8_MANIFEST.is_file():
        checks['base_order_equals_v8'] = [t['task_id'] for t in tasks] == json.loads(V8_MANIFEST.read_text())['task_ids']
    order = arms.campaign_order([t['task_id'] for t in tasks], spec)
    info['order_sha256'] = arms.order_sha256(order)
    info['question_count'] = len(order)
    if spec.subset is not None:
        # verify_prepared already refused a manifest whose subset differs; recorded here for the operator
        info['subset'] = verification.get('subset')
        checks['subset_hash_matches_manifest'] = (
            (verification.get('subset') or {}).get('sha256') == spec.subset.sha256 == arms.subset_sha256(spec.subset.task_ids)
            and sorted(order) == sorted(spec.subset.task_ids))
    info['temperature'] = spec.temperature
    info['top_p'] = spec.top_p
    if spec.barrier is not None:
        import barrier
        group = barrier.FileBarrier(spec.barrier.dir, spec.barrier.roots, spec.root, timeout_s=spec.barrier.timeout_s)
        group.register_order(spec.repeat, info['order_sha256'], len(order))
        checks['barrier_order_registered'] = True
        checks['barrier_not_aborted'] = group.aborted() is None
    info['frozen_runtime'] = kit.verify_frozen(deep=True)
    checks['frozen_runtime_deep'] = True
    # W0 adapters of both architectures (DESIGN §4): drift, parity checklist, byte counts, check_adapter
    w0 = w0_checks(spec.pack)
    info['w0'] = {k: v for k, v in w0.items() if k != 'ok'}
    checks['w0_prompt_valid'] = w0['w0'].get('text', {}).get('ok') is True
    checks['w0_harness_valid'] = w0['w0'].get('harness', {}).get('ok') is True
    checks['seed_parity_within_budget'] = w0['ok'] and all(v['within_budget'] for v in w0['model_visible'].values())
    info['seed_parity'] = w0['seed_parity']
    info['model_visible'] = w0['model_visible']
    idempotence = w0_idempotence(spec.pack)
    info['w0_idempotence'] = idempotence
    checks['w0_idempotence'] = idempotence['status'] == 'passed'
    w0_dir = arms.w0_dir(arm)
    if w0_dir is not None:
        info['arm_w0'] = arms.check_adapter(w0_dir, arm)
        checks['w0_valid'] = True
    smoke = tool_runner_smoke(spec.pack, c.Layout())
    info['tool_runner_smoke'] = smoke
    checks['tool_runner_smoke'] = smoke['passed']
    # A real offline workspace for this root's first question.
    directory = ROOT / 'preflight' / 'offline-workspace'
    if directory.exists():
        shutil.rmtree(directory)
    try:
        first = next(t for t in tasks if t['task_id'] == order[0])
        prep = c.prepare_episode(c.Layout(), arm, first, w0_dir, directory / 'episode', ordinal=1, role='preflight')
        workspace = Path(prep['workspace'])
        env_extra, data_roots = c.actor_data(arm, c.Layout())
        settings = acting.build_settings(workspace, directory / 'out', c.CORPUS, c.INDEX, None,
                                         endpoint='http://127.0.0.1:1/v1', arm=arm, env_extra=env_extra, data=data_roots,
                                         permissions_instructions=False)
        info['agents_md_bytes'] = prep['agents_md_bytes']
        info['readme_bytes'] = prep['readme_bytes']
        info['notes_first_question'] = prep['notes']
        (ROOT / 'preflight/agents-md-first-question.md').write_text((workspace / 'AGENTS.md').read_text())
        (ROOT / 'preflight/readme-first-question.md').write_text((workspace / 'README.md').read_text())
        marker = json.loads((workspace / acting.MARKER).read_text())
        checks['kit_built_for_arm'] = kit.verify_kit(workspace, marker['kit']) is not None
        # gate off: no submit wrapper, no submit README section, no HARNESS_SUBMIT_URL (DESIGN §3)
        readme = (workspace / 'README.md').read_text()
        checks['submit_iff_gate'] = ((marker['kit'].get('submit') is not None) == arm.gate
                                     and ('# Submitting your answer' in readme) == arm.gate
                                     and (C.SUBMIT_ENV in acting.actor_env_extra(arm, 'http://127.0.0.1:1/v1', env_extra)) == arm.gate)
        checks['codex_config_valid'] = acting.validate_config_offline(settings)['passed']
        checks['settings_v10'] = (settings.get('features.multi_agent') is False and settings.get('agents.enabled') is False
                                  and settings.get('model_auto_compact_token_limit') == 244800
                                  and settings.get('include_permissions_instructions') is False)
        if arm.l1_tools:
            roots = marker['kit'].get('code_roots') or []
            checks['code_root_neutral'] = (len(roots) == 1 and not arms.revealing_fragments(
                Path(roots[0]).resolve().relative_to(ROOT.resolve().parent), arm))
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    checks['temperature_sent_equals_declared'] = (c.broker_temperature(spec.temperature) == spec.temperature
                                                  == experiment.TEMPERATURE)
    checks['top_p_sent_equals_declared'] = c.broker_top_p(spec.top_p) == spec.top_p == experiment.TOP_P
    root_fragments = arms.revealing_fragments(ROOT.name, arm)
    checks['root_name_reveals_no_arm'] = not root_fragments
    info['codex_version'] = subprocess.run([str(CODEX), '--version'], capture_output=True, text=True).stdout.strip()
    checks['frameworks_are_private_copies'] = ('frameworks-v8' in str(CODEX) and 'frameworks-v9' in str(ACTOR_PYTHON_PREFIX))
    checks['prereg_present'] = (ROOT / 'PREREG.md').is_file()
    checks['prereg_is_v10'] = prereg_is_v10()
    info['prereg_sha256'] = c.sha(ROOT / 'PREREG.md') if checks['prereg_present'] else None
    info['prereg_pending_decisions'] = prereg_pending()
    # the calibration round (DESIGN §5, PREREG §4-§5; calibrate.py): required before PREREG can be hashed for a
    # harness-learner root; recorded for every root
    calibration = calibration_record()
    info['calibration'] = calibration
    if arm.learner == 'harness':
        checks['calibration_recorded'] = (calibration is not None and calibration.get('status') not in (None, 'failed')
                                          and calibration.get('dry_run') is False)      # a dry run is not the calibration
        if not checks['calibration_recorded']:
            info['prereg_pending_decisions'].append('[DECISION PENDING] calibration outcome missing: run calibrate.py '
                                                    '(preflight/calibration.json) before hashing PREREG')
    if suites:
        results = {name: pytest(suite_files(files)) for name, files in SUITES.items()}
        learners = sorted({str(p.relative_to(ROOT)) for pattern in LEARNER_TESTS for p in ROOT.glob(pattern)})
        results['learners'] = pytest(learners)
        results['live_codex'] = pytest(suite_files(LIVE), marker='live_codex')
        info['suites'] = results
        for name, value in results.items():
            checks[f'suite_{name}'] = value['passed']
        if arm.learner is not None:
            checks['learner_smoke_fake_model'] = results['learners']['passed'] and bool(learners)
    passed = all(v for v in checks.values() if isinstance(v, bool))
    return passed, checks, info


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--offline-only', action='store_true', help='$0: skip the paid smoke; no readiness file')
    parser.add_argument('--no-suites', action='store_true', help='skip the pytest suites (debugging only)')
    parser.add_argument('--replay-v9a', type=Path, metavar='ROOT',
                        help='$0, separate from readiness: replay the recorded tool commands of this read-only '
                             'evidence root against the unchanged W0 tools (>= 200 rebuilt, 0 changed)')
    parser.add_argument('--replay-limit', type=int, default=None, help='replay only the N most recent invocations')
    parser.add_argument('--replay-min-jobs', type=int, default=REPLAY_MIN_JOBS)
    parser.add_argument('--replay-tables-index', type=Path, default=None, metavar='FILE',
                        help='replay over this tables index instead of the evidence root\'s own (the v10 index)')
    args = parser.parse_args()
    if args.replay_v9a is not None:
        record = replay_v9a(args.replay_v9a, min_jobs=args.replay_min_jobs, limit=args.replay_limit,
                            out=ROOT / 'preflight/replay-v9a.json', tables_index=args.replay_tables_index)
        print(json.dumps({k: v for k, v in record.items() if k != 'table'}, ensure_ascii=False, indent=2))
        raise SystemExit(0 if record['passed'] else 1)
    passed, checks, info = offline(suites=not args.no_suites)
    print(json.dumps({'offline_checks_passed': passed, **checks}, ensure_ascii=False, indent=2))
    c.save(ROOT / 'preflight/offline-checks.json', {'passed': passed, 'checks': checks, 'info': info, 'utc': c.utc()})
    if not passed or args.offline_only or args.no_suites:
        raise SystemExit(0 if passed else 1)
    key = os.environ.get('FIREWORKS_API_KEY') or getpass.getpass('Fireworks API key (hidden): ')
    import run as runner
    name = f"smoke-{1 + len(list((ROOT / 'preflight').glob('smoke-*'))):02}"
    smoke = runner.smoke(key, name)
    readiness = {'passed': passed and smoke['passed'], 'offline_checks_passed': passed, 'offline_checks': checks,
                 'paid_model_roundtrip_passed': smoke['passed'], 'smoke': name, 'campaign': info['campaign'],
                 'order_sha256': info['order_sha256'], 'w0': info['w0'], 'seed_parity': info['seed_parity'],
                 'model_visible': info['model_visible'], 'w0_idempotence': info['w0_idempotence'],
                 'tool_runner_smoke': {k: v for k, v in info['tool_runner_smoke'].items() if k != 'tools'},
                 'question_count': info['question_count'], 'subset': info.get('subset'),
                 'temperature': info['temperature'], 'top_p': info['top_p'],
                 'prereg_sha256': info['prereg_sha256'], 'prereg_pending_decisions': info['prereg_pending_decisions'],
                 'gold_feedback_use_resolved': True,
                 'gold_feedback_resolution': 'Operator explicitly requested completed-block reference release to the learner arms (same protocol as v4-v9); issue-47 references withheld; not a publisher permission claim.',
                 'source_hashes': {n: c.sha(ROOT / n) for n in source_names() if (ROOT / n).is_file()},
                 'source_trees': source_trees(),
                 'created_utc': c.utc()}
    c.save(ROOT / 'preflight/readiness.json', readiness)
    print(json.dumps({'readiness_passed': readiness['passed']}))
    raise SystemExit(0 if readiness['passed'] else 1)


if __name__ == '__main__':
    main()
