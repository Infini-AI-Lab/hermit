"""Self-contained, answer-free HTML progress page for one v8 root. No model calls or remote assets."""
import argparse
from collections import Counter
from html import escape
import json
from pathlib import Path
import tempfile
import os


def _read(path):
    return json.loads(Path(path).read_text())


def render(root):
    state = _read(root / 'state.json')
    config = _read(root / '.task-paths.json')
    total = state.get('question_count') or 133
    metrics_path = root / 'private-evaluation/paired-metrics.json'
    metrics = _read(metrics_path) if metrics_path.is_file() else {}
    e = lambda value: escape(str(value))
    titles = {'baseline': 'GLM + Codex (no adapter)',
              'adapted': {'full_prompt_only_unbounded': 'GLM + Codex + prompt-only adapter',
                          'full_cards_unbounded': 'GLM + Codex + cards adapter'}.get(config.get('profile'), 'GLM + Codex + adapter')}
    arms = ''
    for arm, value in state['arms'].items():
        statuses = Counter(r.get('status') for r in value['results'])
        scored = metrics.get('arms', {}).get(arm)
        headline = (f"{scored['correct']}/{total} strict · {scored['one_percent_accuracy']:.0%} at 1%" if scored
                    else 'Grades after all questions are sealed')
        arms += (f"<article><h2>{e(titles[arm])}</h2><p><b>{value['completed']}/{total}</b> completed</p>"
                 f"<p>{e(headline)}</p><p>{e(dict(statuses))}</p></article>")
    rounds = ''.join(f"<tr><td>{r.get('block')}</td><td>{e(r.get('status'))}</td><td>{'yes' if r.get('activated') else 'no'}</td>"
                     f"<td>{e(r.get('admitted', ''))}</td></tr>" for r in state.get('learning', []))
    refresh = '<meta http-equiv="refresh" content="30">' if state.get('status') in ('running', 'grading') else ''
    notes = [json.dumps(w) for w in state.get('warnings', [])] + ([str(state['error'])] if state.get('error') else [])
    warnings = ('<section><h2>Warnings</h2><ul>' + ''.join(f'<li>{e(n)}</li>' for n in notes) + '</ul></section>') if notes else ''
    return f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
{refresh}<title>OfficeQA Pro 133 v8</title><style>
body{{margin:0;background:#f4f5ef;color:#172b29;font:16px/1.6 system-ui,sans-serif}}main{{max-width:960px;margin:auto;padding:32px 16px}}
article,section{{background:#fff;border:1px solid #d8dfd6;border-radius:12px;padding:16px 20px;margin:16px 0}}
table{{border-collapse:collapse;width:100%}}td,th{{padding:6px 8px;border-bottom:1px solid #e2e7df;text-align:left}}
</style><main><h1>OfficeQA Pro · 133 questions · v8</h1>
<p>Profile <code>{e(config.get('profile'))}</code> · arms {e(', '.join(state['arms']))} · status <b>{e(state.get('status'))}</b>
 · started {e(state.get('started_utc'))} · active learning block {e(state.get('active_learning_block') or 'none')}</p>
{arms}{warnings}<section><h2>Learning rounds</h2><table><tr><th>Block</th><th>Status</th><th>Activated</th><th>Admitted</th></tr>{rounds}</table></section>
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
