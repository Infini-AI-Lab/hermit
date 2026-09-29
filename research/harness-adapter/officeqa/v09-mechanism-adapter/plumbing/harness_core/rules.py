"""Rule and policy files: core seed rules, validation, (de)serialisation (DESIGN §2 L4, §3).

A mechanism adapter stores rules/<id>.json (contracts.Rule) and policy.json (POLICY_DEFAULTS keys).
`validate_rule` is the schema gate for learned rules: fixed vocabulary (known predicate/offer names),
≤ 200-byte templates whose fields are dotted names only, keyword triggers without regex syntax,
JSON-only arguments. The core seed rules below are the benchmark-neutral half of the seed set.
"""
from __future__ import annotations

from dataclasses import asdict, fields
import json
import math
import os
from pathlib import Path
import re
from typing import Any, Iterable, Mapping

from . import contracts as C
from .engine import FIELD_RE, MAX_FINDING

RULE_ID = re.compile(r'^[a-z][a-z0-9_]{1,47}$')
TRIGGER = re.compile(r'^(\*|[a-z0-9][a-z0-9 \-/.&]{0,59})$')
MAX_WHEN = 4
MAX_TRIGGERS = 12


def core_seed_rules() -> list[C.Rule]:
    """answer_count, scale, competing_candidates, grounded, recompute_final (priority = display order;
    ordered by expected precision, replay.md §4-§5: count is near-certain, provenance advisory)."""
    return [
        C.Rule(id='answer_count', when=[{'pred': 'count_mismatch', 'args': {}}], priority=10,
               finding='The question asks for {count.expected} value(s); your answer has {count.got}.'),
        C.Rule(id='scale', when=[{'pred': 'scaled_100', 'args': {}}], priority=20,
               offer={'op': 'unscale', 'args': {}},
               finding='Your answer {value.text} is {relation} the output {compute.value} of calc {compute.sha}; {why}.'),
        C.Rule(id='competing_candidates', when=[{'pred': 'competing_candidates', 'args': {}}], priority=40,
               offer={'op': 'use_alt', 'args': {}},
               finding='You also proposed {alt.answer} ({alt.source}); submit the one the question asks for.'),
        C.Rule(id='grounded', when=[{'pred': 'ungrounded_inputs', 'args': {}}], priority=50,
               finding='calc {compute.sha} uses {input.literal}, a value no tool output showed. '
                       'Print the line it comes from, or keep.'),
        C.Rule(id='recompute_final', when=[{'pred': 'not_from_compute', 'args': {}}], priority=60,
               finding='Your answer {value.text} is not an output of any calc run. Run the final arithmetic '
                       'with calc and submit its result, or keep.'),
    ]


def _json_ok(value: Any, depth: int = 0) -> bool:
    if depth > 4:
        return False
    if value is None or isinstance(value, (bool, str)):
        return not isinstance(value, str) or len(value) <= 200
    if isinstance(value, (int, float)):
        return math.isfinite(value)
    if isinstance(value, (list, tuple)):
        return len(value) <= 32 and all(_json_ok(v, depth + 1) for v in value)
    if isinstance(value, dict):
        return len(value) <= 32 and all(isinstance(k, str) and _json_ok(v, depth + 1) for k, v in value.items())
    return False


