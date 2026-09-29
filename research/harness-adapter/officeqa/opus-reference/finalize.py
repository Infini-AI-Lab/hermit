"""Finalize the Opus OfficeQA 133 run: attach agent transcripts, seal answers, then grade.

  python finalize.py transcripts   copy each agent transcript into logs/NNN/transcripts/
  python finalize.py seal          write predictions-final.jsonl + predictions-solver.jsonl and seal.json
  python finalize.py grade         grade the sealed files with the pinned reward.py and compare with v8

Sealing happens before any answer key is opened; grading refuses to run without a matching seal.
"""
from datetime import datetime, timezone
import glob
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import sys

W = Path(__file__).resolve().parent
B = W.parent
WORKFLOWS = Path('/Users/albertzhao/.claude/projects/-Users-albertzhao--codex--chatgpt-projects-g-p-6aa867f0012881918044d250b055e9dd/'
                 '3c1bdbf8-e746-47dd-9d26-6d6f3123e882/subagents/workflows')
RUNS = {'wf_37716070-117': 'attempt1', 'wf_32e17a79-45e': 'rerun'}
RERUN = {2, 3, 4, 15, 19, 20, 45, 50, 63, 71, 87, 92, 119, 127}
V8 = {'none': ('officeqa-pro133-none-v8-work', 'baseline'), 'prompt': ('officeqa-pro133-prompt-v8-work', 'adapted'),
      'cards': ('officeqa-pro133-cards-v8-work', 'adapted')}


def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def transcripts():
    copied = 0
    for run, tag in RUNS.items():
        for path in sorted((WORKFLOWS / run).glob('agent-*.jsonl')):
            # The first user turn may be the harness relay of the operator's request; the
            # task prompt (with the question path) follows in a later user turn.
            first, m = None, None
            for line in open(path, errors='ignore'):
                row = json.loads(line)
                msg = row.get('message') or {}
                if msg.get('role') == 'user':
                    text = msg.get('content') if isinstance(msg.get('content'), str) else json.dumps(msg.get('content'))
                    m = re.search(r'questions/(\d{3})\.json', text or '')
                    if m:
                        first = text
                        break
            if not m: continue
            n = int(m.group(1))
            role = 'reviewer' if 'adversarial reviewer' in first else 'solver'
            # Attempt-1 transcripts of rerun questions belong with the moved attempt-1 folder.
            base = W / 'logs' / ('_attempt1/' if (tag == 'attempt1' and n in RERUN) else '') / f'{n:03}'
            target = base / 'transcripts' / f'{role}.jsonl'
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            copied += 1
    print('copied', copied, 'transcripts')


def answer_of(folder):
    final = folder / 'final.txt'
    review = folder / 'review.json'
    solution = folder / 'solution.json'
    solver = json.loads(solution.read_text()).get('answer', '') if solution.is_file() else ''
    if final.is_file() and final.read_text().strip():
        return final.read_text().strip().splitlines()[0], str(solver)
    if review.is_file():
        return str(json.loads(review.read_text()).get('final_answer', '')), str(solver)
    return str(solver), str(solver)


def seal():
    tasks = json.loads((W / 'questions.json').read_text())
    finals, solvers, missing = [], [], []
    for t in tasks:
        folder = W / 'logs' / f'{t["ordinal"]:03}'
        final, solver = answer_of(folder) if folder.is_dir() else ('', '')
        if not final: missing.append(t['ordinal'])
        finals.append({'task_id': t['task_id'], 'answer': final})
        solvers.append({'task_id': t['task_id'], 'answer': solver})
    for name, rows in (('predictions-final.jsonl', finals), ('predictions-solver.jsonl', solvers)):
        (W / name).write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows))
    value = {'sealed_utc': datetime.now(timezone.utc).isoformat(), 'questions': len(tasks), 'missing_final': missing,
             'files': {name: sha(W / name) for name in ('predictions-final.jsonl', 'predictions-solver.jsonl')},
             'note': 'Sealed before any reference answer was opened for this run.'}
    (W / 'seal.json').write_text(json.dumps(value, indent=2) + '\n')
    print(json.dumps(value, indent=2))


def grade():
    seal = json.loads((W / 'seal.json').read_text())
    for name, digest in seal['files'].items():
        if sha(W / name) != digest: raise SystemExit('Predictions changed after sealing: ' + name)
    root = B / V8['none'][0]
    spec = importlib.util.spec_from_file_location('reward', root / 'prepared/private/reward.py')
    reward = importlib.util.module_from_spec(spec); spec.loader.exec_module(reward)
    key = {json.loads(l)['task_id']: json.loads(l)['answer'] for l in open(root / 'prepared/private/answers.jsonl')}
    tasks = json.loads((W / 'questions.json').read_text())
    grades = {}
    for name in ('final', 'solver'):
        preds = {json.loads(l)['task_id']: json.loads(l)['answer'] for l in open(W / f'predictions-{name}.jsonl')}
        grades['opus_' + name] = {t['task_id']: (int(reward.score_answer(key[t['task_id']], preds[t['task_id']], tolerance=0.0)),
                                                 int(reward.score_answer(key[t['task_id']], preds[t['task_id']], tolerance=0.01)))
                                  for t in tasks}
    for arm, (rootname, armname) in V8.items():
        rows = json.loads((B / rootname / f'private-evaluation/{armname}-grades.json').read_text())['results']
        grades[arm] = {r['task_id']: (int(r['strict_score']), int(r['one_percent_score'])) for r in rows}
    ids = [t['task_id'] for t in tasks]
    summary = {name: {'strict': sum(g[i][0] for i in ids), 'one_percent': sum(g[i][1] for i in ids)} for name, g in grades.items()}
    per_question = [{'ordinal': t['ordinal'], 'task_id': t['task_id'], **{name: grades[name][t['task_id']][0] for name in grades}}
                    for t in tasks]
    out = W / 'evaluation'
    out.mkdir(exist_ok=True)
    (out / 'grades.json').write_text(json.dumps({'graded_utc': datetime.now(timezone.utc).isoformat(), 'seal': seal,
                                                'summary': summary, 'per_question': per_question}, indent=1) + '\n')
    print(json.dumps(summary, indent=1))
    o = grades['opus_final']
    for arm in ('none', 'prompt', 'cards'):
        a = sum(1 for i in ids if o[i][0] > grades[arm][i][0]); b = sum(1 for i in ids if o[i][0] < grades[arm][i][0])
        print(f'opus_final vs {arm}: opus only {a}, {arm} only {b}')
    print('reviewer changed answers:', sum(1 for i in ids if o[i] != grades['opus_solver'][i]),
          '| fixed', sum(1 for i in ids if o[i][0] > grades['opus_solver'][i][0]),
          '| broke', sum(1 for i in ids if o[i][0] < grades['opus_solver'][i][0]))
    allglm_wrong = [t['ordinal'] for t in tasks if not any(grades[a][t['task_id']][0] for a in V8)]
    print('questions all three GLM arms missed:', len(allglm_wrong), '| opus right on', sum(o[tasks[n-1]['task_id']][0] for n in allglm_wrong))


if __name__ == '__main__':
    {'transcripts': transcripts, 'seal': seal, 'grade': grade}[sys.argv[1]]()
