"""Load the 399 v8 arm-runs (3 roots x 133) for offline checker replay.

Read-only. Correctness labels come only from officeqa-divergence-analysis/index.json.
Never touches prepared/private, answers.jsonl, reward.py or private-evaluation.
"""
from __future__ import annotations

import glob
import json
import os
import re
from dataclasses import dataclass, field

BASE = '/Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd'
INDEX = f'{BASE}/officeqa-divergence-analysis/index.json'
PERQ = f'{BASE}/officeqa-divergence-analysis/per-question'
ARMS = {'none': 'baseline', 'prompt': 'adapted', 'cards': 'adapted'}

# Codex emits this 'error' item right after each automatic context compaction.
# Verified: 72 runs carry it; in 71 the count equals the number of tool-less
# (compaction) requests under api/ (report.py compaction_marker_check).
COMPACTION_RE = re.compile(r'compaction', re.I)


@dataclass
class Step:
    kind: str            # 'cmd' | 'reason' | 'msg' | 'compaction'
    text: str = ''       # command text / reasoning text / message text
    output: str = ''     # aggregated_output (cmd only)
    exit_code: int | None = None
    status: str = ''
    cmd_index: int = 0   # number of commands issued up to and including this step


@dataclass
class Run:
    arm: str
    ordinal: int
    task_id: str
    question: str
    answer: str
    correct: int
    status: str
    error_bin: str | None
    run_dir: str
    steps: list[Step] = field(default_factory=list)

    @property
    def wrong(self) -> int:
        return 1 - self.correct


def run_dir(arm: str, ordinal: int) -> str:
    return f'{BASE}/officeqa-pro133-{arm}-v8-work/runs/{ARMS[arm]}/{ordinal:03}'


def load_events(path: str) -> list[Step]:
    """Command, reasoning, message and compaction steps in trajectory order.

    Commands are keyed on item.started (issue order); output/exit code are
    filled from the matching item.completed (same item id). A command that
    never completed (wall-clock kill) keeps empty output.
    """
    steps: list[Step] = []
    by_id: dict[str, Step] = {}
    n_cmd = 0
    with open(path) as fh:
        for line in fh:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            it = ev.get('item') or {}
            typ = it.get('type')
            if typ == 'command_execution':
                iid = it.get('id')
                if ev.get('type') == 'item.started' or iid not in by_id:
                    n_cmd += 1
                    st = Step('cmd', text=it.get('command') or '', cmd_index=n_cmd)
                    by_id[iid] = st
                    steps.append(st)
                if ev.get('type') == 'item.completed':
                    st = by_id[iid]
                    st.output = it.get('aggregated_output') or ''
                    st.exit_code = it.get('exit_code')
                    st.status = it.get('status') or ''
            elif ev.get('type') != 'item.completed':
                continue
            elif typ == 'reasoning':
                steps.append(Step('reason', text=it.get('text') or '', cmd_index=n_cmd))
            elif typ == 'agent_message':
                steps.append(Step('msg', text=it.get('text') or '', cmd_index=n_cmd))
            elif typ == 'error' and COMPACTION_RE.search(it.get('message') or ''):
                steps.append(Step('compaction', cmd_index=n_cmd))
    return steps


def load_runs(with_events: bool = True) -> list[Run]:
    index = json.load(open(INDEX))
    runs: list[Run] = []
    for e in index:
        for arm in ARMS:
            d = run_dir(arm, e['ordinal'])
            task = json.load(open(f'{d}/task.json'))
            summ = json.load(open(f'{d}/summary.json'))
            assert task['task_id'] == e['task_id'] == summ['task_id']
            r = Run(arm=arm, ordinal=e['ordinal'], task_id=e['task_id'], question=task['question'],
                    answer=summ.get('answer') or '', correct=int(e['correct'][arm]),
                    status=summ.get('status') or '', error_bin=e['arms'][arm].get('error_bin'),
                    run_dir=d)
            if with_events:
                r.steps = load_events(f'{d}/records/events.jsonl')
            runs.append(r)
    return runs


# ---------------------------------------------------------------------------
# Model-visible tool outputs (what GLM actually saw after Codex truncation).
# Each request.json carries the whole live history, so the union of the
# compaction requests (no tools offered) and the final request covers every
# tool output the model ever received.
_META_LINE = re.compile(r'^(Chunk ID:|Wall time:|Process exited with code|Original token count:|Output:$|'
                        r'Warning: truncated output|Total output lines:)')
_TRUNC_MARK = re.compile(r'…\d+ (?:chars|tokens) truncated…')


def visible_outputs(d: str) -> list[str]:
    reqs = sorted(glob.glob(f'{d}/api/[0-9]*/request.json'))
    if not reqs:
        return []
    chosen = []
    for i, path in enumerate(reqs):
        if i == len(reqs) - 1:
            chosen.append(path)
            continue
        # Cheap test for a compaction request ("tools": []) without parsing the
        # multi-MB body: 'tools' is serialised after 'input', so it is in the tail.
        with open(path, 'rb') as fh:
            fh.seek(max(0, os.path.getsize(path) - 200_000))
            tail = fh.read()
        k = tail.rfind(b'"tools":')
        if k < 0 or tail[k + 8:k + 12].lstrip().startswith(b'[]'):
            chosen.append(path)
    seen: dict[str, str] = {}
    for path in chosen:
        try:
            body = json.load(open(path))
        except (json.JSONDecodeError, OSError):
            continue
        for item in body.get('input') or []:
            if item.get('type') != 'function_call_output':
                continue
            out = item.get('output')
            if isinstance(out, list):
                out = '\n'.join(p.get('text', '') for p in out if isinstance(p, dict))
            if not isinstance(out, str):
                continue
            lines = [ln for ln in out.splitlines() if not _META_LINE.match(ln)]
            seen[item.get('call_id') or str(len(seen))] = _TRUNC_MARK.sub(' ', '\n'.join(lines))
    return list(seen.values())


def per_question_annotations() -> dict[tuple[str, int], dict]:
    """Analyst annotations for the 103 analysed failed arm-runs (descriptive only)."""
    out = {}
    for path in glob.glob(f'{PERQ}/*.json'):
        q = json.load(open(path))
        for arm, a in (q.get('arms') or {}).items():
            if a.get('correct') is False:
                out[(arm, q['ordinal'])] = {
                    'stage': (a.get('first_divergence') or {}).get('stage'),
                    'loop': (a.get('operational') or {}).get('loop_or_thrash'),
                    'doubt': (a.get('glm_expressed_doubt') or {}).get('value'),
                }
    return out
