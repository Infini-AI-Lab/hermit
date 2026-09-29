"""v9 broker-side controller (L2 ledger feed, L3 loop guard + ledger splice, L4 submit/enforcement).

Two parts:
- Pure helpers the broker and the offline replay share: `classify` (turn / compaction / resume),
  `normalize_output` (drop Codex's volatile 'Chunk ID:' / 'Wall time:' header lines), `command_of`
  (normalised command of a tool call), `echo` (Codex's own re-serialisation of provider output items),
  `note_item` (a harness note as a role:user input_text message), `is_final_answer`, `final_text`,
  `numeric_tokens`, `anchor_key`, plus the v8 replay helpers `replay_run` / `replay_root`.
- `MechController`, the framework-owned implementation of `contracts.Controller` for the mech arms. It
  owns no model code and no network: it feeds the broker-derived ledger (`contracts.LedgerAPI`), decides
  loop notes and the post-compaction ledger splice, and asks the gate (`contracts.GateAPI`) for submit
  results and for the checks behind a submit enforcement re-query. Every decision is deterministic in
  its inputs (the Codex items, ledger state, policy and the injected clock).

Codex facts these helpers rely on (verified with the pinned 0.155.0-alpha.9.2 binary against a fake
upstream, tests/test_v9_codex_live.py, and on the v8 records, understand/broker.md §3, compaction.md §2):
- a compaction request offers no tools and ends with the fixed user prompt COMPACTION_PROMPT;
- the resume request is [developer, AGENTS.md user, task user, summary user] with fresh ids, the summary
  starting with RESUME_PREFIX;
- Codex re-sends provider items as: reasoning {type,id,summary,content,encrypted_content} (missing
  fields as null), message {type,id,role,content:[{type,text}]} (status, annotations, logprobs dropped),
  function_call {type,id,name,arguments,call_id} (status and unknown keys dropped).
"""
from __future__ import annotations

from collections import Counter
import glob
import hashlib
import json
import math
import os
import re
import time
from typing import Any, Mapping, Sequence

from harness_core import contracts as C

COMPACTION_PROMPT = ('You are performing a CONTEXT CHECKPOINT COMPACTION. Create a handoff summary '
                     'for another LLM that will resume the task.')
RESUME_PREFIX = 'Another language model started to solve this problem'
CALL_TYPES = ('function_call', 'custom_tool_call', 'local_shell_call')
OUTPUT_TYPES = ('function_call_output', 'custom_tool_call_output', 'local_shell_call_output')
POLL_TOOLS = ('write_stdin',)          # polls of a running session: repeats are expected, never a loop
NOTE_PREFIX = '[harness:{reason}] '
_VOLATILE = re.compile(r'^(Chunk ID:|Wall time:)')
_HEADER = re.compile(r'^(Chunk ID:|Wall time:|Process exited with code|Process running with session ID|'
                     r'Original token count:|Output:)')
_NUMBER = re.compile(r'(?<![\w.])[-−]?\$?\s?(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?(?![\w])')


# ---------------------------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------------------------

def canonical_sha(value) -> str:
    """sha256 of the canonical JSON form (sorted keys, compact): independent of Codex's HashMap order."""
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(data.encode('utf-8')).hexdigest()


def message_text(item) -> str:
    if not isinstance(item, dict) or item.get('type', 'message') != 'message':
        return ''
    content = item.get('content')
    if isinstance(content, str):
        return content
    return ''.join(str(p.get('text') or '') for p in content or [] if isinstance(p, dict))


def is_compaction_prompt(item) -> bool:
    return isinstance(item, dict) and item.get('role') == 'user' and message_text(item).startswith(COMPACTION_PROMPT)


def is_summary(item) -> bool:
    return isinstance(item, dict) and item.get('role') == 'user' and message_text(item).startswith(RESUME_PREFIX)


