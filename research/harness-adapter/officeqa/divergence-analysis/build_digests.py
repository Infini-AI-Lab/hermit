"""Build compact, readable trajectory digests of the three v8 GLM arms for the divergence analysis.

  digests/NNN/<arm>.md   one file per question and arm: status line, then every reasoning note,
                         message, and shell command (with a truncated output) in order.
  index.json             per question: which systems were right (0/1 grades only), run stats,
                         path of the adapter context the actor saw, Opus log folder.

No reference answer is written anywhere; grades are 0/1 only.
"""
import json
from pathlib import Path

B = Path(__file__).resolve().parent.parent
OUT = Path(__file__).resolve().parent
ARMS = {'none': ('officeqa-pro133-none-v8-work', 'baseline'),
        'prompt': ('officeqa-pro133-prompt-v8-work', 'adapted'),
        'cards': ('officeqa-pro133-cards-v8-work', 'adapted')}
HEAD, TAIL, REASON = 1500, 500, 900


def clip(text, head=HEAD, tail=TAIL):
    text = text or ''
    if len(text) <= head + tail + 40:
        return text
    return f'{text[:head]}\n… [{len(text) - head - tail} chars omitted] …\n{text[-tail:]}'


def digest(arm, ordinal):
    root, name = ARMS[arm]
    run = B / root / 'runs' / name / f'{ordinal:03}'
    summary = json.loads((run / 'summary.json').read_text())
    lines = [f'# {arm} — question {ordinal:03} ({summary["task_id"]})', '',
             f'status={summary["status"]} failure_kind={summary.get("failure_kind")} '
             f'elapsed={summary["elapsed_seconds"]:.0f}s requests={summary["requests"]} '
             f'input_tokens={summary["usage"]["input_tokens"]} output_tokens={summary["usage"]["output_tokens"]} '
             f'agents_md_bytes={summary["agents_md_bytes"]} format_valid={summary.get("answer_format_valid")}',
             f'final answer submitted: {summary.get("answer")!r}',
             f'adapter context the actor saw: {run / "workspace" / "AGENTS.md"}',
             f'raw events: {run / "records" / "events.jsonl"}', '', '## Trajectory', '']
    step = 0
    for raw in open(run / 'records' / 'events.jsonl', errors='ignore'):
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if row.get('type') != 'item.completed':
            continue
        item = row['item']
        kind = item.get('type')
        if kind == 'reasoning':
            step += 1
            lines.append(f'[{step}] THINK: {clip(item.get("text", ""), REASON, 200)}')
        elif kind == 'agent_message':
            step += 1
            lines.append(f'[{step}] SAY: {clip(item.get("text", ""), REASON, 300)}')
        elif kind == 'command_execution':
            step += 1
            lines.append(f'[{step}] RUN (exit {item.get("exit_code")}): {clip(item.get("command", ""), 2500, 300)}')
            lines.append('```')
            lines.append(clip(item.get('aggregated_output', '')))
            lines.append('```')
        elif kind == 'file_change':
            step += 1
            lines.append(f'[{step}] FILE_CHANGE: {json.dumps(item.get("changes", item))[:600]}')
        elif kind == 'error':
            msg = item.get('message', '')
            if 'Under-development features' in msg or 'Model metadata' in msg:
                continue
            step += 1
            lines.append(f'[{step}] ERROR: {clip(msg, 600, 100)}')
        elif kind not in (None,):
            step += 1
            lines.append(f'[{step}] {kind.upper()}: {json.dumps(item)[:600]}')
    path = OUT / 'digests' / f'{ordinal:03}' / f'{arm}.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(lines) + '\n')
    return summary, run


def main():
    grades = json.loads((B / 'opus-officeqa-133/evaluation/grades.json').read_text())['per_question']
    tasks = {t['ordinal']: t for t in json.loads((B / 'opus-officeqa-133/questions.json').read_text())}
    index = []
    for q in grades:
        o = q['ordinal']
        entry = {'ordinal': o, 'task_id': q['task_id'], 'question': tasks[o]['question'],
                 'correct': {k: q[k] for k in ('opus_final', 'opus_solver_structured', 'none', 'prompt', 'cards')},
                 'opus_logs': str(B / 'opus-officeqa-133/logs' / f'{o:03}'), 'arms': {}}
        for arm in ARMS:
            summary, run = digest(arm, o)
            entry['arms'][arm] = {'status': summary['status'], 'failure_kind': summary.get('failure_kind'),
                                  'elapsed': round(summary['elapsed_seconds']), 'requests': summary['requests'],
                                  'input_tokens': summary['usage']['input_tokens'],
                                  'agents_md_bytes': summary['agents_md_bytes'],
                                  'answer_empty': not str(summary.get('answer') or '').strip(),
                                  'digest': str(OUT / 'digests' / f'{o:03}' / f'{arm}.md'),
                                  'adapter_context': str(run / 'workspace' / 'AGENTS.md')}
        index.append(entry)
    (OUT / 'index.json').write_text(json.dumps(index, indent=1, ensure_ascii=False) + '\n')
    print('questions', len(index))


if __name__ == '__main__':
    main()
