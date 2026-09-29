"""v9 learner evidence: ONE builder for both learners (DESIGN §1 "equal learner inputs", §3; validity P4).

For learning block b of a campaign root this module collects every *released* episode of the campaign
(blocks 1..b) except the disputed-reference items (OfficeQA issue #47: withheld from both learners
entirely, still scored by the campaign), and gives each one:

- the task (question), the sealed submitted answer, the released reference, and the strict / 1 % grades
  computed here, controller-side, with an injected grader (production: the pinned VENDOR reward.py);
- the *model-view trajectory*: what the actor model actually saw, rebuilt from the broker records
  `api/NNN/request.json` (the upstream input as sent, broker inserts included) — one segment per
  compaction request plus the final request and its response, so everything the model saw before and
  after each context compaction appears once, with Codex's own truncation as the model saw it;
  broker-inserted items are labelled from `api/NNN/controller.json`;
- the controller log (`api/controller.jsonl` rows and the per-call `controller.json` verdicts);
- for controller arms, a ledger summary (`api/ledger.jsonl` + `api/checks/NNN.json`): candidates,
  calc runs with their linked inputs, gate checks with fired / shown findings and offers.

Truncation is identical for both learners (the cards_learning limits): commands 600 + 1400 characters,
tool outputs 1500 + 500, cut in the middle with an explicit marker. Private material never enters:
provider reasoning items are skipped, the Codex developer prompt is replaced by a one-line marker, and
every JSON value built here is checked for the private field names of `learning.validate_evidence`.

Expected root layout (v8 `campaign.assemble_evidence` format; see S5-learners-requests.md for S6):
    <root>/evidence/<BB>/manifest.json         block, episodes[{id, ordinal, directory[, run_dir]}], batch_seal,
                                              includes_gold true, includes_{grades,future_questions,
                                              baseline_traces,source_hints} false
    <root>/evidence/<BB>/<dir>/task.json, prediction.json, reference_answer.json ({task_id, official_answer}),
                              summary.json [, failure.json]
    <root>/private-learning-seals/<BB>.json    the controller's seal of the block (hashes of task, prediction,
                                              reference; sealed_utc <= reference_released_utc)
    run dir of an episode: manifest `run_dir` (inside the root) | parent of summary.json `records` |
                           <root>/runs/adapted/<NNN>
    <run>/api/NNN/{request,response,transport,controller}.json, <run>/api/controller.jsonl,
    <run>/api/ledger.jsonl, <run>/api/checks/NNN.json, <run>/workspace/.acting-prepared.json (extra.notes)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from functools import cached_property
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Callable, Iterable, Mapping

from harness_core import contracts as C
from harness_core.ledger import (RESUME_PREFIX, call_tool_name, is_compaction_request, load_events,
                                 message_text, output_text, raw_command_text)

# Identical for both learners (cards_learning.compact_trajectory limits).
CMD_HEAD, CMD_TAIL, CMD_LIMIT = 600, 1400, 2000
OUT_HEAD, OUT_TAIL, OUT_LIMIT = 1500, 500, 2000
MSG_HEAD, MSG_TAIL, MSG_LIMIT = 3000, 1000, 4000       # model messages, compaction summaries
USER_HEAD, USER_TAIL, USER_LIMIT = 1500, 500, 2000     # AGENTS.md / task / harness notes
PAGE = 20_000                                          # characters per page of any evidence part
PARTS = ('summary', 'trajectory', 'controller', 'ledger')
FORBIDDEN_KEYS = frozenset({'reasoning_content', 'api_key', 'authorization', 'gold_answer', 'ground_truth',
                            'correct_answer', 'grader_result', 'official_answer'})
SEAL_DIR = 'private-learning-seals'
_CALL_DIR = re.compile(r'^\d{3,}$')
_HARNESS = re.compile(r'^\[harness:(\w+)\] ')


# ---------------------------------------------------------------------------------------------
# Grading (controller-side only; the learner never imports a grader)
# ---------------------------------------------------------------------------------------------

def default_normalize(text: str) -> str:
    return ' '.join(str(text or '').lower().replace(',', '').split())


@dataclass
class Grader:
    """`score(reference, answer, tolerance=...)` -> truthy/1.0 when right (reward.py `score_answer` shape).
    Production: `vendor_grader()`. Tests inject the toy grader."""
    score: Callable[..., Any]
    normalize: Callable[[str], str] = default_normalize
    name: str = 'injected'

    def _grade(self, answer: str, reference: str, tolerance: float) -> int:
        value = self.score(reference, answer, tolerance=tolerance)
        if isinstance(value, bool):
            return int(value)
        return int(float(value) >= 1.0)

    def strict(self, answer: str, reference: str) -> int:
        return self._grade(answer or '', reference or '', 0.0)

    def one_percent(self, answer: str, reference: str) -> int:
        return self._grade(answer or '', reference or '', 0.01)

    def grade_fn(self, answer: str, reference: str) -> bool:
        """harness_core.replay.counterfactual calling convention: grade_fn(answer, reference)."""
        return bool(self.strict(answer, reference))


def vendor_grader() -> Grader:
    """The pinned official grader (paths.VENDOR/reward.py, sha-checked by officeqa_prepare.load_grader).
    Imported lazily: only the controller calls it, at run time."""
    import postmortem
    from paths import VENDOR
    score, normalize = postmortem.grader(VENDOR / 'reward.py')
    return Grader(score=score, normalize=normalize, name='vendor reward.py (pinned sha)')


# ---------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------

def cut(text: Any, head: int, tail: int, limit: int) -> str:
    """cards_learning._cut: keep head + tail characters around an explicit omission marker."""
    text = str(text or '')
    if len(text) <= limit:
        return text
    return text[:head] + f' ... [{len(text) - head - tail} chars omitted] ... ' + text[-tail:]


def one_line(text: Any, n: int) -> str:
    text = ' '.join(str('' if text is None else text).split())
    return text if len(text) <= n else text[:n - 1] + '…'


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_sha(value: Any) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str)
    return sha256_bytes(data.encode('utf-8', 'surrogatepass'))


def no_private_fields(value: Any, where: str = 'evidence') -> None:
    """learning._no_private_fields: refuse private field names anywhere in a JSON value."""
    if isinstance(value, dict):
        bad = [k for k in value if str(k).lower() in FORBIDDEN_KEYS]
        if bad:
            raise ValueError(f'private field {bad[0]!r} must not enter learner evidence ({where})')
        for item in value.values():
            no_private_fields(item, where)
    elif isinstance(value, (list, tuple)):
        for item in value:
            no_private_fields(item, where)


def read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _load(path: Path) -> Any:
    try:
        return read_json(path)
    except (OSError, ValueError):
        return None


def _regular_file(path: Path, root: Path) -> Path:
    """A regular, non-symlinked file inside `root` (evidence dirs are controller-written; refuse links)."""
    path = Path(path)
    if not path.is_relative_to(root) or path.is_symlink() or not path.is_file():
        raise ValueError(f'evidence file missing, linked or outside the root: {path}')
    mode = path.lstat().st_mode
    if not stat.S_ISREG(mode):
        raise ValueError(f'evidence file is not a regular file: {path}')
    return path


def token_count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


# ---------------------------------------------------------------------------------------------
# Block validation (learning.validate_evidence / validate_batch_seal, parametrised by root)
# ---------------------------------------------------------------------------------------------

def released_blocks(root: Path, block: int) -> list[tuple[int, Path]]:
    """(block number, evidence dir) of every released block 1..block; every one must exist."""
    out = []
    for b in range(1, block + 1):
        d = root / 'evidence' / f'{b:02}'
        if not (d / 'manifest.json').is_file():
            raise ValueError(f'released block {b} has no evidence manifest ({d})')
        out.append((b, d))
    return out


def validate_block(root: Path, directory: Path, block: int) -> dict:
    """The controller's evidence contract for one released block; returns the seal receipt."""
    manifest = read_json(_regular_file(directory / 'manifest.json', root))
    no_private_fields(manifest, f'block {block} manifest')
    episodes = manifest.get('episodes')
    withheld = manifest.get('withheld') or []
    if manifest.get('block') != block or not isinstance(episodes, list) or (not episodes and not withheld):
        raise ValueError(f'evidence manifest must declare block {block} and its episodes')
    ids = [str(e.get('id')) for e in episodes if isinstance(e, dict)]
    if len(ids) != len(episodes) or len(set(ids)) != len(ids) or not all(ids) or \
            len({e.get('directory') for e in episodes}) != len(episodes):
        raise ValueError(f'block {block}: episodes need distinct ids and directories')
    if manifest.get('includes_gold') is not True:
        raise ValueError(f'block {block}: the protocol releases the completed batch references')
    for name in ('includes_grades', 'includes_future_questions', 'includes_baseline_traces', 'includes_source_hints'):
        if manifest.get(name) is not False:
            raise ValueError(f'block {block}: evidence must exclude grades, future questions, baseline traces, hints')
    entry = manifest.get('batch_seal')
    if not isinstance(entry, dict) or entry.get('file') != f'{block:02}.json':
        raise ValueError(f'block {block}: evidence must name its controller seal')
    seal_path = _regular_file(root / SEAL_DIR / entry['file'], root)
    if sha256_bytes(seal_path.read_bytes()) != entry.get('sha256'):
        raise ValueError(f'block {block}: controller seal hash changed')
    seal = read_json(seal_path)
    if (seal.get('block') != block or seal.get('predictions_sealed') is not True
            or seal.get('reference_scope') != 'completed_batch_only'):
        raise ValueError(f'block {block}: the seal must cover this completed batch before reference release')
    rows = seal.get('episodes')
    if not isinstance(rows, list) or len(rows) != len(episodes):
        raise ValueError(f'block {block}: seal and manifest list different episodes')
    for episode, row in zip(sorted(episodes, key=lambda e: e['ordinal']), sorted(rows, key=lambda r: r['ordinal'])):
        if row.get('id') != episode['id'] or row.get('ordinal') != episode['ordinal']:
            raise ValueError(f'block {block}: seal ids/ordinals differ from the evidence')
        base = directory / str(episode['directory'])
        for filename, key in (('task.json', 'task_sha256'), ('prediction.json', 'prediction_sha256'),
                              ('reference_answer.json', 'reference_answer_sha256')):
            path = _regular_file(base / filename, root)
            if sha256_bytes(path.read_bytes()) != row.get(key):
                raise ValueError(f'block {block}: sealed {filename} of {episode["id"]} changed')
            if read_json(path).get('task_id') != episode['id']:
                raise ValueError(f'block {block}: {filename} task_id differs from its episode')
        reference = read_json(base / 'reference_answer.json')
        if set(reference) != {'task_id', 'official_answer'} or not isinstance(reference['official_answer'], str):
            raise ValueError(f'block {block}: reference file must hold exactly task_id and official_answer')
        for filename in ('task.json', 'prediction.json', 'summary.json', 'failure.json'):
            if (base / filename).is_file():
                no_private_fields(read_json(_regular_file(base / filename, root)), f'{episode["id"]}/{filename}')
    try:
        sealed = datetime.fromisoformat(seal['sealed_utc'])
        released = datetime.fromisoformat(seal['reference_released_utc'])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f'block {block}: seal needs timezone-aware sealed/released timestamps') from exc
    if sealed.tzinfo is None or released.tzinfo is None or sealed > released:
        raise ValueError(f'block {block}: references cannot precede the seal')
    return {'block': block, 'seal': str(seal_path), 'seal_sha256': entry['sha256'], 'episodes': len(episodes)}


