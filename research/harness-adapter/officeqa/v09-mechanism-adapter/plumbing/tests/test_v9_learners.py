"""v9 learners (S5): evidence builder, shared session/verification/retirement, text and mechanism learners.

Offline and $0: the learner model is tests/fake_learner_model.ScriptedModel, the actor re-runs are a fake
actor_runner, the grader is the toy grader, the corpus is the toy corpus. Episodes are produced by the toy
simulator (packs/toy/sim.py) driven through a recorder that writes the broker's api/NNN records, so the
evidence builder reads exactly the file layout a real campaign root has. Mechanism episodes run through the
real Ledger and Gate (core seed rules), and counterfactual offers run in the real sandbox runner.
"""
import ast
import copy
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import shutil
import sys
import threading

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness_core import contracts as C  # noqa: E402
from harness_core import replay as RP  # noqa: E402
from harness_core.gate import Gate  # noqa: E402
from harness_core.ledger import Ledger  # noqa: E402
from harness_core.rules import core_seed_rules, load_rules, save_rule  # noqa: E402
from harness_core.runner import SandboxRunner  # noqa: E402
from packs.toy import PACK, REFERENCES, grade  # noqa: E402
from packs.toy.corpus import make_corpus  # noqa: E402
from packs.toy.sim import QUESTIONS, run_item  # noqa: E402

import evidence as EV  # noqa: E402
import learner_common as LC  # noqa: E402
import mechanism_learning as ML  # noqa: E402
import text_learning as TL  # noqa: E402
from fake_learner_model import (MODEL, PRIVATE_REASONING, ScriptedModel, calls, runaway, say,  # noqa: E402
                                tool_call)

ITEMS = sorted(QUESTIONS)                                   # toy01 .. toy10
GRADER = EV.Grader(score=lambda reference, answer, tolerance=0.0: grade(answer, reference), name='toy grader')
V8_NONE = ROOT.parent / 'officeqa-pro133-none-v8-work' / 'runs' / 'baseline'
CARDS_BLOCK = ROOT.parent / 'frameworks-v8/next-a48c0f9/harnessadapter/evolvers/cards/block.py'


# ---------------------------------------------------------------------------------------------
# Synthetic campaign roots (v8 assemble_evidence layout + broker api records)
# ---------------------------------------------------------------------------------------------

def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Recorder:
    """Ledger proxy that also writes api/NNN/{request,response,transport}.json as the broker does."""

    def __init__(self, ledger, api):
        self.ledger, self.api = ledger, Path(api)

    def ingest_request(self, call, items, t, *, tools=None):
        save(self.api / f'{call:03}' / 'request.json', {'model': MODEL, 'input': copy.deepcopy(list(items)),
                                                        'tools': list(tools or [])})
        self.ledger.ingest_request(call, items, t, tools=tools)

    def ingest_response(self, call, response, t):
        save(self.api / f'{call:03}' / 'response.json', {'id': f'resp_{call}', 'model': MODEL, 'status': 'completed',
                                                         'output': copy.deepcopy(response.get('output') or [])})
        save(self.api / f'{call:03}' / 'transport.json', {'number': call, 'origin': 'codex', 'forwarded': True})
        self.ledger.ingest_response(call, response, t)

    def __getattr__(self, name):
        return getattr(self.ledger, name)


def make_campaign(root, items, *, mech=False, rules=None, runner=None, block_size=5, notes_served=None):
    root = Path(root)
    episodes = []
    for ordinal, item in enumerate(items, 1):
        run = root / 'runs' / 'adapted' / f'{ordinal:03}'
        api = run / 'api'
        api.mkdir(parents=True)
        if mech:
            ledger = Ledger(PACK, api / 'ledger.jsonl')
            gate = Gate(PACK, rules, {}, runner, QUESTIONS[item], api_dir=api)
        else:
            ledger, gate = Ledger(PACK, None), None
        sim = run_item(item, Recorder(ledger, api), gate)
        tid = f'T{ordinal:03}'
        save(run / 'task.json', {'ordinal': ordinal, 'task_id': tid, 'question': QUESTIONS[item]})
        save(run / 'prediction.json', {'task_id': tid, 'answer': sim.final_text})
        save(run / 'summary.json', {'ordinal': ordinal, 'task_id': tid, 'status': 'completed', 'answer': sim.final_text,
                                    'usage': {'output_tokens': 1000}, 'transport_failed': False,
                                    'records': str(run / 'records')})
        if notes_served is not None:
            save(run / 'workspace' / '.acting-prepared.json', {'extra': {'notes': notes_served.get(ordinal, [])}})
        episodes.append((ordinal, item, tid, run))
    for b in range(1, math.ceil(len(episodes) / block_size) + 1):
        out = root / 'evidence' / f'{b:02}'
        rows, entries = [], []
        for ordinal, item, tid, run in episodes[(b - 1) * block_size: b * block_size]:
            d = out / f'{ordinal:03}'
            d.mkdir(parents=True)
            for name in ('task.json', 'prediction.json', 'summary.json'):
                shutil.copyfile(run / name, d / name)
            save(d / 'reference_answer.json', {'task_id': tid, 'official_answer': REFERENCES[item]})
            (d / 'trajectory.jsonl').write_text('')
            save(d / 'failure.json', {'status': 'completed'})
            rows.append({'id': tid, 'ordinal': ordinal, 'task_sha256': sha(d / 'task.json'),
                         'prediction_sha256': sha(d / 'prediction.json'),
                         'reference_answer_sha256': sha(d / 'reference_answer.json')})
            entries.append({'id': tid, 'ordinal': ordinal, 'directory': f'{ordinal:03}'})
        seal = root / 'private-learning-seals' / f'{b:02}.json'
        save(seal, {'block': b, 'source_arm': 'adapted', 'predictions_sealed': True,
                    'reference_scope': 'completed_batch_only', 'sealed_utc': '2026-09-28T00:00:00+00:00',
                    'reference_released_utc': '2026-09-28T00:00:01+00:00', 'episodes': rows})
        save(out / 'manifest.json', {'source_arm': 'adapted', 'block': b, 'episodes': entries, 'includes_gold': True,
                                     'includes_grades': False, 'includes_future_questions': False,
                                     'includes_baseline_traces': False, 'includes_source_hints': False,
                                     'batch_seal': {'file': seal.name, 'sha256': sha(seal)}})
        (out / 'README.md').write_text('toy evidence\n')
    return root


def text_w0(path, notes=()):
    path = Path(path)
    save(path / 'adapter.json', {'architecture': 'text', 'contract': C.CONTRACT_VERSION, 'pack': 'toy'})
    (path / 'SEED.md').write_text('Check the answer count and the scale before answering.\n' + PACK.seed_text() + '\n')
    (path / 'notes').mkdir(parents=True, exist_ok=True)
    for note in notes:
        LC.save_note(path / 'notes', note)
    return path


def mech_w0(path, rules):
    path = Path(path)
    save(path / 'adapter.json', {'architecture': 'mechanism', 'contract': C.CONTRACT_VERSION, 'pack': 'toy'})
    (path / 'SEED.md').write_text('Submit your answer with submit --answer.\n')
    (path / 'notes').mkdir(parents=True, exist_ok=True)
    for rule in rules:
        save_rule(path / 'rules', rule)
    save(path / 'policy.json', {})
    return path


class FakeActor:
    """actor_runner stand-in: right when the scenario says so for this (adapter, question), else wrong."""

    def __init__(self, references, right_when):
        self.references, self.right_when = references, right_when
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, adapter, task, run_dir):
        run_dir.mkdir(parents=True)
        notes = sorted(p.stem for p in (Path(adapter) / 'notes').glob('*.json'))
        rules = sorted(p.stem for p in (Path(adapter) / 'rules').glob('*.json'))
        with self.lock:
            self.calls.append({'adapter': str(adapter), 'ordinal': task['ordinal'], 'notes': notes, 'rules': rules})
        right = self.right_when(task['ordinal'], notes)
        return {'status': 'completed', 'answer': self.references[task['ordinal']] if right else 'wrong',
                'out_tokens': 1200, 'transport_failed': False, 'elapsed_seconds': 1.0, 'requests': 3}


def refs_of(items):
    return {o: REFERENCES[item] for o, item in enumerate(items, 1)}


@pytest.fixture(scope='module')
def runner():
    return SandboxRunner()


@pytest.fixture(scope='module')
def toy_corpus(tmp_path_factory):
    return LC.TextDirCorpus(make_corpus(tmp_path_factory.mktemp('corpus')))


@pytest.fixture(scope='module')
def text_root(tmp_path_factory):
    """10 no-gate (text-arm) episodes in 2 blocks; wrong: 1, 3, 5, 7, 9."""
    return make_campaign(tmp_path_factory.mktemp('text') / 'root', ITEMS)


@pytest.fixture(scope='module')
def mech_root(tmp_path_factory, runner):
    """30 gated episodes (core seed rules) in 6 blocks; wrong: toy01, toy07, toy09 of each repeat."""
    return make_campaign(tmp_path_factory.mktemp('mech') / 'root', ITEMS * 3, mech=True, rules=core_seed_rules(),
                         runner=runner)


def copy_root(root, tmp_path, name='root'):
    target = tmp_path / name
    shutil.copytree(root, target, symlinks=True)
    return target


