"""L2 ledger: the broker-owned event log of one episode (implements contracts.LedgerAPI).

Built only from the Responses API items the broker relays (what the model actually saw) and from
events the trusted gate adds; it never reads a file the actor can write. Each event is appended as
one JSON line to `out_path` (`<run>/api/ledger.jsonl`) the moment it exists.

Line format. A Command is written twice: when its `function_call` is relayed (cmd, running count n)
and again, same `ref`, when its `function_call_output` arrives (exit code, output hash/size,
truncation). Readers fold lines by `ref`, later line wins, first-seen order kept (`load_events`), so
the first N lines always reproduce the exact ledger state at the moment line N was written; gate
checks record N so offline replay sees identical inputs.

Output normalisation (Codex exec_command outputs):
    Chunk ID: 973a2d                      <- random, stripped
    Wall time: 0.1297 seconds             <- varies, stripped
    Process exited with code 0            <- exit_code
    Original token count: 12483
    Output:
    Warning: truncated output (original token count: 12483)   <- Codex truncation (also '…N chars truncated…')
    Total output lines: 720
    <body>
`output_sha256` is over the text minus the first two lines, so identical outputs hash identically.
Observations are derived from the body only (header/warning lines carry unrelated numbers), harness
text (gate messages) is removed first, and `calc` frames become Compute events instead of
observations. Lines cut by a truncation marker are dropped before a pack sees them, unless the pack
declares `handles_truncation = True` (it then gets the body with Codex's markers kept and drops the
cut fragments itself; S2-review 1). A pack may declare `max_observations` (per output; default 400).
`pack.observations` receives the normalised command and, when its signature accepts it,
`raw_command=` (the command exactly as issued, ≤ 8 KB; Command.raw_cmd).

Concurrency (S2-review 3): `ingest_request` does its bookkeeping under the ledger lock, runs the pack
re-derivation (pure; may re-run pack tools for seconds) outside it, then appends the observations
and computes under the lock. Requests are committed in the order they entered (a ticket per call),
so the event order never depends on thread timing.
"""
from __future__ import annotations

from collections import Counter
import dataclasses
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import threading
from typing import Any, Callable, Iterable, Sequence

from . import contracts as C
from .numeric import ValueIndex, code_literals, numbers_in

COMPACTION_PROMPT = 'You are performing a CONTEXT CHECKPOINT COMPACTION'
RESUME_PREFIX = 'Another language model started to solve this problem'
REF_PREFIX = {'command': 'x', 'observe': 'c', 'compute': 'k', 'candidate': 'a', 'note': 'n', 'compaction': 'z'}
CALL_TYPES = ('function_call', 'custom_tool_call', 'local_shell_call')
OUTPUT_TYPES = ('function_call_output', 'custom_tool_call_output', 'local_shell_call_output')

_HEADER = re.compile(r'^(Chunk ID:|Wall time:|Process exited with code|Process running with session ID|'
                     r'Original token count:|Output:$)')
_VOLATILE = re.compile(r'^(Chunk ID:|Wall time:)')
_EXIT = re.compile(r'^Process exited with code (-?\d+)')
_BODY_NOTICE = re.compile(r'^(Warning: truncated output \(original token count: \d+\)|Total output lines: \d+)$')
TRUNC_MARK = re.compile(r'…\d+ (?:chars|tokens) truncated…')
MAX_OBS_TEXT = 200
DEFAULT_MAX_OBSERVATIONS = 400    # per output, unless the pack declares max_observations
RAW_CMD_MAX = 8192                # bytes of Command.raw_cmd (contracts: ≤ 8 KB)
_PROVISIONAL = '?'                # prefix of the refs a pack gets while it runs outside the lock


@dataclasses.dataclass(frozen=True)
class OutputView:
    normalized: str               # the model-visible text minus 'Chunk ID:'/'Wall time:' lines
    body: str                     # text after the Codex header and truncation notices
    exit_code: int | None
    truncated: bool
    sha256: str


def normalize_command(cmd: str) -> str:
    return ' '.join(str(cmd or '').split())


