"""v10 learners (S2): evidence builder, shared session/verification/retirement, the prompt (notes-only) learner
and the harness (notes + tool revisions) learner, the staging screens, the recorded-command replay and the
paired re-run admission of a revision.

Offline and $0: the learner model is tests/fake_learner_model.ScriptedModel, the actor re-runs are a fake
actor_runner, the grader is the toy grader, the corpus is the toy corpus, the tool runner is the real
harness_core.runner.ToolRunner over the toy W0 tools (lookup.py). Episodes are produced by the toy simulator
(packs/toy/sim.py) driven through a recorder that writes the broker's api/NNN records, so the evidence builder
reads exactly the file layout a real campaign root has; harness episodes also write the real Ledger.
"""
import ast
import copy
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import shutil
import stat
import sys
import threading

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness_core import contracts as C  # noqa: E402
from harness_core.gate import Gate  # noqa: E402
from harness_core.ledger import Ledger  # noqa: E402
from harness_core.rules import core_seed_rules  # noqa: E402
from harness_core.runner import SandboxRunner, ToolRunner  # noqa: E402
from packs.toy import PACK, REFERENCES, grade  # noqa: E402
from packs.toy.corpus import make_corpus  # noqa: E402
from packs.toy.sim import QUESTIONS, run_item  # noqa: E402

import arms  # noqa: E402
import evidence as EV  # noqa: E402
import harness_learning as HL  # noqa: E402
import learner_common as LC  # noqa: E402
import prompt_learning as PL  # noqa: E402
from fake_learner_model import (MODEL, PRIVATE_REASONING, ScriptedModel, calls, runaway, say,  # noqa: E402
                                tool_call)

ITEMS = sorted(QUESTIONS)                                   # toy01 .. toy10
GRADER = EV.Grader(score=lambda reference, answer, tolerance=0.0: grade(answer, reference), name='toy grader')
V8_NONE = ROOT.parent / 'officeqa-pro133-none-v8-work' / 'runs' / 'baseline'
CARDS_BLOCK = ROOT.parent / 'frameworks-v8/next-a48c0f9/harnessadapter/evolvers/cards/block.py'
PROMPT_ARM = arms.parse_arm('prompt-evolve', pack='toy')
HARNESS_ARM = arms.parse_arm('harness-evolve', pack='toy')
NOTES_ARM = arms.parse_arm('harness-evolve-notes', pack='toy')
pytestmark = pytest.mark.skipif(not Path('/usr/bin/sandbox-exec').exists(), reason='macOS sandbox-exec required')


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


def make_campaign(root, items, *, ledger=False, gate=False, rules=None, runner=None, block_size=5, notes_served=None):
    """ledger=True: harness episodes (the real Ledger written, no gate = the v10 default); gate=True: the v9
    gated mechanism (rules + Gate) for the ledger-view test."""
    root = Path(root)
    episodes = []
    for ordinal, item in enumerate(items, 1):
        run = root / 'runs' / 'adapted' / f'{ordinal:03}'
        api = run / 'api'
        api.mkdir(parents=True)
        if ledger or gate:
            led = Ledger(PACK, api / 'ledger.jsonl')
            gt = Gate(PACK, rules, {}, runner, QUESTIONS[item], api_dir=api) if gate else None
        else:
            led, gt = Ledger(PACK, None), None
        sim = run_item(item, Recorder(led, api), gt)
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


def prompt_w0(path, notes=()):
    """The toy prompt W0 (arms.build_w0, as `arms.py build --pack toy`), plus learned notes when given."""
    path = Path(path)
    arms.build_w0('text', PACK, path)
    for note in notes:
        LC.save_note(path / 'notes', note)
    return path


def harness_w0(path, notes=()):
    path = Path(path)
    arms.build_w0('harness', PACK, path)
    for note in notes:
        LC.save_note(path / 'notes', note)
    return path


class FakeActor:
    """actor_runner stand-in: right when the scenario says so for this (adapter, question), else wrong.
    right_when(ordinal, note ids of the adapter, adapter dir)."""

    def __init__(self, references, right_when):
        self.references, self.right_when = references, right_when
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, adapter, task, run_dir):
        run_dir.mkdir(parents=True)
        notes = sorted(p.stem for p in (Path(adapter) / 'notes').glob('*.json'))
        with self.lock:
            self.calls.append({'adapter': str(adapter), 'ordinal': task['ordinal'], 'notes': notes})
        right = self.right_when(task['ordinal'], notes, Path(adapter))
        return {'status': 'completed', 'answer': self.references[task['ordinal']] if right else 'wrong',
                'out_tokens': 1200, 'transport_failed': False, 'elapsed_seconds': 1.0, 'requests': 3}


def refs_of(items):
    return {o: REFERENCES[item] for o, item in enumerate(items, 1)}


def even(q, notes, adapter):
    return q % 2 == 0


def runner_factory(tmp_path):
    """The campaign's tool_runner keyword: tools_dir -> ToolRunner (toy: no corpus / index environment)."""
    return lambda tools_dir: ToolRunner(tools_dir, tmp_root=tmp_path / 'tool-tmp')


@pytest.fixture(scope='module')
def runner():
    return SandboxRunner()


@pytest.fixture(scope='module')
def toy_corpus(tmp_path_factory):
    return LC.TextDirCorpus(make_corpus(tmp_path_factory.mktemp('corpus')))


@pytest.fixture(scope='module')
def text_root(tmp_path_factory):
    """10 prompt-arm episodes (no ledger) in 2 blocks; wrong: 1, 3, 5, 7, 9."""
    return make_campaign(tmp_path_factory.mktemp('text') / 'root', ITEMS)


@pytest.fixture(scope='module')
def harness_root(tmp_path_factory):
    """10 harness-arm episodes (ledger, gate off) in 2 blocks; wrong: 1, 3, 5, 7, 9."""
    return make_campaign(tmp_path_factory.mktemp('harness') / 'root', ITEMS, ledger=True)


@pytest.fixture(scope='module')
def gated_root(tmp_path_factory, runner):
    """10 gated episodes (core seed rules, the v9 mechanism kept for gate:true adapters): the ledger view."""
    return make_campaign(tmp_path_factory.mktemp('gated') / 'root', ITEMS, gate=True, rules=core_seed_rules(),
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
              _msg('user', '[runtime:ledger] Ledger (recorded by the harness ...)')]
    write_call(api, 4, resume, [_fc('c3', 'rg total f.txt')],
               inserted=[{'public_index': 4, 'reason': 'ledger', 'item': None}])
    write_call(api, 5, resume + [_fc('c3', 'rg total f.txt'), _fco('c3', 'Process exited with code 1\nOutput:\nf.txt:9: total 42')],
               [_reasoning(), _msg('assistant', 'Final answer: 42')])
    text, counts, commands = EV.model_view_with_commands(api)
    assert (text, counts) == EV.model_view(api)
    assert counts == {'calls': 5, 'compactions': 1, 'final_call': 5, 'controller_calls': 0, 'harness_notes': {'ledger': 1}}
    assert text.count('DEVELOPER INSTRUCTIONS') == 1 and 'SYSTEM SYSTEM' not in text     # omitted, and once only
    assert text.count('USER: # AGENTS.md') == 1 and text.count('USER: Question: what is x?') == 1
    assert text.count('CONTEXT COMPACTION') == 1 and text.count("SUMMARY (the model's own hand-off") == 1
    assert 'HARNESS NOTE (ledger): [runtime:ledger] Ledger' in text
    assert 'ENCRYPTED-SECRET' not in text and 'reasoning' not in text.lower()
    order = [text.index(s) for s in ('cat big.txt', 'CONTEXT COMPACTION', 'SUMMARY (', 'rg total', 'FINAL MESSAGE: Final answer: 42')]
    assert order == sorted(order)
    out_line = next(l for l in text.split('\n[#') if l.split('] ', 1)[-1].startswith('OUTPUT: Output:\nAAA'))
    assert '[' in out_line and 'chars omitted]' in out_line and out_line.endswith('Z' * 500)
    body = out_line.split('OUTPUT: ', 1)[1]
    assert body.startswith('Output:\n' + 'A' * (EV.OUT_HEAD - 8)) and len(body) < EV.OUT_HEAD + EV.OUT_TAIL + 60
    cmd_line = next(l for l in text.split('\n') if 'COMMAND (exec_command): python3' in l)
    assert 'chars omitted' in cmd_line and len(cmd_line.split(': ', 1)[1]) < EV.CMD_HEAD + EV.CMD_TAIL + 60
    # v10: the exit code Codex printed is on the OUTPUT line, and the command list carries the full output
    assert 'OUTPUT (exit 1): Process exited with code 1' in text
    assert [(c['raw_cmd'], c['exit_code'], c['output'] is not None) for c in commands] == \
        [('cat big.txt', None, True), (long_cmd, None, True), ('rg total f.txt', 1, True)]
    assert commands[0]['output'] == big and commands[0]['output_step'] == commands[0]['step'] + 1


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
    requery = h + [_msg('assistant', 'Answer: 41'), _msg('user', '[runtime:enforce] Your final answer was not submitted')]
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
    assert 'OUTPUT (exit 0): Chunk ID' in traj and 'OUTPUT (exit 2): ' in traj        # submit without a route: exit 2
    with pytest.raises(ValueError, match='page must be'):
        ev.read(1, 'trajectory', 9)
    index = json.dumps(ev.index())
    assert '252.00' not in index and 'official_answer' not in index               # controller index: no references
    assert EV.build_evidence(text_root, 1, grader=GRADER).ordinals() == [1, 2, 3, 4, 5]  # only released blocks
    # the controller-side command list: one record per COMMAND line, outputs in full, exit codes
    commands = ev.episodes[1].commands
    assert [c['raw_cmd'].split()[0] for c in commands] == ['lookup', 'calc', 'submit']
    assert [c['exit_code'] for c in commands] == [0, 0, 2] and all(c['output'] for c in commands)
    assert ev.episodes[1].command_at(commands[0]['step']) is commands[0]
    assert ev.episodes[1].command_at(commands[0]['output_step']) is commands[0]
    assert 'commands' not in ev.episodes[1].model_visible()


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


def test_ledger_view_of_a_gated_episode(gated_root):
    ev = EV.build_evidence(gated_root, 1, grader=GRADER)
    ledger = ev.read(3, 'ledger')['content']                  # toy03: scale finding, offer accepted
    assert 'Candidates:' in ledger and '"14.77" via submit' in ledger and '"0.1477" via submit' in ledger
    assert 'fired scale' in ledger and 'offer "0.1477"' in ledger
    assert '120.5 = c1 widgets_2020_a.txt:5' in ev.read(1, 'ledger')['content']   # calc input linked to its cell
    assert ev.episodes[3].has_ledger and ev.episodes[3].counts['harness_notes'] == {}