def all_text(directory):
    return '\n'.join(p.read_text(errors='replace') for p in Path(directory).rglob('*') if p.is_file())


# ---------------------------------------------------------------------------------------------
# Evidence builder
# ---------------------------------------------------------------------------------------------

def _msg(role, text, id=None):
    item = {'type': 'message', 'role': role, 'content': [{'type': 'input_text' if role != 'assistant' else 'output_text',
                                                          'text': text}]}
    if id:
        item['id'] = id
    return item


def _fc(call_id, cmd):
    return {'type': 'function_call', 'id': 'fc_' + call_id, 'call_id': call_id, 'name': 'exec_command',
            'arguments': json.dumps({'cmd': cmd})}


def _fco(call_id, text):
    return {'type': 'function_call_output', 'call_id': call_id, 'output': text}


def _reasoning():
    return {'type': 'reasoning', 'id': 'rs_1', 'summary': [], 'encrypted_content': 'ENCRYPTED-SECRET'}


def write_call(api, n, items, output, *, tools=True, origin='codex', forwarded=True, parent=None, inserted=None):
    save(api / f'{n:03}' / 'request.json', {'model': MODEL, 'input': items, 'tools': [{'type': 'function'}] if tools else []})
    save(api / f'{n:03}' / 'response.json', {'id': f'r{n}', 'model': MODEL, 'output': output})
    save(api / f'{n:03}' / 'transport.json', {'number': n, 'origin': origin, 'forwarded': forwarded, 'parent': parent})
    if inserted is not None:
        save(api / f'{n:03}' / 'controller.json', {'number': n, 'origin': origin, 'kind': 'turn', 'inserted': inserted})


def test_model_view_segments_compaction_notes_and_truncation(tmp_path):
    api = tmp_path / 'api'
    dev, agents, task = _msg('developer', 'SYSTEM ' * 400, 'd1'), _msg('user', '# AGENTS.md instructions ...', 'u1'), \
        _msg('user', 'Question: what is x?', 'u2')
    big = 'Output:\n' + 'A' * 3000 + '\n…2000 chars truncated…\n' + 'Z' * 3000
    long_cmd = 'python3 -c "' + 'x' * 2600 + '"'
    h1 = [dev, agents, task]
    write_call(api, 1, h1, [_reasoning(), _fc('c1', 'cat big.txt')])
    h2 = h1 + [_fc('c1', 'cat big.txt'), _fco('c1', big)]
    write_call(api, 2, h2, [_fc('c2', long_cmd)])
    h3 = h2 + [_fc('c2', long_cmd), _fco('c2', 'ok'),
               _msg('user', 'You are performing a CONTEXT CHECKPOINT COMPACTION. Create a handoff summary for another LLM')]
    write_call(api, 3, h3, [_msg('assistant', 'summary text')], tools=False)
    resume = [_msg('developer', 'SYSTEM ' * 400, 'd9'), _msg('user', '# AGENTS.md instructions ...', 'u9'),
              _msg('user', 'Question: what is x?', 'u8'),
              _msg('user', 'Another language model started to solve this problem and wrote: summary text', 'u7'),
              _msg('user', '[harness:ledger] Ledger (recorded by the harness ...)')]
    write_call(api, 4, resume, [_fc('c3', 'rg total f.txt')],
               inserted=[{'public_index': 4, 'reason': 'ledger', 'item': None}])
    write_call(api, 5, resume + [_fc('c3', 'rg total f.txt'), _fco('c3', 'f.txt:9: total 42')],
               [_reasoning(), _msg('assistant', 'Final answer: 42')])
    text, counts = EV.model_view(api)
    assert counts == {'calls': 5, 'compactions': 1, 'final_call': 5, 'controller_calls': 0, 'harness_notes': {'ledger': 1}}
    assert text.count('DEVELOPER INSTRUCTIONS') == 1 and 'SYSTEM SYSTEM' not in text     # omitted, and once only
    assert text.count('USER: # AGENTS.md') == 1 and text.count('USER: Question: what is x?') == 1
    assert text.count('CONTEXT COMPACTION') == 1 and text.count("SUMMARY (the model's own hand-off") == 1
    assert 'HARNESS NOTE (ledger): [harness:ledger] Ledger' in text
    assert 'ENCRYPTED-SECRET' not in text and 'reasoning' not in text.lower()
    order = [text.index(s) for s in ('cat big.txt', 'CONTEXT COMPACTION', 'SUMMARY (', 'rg total', 'FINAL MESSAGE: Final answer: 42')]
    assert order == sorted(order)
    out_line = next(l for l in text.split('\n[#') if l.split('] ', 1)[-1].startswith('OUTPUT: Output:\nAAA'))
    assert '[' in out_line and 'chars omitted]' in out_line and out_line.endswith('Z' * 500)
    body = out_line.split('OUTPUT: ', 1)[1]
    assert body.startswith('Output:\n' + 'A' * (EV.OUT_HEAD - 8)) and len(body) < EV.OUT_HEAD + EV.OUT_TAIL + 60
    cmd_line = next(l for l in text.split('\n') if 'COMMAND (exec_command): python3' in l)
    assert 'chars omitted' in cmd_line and len(cmd_line.split(': ', 1)[1]) < EV.CMD_HEAD + EV.CMD_TAIL + 60



def test_model_view_refuses_a_missing_request_record(tmp_path):
    """review: without request.json the segments cannot be rebuilt; refuse rather than show a thin view."""
    api = tmp_path / 'api'
    h = [_msg('user', 'Question: q?')]
    write_call(api, 1, h, [_fc('c1', 'ls')])
    write_call(api, 2, h + [_fc('c1', 'ls'), _fco('c1', 'a.txt')], [_msg('assistant', 'Final answer: 1')])
    assert 'FINAL MESSAGE: Final answer: 1' in EV.model_view(api)[0]
    (api / '001' / 'request.json').unlink()
    with pytest.raises(ValueError, match='001/request.json is missing'):
        EV.model_view(api)
    (api / '001' / 'request.json').write_text('{"input": "not a list"}')
    with pytest.raises(ValueError, match='missing or unreadable'):
        EV.model_view(api)


def test_model_view_follows_a_harness_requery(tmp_path):
    api = tmp_path / 'api'
    h = [_msg('user', 'Question: q?')]
    write_call(api, 1, h, [_msg('assistant', 'Answer: 41')], forwarded=False)
    requery = h + [_msg('assistant', 'Answer: 41'), _msg('user', '[harness:enforce] Your final answer was not submitted')]
    write_call(api, 2, requery, [_msg('assistant', 'Final answer: 42')], origin='controller', parent=1,
               inserted=[{'public_index': 1, 'reason': 'enforce'}, {'public_index': 2, 'reason': 'enforce'}])
    (api / 'controller.jsonl').write_text(json.dumps({'t': 1.0, 'event': 'enforce', 'rules': ['answer_count'], 'n': 1}) + '\n')
    text, counts = EV.model_view(api)
    assert counts['final_call'] == 2 and counts['controller_calls'] == 1 and counts['harness_notes'] == {'enforce': 1}
    assert 'harness re-query of call 1' in text and 'MODEL MESSAGE: Answer: 41' in text
    assert 'HARNESS NOTE (enforce)' in text and text.rstrip().endswith('FINAL MESSAGE: Final answer: 42')
    ctrl = EV.controller_view(api)
    assert '"event": "enforce"' in ctrl and 'call 2 (controller, turn) | inserted: enforce, enforce' in ctrl


def test_build_evidence_grades_withholds_and_is_deterministic(text_root):
    ev = EV.build_evidence(text_root, 2, grader=GRADER, exclude=['T003'])
    assert ev.ordinals() == [1, 2, 4, 5, 6, 7, 8, 9, 10] and ev.excluded == [3]
    assert {o: e.strict for o, e in ev.episodes.items()} == {1: 0, 2: 1, 4: 1, 5: 0, 6: 1, 7: 0, 8: 1, 9: 0, 10: 1}
    assert ev.episodes[1].reference == REFERENCES['toy01'] and ev.episodes[1].submitted == '250.75'
    assert ev.digest == EV.build_evidence(text_root, 2, grader=GRADER, exclude=['T003']).digest
    assert ev.digest != EV.build_evidence(text_root, 2, grader=GRADER).digest
    assert '1 further episodes are withheld' in ev.listing() and 'T003' not in ev.listing()
    with pytest.raises(ValueError, match='withheld'):
        ev.read(3, 'summary')
    summary = ev.read(1, 'summary')
    assert summary['pages'] == 1 and 'Reference (released): 252.00' in summary['content']
    assert 'no ledger' in ev.read(1, 'ledger')['content'] and 'no controller records' in ev.read(1, 'controller')['content']
    traj = ev.read(1, 'trajectory')['content']
    assert 'COMMAND (exec_command): lookup widgets_2020_a.txt North' in traj and 'FINAL MESSAGE: 250.75' in traj
    with pytest.raises(ValueError, match='page must be'):
        ev.read(1, 'trajectory', 9)
    index = json.dumps(ev.index())
    assert '252.00' not in index and 'official_answer' not in index               # controller index: no references
    assert EV.build_evidence(text_root, 1, grader=GRADER).ordinals() == [1, 2, 3, 4, 5]  # only released blocks