# ---------------------------------------------------------------------------------------------
# Model-view trajectory from the broker records
# ---------------------------------------------------------------------------------------------

def api_calls(api_dir: Path) -> list[int]:
    if not api_dir.is_dir():
        return []
    return sorted(int(p.name) for p in api_dir.iterdir() if p.is_dir() and _CALL_DIR.match(p.name))


def _call_json(api_dir: Path, n: int, name: str) -> Any:
    return _load(api_dir / f'{n:03}' / name)


def _item_text(item: dict) -> str:
    return message_text(item) if item.get('type', 'message') == 'message' else ''


def _content_key(item: dict) -> str:
    """Identity of an item across segments, ignoring the fresh ids Codex gives re-sent items after a resume."""
    kind = item.get('type', 'message')
    if kind == 'message':
        return _key_of(('message', item.get('role'), _item_text(item)))
    if kind in ('function_call', 'custom_tool_call', 'local_shell_call'):
        return _key_of((kind, item.get('call_id') or item.get('id'), raw_command_text(item)))
    if kind in ('function_call_output', 'custom_tool_call_output', 'local_shell_call_output'):
        return _key_of((kind, item.get('call_id'), output_text(item)))
    return _key_of({k: v for k, v in item.items() if k != 'id'})


def _key_of(value: Any) -> str:
    return canonical_sha(value)


