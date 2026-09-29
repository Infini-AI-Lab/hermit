"""v10 L2 ledger (harness_core/ledger.py): normalisation, counts across compaction, observations,
calc frames and input linking, persistence/fold, render, safety, and the v10 parser modes + observation
coverage (DESIGN §3) over the real OfficeQA pack on a synthetic corpus. Offline, no network.

The slow test (opt-in, tests/conftest.py: -m slow or V9_SLOW=1; V9_SLOW=all = every v8 run) replays
recorded v8 broker traffic read-only through the ledger.
"""
import glob
import json
import os
from pathlib import Path
import sys
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
from harness_core import contracts as C  # noqa: E402
from harness_core import ledger as L  # noqa: E402
from harness_core.observe import RawPack  # noqa: E402
from harness_core.tools import calc as calc_tool  # noqa: E402
from packs.toy import PACK as TOY  # noqa: E402
from packs.toy.sim import codex_output, run_item  # noqa: E402

PROJECT = ROOT.parent
TOOLS = [{'type': 'function', 'name': 'exec_command'}]


def fc(call_id, cmd, name='exec_command', **extra):
    args = {'cmd': cmd, 'workdir': '/ws', **extra} if name == 'exec_command' else extra
    return {'type': 'function_call', 'id': 'fc_' + call_id, 'call_id': call_id, 'name': name,
            'arguments': json.dumps(args)}


def fco(call_id, text, n=0, rc=0):
    return {'type': 'function_call_output', 'id': 'fco_' + call_id, 'call_id': call_id,
            'output': codex_output(text, rc, n)}