def test_build_evidence_refuses_broken_contracts(text_root, tmp_path):
    def broken(mutate):
        root = copy_root(text_root, tmp_path, f'r{len(list(tmp_path.iterdir()))}')
        mutate(root)
        return root
    root = broken(lambda r: (r / 'evidence/01/002/prediction.json').write_text('{"task_id": "T002", "answer": "9"}'))
    with pytest.raises(ValueError, match='sealed prediction.json'):
        EV.build_evidence(root, 2, grader=GRADER)
    root = broken(lambda r: save(r / 'evidence/02/007/summary.json', {'status': 'completed', 'gold_answer': 'x'}))
    with pytest.raises(ValueError, match='private field'):
        EV.build_evidence(root, 2, grader=GRADER)
    root = broken(lambda r: shutil.rmtree(r / 'runs/adapted/004/api'))
    with pytest.raises(ValueError, match='no broker records'):
        EV.build_evidence(root, 1, grader=GRADER)
    root = broken(lambda r: shutil.rmtree(r / 'evidence/02'))
    with pytest.raises(ValueError, match='released block 2'):
        EV.build_evidence(root, 2, grader=GRADER)

    def swap_times(r):
        seal = r / 'private-learning-seals/01.json'
        data = json.loads(seal.read_text())
        data['sealed_utc'], data['reference_released_utc'] = data['reference_released_utc'], data['sealed_utc']
        save(seal, data)
        manifest = json.loads((r / 'evidence/01/manifest.json').read_text())
        manifest['batch_seal']['sha256'] = sha(seal)
        save(r / 'evidence/01/manifest.json', manifest)
    with pytest.raises(ValueError, match='references cannot precede'):
        EV.build_evidence(broken(swap_times), 1, grader=GRADER)

    def escape(r):
        manifest = json.loads((r / 'evidence/01/manifest.json').read_text())
        manifest['episodes'][0]['run_dir'] = str(tmp_path)
        save(r / 'evidence/01/manifest.json', manifest)
    with pytest.raises(ValueError, match='outside the campaign root'):
        EV.build_evidence(broken(escape), 1, grader=GRADER)


def test_a_block_whose_episodes_are_all_withheld_is_valid(text_root, tmp_path):
    """S6 releases only non-withheld episodes (manifest 'withheld'); a block may hold none."""
    root = copy_root(text_root, tmp_path)
    manifest = json.loads((root / 'evidence/02/manifest.json').read_text())
    manifest['withheld'] = [{'id': e['id'], 'ordinal': e['ordinal']} for e in manifest['episodes']]
    manifest['episodes'] = []
    save(root / 'evidence/02/manifest.json', manifest)
    seal = root / 'private-learning-seals/02.json'
    data = json.loads(seal.read_text())
    data['withheld_episodes'], data['episodes'] = data['episodes'], []
    save(seal, data)
    manifest['batch_seal']['sha256'] = sha(seal)
    save(root / 'evidence/02/manifest.json', manifest)
    ev = EV.build_evidence(root, 2, grader=GRADER)
    assert ev.ordinals() == [1, 2, 3, 4, 5] and ev.of_block(2) == []
    assert ev.excluded == [6, 7, 8, 9, 10]                      # recorded for the prompt and evidence-index.json
    manifest['withheld'] = []
    save(root / 'evidence/02/manifest.json', manifest)
    with pytest.raises(ValueError, match='must declare block 2'):
        EV.build_evidence(root, 2, grader=GRADER)


def test_default_corpus_follows_the_campaign_layout(tmp_path):
    with pytest.raises(LC.RoundFailure, match='not configured'):
        LC.default_corpus(tmp_path)
    make_corpus(tmp_path / 'prepared/agent/corpus')
    corpus = LC.default_corpus(tmp_path)
    assert isinstance(corpus, LC.TextDirCorpus) and corpus.read('widgets_2020_a.txt', 1, 1)['total_lines'] > 5


def test_ledger_view_of_a_gated_episode(mech_root):
    ev = EV.build_evidence(mech_root, 1, grader=GRADER)
    ledger = ev.read(3, 'ledger')['content']                  # toy03: scale finding, offer accepted
    assert 'Candidates:' in ledger and '"14.77" via submit' in ledger and '"0.1477" via submit' in ledger
    assert 'fired scale' in ledger and 'offer "0.1477"' in ledger
    assert '120.5 = c1 widgets_2020_a.txt:5' in ev.read(1, 'ledger')['content']   # calc input linked to its cell
    assert ev.episodes[3].has_ledger and ev.episodes[3].counts['harness_notes'] == {}


def check_v8_model_view(api):
    """model_view of a recorded v8 run agrees with controller_runtime.replay_run (compactions, calls, and
    non-poll commands: replay_run skips write_stdin polls, the model saw them); nothing private; the Codex
    developer prompt shown once; every output within the cut."""
    import controller_runtime as CR
    text, counts = EV.model_view(api)
    replay = CR.replay_run(str(api))
    assert counts['compactions'] == replay['compactions'] and counts['calls'] == replay['calls']
    assert text.count('COMMAND (') - text.count('COMMAND (write_stdin)') == replay['commands']
    assert text.count('CONTEXT COMPACTION') == counts['compactions'] and 'encrypted_content' not in text
    assert text.count('DEVELOPER INSTRUCTIONS') == 1
    for line in text.split('\n[#'):
        body = line.split('] OUTPUT: ', 1)
        if len(body) == 2:
            assert len(body[1].split('\n=== call')[0]) <= EV.OUT_LIMIT + 60
    return counts


@pytest.mark.skipif(not (V8_NONE / '072' / 'api').is_dir(), reason='v8 none-arm records not on disk')
def test_model_view_of_real_v8_runs_matches_the_broker_replay():
    """Read-only on the model's own recorded traffic (no answers are opened)."""
    assert check_v8_model_view(V8_NONE / '072' / 'api')['compactions'] == 2
    assert check_v8_model_view(V8_NONE / '053' / 'api')['compactions'] == 2
    assert check_v8_model_view(V8_NONE / '010' / 'api')['compactions'] == 3     # has a write_stdin poll


@pytest.mark.slow
@pytest.mark.skipif(not V8_NONE.is_dir(), reason='v8 none-arm records not on disk')
def test_model_view_of_every_v8_none_run():
    runs = sorted(p for p in V8_NONE.iterdir() if (p / 'api').is_dir())
    assert len(runs) == 133
    assert sum(check_v8_model_view(p / 'api')['compactions'] for p in runs) == 35


# ---------------------------------------------------------------------------------------------
# Shared pieces
# ---------------------------------------------------------------------------------------------

def test_calc_is_arithmetic_only():
    assert LC.calc('round((101 - 88) / 88, 4)')['value'] == 0.1477
    assert LC.calc('mean([1, 2, 3])')['value'] == 2
    for bad in ('__import__("os")', 'open("x")', '(1).real', '[x for x in [1]]', '2 ** 100000'):
        with pytest.raises((ValueError, SyntaxError)):
            LC.calc(bad)
    # review: runaway work is refused before it is computed (the v8 whitelist accepted all of these)
    for bad in ('(10 ** 1000) ** 10', '[0] * 10 ** 9', '2 ** 4096', 'round(1, 10 ** 9)', '-[1, 2]'):
        with pytest.raises(ValueError):
            LC.calc(bad)
    assert LC.calc('2 ** 4095')['value'] == 2 ** 4095 and LC.calc('sum([1.5, 2.5])')['value'] == 4.0


def test_note_selection_triggers_and_rendering():
    def note(i, triggers, block=1):
        return C.NoteSpec(id=f'n{i}', triggers=triggers, text=f'text {i}', admitted_block=block, origin='learned')
    q = 'What was the total public debt in fiscal year 1950?'
    notes = [note(1, ['*'], 0), note(2, ['public debt'], 2), note(3, ['debt']), note(4, ['fiscal year'], 3),
             note(5, ['fiscal year'], 2), note(6, ['revenue']), note(7, ['debt public'])]
    assert [n.id for n in LC.select_notes(notes, q)] == ['n1', 'n3', 'n2']        # (admitted_block, id), at most 3
    assert [n.id for n in LC.select_notes(notes, q, k=9)] == ['n1', 'n3', 'n2', 'n5', 'n4']   # word order matters
    assert LC.select_notes(notes, 'unrelated question') == [notes[0]]
    assert LC.trigger_errors(['public debt', 'fy']) == ["trigger 'fy' needs a content word of 3+ letters"]
    assert any('no regex' in e for e in LC.trigger_errors(['debt.*total']))
    assert any('no regex' in e for e in LC.trigger_errors(['*']))
    assert any('no regex' in e for e in LC.trigger_errors(['Public Debt']))
    assert any('content word' in e for e in LC.trigger_errors(['of the']))
    assert LC.trigger_errors(['department of the army']) == []                   # the v8 KEYWORD_MIN false reject
    assert LC.note_text_errors('x' * (C.NOTE_MAX + 1)) == [f'text exceeds {C.NOTE_MAX} bytes']
    assert LC.render_note(note(7, ['debt'])) == '- text 7\n'


