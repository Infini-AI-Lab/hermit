"""Self-contained, answer-free HTML progress page for one v10 root (one arm, one repeat). No model calls,
no remote assets, no questions, answers or keys.

Sections: the arm and its status; the strict / 1 % headline once graded; the per-block prequential table
(each block's questions were answered before its references were released: right so far, cumulative, the
adapter revision served); the controller counters (loop notes, hard blocks, compactions, splices; submits
only behind a gate) and the block average of the ledger's observation coverage (commands parsed by
invocation / recovered by header / raw only); the learning rounds with their admitted notes and tool
revisions (tier, files, patches, replay numbers, from the rounds' mechanical-admission.json); warnings.
"""
import argparse
from collections import Counter
from html import escape
import json
from pathlib import Path
import tempfile
import os


def _read(path):
    return json.loads(Path(path).read_text())


TITLES = {'plain': 'GLM + Codex (plain: base harness only)',
          'prompt-static': 'GLM + Codex + prompt adapter W0 (SEED.md; static)',
          'prompt-evolve': 'GLM + Codex + prompt adapter + scoped-note learner',
          'harness-static': 'GLM + Codex + harness adapter W0 (SEED.md, tools, controls; static)',
          'harness-evolve': 'GLM + Codex + harness adapter + harness learner (notes + tool revisions)',
          'harness-evolve-notes': 'GLM + Codex + harness adapter + scoped-note learner (notes only)'}
COVERAGE_KINDS = ('invocation', 'header', 'raw', 'none')


def coverage_average(records):
    """Block average of observation coverage records ({'commands', 'invocation', 'header', 'raw', 'none',
    'mode', ...}): the share of commands per parse kind, averaged over the episodes that recorded any."""
    shares = {kind: [] for kind in COVERAGE_KINDS}
    modes = Counter()
    for record in records:
        if not isinstance(record, dict):
            continue
        commands = record.get('commands') or 0
        modes[record.get('mode') or 'unknown'] += 1
        if not commands:
            continue
        for kind in COVERAGE_KINDS:
            shares[kind].append((record.get(kind) or 0) / commands)
    return {'episodes': len(next(iter(shares.values()))),
            'shares': {kind: (sum(v) / len(v) if v else None) for kind, v in shares.items()},
            'modes': dict(modes)}


def episode_coverage(root: Path, results):
    """observation_coverage of every scored episode (the episode summary, else the broker summary)."""
    out = []
    for row in results:
        record = row.get('observation_coverage') if isinstance(row, dict) else None
        if record is None:
            base = root / 'runs' / 'episodes' / f'{int(row.get("ordinal", 0)):03}'
            for path in (base / 'summary.json', base / 'api' / 'summary.json'):
                if path.is_file():
                    try:
                        record = _read(path).get('observation_coverage')
                    except ValueError:
                        record = None
                    if record is not None:
                        break
        if record is not None:
            out.append(record)
    return out


def admitted_revisions(root: Path, rounds):
    """Admitted tool revisions per learning round from evolution/BB/**/mechanical-admission.json
    (learner_common: `admitted_revision` id, `revision` record with kind, tier, files, patches, replay counts,
    `candidate_revision`): ids, tiers, file and patch counts and replay numbers only."""
    out = []
    for round_ in rounds:
        block = round_.get('block')
        if not isinstance(block, int):
            continue
        for path in sorted((root / 'evolution' / f'{block:02}').glob('**/mechanical-admission.json')):
            try:
                record = _read(path)
            except ValueError:
                continue
            entry = record.get('revision') or {}
            if not record.get('admitted_revision') or entry.get('admitted') is not True:
                continue
            files = entry.get('files') or []
            patches = entry.get('patches') or {}
            counts = (entry.get('replay') or {}).get('counts') or {}
            out.append({'block': block, 'id': entry.get('id'), 'kind': entry.get('kind'), 'tier': entry.get('tier'),
                        'files': len(files) if isinstance(files, list) else files,
                        'patches': sum(v for v in patches.values() if isinstance(v, int)) if isinstance(patches, dict) else None,
                        'replay_jobs': counts.get('jobs'), 'replay_changed': counts.get('changed_output'),
                        'note': bool(entry.get('note')), 'revision': record.get('candidate_revision')})
    return out


