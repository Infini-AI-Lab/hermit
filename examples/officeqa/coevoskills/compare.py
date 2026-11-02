"""Paired held-out comparison; completeness is explicit and errors stay in denominators."""
from __future__ import annotations
import json
from pathlib import Path


def compare(run: Path, intended: list[str]):
    expected = set(intended)
    if len(expected) != len(intended):
        raise ValueError('duplicate intended test IDs')
    rows = {}
    for label in ('none', 'common_seed', 'final'):
        values = {}
        source = run / 'eval' / label / 'results.jsonl'
        for line in source.read_text().splitlines() if source.is_file() else []:
            record = json.loads(line)
            tid = record['id']
            if tid not in expected or tid in values:
                raise ValueError('unexpected or duplicate evaluation ID')
            values[tid] = record
        rows[label] = values
    report = {'intended_tasks': len(intended), 'complete': all(set(v) == expected for v in rows.values()),
              'arms': {}, 'paired': {}}
    for label, values in rows.items():
        score = sum(float(r['judge']['score']) for r in values.values())
        report['arms'][label] = {'completed': len(values), 'errors': sum(r['status'] != 'ok' for r in values.values()),
            'accuracy': score / len(intended) if intended else None,
            'missing': sorted(expected - values.keys())}
    if report['complete']:
        for baseline in ('none', 'common_seed'):
            differences = [float(rows['final'][tid]['judge']['score']) -
                           float(rows[baseline][tid]['judge']['score']) for tid in intended]
            report['paired'][baseline] = {'wins': sum(d > 0 for d in differences),
                'losses': sum(d < 0 for d in differences), 'ties': sum(d == 0 for d in differences),
                'improvement_percentage_points': 100 * sum(differences) / len(differences)}
    return report
