"""Self-contained, answer-free HTML. No model calls or remote assets."""
import argparse
from html import escape
import json
from pathlib import Path
import tempfile
import os

from export_results import summarize


def render(summary, pairs):
    full=str(summary.get('profile','')).startswith('full_')
    mwm=summary.get('architecture','mwm')=='mwm'
    banner=('New full run ('+str(summary.get('profile'))+') · One-hour wall clock per question (both arms); no cumulative token, model-call or cost cap; transport retries are counted, never cut. Provider limits still apply. Completed-batch grades are released to the learner.' if full else
            'Historical result: use the recorded framework revision above. Updating the repository does not rerun or reproduce these scores.')
    cards=summary.get('architecture')=='cards'
    policy=('Actors have a 3,600 s wall clock per question; learner inference is uncapped. The learner runs a controller-checked postmortem over the sealed batch (answers and strict/1% grades released), investigates documents, calculates, and '
            +('evolves native memory, examples, tools and role specifications. ' if mwm else
              'proposes at most two cards; each is admitted only after the actor is re-run with it on its support and control questions. ' if cards else
              'edits ADAPTER.md only. ')
            +'Active learning batch: '+str(summary.get('active_learning_block') or 'none') if full else
            'Each actor and each learner: 30 minutes, 65,536 cumulative output tokens, no request-count limit.')
    last=('Round 10 is skipped: no later actor would use it.' if any(r.get('status')=='skipped_unused' for r in summary['learning'])
          else 'Round 10 happens after all scored questions.')
    refresh='<meta http-equiv="refresh" content="20">' if full and summary['status'] in ('running','grading') else ''
    def e(value):
        return escape(str(value))
    def pct(value):
        return 'Pending' if value is None else f'{value:.0%}'
    arms = summary['arms']
    cards = ''.join(f'''<article><h2>{title}</h2><strong>{pct(arms[arm]['strict_accuracy'])}</strong>
        <p>Strict accuracy · {arms[arm]['completed']}/50 completed</p>
        <div class="bar"><i style="width:{100*(arms[arm]['strict_accuracy'] or 0)}%"></i></div>
        <p>1% numeric tolerance: {pct(arms[arm]['one_percent_accuracy'])}<br>
        Valid final answers: {arms[arm]['valid_final_answers']}/50</p></article>'''
        for arm, title in [('baseline', 'A · GLM + Codex'), ('adapted', 'B · GLM + Codex + cards adapter (preamble + verified deck)' if cards else 'B · GLM + Codex + adapter')])
    rounds = ''.join(f'<tr><td>{r["block"]}</td><td>{"Adopted" if r.get("activated") else "Retained previous"}</td>'
                     f'<td>{e(r["reason"])}</td><td>{e(r["model_calls"])}</td><td>{e(r["completion_tokens"])}</td></tr>'
                     for r in summary['learning'])
    tasks = ''.join('<tr><td>'+str(p['ordinal'])+'</td><td>'+e(p['task_id'])+'</td>'+''.join(
        '<td>'+e(p.get(a, {}).get('strict_score', 'Pending'))+'</td><td>'+e(p.get(a, {}).get('status', 'Pending'))+'</td>'
        for a in ('baseline','adapted'))+'</tr>' for p in pairs)
    lineage = ''.join(f'<span>Q{r["questions"][0]}–{r["questions"][1]} <b>W{r["adapter_round"]}</b></span>' for r in summary['adapter_lineage'])
    caveats = ''.join(f'<li>{e(s)}</li>' for s in summary['limitations'])
    costs = summary.get('recorded_cost')
    cost_text = ('Not yet recorded' if not costs else
        f'Known formal API charges ≥ ${costs["formal_total"]["known_cost_lower_bound_usd"]:.4f}; '
        f'{costs["formal_total"]["unknown_usage_attempts"]} request(s) have unknown usage. '
        f'Public prices dated {e(costs["price_checked_date"])}; estimate, not invoice.')
    return f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
{refresh}<title>OfficeQA Pro · GLM / Codex paired evaluation</title><style>
*{{box-sizing:border-box}}body{{margin:0;background:#f4f5ef;color:#172b29;font:16px/1.6 system-ui,sans-serif}}main{{max-width:1100px;margin:auto;padding:44px 24px}}
h1{{font-size:clamp(28px,5vw,50px);line-height:1.1;margin:12px 0 20px}}h2{{font-size:21px}}.tag{{font:12px/1.5 monospace;text-transform:uppercase;letter-spacing:2px;color:#46655e}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,300px),1fr));gap:20px}}article,section{{background:white;border:1px solid #d8dfd6;border-radius:16px;padding:24px;margin:20px 0}}
strong{{font-size:56px}}.bar{{height:9px;border-radius:8px;background:#e9eee6;overflow:hidden}}i{{display:block;height:100%;background:#277367}}
.note{{background:#fff2d9;border-left:4px solid #bd8327;padding:16px}}.scroll{{overflow:auto}}table{{border-collapse:collapse;width:100%;font-size:14px}}th,td{{padding:10px;text-align:left;border-bottom:1px solid #e2e7df}}td{{overflow-wrap:anywhere}}th{{white-space:nowrap}}code{{overflow-wrap:anywhere}}.flow{{display:flex;flex-wrap:wrap;gap:10px}}.flow span{{background:#edf3eb;border-radius:8px;padding:10px}}footer{{font-size:13px;color:#46655e}}a{{color:#166653}}
</style><main><div class="tag">OfficeQA Pro / answer-supervised online adaptation</div><h1>Same 50 questions.<br>Two GLM + Codex conditions.</h1>
<p>Status: <b>{e(summary['status'])}</b> · Model: <code>{e(summary['model'])}</code><br>Framework used for these records: <code>{e(summary['framework_revision'])}</code></p>
<p class="note">{banner}</p>
<div class="cards">{cards}</div><section><h2>How learning works</h2><p>Seal five original submissions → release only those five reference answers → GLM reviews traces and edits memory → mechanically validate → next five questions.</p>
<p>{policy} A failed learning session keeps the previous adapter. {last}</p>
<div class="flow">{lineage}</div></section><section><h2>Learning rounds</h2><div class="scroll"><table><thead><tr><th>Round</th><th>Deployment</th><th>Outcome</th><th>Requests</th><th>Output tokens</th></tr></thead><tbody>{rounds}</tbody></table></div></section>
<section><h2>Interpretation and cost</h2><p>{cost_text}</p><ul>{caveats}</ul></section>
<section><h2>All paired outcomes</h2><p>1 = correct under strict grading; 0 = incorrect. Failed attempts remain in the denominator. Questions and answers are omitted.</p><div class="scroll"><table><thead><tr><th>#</th><th>Task ID</th><th>A score</th><th>A status</th><th>B score</th><th>B status</th></tr></thead><tbody>{tasks}</tbody></table></div></section>
<footer>Self-contained report · No external assets, credentials or answer keys.</footer></main></html>'''


def build(root):
    root = Path(root)
    frozen = root / 'freeze.json'
    revision = json.loads(frozen.read_text()).get('framework_revision', 'unrecorded') if frozen.exists() else 'unrecorded'
    summary, pairs = summarize(root, revision)
    with tempfile.NamedTemporaryFile('w', suffix='.html', dir=root, delete=False) as stream:
        stream.write(render(summary, pairs))
        temporary = Path(stream.name)
    target = root / 'report.html'
    os.replace(temporary, target)
    return target


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).parent)
    print(build(parser.parse_args().root))
