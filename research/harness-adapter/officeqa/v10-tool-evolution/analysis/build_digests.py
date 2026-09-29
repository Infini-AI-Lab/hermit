"""Readable trajectory digests of the v9 pilot cells (a text-static T0, b mech-static T0 [pilot 1], b2 mech-static T0
[pilot 2: advice text + 5 rules], c text-static T1.0) on the same first-50 questions, for the v10 adapter analysis.

  digests/NNN/<cell>.md          status line, then every reasoning note, message and shell command (clipped output),
                                 plus (controller cells) the harness inserts the model saw (loop notes, gate results).
  digests/NNN/<cell>.outline.md  one line per step.
  index.json                     per question: 0/1 correctness per cell (+ v8 arms, Opus), value-free error bins,
                                 run stats, paths. No reference answer is written anywhere.
"""
import json, math, re, importlib.util
from pathlib import Path

B = Path(__file__).resolve().parent.parent
OUT = Path(__file__).resolve().parent
CELLS = {'a': 'v9-p1-a', 'b': 'v9-p1-b', 'b2': 'v9-p2-a', 'c': 'v9-p1-c'}
HEAD, TAIL, REASON = 1500, 500, 900
V8 = B / 'officeqa-pro133-none-v8-work'
spec = importlib.util.spec_from_file_location('reward', V8 / 'prepared/private/reward.py')
reward = importlib.util.module_from_spec(spec); spec.loader.exec_module(reward)
KEY = {json.loads(l)['task_id']: json.loads(l)['answer'] for l in open(V8 / 'prepared/private/answers.jsonl')}
OPUS = {q['task_id']: q for q in json.load(open(B / 'opus-officeqa-133/evaluation/grades.json'))['per_question']}
V8IDX = {e['task_id']: e for e in json.load(open(B / 'officeqa-divergence-analysis/index.json'))}
P1IDX = {e['task_id']: e for e in json.load(open(B / 'officeqa-v9-design/pilot-analysis/index.json'))}
NUM = re.compile(r'-?\d[\d,]*\.?\d*(?:[eE][-+]?\d+)?')


def nums(text):
    out = []
    for m in NUM.findall(str(text)):
        try: out.append(float(m.replace(',', '')))
        except ValueError: pass
    return out


def bin_of(pred, ref):
    if not str(pred).strip(): return 'empty'
    p, r = nums(pred), nums(ref)
    if len(p) != len(r):
        alt = [float(x) for x in re.findall(r'-?\d+\.?\d*(?:[eE][-+]?\d+)?', str(pred))]
        if len(alt) == len(r): p = alt
    if not r: return 'non_numeric_reference'
    if not p: return 'non_numeric_prediction'
    if len(p) != len(r): return f'count_mismatch({len(p)} vs {len(r)})'
    worst, kinds = 0.0, set()
    for a, b in zip(p, r):
        if b == 0: err = abs(a)
        else:
            err = abs(a - b) / abs(b)
            if a != 0 and a * b < 0 and abs(abs(a) - abs(b)) / abs(b) < 0.01: kinds.add('sign_flip')
            if a != 0 and a * b > 0:
                ratio = math.log10(abs(a) / abs(b))
                if abs(ratio) > 0.9 and abs(ratio - round(ratio)) < 0.01: kinds.add('scale_10^%d' % round(ratio))
        worst = max(worst, err)
    if kinds: return '+'.join(sorted(kinds))
    for limit, label in ((1e-9, 'numerically_equal(format?)'), (0.001, 'within_0.1pct'), (0.01, 'within_1pct'),
                         (0.05, '1-5pct'), (0.25, '5-25pct')):
        if worst <= limit: return label
    return '>25pct'


def clip(text, head=HEAD, tail=TAIL):
    text = text or ''
    if len(text) <= head + tail + 40: return text
    return f'{text[:head]}\n… [{len(text) - head - tail} chars omitted] …\n{text[-tail:]}'


def inserts(run):
    """Harness text the model saw that is not in events.jsonl (controller cells): ledger 'note' events (loop notes,
    post-compaction ledger splices, enforcement messages)."""
    path = run / 'api/ledger.jsonl'
    if not path.is_file(): return []
    rows = []
    for raw in open(path, errors='ignore'):
        try: row = json.loads(raw)
        except ValueError: continue
        if row.get('kind') != 'note': continue
        text = row.get('text') or ''
        rows.append((row.get('call'), row.get('reason') or row.get('note_kind') or 'note', clip(str(text), 900, 200)))
    return rows