def _render_item(item: dict, step: int, note_reason: str | None) -> str | None:
    kind = item.get('type', 'message')
    if kind == 'reasoning':
        return None                                               # private: never shown to learners
    if kind == 'message':
        role, text = item.get('role'), _item_text(item)
        if role in ('developer', 'system'):
            return f'[#{step}] {role.upper()} INSTRUCTIONS ({len(text)} chars; the Codex harness prompt, omitted)'
        if role == 'assistant':
            return f'[#{step}] MODEL MESSAGE: {cut(text, MSG_HEAD, MSG_TAIL, MSG_LIMIT)}'
        match = _HARNESS.match(text)
        reason = note_reason or (match.group(1) if match else None)
        if reason:
            return f'[#{step}] HARNESS NOTE ({reason}): {cut(text, USER_HEAD, USER_TAIL, USER_LIMIT)}'
        if text.startswith(RESUME_PREFIX):
            return f'[#{step}] SUMMARY (the model\'s own hand-off after compaction): {cut(text, MSG_HEAD, MSG_TAIL, MSG_LIMIT)}'
        return f'[#{step}] USER: {cut(text, USER_HEAD, USER_TAIL, USER_LIMIT)}'
    if kind in ('function_call', 'custom_tool_call', 'local_shell_call'):
        return f'[#{step}] COMMAND ({call_tool_name(item)}): {cut(raw_command_text(item), CMD_HEAD, CMD_TAIL, CMD_LIMIT)}'
    if kind in ('function_call_output', 'custom_tool_call_output', 'local_shell_call_output'):
        return f'[#{step}] OUTPUT: {cut(output_text(item), OUT_HEAD, OUT_TAIL, OUT_LIMIT)}'
    return f'[#{step}] {str(kind).upper()} item'