@pytest.mark.skipif(not (ROOT / 'notes_render.py').is_file(), reason='S6 notes_render.py not present')
def test_selection_and_rendering_equal_the_served_ones():
    """What the learner screens against is what orchestration serves (S6 notes_render)."""
    import notes_render as NR
    notes = [C.NoteSpec(id=f'n{i}', triggers=t, text=f'  text\n{i} ', admitted_block=b, origin='learned')
             for i, (t, b) in enumerate([(['public debt'], 2), (['debt'], 1), (['fiscal year'], 3), (['year'], 0),
                                         (['revenue'], 1), (['public debt'], 1)])]
    for q in ('What was the total public debt in fiscal year 1950?', 'Revenue in 1950?', 'nothing here', ''):
        assert [n.id for n in LC.select_notes(notes, q)] == [n.id for n in NR.choose(notes, q)]
    assert all(LC.render_note(n) == NR.render_note(n) for n in notes)


@pytest.mark.skipif(not CARDS_BLOCK.is_file(), reason='frameworks-v8 cards block.py not on disk')
def test_judge_is_cards_block_judge():
    spec = importlib.util.spec_from_file_location('cards_block_ref', CARDS_BLOCK)
    block = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(block)
    assert (LC.K, LC.KEEP, LC.SUPPORT_MIN, LC.COST_MAX) == (block.K, block.KEEP, block.SUPPORT_MIN, block.COST_MAX)

    def run(q, role, k, strict, tokens=100, excluded=False):
        return {'q': q, 'role': role, 'k': k, 'strict': strict, 'out_tokens': tokens, 'excluded': excluded}
    cases = [
        [run(1, 'support', 1, 1), run(1, 'support', 2, 1), run(5, 'keep', 1, 1), run(5, 'keep', 2, 0)],
        [run(1, 'support', 1, 1), run(1, 'support', 2, 0), run(5, 'keep', 1, 1), run(5, 'keep', 2, 1)],
        [run(1, 'support', 1, 1), run(1, 'support', 2, 0), run(2, 'support', 1, 1), run(2, 'support', 2, 1)],
        [run(1, 'support', 1, 1), run(1, 'support', 2, 1, excluded=True), run(5, 'keep', 1, 0), run(5, 'keep', 2, 0)],
        [run(1, 'support', 1, 1, 900), run(1, 'support', 2, 1, 900), run(5, 'keep', 1, 1), run(5, 'keep', 2, 1)],
        [run(1, 'support', 1, 1, None), run(1, 'support', 2, 1), run(5, 'keep', 1, 0), run(5, 'keep', 2, 0)],
    ]
    originals = {1: 100, 2: None, 5: 100}
    for runs in cases:
        assert LC.judge(runs, originals) == block.judge(runs, originals)


def test_issue47_list_is_the_cards_list():
    tree = ast.parse((ROOT / 'cards_learning.py').read_text())
    value = next(node.value for node in tree.body if isinstance(node, ast.Assign)
                 and any(getattr(t, 'id', '') == 'EXCLUDED_TASKS' for t in node.targets))
    assert tuple(ast.literal_eval(value)) == LC.OFFICEQA_ISSUE47 and len(LC.OFFICEQA_ISSUE47) == 18
    assert LC.default_exclude(PACK) == ()
    from packs.officeqa.pack import OfficeQAPack
    assert LC.default_exclude(OfficeQAPack()) == LC.OFFICEQA_ISSUE47


def test_text_budget_counts_seed_notes_rules_and_templates(tmp_path):
    w0 = text_w0(tmp_path / 'w0')
    seed, source = LC.seed_text(w0, PACK)
    assert source == 'SEED.md' and PACK.seed_text() in seed
    note = C.NoteSpec(id='n', triggers=['debt'], text='x' * 500)
    budget = LC.text_budget(seed, [note] * 3, core_seed_rules(), LC.controller_template_bytes())
    assert budget['notes'] == 3 * len(LC.render_note(note).encode())
    assert budget['rules'] == sum(len(r.finding.encode()) for r in core_seed_rules())
    assert 300 < budget['controller'] < 1000 and budget['ok']
    assert not LC.text_budget('s' * C.TEXT_BUDGET, [note], [])['ok']
    (w0 / 'SEED.md').unlink()
    assert LC.seed_text(w0, PACK) == (PACK.seed_text(), 'pack.seed_text() (SEED.md absent)')


def test_fireworks_model_passes_the_declared_reasoning_effort(monkeypatch):
    import client
    assert LC.MODEL_NAME == client.MODEL
    seen = {}

    def fake_complete(key, messages, *, timeout=1200, unbounded=False, **options):
        seen.update(key=key, messages=messages, timeout=timeout, unbounded=unbounded, **options)
        return None, {}
    monkeypatch.setattr(client, 'complete', fake_complete)
    model = LC.FireworksModel('sk-test-key-not-real', reasoning_effort='high')
    assert 'sk-test' not in repr(model) and model.name == client.MODEL
    model([{'role': 'user', 'content': 'x'}], tools=[{'type': 'function'}], max_tokens=1234)
    assert seen['reasoning_effort'] == 'high' and seen['max_tokens'] == 1234 and not seen['unbounded']
    assert seen['tools'] == [{'type': 'function'}] and 'tool_choice' not in seen


# ---------------------------------------------------------------------------------------------
# Text learner
# ---------------------------------------------------------------------------------------------

NOTE_ARGS = {'id': 'latest_printing', 'triggers': ['widgets shipped'],
             'text': 'When a later bulletin revises a figure (marked r), use the revised printing unless the question '
                     'names a printing.', 'support': [1, 7], 'rationale': 'q1 and q7 used the first printing'}


def text_session():
    return [
        calls(tool_call('list_episodes'), tool_call('read_episode', ordinal=1, part='summary'),
              tool_call('read_episode', ordinal=1, part='trajectory'), tool_call('search_corpus', query='North widgets'),
              tool_call('read_corpus', filename='widgets_2020_b.txt', start_line=1, num_lines=6),
              tool_call('verify_cell', file='widgets_2020_b.txt', line=5, contains='131.5'),
              tool_call('verify_answer', ordinal=1, candidate='252.00'), tool_call('calc', expression='120.5 + 131.5'),
              tool_call('read_episode', ordinal=3, part='nonsense'), tool_call('no_such_tool')),
        calls(tool_call('propose_note', **{**NOTE_ARGS, 'text': 'The North total is 252 here.'}),   # fixed below, same id
              tool_call('propose_note', **{**NOTE_ARGS, 'id': 'outside', 'support': [2]}),
              tool_call('propose_note', **NOTE_ARGS)),
        say('Report: first printings were used where a revision existed; one note proposed.'),
    ]


def run_text(root, tmp_path, model, *, fixes=True, name='out', **options):
    w0 = text_w0(tmp_path / f'w0-{name}')
    refs = refs_of(ITEMS)
    actor = FakeActor(refs, lambda q, notes: (fixes and 'latest_printing' in notes) or q in (2, 4, 6, 8, 10))
    options.setdefault('corpus', LC.TextDirCorpus(make_corpus(tmp_path / f'corpus-{name}')))
    result = TL.run_learning('', w0, root / 'evidence/02', tmp_path / name, 2, actor_runner=actor, pack=PACK,
                             grader=GRADER, model=model, w0=w0, **options)
    return result, actor, w0