def validate_rule(rule: C.Rule, *, predicates: Iterable[str], offers: Iterable[str]) -> list[str]:
    """Schema/vocabulary errors (empty list = valid)."""
    preds, offs = set(predicates), set(offers)
    errors = []
    if not isinstance(rule.id, str) or not RULE_ID.match(rule.id):
        errors.append('id must match ^[a-z][a-z0-9_]{1,47}$')
    if not isinstance(rule.when, list) or not 1 <= len(rule.when) <= MAX_WHEN:
        errors.append(f'when must list 1-{MAX_WHEN} predicates')
    else:
        for clause in rule.when:
            if not isinstance(clause, dict) or set(clause) - {'pred', 'args'}:
                errors.append('when clauses are {"pred": name, "args": {...}}')
            elif clause.get('pred') not in preds:
                errors.append(f"unknown predicate {clause.get('pred')!r}")
            elif not isinstance(clause.get('args', {}), dict) or not _json_ok(clause.get('args', {})):
                errors.append(f"args of {clause.get('pred')} must be a small JSON object")
    if not isinstance(rule.finding, str) or not rule.finding.strip():
        errors.append('finding template required')
    elif len(rule.finding.encode('utf-8')) > MAX_FINDING:
        errors.append(f'finding template exceeds {MAX_FINDING} bytes')
    else:
        stray = FIELD_RE.sub('', rule.finding)
        if '{' in stray or '}' in stray:
            errors.append('finding may contain only {dotted.name} fields')
    if rule.offer is not None:
        if not isinstance(rule.offer, dict) or set(rule.offer) - {'op', 'args'}:
            errors.append('offer is {"op": name, "args": {...}}')
        elif rule.offer.get('op') not in offs:
            errors.append(f"unknown offer {rule.offer.get('op')!r}")
        elif not isinstance(rule.offer.get('args', {}), dict) or not _json_ok(rule.offer.get('args', {})):
            errors.append('offer args must be a small JSON object')
    if not isinstance(rule.priority, int) or isinstance(rule.priority, bool) or not 0 <= rule.priority <= 100:
        errors.append('priority must be an integer 0-100')
    if not isinstance(rule.triggers, list) or len(rule.triggers) > MAX_TRIGGERS or \
            not all(isinstance(t, str) and TRIGGER.match(t) for t in rule.triggers):
        errors.append(f'triggers: ≤ {MAX_TRIGGERS} lowercase keywords/phrases (no regex), or "*"')
    if rule.scope not in ('core', 'pack') or rule.origin not in ('seed', 'learned'):
        errors.append('scope must be core|pack and origin seed|learned')
    if not isinstance(rule.enabled, bool) or not isinstance(rule.admitted_block, int):
        errors.append('enabled must be bool and admitted_block int')
    if not isinstance(rule.stats, dict) or not _json_ok(rule.stats):
        errors.append('stats must be a small JSON object')
    return errors


def rule_from_dict(data: Mapping[str, Any]) -> C.Rule:
    names = {f.name for f in fields(C.Rule)}
    unknown = set(data) - names
    if unknown:
        raise ValueError(f'unknown rule fields: {sorted(unknown)}')
    return C.Rule(**{k: data[k] for k in data})


def rule_to_dict(rule: C.Rule) -> dict:
    return asdict(rule)


def load_rules(directory: str | os.PathLike) -> list[C.Rule]:
    """rules/<id>.json files, sorted by id; the file name must equal the rule id."""
    out = []
    for path in sorted(Path(directory).glob('*.json')):
        if path.is_symlink() or not path.is_file():
            raise ValueError(f'rule file must be a regular file: {path.name}')
        rule = rule_from_dict(json.loads(path.read_text(encoding='utf-8')))
        if path.stem != rule.id:
            raise ValueError(f'rule file {path.name} holds rule {rule.id}')
        out.append(rule)
    return out


def save_rule(directory: str | os.PathLike, rule: C.Rule) -> Path:
    """Atomic write (temp file + rename): a symlink planted at rules/<id>.json is replaced, never
    written through."""
    if not isinstance(rule.id, str) or not RULE_ID.match(rule.id):
        raise ValueError('rule id must match ^[a-z][a-z0-9_]{1,47}$')
    path = Path(directory) / f'{rule.id}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'.{rule.id}.json.{os.getpid()}.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, 'w', encoding='utf-8') as fh:
        fh.write(json.dumps(rule_to_dict(rule), ensure_ascii=False, indent=2, sort_keys=True) + '\n')
    os.replace(tmp, path)
    return path


def validate_policy(policy: Mapping[str, Any]) -> list[str]:
    errors = []
    for key, value in policy.items():
        if key not in C.POLICY_DEFAULTS:
            errors.append(f'unknown policy key {key}')
        elif key == 'disabled_rules':
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                errors.append('disabled_rules must be a list of rule ids')
        elif key in C.POLICY_RANGES:
            lo, hi = C.POLICY_RANGES[key]
            if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
                errors.append(f'{key} must be an integer in [{lo}, {hi}]')
    return errors


def effective_policy(policy: Mapping[str, Any] | None) -> dict[str, Any]:
    merged = {**C.POLICY_DEFAULTS, **dict(policy or {})}
    errors = validate_policy(merged)
    if errors:
        raise ValueError('; '.join(errors))
    return merged


def rendered_text_bytes(rules: Iterable[C.Rule]) -> int:
    """Model-visible bytes a rule set contributes to the TEXT_BUDGET (finding templates)."""
    return sum(len(r.finding.encode('utf-8')) for r in rules if r.enabled)