def model_view(api_dir: Path) -> tuple[str, dict]:
    """The trajectory as the model saw it (see module docstring). Returns (text, counts)."""
    api_dir = Path(api_dir)
    calls = api_calls(api_dir)
    if not calls:
        return '(no model calls were recorded for this episode)', {'calls': 0, 'compactions': 0,
                                                                  'controller_calls': 0, 'harness_notes': {}}
    transports = {n: _call_json(api_dir, n, 'transport.json') or {} for n in calls}
    forwarded = [n for n in calls if transports[n].get('forwarded') is True]
    final = forwarded[-1] if forwarded else calls[-1]
    requests: dict[int, dict] = {}            # only the segment requests are kept (one request can be ~1 MB)
    compactions = []
    for n in calls:
        request = _call_json(api_dir, n, 'request.json')
        if not isinstance(request, dict) or not isinstance(request.get('input'), list):
            # the broker writes request.json before every upstream call; without it the segments (and so
            # what the model saw) cannot be reconstructed: refuse instead of showing a silently thin view
            raise ValueError(f'broker record {api_dir.name}/{n:03}/request.json is missing or unreadable')
        tools = request.get('tools')
        if is_compaction_request(request.get('input') or [], tools if isinstance(tools, list) else None):
            compactions.append(n)
            if n < final:
                requests[n] = request
        if n == final:
            requests[n] = request
    segments = [n for n in compactions if n < final] + [final]
    seen: set[str] = set()                     # items of earlier segments (re-sent context after a resume)
    lines: list[str] = []
    notes: dict[str, int] = {}
    step = 0
    for n in segments:
        ctrl = _call_json(api_dir, n, 'controller.json') or {}
        inserted = {e.get('public_index'): str(e.get('reason') or 'harness')
                    for e in ctrl.get('inserted') or [] if isinstance(e, dict) and e.get('public_index') is not None}
        items = [it for it in requests[n].get('input') or [] if isinstance(it, dict)]
        is_compaction = n in compactions
        origin = transports[n].get('origin')
        if origin == 'controller':
            lines.append(f'--- call {n}: harness re-query of call {transports[n].get("parent")} (the draft and '
                         f'the harness note below were shown to the model before this response) ---')
        segment_keys: set[str] = set()
        for index, item in enumerate(items):
            if is_compaction and index == len(items) - 1:
                lines.append(f'=== call {n}: CONTEXT COMPACTION. The model was asked to summarise its context; '
                             f'the next calls started from that summary. ===')
                continue
            key = _content_key(item)
            segment_keys.add(key)
            if key in seen:
                continue
            reason = inserted.get(index)
            if reason is None and item.get('type', 'message') == 'message' and item.get('role') == 'user':
                m = _HARNESS.match(_item_text(item))
                reason = m.group(1) if m else None
            step += 1
            text = _render_item(item, step, reason)
            if text is None:
                step -= 1
                continue
            if reason and item.get('role') == 'user':
                notes[reason] = notes.get(reason, 0) + 1
            lines.append(text)
        seen |= segment_keys
    if final not in compactions:
        response = _call_json(api_dir, final, 'response.json') or {}
        for item in response.get('output') or []:
            if not isinstance(item, dict) or item.get('type') == 'reasoning':
                continue
            step += 1
            if item.get('type') == 'message':
                lines.append(f'[#{step}] FINAL MESSAGE: {cut(_item_text(item), MSG_HEAD, MSG_TAIL, MSG_LIMIT)}')
            else:
                rendered = _render_item(item, step, None)
                lines.append((rendered or '') + ' (last response; the episode ended here)')
    else:
        lines.append('(the episode ended during a context compaction)')
    counts = {'calls': len(calls), 'compactions': len(compactions), 'final_call': final,
              'controller_calls': sum(1 for n in calls if transports[n].get('origin') == 'controller'),
              'harness_notes': notes}
    return '\n'.join(lines), counts