def test_text_learner_admits_a_verified_note(text_root, tmp_path):
    root = copy_root(text_root, tmp_path)
    model = ScriptedModel(text_session())
    result, actor, w0 = run_text(root, tmp_path, model)
    out = tmp_path / 'out'
    for key in ('status', 'activated', 'candidate', 'block', 'model', 'profile', 'architecture', 'usage', 'session'):
        assert key in result
    assert result['status'] == 'admitted' and result['activated'] and result['candidate'] == str(out / 'adapter')
    assert (result['profile'], result['architecture'], result['model'], result['block']) == ('text-evolve', 'text', MODEL, 2)
    assert result['reasoning_effort'] == 'high' and result['learner_status'] == 'completed'
    assert result['usage'] == {'prompt_tokens': 3000, 'completion_tokens': 600, 'cached_tokens': 0}
    assert result['admitted'] == ['latest_printing'] and result['verification_episodes'] == 8
    assert json.loads((out / 'result.json').read_text()) == result
    note = json.loads((out / 'adapter/notes/latest_printing.json').read_text())
    assert note['admitted_block'] == 2 and note['origin'] == 'learned' and note['triggers'] == ['widgets shipped']
    assert note['stats']['support'] == [1, 7] and note['stats']['keep'] == [4, 10]
    # verification: the note alone on the source, support x2 + controls x2
    assert sorted((c['ordinal'], tuple(c['notes'])) for c in actor.calls) == \
        sorted([(q, ('latest_printing',)) for q in (1, 1, 7, 7, 4, 4, 10, 10)])
    assert all(c['adapter'].endswith('verify/latest_printing/adapter') for c in actor.calls)
    # screen: leak and pool refusals, answered at once
    screen = json.loads((out / 'screen.json').read_text())
    assert [s['ok'] for s in screen] == [False, False, True]
    assert any('leak' in r for r in screen[0]['reasons']) and 'support outside the pool: 2' in screen[1]['reasons']
    # admission record: real values
    admission = json.loads((out / 'sessions/learner/control/mechanical-admission.json').read_text())
    assert admission['admitted'] and admission['operation_provenance_matches'] is True
    assert admission['diff'] == {'added': ['notes/latest_printing.json'], 'removed': [], 'changed': []}
    assert admission['protected_unchanged'] == {'source': True, 'evidence': True} and admission['text_budget']['ok']
    # durable state and ledger
    state = json.loads((root / LC.STATE_FILE).read_text())
    assert state['claimed'] == {'1': 'latest_printing', '7': 'latest_printing'} and state['adapter_since'] == 3
    rows = [json.loads(l) for l in (root / LC.LEDGER_FILE).read_text().splitlines()]
    assert [(r['kind'], r['action'], r['admitted']) for r in rows] == \
        [('note', 'add', True), ('note', 'screen_rejected', False), ('note', 'screen_rejected', False)]
    assert rows[0]['runs'] and all(set(r) >= {'q', 'role', 'k', 'strict', 'status', 'excluded'} for r in rows[0]['runs'])
    # model call records: client.complete record shape, no private reasoning anywhere
    record = json.loads((out / 'sessions/learner/control/fireworks-calls/001.json').read_text())
    assert set(record) >= {'request', 'purpose', 'requested_model', 'meta', 'public_request_sha256',
                           'public_message_count', 'request_max_tokens', 'reasoning_effort', 'message', 'finish_reason'}
    assert record['reasoning_effort'] == 'high' and record['request_max_tokens'] == 65_536
    assert PRIVATE_REASONING not in all_text(out) and PRIVATE_REASONING not in all_text(root)
    # tool results: every common tool answered; errors returned, not raised
    results = sorted((out / 'sessions/learner/control/fireworks-tool-results').glob('*.json'))
    by_name = {}
    for path in results:
        rec = json.loads(path.read_text())
        by_name.setdefault(rec['name'], []).append(rec)
    assert json.loads(by_name['verify_answer'][0]['response'])['strict'] == 1
    assert json.loads(by_name['verify_cell'][0]['response'])['found'] is True
    assert json.loads(by_name['calc'][0]['response'])['value'] == 252.0
    assert json.loads(by_name['search_corpus'][0]['response'])['results']
    assert 'error' in json.loads(by_name['no_such_tool'][0]['response'])
    assert not by_name['read_episode'][-1]['ok']                          # schema refusal of part='nonsense'
    # the prompt: common text, pool, the task conventions; no key-derived ladder
    prompt = (out / 'sessions/learner/control/prompt.md').read_text()
    assert 'Pool (wrong under the current adapter' in prompt and PACK.task_conventions().strip() in prompt
    assert 'Ladder' not in prompt and 'shelf' not in prompt
    request = json.loads((out / 'request.json').read_text())
    assert request['evidence_digest'] == EV.build_evidence(root, 2, grader=GRADER).digest
    assert request['pool'] == [1, 3, 5, 7, 9] and request['right'] == [2, 4, 6, 8, 10]


def test_text_learner_rejects_a_note_that_fails_verification(text_root, tmp_path):
    root = copy_root(text_root, tmp_path)
    result, actor, w0 = run_text(root, tmp_path, ScriptedModel(text_session()), fixes=False)
    assert result['status'] == 'retained' and not result['activated'] and result['candidate'] == str(w0.resolve())
    assert not (tmp_path / 'out/adapter').exists() and len(actor.calls) == 8
    rows = [json.loads(l) for l in (root / LC.LEDGER_FILE).read_text().splitlines()]
    assert rows[0]['admitted'] is False and rows[0]['reason'].startswith('support 0/4')
    state = json.loads((root / LC.STATE_FILE).read_text())
    assert len(state['rejected_sha']) == 1 and state['claimed'] == {} and state['adapter_since'] == 1
    admission = json.loads((tmp_path / 'out/sessions/learner/control/mechanical-admission.json').read_text())
    assert admission['admitted'] is False and admission['diff'] == admission['expected_diff']


def test_rounds_chain_through_the_previous_candidate(tmp_path):
    items = ITEMS + ITEMS[:5]                        # 15 episodes, 3 blocks
    root = make_campaign(tmp_path / 'root', items)
    w0 = text_w0(tmp_path / 'w0')
    refs = refs_of(items)
    actor = FakeActor(refs, lambda q, notes: 'latest_printing' in notes or q % 2 == 0)   # controls are the even (right) ones
    corpus = LC.TextDirCorpus(make_corpus(tmp_path / 'corpus'))
    common = dict(actor_runner=actor, pack=PACK, grader=GRADER, w0=w0, corpus=corpus)
    first = TL.run_learning('', w0, root / 'evidence/01', tmp_path / 'evolution/01', 1,
                            model=ScriptedModel([calls(tool_call('propose_note', **{**NOTE_ARGS, 'support': [1]}))]),
                            **common)
    assert first['status'] == 'admitted' and first['candidate'] == str((tmp_path / 'evolution/01/adapter').resolve())
    second = TL.run_learning('', first['candidate'], root / 'evidence/02', tmp_path / 'evolution/02', 2,
                             model=ScriptedModel([say('no note'), say('no note')]), profile='text-evolve-r2', **common)
    assert second['status'] == 'retained' and second['candidate'] == first['candidate']
    assert second['profile'] == 'text-evolve-r2'
    request = json.loads((tmp_path / 'evolution/02/request.json').read_text())
    assert request['source_lineage']['kind'] == 'previous_candidate'
    assert request['adapter_since'] == 2 and request['pool'] == [7, 9] and request['stale'] == [3, 5]
    prompt = (tmp_path / 'evolution/02/sessions/learner/control/prompt.md').read_text()
    assert 'Earlier learner report (block 1' in prompt and '- latest_printing (admitted block 1' in prompt
    note = Path(first['candidate']) / 'notes/latest_printing.json'
    note.write_text(note.read_text().replace('revised printing', 'first printing'))
    third = TL.run_learning('', first['candidate'], root / 'evidence/03', tmp_path / 'evolution/03', 3,
                            model=ScriptedModel([say('no note')]), **common)
    assert third['status'] == 'failed' and "previous activated candidate" in third['error']


def test_retirement_removes_an_underperforming_note(tmp_path):
    items = ITEMS + ITEMS[:5]                        # 15 episodes, 3 blocks; wrong: 1 3 5 7 9 11 13 15
    served = {7: ['by_region'], 9: ['by_region'], 11: ['by_region'], 13: ['by_region'], 6: ['kept_note'],
              8: ['kept_note'], 12: ['kept_note']}
    root = make_campaign(tmp_path / 'root', items, notes_served=served)
    notes = [C.NoteSpec(id='by_region', triggers=['region'], text='Check the region row.', admitted_block=1, origin='learned'),
             C.NoteSpec(id='kept_note', triggers=['shipments'], text='Check the quarter.', admitted_block=1, origin='learned')]
    w0 = text_w0(tmp_path / 'w0', notes)
    model = ScriptedModel([say('no note'), say('no note: nothing generalises')])
    result = TL.run_learning('', w0, root / 'evidence/03', tmp_path / 'out', 3, actor_runner=FakeActor({}, None),
                             pack=PACK, grader=GRADER, model=model, w0=w0, corpus=LC.TextDirCorpus(tmp_path))
    assert result['status'] == 'revised' and result['activated'] and result['retired'] == ['by_region']
    assert not (tmp_path / 'out/adapter/notes/by_region.json').exists()
    assert (tmp_path / 'out/adapter/notes/kept_note.json').exists()
    request = json.loads((tmp_path / 'out/request.json').read_text())
    stats = {r['id']: r for r in request['retirement']}
    assert stats['by_region']['served'] == 4 and stats['by_region']['served_right'] == 0
    assert stats['by_region']['others'] == 6 and stats['by_region']['others_right'] == 5
    assert stats['kept_note']['served'] == 3 and not stats['kept_note']['retire']     # < 4 served: kept
    assert stats['by_region']['served_source'] == ['marker']
    rows = [json.loads(l) for l in (root / LC.LEDGER_FILE).read_text().splitlines()]
    assert [(r['action'], r['id']) for r in rows] == [('retire', 'by_region')]
    assert json.loads((root / LC.STATE_FILE).read_text())['adapter_since'] == 4
    assert model.requests[1]['messages'][-1]['content'] == TL.NUDGE                   # one nudge, then accepted


def test_retirement_derives_served_notes_when_unrecorded(tmp_path):
    root = make_campaign(tmp_path / 'root', ITEMS)
    ev = EV.build_evidence(root, 2, grader=GRADER)
    note = C.NoteSpec(id='quarters', triggers=['q2 2020'], text='x', admitted_block=0, origin='learned')
    [stat] = LC.retirement([note], ev)
    assert stat['served_source'] == ['derived'] and stat['served'] == 3              # toy01, toy04, toy07 name Q2 2020


# ---------------------------------------------------------------------------------------------
# Session loop: budget, runaway, checkpoint, failures
# ---------------------------------------------------------------------------------------------

