"""Per arm-question behaviour features from raw GLM events and Opus worklogs (value-free)."""
import json, re, statistics as st
from collections import Counter, defaultdict
from pathlib import Path
B = Path(__file__).resolve().parent.parent
OUT = Path(__file__).resolve().parent
ROOTS = {'none': ('officeqa-pro133-none-v8-work', 'baseline'), 'prompt': ('officeqa-pro133-prompt-v8-work', 'adapted'),
         'cards': ('officeqa-pro133-cards-v8-work', 'adapted')}
FILE = re.compile(r'treasury_bulletin_(\d{4})_(\d{2})')
LESSON = re.compile(r'\[?(L-[a-z0-9-]+)\]?')
CARD = re.compile(r'\bC0\d\d\b')
idx = json.loads((OUT / 'index.json').read_text())
rows = []
for e in idx:
    o = e['ordinal']
    for arm, (root, name) in ROOTS.items():
        run = B / root / 'runs' / name / f'{o:03}'
        cmds = fails = e127 = heredoc_fail = 0
        files_cmd, files_out = set(), set()
        think = []
        for raw in open(run / 'records/events.jsonl', errors='ignore'):
            try: r = json.loads(raw)
            except ValueError: continue
            if r.get('type') != 'item.completed': continue
            it = r['item']
            if it['type'] == 'command_execution':
                cmds += 1
                c = it.get('command', ''); out = it.get('aggregated_output', '') or ''
                if it.get('exit_code') not in (0, None): fails += 1
                if it.get('exit_code') == 127: e127 += 1
                if '<<' in c and it.get('exit_code') not in (0, None): heredoc_fail += 1
                files_cmd |= set(FILE.findall(c))
                files_out |= set(FILE.findall(out))
            elif it['type'] in ('reasoning', 'agent_message'):
                think.append(it.get('text', ''))
        text = '\n'.join(think)
        s = json.loads((run / 'summary.json').read_text())
        rows.append({'ordinal': o, 'arm': arm, 'correct': e['correct'][arm], 'opus': e['correct']['opus_final'],
                     'cmds': cmds, 'fails': fails, 'e127': e127, 'heredoc_fail': heredoc_fail,
                     'issues_opened': len(files_cmd), 'issues_seen': len(files_out), 'elapsed': s['elapsed_seconds'],
                     'timeout': s['status'] == 'timeout', 'lessons_cited': len(set(LESSON.findall(text))),
                     'cards_cited': len(set(CARD.findall(text))), 'think_chars': len(text)})
# Opus: issues cited in worklog + tool calls from transcripts
opus = {}
for e in idx:
    d = Path(e['opus_logs'])
    wl = (d / 'worklog.md').read_text(errors='ignore') if (d / 'worklog.md').exists() else ''
    calls = 0
    tp = d / 'transcripts' / 'solver.jsonl'
    if tp.exists():
        for raw in open(tp, errors='ignore'):
            try: r = json.loads(raw)
            except ValueError: continue
            m = r.get('message') or {}
            if m.get('role') == 'assistant' and isinstance(m.get('content'), list):
                calls += sum(1 for b in m['content'] if b.get('type') == 'tool_use')
    opus[e['ordinal']] = {'issues_cited': len(set(FILE.findall(wl))), 'tool_calls': calls,
                          'vintage_words': len(re.findall(r'vintage|revis|later issue|latest', wl, re.I)),
                          'check_words': len(re.findall(r'cross-check|check:|verify|verified|sum(s|med)? to|matches', wl, re.I))}
json.dump({'glm': rows, 'opus': opus}, open(OUT / 'behavior.json', 'w'))

def med(xs): return st.median(xs) if xs else None
print('arm     | cmds med | fail% | exit127 runs | heredoc-fail runs | issues opened med (right/wrong) | elapsed med s (right/wrong) | timeouts')
for arm in ROOTS:
    R = [r for r in rows if r['arm'] == arm]
    right = [r for r in R if r['correct']]; wrong = [r for r in R if not r['correct']]
    print(f"{arm:7} | {med([r['cmds'] for r in R])} | {100*sum(r['fails'] for r in R)/max(1,sum(r['cmds'] for r in R)):.0f}% | "
          f"{sum(1 for r in R if r['e127'])} | {sum(1 for r in R if r['heredoc_fail'])} | "
          f"{med([r['issues_opened'] for r in right])}/{med([r['issues_opened'] for r in wrong])} | "
          f"{med([r['elapsed'] for r in right]):.0f}/{med([r['elapsed'] for r in wrong]):.0f} | {sum(r['timeout'] for r in R)}")
print('opus    | tool calls med', med([v['tool_calls'] for v in opus.values()]), '| issues cited med', med([v['issues_cited'] for v in opus.values()]),
      '| questions w/ vintage talk', sum(1 for v in opus.values() if v['vintage_words']), '| w/ explicit checks', sum(1 for v in opus.values() if v['check_words']))
# accuracy by run length quartile (none arm)
for arm in ROOTS:
    R = sorted([r for r in rows if r['arm'] == arm], key=lambda r: r['cmds'])
    q = [R[i*len(R)//4:(i+1)*len(R)//4] for i in range(4)]
    print(arm, 'accuracy by command-count quartile:', [f"{sum(r['correct'] for r in g)}/{len(g)} (≤{g[-1]['cmds']} cmds)" for g in q])
P = [r for r in rows if r['arm'] == 'prompt']
print('prompt arm: runs citing ≥1 lesson', sum(1 for r in P if r['lessons_cited']), '| acc when citing', sum(r['correct'] for r in P if r['lessons_cited']), '/', sum(1 for r in P if r['lessons_cited']),
      '| acc when not', sum(r['correct'] for r in P if not r['lessons_cited']), '/', sum(1 for r in P if not r['lessons_cited']))
C = [r for r in rows if r['arm'] == 'cards']
print('cards arm: runs citing ≥1 card', sum(1 for r in C if r['cards_cited']), '| acc when citing', sum(r['correct'] for r in C if r['cards_cited']), '/', sum(1 for r in C if r['cards_cited']))