def summary_index(codex_input: Sequence[dict]) -> int | None:
    """Index of the (latest) compaction summary item, or None."""
    found = None
    for index, item in enumerate(codex_input or []):
        if is_summary(item):
            found = index
    return found


def classify(codex_input: Sequence[dict], tools_offered: bool) -> str:
    """'compaction': no tools offered and the last item is Codex's fixed compaction prompt (v8: 119/119);
    'resume': the history restarts from a compaction summary and nothing but user/developer messages
    follow it (the first request after a compaction, or its retry); otherwise 'turn'."""
    items = [it for it in codex_input or [] if isinstance(it, dict)]
    if not tools_offered and items and is_compaction_prompt(items[-1]):
        return 'compaction'
    index = summary_index(items)
    if index is not None and all(it.get('type', 'message') == 'message' and it.get('role') in ('user', 'developer')
                                 for it in items[index + 1:]):
        return 'resume'
    return 'turn'


def normalize_output(text: str) -> str:
    """The tool output as the model saw it, minus the per-call random 'Chunk ID:' and 'Wall time:'
    header lines (only in the leading header block; body lines are never touched)."""
    lines = str(text or '').split('\n')
    out, header = [], True
    for line in lines:
        if header and _HEADER.match(line):
            if not _VOLATILE.match(line):
                out.append(line)
            if line == 'Output:':
                header = False
            continue
        header = False
        out.append(line)
    return '\n'.join(out)


def output_text(item) -> str:
    out = item.get('output') if isinstance(item, dict) else None
    if isinstance(out, list):
        return '\n'.join(str(p.get('text') or '') for p in out if isinstance(p, dict))
    return '' if out is None else str(out)


def tool_name(item) -> str:
    return str(item.get('name') or item.get('type') or '') if isinstance(item, dict) else ''


def command_of(item) -> str | None:
    """Normalised command text of a tool-call item (None for other items): exec_command -> its 'cmd'
    with whitespace collapsed (what v8 events.jsonl commands compare equal on); any other tool ->
    '<name> <canonical JSON arguments>'."""
    if not isinstance(item, dict) or item.get('type') not in CALL_TYPES:
        return None
    name = tool_name(item)
    if item.get('type') == 'custom_tool_call':
        return ' '.join(f"{name} {item.get('input') or ''}".split())
    raw = item.get('arguments') if item.get('type') == 'function_call' else item.get('action')
    try:
        args = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return ' '.join(f'{name} {raw}'.split())
    if isinstance(args, dict) and isinstance(args.get('cmd'), str):
        return ' '.join(args['cmd'].split())
    command = args.get('command') if isinstance(args, dict) else None
    if isinstance(command, list) and command and all(isinstance(c, str) for c in command):
        return ' '.join((command[-1] if len(command) >= 3 and command[-2] in ('-lc', '-c') else ' '.join(command)).split())
    if isinstance(command, str):              # same as harness_core.ledger.command_text (S4)
        return ' '.join(command.split())
    return ' '.join(f'{name} {json.dumps(args, sort_keys=True, ensure_ascii=False)}'.split())


def echo(output: Sequence[dict]) -> list[dict]:
    """Codex's re-serialisation of provider output items, as it appears in its next request (live-checked
    key order and field set). Unknown item types keep every field except 'status'."""
    items = []
    for item in output or []:
        if not isinstance(item, dict):
            continue
        kind = item.get('type')
        if kind == 'reasoning':
            value = {'type': 'reasoning'}
            if item.get('id') is not None:
                value['id'] = item['id']
            value.update(summary=item.get('summary') or [], content=item.get('content'),
                         encrypted_content=item.get('encrypted_content'))
        elif kind == 'message':
            value = {'type': 'message'}
            if item.get('id') is not None:
                value['id'] = item['id']
            value['role'] = item.get('role', 'assistant')
            value['content'] = [{'type': p.get('type'), 'text': p.get('text')} if isinstance(p, dict) and 'text' in p
                                else p for p in item.get('content') or []]
        elif kind == 'function_call':
            value = {'type': 'function_call'}
            if item.get('id') is not None:
                value['id'] = item['id']
            value.update(name=item.get('name'), arguments=item.get('arguments'), call_id=item.get('call_id'))
        else:
            value = {k: v for k, v in item.items() if k != 'status'}
        items.append(value)
    return items