def test_budget_exhaustion_by_calls_and_by_output_tokens(text_root, tmp_path):
    root = copy_root(text_root, tmp_path)
    loop = [calls(tool_call('list_episodes'))] * 20
    result, _, _ = run_text(root, tmp_path, ScriptedModel(list(loop)), name='calls', budget=LC.Budget(calls=3))
    assert result['status'] == 'retained' and result['learner_status'] == 'budget_exhausted'
    assert result['budget_exhausted'] == 'calls' and result['model_calls'] == 3 and result['budget']['calls'] == 3
    model = ScriptedModel(list(loop), completion_tokens=200)
    result, _, _ = run_text(root, tmp_path, model, name='tokens', budget=LC.Budget(output_tokens=500))
    assert result['budget_exhausted'] == 'output_tokens' and result['model_calls'] == 3
    assert [r['max_tokens'] for r in model.requests] == [500, 300, 100]
    model = ScriptedModel(list(loop), prompt_tokens=400_000)
    result, _, _ = run_text(root, tmp_path, model, name='input', budget=LC.Budget(input_tokens=1_000_000))
    assert result['budget_exhausted'] == 'input_tokens' and result['model_calls'] == 3


def test_runaway_resend_and_context_checkpoint(text_root, tmp_path):
    root = copy_root(text_root, tmp_path)
    model = ScriptedModel([runaway(), runaway(), runaway(), calls(tool_call('list_episodes'), prompt_tokens=700_000),
                           say('checkpoint: nothing found yet'), say('no note'), say('no note')], prompt_tokens=10)
    result, _, _ = run_text(root, tmp_path, model, name='cp', budget=LC.Budget(input_tokens=10_000_000))
    control = tmp_path / 'cp/sessions/learner/control'
    state = json.loads((control / 'fireworks-result.json').read_text())
    assert state['runaways'] == 3 and state['compactions'] == 1 and result['learner_status'] == 'completed'
    assert [len(r['messages']) for r in model.requests[:4]] == [1, 1, 1, 1]     # runaways re-sent unchanged
    assert model.requests[4]['tool_choice'] == 'none'                          # the checkpoint call
    assert json.loads((control / 'fireworks-calls/005.json').read_text())['purpose'] == 'learner_context_checkpoint'
    assert [m['role'] for m in model.requests[5]['messages']] == ['user', 'assistant', 'user']
    assert (control / 'learner-checkpoint-01.md').read_text() == 'checkpoint: nothing found yet'


@pytest.mark.parametrize('case', ['transport', 'provenance', 'stop', 'tampered', 'effort', 'w0', 'corpus'])
def test_failures_are_loud(text_root, tmp_path, case):
    root = copy_root(text_root, tmp_path)
    model = ScriptedModel(text_session())
    options = {}
    if case == 'transport':
        model = ScriptedModel([calls(tool_call('list_episodes')), None])
    elif case == 'provenance':
        model = ScriptedModel(text_session(), model_name='accounts/fireworks/models/other')
    elif case == 'stop':
        options['stop'] = threading.Event()
        options['stop'].set()
    elif case == 'tampered':
        (root / 'evidence/02/006/prediction.json').write_text('{"task_id": "T006", "answer": "1"}')
    elif case == 'effort':
        model = ScriptedModel(text_session(), reasoning_effort='low')
    elif case == 'w0':
        options['w0'] = text_w0(tmp_path / 'other-w0')
    elif case == 'corpus':
        options['corpus'] = None
    w0 = text_w0(tmp_path / 'w0')
    actor = FakeActor(refs_of(ITEMS), lambda q, notes: True)
    stop = options.pop('stop', None)
    corpus = options.pop('corpus', LC.TextDirCorpus(make_corpus(tmp_path / 'corpus')))
    result = TL.run_learning('secret-key-value', w0, root / 'evidence/02', tmp_path / 'out', 2, stop, actor_runner=actor,
                             pack=PACK, grader=GRADER, model=model, corpus=corpus, **{'w0': w0, **options})
    assert result['status'] == 'failed' and result['activated'] is False and result['candidate'] == str(w0.resolve())
    assert result['error'] and 'secret-key-value' not in json.dumps(result)
    assert json.loads((tmp_path / 'out/result.json').read_text())['status'] == 'failed'
    assert not (tmp_path / 'out/adapter').exists() and not (root / LC.STATE_FILE).exists()
    expected = {'transport': 'transport failed', 'provenance': 'provenance', 'stop': 'aborted', 'tampered': 'sealed',
                'effort': 'reasoning effort', 'w0': 'neither the declared W0', 'corpus': 'corpus tools are not configured'}
    assert expected[case] in result['error']
    if case == 'transport':
        assert result['model_calls'] == 2
    with pytest.raises(FileExistsError):
        TL.run_learning('', w0, root / 'evidence/02', tmp_path / 'out', 2, actor_runner=actor, pack=PACK, grader=GRADER,
                        model=ScriptedModel([]), w0=w0, corpus=corpus)


def test_unexpected_revision_diff_fails_the_round(text_root, tmp_path, monkeypatch):
    root = copy_root(text_root, tmp_path)
    original = LC.write_revision

    def sneaky(source, target, **kwargs):
        expected = original(source, target, **kwargs)
        if target.name == 'adapter' and target.parent.name == 'out':
            (target / 'EXTRA.md').write_text('not admitted by anyone')
        return expected
    monkeypatch.setattr(LC, 'write_revision', sneaky)
    result, _, w0 = run_text(root, tmp_path, ScriptedModel(text_session()))
    assert result['status'] == 'failed' and 'mechanical admission failed' in result['error']
    admission = json.loads((tmp_path / 'out/sessions/learner/control/mechanical-admission.json').read_text())
    assert admission['operation_provenance_matches'] is False and admission['diff']['added'] == ['EXTRA.md',
                                                                                                 'notes/latest_printing.json']
    rows = [json.loads(l) for l in (root / LC.LEDGER_FILE).read_text().splitlines()]
    assert rows and all(r['action'] == 'void' and not r['admitted'] for r in rows)
    assert not (root / LC.STATE_FILE).exists()


# ---------------------------------------------------------------------------------------------
# Mechanism learner
# ---------------------------------------------------------------------------------------------

LEARNED_RULE = {'id': 'learned_later_printing', 'when': [{'pred': 'later_printing', 'args': {}}],
                'finding': 'calc {compute.sha} uses {cell.locator} ({cell.value}); a later printing '
                           '{later.locator} shows {later.value}.',
                'offer': {'op': 'recompute', 'args': {}}, 'priority': 30}


def mech_ctx(root, tmp_path, block, runner, toy_corpus):
    w0 = mech_w0(tmp_path / f'w0-{block}', core_seed_rules())
    return LC.prepare_round(ML.SPEC, '', w0, root / 'evidence' / f'{block:02}', tmp_path / f'out-{block}', block,
                            pack=PACK, grader=GRADER, corpus=toy_corpus, w0=w0, runner=runner)


def test_rule_stats_equals_replay_fire_stats(mech_root, tmp_path, runner, toy_corpus):
    ctx = mech_ctx(mech_root, tmp_path, 6, runner, toy_corpus)
    episodes = list(ML.replay_episodes(ctx).values())
    assert len(episodes) == 30 and sum(e.label for e in episodes) == 21
    rules = core_seed_rules() + [C.Rule(**LEARNED_RULE)]
    reference = RP.fire_stats(rules, episodes, PACK, runner)
    for rule in rules:
        mine = ML.rule_stats(rule, episodes, PACK, runner)
        assert {k: mine[k] for k in reference[rule.id]} == reference[rule.id]
        assert mine['max_ms'] < ML.RULE_RUNTIME_MS
        assert mine['max_runner_ms'] >= 0
    # review: the runtime limit is engine time; sandboxed offer re-execution is measured separately
    learned = ML.rule_stats(C.Rule(**LEARNED_RULE), episodes, PACK, runner)
    assert learned['offers'] > 0 and learned['max_runner_ms'] > 0

    class SlowRunner:
        def run(self, code, *, timeout_s=5.0):
            import time as _t
            _t.sleep(0.3)
            return runner.run(code, timeout_s=timeout_s)
    slow = ML.rule_stats(C.Rule(**LEARNED_RULE), episodes[:10], PACK, SlowRunner())
    assert slow['max_runner_ms'] >= 300 and slow['max_ms'] < 300


def test_rule_admission_counterfactual_precision_leak_schema(mech_root, tmp_path, runner, toy_corpus):
    ctx = mech_ctx(mech_root, tmp_path, 6, runner, toy_corpus)
    rule = C.Rule(**LEARNED_RULE, origin='learned', admitted_block=6)
    verdict = ML.admit_rule(rule, [1, 7, 2], ctx)
    assert verdict['admitted'] and verdict['reasons'] == []
    cf = {r['ordinal']: r for r in verdict['counterfactual']}
    assert cf[1]['admit'] and cf[7]['admit'] and cf[1]['offer_right'] and cf[1]['silent_on_reference']
    assert not cf[2]['admit'] and 'episode is not labelled wrong' in cf[2]['reasons']
    assert verdict['precision']['ok']
    assert (verdict['precision']['n_right'], verdict['precision']['fired_right'], verdict['precision']['fired_wrong']) == (21, 0, 6)
    assert REFERENCES['toy01'] not in json.dumps(verdict)               # booleans and counts, no answer values
    noisy = C.Rule(id='noisy', when=[{'pred': 'not_from_compute', 'args': {'allow_observed': False}}],
                   finding='Your answer {value.text} is not a calc output.')
    verdict = ML.admit_rule(noisy, [1], ctx)
    assert not verdict['admitted'] and any(r.startswith('precision: right-episode fire rate bound') for r in verdict['reasons'])
    assert any(r.startswith('counterfactual') for r in verdict['reasons'])
    bad = C.Rule(id='bad', when=[{'pred': 'no_such_predicate', 'args': {}}], finding='x')
    assert "unknown predicate 'no_such_predicate'" in ML.admit_rule(bad, [1], ctx)['reasons']
    leaky = C.Rule(**{**LEARNED_RULE, 'finding': 'The North total should be 252.'})
    assert any(r.startswith('leak') for r in ML.admit_rule(leaky, [1], ctx)['reasons'])
    early = mech_ctx(mech_root, tmp_path, 1, runner, toy_corpus)              # 5 episodes: too few right ones
    verdict = ML.admit_rule(rule, [1], early)
    assert verdict['counterfactual'][0]['admit'] and not verdict['admitted']
    assert 'precision: only 4 right episodes (< 10)' in verdict['reasons']


