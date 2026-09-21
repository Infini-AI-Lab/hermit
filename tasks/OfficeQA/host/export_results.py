"""Allowlist-only publication export. Never emit questions, answers or raw feedback."""
from __future__ import annotations
from collections import Counter
import hashlib
import json
from pathlib import Path
import re


def read(path):
    return json.loads(path.read_text())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def summarize(root, framework_revision='unrecorded'):
    root = Path(root)
    state = read(root / 'state.json')
    metrics = state.get('metrics', {})
    scores = {r['task_id']: r for r in metrics.get('results', [])}
    pairs = {}
    arms = {}
    for arm in ('baseline', 'adapted'):
        records = state['arms'][arm]['results']
        scored = metrics.get('arms', {}).get(arm, {})
        arms[arm] = {k: scored.get(k) for k in ('correct', 'strict_accuracy', 'one_percent_accuracy')}
        arms[arm].update(completed=len(records), valid_final_answers=sum(bool(r['answer_format_valid']) for r in records),
                         model_requests=sum(r['requests'] for r in records),
                         statuses=dict(Counter(r['status'] for r in records)))
        for row in records:
            uid = row['task_id']
            if not re.fullmatch(r'UID\d+', uid):
                raise ValueError('Unexpected public task ID; refuse to export free text')
            item = pairs.setdefault(row['ordinal'], {'ordinal': row['ordinal'], 'task_id': uid})
            if item['task_id'] != uid or arm in item:
                raise ValueError('Unpaired or duplicate task identity')
            item[arm] = {key: row[key] for key in ('status', 'requests', 'elapsed_seconds', 'answer_format_valid')}
            item[arm]['strict_score'] = scores.get(uid, {}).get(arm)
    rounds = []
    for row in state.get('learning', []):
        status = row.get('learner_status', 'unknown')
        context_error = False
        if status == 'api_error':
            records = (root / 'evolution' / f'{row["block"]:02}').glob('sessions/*/control/fireworks-calls/*.json')
            for record in records:
                # Inspect provider error metadata only; never export the request or response body.
                attempts = read(record).get('meta', {}).get('attempts', [])
                context_error |= any('prompt is too long' in str(a.get('provider_error', '')).lower() for a in attempts)
        reason = ('completed' if row['activated'] else 'input_context_limit' if context_error
                  else 'output_budget_exhausted' if status in ('budget_exhausted', 'response_truncated')
                  and row.get('usage', {}).get('completion_tokens') == 65536 else status)
        rounds.append({key: row.get(key) for key in ('block', 'activated', 'status', 'learner_status', 'model_calls', 'elapsed_seconds')} | {
            'completion_tokens': row.get('usage', {}).get('completion_tokens'),
            'reason': reason, 'used_by_scored_future_questions': bool(row['activated'] and row['block'] < 10)})
    lineage, active = [], 0
    for block in range(1, 11):
        lineage.append({'questions': [(block-1)*5+1, block*5], 'adapter_round': active})
        learned = next((r for r in rounds if r['block'] == block), None)
        if learned and learned['activated']:
            active = block
    result = {'schema_version': 1, 'benchmark': 'OfficeQA Pro', 'status': state['status'],
              'started_utc': state['started_utc'], 'finished_utc': state.get('finished_utc'),
              'model': state['model'], 'framework_revision': framework_revision,
              'protocol': metrics.get('protocol', 'officeqa-pro-codex50-gold-online-v4'),
              'denominator_per_arm': 50, 'learn_every': 5,
              'budgets': {'seconds': 1800, 'completion_tokens': 65536, 'request_limit': None},
              'arms': arms, 'primary_tolerance': 0, 'secondary_tolerance': 0.01,
              'paired_strict': metrics.get('paired_strict'), 'learning': rounds, 'adapter_lineage': lineage,
              'limitations': [
                  'Historical scores are not measurements of the updated task or upstream framework.',
                  'One sequential, answer-supervised online experiment; no established improvement.',
                  'Offline 50-question parsed-TXT subset, not the official PDF-and-web leaderboard condition.',
                  'Adapted arm also receives extra learning compute and completed-batch answers.',
                  'Published tables omit questions, predictions, reference answers, raw traces and learned memory.']}
    audit_path = root / 'preflight/final-audit.json'
    if audit_path.is_file():
        audit = read(audit_path)
        result['recorded_audit'] = {k: audit[k] for k in ('passed', 'counts', 'created_utc')}
        result['recorded_audit']['sha256'] = sha(audit_path)
        result['recorded_audit']['scope'] = 'Historical saved-artifact consistency check, not a new audit or independent network attestation'
    cost_path = root / 'cost-estimate.json'
    if cost_path.is_file():
        cost = read(cost_path)
        result['recorded_cost'] = {'price_checked_date': cost['price_checked_date'],
            'price_usd_per_million_tokens': cost['price_usd_per_million_tokens'],
            **{name: {k: cost[name][k] for k in ('known_cost_lower_bound_usd', 'usage_complete', 'unknown_usage_attempts')}
               for name in ('formal_total', 'including_synthetic_preflight')}}
    return result, [pairs[n] for n in sorted(pairs)]


def export(root, output, framework_revision):
    root, output = Path(root), Path(output)
    if not re.fullmatch(r'[0-9a-f]{40}', framework_revision):
        raise ValueError('Use the full actual framework commit SHA')
    summary, pairs = summarize(root, framework_revision)
    if summary['status'] != 'completed' or len(pairs) != 50 or any(
        not all(a in p and p[a]['strict_score'] in (0, 1) for a in ('baseline', 'adapted')) for p in pairs):
        raise ValueError('Publication requires all 50 paired, graded original attempts')
    if len(summary['learning']) != 10:
        raise ValueError('Expected ten recorded learning rounds')
    for arm in ('baseline', 'adapted'):
        if sum(p[arm]['strict_score'] for p in pairs) != summary['arms'][arm]['correct']:
            raise ValueError('Per-task and aggregate scores disagree')
    frozen = read(root / 'freeze.json')
    if frozen.get('framework_revision', framework_revision) != framework_revision:
        raise ValueError('Framework revision conflicts with the frozen campaign')
    artifacts = ('state.json', 'freeze.json', 'private-evaluation/paired-metrics.json',
                 'baseline-predictions.jsonl', 'adapted-predictions.jsonl', 'PROTOCOL.md')
    provenance = {'framework_revision': framework_revision,
                  'original_artifact_sha256': {name: sha(root / name) for name in artifacts},
                  'original_execution_file_sha256': frozen['files'],
                  'note': 'Hashes identify private local originals; raw evidence is not redistributed.'}
    runtime_path = root / 'preflight/runtime-provenance.json'
    if runtime_path.is_file():
        runtime = read(runtime_path)
        if runtime.get('native_adapter_commit') != framework_revision:
            raise ValueError('Framework revision conflicts with the original runtime receipt')
        provenance['runtime'] = {k: runtime[k] for k in ('codex_version', 'native_adapter_commit', 'packages') if k in runtime}
    output.mkdir(parents=True, exist_ok=False)
    for name, value in [('summary.json', summary), ('per-task.json', pairs)]:
        (output / name).write_text(json.dumps(value, indent=2) + '\n')
    (output / 'provenance.json').write_text(json.dumps(provenance, indent=2) + '\n')
    from report import render
    (output / 'report.html').write_text(render(summary, pairs))
    print(json.dumps({'exported': str(output), 'questions': len(pairs), 'raw_data_exported': False}))