def test_friction_digest_counts_per_tool(harness_root):
    ev = EV.build_evidence(harness_root, 2, grader=GRADER)
    manifest = arms.w0_manifest(PACK)
    digest = EV.friction_digest([ev.episodes[o] for o in (1, 5, 8)], lambda raw: LC.tool_invocations(raw, manifest))
    by = {d['ordinal']: d for d in digest}
    assert by[1]['tools']['lookup']['invocations'] == 1 and by[5]['tools']['lookup']['invocations'] == 2
    assert 'calc' in by[1]['tools'] and by[1]['tools']['calc']['errors'] == [] and by[1]['strict'] == 0
    assert by[8]['tools']['lookup']['no_match'] == 0 and by[8]['repeats'] == 0
    text = EV.render_friction(digest)
    assert text.startswith('q001 (block 1, strict 0;') and 'lookup: 2 calls' in text
    assert EV.exit_code_of('Chunk ID: x\nWall time: 1\nProcess exited with code 2\nOutput:\nx') == 2
    assert EV.first_error_line('lookup: error: bad thing\nmore') == 'lookup: error: bad thing'


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
        body = line.split('] OUTPUT', 1)
        if len(body) == 2:
            assert len(body[1].split(': ', 1)[-1].split('\n=== call')[0]) <= EV.OUT_LIMIT + 60
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


def test_judge_revision_records_cost_and_enforces_strict_controls():
    def run(q, role, k, strict, tokens=100):
        return {'q': q, 'role': role, 'k': k, 'strict': strict, 'out_tokens': tokens, 'excluded': False}
    runs = [run(1, 'support', 1, 1, 900), run(1, 'support', 2, 1, 900), run(5, 'keep', 1, 1), run(5, 'keep', 2, 0)]
    originals = {1: 100, 5: 100}
    assert not LC.judge(runs, originals)['admitted']                              # cost x5 fails a note
    verdict = LC.judge_revision(runs, originals, strict_controls=[])
    assert verdict['admitted'] and verdict['cost_gating'] is False and verdict['tokens_ratio'] == 5.0
    verdict = LC.judge_revision(runs, originals, strict_controls=[5])              # q5 missed one of its runs
    assert not verdict['admitted'] and not verdict['strict_ok'] and 'replay-changed control missed a run [5]' in verdict['reason']


def test_issue47_list_is_the_cards_list():
    tree = ast.parse((ROOT / 'cards_learning.py').read_text())
    value = next(node.value for node in tree.body if isinstance(node, ast.Assign)
                 and any(getattr(t, 'id', '') == 'EXCLUDED_TASKS' for t in node.targets))
    assert tuple(ast.literal_eval(value)) == LC.OFFICEQA_ISSUE47 and len(LC.OFFICEQA_ISSUE47) == 18
    assert LC.default_exclude(PACK) == ()
    from packs.officeqa.pack import OfficeQAPack
    assert LC.default_exclude(OfficeQAPack()) == LC.OFFICEQA_ISSUE47


def test_text_budget_counts_seed_notes_usage_and_templates(tmp_path):
    w0 = prompt_w0(tmp_path / 'w0')
    seed, source = LC.seed_text(w0, PACK, 'prompt')
    assert source == 'SEED.md' and seed == PACK.seed_text_prompt()
    note = C.NoteSpec(id='n', triggers=['debt'], text='x' * 500)
    budget = LC.text_budget(seed, [note] * 3, core_seed_rules(), LC.controller_template_bytes(), 'usage text')
    assert budget['notes'] == 3 * len(LC.render_note(note).encode()) and budget['usage'] == len(b'usage text')
    assert budget['rules'] == sum(len(r.finding.encode()) for r in core_seed_rules())
    assert 100 < budget['controller'] < 1000 and budget['ok']
    assert not LC.text_budget('s' * C.TEXT_BUDGET, [note], [])['ok']
    (w0 / 'SEED.md').unlink()
    assert LC.seed_text(w0, PACK, 'prompt') == (PACK.seed_text_prompt(), 'pack.seed_text_prompt() (SEED.md absent)')
    assert LC.seed_text(w0, PACK, 'harness')[0] == PACK.seed_text_harness()
    # the admission formula equals what arms serves and the templates equal arms' count (DESIGN §3)
    hw0 = harness_w0(tmp_path / 'hw0', [C.NoteSpec(id='n1', triggers=['debt'], text='check', admitted_block=1, origin='learned')])
    visible = arms.model_visible(hw0, HARNESS_ARM)
    mine = LC.text_budget((hw0 / 'SEED.md').read_text(), LC.load_notes(hw0), [], LC.controller_template_bytes(),
                          (hw0 / 'tools/USAGE.md').read_text())
    assert mine['total'] == visible['total'] and mine['usage'] == visible['usage'] > 0
    assert LC.controller_template_bytes() == sum(arms.harness_fixed_text()[k] for k in arms.CONTROLLER_COUNTED)


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


def test_cost_bound_per_block_from_the_constants():
    """DESIGN scope decision 8: notes <= 20 runs + revision <= 14 runs = 34 verification actor runs."""
    notes = LC.B_MAX_NOTES * (LC.SUPPORT_MAX + LC.KEEP) * LC.K
    revision = LC.B_MAX_REVISIONS * (LC.SUPPORT_MAX + LC.KEEP_VALUE_CHANGING) * LC.K
    assert (notes, revision, notes + revision) == (20, 14, 34) and LC.SUPPORT_MAX == C.SUPPORT_MAX == 3


# ---------------------------------------------------------------------------------------------
# Prompt (notes-only) learner
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
              tool_call('read_adapter_file', path='SEED.md'), tool_call('read_adapter_file', path='tools/lookup.py'),
              tool_call('read_tool_output', ordinal=1, step=3),
              tool_call('read_episode', ordinal=3, part='nonsense'), tool_call('no_such_tool')),
        calls(tool_call('propose_note', **{**NOTE_ARGS, 'text': 'The North total is 252 here.'}),   # fixed below, same id
              tool_call('propose_note', **{**NOTE_ARGS, 'id': 'outside', 'support': [2]}),
              tool_call('propose_note', **{**NOTE_ARGS, 'id': 'names_tool', 'text': 'Run lookup on the later file.'}),
              tool_call('propose_note', **NOTE_ARGS)),
        say('Report: first printings were used where a revision existed; one note proposed.'),
    ]


def run_text(root, tmp_path, model, *, fixes=True, name='out', **options):
    w0 = prompt_w0(tmp_path / f'w0-{name}')
    refs = refs_of(ITEMS)
    actor = FakeActor(refs, lambda q, notes, adapter: (fixes and 'latest_printing' in notes) or q in (2, 4, 6, 8, 10))
    options.setdefault('corpus', LC.TextDirCorpus(make_corpus(tmp_path / f'corpus-{name}')))
    result = PL.run_learning('', w0, root / 'evidence/02', tmp_path / name, 2, actor_runner=actor, pack=PACK,
                             grader=GRADER, model=model, w0=w0, **options)
    return result, actor, w0


def test_prompt_learner_admits_a_verified_note(text_root, tmp_path):
    root = copy_root(text_root, tmp_path)
    model = ScriptedModel(text_session())
    result, actor, w0 = run_text(root, tmp_path, model)
    out = tmp_path / 'out'
    for key in ('status', 'activated', 'candidate', 'block', 'model', 'profile', 'architecture', 'usage', 'session'):
        assert key in result
    assert result['status'] == 'admitted' and result['activated'] and result['candidate'] == str(out / 'adapter')
    assert (result['profile'], result['architecture'], result['model'], result['block']) == ('prompt-evolve', 'prompt', MODEL, 2)
    assert result['reasoning_effort'] == 'high' and result['learner_status'] == 'completed'
    assert result['usage'] == {'prompt_tokens': 3000, 'completion_tokens': 600, 'cached_tokens': 0}
    assert result['admitted'] == ['latest_printing'] and result['verification_episodes'] == 8
    assert json.loads((out / 'result.json').read_text()) == result
    note = json.loads((out / 'adapter/notes/latest_printing.json').read_text())
    assert note['admitted_block'] == 2 and note['origin'] == 'learned' and note['triggers'] == ['widgets shipped']
    assert note['stats']['support'] == [1, 7] and note['stats']['keep'] == [4, 10]
    # the new revision: adapter.json carries arms.revision(tree), every file is read-only, the arm accepts it
    candidate = out / 'adapter'
    assert json.loads((candidate / 'adapter.json').read_text())['revision'] == arms.revision(candidate) == result['revision']
    assert not any(p.stat().st_mode & 0o222 for p in candidate.rglob('*') if p.is_file())
    assert arms.check_adapter(candidate, PROMPT_ARM)['notes'] == 1
    # verification: the note alone on the source, support x2 + controls x2
    assert sorted((c['ordinal'], tuple(c['notes'])) for c in actor.calls) == \
        sorted([(q, ('latest_printing',)) for q in (1, 1, 7, 7, 4, 4, 10, 10)])
    assert all(c['adapter'].endswith('verify/latest_printing/adapter') for c in actor.calls)
    # screen: leak, pool and prompt-adapter tool-name refusals, answered at once
    screen = json.loads((out / 'screen.json').read_text())
    assert [s['ok'] for s in screen] == [False, False, False, True]
    assert any('leak' in r for r in screen[0]['reasons']) and 'support outside the pool: 2' in screen[1]['reasons']
    assert any("must not name the harness tools ['lookup']" in r for r in screen[2]['reasons'])
    # admission record: real values
    admission = json.loads((out / 'sessions/learner/control/mechanical-admission.json').read_text())
    assert admission['admitted'] and admission['operation_provenance_matches'] is True
    assert admission['diff'] == {'added': ['notes/latest_printing.json'], 'removed': [], 'changed': ['adapter.json']}
    assert admission['protected_unchanged'] == {'source': True, 'evidence': True} and admission['text_budget']['ok']
    assert admission['admitted_revision'] is None and admission['screen_version'] == LC.screen_version()
    # durable state and ledger
    state = json.loads((root / LC.STATE_FILE).read_text())
    assert state['claimed'] == {'1': 'latest_printing', '7': 'latest_printing'} and state['adapter_since'] == 3
    rows = [json.loads(l) for l in (root / LC.LEDGER_FILE).read_text().splitlines()]
    assert [(r['kind'], r['action'], r['admitted']) for r in rows] == \
        [('note', 'add', True)] + [('note', 'screen_rejected', False)] * 3
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
    assert '1: # Working advice' in json.loads(by_name['read_adapter_file'][0]['response'])['content']
    assert not by_name['read_adapter_file'][1]['ok']                      # the prompt learner reads no tools/
    output = json.loads(by_name['read_tool_output'][0]['response'])
    assert output['exit_code'] == 0 and output['command'].startswith('lookup widgets_2020_a.txt North')
    assert '⟦toy widgets_2020_a.txt:5|Q1 2020|North|120.5⟧' in output['content']
    # the prompt: common text, pool, the task conventions; no key-derived ladder
    prompt = (out / 'sessions/learner/control/prompt.md').read_text()
    assert 'Pool (wrong under the current adapter' in prompt and PACK.task_conventions().strip() in prompt
    assert 'must not name find_tables' in prompt and 'Ladder' not in prompt and 'shelf' not in prompt
    request = json.loads((out / 'request.json').read_text())
    assert request['evidence_digest'] == EV.build_evidence(root, 2, grader=GRADER).digest
    assert request['pool'] == [1, 3, 5, 7, 9] and request['right'] == [2, 4, 6, 8, 10]
    assert request['architecture'] == 'prompt' and request['support_max'] == 3