def block_table(state, metrics):
    """Per-block prequential rows: block, ordinals, done, right (strict, once graded), cumulative, adapter."""
    strict = {}
    arm_name = (state.get('arm') or {}).get('name')
    for row in metrics.get('results') or []:
        if isinstance(row, dict) and row.get('task_id') is not None:
            # data.grade rows carry the strict score under the arm's name (and '<arm>@1%' for the 1 % score)
            strict[row['task_id']] = row.get(arm_name, row.get('strict_score', row.get('strict')))
    by_ordinal = {r.get('ordinal'): r for r in state.get('results', []) if isinstance(r, dict)}
    rows, seen, right_total = [], 0, 0
    for number, record in sorted(state.get('blocks', {}).items(), key=lambda kv: int(kv[0])):
        ordinals = record.get('ordinals') or []
        completed = [by_ordinal[o] for o in ordinals if o in by_ordinal]
        grades = [strict.get(r.get('task_id')) for r in completed]
        graded = [g for g in grades if isinstance(g, (int, float))]
        right = sum(1 for g in graded if g >= 1) if graded and len(graded) == len(completed) and completed else None
        seen += len(completed)
        if right is not None:
            right_total += right
        rows.append({'block': int(number), 'n': len(ordinals), 'completed': len(completed), 'status': record.get('status'),
                     'right': right, 'cumulative': (right_total, seen) if right is not None else None,
                     'adapter_revision': record.get('adapter_revision')})
    return rows