def controller_view(api_dir: Path) -> str:
    """api/controller.jsonl rows plus the per-call controller.json verdicts (controller arms only)."""
    api_dir = Path(api_dir)
    lines = []
    log = api_dir / 'controller.jsonl'
    if log.is_file():
        for raw in log.read_text(encoding='utf-8').splitlines():
            if not raw.strip():
                continue
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            no_private_fields(row, 'controller.jsonl')
            lines.append('log ' + one_line(json.dumps(row, ensure_ascii=False, sort_keys=True), 400))
    for n in api_calls(api_dir):
        ctrl = _call_json(api_dir, n, 'controller.json')
        if not isinstance(ctrl, dict):
            continue
        reasons = [str(e.get('reason')) for e in ctrl.get('inserted') or [] if isinstance(e, dict)]
        verdict = ctrl.get('verdict') or {}
        if not reasons and not verdict and not ctrl.get('errors'):
            continue
        parts = [f'call {n} ({ctrl.get("origin")}, {ctrl.get("kind")})']
        if reasons:
            parts.append('inserted: ' + ', '.join(reasons))
        if verdict:
            parts.append(f'verdict: {verdict.get("action")}')
        if ctrl.get('errors'):
            parts.append('errors: ' + one_line('; '.join(map(str, ctrl['errors'])), 300))
        lines.append(' | '.join(parts))
    return '\n'.join(lines) if lines else '(no controller records: this arm runs without the harness controller)'


def _obs_text(event: C.Event | None) -> str:
    if not isinstance(event, C.Observe):
        return 'unmatched'
    attrs = ', '.join(f'{k}={one_line(v, 30)}' for k, v in sorted((event.attrs or {}).items())
                      if k != C.VALUES_ATTR and v not in (None, '', [], {}))[:160]
    return f'{event.ref} {event.locator or "?"}' + (f' [{attrs}]' if attrs else '')