def test_prompt_learner_rejects_a_note_that_fails_verification(text_root, tmp_path):
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
    w0 = prompt_w0(tmp_path / 'w0')
    refs = refs_of(items)
    actor = FakeActor(refs, lambda q, notes, adapter: 'latest_printing' in notes or q % 2 == 0)   # controls are the even (right) ones
    corpus = LC.TextDirCorpus(make_corpus(tmp_path / 'corpus'))
    common = dict(actor_runner=actor, pack=PACK, grader=GRADER, w0=w0, corpus=corpus)
    first = PL.run_learning('', w0, root / 'evidence/01', tmp_path / 'evolution/01', 1,
                            model=ScriptedModel([calls(tool_call('propose_note', **{**NOTE_ARGS, 'support': [1]}))]),
                            **common)
    assert first['status'] == 'admitted' and first['candidate'] == str((tmp_path / 'evolution/01/adapter').resolve())
    second = PL.run_learning('', first['candidate'], root / 'evidence/02', tmp_path / 'evolution/02', 2,
                             model=ScriptedModel([say('no note'), say('no note')]), profile='prompt-evolve-r2', **common)
    assert second['status'] == 'retained' and second['candidate'] == first['candidate']
    assert second['profile'] == 'prompt-evolve-r2'
    request = json.loads((tmp_path / 'evolution/02/request.json').read_text())
    assert request['source_lineage']['kind'] == 'previous_candidate'
    assert request['adapter_since'] == 2 and request['pool'] == [7, 9] and request['stale'] == [3, 5]
    prompt = (tmp_path / 'evolution/02/sessions/learner/control/prompt.md').read_text()
    assert 'Earlier learner report (block 1' in prompt and '- latest_printing (admitted block 1' in prompt
    note = Path(first['candidate']) / 'notes/latest_printing.json'
    note.chmod(0o644)                                # the revision is read-only; tamper with it explicitly
    note.write_text(note.read_text().replace('revised printing', 'first printing'))
    third = PL.run_learning('', first['candidate'], root / 'evidence/03', tmp_path / 'evolution/03', 3,
                            model=ScriptedModel([say('no note')]), **common)
    assert third['status'] == 'failed' and "previous activated candidate" in third['error']