def render(root):
    root = Path(root)
    state = _read(root / 'state.json')
    arm = state.get('arm') or {}
    total = state.get('question_count') or 133
    metrics_path = root / 'private-evaluation/paired-metrics.json'
    metrics = _read(metrics_path) if metrics_path.is_file() else {}
    e = lambda value: escape(str(value))  # noqa: E731
    results = state.get('results', [])
    statuses = Counter(r.get('status') for r in results)
    scored = (metrics.get('arms') or {}).get(arm.get('name'))
    undisputed = metrics.get('undisputed')
    headline = (f"{scored['correct']:g}/{scored['n']} strict · {scored['one_percent_accuracy']:.0%} at 1% · "
                f"undisputed {undisputed['correct']:g}/{undisputed['n']}" if scored and undisputed
                else 'Grades after all questions are sealed')
    blocks = block_table(state, metrics)
    block_rows = ''.join(
        f"<tr><td>{b['block']}</td><td>{b['completed']}/{b['n']}</td><td>{e(b['status'])}</td>"
        f"<td>{'—' if b['right'] is None else b['right']}</td>"
        f"<td>{'—' if b['cumulative'] is None else f'{b['cumulative'][0]}/{b['cumulative'][1]}'}</td>"
        f"<td><code>{e(b['adapter_revision'] or '—')}</code></td></tr>" for b in blocks)
    prequential = (f'<section><h2>Blocks (prequential: answered before the references were released)</h2>'
                   f'<table><tr><th>Block</th><th>Done</th><th>Status</th><th>Right (strict)</th><th>Cumulative</th>'
                   f'<th>Adapter served</th></tr>{block_rows}</table></section>') if blocks else ''
    controller = [r.get('controller') or {} for r in results]
    ctl = ''
    if arm.get('controller'):
        def total_of(key):
            return sum((c.get(key) or 0) for c in controller if isinstance(c.get(key), (int, float)))
        loops = sum(((c.get('inserts_by_reason') or {}).get('loop_guard') or 0) for c in controller)
        splices = sum(((c.get('inserts_by_reason') or {}).get('ledger') or 0) for c in controller)
        parts = [f'loop notes {loops}', f"hard blocks {total_of('hard_blocks'):g}", f"compactions {total_of('compactions'):g}",
                 f'ledger splices {splices}', f"controller errors {total_of('controller_errors'):g}"]
        if arm.get('gate'):
            fallback = sum(1 for r in results if r.get('fallback_differs'))
            parts += [f"submits {total_of('submits'):g}", f"gate rounds {total_of('gate_rounds'):g}",
                      f"re-queries {total_of('enforce_requeries'):g}", f'final differs from last submit {fallback}']
        coverage = coverage_average(episode_coverage(root, results))
        shares = ' · '.join(f"{kind} {'—' if v is None else f'{v:.0%}'}" for kind, v in coverage['shares'].items())
        ctl = (f"<section><h2>Controller (gate {'on' if arm.get('gate') else 'off'})</h2><p>{' · '.join(parts)}</p>"
               f"<p>Observation coverage (block average over {coverage['episodes']} episodes): {shares}"
               f" · parser modes {e(coverage['modes'])}</p></section>")
    rounds = state.get('learning', [])
    revisions = admitted_revisions(root, rounds) if arm.get('learner') else []
    round_rows = ''.join(
        f"<tr><td>{r.get('block')}</td><td>{e(r.get('status'))}</td><td>{'yes' if r.get('activated') else 'no'}</td>"
        f"<td>{e(', '.join(str(a) for a in (r.get('admitted') or [])) or '—')}</td>"
        f"<td>{e((r.get('check') or {}).get('notes', ''))}</td>"
        f"<td>{e(', '.join(str(x) for x in (r.get('retired') or [])) or '—')}</td></tr>" for r in rounds)
    revision_rows = ''.join(
        f"<tr><td>{r['block']}</td><td>{e(r['id'])}</td><td>{e(r['tier'] or '—')}</td><td>{e(r['files'])}</td>"
        f"<td>{e(r['patches'] if r['patches'] is not None else '—')}</td>"
        f"<td>{e(r['replay_jobs'] if r['replay_jobs'] is not None else '—')} / {e(r['replay_changed'] if r['replay_changed'] is not None else '—')}</td>"
        f"<td>{'yes' if r['note'] else 'no'}</td><td><code>{e(r['revision'] or '—')}</code></td></tr>" for r in revisions)
    learning = ''
    if arm.get('learner'):
        learning = (f'<section><h2>Learning rounds ({e(arm.get("learner"))} learner)</h2><table><tr><th>Block</th><th>Status</th>'
                    f'<th>Activated</th><th>Admitted</th><th>Notes served</th><th>Retired</th></tr>{round_rows}</table>')
        learning += (f'<h3>Admitted tool revisions</h3><table><tr><th>Block</th><th>Id</th><th>Tier</th><th>Files</th>'
                     f'<th>Patches</th><th>Replay jobs / changed</th><th>Bundled note</th><th>Revision</th></tr>{revision_rows}</table>'
                     if revisions else '<p>No tool revision admitted.</p>')
        learning += '</section>'
    done = sum(1 for b in state.get('blocks', {}).values() if b.get('status') == 'done')
    refresh = '<meta http-equiv="refresh" content="30">' if state.get('status') in ('running', 'grading') else ''
    notes = [json.dumps(w) for w in state.get('warnings', [])] + ([str(state['error'])] if state.get('error') else [])
    warnings = ('<section><h2>Warnings</h2><ul>' + ''.join(f'<li>{e(n)}</li>' for n in notes) + '</ul></section>') if notes else ''
    return f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
{refresh}<title>OfficeQA v10 {e(arm.get('name', ''))}</title><style>
:root{{--bg:#f4f5ef;--fg:#172b29;--card:#fff;--line:#d8dfd6}}
@media (prefers-color-scheme: dark){{:root{{--bg:#131a19;--fg:#e4ece9;--card:#1c2523;--line:#33403d}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:16px/1.6 system-ui,sans-serif}}main{{max-width:960px;margin:auto;padding:32px 16px}}
article,section{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 20px;margin:16px 0}}
table{{border-collapse:collapse;width:100%}}td,th{{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left}}
</style><main><h1>OfficeQA Pro · {e(total)} questions{e(' (subset ' + state['subset']['name'] + ')') if state.get('subset') else ''} · v10</h1>
<p>Arm <code>{e(arm.get('name'))}</code> (repeat {e(state.get('repeat'))}) · status <b>{e(state.get('status'))}</b>
 · started {e(state.get('started_utc'))} · blocks done {done} · active learning block {e(state.get('active_learning_block') or 'none')}</p>
<article><h2>{e(TITLES.get(arm.get('name'), arm.get('name')))}</h2><p><b>{len(results)}/{total}</b> completed</p>
<p>{e(headline)}</p><p>{e(dict(statuses))}</p></article>
{prequential}{ctl}{warnings}{learning}
<p>No questions, answers or keys are shown on this page.</p></main></html>'''


def build(root):
    root = Path(root)
    with tempfile.NamedTemporaryFile('w', suffix='.html', dir=root, delete=False) as stream:
        stream.write(render(root))
        temporary = Path(stream.name)
    target = root / 'report.html'
    os.replace(temporary, target)
    return target


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).parent)
    print(build(parser.parse_args().root))
