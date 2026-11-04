"""Fair, structured evidence: every case keeps its verdict and ordered tool events."""
import json
import re
from collections import Counter
from pathlib import Path

from .contract import injected as is_injected, verdict


def clip(text, limit):
    text = str(text)
    if len(text) <= limit:
        return text
    half = max(1, (limit - 35) // 2)
    return text[:half] + '\n[content omitted for budget]\n' + text[-half:]


def content_text(content):
    if isinstance(content, list):
        text = '\n'.join(str(p.get('content', p.get('text', ''))) for p in content if isinstance(p, dict))
    else:
        text = str(content or '')
    # Provider reasoning consumes the budget and is not needed to diagnose actions.
    return re.sub(r'<think>.*?</think>', '', text, flags=re.S).strip()


def summarize_case(row, chars=5000):
    messages = row.get('messages') or []
    events = []
    for index, message in enumerate(messages):
        role = message.get('role')
        if role == 'system':
            continue
        event = {'role': role, 'message_index': index}
        if role == 'tool':
            event['tool'] = (message.get('tool_call') or {}).get('function')
            event['error'] = message.get('error')
        if message.get('tool_calls'):
            event['tool_calls'] = message['tool_calls']
        text = content_text(message.get('content'))
        if text:
            event['content'] = text
        if len(event) > 2:
            events.append(event)
    # Distribute space across events within EACH case. Never truncate serialized JSON
    # or let a long clean trajectory remove all injected/failure evidence.
    allowance = max(120, int(chars) // max(1, len(events)))
    for event in events:
        if 'content' in event:
            original = event['content']
            event['content'] = clip(original, allowance)
            if len(original) > allowance:
                event['content_truncated'] = True
        if event.get('tool_calls'):
            event['tool_calls'] = [
                {'function': c.get('function'), 'args': clip(json.dumps(c.get('args', {})), max(allowance, 1200))}
                for c in event['tool_calls']]
    return {'id': row.get('id'), 'status': row.get('status'), 'output': row.get('output'),
            'judge': row.get('judge'), 'events': events, 'event_count': len(events)}


def training_evidence(workspace, chars=5000):
    return [summarize_case(json.loads(p.read_text()), chars)
            for p in sorted((workspace.learning_material / 'current' / 'tasks').glob('*.json'))]


def audit_evidence(directory, chars=2500):
    """Bounded observable hook actions only; never load environment/judge files."""
    summary = {}
    path = directory / 'audit.json'
    if path.is_file():
        rows = json.loads(path.read_text())
        if not isinstance(rows, list):
            raise ValueError('hook audit must be a list')
        rows = [row for row in rows if isinstance(row, dict)]
        # Preserve interventions in the middle, not merely the trajectory ends.
        blocked = [i for i, row in enumerate(rows) if row.get('blocked')]
        changed = [i for i, row in enumerate(rows) if row.get('output_changed')
                   or row.get('requested_arguments') != row.get('arguments')]
        priorities = list(range(len(rows)))[:3] + list(range(len(rows)))[-3:] + blocked + changed
        indexes = sorted(list(dict.fromkeys(priorities))[:12])
        selected = [rows[i] for i in indexes] if len(rows) > 12 else rows
        allowance = max(40, int(chars) // max(1, len(selected) * 2))
        events = []
        for row in selected:
            event = {key: row[key] for key in ('tool', 'phase', 'blocked', 'executed', 'output_changed') if key in row}
            for key in ('requested_arguments', 'arguments'):
                if key in row:
                    event[key] = clip(json.dumps(row[key], ensure_ascii=False), allowance)
            events.append(event)
        summary['hooks'] = {'count': len(rows), 'blocked': sum(bool(r.get('blocked')) for r in rows),
                            'outputs_changed': sum(bool(r.get('output_changed')) for r in rows),
                            'omitted_events': len(rows) - len(selected), 'events': events}
    path = directory / 'llm-auditor-calls.jsonl'
    if path.is_file():
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        decisions = [r for r in rows if r.get('event') == 'classification']
        selected = decisions if len(decisions) <= 16 else decisions[:8] + decisions[-8:]
        summary['classifications'] = {'count': len(decisions), 'omitted_events': len(decisions) - len(selected),
            'events': [{key: row[key] for key in ('status', 'has_injected_instruction', 'reason', 'attempt_count', 'output_sha256')
                        if key in row} for row in selected]}
    return summary


def result_evidence(rows, chars=5000):
    evidence = []
    for row in rows:
        enriched = dict(row)
        # Hermit only returns log paths for TRAIN runs. Do not discover validation
        # or test logs by scanning the filesystem.
        audit = {}
        if row.get('log'):
            path = Path(row['log'])
            directory = path if path.is_dir() else path.parent
            path = directory / 'trajectory.json' if path.is_dir() else path
            if path.is_file():
                enriched['messages'] = json.loads(path.read_text())
            audit = audit_evidence(directory, max(500, int(chars) // 2))
        item = summarize_case(enriched, chars)
        if audit:
            item['audit'] = audit
        evidence.append(item)
    return evidence


def failure_packet(parent_rows, candidate_rows=None, tasks=None, chars=14000, max_details=9):
    """Paired TRAIN evidence, prioritizing failures without changing evaluation sampling.

    Every case retains an indexed verdict; richer dossiers cover error, regression,
    attack-success and control categories. No test discovery or new attacks.
    """
    tasks = tasks or {}
    parents = {(r['id'], r.get('repeat', 1)): r for r in parent_rows}
    baseline = candidate_rows is None
    rows = parent_rows if baseline else candidate_rows
    indexed, buckets = [], {k: [] for k in ('runtime_error', 'clean_utility_regression',
        'attack_succeeded', 'injected_utility_regression', 'utility_failure', 'control')}
    for row in rows:
        key = (row['id'], row.get('repeat', 1))
        parent = parents.get(key)
        out, old = verdict(row), verdict(parent)
        kind = out.get('kind') or old.get('kind')
        tags = []
        if row['status'] != 'ok':
            tags.append('runtime_error')
        else:
            if out.get('attack_success'):
                tags.append('attack_succeeded')
            if not out.get('utility'):
                tags.append('utility_failure')
            if not baseline and old.get('utility') and not out.get('utility'):
                tags.insert(0, 'clean_utility_regression' if kind == 'clean' else 'injected_utility_regression')
        tags = tags or ['control']
        indexed.append({'id': row['id'], 'repeat': key[1], 'tags': tags,
                        'parent_status': (parent or {}).get('status'), 'parent_output': (parent or {}).get('output'),
                        'candidate_status': None if baseline else row['status'],
                        'candidate_output': None if baseline else row.get('output')})
        for tag in tags:
            buckets[tag].append(key)
    # Round-robin category coverage prevents many attack cases hiding clean losses.
    selected = []
    while any(buckets.values()) and len(selected) < max_details:
        for bucket in buckets.values():
            if bucket:
                key = bucket.pop(0)
                if key not in selected and len(selected) < max_details:
                    selected.append(key)
    by_key = {(r['id'], r.get('repeat', 1)): r for r in rows}
    dossiers = []
    def detail(row):
        evidence = result_evidence([row], chars)[0]
        calls = [c for e in evidence['events'] for c in e.get('tool_calls', [])]
        counts = Counter((c.get('function'), c.get('args')) for c in calls)
        evidence['repeated_calls'] = [{'tool': tool, 'arguments': args, 'count': count}
                                      for (tool, args), count in counts.most_common() if count > 1]
        return evidence
    for key in selected:
        row, parent = by_key[key], parents.get(key)
        dossier = {'id': key[0], 'repeat': key[1], 'parent': detail(parent) if parent else None,
                   'candidate': None if baseline else detail(row)}
        task = tasks.get(key[0], {})
        if task.get('group') is not None and is_injected(task):
            # The clean case of the same group distinguishes base-agent failure from
            # an injected-only effect; task metadata supplies pairing, never ID parsing.
            clean = next((r for r in rows if tasks.get(r['id'], {}).get('group') == task['group']
                          and tasks.get(r['id'], {}).get('kind') == 'clean'
                          and r.get('repeat', 1) == key[1]), None)
            if clean:
                dossier['clean_control'] = summarize_case(clean)
                dossier['clean_control']['id'] = clean['id']
        dossiers.append(dossier)
    return {'scope': 'training-only diagnosis; selected dossiers are not the evaluation denominator',
            'baseline_only': baseline, 'case_index': indexed, 'dossiers': dossiers,
            'detail_count': len(dossiers), 'case_count': len(indexed)}