def test_retirement_removes_an_underperforming_note(tmp_path):
    items = ITEMS + ITEMS[:5]                        # 15 episodes, 3 blocks; wrong: 1 3 5 7 9 11 13 15
    served = {7: ['by_region'], 9: ['by_region'], 11: ['by_region'], 13: ['by_region'], 6: ['kept_note'],
              8: ['kept_note'], 12: ['kept_note']}
    root = make_campaign(tmp_path / 'root', items, notes_served=served)
    notes = [C.NoteSpec(id='by_region', triggers=['region'], text='Check the region row.', admitted_block=1, origin='learned'),
             C.NoteSpec(id='kept_note', triggers=['shipments'], text='Check the quarter.', admitted_block=1, origin='learned')]
    w0 = prompt_w0(tmp_path / 'w0', notes)
    model = ScriptedModel([say('no note'), say('no note: nothing generalises')])
    result = PL.run_learning('', w0, root / 'evidence/03', tmp_path / 'out', 3, actor_runner=FakeActor({}, None),
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
    assert model.requests[1]['messages'][-1]['content'] == PL.NUDGE                   # one nudge, then accepted


def test_retirement_derives_served_notes_when_unrecorded(tmp_path):
    root = make_campaign(tmp_path / 'root', ITEMS)
    ev = EV.build_evidence(root, 2, grader=GRADER)
    note = C.NoteSpec(id='quarters', triggers=['q2 2020'], text='x', admitted_block=0, origin='learned')
    [stat_] = LC.retirement([note], ev)
    assert stat_['served_source'] == ['derived'] and stat_['served'] == 3              # toy01, toy04, toy07 name Q2 2020


def test_prompt_learner_on_a_harness_adapter_may_name_tools(harness_root, tmp_path):
    """harness-evolve-notes: the notes-only learner on the harness W0 (same tools, controls, SEED, USAGE.md)."""
    root = copy_root(harness_root, tmp_path)
    w0 = harness_w0(tmp_path / 'w0')
    actor = FakeActor(refs_of(ITEMS), lambda q, notes, adapter: 'names_tool' in notes or q % 2 == 0)
    model = ScriptedModel([calls(tool_call('propose_note', **{**NOTE_ARGS, 'id': 'names_tool',
                                                               'text': 'Run lookup on the later file too.'})),
                           say('done')])
    result = PL.run_learning('', w0, root / 'evidence/02', tmp_path / 'out', 2, actor_runner=actor, pack=PACK,
                             grader=GRADER, model=model, w0=w0, corpus=LC.TextDirCorpus(tmp_path), arm=NOTES_ARM)
    assert result['status'] == 'admitted' and result['architecture'] == 'harness', result.get('error')
    candidate = Path(result['candidate'])
    assert arms.check_adapter(candidate, NOTES_ARM)['notes'] == 1
    assert arms.tree_files(candidate / 'tools') == arms.tree_files(w0 / 'tools')          # tools untouched
    request = json.loads((tmp_path / 'out/request.json').read_text())
    assert request['text_budget']['usage'] > 0 and request['text_budget']['controller'] > 0
    assert 'stage_tool_patch' not in request['tools'] and request['candidate_tools'] is None


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
        options['w0'] = prompt_w0(tmp_path / 'other-w0')
    elif case == 'corpus':
        options['corpus'] = None
    w0 = prompt_w0(tmp_path / 'w0')
    actor = FakeActor(refs_of(ITEMS), lambda q, notes, adapter: True)
    stop = options.pop('stop', None)
    corpus = options.pop('corpus', LC.TextDirCorpus(make_corpus(tmp_path / 'corpus')))
    result = PL.run_learning('secret-key-value', w0, root / 'evidence/02', tmp_path / 'out', 2, stop, actor_runner=actor,
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
        PL.run_learning('', w0, root / 'evidence/02', tmp_path / 'out', 2, actor_runner=actor, pack=PACK, grader=GRADER,
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


def test_write_revision_lists_an_identical_rewrite_as_no_change(tmp_path):
    w0 = harness_w0(tmp_path / 'w0')
    same = (w0 / 'tools/lookup.py').read_text()
    expected = LC.write_revision(w0, tmp_path / 'rev', tools={'tools/lookup.py': same}, usage=(w0 / 'tools/USAGE.md').read_text())
    assert expected == {'added': [], 'removed': [], 'changed': ['adapter.json']}         # only the revision hash
    assert LC.tree_diff(EV.snapshot(w0), EV.snapshot(tmp_path / 'rev')) == expected
    assert json.loads((tmp_path / 'rev/adapter.json').read_text())['revision'] == arms.revision(tmp_path / 'rev')
    with pytest.raises(LC.RoundFailure, match='written twice'):
        LC.write_revision(w0, tmp_path / 'rev2', tools={'tools/lookup.py': same, 'lookup.py': same})
    with pytest.raises(LC.RoundFailure, match='pinned'):
        LC.write_revision(w0, tmp_path / 'rev3', tools={'tools/calc.py': 'x'})
    with pytest.raises(LC.RoundFailure, match='path must match'):
        LC.write_revision(w0, tmp_path / 'rev4', tools={'tools/Bad-Name.py': 'x'})
    # a read-only revision is still copyable and editable in its copy (arms.copy_adapter gives fresh modes)
    expected = LC.write_revision(tmp_path / 'rev', tmp_path / 'rev5', add_notes=[C.NoteSpec(id='n1', triggers=['x'], text='t')])
    assert expected['added'] == ['notes/n1.json'] and (tmp_path / 'rev5/notes/n1.json').is_file()
    assert not (tmp_path / 'rev5/notes/n1.json').stat().st_mode & 0o222


def test_a_verification_runner_that_always_fails_is_loud(text_root, tmp_path):
    """review: every verification job ending in launch_error used to give a quiet 'retained' round."""
    root = copy_root(text_root, tmp_path)
    w0 = prompt_w0(tmp_path / 'w0')

    def broken(adapter, task, run_dir):
        raise ValueError('a verification adapter is the source or a candidate inside this round')
    result = PL.run_learning('', w0, root / 'evidence/02', tmp_path / 'out', 2, actor_runner=broken, pack=PACK,
                             grader=GRADER, model=ScriptedModel(text_session()), w0=w0,
                             corpus=LC.TextDirCorpus(make_corpus(tmp_path / 'corpus')))
    assert result['status'] == 'failed' and 'verification actor runner failed on every job (8)' in result['error']
    assert 'a verification adapter is the source' in result['error'] and not (root / LC.STATE_FILE).exists()
    assert not (root / LC.LEDGER_FILE).exists() and not (tmp_path / 'out/adapter').exists()
    assert [s['ok'] for s in json.loads((tmp_path / 'out/screen.json').read_text())] == [False, False, False, True]


def test_note_ids_are_leak_screened(text_root, tmp_path, toy_corpus):
    """review: ids name notes/<id>.json in the adapter directory, which the actor sandbox can read, so an id
    must pass the same leak screen as the text."""
    w0 = prompt_w0(tmp_path / 'w0')
    ctx = LC.prepare_round(PL.SPEC, '', w0, text_root / 'evidence/02', tmp_path / 'out', 2, pack=PACK, grader=GRADER,
                           corpus=toy_corpus, w0=w0)
    out, ok = LC.propose_note({**NOTE_ARGS, 'id': 'north_total_252'}, ctx)
    assert not ok and any(r.startswith('leak') for r in json.loads(out)['reasons'])
    out, ok = LC.propose_note(NOTE_ARGS, ctx)
    assert ok, out


# ---------------------------------------------------------------------------------------------
# Recorded commands -> tool invocations (replay jobs)
# ---------------------------------------------------------------------------------------------

MANIFEST = {'find_tables': {'script': 'cli.py', 'args': ['find_tables']}, 'show_table': {'script': 'cli.py', 'args': ['show_table']},
            'printings': {'script': 'cli.py', 'args': ['printings']}, 'calc': {'script': 'calc.py', 'args': []},
            'lookup': {'script': 'lookup.py', 'args': []}}


def test_tool_invocation_parses_the_recorded_command_shapes():
    inv = lambda raw: LC.tool_invocations(raw, MANIFEST)  # noqa: E731
    assert inv('show_table treasury_bulletin_2004_03.txt 728 --match "general services"') == \
        [('show_table', ['treasury_bulletin_2004_03.txt', '728', '--match', 'general services'])]
    assert inv('cd /ws && /ws/.kit/bin/find_tables "judicial branch" --years 2003-2004 | head -20') == \
        [('find_tables', ['judicial branch', '--years', '2003-2004'])]
    assert inv('python3 /code/tools/cli.py printings --table FFO-3 --row 2003-01 2>&1 | tail -5') == \
        [('printings', ['--table', 'FFO-3', '--row', '2003-01'])]
    assert inv('python -m packs.officeqa.tools.cli show_table f.txt 1 --stitch') == [('show_table', ['f.txt', '1', '--stitch'])]
    assert inv('python /ws/.kit/bin/show_table f.txt 1') == [('show_table', ['f.txt', '1'])]
    assert inv('OFFICEQA_CORPUS=/c env timeout 30 show_table f.txt 1; printings --table X --row 1995') == \
        [('show_table', ['f.txt', '1']), ('printings', ['--table', 'X', '--row', '1995'])]
    # loop bodies, unexpanded variables, pipelines after the tool and calc are dropped
    assert inv('for f in a.txt b.txt; do show_table $f 1; done') == []
    assert inv('for f in a.txt; do show_table a.txt 1; done | head') == []
    assert inv('show_table f.txt 1 | grep -c total; ls; rg foo') == [('show_table', ['f.txt', '1'])]
    assert inv("calc -c 'print(1)'\nshow_table f.txt 2\nshow_table f.txt 3") == \
        [('calc', ['-c', 'print(1)']), ('show_table', ['f.txt', '2']), ('show_table', ['f.txt', '3'])]
    assert inv("calc <<'PY'\nshow_table x 1\nPY\nshow_table f.txt 4") == [('calc', []), ('show_table', ['f.txt', '4'])]
    assert inv('lookup widgets_2020_a.txt North South') == [('lookup', ['widgets_2020_a.txt', 'North', 'South'])]
    assert inv("/bin/zsh -lc 'python3 lookup.py widgets_2020_b.txt East | head'") == [('lookup', ['widgets_2020_b.txt', 'East'])]
    assert inv("/bin/zsh -lc 'for r in North; do lookup a.txt $r; done'") == [] and inv("bash -c 'cat x'") == []
    assert inv('cat f.txt; sed -n 1,5p g.txt; python3 -c "print(1)"') == [] and inv('') == []
    assert LC.tool_invocation(['python', 'cli.py', 'unknown_tool', 'x'], MANIFEST) is None


def harness_ctx(root, tmp_path, block=2, *, name='ctx', notes=(), w0=None):
    w0 = w0 or harness_w0(tmp_path / f'w0-{name}', notes)
    return LC.prepare_round(HL.SPEC, '', w0, root / 'evidence' / f'{block:02}', tmp_path / name, block, pack=PACK,
                            grader=GRADER, corpus=LC.TextDirCorpus(tmp_path), w0=w0, tool_runner=runner_factory(tmp_path),
                            arm=HARNESS_ARM)


def test_replay_jobs_are_distinct_recent_recorded_invocations(harness_root, tmp_path):
    ctx = harness_ctx(harness_root, tmp_path)
    assert ctx.architecture == 'harness' and ctx.candidate_tools == tmp_path / 'ctx/candidate/tools'
    assert sorted(p.name for p in ctx.candidate_tools.iterdir()) == ['USAGE.md', 'calc.py', 'corpus.py', 'lookup.py', 'manifest.json']
    jobs = LC.replay_jobs(ctx)
    assert {j['tool'] for j in jobs} == {'lookup'} and len(jobs) == 11                 # calc skipped, argv de-duplicated
    first = jobs[0]
    assert first['argv'] == ['widgets_2021_a.txt', 'West'] and first['at'][0][0] == 10   # most recent first
    by_argv = {tuple(j['argv']): j for j in jobs}
    assert by_argv[('widgets_2020_a.txt', 'North')]['at'] == [[1, 3]] and by_argv[('widgets_2020_a.txt', 'North')]['wrong'] == [1]
    with pytest.raises(LC.RoundFailure, match='needs a tool runner'):
        LC.prepare_round(HL.SPEC, '', harness_w0(tmp_path / 'w0-x'), harness_root / 'evidence/02', tmp_path / 'x', 2,
                         pack=PACK, grader=GRADER, corpus=LC.TextDirCorpus(tmp_path), w0=tmp_path / 'w0-x')
    with pytest.raises(LC.RoundFailure, match="architecture 'prompt' is not one of"):
        LC.prepare_round(HL.SPEC, '', prompt_w0(tmp_path / 'w0-p'), harness_root / 'evidence/02', tmp_path / 'p', 2,
                         pack=PACK, grader=GRADER, corpus=LC.TextDirCorpus(tmp_path), w0=tmp_path / 'w0-p',
                         tool_runner=runner_factory(tmp_path))


# ---------------------------------------------------------------------------------------------
# Screens on staged files
# ---------------------------------------------------------------------------------------------

def test_w0_tool_files_restaged_unchanged_pass_with_zero_findings(harness_root, tmp_path):
    ctx = harness_ctx(harness_root, tmp_path)
    for name, text in arms.tree_files(ctx.source / 'tools').items():
        assert LC.code_leak_findings(text, text, name, PACK, ctx.evidence) == ([], []) if name.endswith('.py') else True
        if name == 'calc.py':
            assert LC.screen_staged(ctx, f'tools/{name}', text, new_file=False)[0] == \
                ['tools/calc.py is pinned to the W0 copy this round (its output cannot change)']
        else:
            assert LC.screen_staged(ctx, f'tools/{name}', text, new_file=False) == ([], []), name
    # the toy references as fresh code fail (delta rule: only new lines count)
    text = (ctx.source / 'tools/lookup.py').read_text()
    assert LC.changed_lines(text, text) == set() and LC.changed_lines(None, 'a\nb') == {1, 2}
    assert LC.changed_lines(text, text + '\nprint(1)\n') == {text.count('\n') + 2}
    # the OfficeQA W0 tool files pass too (the v9 whole-file screen refused them: file globs, stamps, budgets)
    from packs.officeqa.pack import OfficeQAPack
    for name, text in OfficeQAPack().w0_tools().files.items():
        assert LC.code_leak_findings(text, text, name, OfficeQAPack(), ctx.evidence) == ([], [])
        assert LC.code_leak_findings(text, None, name, OfficeQAPack(), ctx.evidence) != ([], []) or name == 'cli.py'


def test_planted_memorising_file_is_refused(harness_root, tmp_path):
    ctx = harness_ctx(harness_root, tmp_path)
    r1 = float(REFERENCES['toy01'])                      # 252.00: split arithmetic and a dict keyed on question words
    planted = (f'import sys\n\nANSWERS = {{"north": {r1 - 2} + 2, "west": 1.5}}\n\n'
               'def main():\n'
               f'    if "north" in sys.argv[1:]: print({int(r1) - 200} * 1 + 200)\n'
               '    print("The combined number of widgets shipped by the North region in Q1 2020 is known")\n'
               '    print("aGVsbG8gd29ybGQgdGhpcyBpcyBhIGxvbmcgYmFzZTY0IHRva2VuIGZvciB0ZXN0aW5n")\n'
               '    z = 987.65\n'
               '    return [4600, 1000, 7]\n\nmain()\n')
    reasons, reported = LC.code_leak_findings(planted, None, 'memo.py', PACK, ctx.evidence)
    assert any('agrees with released answer' in r for r in reasons)
    assert any('dict literal of numbers' in r for r in reasons) and any('list literal of numbers' in r for r in reasons)
    assert any('base64' in r for r in reasons)
    assert any('copies released question wording' in r for r in reasons)
    assert any('new constant' in r for r in reported)
    out, ok = LC.stage_tool_file({'path': 'tools/memo.py', 'content': planted}, ctx)
    assert not ok and any('leak' in r for r in json.loads(out)['reasons']) and not (ctx.candidate_tools / 'memo.py').exists()
    # identifiers and comments are screened too; years and small constants are not leaks
    reasons, _ = LC.code_leak_findings('x = 2020\ny = 100  # UID0007 episode\nq = north_total_252\n', None, 'a.py', PACK, ctx.evidence)
    assert any('question id' in r for r in reasons) and any('episode' in r for r in reasons)
    assert LC.code_leak_findings('x = 2020\ny = 100 + 1\n', None, 'a.py', PACK, ctx.evidence) == \
        ([], ['a.py line 2: new constant 100 + 1'])                                  # folded, reported, not refused
    assert LC.code_leak_findings('x = 2020\ny = 100\n', None, 'a.py', PACK, ctx.evidence) == ([], [])
    assert LC.agrees_with_reference(252.0, LC.reference_values(PACK, ctx.evidence)) == 0
    assert LC.agrees_with_reference(252.4, LC.reference_values(PACK, ctx.evidence)) == 0        # 3 significant digits
    assert LC.agrees_with_reference(25.2, LC.reference_values(PACK, ctx.evidence)) is None
    assert LC.agrees_with_reference(2021, LC.reference_values(PACK, ctx.evidence)) is None


def test_functional_python_screen(harness_root, tmp_path):
    ctx = harness_ctx(harness_root, tmp_path)

    def stage(content, path='tools/extra.py'):
        out, ok = LC.stage_tool_file({'path': path, 'content': content}, ctx)
        return ok, json.loads(out).get('reasons', [])
    ok, reasons = stage('def f(:\n    pass\n')
    assert not ok and 'does not compile' in reasons[0]
    ok, reasons = stage('import os\nexec("print(1)")\nx = eval("1")\n__import__("sys")\n')
    assert not ok and [r for r in reasons if 'exec()' in r] and [r for r in reasons if 'eval()' in r] and [r for r in reasons if '__import__()' in r]
    ok, reasons = stage('import builtins\nbuiltins.compile("1", "<s>", "eval")\n')
    assert not ok and 'compile()' in reasons[0]
    # a method of the same name on another object is not the builtin (W0's otables.py calls re.compile; the
    # preflight W0 idempotence check pins this)
    ok, reasons = stage('import re\nRX = re.compile("x")\n\nclass P:\n    def eval(self):\n        return 1\n\nP().eval()\n', path='tools/extra_ok.py')
    assert ok, reasons
    ok, reasons = stage('import socket, urllib.request\n')
    assert not ok and 'socket, urllib cannot be used' in reasons[0]
    ok, reasons = stage('import no_such_module_xyz\n')
    assert not ok and "no module ['no_such_module_xyz']" in reasons[0]
    ok, reasons = stage('from .nothing import x\n')
    assert not ok and 'names no sibling module' in reasons[0]
    for path, reason in (('tools/json.py', 'shadow the standard-library'), ('tools/Bad.py', 'path must be'),
                         ('tools/calc.py', 'pinned'), ('tools/harness_helper.py', 'reveals the arm'),
                         ('tools/lookup.py', 'exists; change an existing')):
        ok, reasons = stage('x = 1\n', path)
        assert not ok and reason in ' '.join(reasons), path
    ok, reasons = stage('x = 1\n' * 9000)
    assert not ok and 'at most 16384 bytes' in reasons[0]
    # a valid new module: sibling import, stdlib, numpy; staged and listed
    ok, reasons = stage('import argparse, json\nimport numpy\nfrom corpus import FILES\n\ndef names():\n    return sorted(FILES)\n')
    assert ok, reasons
    assert (ctx.candidate_tools / 'extra.py').is_file() and ctx.staged['tools/extra.py']['new'] is True
    assert json.loads(LC._read_adapter_file({'path': 'tools/extra.py'}, ctx)[0])['staged'] is True
    assert json.loads(LC._search_adapter_file({'path': 'tools/lookup.py', 'pattern': 'def lookup'}, ctx)[0])['matches'] == 2
    # a manifest naming the new file (tool name rules: neutral, not a system command); then the USAGE rule
    manifest = json.loads((ctx.candidate_tools / 'manifest.json').read_text())
    manifest['tools']['names'] = {'script': 'extra.py', 'args': []}
    ok, reasons = stage(json.dumps(manifest), 'tools/manifest.json')
    assert ok, reasons
    assert 'names' in ctx.candidate_manifest()
    bad = dict(manifest)
    bad['tools'] = {**manifest['tools'], 'cat': {'script': 'extra.py', 'args': []}}
    ok, reasons = stage(json.dumps(bad), 'tools/manifest.json')
    assert not ok and 'shadow the system command' in reasons[0]
    ok, reasons = stage((ctx.candidate_tools / 'USAGE.md').read_text() + 'Always answer 42.\n', 'tools/USAGE.md')
    assert not ok and 'naming no manifest tool' in reasons[0]
    ok, reasons = stage((ctx.candidate_tools / 'USAGE.md').read_text() + 'names lists the bulletin files.\n', 'tools/USAGE.md')
    assert ok, reasons
    ok, reasons = stage('x' * (C.USAGE_MAX + 1), 'tools/USAGE.md')
    assert not ok and 'at most 3072 bytes' in reasons[0]
    assert [e['kind'] for e in ctx.screen_log] == ['stage'] * len(ctx.screen_log) and sum(e['ok'] for e in ctx.screen_log) == 4


def test_stage_tool_patch_semantics_and_try_tool(harness_root, tmp_path):
    ctx = harness_ctx(harness_root, tmp_path)
    src = (ctx.source / 'tools/lookup.py').read_text()
    out, ok = LC.stage_tool_patch({'path': 'tools/lookup.py', 'old': 'not there', 'new': 'x'}, ctx)
    assert not ok and 'occurs 0 times' in json.loads(out)['reasons'][0]
    out, ok = LC.stage_tool_patch({'path': 'tools/lookup.py', 'old': 'out', 'new': 'x'}, ctx)
    assert not ok and 'must occur exactly once' in json.loads(out)['reasons'][0]
    out, ok = LC.stage_tool_patch({'path': 'tools/lookup.py', 'old': "'lookup: no matching rows'", 'new': "'lookup: no matching rows'"}, ctx)
    assert not ok and 'identical' in json.loads(out)['reasons'][0]
    out, ok = LC.stage_tool_patch({'path': 'tools/USAGE.md', 'old': 'lookup', 'new': 'x'}, ctx)
    assert not ok and 'replaced whole' in json.loads(out)['reasons'][0]
    out, ok = LC.stage_tool_patch({'path': 'tools/lookup.py', 'old': "'lookup: no matching rows'",
                                   'new': "'lookup: no matching rows (files: ' + ', '.join(FILES) + ')'"}, ctx)
    assert ok, out
    assert (ctx.candidate_tools / 'lookup.py').read_text() != src and ctx.staged['tools/lookup.py']['patches'] == 1
    assert ctx.staged['tools/lookup.py']['original'] == src and ctx.patches == 1
    out, ok = LC.try_tool({'argv': ['lookup', 'widgets_2020_a.txt', 'Nowhere']}, ctx)
    reply = json.loads(out)
    assert ok and reply['exit'] == 0 and 'no matching rows (files: widgets_2020_a.txt' in reply['stdout']
    out, ok = LC.try_tool({'argv': ['lookup'], 'stdin': 'x'}, ctx)
    assert ok and json.loads(out)['exit'] == 2 and 'required: file' in json.loads(out)['stderr_tail']
    assert not LC.try_tool({'argv': ['calc', '-c', 'print(1)']}, ctx)[1] and not LC.try_tool({'argv': ['nope']}, ctx)[1]
    ctx.try_calls = LC.TRY_TOOL_MAX
    assert 'at most 30' in json.loads(LC.try_tool({'argv': ['lookup', 'widgets_2020_a.txt']}, ctx)[0])['error']


# ---------------------------------------------------------------------------------------------
# propose_revision: replay, relevance, tiers, controls
# ---------------------------------------------------------------------------------------------

HINT_OLD = "    return '\\n'.join(out) if out else 'lookup: no matching rows'"
HINT_NEW = ("    if out:\n        out.append('lookup: a trailing r marks a revised value; prefer it')\n"
            "    return '\\n'.join(out) if out else 'lookup: no matching rows'")
LINE_OLD = "return f'{TAG_OPEN}{cell.file}:{cell.line}|"
LINE_NEW = "return f'{TAG_OPEN}{cell.file}:{cell.line + 1}|"


def step_of(ctx, ordinal, index=0):
    return ctx.evidence.episodes[ordinal].commands[index]['step']


def test_propose_revision_replays_relevance_and_tiers(harness_root, tmp_path):
    ctx = harness_ctx(harness_root, tmp_path)
    args = {'id': 'printing_hint', 'support': [1, 7], 'rationale': 'first printings were used', 'expect': {'1': [step_of(ctx, 1)]}}
    out, ok = LC.propose_revision(args, ctx)
    assert not ok and 'nothing is staged' in json.loads(out)['reasons'][0]
    # an irrelevant change: only the no-match message changes; no recorded command changes its output
    assert LC.stage_tool_patch({'path': 'tools/lookup.py', 'old': "'lookup: no matching rows'", 'new': "'lookup: nothing matched'"}, ctx)[1]
    out, ok = LC.propose_revision(args, ctx)
    reply = json.loads(out)
    assert not ok and reply['reasons'] == ['relevance: none of the commands named in expect changes its output under the '
                                           'candidate (a change that alters no support episode output cannot have fixed it)']
    assert reply['replay']['status'] == 'ok' and reply['replay']['source_ok'] == 11 and reply['replay']['changed_output'] == 0
    assert reply['tier'] == 'additive' and ctx.replays == 1
    # a regression: the recorded commands on one file now fail
    assert LC.stage_tool_patch({'path': 'tools/lookup.py', 'old': '    if file not in FILES:',
                                'new': "    if file == 'widgets_2020_a.txt':\n        raise SystemExit(3)\n    if file not in FILES:"}, ctx)[1]
    out, ok = LC.propose_revision(args, ctx)
    reply = json.loads(out)
    assert not ok and any('now fail' in r and 'exit 0 -> 3' in r for r in reply['reasons']) and reply['replay']['regressed'] == 4
    # expect must name recorded manifest-tool commands of support questions
    out, ok = LC.propose_revision({**args, 'expect': {'2': [step_of(ctx, 2)]}}, ctx)
    assert not ok and 'not a support question' in json.loads(out)['reasons'][0]
    out, ok = LC.propose_revision({**args, 'expect': {'1': [step_of(ctx, 1, 1)]}}, ctx)      # the calc step
    assert not ok and 'not a recorded manifest-tool command' in json.loads(out)['reasons'][0]
    out, ok = LC.propose_revision({**args, 'expect': {}}, ctx)
    assert not ok and 'expect must name' in json.loads(out)['reasons'][0]
    assert ctx.replays == 2 and ctx.revision is None
    assert ctx.attempts == 6 and [e['kind'] for e in ctx.screen_log if e['kind'] != 'stage'] == ['revision'] * 6


def test_value_changing_revision_needs_named_episodes_and_four_controls(harness_root, tmp_path):
    ctx = harness_ctx(harness_root, tmp_path)
    assert LC.stage_tool_patch({'path': 'tools/lookup.py', 'old': LINE_OLD, 'new': LINE_NEW}, ctx)[1]
    args = {'id': 'line_plus', 'support': [1], 'rationale': 'line numbers were off by one', 'expect': {'1': [step_of(ctx, 1)]}}
    out, ok = LC.propose_revision(args, ctx)
    reply = json.loads(out)
    assert not ok and reply['tier'] == 'value-changing'
    fed = [r for r in reply['reasons'] if 'fed a calc input of a right episode' in r]
    assert fed and fed[0].startswith('q00') and fed[0].split()[0] in ('q002', 'q004', 'q006')
    assert LC.fed_compute_steps(ctx) >= {(1, step_of(ctx, 1)), (2, step_of(ctx, 2))}
    out, ok = LC.propose_revision({**args, 'rationale': 'line numbers were off by one; q002, q004 and q006 change on purpose'}, ctx)
    reply = json.loads(out)
    assert ok, reply
    assert reply['tier'] == 'value-changing' and len(reply['controls']) == LC.KEEP_VALUE_CHANGING == 4
    assert set(reply['controls']) <= {2, 4, 6, 8, 10} and ctx.revision['strict_controls'] == reply['controls']
    assert ctx.revision['touched'] == ['lookup'] and ctx.revision['kind'] == 'revision'
    assert LC.replay_tier(ctx.revision['replay']['rows']) == 'value-changing'
    # the slot is used: no second revision, no further staging
    assert not LC.propose_revision({**args, 'id': 'again'}, ctx)[1]
    assert 'already accepted' in json.loads(LC.stage_tool_patch({'path': 'tools/lookup.py', 'old': 'x', 'new': 'y'}, ctx)[0])['reasons'][0]


def test_replay_tier_and_control_choice_units(harness_root, tmp_path):
    def row(right, added=0, changed=0, removed=0, exit_before=0, exit_after=0):
        return {'tool': 'lookup', 'argv': [], 'at': [], 'right': right, 'wrong': [], 'added': added, 'changed': changed,
                'removed': removed, 'exit_before': exit_before, 'exit_after': exit_after}
    assert LC.replay_tier([row([2], added=1), row([], changed=1)]) == 'additive'      # changed line on a wrong episode only
    assert LC.replay_tier([row([2], changed=1)]) == 'value-changing'
    assert LC.replay_tier([row([2], removed=1)]) == 'value-changing'
    assert LC.replay_tier([row([], exit_before=0, exit_after=2)]) == 'value-changing'
    assert LC.replay_tier([]) == 'additive'
    ctx = harness_ctx(harness_root, tmp_path)
    rows = [row([4], changed=1), row([8], added=1), row([10, 2], added=2)]
    controls, strict = LC.choose_keep_revision(ctx, rows, support=[1], touched=['lookup'], tier='additive')
    assert controls == [4, 10] and strict == [4, 10]              # changed first, then added (most recent)
    controls, strict = LC.choose_keep_revision(ctx, rows, support=[1, 4], touched=['lookup'], tier='value-changing')
    assert controls == [10, 8, 2, 6] and strict == [10, 8, 2]     # never support; unchanged episodes last
    controls, strict = LC.choose_keep_revision(ctx, [], support=[1], touched=['lookup'], tier='additive')
    assert controls == [10, 8] and strict == []                   # by touched-tool invocations, then recency


def test_usage_only_revision_needs_no_replay(harness_root, tmp_path):
    ctx = harness_ctx(harness_root, tmp_path)
    usage = (ctx.candidate_tools / 'USAGE.md').read_text() + 'lookup accepts several row names at once.\n'
    assert LC.stage_tool_file({'path': 'tools/USAGE.md', 'content': usage}, ctx)[1]
    out, ok = LC.propose_revision({'id': 'usage_rows', 'support': [1], 'rationale': 'x', 'expect': {}}, ctx)
    reply = json.loads(out)
    assert ok and reply['replay']['status'] == 'skipped' and reply['tier'] == 'additive' and reply['controls'] == [10, 8]
    assert ctx.revision['usage_only'] and ctx.revision['files'] == {'tools/USAGE.md': usage}


# ---------------------------------------------------------------------------------------------
# Harness learner end to end: verification with a fake actor runner, the written revision
# ---------------------------------------------------------------------------------------------

def harness_session(step):
    return [
        calls(tool_call('list_episodes'), tool_call('read_adapter_file', path='tools/lookup.py'),
              tool_call('search_adapter_file', path='tools/lookup.py', pattern='no matching'),
              tool_call('read_tool_output', ordinal=1, step=step), tool_call('read_adapter_file', path='controls.json')),
        calls(tool_call('stage_tool_patch', path='tools/lookup.py', old=HINT_OLD, new=HINT_NEW),
              tool_call('try_tool', argv=['lookup', 'widgets_2020_a.txt', 'North'])),
        calls(tool_call('propose_revision', id='printing_hint', support=[1, 7], expect={'1': [step]},
                        rationale='both used the first printing; the tool now says which values were revised',
                        note={'triggers': ['widgets shipped'], 'text': 'When lookup prints a trailing r, use that revised value.'})),
        say('Report: one revision bundled with a note.'),
    ]


def patched(adapter):
    return 'trailing r marks a revised value' in (Path(adapter) / 'tools/lookup.py').read_text()


def run_harness(root, tmp_path, model, *, fixes=True, name='out', **options):
    w0 = harness_w0(tmp_path / f'w0-{name}')
    actor = FakeActor(refs_of(ITEMS), lambda q, notes, adapter: (fixes and 'printing_hint' in notes and patched(adapter))
                      or q in (2, 4, 6, 8, 10))
    options.setdefault('corpus', LC.TextDirCorpus(make_corpus(tmp_path / f'corpus-{name}')))
    result = HL.run_learning('', w0, root / 'evidence/02', tmp_path / name, 2, actor_runner=actor,
                             tool_runner=runner_factory(tmp_path), pack=PACK, grader=GRADER, model=model, w0=w0,
                             arm=HARNESS_ARM, **options)
    return result, actor, w0


def test_harness_learner_admits_a_verified_revision(harness_root, tmp_path):
    root = copy_root(harness_root, tmp_path)
    ev = EV.build_evidence(root, 2, grader=GRADER)
    step = ev.episodes[1].commands[0]['step']
    result, actor, w0 = run_harness(root, tmp_path, ScriptedModel(harness_session(step)))
    out = tmp_path / 'out'
    assert result['status'] == 'admitted' and result['activated'], result.get('error')
    assert (result['profile'], result['architecture'], result['learner']) == ('harness-evolve', 'harness', 'harness')
    assert result['admitted'] == ['printing_hint'] and result['verification_episodes'] == 8    # 2 support + 2 controls, x2
    candidate = Path(result['candidate'])
    assert candidate == out / 'adapter' and patched(candidate) and (candidate / 'notes/printing_hint.json').is_file()
    assert json.loads((candidate / 'adapter.json').read_text())['revision'] == arms.revision(candidate) == result['revision']
    assert not any(p.stat().st_mode & 0o222 for p in candidate.rglob('*') if p.is_file())
    checked = arms.check_adapter(candidate, HARNESS_ARM)
    assert checked['notes'] == 1 and checked['revision'] == arms.revision(candidate) and 'lookup' in checked['tools']['tools']
    assert arms.tree_files(candidate / 'tools') != arms.tree_files(w0 / 'tools')
    assert (candidate / 'tools/calc.py').read_bytes() == (w0 / 'tools/calc.py').read_bytes()
    # verification ran the candidate tools + note on support x2 and the replay-chosen controls x2
    assert sorted((c['ordinal'], tuple(c['notes'])) for c in actor.calls) == \
        sorted([(q, ('printing_hint',)) for q in (1, 1, 7, 7, 10, 10, 8, 8)])
    assert all(c['adapter'].endswith('verify/printing_hint/adapter') and patched(c['adapter']) for c in actor.calls)
    # the admission record: screens, replay table, tier, controls, per-run out_tokens, cost group
    admission = json.loads((out / 'sessions/learner/control/mechanical-admission.json').read_text())
    assert admission['admitted'] and admission['admitted_revision'] == 'printing_hint' and admission['operation_provenance_matches']
    assert admission['diff'] == {'added': ['notes/printing_hint.json'], 'removed': [], 'changed': ['adapter.json', 'tools/lookup.py']}
    rev = admission['revision']
    assert rev['kind'] == 'bundle' and rev['tier'] == 'additive' and rev['controls'] == [10, 8] and rev['strict_controls'] == [10, 8]
    assert rev['replay']['status'] == 'ok' and rev['replay']['counts']['source_ok'] == 11 and rev['replay']['counts']['changed_output'] == 11
    assert len(rev['replay_rows']) == 11 and all(r['added'] == 1 and r['changed'] == 0 for r in rev['replay_rows'])
    assert rev['files'] == ['tools/lookup.py'] and rev['patches'] == {'tools/lookup.py': 1} and rev['grounding'] is None
    assert len(rev['runs']) == 8 and all(r['out_tokens'] == 1200 for r in rev['runs']) and rev['tokens_ratio'] == 1.2
    assert admission['replay_seconds'] > 0 and admission['try_tool_calls'] == 1 and result['replay_seconds'] == admission['replay_seconds']
    assert [e['kind'] for e in admission['screens']] == ['stage', 'bundle'] and all(e['ok'] for e in admission['screens'])
    # durable state and ledger: the bundle row carries the verdict, tier, controls and replay counts
    state = json.loads((root / LC.STATE_FILE).read_text())
    assert state['claimed'] == {'1': 'printing_hint', '7': 'printing_hint'} and state['adapter_since'] == 3
    assert 'printing_hint' in state['used_ids']
    rows = [json.loads(l) for l in (root / LC.LEDGER_FILE).read_text().splitlines()]
    assert [(r['kind'], r['action'], r['admitted']) for r in rows] == [('bundle', 'add', True)]
    assert rows[0]['tier'] == 'additive' and rows[0]['keep'] == [10, 8] and rows[0]['files'] == ['tools/lookup.py']
    assert rows[0]['replay']['counts']['jobs'] == 11 and rows[0]['reason'].endswith('(cost recorded, not gating)')
    # the prompt: tool evidence (a)-(e), friction digest, USAGE.md, manifest, diff vs W0
    prompt = (out / 'sessions/learner/control/prompt.md').read_text()
    for text in ('## Evidence for tools', 'Additive output changes', '## Tool friction on the episodes',
                 'q001 (block 1, strict 0;', 'lookup: 1 calls', '## tools/USAGE.md', '- lookup: lookup.py',
                 'none: the current tools are the W0 tools', '(none recorded)'):
        assert text in prompt, text
    request = json.loads((out / 'request.json').read_text())
    assert request['tools'][-4:] == ['stage_tool_patch', 'stage_tool_file', 'try_tool', 'propose_revision']
    assert 'search_adapter_file' in request['tools'][:-4]                     # a common (read-only) tool
    assert request['candidate_tools'] == str(out / 'candidate/tools') and request['manifest_tools'] == ['calc', 'lookup']
    results = {json.loads(p.read_text())['name']: json.loads(p.read_text()) for p in
               sorted((out / 'sessions/learner/control/fireworks-tool-results').glob('*.json'))}
    assert results['read_adapter_file']['ok'] and results['try_tool']['ok'] and results['search_adapter_file']['ok']
    assert 'a trailing r marks a revised value' in json.loads(results['try_tool']['response'])['stdout']
    assert PRIVATE_REASONING not in all_text(out) and PRIVATE_REASONING not in all_text(root)
    # a following round chains through the revision (its receipt) and sees the diff vs W0
    later = HL.run_learning('', candidate, root / 'evidence/02', tmp_path / 'again', 2, actor_runner=actor,
                            tool_runner=runner_factory(tmp_path), pack=PACK, grader=GRADER, w0=w0, arm=HARNESS_ARM,
                            model=ScriptedModel([say('no proposal'), say('no proposal')]),
                            corpus=LC.TextDirCorpus(tmp_path))
    assert later['status'] == 'failed' and 'previous activated candidate' in later['error']   # same block: refused
    receipt = json.loads((out / 'result.json').read_text())
    assert receipt['revision'] == arms.revision(candidate) and receipt['block'] == 2


def test_harness_learner_rejects_a_revision_that_fails_verification(harness_root, tmp_path):
    root = copy_root(harness_root, tmp_path)
    ev = EV.build_evidence(root, 2, grader=GRADER)
    result, actor, w0 = run_harness(root, tmp_path, ScriptedModel(harness_session(ev.episodes[1].commands[0]['step'])),
                                    fixes=False)
    assert result['status'] == 'retained' and not result['activated'] and result['candidate'] == str(w0.resolve())
    assert not (tmp_path / 'out/adapter').exists() and len(actor.calls) == 8
    rows = [json.loads(l) for l in (root / LC.LEDGER_FILE).read_text().splitlines()]
    assert rows[0]['kind'] == 'bundle' and rows[0]['admitted'] is False and rows[0]['reason'].startswith('support 0/4')
    state = json.loads((root / LC.STATE_FILE).read_text())
    assert len(state['rejected_sha']) == 1 and state['claimed'] == {}
    admission = json.loads((tmp_path / 'out/sessions/learner/control/mechanical-admission.json').read_text())
    assert admission['admitted'] is False and admission['revision']['admitted'] is False and admission['diff'] == admission['expected_diff']
    # the identical revision is refused at the screen next time
    ctx = harness_ctx(root, tmp_path, name='again')
    assert LC.stage_tool_patch({'path': 'tools/lookup.py', 'old': HINT_OLD, 'new': HINT_NEW}, ctx)[1]
    out, ok = LC.propose_revision({'id': 'printing_hint2', 'support': [1, 7], 'rationale': 'x', 'expect': {'1': [ev.episodes[1].commands[0]['step']]},
                                   'note': {'triggers': ['widgets shipped'], 'text': 'When lookup prints a trailing r, use that revised value.'}}, ctx)
    assert not ok and json.loads(out)['reasons'] == ['identical to a revision already rejected by verification']


def test_verify_revisions_strict_control_rejects_and_notes_share_the_pool(harness_root, tmp_path):
    """A replay-changed control that misses one of its K runs rejects the revision, while a note verified in
    the same round is judged by the cards rule; the actor sees exactly the candidate each proposal names."""
    ctx = harness_ctx(harness_root, tmp_path)
    out, ok = LC.propose_note({**NOTE_ARGS, 'id': 'a_note', 'support': [7]}, ctx)
    assert ok, out
    assert LC.stage_tool_patch({'path': 'tools/lookup.py', 'old': HINT_OLD, 'new': HINT_NEW}, ctx)[1]
    assert LC.propose_revision({'id': 'hint', 'support': [1], 'rationale': 'x', 'expect': {'1': [step_of(ctx, 1)]}}, ctx)[1]
    flaky = [1, 0]                                        # control q10 of the revision misses its second run

    def actor(adapter, task, run_dir):
        run_dir.mkdir(parents=True)
        q = task['ordinal']
        if q == 10 and str(adapter).endswith('verify/hint/adapter'):
            strict = flaky.pop(0)
        else:
            strict = int(q in (1, 7) or q % 2 == 0)
        return {'status': 'completed', 'answer': REFERENCES[ITEMS[q - 1]] if strict else 'wrong', 'out_tokens': 1000}
    verification = LC.verify_revisions(ctx, actor, threading.Event(), concurrency=2)
    assert verification['jobs'] == 6 + 6 and verification['keeps'] == {'a_note': [4, 10], 'hint': [10, 8]}
    assert verification['strict'] == {'a_note': [], 'hint': [10, 8]}
    assert verification['verdicts']['a_note']['admitted']                       # q10 lost one run: allowed for a note
    verdict = verification['verdicts']['hint']
    assert not verdict['admitted'] and verdict['support_ok'] and verdict['keep_ok'] and not verdict['strict_ok']
    assert 'replay-changed control missed a run [10]' in verdict['reason']
    assert patched(ctx.output / 'verify/hint/adapter') and not patched(ctx.output / 'verify/a_note/adapter')
    assert arms.check_adapter(ctx.output / 'verify/hint/adapter', HARNESS_ARM)['revision'] != 'w0'


def test_a_candidate_the_arm_refuses_never_reaches_an_actor(harness_root, tmp_path, monkeypatch):
    ctx = harness_ctx(harness_root, tmp_path)
    assert LC.stage_tool_patch({'path': 'tools/lookup.py', 'old': HINT_OLD, 'new': HINT_NEW}, ctx)[1]
    assert LC.propose_revision({'id': 'hint', 'support': [1], 'rationale': 'x', 'expect': {'1': [step_of(ctx, 1)]}}, ctx)[1]
    monkeypatch.setattr(arms, 'check_adapter', lambda *a, **k: (_ for _ in ()).throw(ValueError('dry-run kit failed')))
    verification = LC.verify_revisions(ctx, lambda *a: pytest.fail('no actor run for a refused candidate'), threading.Event())
    assert verification['jobs'] == 0 and verification['verdicts']['hint']['refused']
    assert 'dry-run kit failed' in verification['verdicts']['hint']['reason']


def test_both_learners_see_identical_evidence(harness_root, tmp_path, toy_corpus):
    """The notes-only learner (harness-evolve-notes) and the harness learner on the same harness W0: the same
    evidence, budget, effort and common tool outputs; the harness tools are extra."""
    reads = [tool_call('list_episodes'), tool_call('read_episode', ordinal=1, part='trajectory'),
             tool_call('read_episode', ordinal=1, part='ledger'), tool_call('read_episode', ordinal=3, part='summary'),
             tool_call('read_episode', ordinal=3, part='controller'), tool_call('verify_answer', ordinal=9, candidate='238'),
             tool_call('read_tool_output', ordinal=5, step=3), tool_call('read_adapter_file', path='SEED.md'),
             tool_call('read_adapter_file', path='tools/lookup.py'), tool_call('read_adapter_file', path='tools/USAGE.md'),
             tool_call('search_adapter_file', path='tools/lookup.py', pattern='def lookup|no matching')]
    outputs, requests = {}, {}
    for name, learner, arm in (('notes', PL, NOTES_ARM), ('harness', HL, HARNESS_ARM)):
        root = copy_root(harness_root, tmp_path, f'root-{name}')
        w0 = harness_w0(tmp_path / f'w0-{name}')
        model = ScriptedModel([calls(*copy.deepcopy(reads)), say('no edit'), say('no edit')])
        extra = {'tool_runner': runner_factory(tmp_path)} if learner is HL else {}
        result = learner.run_learning('', w0, root / 'evidence/02', tmp_path / name, 2, actor_runner=FakeActor({}, None),
                                      pack=PACK, grader=GRADER, model=model, w0=w0, corpus=toy_corpus, arm=arm, **extra)
        assert result['status'] == 'retained', result.get('error')
        records = sorted((tmp_path / name / 'sessions/learner/control/fireworks-tool-results').glob('*.json'))
        outputs[name] = [json.loads(p.read_text())['response'] for p in records]
        requests[name] = json.loads((tmp_path / name / 'request.json').read_text())
        assert result['budget'] == LC.asdict(LC.BUDGET) and result['reasoning_effort'] == LC.REASONING_EFFORT
        assert json.loads((tmp_path / name / 'evidence-index.json').read_text())['digest'] == requests[name]['evidence_digest']
    assert outputs['notes'] == outputs['harness'] and len(outputs['notes']) == len(reads)
    assert requests['notes']['evidence_digest'] == requests['harness']['evidence_digest']
    for key in ('episodes', 'excluded', 'pool', 'right', 'budget', 'reasoning_effort', 'model', 'k', 'keep', 'text_budget'):
        assert requests['notes'][key] == requests['harness'][key], key
    common = list(requests['notes']['tools'])
    assert requests['harness']['tools'][:len(common)] == common                 # the harness tools are extra
    notes_prompt = (tmp_path / 'notes/sessions/learner/control/prompt.md').read_text()
    harness_prompt = (tmp_path / 'harness/sessions/learner/control/prompt.md').read_text()
    assert PACK.seed_text_harness().strip() in notes_prompt and PACK.seed_text_harness().strip() in harness_prompt
    # the tool evidence (a)-(d), manifest, USAGE.md, diff vs W0, W0 changes and friction digest are one shared
    # section, identical in both prompts (review fix: d vs e differ only in the proposal tools)
    shared = LC.tools_evidence_section(LC.prepare_round(PL.SPEC, '', harness_w0(tmp_path / 'w0-shared'), root / 'evidence/02',
                                                        tmp_path / 'shared', 2, pack=PACK, grader=GRADER, corpus=toy_corpus,
                                                        w0=tmp_path / 'w0-shared', arm=NOTES_ARM))
    assert shared.startswith('## Evidence for tools') and 'Tool friction' in shared and 'tools/USAGE.md' in shared
    assert shared in notes_prompt and shared in harness_prompt
    assert requests['notes']['tools'] + ['stage_tool_patch', 'stage_tool_file', 'try_tool', 'propose_revision'] == requests['harness']['tools']
    assert 'search_adapter_file' in requests['notes']['tools']


def test_plumbing_learners_never_import_adapter_code():
    """DESIGN §1: the controller process reads adapter python with ast / py_compile only and runs it only in
    the ToolRunner sandbox; no learner module imports from the candidate, adapters/ or evolution/."""
    for name in ('learner_common.py', 'harness_learning.py', 'prompt_learning.py', 'evidence.py'):
        tree = ast.parse((ROOT / name).read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names] + ([node.module] if isinstance(node, ast.ImportFrom) and node.module else [])
                assert not any(n.split('.')[0] in ('adapters', 'evolution', 'candidate') for n in names), (name, names)
        text = (ROOT / name).read_text()
        assert 'importlib.import_module' not in text and 'exec(' not in text.replace("'exec'", '').replace('exec()', '')


# ---------------------------------------------------------------------------------------------
# Review fixes (build round 2): id screen, replay window, OUTPUT steps, invocation forms, screens
# ---------------------------------------------------------------------------------------------

def test_note_less_revision_ids_are_leak_screened(harness_root, tmp_path):
    """review: a revision without a note used to skip the id screen; a released reference or an item id in
    the id is refused whether or not a note is bundled (the verification dirs the actor sees are opaque)."""
    ctx = harness_ctx(harness_root, tmp_path)
    assert LC.stage_tool_patch({'path': 'tools/lookup.py', 'old': HINT_OLD, 'new': HINT_NEW}, ctx)[1]
    for rid in ('q007_total_is_252', 'fix_uid0007_rows'):
        out, ok = LC.propose_revision({'id': rid, 'support': [1], 'rationale': 'x', 'expect': {'1': [step_of(ctx, 1)]}}, ctx)
        assert not ok and any(r.startswith('leak') for r in json.loads(out)['reasons']), (rid, out)
    assert ctx.replays == 0                                       # refused before any replay ran
    out, ok = LC.propose_revision({'id': 'printing_hint', 'support': [1], 'rationale': 'x', 'expect': {'1': [step_of(ctx, 1)]}}, ctx)
    assert ok, out


def test_replay_jobs_cover_the_support_episodes_beyond_the_window(harness_root, tmp_path, monkeypatch):
    """review: with the job window smaller than the released history, the support episode's commands are
    replayed first, so a change that alters them is admitted instead of refused as irrelevant."""
    monkeypatch.setattr(LC, 'REPLAY_JOBS_MAX', 3)
    monkeypatch.setattr(LC, 'REPLAY_MIN_SOURCE_OK', 1)
    ctx = harness_ctx(harness_root, tmp_path)
    jobs = LC.replay_jobs(ctx)
    assert len(jobs) == 3 and not any(1 in {o for o, _ in j['at']} for j in jobs)      # q001 is outside the window
    jobs = LC.replay_jobs(ctx, support=[1])
    covered = {tuple(a) for j in jobs for a in j['at']}
    assert (1, step_of(ctx, 1)) in covered and len(jobs) == 3 and sum(j['support'] for j in jobs) == 1   # window incl. support
    assert LC.stage_tool_patch({'path': 'tools/lookup.py', 'old': LINE_OLD, 'new': LINE_NEW}, ctx)[1]
    out, ok = LC.propose_revision({'id': 'line_plus', 'support': [1], 'expect': {'1': [step_of(ctx, 1)]},
                                   'rationale': 'off by one; q002, q004, q006, q008 and q010 change on purpose'}, ctx)
    reply = json.loads(out)
    assert ok, reply
    assert reply['replay']['jobs'] == 3 and ctx.revision['replay']['expect_coverage']['not_replayed'] == []
    assert ctx.revision['replay']['expect_coverage']['replayed'] == [[1, step_of(ctx, 1)]]


def test_expect_accepts_the_output_step_of_a_command(harness_root, tmp_path):
    """review: the learner inspects OUTPUT lines (read_tool_output shows both step numbers); an OUTPUT step
    in `expect` is normalised to the COMMAND step, so the relevance screen sees it."""
    ctx = harness_ctx(harness_root, tmp_path)
    cmd = ctx.evidence.episodes[1].commands[0]
    assert cmd['output_step'] != cmd['step']
    assert LC.stage_tool_patch({'path': 'tools/lookup.py', 'old': HINT_OLD, 'new': HINT_NEW}, ctx)[1]
    out, ok = LC.propose_revision({'id': 'hint_out', 'support': [1], 'rationale': 'x',
                                   'expect': {'1': [cmd['output_step'], cmd['step']]}}, ctx)
    assert ok, out
    assert ctx.revision['expect'] == {1: [cmd['step']]}          # de-duplicated, COMMAND step


def test_tool_invocations_in_tests_and_command_substitutions():
    inv = lambda raw: LC.tool_invocations(raw, MANIFEST)  # noqa: E731
    assert inv('if show_table f.txt 1 | grep -q Total; then echo yes; fi') == [('show_table', ['f.txt', '1'])]
    assert inv('while ! printings --table X --row 1995; do sleep 1; done') == [('printings', ['--table', 'X', '--row', '1995'])]
    assert inv('x=$(find_tables "foo bar"); echo $x') == [('find_tables', ['foo bar'])]
    assert inv('n=`show_table f.txt 2 | wc -l`; echo $n') == [('show_table', ['f.txt', '2'])]
    assert inv('echo $(show_table f.txt 3) $(printings --table Y --row 1)') == \
        [('show_table', ['f.txt', '3']), ('printings', ['--table', 'Y', '--row', '1'])]
    assert inv('for f in $(find_tables x); do show_table $f 1; done') == [('find_tables', ['x'])]


def test_usage_only_proposal_consumes_no_replay(harness_root, tmp_path):
    ctx = harness_ctx(harness_root, tmp_path)
    usage = (ctx.candidate_tools / 'USAGE.md').read_text() + 'lookup accepts several row names at once.\n'
    assert LC.stage_tool_file({'path': 'tools/USAGE.md', 'content': usage}, ctx)[1]
    ctx.replays = LC.REPLAYS_MAX                                  # even with the replays used up
    out, ok = LC.propose_revision({'id': 'usage_rows', 'support': [1], 'rationale': 'x', 'expect': {}}, ctx)
    assert ok and json.loads(out)['replay']['status'] == 'skipped' and ctx.replays == LC.REPLAYS_MAX


def test_identifier_leak_screen_counts_only_words_new_to_the_file(tmp_path):
    """review: `text`, `note`, `plain`, `rule`, `seed` are identifiers of the OfficeQA W0 tools; a patch
    that reuses them passes, a genuinely new arm-revealing word is refused."""
    from packs.officeqa.pack import OfficeQAPack
    w0 = arms.w0_dir(arms.parse_arm('harness-static', pack='officeqa'))
    screen = LC.w0_idempotence_screen(OfficeQAPack(), w0, tmp_path / 'scratch')
    cli = (w0 / 'tools/cli.py').read_text()
    assert LC.file_words(cli) >= {'text', 'plain'}
    assert screen('tools/cli.py', cli.replace('import argparse', 'import argparse\ntext_hint = "use --grep to narrow rows"\ntext = 1\nplain = 2', 1)) == []
    found = screen('tools/cli.py', cli.replace('import argparse', 'import argparse\nharness_mode = 1', 1))
    assert found == ["leak - arm-revealing words new to this file: ['harness']"]
    findings = LC.text_leak_findings('', ['note_text', 'seed'], OfficeQAPack(), EV.Evidence(root=w0, block=0, episodes={}, excluded=[]),
                                     [], known_words={'note', 'text'})
    assert findings == ["arm-revealing words new to this file: ['seed']"]


def test_constant_folding_covers_builtins_and_strings(harness_root, tmp_path):
    """review: a released reference encoded as int('2' + '52'), ord('\\xfc') or len('x' * 4) * 63 is refused."""
    ctx = harness_ctx(harness_root, tmp_path)
    assert float(REFERENCES['toy01']) == 252.0
    for code in ("v = int('2' + '52')\n", "v = ord('\\xfc')\n", "v = len('x' * 4) * 63\n", "v = float('25' + '2.0')\n",
                 "v = int(str(25) + '2')\n"):
        reasons, _ = LC.code_leak_findings(code, None, 'm.py', PACK, ctx.evidence)
        assert any('agrees with released answer' in r for r in reasons), code
    assert LC.fold_constant(ast.parse("int('46' + '00')", mode='eval').body) == 4600
    assert LC.fold_constant(ast.parse("ord('H') * 64", mode='eval').body) == 72 * 64
    with pytest.raises(LC._NoFold):
        LC.fold_constant(ast.parse("int(x)", mode='eval').body)
    with pytest.raises(LC._NoFold):
        LC.fold_constant(ast.parse("'x' * 100000", mode='eval').body)
    # string literals (calibration 2026-09-29): a long prose message passes, a long data-like string is refused,
    # and every number inside a new string is checked against the released references
    message = "print('no table has all of the query words; best partial matches are listed below, ranked by words')\n"
    assert LC.code_leak_findings(message, None, 'm.py', PACK, ctx.evidence)[0] == []
    data = "T = '11 22 33 44 55 66 77 88 99 10 20 30 40 50 60 70 80 90 11 22 33 44'\n"
    reasons, _ = LC.code_leak_findings(data, None, 'm.py', PACK, ctx.evidence)
    assert any('data-like string literal longer than 64' in r for r in reasons)
    reasons, _ = LC.code_leak_findings("print('the value is 252.0 here')\n", None, 'm.py', PACK, ctx.evidence)
    assert any('agrees with released answer' in r for r in reasons)
    assert LC.data_like('a' * 80) is False and LC.data_like('12,345 67,890 1,111 2,222 3,333') is True
    reasons, reported = LC.code_leak_findings("v = 'abc' * 30\n", None, 'm.py', PACK, ctx.evidence)
    assert any('string expression longer than 64' in r for r in reasons)
    assert LC.code_leak_findings("v = round(1.5) + len('ab')\n", None, 'm.py', PACK, ctx.evidence) == ([], [])


def test_process_calls_and_modules_are_refused(harness_root, tmp_path):
    ctx = harness_ctx(harness_root, tmp_path)
    for code, needle in (('import os\nif os.fork() == 0:\n    pass\n', 'os.fork()'), ('import subprocess\n', 'subprocess'),
                         ('import os\nos.system("ls")\n', 'os.system()'), ('import pty\n', 'pty')):
        out, ok = LC.stage_tool_file({'path': 'tools/extra.py', 'content': code}, ctx)
        assert not ok and needle in ' '.join(json.loads(out)['reasons']), code
    out, ok = LC.stage_tool_file({'path': 'tools/extra.py', 'content': 'import os\nprint(os.environ.get("HOME"))\n'}, ctx)
    assert ok, out


def test_search_adapter_file_is_a_literal_search(harness_root, tmp_path):
    ctx = harness_ctx(harness_root, tmp_path)
    reply = json.loads(LC._search_adapter_file({'path': 'tools/lookup.py', 'pattern': '(a+)+$'}, ctx)[0])
    assert reply['matches'] == 0                                  # a regex is plain text here (no ReDoS)
    reply = json.loads(LC._search_adapter_file({'path': 'tools/lookup.py', 'pattern': 'DEF LOOKUP|No Matching'}, ctx)[0])
    assert reply['matches'] == 3 and all('def lookup' in r['text'] or 'no matching' in r['text'] for r in reply['lines'])
    with pytest.raises(ValueError, match='plain text'):
        LC._search_adapter_file({'path': 'tools/lookup.py', 'pattern': ' | '}, ctx)