def command_text(item: dict) -> str:
    """Normalised command for a function_call / custom_tool_call item.
    exec_command -> its 'cmd' argument; shell-style list commands -> the script; any other tool ->
    '<name> <canonical JSON arguments>' (so identical calls compare equal)."""
    name = str(item.get('name') or item.get('type') or '')
    if item.get('type') == 'custom_tool_call':
        return normalize_command(f"{name} {item.get('input') or ''}")
    raw = item.get('arguments')
    if item.get('type') == 'local_shell_call':
        raw = (item.get('action') or {})
    try:
        args = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return normalize_command(f'{name} {raw}')
    if isinstance(args, dict):
        if isinstance(args.get('cmd'), str):
            return normalize_command(args['cmd'])
        command = args.get('command')
        if isinstance(command, list) and command and all(isinstance(c, str) for c in command):
            if len(command) >= 3 and command[-2] in ('-lc', '-c'):
                return normalize_command(command[-1])
            return normalize_command(' '.join(command))
        if isinstance(command, str):
            return normalize_command(command)
    return normalize_command(f'{name} {json.dumps(args, sort_keys=True, ensure_ascii=False)}')


def raw_command_text(item: dict) -> str:
    """The command of a call item as issued (no whitespace collapsing), for pack parsers: exec_command
    -> its 'cmd'; shell-style list commands -> the script; any other tool -> '<name> <JSON arguments>'."""
    name = str(item.get('name') or item.get('type') or '')
    if item.get('type') == 'custom_tool_call':
        return f"{name} {item.get('input') or ''}"
    raw = item.get('arguments')
    if item.get('type') == 'local_shell_call':
        raw = (item.get('action') or {})
    try:
        args = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return f'{name} {raw}'
    if isinstance(args, dict):
        if isinstance(args.get('cmd'), str):
            return args['cmd']
        command = args.get('command')
        if isinstance(command, list) and command and all(isinstance(c, str) for c in command):
            if len(command) >= 3 and command[-2] in ('-lc', '-c'):
                return command[-1]
            return ' '.join(command)
        if isinstance(command, str):
            return command
    return f'{name} {json.dumps(args, sort_keys=True, ensure_ascii=False)}'


def call_tool_name(item: dict) -> str:
    """Codex tool name of a call item (exec_command, write_stdin, …; 'local_shell' for local_shell_call)."""
    if item.get('type') == 'local_shell_call':
        return 'local_shell'
    return str(item.get('name') or item.get('type') or '')


def clip_utf8(text: str, max_bytes: int) -> str:
    data = str(text or '').encode('utf-8')
    if len(data) <= max_bytes:
        return str(text or '')
    return data[:max_bytes].decode('utf-8', errors='ignore')


def _accepts_keyword(fn: Any, name: str) -> bool:
    """Whether `fn` takes keyword `name` (explicitly or via **kwargs); packs written against the
    v9.0 contract have no `raw_command` parameter."""
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.name == name and p.kind in (p.KEYWORD_ONLY, p.POSITIONAL_OR_KEYWORD) or p.kind == p.VAR_KEYWORD
               for p in params)


def output_text(item: dict) -> str:
    out = item.get('output')
    if isinstance(out, list):
        return '\n'.join(str(p.get('text') or '') for p in out if isinstance(p, dict))
    if isinstance(out, dict):
        return str(out.get('text') or out.get('output') or json.dumps(out, sort_keys=True))
    return '' if out is None else str(out)


def normalize_output(text: str) -> OutputView:
    lines = (text or '').split('\n')
    i, exit_code, saw_output = 0, None, False
    while i < len(lines) and _HEADER.match(lines[i]):
        m = _EXIT.match(lines[i])
        if m:
            exit_code = int(m.group(1))
        saw_output = lines[i] == 'Output:'
        i += 1
        if saw_output:
            break
    header, rest = lines[:i], lines[i:]
    normalized = '\n'.join([ln for ln in header if not _VOLATILE.match(ln)] + rest)
    j = 0
    while j < len(rest) and (_BODY_NOTICE.match(rest[j]) or (j > 0 and not rest[j] and _BODY_NOTICE.match(rest[j - 1]))):
        j += 1
    truncated = bool(TRUNC_MARK.search(text or '')) or any(_BODY_NOTICE.match(ln) and ln.startswith('Warning')
                                                           for ln in rest[:3])
    return OutputView(normalized=normalized, body='\n'.join(rest[j:]), exit_code=exit_code,
                      truncated=truncated, sha256=C.sha256_text(normalized))