def user(text, id=None):
    item = {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': text}]}
    if id:
        item['id'] = id
    return item


def calc_output(code):
    code = calc_tool.normalise(code)
    rc, out, err = calc_tool.execute(code, timeout=30)
    assert rc == 0, err
    return calc_tool.frame(code, out)


def step(ledger, history, call, cmd, text, *, call_id=None, rc=0):
    """One broker round: request (history) -> response (function_call) -> tool output appended."""
    call_id = call_id or f'c{call}'
    ledger.ingest_request(call, list(history), float(call), tools=TOOLS)
    item = fc(call_id, cmd)
    ledger.ingest_response(call, {'output': [item]}, float(call))
    history += [item, fco(call_id, text, call, rc)]
    return item


def test_normalize_output_strips_volatile_header_and_detects_truncation():
    a = L.normalize_output(codex_output('line 1\n12.5 x', 0, 1))
    b = L.normalize_output(codex_output('line 1\n12.5 x', 0, 2))
    assert 'Chunk ID' not in a.normalized and 'Wall time' not in a.normalized
    assert a.sha256 == b.sha256 and a.body == 'line 1\n12.5 x' and a.exit_code == 0 and not a.truncated
    t = L.normalize_output(codex_output('Warning: truncated output (original token count: 999)\n'
                                        'Total output lines: 40\n\nhead 1\n…321 chars truncated…tail 2', 1, 3))
    assert t.truncated and t.exit_code == 1 and t.body.startswith('head 1')
    assert L.observable_text(t.body) == 'head 1'
    raw = L.normalize_output('exec_command failed: CreateProcess { message: "x" }')
    assert raw.exit_code is None and raw.body.startswith('exec_command failed')


def test_command_text_normalisation():
    assert L.command_text(fc('a', "  sed  -n '1,5p'\n  file.txt ")) == "sed -n '1,5p' file.txt"
    ws = {'type': 'function_call', 'call_id': 'w', 'name': 'write_stdin',
          'arguments': json.dumps({'session_id': 3, 'chars': ''})}
    assert L.command_text(ws) == 'write_stdin {"chars": "", "session_id": 3}'
    shell = {'type': 'function_call', 'call_id': 's', 'name': 'shell',
             'arguments': json.dumps({'command': ['bash', '-lc', 'ls  -la']})}
    assert L.command_text(shell) == 'ls -la'
    bad = {'type': 'function_call', 'call_id': 'b', 'name': 'x', 'arguments': '{not json'}
    assert L.command_text(bad) == 'x {not json'
    custom = {'type': 'custom_tool_call', 'call_id': 'p', 'name': 'apply_patch', 'input': '*** Begin\n x'}
    assert L.command_text(custom) == 'apply_patch *** Begin x'


def test_counts_survive_compaction_and_compaction_is_detected_once():
    led = L.Ledger(RawPack(), None)
    hist = [user('question', 'u1')]
    for call in (1, 2, 3):
        step(led, hist, call, 'cat  notes.txt', 'same output 1')
    assert led.command_count('cat notes.txt') == 3
    # compaction request: whole history + the fixed prompt, no tools
    led.ingest_request(4, hist + [user(L.COMPACTION_PROMPT + '. Create a handoff summary ...')], 4.0, tools=[])
    assert led.compaction_requests == [4]
    # resume: fresh ids, summary message; history then grows again
    hist = [user('question', 'u9'), user(L.RESUME_PREFIX + ' and produced a summary: ...', 'u10')]
    step(led, hist, 5, 'cat notes.txt', 'same output 1')
    step(led, hist, 6, 'ls', 'a b')
    led.ingest_request(7, hist, 7.0, tools=TOOLS)       # resume message seen again: no new event
    comps = [e for e in led.events if isinstance(e, C.Compaction)]
    assert len(comps) == 1 and (comps[0].request_call, comps[0].summary_call) == (4, 5)
    cmds = [e for e in led.events if isinstance(e, C.Command) and e.cmd == 'cat notes.txt']
    assert [c.n for c in cmds] == [1, 2, 3, 4] and led.command_count('cat   notes.txt') == 4
    sha = cmds[0].output_sha256
    assert sha and all(c.output_sha256 == sha for c in cmds)
    assert led.command_output_count('cat notes.txt', sha) == 4


def test_retried_compaction_request_is_not_double_counted():
    led = L.Ledger(RawPack(), None)
    prompt = user(L.COMPACTION_PROMPT + '. ...')
    led.ingest_request(1, [user('q', 'u1'), prompt], 1.0, tools=[])
    led.ingest_request(2, [user('q', 'u1'), prompt], 2.0, tools=[])
    led.ingest_request(3, [user('q', 'u2'), user(L.RESUME_PREFIX + ' x', 'u3')], 3.0, tools=TOOLS)
    comps = [e for e in led.events if isinstance(e, C.Compaction)]
    assert led.compaction_requests == [1, 2] and len(comps) == 1 and comps[0].request_call == 2
    # a request with tools is never a compaction request, whatever its last message says
    assert not L.is_compaction_request([prompt], tools=TOOLS)


def test_output_completion_is_a_second_line_and_fold_reproduces_every_prefix(tmp_path):
    path = tmp_path / 'api' / 'ledger.jsonl'
    led = L.Ledger(RawPack(), path)
    hist = [user('q', 'u1')]
    step(led, hist, 1, 'echo 12.5', '12.5', rc=0)
    assert led.lines == 1                                    # command issued, output not yet seen
    led.ingest_request(2, hist, 2.0, tools=TOOLS)
    lines = path.read_text().splitlines()
    assert json.loads(lines[0])['ref'] == json.loads(lines[1])['ref'] == 'x1'
    assert json.loads(lines[0])['output_sha256'] == '' and json.loads(lines[1])['exit_code'] == 0
    folded = L.load_events(path)
    assert [e.ref for e in folded] == [e.ref for e in led.events]
    assert folded[0].output_sha256 == led.events[0].output_sha256
    first = L.load_events(path, upto_lines=1)
    assert len(first) == 1 and first[0].output_sha256 == ''
    events, n, sha = led.snapshot()
    assert n == len(lines) and sha == L.ledger_prefix_sha(path, n)


def test_raw_observations_and_identical_output_dedupe():
    led = L.Ledger(RawPack(), None)
    hist = [user('q', 'u1')]
    step(led, hist, 1, 'rg total', 'Total receipts | 1,234.5 | 99\nno numbers here')
    step(led, hist, 2, 'rg total', 'Total receipts | 1,234.5 | 99\nno numbers here')   # new chunk id
    led.ingest_request(3, hist, 3.0, tools=TOOLS)
    obs = [e for e in led.events if isinstance(e, C.Observe)]
    assert len(obs) == 1 and obs[0].attrs[C.VALUES_ATTR] == [1234.5, 99.0] and obs[0].source == 'x1'
    assert obs[0].locator == 'x1:L1'


def test_toy_tags_become_attributed_observations():
    led = L.Ledger(TOY, None)
    hist = [user('q', 'u1')]
    step(led, hist, 1, 'lookup widgets_2020_b.txt North', '⟦toy widgets_2020_b.txt:5|Q2 2020|North|131.5r⟧\nfree 7.25')
    led.ingest_request(2, hist, 2.0, tools=TOOLS)
    obs = [e for e in led.events if isinstance(e, C.Observe)]
    tagged = [o for o in obs if 'toy.period' in o.attrs]
    assert len(tagged) == 1 and tagged[0].value == 131.5 and tagged[0].attrs['toy.printing'] == 'b'
    assert tagged[0].attrs['toy.revised'] is True and tagged[0].locator == 'widgets_2020_b.txt:5'
    assert any(o.attrs.get(C.VALUES_ATTR) == [7.25] for o in obs)


def test_truncated_lines_are_not_observed():
    led = L.Ledger(RawPack(), None)
    hist = [user('q', 'u1')]
    step(led, hist, 1, 'cat big', 'keep 11.5\ncut 123…4000 chars truncated…45 tail\nalso 22.5')
    led.ingest_request(2, hist, 2.0, tools=TOOLS)
    values = sorted(v for e in led.events if isinstance(e, C.Observe) for v in e.attrs[C.VALUES_ATTR])
    assert values == [11.5, 22.5]
    assert [e for e in led.events if isinstance(e, C.Command)][0].truncated


def test_calc_frames_become_computes_with_linked_inputs():
    led = L.Ledger(RawPack(), None)
    hist = [user('q', 'u1')]
    step(led, hist, 1, 'rg x', 'row A | 1234.567 | 88.4')
    code = 'a = 1234.567\nb = 1234.57\nc = 1.234567\nd = 99.99\nprint(round((a + b) / 2 * 100, 2))\nprint(c, d, 2)'
    step(led, hist, 2, 'calc -c ...', calc_output(code))
    step(led, hist, 3, 'calc -c ...', calc_output('print(123456.85 / 1000)'))   # reuses k1's printed value
    led.ingest_request(4, hist, 4.0, tools=TOOLS)
    k1, k2 = [e for e in led.events if isinstance(e, C.Compute)]
    by_lit = {i['literal']: i for i in k1.inputs}
    assert by_lit['1234.567']['match'] == 'exact' and by_lit['1234.567']['observe'] == 'c1'
    assert by_lit['1234.57']['match'] == 'rounded'
    assert by_lit['1.234567']['match'] == 'scaled' and by_lit['1.234567']['scale'] == -3
    assert by_lit['99.99']['observe'] is None and by_lit['99.99']['match'] is None
    assert by_lit['100'].get('trivial') and by_lit['2'].get('trivial')
    assert k1.values[0] == pytest.approx(123456.85) and k1.code_sha256 == C.sha256_text(k1.code)
    assert k1.inputs[0]['span'] == [4, 12] and k1.code[4:12] == '1234.567'
    assert k2.inputs[0]['observe'] == 'k1' and k2.inputs[0]['match'] == 'exact'
    # calc numbers never become observations (no self-grounding)
    assert not any(isinstance(e, C.Observe) and 99.99 in e.attrs.get(C.VALUES_ATTR, []) for e in led.events)


def test_truncated_calc_code_is_marked_incomplete():
    led = L.Ledger(RawPack(), None)
    hist = [user('q', 'u1')]
    code = 'x = 1\n' + '# pad\n' * 2000 + 'print(x)'
    step(led, hist, 1, 'calc big.py', calc_output(code))
    led.ingest_request(2, hist, 2.0, tools=TOOLS)
    (k,) = [e for e in led.events if isinstance(e, C.Compute)]
    assert len(k.code_sha256) == 12 and k.values == [1.0]


def test_gate_text_is_never_evidence():
    led = L.Ledger(RawPack(), None)
    hist = [user('q', 'u1')]
    item = fc('s1', 'submit --answer 12.5; rg total')
    led.ingest_request(1, hist, 1.0, tools=TOOLS)
    led.ingest_response(1, {'output': [item]}, 1.0)
    note = led.add(C.Note(kind='note', ref='', call=1, t=1.0, reason='gate', sticky=False,
                          text='[runtime:gate] recorded "12.5". End your turn with exactly this answer.'))
    assert note.ref == 'n1'
    hist += [item, fco('s1', note.text + '\nTotal | 7.25', 1)]
    led.ingest_request(2, hist, 2.0, tools=TOOLS)
    values = [v for e in led.events if isinstance(e, C.Observe) for v in e.attrs[C.VALUES_ATTR]]
    assert values == [7.25]


def test_add_assigns_refs_rejects_duplicates_and_types():
    led = L.Ledger(RawPack(), None)
    a = led.add(C.Candidate(kind='candidate', ref='', call=1, t=0.0, answer='1'))
    b = led.add(C.Candidate(kind='candidate', ref='a7', call=1, t=0.0, answer='2'))
    c = led.add(C.Candidate(kind='candidate', ref='', call=1, t=0.0, answer='3'))
    assert (a.ref, b.ref, c.ref) == ('a1', 'a7', 'a8') and [x.answer for x in led.candidates()] == ['1', '2', '3']
    with pytest.raises(ValueError):
        led.add(C.Candidate(kind='candidate', ref='a7', call=1, t=0.0))
    with pytest.raises(TypeError):
        led.add({'kind': 'note'})


def test_refuses_existing_file_and_symlink(tmp_path):
    existing = tmp_path / 'ledger.jsonl'
    existing.write_text('x\n')
    with pytest.raises(FileExistsError):
        L.Ledger(RawPack(), existing)
    target = tmp_path / 'elsewhere.txt'
    link = tmp_path / 'link.jsonl'
    link.symlink_to(target)
    with pytest.raises(OSError):
        L.Ledger(RawPack(), link)
    assert not target.exists()


def test_pack_failure_is_contained():
    class Broken(RawPack):
        def observations(self, *a, **k):
            raise RuntimeError('boom')
    led = L.Ledger(Broken(), None)
    hist = [user('q', 'u1')]
    step(led, hist, 1, 'cat f', '1.5 2.5')
    led.ingest_request(2, hist, 2.0, tools=TOOLS)
    assert led.errors and 'boom' in led.errors[0]
    assert [e.kind for e in led.events] == ['command']


def test_render_is_deterministic_and_bounded():
    from harness_core.gate import Gate
    from harness_core.rules import core_seed_rules
    from packs.toy.pack import toy_seed_rules

    class Runner:
        def run(self, code, *, timeout_s=5.0):
            rc, out, _ = calc_tool.execute(code, timeout=30)
            return rc == 0, out
    led = L.Ledger(TOY, None)
    gate = Gate(TOY, core_seed_rules() + toy_seed_rules(), {}, Runner(), 'q')
    run_item('toy07', led, gate)
    text = led.render(2000)
    assert text == led.render(2000) and len(text.encode()) <= 2000
    assert 'Candidates:' in text and 'Calc results' in text and 'Cited values:' in text
    for limit in (10, 100, 300):
        assert len(led.render(limit).encode()) <= limit


def test_concurrent_adds_and_ingest_keep_refs_unique(tmp_path):
    led = L.Ledger(RawPack(), tmp_path / 'l.jsonl')
    hist = [user('q', 'u1')]

    def adder():
        for i in range(50):
            led.add(C.Note(kind='note', ref='', call=0, t=0.0, text=f'n{i}', sticky=False))
    threads = [threading.Thread(target=adder) for _ in range(4)]
    for th in threads:
        th.start()
    for call in range(1, 30):
        step(led, hist, call, f'echo {call}', f'value {call}.5')
    for th in threads:
        th.join()
    refs = [e.ref for e in led.events]
    assert len(refs) == len(set(refs))
    assert len((tmp_path / 'l.jsonl').read_text().splitlines()) == led.lines

# ---------------------------------------------------------------------------------------------
# Offline check on recorded v8 traffic (read-only; slow: V9_SLOW=1)
# ---------------------------------------------------------------------------------------------

V8_ROOTS = {'none': 'officeqa-pro133-none-v8-work/runs/baseline', 'prompt': 'officeqa-pro133-prompt-v8-work/runs/adapted',
            'cards': 'officeqa-pro133-cards-v8-work/runs/adapted'}
V8_SAMPLES = [('none', V8_ROOTS['none'] + '/072'), ('none', V8_ROOTS['none'] + '/053'),
              ('prompt', V8_ROOTS['prompt'] + '/071'), ('cards', V8_ROOTS['cards'] + '/100')]
if os.environ.get('V9_SLOW') == 'all':          # every recorded v8 run (399, ~1 min)
    V8_SAMPLES = [(arm, str(Path(p).relative_to(PROJECT))) for arm, rel in V8_ROOTS.items()
                  for p in sorted(glob.glob(str(PROJECT / rel / '[0-9][0-9][0-9]')))]


def _v8_expectations(run):
    """Independent counts from the raw records: function calls in responses, first-seen spawn
    failures, command_execution starts in events.jsonl, tool-less requests."""
    calls, fails, seen, tool_less = set(), 0, set(), 0
    for d in sorted(glob.glob(os.path.join(run, 'api', '[0-9][0-9][0-9]'))):
        with open(os.path.join(d, 'request.json')) as fh:
            req = json.load(fh)
        if req.get('tools') == []:
            tool_less += 1
        for it in req.get('input') or []:
            if it.get('type') == 'function_call_output' and it['call_id'] not in seen:
                seen.add(it['call_id'])
                fails += str(it.get('output', '')).startswith('exec_command failed')
        try:
            with open(os.path.join(d, 'response.json')) as fh:
                resp = json.load(fh)
        except (OSError, ValueError):
            continue
        for it in resp.get('output') or []:
            if it.get('type') == 'function_call':
                calls.add((it['call_id'], it.get('name')))
    started = 0
    with open(os.path.join(run, 'records', 'events.jsonl')) as fh:
        for line in fh:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if (ev.get('item') or {}).get('type') == 'command_execution' and ev.get('type') == 'item.started':
                started += 1
    return calls, fails, started, tool_less


@pytest.mark.slow
@pytest.mark.parametrize('arm,rel', V8_SAMPLES)
def test_v8_traffic_offline(arm, rel):
    from harness_core.replay import ledger_from_api_records
    run = PROJECT / rel
    if not (run / 'api').is_dir():
        pytest.skip('v8 records not present')
    started = time.perf_counter()
    led, info = ledger_from_api_records(run / 'api', RawPack())
    seconds = time.perf_counter() - started
    calls, fails, exec_started, tool_less = _v8_expectations(run)
    cmds = [e for e in led.events if isinstance(e, C.Command)]
    comps = [e for e in led.events if isinstance(e, C.Compaction)]
    assert not led.errors and not info['errors']
    assert len(cmds) == len(calls)                                            # every issued call, once
    non_exec = sum(1 for _, name in calls if name != 'exec_command')
    assert len(cmds) == exec_started + fails + non_exec                      # events.jsonl agrees
    assert sum(1 for c in cmds if not c.output_sha256) <= 1                  # outputs matched by call_id
    #                                                                         (a call in flight at a timeout)
    assert len(led.compaction_requests) == tool_less
    assert len(comps) in (tool_less, tool_less - 1)                          # last may never resume
    top = max({c.cmd for c in cmds}, key=led.command_count) if cmds else ''
    assert led.command_count(top) == sum(1 for c in cmds if c.cmd == top)
    assert len(led.render(8000).encode()) <= 8000
    print(f'\n{arm} {rel.split("/")[-1]}: calls={info["calls"]} commands={len(cmds)} '
          f'compaction_requests={len(led.compaction_requests)} compactions={len(comps)} '
          f'max_repeat={led.command_count(top)} observations={sum(1 for e in led.events if e.kind == "observe")} '
          f'ledger_seconds={info["seconds"]} total_seconds={seconds:.2f}')


def test_literal_spans_survive_unusual_line_breaks():
    """Review fix: rows are split like tokenize reads them ('\\n' only), not like str.splitlines."""
    from harness_core.numeric import code_literals
    for code in ('# page\x0cbreak\na = 1234.5\nb = 678.25\nprint(a - b)',
                 's = "x y"\na = 1234.5\nprint(a)',
                 'a = 1234.5  # \r old mac\nb = 777.75\nprint(a + b)',
                 'x = float("1,234.5")\x0c\ny = 9.75'):
        lits = [lit for lit in code_literals(code) if not lit.trivial]
        assert lits and all(code[lit.start:lit.end] == lit.text for lit in lits), code


def test_truncated_calc_output_and_code_lines_give_no_values():
    """Review fix: numbers beside a Codex truncation marker are halves of cut numbers."""
    led = L.Ledger(RawPack(), None)
    hist = [user('q', 'u1')]
    code = 'a = 1234.5\nb = 98…40 chars truncated…76.5\nprint(a)'
    body = (f'{C.CALC_OPEN}{"0" * 12}⟧\n{code}\n{C.CALC_RESULT}\n11.25\n12…9 chars truncated…34\n{C.CALC_END}')
    step(led, hist, 1, 'calc -c ...', body)
    led.ingest_request(2, hist, 2.0, tools=TOOLS)
    (k,) = [e for e in led.events if isinstance(e, C.Compute)]
    assert k.values == [11.25]
    assert [i['literal'] for i in k.inputs] == ['1234.5']


def test_lone_surrogates_are_persisted_not_crashing(tmp_path):
    """Review fix: a JSON '\\udc00' escape in model text must not desync memory and file."""
    path = tmp_path / 'ledger.jsonl'
    led = L.Ledger(RawPack(), path)
    led.add(C.Candidate(kind='candidate', ref='', call=1, t=0.0, answer='12.5 \udc00'))
    events, n, sha = led.snapshot()
    assert n == 1 and sha == L.ledger_prefix_sha(path, 1)
    assert L.load_events(path)[0].answer == '12.5 \udc00'

# ---------------------------------------------------------------------------------------------
# wave-2 integration (S7): raw command + tool, per-pack cap, truncation-aware packs, work outside the lock
# ---------------------------------------------------------------------------------------------


class RecordingPack(RawPack):
    """RawPack that takes the v9.1 `raw_command` keyword and records what it was given."""
    def __init__(self, **attrs):
        self.seen = []
        for key, value in attrs.items():
            setattr(self, key, value)

    def observations(self, command, output, *, source, call, t, next_ref, raw_command=''):
        self.seen.append({'command': command, 'output': output, 'raw_command': raw_command, 'source': source})
        return super().observations(command, output, source=source, call=call, t=t, next_ref=next_ref)


def test_command_keeps_raw_text_and_tool_name_and_the_pack_gets_raw_command():
    pack = RecordingPack()
    led = L.Ledger(pack, None)
    hist = [user('q', 'u1')]
    raw = "cd /corpus\nsed -n '10,20p'   f.txt"
    step(led, hist, 1, raw, 'line 10 has 1.5')
    poll = {'type': 'function_call', 'id': 'fc_p', 'call_id': 'p1', 'name': 'write_stdin',
            'arguments': json.dumps({'session_id': 7, 'chars': ''})}
    led.ingest_response(2, {'output': [poll]}, 2.0)
    led.ingest_request(3, hist + [poll, fco('p1', 'still 2.5 running', 3)], 3.0, tools=TOOLS)
    first, second = [e for e in led.events if isinstance(e, C.Command)]
    assert first.cmd == "cd /corpus sed -n '10,20p' f.txt" and first.raw_cmd == raw and first.tool == 'exec_command'
    assert second.tool == 'write_stdin' and second.raw_cmd == 'write_stdin {"chars": "", "session_id": 7}'
    assert pack.seen[0]['command'] == first.cmd and pack.seen[0]['raw_command'] == raw
    # raw text is clipped at 8 KB (UTF-8 safe); a pack without the keyword (v9.0 signature) still works
    led2 = L.Ledger(RawPack(), None)
    assert not led2.pack_takes_raw_command and led.pack_takes_raw_command
    big = 'echo ' + 'é' * 6000
    step(led2, [user('q', 'u1')], 1, big, 'x 1.25')
    command = [e for e in led2.events if isinstance(e, C.Command)][0]
    assert len(command.raw_cmd.encode()) <= L.RAW_CMD_MAX and big.startswith(command.raw_cmd)
    assert [e.kind for e in led2.events] == ['command']        # output not yet relayed
    # both command lines in ledger.jsonl carry raw_cmd/tool (the event is written twice)
    assert C.event_from_json(first.to_json()).raw_cmd == raw


def test_observation_cap_is_per_pack():
    text = '\n'.join(f'row {i} value {i}.5' for i in range(1500))
    counts = {}
    for label, pack, kwargs in (('default', RawPack(), {}), ('declared', RecordingPack(max_observations=1000), {}),
                                ('explicit', RecordingPack(max_observations=1000), {'max_obs_per_output': 50})):
        pack.observations = (lambda p: lambda cmd, out, **kw: [
            C.Observe(kind='observe', ref=kw['next_ref'](), call=kw['call'], t=kw['t'], locator=f'L{i}', text=ln,
                      value=float(i)) for i, ln in enumerate(out.split('\n'))])(pack)
        led = L.Ledger(pack, None, **kwargs)
        step(led, [user('q', 'u1')], 1, 'cat big', text)
        led.ingest_request(2, [user('q', 'u1'), fc('c1', 'cat big'), fco('c1', text, 1)], 2.0, tools=TOOLS)
        counts[label] = sum(1 for e in led.events if isinstance(e, C.Observe))
    assert counts == {'default': L.DEFAULT_MAX_OBSERVATIONS, 'declared': 1000, 'explicit': 50}
    assert L.DEFAULT_MAX_OBSERVATIONS == 400


def test_truncation_markers_reach_only_packs_that_handle_them():
    text = 'keep 11.5\ncut 123…4000 chars truncated…45 tail\nalso 22.5'
    seen = {}
    for aware in (False, True):
        pack = RecordingPack(handles_truncation=aware)
        led = L.Ledger(pack, None)
        hist = [user('q', 'u1')]
        step(led, hist, 1, 'cat big', text)
        led.ingest_request(2, hist, 2.0, tools=TOOLS)
        seen[aware] = pack.seen[0]['output']
        assert [e for e in led.events if isinstance(e, C.Command)][0].truncated
    assert '…4000 chars truncated…' in seen[True] and seen[True].endswith('also 22.5')
    assert seen[False] == 'keep 11.5\nalso 22.5'                # the core raw path drops marked lines
    assert L.Ledger(RecordingPack(handles_truncation='yes'), None).pack_handles_truncation is False   # only True


def test_pack_work_runs_outside_the_lock_and_commits_in_ticket_order():
    """S2-review 3: the pack re-derivation (seconds for pack tools) must not hold the ledger lock; events
    are still appended in the order the ingest calls started."""
    release_first, first_running = threading.Event(), threading.Event()

    class SlowPack(RawPack):
        def observations(self, command, output, *, source, call, t, next_ref, raw_command=''):
            refs = [next_ref(), next_ref()]
            assert all(r.startswith('?') for r in refs)          # provisional until the commit
            if 'first' in output:
                first_running.set()
                assert release_first.wait(10)
            return [C.Observe(kind='observe', ref=refs[0], call=call, t=t, locator=f'{source}:L1', text=output,
                              value=float(call))]
    led = L.Ledger(SlowPack(), None)
    led.ingest_response(1, {'output': [fc('a', 'echo first'), fc('b', 'echo second')]}, 1.0)
    errors = []

    def ingest(call, cid, text):
        try:
            led.ingest_request(call, [fc(cid, 'echo x'), fco(cid, text, call)], float(call), tools=TOOLS)
        except Exception as exc:          # surfaced below
            errors.append(exc)
    one = threading.Thread(target=ingest, args=(2, 'a', 'first 1.5'))
    one.start()
    assert first_running.wait(10)
    # while the first derivation is running: the lock is free for the gate thread ...
    added = led.add(C.Candidate(kind='candidate', ref='', call=2, t=2.0, answer='7', via='submit'))
    events, lines, _ = led.snapshot()
    assert added.ref == 'a1' and lines == len(events) + 1        # x1 written twice (issued, then its output)
    # ... and a later request waits for the earlier one's commit, whatever its own speed
    two = threading.Thread(target=ingest, args=(3, 'b', 'second 2.5'))
    two.start()
    time.sleep(0.3)
    assert not [e for e in led.events if isinstance(e, C.Observe)]
    release_first.set()
    one.join(10)
    two.join(10)
    assert not errors
    obs = [e for e in led.events if isinstance(e, C.Observe)]
    assert [(o.ref, o.source, o.call) for o in obs] == [('c1', 'x1', 2), ('c2', 'x2', 3)]


def test_a_failed_request_still_releases_its_turn(monkeypatch):
    led = L.Ledger(RawPack(), None)
    real = led._output
    calls = {'n': 0}

    def flaky(item, call, t):
        calls['n'] += 1
        if calls['n'] == 1:
            raise RuntimeError('boom in phase 1')
        return real(item, call, t)
    monkeypatch.setattr(led, '_output', flaky)
    hist = [user('q', 'u1')]
    step(led, hist, 1, 'echo 1', 'v 1.5')
    with pytest.raises(RuntimeError, match='boom'):
        led.ingest_request(2, hist, 2.0, tools=TOOLS)
    done = threading.Event()
    threading.Thread(target=lambda: (led.ingest_request(3, hist + [fc('z', 'echo 2'), fco('z', 'w 2.5', 3)], 3.0,
                                                        tools=TOOLS), done.set()), daemon=True).start()
    assert done.wait(10)                                        # no deadlock on the ticket
    assert any(isinstance(e, C.Observe) and e.attrs[C.VALUES_ATTR] == [2.5] for e in led.events)


def test_an_interrupted_derivation_still_releases_its_turn():
    """Review fix: a BaseException (KeyboardInterrupt, SystemExit) in phase 2 used to leave the ticket
    uncommitted, so every later ingest_request waited forever."""
    class Interrupting(RawPack):
        armed = True

        def observations(self, command, output, *, source, call, t, next_ref):
            if Interrupting.armed:
                Interrupting.armed = False
                raise KeyboardInterrupt
            return super().observations(command, output, source=source, call=call, t=t, next_ref=next_ref)
    led = L.Ledger(Interrupting(), None)
    hist = [user('q', 'u1')]
    led.ingest_response(1, {'output': [fc('a', 'echo 1')]}, 1.0)
    with pytest.raises(KeyboardInterrupt):
        led.ingest_request(2, hist + [fc('a', 'echo 1'), fco('a', 'v 1.5', 2)], 2.0, tools=TOOLS)
    done = threading.Event()
    threading.Thread(target=lambda: (led.ingest_request(3, hist + [fc('z', 'echo 2'), fco('z', 'w 2.5', 3)], 3.0,
                                                        tools=TOOLS), done.set()), daemon=True).start()
    assert done.wait(10)                                        # no deadlock on the ticket
    assert any(isinstance(e, C.Observe) and e.attrs[C.VALUES_ATTR] == [2.5] for e in led.events)

# ---------------------------------------------------------------------------------------------
# v10: parser modes, the Observe stamp and observation coverage (DESIGN §3; scope decision 5)
# ---------------------------------------------------------------------------------------------


def observes(led):
    return [e for e in led.events if isinstance(e, C.Observe)]


def test_parser_mode_stamp_and_coverage_with_a_pack_that_ignores_the_mode():
    """RawPack takes no `mode`: every Observe is still stamped, and each command with a body counts once,
    as 'raw' when it produced observations, 'none' otherwise."""
    with pytest.raises(ValueError, match='parser_mode'):
        L.Ledger(RawPack(), None, parser_mode='guess')
    led = L.Ledger(RawPack(), None, parser_mode='header', revision='abc123def456')
    assert not led.pack_takes_mode
    hist = [user('q', 'u1')]
    step(led, hist, 1, 'rg total', 'Total | 1,234.5')
    step(led, hist, 2, 'ls', 'no numbers here')
    step(led, hist, 3, 'true', '')
    led.ingest_request(4, hist, 4.0, tools=TOOLS)
    obs = observes(led)
    assert [o.attrs[L.MODE_ATTR] for o in obs] == ['header'] and obs[0].attrs[L.REVISION_ATTR] == 'abc123def456'
    assert led.observation_coverage() == {'mode': 'header', 'revision': 'abc123def456', 'commands': 2,
                                          'invocation': 0, 'header': 0, 'raw': 1, 'none': 1}
    plain = L.Ledger(RawPack(), None)
    step(plain, [user('q', 'u1')], 1, 'rg total', 'Total | 7')
    plain.ingest_request(2, [user('q', 'u1'), fc('c1', 'rg total'), fco('c1', 'Total | 7', 1)], 2.0, tools=TOOLS)
    assert L.REVISION_ATTR not in observes(plain)[0].attrs and observes(plain)[0].attrs[L.MODE_ATTR] == 'rerun'
    assert L.PARSE_ATTR == 'parse' and L.PARSER_MODES == ('rerun', 'header')


@pytest.fixture(scope='module')
def officeqa(tmp_path_factory):
    """The real OfficeQA pack over the synthetic two-issue corpus and its tables index."""
    import synthetic_officeqa as S
    from packs.officeqa.pack import OfficeQAPack
    base = tmp_path_factory.mktemp('oq')
    corpus = S.make_corpus(base / 'corpus')
    index = S.build_tables_index(corpus, base / 'tables' / 'tables.sqlite')
    pack = OfficeQAPack(corpus, index)
    yield pack, corpus, S.table_header_line(corpus, '2002_03')
    pack.close()


def _run_command(pack, cmd, output, **ledger_options):
    led = L.Ledger(pack, None, **ledger_options)
    hist = [user('q', 'u1')]
    step(led, hist, 1, cmd, output)
    led.ingest_request(2, hist, 2.0, tools=TOOLS)
    return led


def test_officeqa_rerun_vs_header_mode_and_coverage(officeqa):
    """rerun: a parsed invocation is re-run with the W0 tools (parse 'invocation'); header: the same output is
    recovered only from its self-describing first line (parse 'header'); the cells are the same either way,
    every one present verbatim in the output; the coverage counters name the mode that produced them."""
    from packs.officeqa import observe as OBS
    from packs.officeqa.tools import cli
    pack, corpus, header_line = officeqa
    assert OBS.PARSE_ATTR == L.PARSE_ATTR
    fname = 'treasury_bulletin_2002_03.txt'
    args = cli.parser('show_table').parse_args([fname, str(header_line)])
    output = cli.page('show_table', args, pack.tables).text
    assert output.startswith('TABLE ')
    cmd = f'show_table {fname} {header_line}'
    rerun = _run_command(pack, cmd, output, parser_mode='rerun', revision='w0w0w0w0w0w0')
    header = _run_command(pack, cmd, output, parser_mode='header', revision='1234567890ab')
    for led, how in ((rerun, 'invocation'), (header, 'header')):
        obs = observes(led)
        assert obs and {o.attrs[L.PARSE_ATTR] for o in obs} == {how}, how
        assert all(o.attrs[L.MODE_ATTR] == led.parser_mode and o.attrs[L.REVISION_ATTR] == led.revision for o in obs)
        assert led.observation_coverage()[how] == 1 and led.observation_coverage()['commands'] == 1
        assert any(o.value == 1991082.0 and o.locator.startswith(fname) for o in obs), how
    cells = lambda led: sorted((o.locator, o.text, o.value) for o in observes(led))          # noqa: E731
    assert cells(rerun) == cells(header)
    assert not led.errors


def test_officeqa_header_mode_over_a_changed_tool_format_records_only_what_was_seen(officeqa):
    """DESIGN §3: an evolved show_table that prints a different format. The frozen parser raises nothing, the
    unknown first line yields no page, and every observation's text is present verbatim in the output the
    model saw (raw corpus lines and raw numeric lines only; no W0 value under a changed locator)."""
    pack, corpus, header_line = officeqa
    fname = 'treasury_bulletin_2002_03.txt'
    changed = ('Table FFO-1 (Summary of Fiscal Operations) from treasury_bulletin_2002_03.txt\n'
               'FY2001: receipts=1991082 outlays=1862846 surplus=128236\n'
               'FY2002: receipts=1853136 outlays=2010894 deficit=157758\n'
               f'{fname}:8:| 2001 | 1,991,082 | 1,862,846 | 128,236 |\n')
    cmd = f'show_table {fname} {header_line} --wide'
    for mode in ('header', 'rerun'):
        led = _run_command(pack, cmd, changed, parser_mode=mode)
        obs = observes(led)
        assert not led.errors and obs
        assert all(o.text in changed for o in obs), mode
        assert all(o.attrs[L.PARSE_ATTR] == 'raw' for o in obs), mode      # nothing parsed by invocation or header
        coverage = led.observation_coverage()
        assert (coverage['raw'], coverage['invocation'], coverage['header']) == (1, 0, 0)
    # the corpus line with a forged prefix is verified against the corpus text (a 'grep' line), the others raw
    kinds = {o.attrs.get('oq.via') for o in obs}
    assert 'grep' in kinds and 'raw' in kinds


def test_toy_pack_observations_count_as_raw_and_keep_their_attrs():
    led = L.Ledger(TOY, None, parser_mode='rerun', revision='deadbeef0000')
    hist = [user('q', 'u1')]
    step(led, hist, 1, 'lookup widgets_2020_b.txt North', '⟦toy widgets_2020_b.txt:5|Q2 2020|North|131.5r⟧')
    led.ingest_request(2, hist, 2.0, tools=TOOLS)
    (tagged,) = observes(led)
    assert tagged.attrs['toy.period'] == 'Q2 2020' and tagged.attrs[L.REVISION_ATTR] == 'deadbeef0000'
    assert led.observation_coverage()['raw'] == 1