def note_item(reason: str, text: str) -> dict:
    """A broker note: role 'user' input_text (Fireworks accepts a user message after a tool output;
    a mid-history developer message is untested), no id (Codex's compaction prompt has none)."""
    return {'type': 'message', 'role': 'user',
            'content': [{'type': 'input_text', 'text': NOTE_PREFIX.format(reason=reason) + str(text)}]}


def is_final_answer(final: Mapping, tools_offered: bool) -> bool:
    """Codex ends the turn on this response: completed, the request offered tools (not a compaction),
    no tool call, and at least one message with non-empty text."""
    if not tools_offered or not isinstance(final, Mapping) or final.get('status') != 'completed':
        return False
    output = [item for item in final.get('output') or [] if isinstance(item, dict)]
    if any(item.get('type') in CALL_TYPES for item in output):
        return False
    return any(item.get('type') == 'message' and message_text(item).strip() for item in output)


def final_text(final: Mapping) -> str:
    """The last non-empty assistant message: what Codex writes to final.md."""
    texts = [message_text(item) for item in (final or {}).get('output') or []
             if isinstance(item, dict) and item.get('type') == 'message' and message_text(item).strip()]
    return texts[-1] if texts else ''


def last_answer_line(text: str) -> str:
    """Default answer span of a final message for packs without `extract_final`: the last non-empty line."""
    lines = [line.strip() for line in str(text or '').splitlines() if line.strip()]
    return lines[-1] if lines else ''


def numeric_tokens(text: str) -> list[float]:
    """Numbers in `text` in order, format-free ('$1,234.5 million' -> [1234.5]); signs ignored because
    tables print negatives as '-1', '(1)' or in words."""
    values = []
    for match in _NUMBER.finditer(str(text or '').replace('−', '-')):
        try:
            value = abs(float(match.group(0).replace('$', '').replace(',', '').replace(' ', '').replace('-', '')))
        except ValueError:
            continue
        if math.isfinite(value):
            values.append(value)
    return values


def same_answer(a: str, b: str) -> bool:
    """Equal numeric tokens (tolerance 1e-9 relative), or, when neither has numbers, equal text up to
    case and whitespace."""
    x, y = numeric_tokens(a), numeric_tokens(b)
    if x or y:
        return len(x) == len(y) and all(math.isclose(p, q, rel_tol=1e-9, abs_tol=1e-12) for p, q in zip(x, y))
    norm = lambda s: ' '.join(str(s or '').lower().split()).strip(' .')
    return norm(a) == norm(b)


def anchor_key(item) -> str:
    """Stable identity of a Codex item for splice anchoring: its id, else a hash of its content."""
    if isinstance(item, dict) and isinstance(item.get('id'), str) and item['id']:
        return 'id:' + item['id']
    return 'sha:' + canonical_sha(item)[:32]


def clip(text: str, n: int) -> str:
    text = ' '.join(str(text or '').split())
    return text if len(text) <= n else text[:n - 1] + '…'

# ---------------------------------------------------------------------------------------------
# Offline replay over recorded v8 api/ directories (read-only)
# ---------------------------------------------------------------------------------------------