def ledger_view(api_dir: Path) -> str:
    """Ledger summary (controller arms): candidates, calc runs with linked inputs, gate checks."""
    api_dir = Path(api_dir)
    path = api_dir / 'ledger.jsonl'
    if not path.is_file():
        return '(no ledger: this arm runs without the harness controller)'
    events = load_events(path)
    by_ref = {e.ref: e for e in events}
    commands = [e for e in events if isinstance(e, C.Command)]
    notes: dict[str, int] = {}
    for e in events:
        if isinstance(e, C.Note):
            notes[e.reason] = notes.get(e.reason, 0) + 1
    top = max(commands, key=lambda e: e.n, default=None)
    lines = [f'Ledger (built by the harness from what the model saw): commands {len(commands)} '
             f'(distinct {len({e.cmd for e in commands})}; most repeated {top.n}x: {one_line(top.cmd, 120)})'
             if top else 'Ledger: no commands recorded',
             f'compactions {sum(isinstance(e, C.Compaction) for e in events)}; harness notes '
             + (', '.join(f'{k} {v}' for k, v in sorted(notes.items())) or 'none')]
    cands = [e for e in events if isinstance(e, C.Candidate)]
    lines.append('Candidates:' if cands else 'Candidates: none')
    for c in cands:
        extra = ''.join([f' alt "{one_line(c.alt, 60)}"' if c.alt else '', f' reading "{one_line(c.reading, 80)}"' if c.reading else '',
                         f' kept ({one_line(c.keep, 80)})' if c.keep else ''])
        lines.append(f'- {c.ref} "{one_line(c.answer or c.artifact_sha256, 120)}" via {c.via} (call {c.call}){extra}')
    computes = [e for e in events if isinstance(e, C.Compute)]
    lines.append('Calc runs (inputs: literal = linked observation):' if computes else 'Calc runs: none')
    for k in computes[-40:]:
        inputs = []
        for inp in k.inputs:
            if inp.get('trivial'):
                continue
            inputs.append(f"{inp.get('literal')} = {_obs_text(by_ref.get(inp.get('observe') or ''))}"
                          + (f" ({inp.get('match')})" if inp.get('match') else ''))
        lines.append(f'- {k.ref} call {k.call} sha={k.code_sha256[:12]}{" (" + k.source + ")" if k.source.startswith("gate:") else ""}'
                     f' -> {one_line(k.output, 160)}' + (f" | inputs: {'; '.join(inputs[:10])}" if inputs else ''))
    checks = sorted((api_dir / 'checks').glob('[0-9]*.json')) if (api_dir / 'checks').is_dir() else []
    lines.append('Gate checks:' if checks else 'Gate checks: none')
    for path in checks:
        check = _load(path)
        if not isinstance(check, dict):
            continue
        no_private_fields({k: v for k, v in check.items() if k not in ('rules',)}, f'check {path.name}')
        result = check.get('result') or {}
        shown = []
        for f in result.get('findings') or []:
            offer = f.get('offer') or {}
            shown.append(f"{f.get('rule')}: \"{one_line(f.get('text'), 200)}\""
                         + (f" offer \"{one_line(offer.get('answer'), 60)}\" ({one_line(offer.get('derivation'), 120)})" if offer else ''))
        cand = check.get('candidate') or {}
        lines.append(f"- #{check.get('n')} {check.get('kind')} call {check.get('call')} candidate {cand.get('ref')} "
                     f"\"{one_line(cand.get('answer'), 80)}\": {result.get('status')}; fired "
                     f"{', '.join(f.get('rule', '?') for f in check.get('fired') or []) or 'none'}"
                     + (f"; shown {' | '.join(shown)}" if shown else ''))
    return '\n'.join(lines)


# ---------------------------------------------------------------------------------------------
# Episodes and the evidence object
# ---------------------------------------------------------------------------------------------

@dataclass
class EpisodeEvidence:
    ordinal: int
    block: int
    task_id: str
    question: str
    submitted: str
    reference: str
    strict: int
    one_percent: int
    status: str | None
    transport_failed: bool
    out_tokens: int | None
    notes_served: list[str] | None          # note ids rendered into this episode's AGENTS.md (None = unknown)
    notes_source: str                       # 'marker' | 'summary' | 'unknown'
    counts: dict[str, Any]
    trajectory: str
    controller: str
    ledger: str
    has_ledger: bool
    run_dir: str = ''                       # controller-side only (never shown to the learner)

    def summary(self) -> str:
        c = self.counts
        notes = ('unknown' if self.notes_served is None else ', '.join(self.notes_served) or 'none')
        return '\n'.join([
            f'Episode q{self.ordinal:03} (block {self.block}, task {self.task_id})',
            f'Question: {self.question}',
            f'Submitted (sealed): {self.submitted or "(empty)"}',
            f'Reference (released): {self.reference}',
            f'Grade: strict {self.strict}, 1% {self.one_percent}. Status: {self.status}; transport failed: '
            f'{self.transport_failed}; output tokens: {self.out_tokens}',
            f'Model calls {c.get("calls")}, compactions {c.get("compactions")}, harness re-queries '
            f'{c.get("controller_calls")}, harness notes seen {c.get("harness_notes") or {}}',
            f'Learned notes served: {notes}',
            'Parts: trajectory' + (', controller, ledger' if self.has_ledger else ', controller'),
        ])

    def part(self, name: str) -> str:
        if name == 'summary':
            return self.summary()
        if name == 'trajectory':
            return self.trajectory
        if name == 'controller':
            return self.controller
        if name == 'ledger':
            return self.ledger
        raise ValueError(f'unknown part {name!r}; parts: {", ".join(PARTS)}')

    def model_visible(self) -> dict:
        """Everything a learner can see of this episode (hashed into the evidence digest)."""
        return {'ordinal': self.ordinal, 'block': self.block, 'task_id': self.task_id, 'question': self.question,
                'submitted': self.submitted, 'reference': self.reference, 'strict': self.strict,
                'one_percent': self.one_percent, 'status': self.status, 'transport_failed': self.transport_failed,
                'out_tokens': self.out_tokens, 'notes_served': self.notes_served, 'counts': self.counts,
                'trajectory': self.trajectory, 'controller': self.controller, 'ledger': self.ledger}

    def line(self) -> str:
        return (f'q{self.ordinal:03} b{self.block} strict {self.strict} 1% {self.one_percent} '
                f'{self.status}{" transport-lost" if self.transport_failed else ""} | Q: {one_line(self.question, 150)} '
                f'| submitted: {one_line(self.submitted, 60) or "(empty)"} | reference: {one_line(self.reference, 60)}')


