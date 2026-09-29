"""Self-contained, answer-free HTML progress page for one v9 root (one arm, one repeat). No model calls,
no remote assets, no questions, answers or keys."""
import argparse
from collections import Counter
from html import escape
import json
from pathlib import Path
import tempfile
import os


def _read(path):
    return json.loads(Path(path).read_text())


TITLES = {'plain': 'GLM + Codex (plain: L0 only)', 'text-static': 'GLM + Codex + seed text (static)',
          'text-evolve': 'GLM + Codex + seed text + text learner',
          'mech-static': 'GLM + Codex + executed seed rules (static)',
          'mech-evolve': 'GLM + Codex + executed seed rules + mechanism learner'}


def render(root):
    root = Path(root)
    state = _read(root / 'state.json')
    arm = state.get('arm') or {}
    total = state.get('question_count') or 133
    metrics_path = root / 'private-evaluation/paired-metrics.json'
    metrics = _read(metrics_path) if metrics_path.is_file() else {}
    e = lambda value: escape(str(value))
    results = state.get('results', [])
    statuses = Counter(r.get('status') for r in results)
    scored = (metrics.get('arms') or {}).get(arm.get('name'))
    undisputed = metrics.get('undisputed')
    headline = (f"{scored['correct']:g}/{scored['n']} strict · {scored['one_percent_accuracy']:.0%} at 1% · "
                f"undisputed {undisputed['correct']:g}/{undisputed['n']}" if scored and undisputed
                else 'Grades after all questions are sealed')
    controller = [r.get('controller') or {} for r in results]
    ctl = ''
    if arm.get('controller'):
        def total_of(key):
            return sum((c.get(key) or 0) for c in controller if isinstance(c.get(key), (int, float)))
        loops = sum(((c.get('inserts_by_reason') or {}).get('loop_guard') or 0) for c in controller)
        fallback = sum(1 for r in results if r.get('fallback_differs'))
        ctl = (f"<section><h2>Controller</h2><p>submits {total_of('submits'):g} · gate rounds {total_of('gate_rounds'):g}"
               f" · re-queries {total_of('enforce_requeries'):g} · loop notes {loops} · compactions {total_of('compactions'):g}"
               f" · controller errors {total_of('controller_errors'):g} · final differs from last submit {fallback}</p></section>")
    rounds = ''.join(f"<tr><td>{r.get('block')}</td><td>{e(r.get('status'))}</td><td>{'yes' if r.get('activated') else 'no'}</td>"
                     f"<td>{e((r.get('check') or {}).get('notes', ''))}</td><td>{e((r.get('check') or {}).get('rules', ''))}</td></tr>"
                     for r in state.get('learning', []))
    blocks = state.get('blocks', {})
    done = sum(1 for b in blocks.values() if b.get('status') == 'done')
    refresh = '<meta http-equiv="refresh" content="30">' if state.get('status') in ('running', 'grading') else ''
    notes = [json.dumps(w) for w in state.get('warnings', [])] + ([str(state['error'])] if state.get('error') else [])
    warnings = ('<section><h2>Warnings</h2><ul>' + ''.join(f'<li>{e(n)}</li>' for n in notes) + '</ul></section>') if notes else ''
    learning = (f'<section><h2>Learning rounds</h2><table><tr><th>Block</th><th>Status</th><th>Activated</th>'
                f'<th>Notes</th><th>Rules</th></tr>{rounds}</table></section>') if arm.get('learner') else ''
    return f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
{refresh}<title>OfficeQA v9 {e(arm.get('name', ''))}</title><style>
:root{{--bg:#f4f5ef;--fg:#172b29;--card:#fff;--line:#d8dfd6}}
@media (prefers-color-scheme: dark){{:root{{--bg:#131a19;--fg:#e4ece9;--card:#1c2523;--line:#33403d}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:16px/1.6 system-ui,sans-serif}}main{{max-width:960px;margin:auto;padding:32px 16px}}
article,section{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 20px;margin:16px 0}}
table{{border-collapse:collapse;width:100%}}td,th{{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left}}
</style><main><h1>OfficeQA Pro · {e(total)} questions{e(' (subset ' + state['subset']['name'] + ')') if state.get('subset') else ''} · v9</h1>
<p>Arm <code>{e(arm.get('name'))}</code> (repeat {e(state.get('repeat'))}) · status <b>{e(state.get('status'))}</b>
 · started {e(state.get('started_utc'))} · blocks done {done} · active learning block {e(state.get('active_learning_block') or 'none')}</p>
<article><h2>{e(TITLES.get(arm.get('name'), arm.get('name')))}</h2><p><b>{len(results)}/{total}</b> completed</p>
<p>{e(headline)}</p><p>{e(dict(statuses))}</p></article>
{ctl}{warnings}{learning}
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