def observable_text(body: str) -> str:
    """Body with every line touched by a truncation marker removed (a number cut in half by
    Codex's head/tail truncation must not become an observation)."""
    if not TRUNC_MARK.search(body):
        return body
    return '\n'.join(ln for ln in body.split('\n') if not TRUNC_MARK.search(ln))


def message_text(item: dict) -> str:
    if item.get('type') != 'message':
        return ''
    return ''.join(str(p.get('text') or '') for p in item.get('content') or [] if isinstance(p, dict))


def is_compaction_request(items: Sequence[dict], tools: Sequence | None = None) -> bool:
    """Codex's tool-less context-checkpoint request (v8: 119/119)."""
    if tools is not None and len(tools) != 0:
        return False
    last = items[-1] if items else None
    return bool(isinstance(last, dict) and last.get('role') == 'user'
                and message_text(last).startswith(COMPACTION_PROMPT))


def resume_items(items: Sequence[dict]) -> list[dict]:
    return [it for it in items if isinstance(it, dict) and it.get('role') == 'user'
            and message_text(it).startswith(RESUME_PREFIX)]

# ---------------------------------------------------------------------------------------------
# calc frames (contracts.CALC_OPEN / CALC_RESULT / CALC_END)
# ---------------------------------------------------------------------------------------------

_FRAME = re.compile(re.escape(C.CALC_OPEN) + r'(?P<sha>[0-9a-f]{12})⟧\n(?P<code>.*?)\n' + re.escape(C.CALC_RESULT)
                    + r'\n(?P<out>.*?)\n?' + re.escape(C.CALC_END), re.S)


def parse_calc_frames(text: str) -> tuple[list[dict], str]:
    """Complete calc frames in `text` and the text with those frames removed (a frame cut by
    truncation is left in the text and yields no compute)."""
    frames, pieces, pos = [], [], 0
    for m in _FRAME.finditer(text):
        code = m.group('code')
        frames.append({'sha12': m.group('sha'), 'code': code, 'output': m.group('out'),
                       'complete': C.sha256_text(code)[:12] == m.group('sha')})
        pieces.append(text[pos:m.start()])
        pos = m.end()
    pieces.append(text[pos:])
    return frames, '\n'.join(pieces)

# ---------------------------------------------------------------------------------------------
# Reading ledgers back
# ---------------------------------------------------------------------------------------------


def fold_lines(lines: Iterable[str]) -> list[C.Event]:
    order: list[str] = []
    latest: dict[str, C.Event] = {}
    for line in lines:
        if not line.strip():
            continue
        event = C.event_from_json(line)
        if event.ref not in latest:
            order.append(event.ref)
        latest[event.ref] = event
    return [latest[ref] for ref in order]


def load_events(path: str | os.PathLike, upto_lines: int | None = None) -> list[C.Event]:
    """Events of a ledger.jsonl, folded by ref; `upto_lines` reproduces the state after that many
    lines (the value a gate check recorded)."""
    with open(path, encoding='utf-8') as fh:
        lines = fh.read().split('\n')
    if lines and lines[-1] == '':
        lines.pop()
    return fold_lines(lines[:upto_lines] if upto_lines is not None else lines)


def ledger_prefix_sha(path: str | os.PathLike, lines: int) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for i, line in enumerate(fh):
            if i >= lines:
                break
            h.update(line)
    return h.hexdigest()

# ---------------------------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------------------------