def _load(path):
    try:
        with open(path) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def replay_run(api_dir: str, threshold: int = 3) -> dict:
    """Classify every recorded request and rebuild the command log from the forwarded responses
    (api/NNN/response.json), the way the broker ledger sees them. Returns per-run counts:
    compactions, resumes, first loop call (any identical normalised exec command issued `threshold`
    times, counted across compactions) and the longest consecutive identical-command streak."""
    calls = sorted(glob.glob(os.path.join(api_dir, '[0-9][0-9][0-9]')))
    kinds, counts, commands = Counter(), Counter(), []
    first_loop = None
    for directory in calls:
        number = int(os.path.basename(directory))
        request = _load(os.path.join(directory, 'request.json'))
        if isinstance(request, dict):
            kinds[classify(request.get('input') or [], bool(request.get('tools')))] += 1
        response = _load(os.path.join(directory, 'response.json'))
        for item in (response or {}).get('output') or []:
            if isinstance(item, dict) and item.get('type') in CALL_TYPES and tool_name(item) not in POLL_TOOLS:
                cmd = command_of(item)
                counts[cmd] += 1
                commands.append(cmd)
                if first_loop is None and counts[cmd] >= threshold:
                    first_loop = number
    streak = best = 0
    for index, cmd in enumerate(commands):
        streak = streak + 1 if index and cmd == commands[index - 1] else 1
        best = max(best, streak)
    return {'calls': len(calls), 'compactions': kinds['compaction'], 'resumes': kinds['resume'],
            'turns': kinds['turn'], 'commands': len(commands), 'first_loop_call': first_loop,
            'loop': first_loop is not None, 'max_streak': best}


def replay_root(runs_dir: str, threshold: int = 3) -> dict:
    """Aggregate `replay_run` over <root>/runs/<arm>/NNN/api."""
    runs = {}
    for run in sorted(glob.glob(os.path.join(runs_dir, '[0-9][0-9][0-9]'))):
        runs[os.path.basename(run)] = replay_run(os.path.join(run, 'api'), threshold)
    return {'runs': len(runs), 'compactions': sum(r['compactions'] for r in runs.values()),
            'resumes': sum(r['resumes'] for r in runs.values()),
            'compacted_runs': sum(r['compactions'] > 0 for r in runs.values()),
            'loop_runs': sum(r['loop'] for r in runs.values()),
            'streak_runs': sum(r['max_streak'] >= threshold for r in runs.values()), 'per_run': runs}

# ---------------------------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------------------------

LOOP_TEMPLATE = ('You have issued this exact command {n} times: {cmd}\n{identical}'
                 'Current candidates: {candidates}\n{options}')
LOOP_OPTIONS = 'Options: submit --answer "<A>" for a candidate you trust, or run a command you have not run yet.'
LOOP_ESCALATE = 'Candidate {ref} was computed after your last new evidence. Submit now: submit --answer "{answer}"'
ENFORCE_TEMPLATE = ('Your final answer was not submitted with `submit`, so it was checked now:\n{message}\n'
                    'Reply with your final answer (revised or unchanged), or run submit --answer "<A>" first.')


def checked_policy(policy: Mapping[str, Any] | None) -> dict[str, Any]:
    """POLICY_DEFAULTS merged with `policy`, refused (ValueError) when a key is unknown or a value is
    outside contracts.POLICY_RANGES. A learner-edited policy.json can otherwise switch the controller
    into an undeclared regime (e.g. loop_threshold 1, enforce_requery_max 5). Same rule as
    harness_core.rules.validate_policy (S4), kept local so the broker side does not import S4."""
    merged = {**C.POLICY_DEFAULTS, **dict(policy or {})}
    errors = []
    for key, value in merged.items():
        if key not in C.POLICY_DEFAULTS:
            errors.append(f'unknown policy key {key}')
        elif key == 'disabled_rules':
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                errors.append('disabled_rules must be a list of rule ids')
        elif key in C.POLICY_RANGES:
            lo, hi = C.POLICY_RANGES[key]
            if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
                errors.append(f'{key} must be an integer in [{lo}, {hi}]')
    if errors:
        raise ValueError('; '.join(errors))
    return merged