def test_modify_rule_and_policy_edits(mech_root, tmp_path, runner, toy_corpus):
    ctx = mech_ctx(mech_root, tmp_path, 6, runner, toy_corpus)
    out, ok = ML.modify_rule({'id': 'recompute_final', 'patch': {'enabled': False}, 'rationale': 'noisy?'}, ctx)
    reply = json.loads(out)
    assert not ok and reply['mode'] == 'disable' and 'within 0.20' in reply['reasons'][0]
    assert reply['precision']['fired_right'] == 3 and reply['precision']['n_right'] == 21
    out, ok = ML.modify_rule({'id': 'grounded', 'patch': {'priority': 70}, 'rationale': 'advisory last'}, ctx)
    assert ok and json.loads(out)['mode'] == 'priority'
    out, ok = ML.set_policy({'key': 'loop_threshold', 'value': 9, 'rationale': 'x'}, ctx)
    assert not ok and 'loop_threshold must be an integer in [3, 6]' in json.loads(out)['reasons']
    out, ok = ML.set_policy({'key': 'loop_threshold', 'value': 3, 'rationale': 'x'}, ctx)
    assert not ok and 'loop_threshold is already 3' in json.loads(out)['reasons']
    out, ok = ML.set_policy({'key': 'loop_threshold', 'value': 4, 'rationale': 'fewer early loop notes'}, ctx)
    reply = json.loads(out)
    assert ok and reply['replay']['loop_guard']['episodes'] == 30 and reply['replay']['gate']['checks'] > 30
    out, ok = ML.propose_rule({'rule': LEARNED_RULE, 'targets': [1], 'rationale': 'x'}, ctx)
    assert not ok and 'at most 2 rule/policy edits' in json.loads(out)['reasons'][0]
    assert ctx.pending_policy() == {'loop_threshold': 4}
    assert {r.id: r.priority for r in ctx.pending_rules()}['grounded'] == 70
    assert [(r['kind'], r['admitted']) for r in ctx.ledger_rows] == [('rule', False), ('rule', True), ('policy', False),
                                                                      ('policy', False), ('policy', True)]


def test_policy_replay_gate_rounds(mech_root, tmp_path, runner, toy_corpus):
    ctx = mech_ctx(mech_root, tmp_path, 6, runner, toy_corpus)
    from harness_core.rules import effective_policy
    replay = ML.policy_replay(ctx, effective_policy({}), effective_policy({'gate_rounds_max': 0}))
    assert replay['gate']['shown_new'] == 0 and replay['gate']['shown_old'] > 0
    assert replay['gate']['status_changed'] == replay['gate']['shown_old']


def test_loop_guard_replay_counts_notes_and_escalations():
    def cmd(i, text, tool='exec_command'):
        return C.Command(kind='command', ref=f'x{i}', call=i, t=float(i), cmd=text, tool=tool)
    events = [cmd(1, 'rg foo'), cmd(2, 'rg foo'), cmd(3, 'rg foo'),
              C.Observe(kind='observe', ref='c1', call=3, t=3.0, locator='f:1', value=1.0),
              C.Candidate(kind='candidate', ref='a1', call=4, t=4.0, answer='1'),
              cmd(5, 'rg bar'), cmd(6, 'rg bar'), cmd(7, 'rg bar'), cmd(8, 'rg bar')] + \
        [cmd(10 + i, 'write_stdin {"session_id": 1}', tool='write_stdin') for i in range(6)]
    base = {**C.POLICY_DEFAULTS}
    assert ML.loop_guard_replay(events, base) == {'loop_notes': 2, 'escalations': 0}
    assert ML.loop_guard_replay(events, {**base, 'loop_threshold': 4}) == {'loop_notes': 1, 'escalations': 0}
    assert ML.loop_guard_replay(events, {**base, 'loop_notes_max': 1}) == {'loop_notes': 2, 'escalations': 1}


def mech_session():
    return [
        calls(tool_call('read_episode', ordinal=1, part='ledger'),
              tool_call('propose_rule', rule=LEARNED_RULE, targets=[1, 7], rationale='first printing used')),
        calls(tool_call('set_policy', key='loop_threshold', value=4, rationale='later loop notes'),
              tool_call('propose_rule', rule={**LEARNED_RULE, 'id': 'second'}, targets=[9], rationale='x'),
              tool_call('propose_note', id='quarter_price', triggers=['average price'],
                        text='Use the price of the quarter the question names, not a neighbouring quarter.',
                        support=[9], rationale='q9 used the Q2 price for Q1')),
        say('Report: one rule, one policy change, one note.'),
    ]


def test_mechanism_learner_end_to_end(mech_root, tmp_path, runner, toy_corpus):
    root = copy_root(mech_root, tmp_path)
    w0 = mech_w0(tmp_path / 'w0', core_seed_rules())
    refs = refs_of(ITEMS * 3)
    actor = FakeActor(refs, lambda q, notes: 'quarter_price' in notes or q % 10 not in (1, 7, 9))
    model = ScriptedModel(mech_session())
    result = ML.run_learning('', w0, root / 'evidence/06', tmp_path / 'out', 6, actor_runner=actor, pack=PACK,
                             grader=GRADER, model=model, w0=w0, corpus=toy_corpus, runner=runner)
    out = tmp_path / 'out'
    assert result['status'] == 'admitted' and result['activated'], result.get('error')
    assert (result['profile'], result['architecture']) == ('mech-evolve', 'mechanism')
    assert result['admitted'] == ['quarter_price', 'learned_later_printing', 'policy:loop_threshold']
    adapter = out / 'adapter'
    rule = json.loads((adapter / 'rules/learned_later_printing.json').read_text())
    assert rule['origin'] == 'learned' and rule['admitted_block'] == 6 and rule['scope'] == 'pack'
    assert rule['stats']['counterfactual_admits'] == [1, 7] and rule['stats']['fired_right'] == 0
    assert json.loads((adapter / 'policy.json').read_text()) == {'loop_threshold': 4}
    assert (adapter / 'notes/quarter_price.json').exists() and not (adapter / 'rules/second.json').exists()
    assert {r.id for r in load_rules(adapter / 'rules')} == {r.id for r in core_seed_rules()} | {'learned_later_printing'}
    # the note was verified alone on the source adapter (no rule admitted this block in the trial)
    assert all(c['rules'] == sorted(r.id for r in core_seed_rules()) for c in actor.calls)
    assert sorted({c['ordinal'] for c in actor.calls}) == [9, 18, 28]           # support 9; controls 28, 18
    admission = json.loads((out / 'sessions/learner/control/mechanical-admission.json').read_text())
    assert admission['operation_provenance_matches'] and admission['admitted_rules'] == ['learned_later_printing']
    assert admission['policy_changes'] == {'loop_threshold': 4}
    assert admission['rule_admission']['learned_later_printing']['precision']['n_right'] == 21
    assert admission['policy_replay']['loop_threshold']['replay']['loop_guard']['episodes'] == 30
    assert admission['diff'] == {'added': ['notes/quarter_price.json', 'rules/learned_later_printing.json'],
                                 'removed': [], 'changed': ['policy.json']}
    assert admission['text_budget']['controller'] > 0 and admission['text_budget']['ok']
    rows = [json.loads(l) for l in (root / LC.LEDGER_FILE).read_text().splitlines()]
    assert [(r['kind'], r.get('action'), r['admitted']) for r in rows] == \
        [('rule', 'add', True), ('policy', 'set', True), ('note', 'add', True)]
    prompt = (out / 'sessions/learner/control/prompt.md').read_text()
    assert '## Rule vocabulary' in prompt and 'later_printing' in prompt and 'loop_threshold = 3 (range 3-6)' in prompt
    assert '### Reference: packs.toy.pack' in prompt and 'Binding keys each predicate produces' in prompt
    assert 'fires on 3/21 right and 0/9 wrong' in prompt                      # recompute_final on the released episodes
    tools = json.loads((out / 'sessions/learner/control/fireworks-tools.json').read_text())
    assert [t['function']['name'] for t in tools][-4:] == ['propose_note', 'propose_rule', 'modify_rule', 'set_policy']