class Ledger:
    """contracts.LedgerAPI over relayed Responses items. Thread-safe (the broker relays on one thread
    while /v1/submit arrives on another). `out_path=None` keeps it in memory (replay, tests)."""

    def __init__(self, pack: C.Pack, out_path: str | os.PathLike | None, *, max_obs_per_output: int | None = None,
                 max_code_bytes: int = 8192, max_result_bytes: int = 2048) -> None:
        """`max_obs_per_output`: observations kept per tool output; None = the pack's
        `max_observations` attribute, else DEFAULT_MAX_OBSERVATIONS (400)."""
        self.pack = pack
        declared = getattr(pack, 'max_observations', None)
        if max_obs_per_output is None:
            max_obs_per_output = declared if isinstance(declared, int) and not isinstance(declared, bool) \
                and declared > 0 else DEFAULT_MAX_OBSERVATIONS
        self.max_obs_per_output = int(max_obs_per_output)
        self.pack_handles_truncation = getattr(pack, 'handles_truncation', False) is True
        self.pack_takes_raw_command = _accepts_keyword(getattr(pack, 'observations', None), 'raw_command')
        self.max_code_bytes, self.max_result_bytes = max_code_bytes, max_result_bytes
        self.errors: list[str] = []            # pack/parse failures (never fatal; counted in reports)
        self._lock = threading.RLock()
        self._turn = threading.Condition(self._lock)   # commits ingest_request calls in ticket order
        self._tickets = 0                      # ingest_request calls started
        self._committed = 0                    # ingest_request calls committed
        self._events: list[C.Event] = []
        self._pos: dict[str, int] = {}
        self._seq: Counter = Counter()
        self._counts: Counter = Counter()      # normalised cmd -> times issued (survives compaction)
        self._pairs: Counter = Counter()       # (cmd, output sha) -> times seen
        self._by_call: dict[str, str] = {}     # call_id -> command ref
        self._outputs_done: set[str] = set()
        self._seen_output_sha: set[str] = set()
        self._seen_frames: set[tuple[str, str]] = set()
        self._harness_texts: list[str] = []
        self._resumes: set[str] = set()
        self._pending_compaction: int | None = None
        self.compaction_requests: list[int] = []
        self._index = ValueIndex()
        self._lines = 0
        self._hash = hashlib.sha256()
        self._fh = None
        if out_path is not None:
            path = Path(out_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            # Fresh file only, never through a symlink: the ledger is written, never read, here.
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
            self._fh = os.fdopen(fd, 'wb')
        self.out_path = out_path

    # -- LedgerAPI -------------------------------------------------------------------------------

    @property
    def events(self) -> Sequence[C.Event]:
        with self._lock:
            return tuple(self._events)

    def snapshot(self) -> tuple[list[C.Event], int, str]:
        """(events, lines written, sha256 of those lines), atomically: the gate's recorded input."""
        with self._lock:
            return list(self._events), self._lines, self._hash.copy().hexdigest()

    @property
    def lines(self) -> int:
        return self._lines

    def command_count(self, cmd: str) -> int:
        with self._lock:
            return self._counts[normalize_command(cmd)]

    def command_output_count(self, cmd: str, output_sha256: str) -> int:
        """How often this command returned this exact normalised output (loop guards may require it)."""
        with self._lock:
            return self._pairs[(normalize_command(cmd), output_sha256)]

    def candidates(self) -> list[C.Candidate]:
        with self._lock:
            return [e for e in self._events if isinstance(e, C.Candidate)]

    def next_ref(self, kind: str) -> str:
        with self._lock:
            prefix = REF_PREFIX[kind]
            self._seq[prefix] += 1
            return f'{prefix}{self._seq[prefix]}'

    def add(self, event: C.Event) -> C.Event:
        """Append a trusted event (candidates, notes). Missing refs are assigned; a ref already in
        use is refused. Gate notes are remembered so their text never becomes an observation."""
        if not isinstance(event, C.Event) or event.kind not in REF_PREFIX:
            raise TypeError('ledger events must be contracts.Event instances of a known kind')
        with self._lock:
            if not event.ref:
                event = dataclasses.replace(event, ref=self.next_ref(event.kind))
            elif event.ref in self._pos:
                raise ValueError(f'duplicate ledger ref {event.ref}')
            else:
                self._bump_seq(event.ref)
            if isinstance(event, C.Note) and event.text:
                self._harness_texts.append(event.text)
            self._append(event)
            if isinstance(event, C.Compute):              # e.g. the gate's offer run: linkable later
                for v in event.values:
                    self._index.add(v, event.ref)
            elif isinstance(event, C.Observe):
                self._index.add(event.value, event.ref)
                for v in (event.attrs or {}).get(C.VALUES_ATTR) or []:
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        self._index.add(float(v), event.ref)
            return event

    def ingest_response(self, call: int, response: dict, t: float) -> None:
        with self._lock:
            for item in (response or {}).get('output') or []:
                if isinstance(item, dict) and item.get('type') in CALL_TYPES:
                    self._command(item, call, t)

    def ingest_request(self, call: int, codex_input: Sequence[dict], t: float, *,
                       tools: Sequence | None = None) -> None:
        """Three phases (see the module docstring): (1) under the lock, record calls and output
        completions and cut each new output into pack work; (2) outside the lock, run the pure pack
        re-derivation; (3) under the lock, in ticket order, append observations, computes and
        compaction events."""
        items = [it for it in (codex_input or []) if isinstance(it, dict)]
        error: BaseException | None = None
        with self._lock:
            ticket = self._tickets
            self._tickets += 1
            try:
                work = self._plan(items, call, t)
            except BaseException as exc:                    # still commit the ticket, then re-raise
                work, error = [], exc
        try:
            for unit in work:
                self._derive(unit, call, t)
        except BaseException as exc:                        # _derive never raises Exception; this is
            error = exc                                     # KeyboardInterrupt & co: release the turn
        with self._turn:
            while self._committed != ticket:
                self._turn.wait()
            try:
                if error is None:
                    for unit in work:
                        self._commit_unit(unit, call, t)
                    self._compactions(items, call, t, tools)
            finally:
                self._committed += 1
                self._turn.notify_all()
        if error is not None:
            raise error

    def render(self, max_bytes: int) -> str:
        """Deterministic splice text: candidates, computes (latest first) with their linked inputs,
        the observations those computes cite, then the most recent observations. ≤ max_bytes UTF-8."""
        with self._lock:
            events = list(self._events)
        by_ref = {e.ref: e for e in events}
        cands = [e for e in events if isinstance(e, C.Candidate)]
        comps = [e for e in events if isinstance(e, C.Compute)][::-1]
        obs = [e for e in events if isinstance(e, C.Observe)][::-1]
        n_cmd = sum(1 for e in events if isinstance(e, C.Command))
        n_comp = sum(1 for e in events if isinstance(e, C.Compaction))
        head = [f'Ledger (recorded by the harness from tool outputs; authoritative where a summary '
                f'disagrees). Commands run: {n_cmd}; compactions: {n_comp}.']
        body: list[str] = []
        if cands:
            body.append('Candidates:')
            for c in cands:
                extra = ''.join([f' alt "{_clip(c.alt, 40)}"' if c.alt else '',
                                 f' kept ({_clip(c.keep, 40)})' if c.keep else ''])
                body.append(f'- {c.ref} "{_clip(c.answer or c.artifact_sha256, 80)}" via {c.via} (call {c.call}){extra}')
        cited: list[str] = []
        if comps:
            body.append('Calc results (latest first):')
            for k in comps[:12]:
                ins = []
                for inp in k.inputs:
                    if inp.get('trivial'):
                        continue
                    src = inp.get('observe')
                    if src and src not in cited:
                        cited.append(src)
                    ins.append(f"{inp.get('literal')}={src or 'unmatched'}")
                body.append(f'- {k.ref} sha={k.code_sha256[:12]} -> {_clip(" ".join(k.output.split()), 80)}'
                            + (f" | inputs: {', '.join(ins[:8])}" if ins else ''))
        cited_obs = [by_ref[r] for r in cited if isinstance(by_ref.get(r), C.Observe)]
        if cited_obs:
            body.append('Cited values:')
            body += [_obs_line(o) for o in cited_obs[:20]]
        recent = [o for o in obs if o.ref not in cited][:20]
        if recent:
            body.append('Recent values (latest first):')
            body += [_obs_line(o) for o in recent]
        return _fit(head + body, max_bytes)

    def close(self) -> None:
        with self._lock:
            if self._fh:
                self._fh.close()
                self._fh = None

    # -- internals -------------------------------------------------------------------------------

    def _bump_seq(self, ref: str) -> None:
        m = re.match(r'^([a-z])(\d+)$', ref)
        if m:
            self._seq[m.group(1)] = max(self._seq[m.group(1)], int(m.group(2)))

    def _append(self, event: C.Event) -> None:
        self._pos[event.ref] = len(self._events)
        self._events.append(event)
        self._write(event)

    def _replace(self, event: C.Event) -> None:
        self._events[self._pos[event.ref]] = event
        self._write(event)

    def _write(self, event: C.Event) -> None:
        try:
            line = (event.to_json() + '\n').encode('utf-8')
        except UnicodeEncodeError:        # lone surrogates (e.g. from a JSON '\udc00' escape): ASCII-escaped JSON
            line = (json.dumps(dataclasses.asdict(event), ensure_ascii=True, sort_keys=True) + '\n').encode('ascii')
        self._hash.update(line)
        self._lines += 1
        if self._fh:
            self._fh.write(line)          # binary: the file bytes are exactly the hashed bytes
            self._fh.flush()

    def _command(self, item: dict, call: int, t: float) -> None:
        call_id = str(item.get('call_id') or item.get('id') or '')
        if not call_id or call_id in self._by_call:
            return
        cmd = command_text(item)
        self._counts[cmd] += 1
        event = C.Command(kind='command', ref=self.next_ref('command'), call=call, t=t, cmd=cmd,
                          raw_cmd=clip_utf8(raw_command_text(item), RAW_CMD_MAX), tool=call_tool_name(item),
                          call_id=call_id, n=self._counts[cmd])
        self._by_call[call_id] = event.ref
        self._append(event)

    def _plan(self, items: list[dict], call: int, t: float) -> list[dict]:
        """Phase 1 (lock held): record calls and output completions in item order; return one work
        unit per new, not-yet-seen output (the text the pack gets, calc frames, the source command)."""
        work = []
        for item in items:
            kind = item.get('type')
            if kind in CALL_TYPES:                         # fallback: a call whose response we never saw
                self._command(item, call, t)
            elif kind in OUTPUT_TYPES:
                unit = self._output(item, call, t)
                if unit is not None:
                    work.append(unit)
        return work

    def _output(self, item: dict, call: int, t: float) -> dict | None:
        call_id = str(item.get('call_id') or '')
        if not call_id or call_id in self._outputs_done:
            return None
        self._outputs_done.add(call_id)
        view = normalize_output(output_text(item))
        ref = self._by_call.get(call_id)
        if ref is None:                                   # output of a call never relayed to us
            self._command({'type': 'function_call', 'call_id': call_id, 'name': '?', 'arguments': '{}'}, call, t)
            ref = self._by_call[call_id]
        cmd_event = self._events[self._pos[ref]]
        self._replace(dataclasses.replace(cmd_event, exit_code=view.exit_code, output_sha256=view.sha256,
                                          output_bytes=len(view.normalized.encode('utf-8')),
                                          truncated=view.truncated))
        self._pairs[(cmd_event.cmd, view.sha256)] += 1
        if view.sha256 in self._seen_output_sha:          # identical output seen before: nothing new
            return None
        self._seen_output_sha.add(view.sha256)
        body = view.body
        for text in self._harness_texts:                  # the gate's own words are not evidence
            if text and text in body:
                body = body.replace(text, '\n')
        frames, rest = parse_calc_frames(body)
        # Packs that handle Codex's truncation markers get the body as the model saw it (S2-review 1);
        # the core raw path drops every line a marker touched.
        text = rest if self.pack_handles_truncation else observable_text(rest)
        return {'source': ref, 'cmd': cmd_event.cmd, 'raw_cmd': cmd_event.raw_cmd, 'text': text,
                'frames': frames, 'found': [], 'errors': []}

    def _derive(self, unit: dict, call: int, t: float) -> None:
        """Phase 2 (no lock): the pack's pure re-derivation. Refs are provisional ('?<n>'); the
        commit assigns the real ones. Never raises (errors are kept on the unit)."""
        if not unit['text'].strip():
            return
        counter = iter(range(1, 1 << 62))
        kwargs = {'raw_command': unit['raw_cmd']} if self.pack_takes_raw_command else {}
        try:
            unit['found'] = list(self.pack.observations(unit['cmd'], unit['text'], source=unit['source'],
                                                        call=call, t=t,
                                                        next_ref=lambda: f'{_PROVISIONAL}{next(counter)}',
                                                        **kwargs))
        except Exception as exc:  # a pack bug must not break the relay
            unit['errors'].append(f"observations {unit['source']}: {type(exc).__name__}: {exc}"[:300])

    def _commit_unit(self, unit: dict, call: int, t: float) -> None:
        """Phase 3 (lock held, ticket order): observations (capped per output), then calc computes."""
        self.errors.extend(unit['errors'])
        source = unit['source']
        for obs in unit['found'][:self.max_obs_per_output]:
            if not isinstance(obs, C.Observe):
                self.errors.append(f'observations {source}: non-Observe item dropped')
                continue
            if not isinstance(obs.ref, str) or not obs.ref or obs.ref.startswith(_PROVISIONAL) or obs.ref in self._pos:
                obs = dataclasses.replace(obs, ref=self.next_ref('observe'))
            else:
                self._bump_seq(obs.ref)
            obs = dataclasses.replace(obs, kind='observe', source=obs.source or source, call=call, t=t,
                                      text=str(obs.text)[:MAX_OBS_TEXT])
            self._append(obs)
            self._index.add(obs.value, obs.ref)
            for v in (obs.attrs or {}).get(C.VALUES_ATTR) or []:
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    self._index.add(float(v), obs.ref)
        for frame in unit['frames']:
            self._compute(frame, source, call, t)

    def _compactions(self, items: list[dict], call: int, t: float, tools: Sequence | None) -> None:
        for item in resume_items(items):
            key = f"{item.get('id') or ''}:{C.sha256_text(message_text(item))}"
            if key not in self._resumes:
                self._resumes.add(key)
                self._append(C.Compaction(kind='compaction', ref=self.next_ref('compaction'), call=call, t=t,
                                          request_call=self._pending_compaction or 0, summary_call=call))
                self._pending_compaction = None
        if is_compaction_request(items, tools):
            # A Compaction event is written when the resume arrives (both calls known); a retried
            # compaction request just moves the pending call. `compaction_requests` keeps them all.
            self.compaction_requests.append(call)
            self._pending_compaction = call

    def _compute(self, frame: dict, source: str, call: int, t: float) -> None:
        code = frame['code'][:self.max_code_bytes * 2]
        output = frame['output'][:self.max_result_bytes * 2]
        key = (frame['sha12'], output)
        if key in self._seen_frames:
            return
        self._seen_frames.add(key)
        full_sha = C.sha256_text(code)
        # Lines touched by Codex truncation: the marker's count is no literal, and the numbers beside it
        # may be halves of cut numbers.
        cut = [(code.rfind('\n', 0, m.start()) + 1, (code.find('\n', m.end()) + 1 or len(code) + 1) - 1)
               for m in TRUNC_MARK.finditer(code)]
        inputs = []
        for lit in code_literals(code):
            if any(lit.start < e and s < lit.end for s, e in cut):
                continue
            entry: dict[str, Any] = {'literal': lit.text, 'value': lit.value, 'span': [lit.start, lit.end],
                                     'observe': None, 'match': None}
            if lit.in_string:
                entry['in_string'] = True
            if lit.trivial:
                entry['trivial'] = True
            else:
                link = self._index.link(lit.value, lit.decimals, lit.sig)
                if link:
                    entry.update(observe=link['ref'], match=link['match'])
                    if link['scale']:
                        entry['scale'] = link['scale']
            inputs.append(entry)
        values = numbers_in(observable_text(output))      # a number cut by Codex truncation is no output
        event = C.Compute(kind='compute', ref=self.next_ref('compute'), call=call, t=t, source=source, code=code,
                          code_sha256=full_sha if frame['complete'] else frame['sha12'], output=output,
                          values=values, inputs=inputs)
        self._append(event)
        for v in values:
            self._index.add(v, event.ref)


def _clip(text: str, n: int) -> str:
    text = ' '.join(str(text or '').split())
    return text if len(text) <= n else text[:n - 1] + '…'


def _obs_line(o: C.Observe) -> str:
    return f'- {o.ref} {_clip(o.locator, 60)} | {_clip(o.text, 100)}'


def _fit(lines: list[str], max_bytes: int) -> str:
    out: list[str] = []
    used = 0
    for i, line in enumerate(lines):
        size = len(line.encode('utf-8')) + (1 if out else 0)
        if used + size > max_bytes:
            more = f'… ({len(lines) - i} more lines omitted)'
            if used + len(more.encode('utf-8')) + 1 <= max_bytes:
                out.append(more)
            break
        out.append(line)
        used += size
    text = '\n'.join(out)
    while len(text.encode('utf-8')) > max_bytes:     # a single over-long first line
        text = text[:-1]
    return text