class MechController:
    """contracts.Controller for the mech arms (DESIGN §2 L2-L4). Construct with the episode's
    ledger (contracts.LedgerAPI), gate (contracts.GateAPI), policy (merged over POLICY_DEFAULTS) and a
    monotonic clock; `t` values given to the ledger/gate are seconds since construction (the broker is
    created right before the actor starts).

    Call numbers: `View.call` (contracts v9.1; the broker sets it) is the api/NNN number of the upstream
    call a view concerns; the optional duck hook `bind_call(n)` (called by the broker right before each
    upstream call) is kept as a fallback for views without it. `drain_log()` returns decision details for
    api/controller.jsonl.

    Enforcement (contracts GateAPI): only the answer span of the final message, `pack.extract_final(...)`
    (the pack is `pack=`, else the gate's or the ledger's; without one, the last non-empty line), is
    compared with the submissions (`gate.needs_enforcement`) and checked (`gate.check_final`), so numbers in
    surrounding prose never become part of the candidate (S4-review 1)."""

    def __init__(self, ledger: C.LedgerAPI, gate: C.GateAPI, policy: Mapping[str, Any] | None = None,
                 clock=time.monotonic, *, t0: float | None = None, requery_when_clean: bool = False,
                 pack: C.Pack | None = None):
        """`requery_when_clean`: also re-query an unsubmitted final answer whose checks found nothing
        (feedback = 'not submitted' + the gate's message). Default off (decided for all v9 arms): a clean
        final is forwarded, so the model is re-queried only when the gate has findings."""
        self.ledger, self.gate, self.clock = ledger, gate, clock
        self.pack = pack if pack is not None else getattr(gate, 'pack', None) or getattr(ledger, 'pack', None)
        self.requery_when_clean = bool(requery_when_clean)
        self.policy = checked_policy(policy)
        self.t0 = clock() if t0 is None else t0
        self.call = 0                          # api/NNN of the current / latest upstream call
        self.tools_by_call: dict[str, str] = {}
        self.noted: set[str] = set()           # commands that already received a loop note
        self.loop_notes = 0
        self.spliced_summaries: set[str] = set()
        self.enforced = 0
        self.decisions: list[dict] = []
        # enforcement feedback waits here until the re-query is actually sent (a Note is text the
        # model saw; if the re-query fails the broker forwards the draft and the note was never shown)
        self.pending_note: dict | None = None

    # -- adapter hooks ---------------------------------------------------------------------------
    def bind_call(self, call: int) -> None:
        self.call = int(call)
        if self.pending_note is not None and self.pending_note['call'] is None:
            self.pending_note['call'] = self.call          # the re-query's own api/NNN

    def drain_log(self) -> list[dict]:
        rows, self.decisions = self.decisions, []
        return rows

    def _t(self, view: C.View | None = None) -> float:
        return round(view.elapsed_s if view is not None else self.clock() - self.t0, 6)

    def _note_event(self, reason: str, text: str, t: float, sticky: bool = True) -> None:
        # ref '' : the ledger assigns it (LedgerAPI.add), so refs never collide with the gate's notes
        self.ledger.add(C.Note(kind='note', ref='', call=self.call, t=t, reason=reason, text=text, sticky=sticky))

    def _use_view_call(self, view: C.View | None) -> None:
        call = getattr(view, 'call', 0) if view is not None else 0
        if isinstance(call, int) and not isinstance(call, bool) and call > 0:
            self.call = call

    def answer_span(self, final_message: str) -> str:
        """The part of a final message that is the answer (pack.extract_final; default: last line)."""
        extract = getattr(self.pack, 'extract_final', None)
        if callable(extract):
            return str(extract(final_message) or '')
        return last_answer_line(final_message)

    def _settle_note(self, call: int | None) -> None:
        """Record the pending enforcement note if `call` is the re-query that carried it (its response
        arrived); drop it (logged) when anything else happens first."""
        pending, self.pending_note = self.pending_note, None
        if pending is None:
            return
        if call is not None and pending['call'] in (None, call):
            self._note_event('enforce', pending['text'], pending['t'])
        else:
            self.decisions.append({'event': 'enforce_not_shown', 'call': pending['call']})

    # -- Controller protocol ---------------------------------------------------------------------
    def on_request(self, view: C.View) -> list[C.Insert]:
        self._settle_note(None)                # a new Codex request: an unsent re-query never happened
        self._use_view_call(view)
        t = self._t(view)
        self.ledger.ingest_request(self.call, view.codex_input, t)
        inserts: list[C.Insert] = []
        if view.kind == 'compaction':
            return inserts
        if view.kind == 'resume':
            index = summary_index(view.codex_input)
            key = canonical_sha(view.codex_input[index]) if index is not None else ''
            if key and key not in self.spliced_summaries:
                self.spliced_summaries.add(key)
                text = self.ledger.render(int(self.policy['ledger_splice_bytes']))
                if text.strip():
                    inserts.append(C.Insert('ledger', text, anchor='summary', sticky=True))
                    self._note_event('ledger', text, t)
                self.decisions.append({'event': 'ledger_splice', 'bytes': len(text.encode('utf-8')),
                                       'sha256': C.sha256_text(text), 'empty': not text.strip()})
        if view.tools_offered:
            inserts.extend(self._loop_guard(t))
        return inserts

    def on_response(self, view: C.View, response: dict, call: int) -> None:
        self.call = int(call)                  # the call whose response this is (== view.call when set)
        self._settle_note(self.call)
        for item in (response or {}).get('output') or []:
            if isinstance(item, dict) and item.get('type') in CALL_TYPES:
                self.tools_by_call[str(item.get('call_id') or item.get('id') or '')] = tool_name(item)
        self.ledger.ingest_response(int(call), response, self._t(view))

    def on_final(self, view: C.View, response: dict, round: int) -> C.Verdict:
        self._use_view_call(view)              # the call that produced this final message
        reason = self._enforce_skip(view, response)
        if reason:
            self.decisions.append({'event': 'enforce_skip', 'reason': reason, 'round': round})
            return C.Verdict('forward')
        span = self.answer_span(final_text(response))
        result = self.gate.check_final(span, self.ledger, self._t(view), self.call)
        clean = result.status != 'findings' or not (result.findings or result.message.strip())
        if clean and not (self.requery_when_clean and result.message.strip()):
            self.decisions.append({'event': 'enforce_skip', 'reason': 'final_checked_' + result.status,
                                   'round': round, 'candidate': result.candidate_ref})
            return C.Verdict('forward')
        self.enforced += 1
        feedback = ENFORCE_TEMPLATE.format(message=result.message.strip())
        self.pending_note = {'text': feedback, 't': self._t(view), 'call': None}   # see _settle_note
        self.decisions.append({'event': 'enforce', 'round': round, 'rules': [f.rule for f in result.findings],
                               'candidate': result.candidate_ref, 'n': self.enforced})
        return C.Verdict('revise', feedback)

    def on_submit(self, payload: Mapping[str, Any]) -> C.GateResult:
        result = self.gate.submit(payload, self.ledger, self._t(), self.call)
        self.decisions.append({'event': 'submit', 'status': result.status, 'candidate': result.candidate_ref,
                               'rules': [f.rule for f in result.findings]})
        return result

    # -- internals -------------------------------------------------------------------------------
    def _enforce_skip(self, view: C.View, response: dict) -> str:
        if view.kind == 'compaction' or not view.tools_offered or not is_final_answer(response, True):
            return 'not_final'
        if self.enforced >= int(self.policy['enforce_requery_max']):
            return 'cap'
        if view.remaining_s is not None and view.remaining_s < float(self.policy['enforce_min_remaining_s']):
            return 'time'
        text = self.answer_span(final_text(response))
        if not text.strip():
            return 'no_answer_span'
        needs = getattr(self.gate, 'needs_enforcement', None)
        if needs is not None:                 # the gate's pack-aware rule (DESIGN: vs the last submission)
            return '' if needs(text, self.ledger) else 'submitted'
        if any(c.via == 'submit' and same_answer(c.answer, text) for c in self.ledger.candidates()):
            return 'submitted'
        return ''

    def _loop_guard(self, t: float) -> list[C.Insert]:
        threshold = int(self.policy['loop_threshold'])
        events = list(self.ledger.events)
        commands: dict[str, list] = {}
        for event in events:
            if getattr(event, 'kind', '') == 'command':
                name = getattr(event, 'tool', '') or self.tools_by_call.get(event.call_id, '')
                if name in POLL_TOOLS or event.cmd.startswith(POLL_TOOLS):
                    continue
                commands.setdefault(event.cmd, []).append(event)
        inserts = []
        for cmd, issued in commands.items():
            if cmd in self.noted or self.ledger.command_count(cmd) < threshold:
                continue
            self.noted.add(cmd)
            self.loop_notes += 1
            text, escalated = self._loop_text(cmd, self.ledger.command_count(cmd), issued, events)
            inserts.append(C.Insert('loop_guard', text, anchor='tail', sticky=True))
            self._note_event('loop_guard', text, t)
            self.decisions.append({'event': 'loop_note', 'cmd_sha256': C.sha256_text(cmd),
                                   'n': self.ledger.command_count(cmd), 'note': self.loop_notes,
                                   'escalated': escalated})
        return inserts

    def _loop_text(self, cmd: str, n: int, issued: list, events: list) -> tuple[str, bool]:
        shas = {e.output_sha256 for e in issued if e.output_sha256}
        identical = ('Its output was identical each time.\n' if len(shas) == 1 else
                     'Its output changed between runs.\n' if len(shas) > 1 else '')
        candidates = self.ledger.candidates()
        computes = [e for e in events if getattr(e, 'kind', '') == 'compute']
        shown = []
        for cand in candidates[-3:]:
            values = numeric_tokens(cand.answer)
            refs = [k.ref for k in computes if any(_close(v, w) for v in values for w in k.values)]
            shown.append(f'{cand.ref} "{clip(cand.answer, 60)}"' + (f' ({", ".join(refs[-3:])})' if refs else ''))
        options, escalated = LOOP_OPTIONS, False
        if self.loop_notes > int(self.policy['loop_notes_max']):
            fresh = self._fresh_candidate(events)
            if fresh is not None:
                options, escalated = LOOP_ESCALATE.format(ref=fresh.ref, answer=clip(fresh.answer, 80)), True
        text = LOOP_TEMPLATE.format(n=n, cmd=clip(cmd, 200), identical=identical,
                                    candidates='; '.join(shown) if shown else 'none yet.', options=options)
        return text, escalated

    @staticmethod
    def _fresh_candidate(events: list):
        """The newest candidate added after the last new observation (evidence), if any."""
        last_evidence = max((i for i, e in enumerate(events) if getattr(e, 'kind', '') == 'observe'), default=-1)
        fresh = [e for i, e in enumerate(events) if getattr(e, 'kind', '') == 'candidate' and i > last_evidence]
        return fresh[-1] if fresh else None


def _close(a: float, b: float) -> bool:
    a, b = abs(float(a)), abs(float(b))
    if math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12):
        return True
    # a candidate printed with fewer decimals than the compute output ('12.35' vs 12.3456)
    for decimals in range(0, 7):
        if round(b, decimals) == a:
            return True
    return False


def gate_result_json(result: C.GateResult) -> dict:
    """What /v1/submit returns to the actor: status, message, candidate ref and per-finding rule, text and
    offer (answer + derivation). Bindings and ledger internals are never exposed."""
    findings = []
    for finding in result.findings or []:
        entry = {'rule': finding.rule, 'text': finding.text}
        if finding.offer is not None:
            entry['offer'] = {'answer': finding.offer.answer, 'derivation': finding.offer.derivation}
        findings.append(entry)
    return {'status': result.status, 'message': result.message, 'candidate_ref': result.candidate_ref,
            'findings': findings}