@dataclass
class Evidence:
    root: Path
    block: int
    episodes: dict[int, EpisodeEvidence]
    excluded: list[int]                      # ordinals withheld (disputed references); controller-side
    blocks: list[dict] = field(default_factory=list)
    grader: str = ''

    @cached_property
    def digest(self) -> str:
        return canonical_sha([self.episodes[o].model_visible() for o in sorted(self.episodes)])

    def ordinals(self) -> list[int]:
        return sorted(self.episodes)

    def of_block(self, block: int) -> list[EpisodeEvidence]:
        return [self.episodes[o] for o in sorted(self.episodes) if self.episodes[o].block == block]

    def references(self) -> list[str]:
        return [self.episodes[o].reference for o in sorted(self.episodes)]

    def questions(self) -> list[str]:
        return [self.episodes[o].question for o in sorted(self.episodes)]

    def listing(self) -> str:
        right = sum(e.strict for e in self.episodes.values())
        head = (f'{len(self.episodes)} released episodes (blocks 1-{self.block}), strict right {right}; '
                f'{len(self.excluded)} further episodes are withheld (disputed references).')
        return head + '\n' + '\n'.join(self.episodes[o].line() for o in sorted(self.episodes))

    def page(self, text: str, page: int) -> dict:
        pages = max(1, -(-len(text) // PAGE))
        if not isinstance(page, int) or not 1 <= page <= pages:
            raise ValueError(f'page must be 1-{pages}')
        return {'page': page, 'pages': pages, 'total_chars': len(text),
                'content': text[(page - 1) * PAGE: page * PAGE]}

    def read(self, ordinal: int, part: str, page: int = 1) -> dict:
        episode = self.episodes.get(ordinal)
        if episode is None:
            raise ValueError('That ordinal is not a released episode of this campaign (or its reference is withheld)')
        return {'ordinal': ordinal, 'part': part, **self.page(episode.part(part), page)}

    def index(self) -> dict:
        """Controller-side record of what the learners were given (no reference values)."""
        return {'block': self.block, 'digest': self.digest, 'grader': self.grader, 'blocks': self.blocks,
                'excluded_ordinals': list(self.excluded),
                'episodes': [{'ordinal': e.ordinal, 'block': e.block, 'task_id': e.task_id, 'strict': e.strict,
                              'one_percent': e.one_percent, 'status': e.status, 'transport_failed': e.transport_failed,
                              'has_ledger': e.has_ledger, 'notes_served': e.notes_served,
                              'notes_source': e.notes_source, 'calls': e.counts.get('calls'),
                              'compactions': e.counts.get('compactions'),
                              'trajectory_sha256': canonical_sha(e.trajectory), 'run_dir': e.run_dir}
                             for e in (self.episodes[o] for o in sorted(self.episodes))]}


def run_dir_of(root: Path, entry: Mapping, summary: Mapping, ordinal: int) -> Path:
    """Where an episode's broker records live, always inside the root: the manifest's explicit `run_dir`
    (an escape is refused), else the first of [parent of summary.json `records`, <root>/runs/adapted/<NNN>]
    that is inside the root and holds api/ (v8 summaries store absolute paths, so a copied root falls back
    to the conventional location)."""
    root = root.resolve()
    if entry.get('run_dir'):
        candidate = Path(entry['run_dir'])
        candidate = (candidate if candidate.is_absolute() else root / candidate).resolve()
        if not candidate.is_relative_to(root):
            raise ValueError(f'episode {ordinal}: run directory outside the campaign root')
        return candidate
    candidates = []
    if isinstance(summary.get('records'), str) and summary['records']:
        candidates.append(Path(summary['records']).parent)
    candidates.append(root / 'runs' / 'adapted' / f'{ordinal:03}')
    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate.is_relative_to(root) and (candidate / 'api').is_dir():
            return candidate
    raise ValueError(f'episode {ordinal}: no broker records (api/) inside the campaign root')


def served_notes(run: Path, summary: Mapping) -> tuple[list[str] | None, str]:
    marker = _load(run / 'workspace' / '.acting-prepared.json')
    extra = (marker or {}).get('extra') if isinstance(marker, dict) else None
    if isinstance(extra, dict) and isinstance(extra.get('notes'), list):
        return [str(n) for n in extra['notes']], 'marker'
    if isinstance(summary.get('notes_served'), list):
        return [str(n) for n in summary['notes_served']], 'summary'
    return None, 'unknown'


def build_evidence(root: str | os.PathLike, block: int, *, grader: Grader,
                   exclude: Iterable[str] = ()) -> Evidence:
    """Validate every released block (1..block) and build the learners' evidence. `exclude` = task ids
    whose references are withheld from both learners (they are dropped entirely)."""
    root = Path(root).resolve()
    excluded_ids = {str(x) for x in exclude}
    episodes: dict[int, EpisodeEvidence] = {}
    excluded: list[int] = []
    receipts = []
    for b, directory in released_blocks(root, block):
        receipts.append(validate_block(root, directory, b))
        manifest = read_json(directory / 'manifest.json')
        # S6 format: disputed-reference episodes are never released and are listed only under 'withheld';
        # count them so the learners' prompt and evidence-index.json record what was withheld
        for entry in manifest.get('withheld') or []:
            ordinal = entry.get('ordinal') if isinstance(entry, dict) else None
            if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal in excluded:
                raise ValueError(f'block {b}: withheld entries need distinct integer ordinals')
            excluded.append(ordinal)
        for entry in sorted(manifest['episodes'], key=lambda e: e['ordinal']):
            ordinal, task_id = int(entry['ordinal']), str(entry['id'])
            if ordinal in episodes or ordinal in excluded:
                raise ValueError(f'ordinal {ordinal} released twice')
            if task_id in excluded_ids:
                excluded.append(ordinal)
                continue
            base = directory / str(entry['directory'])
            task = read_json(base / 'task.json')
            prediction = read_json(base / 'prediction.json')
            reference = str(read_json(base / 'reference_answer.json')['official_answer'])
            summary = _load(base / 'summary.json') or {}
            failure = _load(base / 'failure.json') or {}
            submitted = str(prediction.get('answer') or '')
            run = run_dir_of(root, entry, summary, ordinal)
            api = run / 'api'
            if not api.is_dir():
                raise ValueError(f'episode {ordinal}: no broker records (api/) at the declared run directory')
            trajectory, counts = model_view(api)
            usage = summary.get('usage') or {}
            notes, notes_source = served_notes(run, summary)
            has_ledger = (api / 'ledger.jsonl').is_file()
            episodes[ordinal] = EpisodeEvidence(
                ordinal=ordinal, block=b, task_id=task_id, question=str(task.get('question') or ''),
                submitted=submitted, reference=reference,
                strict=grader.strict(submitted, reference), one_percent=grader.one_percent(submitted, reference),
                status=failure.get('status', summary.get('status')),
                transport_failed=bool(summary.get('transport_failed')),
                out_tokens=token_count(usage.get('output_tokens')) if isinstance(usage, dict) else None,
                notes_served=notes, notes_source=notes_source, counts=counts, trajectory=trajectory,
                controller=controller_view(api), ledger=ledger_view(api) if has_ledger else
                '(no ledger: this arm runs without the harness controller)', has_ledger=has_ledger, run_dir=str(run))
    evidence = Evidence(root=root, block=block, episodes=episodes, excluded=sorted(excluded), blocks=receipts,
                        grader=grader.name)
    for e in episodes.values():
        no_private_fields(e.model_visible(), f'episode {e.ordinal}')
    return evidence


def snapshot(root: str | os.PathLike) -> dict[str, str]:
    """sha256 per regular file under `root` (no symlinks), for protected-tree checks."""
    root = Path(root)
    out = {}
    for path in sorted(root.rglob('*')):
        rel = path.relative_to(root)
        if any(part in ('__pycache__', '.git') for part in rel.parts):
            continue
        if path.is_symlink():
            raise ValueError(f'symlink in a protected tree: {rel}')
        if path.is_file():
            out[rel.as_posix()] = sha256_bytes(path.read_bytes())
    return out


def revision(files: Mapping[str, str]) -> str:
    return sha256_bytes(json.dumps(dict(files), sort_keys=True).encode())