def digest(cell, ordinal):
    run = B / CELLS[cell] / 'runs/episodes' / f'{ordinal:03}'
    if not (run / 'summary.json').is_file(): return None
    s = json.loads((run / 'summary.json').read_text())
    usage = s.get('usage') or {}
    lines = [f'# cell {cell} — question {ordinal:03} ({s["task_id"]})', '',
             f'status={s["status"]} failure_kind={s.get("failure_kind")} elapsed={s.get("elapsed_seconds", 0):.0f}s '
             f'requests={s.get("requests")} input_tokens={usage.get("input_tokens")} output_tokens={usage.get("output_tokens")} '
             f'agents_md_bytes={s.get("agents_md_bytes")} format_valid={s.get("answer_format_valid")} '
             f'compactions={(s.get("controller") or {}).get("compactions")} gate_rounds={(s.get("controller") or {}).get("gate_rounds")}',
             f'final answer submitted: {s.get("answer")!r}',
             f'adapter context the actor saw: {run / "workspace" / "AGENTS.md"}',
             f'raw events: {run / "records" / "events.jsonl"}', '']
    ins = inserts(run)
    if ins:
        lines += ['## Harness inserts the model saw (call, reason, text)', '']
        for call, reason, text in ins:
            lines.append(f'- call {call} [{reason}]: {text}')
        lines.append('')
    lines += ['## Trajectory', '']
    outline, step = [], 0
    for raw in open(run / 'records/events.jsonl', errors='ignore'):
        try: row = json.loads(raw)
        except ValueError: continue
        if row.get('type') != 'item.completed': continue
        item = row['item']; kind = item.get('type')
        if kind == 'reasoning':
            step += 1; t = item.get('text', '')
            lines.append(f'[{step}] THINK: {clip(t, REASON, 200)}'); outline.append(f'[{step}] THINK: {t[:160]!r}')
        elif kind == 'agent_message':
            step += 1; t = item.get('text', '')
            lines.append(f'[{step}] SAY: {clip(t, REASON, 300)}'); outline.append(f'[{step}] SAY: {t[:160]!r}')
        elif kind == 'command_execution':
            step += 1; c = item.get('command', '')
            lines.append(f'[{step}] RUN (exit {item.get("exit_code")}): {clip(c, 2500, 300)}')
            lines.append('```'); lines.append(clip(item.get('aggregated_output', ''))); lines.append('```')
            outline.append(f'[{step}] RUN (exit {item.get("exit_code")}): {c[:200]!r}')
        lines.append('')
    d = OUT / 'digests' / f'{ordinal:03}'; d.mkdir(parents=True, exist_ok=True)
    (d / f'{cell}.md').write_text('\n'.join(lines))
    (d / f'{cell}.outline.md').write_text('\n'.join(outline) + '\n')
    ctl = s.get('controller') or {}
    return {'status': s['status'], 'failure_kind': s.get('failure_kind'), 'elapsed': round(s.get('elapsed_seconds', 0)),
            'requests': s.get('requests'), 'input_tokens': usage.get('input_tokens'), 'output_tokens': usage.get('output_tokens'),
            'steps': step, 'answer_empty': not str(s.get('answer') or '').strip(),
            'compactions': ctl.get('compactions'), 'gate_rounds': ctl.get('gate_rounds'), 'submits': ctl.get('submits'),
            'correct': int(reward.score_answer(KEY[s['task_id']], s.get('answer') or '', tolerance=0.0)),
            'error_bin': None if reward.score_answer(KEY[s['task_id']], s.get('answer') or '', tolerance=0.0) else bin_of(s.get('answer') or '', KEY[s['task_id']]),
            'digest': str(d / f'{cell}.md'), 'outline': str(d / f'{cell}.outline.md'),
            'adapter_context': str(run / 'workspace/AGENTS.md'), 'episode_dir': str(run)}


order = json.load(open(B / 'v9-p1-a/state.json'))['results']
index = []
for r in sorted(order, key=lambda x: x['ordinal']):
    o, tid = r['ordinal'], r['task_id']
    task = json.loads((B / 'v9-p1-a/runs/episodes' / f'{o:03}' / 'task.json').read_text())
    entry = {'ordinal': o, 'task_id': tid, 'question': task['question'], 'issue47': bool(V8IDX.get(tid, {}).get('issue47')),
             'correct': {}, 'cells': {}}
    for cell in CELLS:
        info = digest(cell, o)
        if info is None: continue
        entry['cells'][cell] = info
        entry['correct'][cell] = info.pop('correct')
    g = OPUS[tid]
    entry['correct'].update({'v8_none': int(g['none']), 'v8_prompt': int(g['prompt']), 'v8_cards': int(g['cards']), 'opus': int(g['opus_final'])})
    entry['opus_logs'] = V8IDX[tid]['opus_logs']
    entry['v8_ordinal'] = V8IDX[tid]['ordinal']
    entry['v8_analysis'] = str(B / 'officeqa-divergence-analysis/per-question' / f'{V8IDX[tid]["ordinal"]:03}.json')
    p1 = B / 'officeqa-v9-design/pilot-analysis/q' / f'{tid}.json'
    entry['pilot1_analysis'] = str(p1) if p1.is_file() else None
    index.append(entry)
json.dump(index, open(OUT / 'index.json', 'w'), indent=1)
n = {c: sum(1 for e in index if c in e['correct']) for c in CELLS}
print('questions', len(index), 'cells done', n)
print('right', {c: sum(e['correct'].get(c, 0) for e in index) for c in list(CELLS) + ['v8_none', 'v8_prompt', 'v8_cards', 'opus']})