def test_mechanism_failure_after_admission_voids_the_rows(mech_root, tmp_path, runner, toy_corpus):
    root = copy_root(mech_root, tmp_path)
    w0 = mech_w0(tmp_path / 'w0', core_seed_rules())
    model = ScriptedModel([mech_session()[0], None])
    result = ML.run_learning('', w0, root / 'evidence/06', tmp_path / 'out', 6, actor_runner=FakeActor({}, None),
                             pack=PACK, grader=GRADER, model=model, w0=w0, corpus=toy_corpus, runner=runner)
    assert result['status'] == 'failed' and not result['activated'] and 'transport failed' in result['error']
    rows = [json.loads(l) for l in (root / LC.LEDGER_FILE).read_text().splitlines()]
    assert rows and all(r['action'] == 'void' for r in rows) and rows[0]['id'] == 'learned_later_printing'
    assert not (tmp_path / 'out/adapter').exists()


# ---------------------------------------------------------------------------------------------
# Equal inputs for the two learners
# ---------------------------------------------------------------------------------------------

def test_both_learners_see_identical_evidence(mech_root, tmp_path, toy_corpus, runner):
    reads = [tool_call('list_episodes'), tool_call('read_episode', ordinal=1, part='trajectory'),
             tool_call('read_episode', ordinal=1, part='ledger'), tool_call('read_episode', ordinal=3, part='summary'),
             tool_call('read_episode', ordinal=3, part='controller'), tool_call('verify_answer', ordinal=9, candidate='238')]
    outputs, requests = {}, {}
    for name, learner, w0 in (('text', TL, text_w0(tmp_path / 'w0-text')),
                              ('mech', ML, mech_w0(tmp_path / 'w0-mech', core_seed_rules()))):
        root = copy_root(mech_root, tmp_path, f'root-{name}')
        model = ScriptedModel([calls(*copy.deepcopy(reads)), say('no edit'), say('no edit')])
        result = learner.run_learning('', w0, root / 'evidence/06', tmp_path / name, 6, actor_runner=FakeActor({}, None),
                                      pack=PACK, grader=GRADER, model=model, w0=w0, corpus=toy_corpus, runner=runner)
        assert result['status'] == 'retained', result.get('error')
        records = sorted((tmp_path / name / 'sessions/learner/control/fireworks-tool-results').glob('*.json'))
        outputs[name] = [json.loads(p.read_text())['response'] for p in records]
        requests[name] = json.loads((tmp_path / name / 'request.json').read_text())
        assert result['budget'] == LC.asdict(LC.BUDGET) and result['reasoning_effort'] == LC.REASONING_EFFORT
        assert json.loads((tmp_path / name / 'evidence-index.json').read_text())['digest'] == requests[name]['evidence_digest']
    # review: each learner sees its own adapter's always-shown seed text in full (the mechanism learner also
    # sees every rule); the text learner used to see its W0 seed only through middle-cut AGENTS.md items
    text_prompt = (tmp_path / 'text/sessions/learner/control/prompt.md').read_text()
    mech_prompt = (tmp_path / 'mech/sessions/learner/control/prompt.md').read_text()
    assert PACK.seed_text().strip() in text_prompt and 'Check the answer count and the scale' in text_prompt
    assert 'Submit your answer with submit --answer.' in mech_prompt and PACK.seed_text().strip() not in mech_prompt
    assert outputs['text'] == outputs['mech'] and len(outputs['text']) == len(reads)
    assert requests['text']['evidence_digest'] == requests['mech']['evidence_digest']
    for key in ('episodes', 'excluded', 'pool', 'right', 'budget', 'reasoning_effort', 'model', 'k', 'keep'):
        assert requests['text'][key] == requests['mech'][key], key
    common = [t for t in requests['text']['tools']]
    assert requests['mech']['tools'][:len(common)] == common                # the mechanism tools are extra


# ---------------------------------------------------------------------------------------------
# Review (wave 2) regressions
# ---------------------------------------------------------------------------------------------

def test_a_rule_edited_twice_in_one_block_is_written_once(mech_root, tmp_path, runner, toy_corpus):
    """review: propose + modify of the same rule used to list rules/<id>.json as both added and changed, so the
    provenance check failed the whole round (and the campaign) on a legitimate edit sequence."""
    root = copy_root(mech_root, tmp_path)
    w0 = mech_w0(tmp_path / 'w0', core_seed_rules())
    model = ScriptedModel([
        calls(tool_call('propose_rule', rule=LEARNED_RULE, targets=[1, 7], rationale='first printing used')),
        calls(tool_call('modify_rule', id='learned_later_printing', patch={'priority': 35}, rationale='after count')),
        say('done')])
    result = ML.run_learning('', w0, root / 'evidence/06', tmp_path / 'out', 6, actor_runner=FakeActor({}, None),
                             pack=PACK, grader=GRADER, model=model, w0=w0, corpus=toy_corpus, runner=runner)
    assert result['status'] == 'admitted', result.get('error')
    assert result['admitted'] == ['learned_later_printing'] and result['edits_without_net_change'] == []
    rule = json.loads((tmp_path / 'out/adapter/rules/learned_later_printing.json').read_text())
    assert rule['priority'] == 35 and rule['origin'] == 'learned' and rule['stats']['modified_block'] == 6
    admission = json.loads((tmp_path / 'out/sessions/learner/control/mechanical-admission.json').read_text())
    assert admission['operation_provenance_matches'] and admission['expected_diff']['added'] == \
        ['rules/learned_later_printing.json'] and admission['expected_diff']['changed'] == []
    assert [e['action'] for e in admission['edits']] == ['add', 'modify']


def test_edits_that_cancel_out_change_nothing(mech_root, tmp_path, runner, toy_corpus):
    """review: set_policy A->B then B->A used to fail the round (policy.json rewritten byte-identically but
    listed as changed); the net effect is now computed and the round is 'retained'."""
    root = copy_root(mech_root, tmp_path)
    w0 = mech_w0(tmp_path / 'w0', core_seed_rules())
    model = ScriptedModel([
        calls(tool_call('set_policy', key='loop_threshold', value=4, rationale='x')),
        calls(tool_call('set_policy', key='loop_threshold', value=3, rationale='back')),
        say('done')])
    result = ML.run_learning('', w0, root / 'evidence/06', tmp_path / 'out', 6, actor_runner=FakeActor({}, None),
                             pack=PACK, grader=GRADER, model=model, w0=w0, corpus=toy_corpus, runner=runner)
    assert result['status'] == 'retained' and not result['activated'], result.get('error')
    assert result['edits_without_net_change'] == ['policy:loop_threshold'] and result['admitted'] == []
    assert not (tmp_path / 'out/adapter').exists()
    admission = json.loads((tmp_path / 'out/sessions/learner/control/mechanical-admission.json').read_text())
    assert admission['policy_changes'] == {} and admission['operation_provenance_matches']


def test_write_revision_lists_an_identical_rewrite_as_no_change(tmp_path):
    w0 = mech_w0(tmp_path / 'w0', core_seed_rules())
    same = next(r for r in core_seed_rules() if r.id == 'grounded')
    expected = LC.write_revision(w0, tmp_path / 'rev', rules=[same], policy={})
    assert expected == {'added': [], 'removed': [], 'changed': []}
    assert LC.tree_diff(EV.snapshot(w0), EV.snapshot(tmp_path / 'rev')) == expected
    with pytest.raises(LC.RoundFailure, match='written twice'):
        LC.write_revision(w0, tmp_path / 'rev2', rules=[same, same])


def test_a_verification_runner_that_always_fails_is_loud(text_root, tmp_path):
    """review: every verification job ending in launch_error used to give a quiet 'retained' round."""
    root = copy_root(text_root, tmp_path)
    w0 = text_w0(tmp_path / 'w0')

    def broken(adapter, task, run_dir):
        raise ValueError('a verification adapter is the source or a candidate inside this round')
    result = TL.run_learning('', w0, root / 'evidence/02', tmp_path / 'out', 2, actor_runner=broken, pack=PACK,
                             grader=GRADER, model=ScriptedModel(text_session()), w0=w0,
                             corpus=LC.TextDirCorpus(make_corpus(tmp_path / 'corpus')))
    assert result['status'] == 'failed' and 'verification actor runner failed on every job (8)' in result['error']
    assert 'a verification adapter is the source' in result['error'] and not (root / LC.STATE_FILE).exists()
    assert not (root / LC.LEDGER_FILE).exists() and not (tmp_path / 'out/adapter').exists()
    assert [s['ok'] for s in json.loads((tmp_path / 'out/screen.json').read_text())] == [False, False, True]


def test_note_and_rule_ids_are_leak_screened(text_root, mech_root, tmp_path, runner, toy_corpus):
    """review: ids name notes/<id>.json and rules/<id>.json in the adapter directory, which the actor sandbox
    can read, so an id must pass the same leak screen as the text."""
    w0 = text_w0(tmp_path / 'w0')
    ctx = LC.prepare_round(TL.SPEC, '', w0, text_root / 'evidence/02', tmp_path / 'out', 2, pack=PACK, grader=GRADER,
                           corpus=toy_corpus, w0=w0)
    out, ok = LC.propose_note({**NOTE_ARGS, 'id': 'north_total_252'}, ctx)
    assert not ok and any(r.startswith('leak') for r in json.loads(out)['reasons'])
    out, ok = LC.propose_note(NOTE_ARGS, ctx)
    assert ok, out
    mctx = mech_ctx(mech_root, tmp_path, 6, runner, toy_corpus)
    verdict = ML.admit_rule(C.Rule(**{**LEARNED_RULE, 'id': 'answer_252'}), [1], mctx)
    assert any(r.startswith('leak') for r in verdict['reasons'])
