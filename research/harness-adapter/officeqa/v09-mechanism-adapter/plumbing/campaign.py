"""v9 campaign: one arm, one repeat per root (DESIGN §1, §4). The controller grades each sealed block for
the learner arms; actors never see a grader, a reference or the adapter files.

Loop (per root; every root of a repeat group runs the same question order and advances block by block):

1. Freeze (once; reused on resume): code, PROTOCOL/PREREG, harness_core/, packs/, adapters/, the Codex
   binary, the frozen actor runtime (deep manifest check), the prepared manifest, the tables index, the
   question order of this repeat, the W0 adapter and the v9 settings.
2. For each block b (5 questions; 133 = 26 x 5 + 3):
   - barrier: wait until every root of the repeat group finished block b-1 (block 0 = ready);
   - one episode per question, in this repeat's order, one at a time (`episode`): a fresh workspace with
     the kit (L0), the pack tools (L1 arms), README = L0 + search helper + task conventions (+ tool usage,
     + submit usage), AGENTS.md = COMMON + the adapter's SEED.md + <= 3 notes chosen by trigger; controller
     arms get Ledger + Gate + SandboxRunner + MechController behind the broker (actor_deadline on the
     broker's monotonic clock); the prediction is final.md; the last submitted candidate is recorded as
     `fallback_answer` (diagnostic only, never the prediction);
   - learner arms (not after the last block): seal the block's predictions, release the references of
     the non-withheld questions into evidence/BB (issue-47 references are never released), run the arm's
     learner (text_learning / mechanism_learning) with a verification actor runner, check the returned
     adapter and serve it from the next block on. A failed round stops the campaign (fail loudly);
   - arrive at the barrier for block b.
3. Seal all predictions, grade strict and 1% with the official reward.py (all 133 and the undisputed
   115), write private-evaluation/paired-metrics.json (paired later across roots by task id).

Resume (block level, after an ABORT or a crash): the freeze is reused and verified; the first block not
marked done is moved to runs-aborted/<stamp>/ (only its learning outputs when all of its episodes are
complete and unchanged, otherwise its episodes too) and run again.

Wave 3 (pilot): a root whose spec declares a `subset` runs only those questions, in this repeat's seeded
order (the full order with the other questions removed), in blocks of 5 like a full run; seal, grade and
audit cover exactly the subset. The spec's `temperature` and `top_p` go to every actor episode's broker
(scored and verification; options `temperature` / `top_p`, sent in every upstream body; top_p None = not
sent) and into invocation.json and the freeze; the campaign refuses to start when the broker would send
anything else. The learners' own model calls (client.py) are not affected.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import importlib
import inspect
import json
import os
from pathlib import Path
import re
import shutil
import threading
import time
from typing import Any, Callable, Mapping

ROOT = Path(__file__).resolve().parent
from paths import SOURCE, CODEX, ACTOR_PYTHON, HARNESS_CORE
import acting
import arms
import barrier as barriers
import broker
import data
import kit
import notes_render
from experiment import (PROFILE, CAMPAIGN, ACTOR_SECONDS, BROKER_OPTIONS, LEARN_EVERY, LEARN_LAST_BLOCK,
                        REQUERY_WHEN_CLEAN, TEMPERATURE)
from harness_core import contracts as C

MODEL = broker.MODEL
# This root's W0 adapter (v8 name; None for the plain arm or a directory without a campaign spec).
W0 = arms.w0_dir(CAMPAIGN.arm) if CAMPAIGN is not None else None
CORPUS = ROOT / 'prepared/agent/corpus'
INDEX = ROOT / 'prepared/agent/corpus.sqlite'
TABLES_DIR = ROOT / 'prepared/agent/tables'
TABLES_INDEX = TABLES_DIR / 'tables.sqlite'
COMMON = '''# OfficeQA task workspace

Answer the current question using the permitted local reference documents and
tools. Use the native shell for local file inspection and calculations as needed.
No internet is available. Do not access other tasks, answer keys, private graders,
external resources, credentials or the other experiment arm. Do not change
instructions, runtime configuration or read-only reference material.
The final assistant response must be only a direct answer, on one nonempty line,
at most 250 characters. Keep any working and citations in the task trajectory.
'''
HELPER_README = '''# Search helper

`officeqa.py` in this workspace searches and reads the same corpus (SQLite FTS5, BM25 ranking):
```python
from pathlib import Path
from officeqa import OfficeQACorpus
docs = OfficeQACorpus(Path({corpus!r}), Path({index!r}))
docs.search(query="text", limit=8, filename_glob="*.txt")
docs.read(filename="document.txt", start_line=1, num_lines=80)
docs.list_files(pattern="*.txt", limit=100, offset=0)
```
Search returns ranked text excerpts with filename and line anchors; read returns numbered document
lines. The helper contains no answers or source hints. You may also read the files directly (rg, sed,
Python).
'''
CALC_README = '''Calculation:
  calc -c '<python>'  |  calc file.py  |  calc <<'PY' ... PY
      runs Python 3.12 (numpy, pandas) and prints your code together with its output; the value of a
      final expression is printed, as in an interactive session
'''
SUBMIT_README = '''# Submitting your answer

  submit --answer "<answer>" [--alt "<other value>"] [--reading "<assumption>"] [--keep "<reason>"]
      hands your answer to the harness check before you finish; it prints `recorded` or one finding
      (AGENTS.md explains findings). End your turn with the recorded answer as your final message.
'''
# The broker's v9 summary.json fields (controller mode only), copied into the episode summary.
CONTROLLER_FIELDS = ('codex_requests', 'controller_calls', 'discarded_responses', 'inserts_by_reason', 'gate_rounds',
                     'enforce_requeries', 'enforce_fallbacks', 'submits', 'submit_rejected', 'submit_invalid',
                     'controller_errors', 'compactions', 'resumes', 'dropped_splices', 'retries_replayed',
                     'controller_usage')
# Scored episodes live in <root>/runs/episodes/NNN for every arm (the path is shown to the model).
RUNS_DIR = 'episodes'
SAFE_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
LEARNER_MODULES = {'text': 'text_learning', 'mechanism': 'mechanism_learning'}


class CampaignStopped(RuntimeError):
    """The local stop event was set (signal); state is saved, nothing is lost."""


class LearnerFailed(RuntimeError):
    """A learning round returned status 'failed' or an unusable adapter (DESIGN §3: fail loudly)."""


def utc(): return datetime.now(timezone.utc).isoformat()
def stamp(): return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def read(path): return json.loads(Path(path).read_text())


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{os.getpid()}.{threading.get_ident()}.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def snapshot(path):
    path = Path(path).resolve()
    result = {}
    for file in sorted(path.rglob('*')):
        if any(part in {'.git', '__pycache__', '.pytest_cache'} for part in file.relative_to(path).parts): continue
        if file.is_symlink(): raise ValueError('Adapter/code symlinks prohibited')
        if file.is_file(): result[file.relative_to(path).as_posix()] = sha(file)
    return result


def revision(path):
    return hashlib.sha256(json.dumps(snapshot(path), sort_keys=True).encode()).hexdigest()


def prompt_for(task):
    return ('Answer the following question using the local references. Read README.md '
            'for the available file and search interfaces. Your final response must contain '
            'only the direct answer, one line, at most 250 characters.\n\nQuestion:\n' + task['question'])

# ---------------------------------------------------------------------------------------------
# Layout (production: code and data both live in the run root; tests point `root` at a tmp dir)
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Layout:
    root: Path = ROOT
    code: Path = SOURCE

    @property
    def prepared(self): return self.root / 'prepared'
    @property
    def corpus(self): return self.prepared / 'agent/corpus'
    @property
    def index(self): return self.prepared / 'agent/corpus.sqlite'
    @property
    def tables_dir(self): return self.prepared / 'agent/tables'
    @property
    def tables_index(self): return self.tables_dir / 'tables.sqlite'
    @property
    def state(self): return self.root / 'state.json'
    @property
    def freeze(self): return self.root / 'freeze.json'
    def runs(self, arm=None):
        # Arm-neutral on purpose: the workspace path is model-visible (Codex environment context), so it
        # must not name the arm (v9 review S6). `arm` is accepted for old callers and ignored.
        return self.root / 'runs' / RUNS_DIR
    def evidence(self, block): return self.root / 'evidence' / f'{block:02}'
    def evolution(self, block): return self.root / 'evolution' / f'{block:02}'
    def seal(self, block): return self.root / 'private-learning-seals' / f'{block:02}.json'
    @property
    def adapters(self): return self.code / 'adapters'

# ---------------------------------------------------------------------------------------------
# Episode composition (README / AGENTS.md / kit tools) — the only place arms differ in the workspace
# ---------------------------------------------------------------------------------------------


def compose_readme(arm: C.ArmSpec, pack, corpus, index) -> tuple[str, dict]:
    """README.md = L0 (kit) + search helper (all arms) + task conventions (all arms) + L1 tool usage
    (l1_tools) + submit usage (controller). Returns (text, byte count per part)."""
    parts = [('l0', kit.readme_l0(arm, corpus, index)),
             ('helper', HELPER_README.format(corpus=str(corpus), index=str(index))),
             ('conventions', '# Task conventions\n\n' + pack.task_conventions())]
    if arm.l1_tools:
        parts.append(('tools', '# Table and calculation tools\n\n' + pack.usage().rstrip() + '\n' + CALC_README))
    if arm.controller:
        parts.append(('submit', SUBMIT_README))
    text = '\n'.join(p.rstrip() + '\n' for _, p in parts)
    return text, {name: len((p.rstrip() + '\n').encode('utf-8')) for name, p in parts}


def tool_wrappers(arm: C.ArmSpec) -> dict:
    """kit.build_kit tool_wrappers: the pack CLIs + calc for L1 arms, nothing otherwise (submit is added
    by the kit itself for controller arms)."""
    if not arm.l1_tools:
        return {}
    if arm.pack == 'officeqa':
        from packs.officeqa.pack import tool_wrappers as pack_tools
    elif arm.pack == 'toy':
        from packs.toy.pack import tool_wrappers as pack_tools
    else:
        raise ValueError(f'no kit tool wiring for pack {arm.pack!r}')
    return {**pack_tools(), 'calc': str(HARNESS_CORE / 'tools/calc.py')}


def uses_tables_index(pack: str) -> bool:
    """The OfficeQA tables index (prepared/agent/tables) is an OfficeQA pack artifact; other packs have none."""
    return pack == 'officeqa'


def actor_data(arm: C.ArmSpec, lay: Layout) -> tuple[dict, tuple]:
    """(env_extra, data roots) of an arm: the OfficeQA L1 tools read the corpus and the tables index."""
    if not arm.l1_tools or not uses_tables_index(arm.pack):
        return {}, ()
    return ({'OFFICEQA_CORPUS': str(lay.corpus.resolve()), 'TABLES_INDEX': str(lay.tables_index.resolve())},
            (str(lay.tables_dir.resolve()),))


def prepare_episode(lay: Layout, arm: C.ArmSpec, task: Mapping, adapter: Path | None, base: Path, *,
                    ordinal: int, role: str = 'scored', pack=None) -> dict:
    """Create base/{workspace,task.json} for one episode; nothing runs. Returns the preparation record."""
    base = Path(base)
    if base.exists():
        raise ValueError('An actor is attempted once; prior directory exists')
    base.mkdir(parents=True)
    workspace = base / 'workspace'
    pack = pack or arms.load_pack(arm.pack)
    if (adapter is None) != (arm.seed == 'none'):
        raise ValueError('an arm with seed knowledge needs its adapter; the plain arm has none')
    agents, note_ids, agents_info = notes_render.compose_agents(COMMON, adapter, task['question'])
    readme, readme_info = compose_readme(arm, pack, lay.corpus, lay.index)
    adapter_revision = revision(adapter) if adapter is not None else None
    extra = {'role': role, 'ordinal': ordinal, 'task_id': task['task_id'], 'notes': note_ids,
             'adapter': str(adapter) if adapter is not None else None, 'adapter_revision': adapter_revision,
             'readme_sha256': hashlib.sha256(readme.encode('utf-8')).hexdigest(), 'readme_bytes': readme_info,
             'agents_bytes': agents_info}
    marker = acting.prepare_actor_workspace(workspace, None, common_instructions=agents, arm=arm,
                                            tool_wrappers=tool_wrappers(arm), marker_extra=extra)
    shutil.copyfile(lay.code / 'officeqa_tools.py', workspace / 'officeqa.py')
    (workspace / 'README.md').write_text(readme)
    save(base / 'task.json', {'ordinal': ordinal, 'task_id': task['task_id'], 'question': task['question'],
                              'role': role})
    return {'base': str(base), 'workspace': str(workspace), 'records': str(base / 'records'),
            'api': str(base / 'api'), 'task': dict(task), 'ordinal': ordinal, 'role': role,
            'adapter': str(adapter) if adapter is not None else None, 'adapter_revision': adapter_revision,
            'notes': note_ids, 'agents_md_bytes': agents_info['agents_md_bytes'],
            'readme_bytes': sum(readme_info.values()), 'marker_agents_sha256': marker['agents_sha256']}


def build_controller(lay: Layout, arm: C.ArmSpec, adapter: Path, question: str, api: Path):
    """Ledger + Gate + SandboxRunner + MechController for one controller-arm episode (S3/S4 wiring)."""
    import controller_runtime
    from harness_core.gate import Gate
    from harness_core.ledger import Ledger
    from harness_core.runner import make_runner
    api = Path(api)
    api.mkdir(parents=True, exist_ok=False)
    pack = arms.load_pack(arm.pack, lay.corpus.resolve(), lay.tables_index.resolve())
    rules = arms.load_adapter_rules(adapter)
    policy = arms.load_policy(adapter)
    ledger = Ledger(pack, api / 'ledger.jsonl')
    runner = make_runner(ACTOR_PYTHON, extra_read=[str(lay.corpus.resolve())])
    gate = Gate(pack, rules, policy, runner, question, api_dir=api)
    controller = controller_runtime.MechController(ledger, gate, policy, time.monotonic,
                                                   requery_when_clean=REQUERY_WHEN_CLEAN)
    return controller, pack, ledger


def _fallback_answer(api: Path) -> str | None:
    """The last answer the model submitted (diagnostic: never the prediction)."""
    path = Path(api) / 'ledger.jsonl'
    if not path.is_file():
        return None
    from harness_core.ledger import load_events
    submitted = [e for e in load_events(path) if isinstance(e, C.Candidate) and e.via == 'submit']
    return submitted[-1].answer if submitted else None


def execute_episode(key: str, lay: Layout, arm: C.ArmSpec, prep: Mapping, *, broker_factory=None,
                    run_actor=None, broker_extra: Mapping | None = None,
                    temperature: float = arms.DEFAULT_TEMPERATURE, top_p: float | None = arms.DEFAULT_TOP_P) -> dict:
    """Run one prepared episode behind its broker; write prediction.json and summary.json. `temperature` and
    `top_p` are the campaign spec's (Campaign.run_one passes them; the audit checks every request against the
    freeze)."""
    broker_factory = broker_factory or broker.Broker
    run_actor = run_actor or acting.run_actor
    base, task = Path(prep['base']), prep['task']
    workspace, records, api = Path(prep['workspace']), Path(prep['records']), Path(prep['api'])
    adapter = Path(prep['adapter']) if prep['adapter'] else None
    controller = pack = ledger = None
    if arm.controller:
        controller, pack, ledger = build_controller(lay, arm, adapter, task['question'], api)
    env_extra, data_roots = actor_data(arm, lay)
    try:
        deadline = time.monotonic() + ACTOR_SECONDS
        options = {**BROKER_OPTIONS, 'temperature': temperature, 'top_p': top_p, **dict(broker_extra or {})}
        if controller is not None:
            options.update(controller=controller, actor_deadline=deadline)
        with broker_factory(key, api, **options) as relay:
            result = run_actor(workspace, records, prompt_for(task), lay.corpus, lay.index, timeout=ACTOR_SECONDS,
                               endpoint=relay.endpoint, arm=arm, env_extra=env_extra, data=data_roots,
                               permissions_instructions=False, temperature=options['temperature'],
                               top_p=options['top_p'])
    finally:
        for thing in (ledger, pack):
            close = getattr(thing, 'close', None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
    transport = read(api / 'summary.json')
    final = records / 'final.md'
    # A question is empty only when Codex produced no completed turn; transport drops are retried and
    # counted; there is no answer fallback in the official metric (DESIGN §2 L0).
    eligible = result.get('status') == 'completed'
    answer = final.read_text().strip() if eligible and final.is_file() else ''
    save(base / 'prediction.json', {'task_id': task['task_id'], 'answer': answer})
    if adapter is not None and prep['adapter_revision'] != revision(adapter):
        raise ValueError('Actor mutated its read-only adapter')
    if result.get('timed_out'):
        status = 'timeout'
        transport = dict(transport, transport_failed=False, failure_kind=None)
    else:
        status = ((transport.get('failure_kind') or 'transport_rejected') if transport.get('failed')
                  else result.get('status'))
    fallback = _fallback_answer(api) if arm.controller else None
    kit_after = result.get('kit_after') or {}
    summary = {'ordinal': prep['ordinal'], 'task_id': task['task_id'], 'role': prep['role'], 'arm': arm.name,
               'status': status, 'answer': answer, 'elapsed_seconds': result.get('elapsed_seconds'),
               'usage': transport.get('usage'), 'requests': transport.get('calls'),
               'wire_attempts': transport.get('wire_attempts'), 'rate_refusals': transport.get('rate_refusals'),
               'scheduler_wait_seconds': transport.get('scheduler_wait_seconds'),
               'usage_complete': transport.get('usage_complete'),
               'usage_unknown_calls': transport.get('usage_unknown_calls'),
               'transport_failed': transport.get('transport_failed'), 'failure_kind': transport.get('failure_kind'),
               'terminal_event': result.get('terminal_event'), 'error_text': result.get('error_text'),
               'call_failures': transport.get('call_failures', 0),
               'agents_md_bytes': result.get('agents_md_bytes'), 'readme_bytes': prep['readme_bytes'],
               'project_doc_max_bytes': result.get('project_doc_max_bytes'),
               'records': str(records), 'adapter': prep['adapter'], 'adapter_revision': prep['adapter_revision'],
               'notes_served': list(prep['notes']), 'kit_intact': kit_after.get('intact') is True,
               'answer_format_valid': bool(answer) and len(answer) <= 250 and len(answer.splitlines()) == 1,
               'prediction_sha256': sha(base / 'prediction.json'),
               'native_codex_items': result.get('completed_item_counts'),
               'out_tokens': (transport.get('usage') or {}).get('output_tokens'),
               'controller': ({k: transport.get(k) for k in CONTROLLER_FIELDS} if arm.controller else None),
               'fallback_answer': fallback,
               'fallback_differs': (fallback is not None and fallback.strip() != answer) if arm.controller else None}
    save(base / 'summary.json', summary)
    return summary


def episode(key: str, lay: Layout, arm: C.ArmSpec, task: Mapping, adapter: Path | None, base: Path, *,
            ordinal: int, role: str = 'scored', **options) -> dict:
    prep = prepare_episode(lay, arm, task, adapter, base, ordinal=ordinal, role=role)
    return execute_episode(key, lay, arm, prep, **options)

# ---------------------------------------------------------------------------------------------
# Freeze
# ---------------------------------------------------------------------------------------------

CODE_FILES = ('PROTOCOL.md', 'PREREG.md', '.task-paths.json', 'cards_domain.md')
CODE_TREES = ('harness_core', 'packs', 'adapters')


def code_files(code: Path) -> dict:
    names = [p.name for p in sorted(Path(code).glob('*.py'))] + [n for n in CODE_FILES if (Path(code) / n).is_file()]
    return {name: sha(Path(code) / name) for name in names}


def runtime_record(deep: bool) -> dict:
    record = kit.verify_frozen(deep=deep)
    return {k: record[k] for k in ('actor_python_prefix', 'manifest_sha256', 'tree_sha256', 'python_sha256',
                                   'versions', 'file_count', 'rg', 'rg_sha256')}


def broker_temperature(temperature):
    """The temperature the broker actually sends upstream when given `temperature` as its option
    (broker.normalized overrides the payload)."""
    return broker.normalized({'model': MODEL, 'input': []}, None, None, temperature).get('temperature')


def broker_top_p(top_p):
    """The top_p the broker actually sends upstream when given `top_p` as its option (None = none sent)."""
    return broker.normalized({'model': MODEL, 'input': []}, None, None, arms.DEFAULT_TEMPERATURE, top_p).get('top_p')


def settings_record(temperature, top_p=arms.DEFAULT_TOP_P) -> dict:
    """The frozen settings of a campaign whose spec declares `temperature` and `top_p` (arms.CampaignSpec;
    top_p None = not sent, recorded as null)."""
    return {'v9_settings': dict(acting.V9_SETTINGS), 'declared': acting.V9_DECLARED_SETTINGS,
            'permissions_instructions': False, 'temperature': temperature,
            'broker_temperature': broker_temperature(temperature),
            'top_p': top_p, 'broker_top_p': broker_top_p(top_p),
            'requery_when_clean': REQUERY_WHEN_CLEAN, 'actor_seconds': ACTOR_SECONDS,
            'broker_options': dict(BROKER_OPTIONS), 'learn_every': LEARN_EVERY,
            'learn_last_block': LEARN_LAST_BLOCK, 'policy_defaults': dict(C.POLICY_DEFAULTS),
            'text_budget': C.TEXT_BUDGET, 'notes_per_episode': C.NOTES_PER_EPISODE}


def freeze_record(lay: Layout, spec: arms.CampaignSpec, order: list[str], *, deep: bool = True) -> dict:
    w0 = arms.w0_dir(spec.arm, lay.adapters)
    return {'created_utc': utc(), 'profile': PROFILE, 'model': MODEL, 'contract': C.CONTRACT_VERSION,
            'campaign': spec.to_json(), 'arm': asdict(spec.arm), 'architecture': arms.architecture(spec.arm),
            'order': list(order), 'order_sha256': arms.order_sha256(order),
            'files': code_files(lay.code), 'trees': {name: snapshot(lay.code / name) for name in CODE_TREES
                                                     if (lay.code / name).is_dir()},
            'reference_tool_sha256': sha(lay.code / 'officeqa_tools.py'),
            'vendor_reward_sha256': (sha(lay.code / 'vendor/officeqa/reward.py')
                                     if (lay.code / 'vendor/officeqa/reward.py').is_file() else None),
            'codex': str(CODEX), 'codex_sha256': sha(CODEX),
            'frozen_runtime': runtime_record(deep), 'frozen_runtime_deep_checked': deep,
            'manifest_sha256': sha(lay.prepared / 'manifest.json'),
            'tables_index_sha256': sha(lay.tables_index) if uses_tables_index(spec.pack) else None,
            'w0': ({'dir': str(w0), 'snapshot': snapshot(w0), 'revision': revision(w0),
                    'model_visible': arms.model_visible(w0, spec.arm)} if w0 is not None else None),
            'prereg_sha256': sha(lay.code / 'PREREG.md') if (lay.code / 'PREREG.md').is_file() else None,
            'settings': settings_record(spec.temperature, spec.top_p)}


def verify_freeze(lay: Layout, frozen: Mapping, spec: arms.CampaignSpec, order: list[str]) -> None:
    """Refuse to continue when anything the freeze covers changed (checked at every block start)."""
    if frozen['campaign'] != spec.to_json() or frozen['order'] != list(order):
        raise ValueError('Campaign spec or question order changed after the freeze')
    for name, digest in frozen['files'].items():
        if sha(lay.code / name) != digest: raise ValueError('Frozen experiment source changed: ' + name)
    if code_files(lay.code).keys() != frozen['files'].keys():
        raise ValueError('Experiment source files were added or removed after the freeze')
    for name, tree in frozen['trees'].items():
        if snapshot(lay.code / name) != tree: raise ValueError(f'Frozen tree changed: {name}/')
    if sha(lay.code / 'officeqa_tools.py') != frozen['reference_tool_sha256']:
        raise ValueError('Frozen corpus helper changed')
    if sha(CODEX) != frozen['codex_sha256']: raise ValueError('Codex binary changed during the campaign')
    runtime = runtime_record(False)
    if any(runtime[k] != frozen['frozen_runtime'][k] for k in ('manifest_sha256', 'tree_sha256', 'python_sha256', 'rg_sha256')):
        raise ValueError('Frozen actor runtime changed during the campaign')
    if sha(lay.prepared / 'manifest.json') != frozen['manifest_sha256']: raise ValueError('Prepared manifest changed')
    if uses_tables_index(spec.pack) and sha(lay.tables_index) != frozen['tables_index_sha256']:
        raise ValueError('Tables index changed')
    if settings_record(spec.temperature, spec.top_p) != frozen['settings']:
        raise ValueError('v9 settings changed after the freeze')

# ---------------------------------------------------------------------------------------------
# Learner call (S5 modules) — keyword offer filtered by the learner's signature
# ---------------------------------------------------------------------------------------------


def call_learner(arm: C.ArmSpec, **offered) -> dict:
    """Call <text_learning|mechanism_learning>.run_learning with the keywords it declares (all of them if
    it takes **kwargs). A required parameter we do not offer is a wiring error (raised, never skipped)."""
    module = importlib.import_module(LEARNER_MODULES[arm.learner])
    function = module.run_learning
    parameters = inspect.signature(function).parameters
    takes_all = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values())
    kwargs = {k: v for k, v in offered.items() if takes_all or k in parameters}
    missing = [name for name, p in parameters.items()
               if p.default is inspect.Parameter.empty and name not in kwargs
               and p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)]
    if missing:
        raise TypeError(f'{module.__name__}.run_learning needs {missing}, which the campaign does not offer')
    return function(**kwargs)

# ---------------------------------------------------------------------------------------------
# The campaign
# ---------------------------------------------------------------------------------------------


@dataclass
class Hooks:
    """Injection points (tests; the defaults are the production path)."""
    episode: Callable[..., dict] | None = None      # (task, adapter, base, ordinal, role, temperature, top_p) -> summary
    learn: Callable[..., dict] | None = None        # (**offered) -> learner result
    scorer: Callable[..., float] | None = None      # reward.py score_answer (release grades, final grade)
    verify_data: bool = True                        # data.verify_prepared (needs the pinned reward.py)
    deep_runtime: bool = True                       # deep manifest check of the frozen runtime at freeze
    report: bool = True                             # rebuild report.html on every state change


class Campaign:
    def __init__(self, key: str, spec: arms.CampaignSpec, lay: Layout | None = None, *,
                 hooks: Hooks | None = None, stop: threading.Event | None = None):
        self.key, self.spec, self.arm = key, spec, spec.arm
        self.lay = lay or Layout()
        self.hooks = hooks or Hooks()
        self.stop = stop or threading.Event()
        self.lock = threading.RLock()
        self.barrier = barriers.from_spec(spec, stop=self.stop)
        self.state: dict = {}
        self.tasks: dict[str, dict] = {}
        self.order: list[str] = []
        self.frozen: dict = {}
        self.adapter: Path | None = None

    # -- plumbing ------------------------------------------------------------------------------
    def update(self):
        with self.lock:
            save(self.lay.state, self.state)
            if self.hooks.report:
                try:
                    import report
                    report.build(self.lay.root)
                except Exception as exc:
                    save(self.lay.root / 'report-error.json', {'error': str(exc).replace(self.key, '[REDACTED]')})

    def run_one(self, task, adapter, base, *, ordinal, role):
        if self.hooks.episode is not None:
            return self.hooks.episode(task=task, adapter=adapter, base=base, ordinal=ordinal, role=role,
                                      temperature=self.spec.temperature, top_p=self.spec.top_p)
        return episode(self.key, self.lay, self.arm, task, adapter, base, ordinal=ordinal, role=role,
                       temperature=self.spec.temperature, top_p=self.spec.top_p)

    def check_continue(self):
        if self.stop.is_set():
            raise CampaignStopped('stop requested')
        if self.barrier is not None:
            try:
                self.barrier.check()
            except barriers.BarrierStopped as exc:
                raise CampaignStopped(str(exc)) from exc
            except barriers.BarrierAborted:
                self.stop.set()            # a running learner / verification pool stops at its next check
                raise

    def scorer(self):
        if self.hooks.scorer is None:
            self.hooks.scorer = data.official_scorer(self.lay.root)
        return self.hooks.scorer

    # -- setup ---------------------------------------------------------------------------------
    def load(self, *, resume: bool) -> None:
        tasks = data.load_tasks(self.lay.root)
        if self.hooks.verify_data:
            save(self.lay.root / 'preflight/data-verification.json', data.verify_prepared(self.lay.root, spec=self.spec))
        else:
            if uses_tables_index(self.spec.pack):
                data.verify_tables_index(self.lay.root)
            data.check_subset(self.lay.root, self.spec)
        base = [t['task_id'] for t in tasks]
        selected = set(arms.selected_ids(base, self.spec.subset))
        self.tasks = {t['task_id']: t for t in tasks if t['task_id'] in selected}
        self.order = arms.campaign_order(base, self.spec)
        sent = broker_temperature(self.spec.temperature)
        if sent != self.spec.temperature:
            raise ValueError(f'the campaign declares temperature {self.spec.temperature} but the broker sends '
                             f'{sent}: change both together')
        sent_top_p = broker_top_p(self.spec.top_p)
        if sent_top_p != self.spec.top_p:
            raise ValueError(f'the campaign declares top_p {self.spec.top_p} but the broker sends {sent_top_p}: '
                             'change both together')
        if resume:
            if not self.lay.freeze.is_file() or not self.lay.state.is_file():
                raise ValueError('Nothing to resume: no freeze.json/state.json in this root')
            self.frozen = read(self.lay.freeze)
            verify_freeze(self.lay, self.frozen, self.spec, self.order)
            self.state = read(self.lay.state)
            if self.state.get('status') == 'completed':
                raise ValueError('This campaign already completed')
        else:
            if self.lay.freeze.exists() or self.lay.state.exists():
                raise ValueError('This formal campaign was already started; use resume')
            self.frozen = freeze_record(self.lay, self.spec, self.order, deep=self.hooks.deep_runtime)
            save(self.lay.freeze, self.frozen)
            w0 = arms.w0_dir(self.arm, self.lay.adapters)
            if w0 is not None:
                arms.check_adapter(w0, self.arm)
            self.state = {'status': 'running', 'profile': PROFILE, 'started_utc': utc(), 'model': MODEL,
                          'root': self.spec.root, 'arm': asdict(self.arm), 'repeat': self.spec.repeat,
                          'learn_every': LEARN_EVERY, 'question_count': len(self.order),
                          'order_sha256': arms.order_sha256(self.order), 'blocks': {}, 'results': [],
                          **({'subset': {'name': self.spec.subset.name, 'sha256': self.spec.subset.sha256,
                                         'count': len(self.order)}} if self.spec.subset else {}),
                          **({'temperature': self.spec.temperature}
                             if self.spec.temperature != arms.DEFAULT_TEMPERATURE else {}),
                          **({'top_p': self.spec.top_p} if self.spec.top_p is not None else {}),
                          'learning': [], 'attempts': [], 'warnings': [],
                          'adapter': ({'path': str(w0), 'revision': revision(w0), 'since_block': 1}
                                      if w0 is not None else None)}
        if self.barrier is not None:
            self.barrier.register_order(self.spec.repeat, arms.order_sha256(self.order), len(self.order))
        adapter = self.state.get('adapter')
        self.adapter = Path(adapter['path']) if adapter else None
        if self.adapter is not None and revision(self.adapter) != adapter['revision']:
            raise ValueError('The served adapter changed on disk')

    def blocks(self) -> list[range]:
        return arms.blocks_of(len(self.order), LEARN_EVERY)

    def first_open_block(self) -> int | None:
        for number, _ in enumerate(self.blocks(), start=1):
            if self.state['blocks'].get(str(number), {}).get('status') != 'done':
                return number
        return None

    # -- resume --------------------------------------------------------------------------------
    def move_partial(self, block: int) -> dict | None:
        """Move block `block`'s partial outputs to runs-aborted/<stamp>/ (block-level resume)."""
        positions = list(self.blocks()[block - 1])
        by_ordinal = {r['ordinal']: r for r in self.state['results']}
        runs = self.lay.runs()
        complete = all(p in by_ordinal and (runs / f'{p:03}' / 'summary.json').is_file()
                       and sha(runs / f'{p:03}' / 'prediction.json') == by_ordinal[p]['prediction_sha256']
                       for p in positions)
        target = self.lay.root / 'runs-aborted' / stamp()
        moved = []
        candidates = [self.lay.evidence(block), self.lay.evolution(block), self.lay.seal(block)]
        if not complete:
            candidates += [runs / f'{p:03}' for p in positions]
        for path in candidates:
            if path.exists():
                destination = target / path.relative_to(self.lay.root)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path), str(destination))
                moved.append(str(path.relative_to(self.lay.root)))
        if not complete:
            self.state['results'] = [r for r in self.state['results'] if r['ordinal'] not in positions]
        restored = self.restore_learner_files(block, target / 'learner-files')
        if restored:
            moved += [f'{name} (restored from learner-backups/{block:02})' for name in restored]
        record = {'block': block, 'utc': utc(), 'kept_episodes': complete, 'moved': moved,
                  'to': str(target.relative_to(self.lay.root)) if moved else None}
        started = self.state['blocks'].pop(str(block), None) or {}
        self.state['learning'] = [r for r in self.state['learning'] if r.get('block') != block]
        self.state.pop('active_learning_block', None)
        if started:
            # the adapter served when the block started (a crash after learning may have advanced it)
            self.state['adapter'] = started.get('adapter')
        adapter = self.state.get('adapter')
        self.adapter = Path(adapter['path']) if adapter else None
        if self.adapter is not None and revision(self.adapter) != adapter['revision']:
            raise ValueError('The adapter served at the start of the resumed block changed on disk')
        return record

    # -- learning ------------------------------------------------------------------------------
    def release_block(self, block: int, results: list[dict]) -> Path:
        """Seal the block's predictions, then release the non-withheld references into evidence/BB in the
        format evidence.validate_block reads. Withheld (issue-47) references are never read into evidence."""
        positions = list(self.blocks()[block - 1])
        ordered = sorted(results, key=lambda r: r['ordinal'])
        if [r['ordinal'] for r in ordered] != positions:
            raise ValueError('Evidence must be the exact current completed block')
        withheld = set(data.withheld_ids(self.lay.root))
        runs = self.lay.runs()
        sealed, held = [], []
        for result in ordered:
            base = runs / f'{result["ordinal"]:03}'
            task, prediction = read(base / 'task.json'), read(base / 'prediction.json')
            expected = self.order[result['ordinal'] - 1]
            if (result['task_id'] != expected or task['task_id'] != expected or prediction['task_id'] != expected
                    or task['question'] != self.tasks[expected]['question'] or not (base / 'summary.json').is_file()):
                raise ValueError('Completed block identity mismatch')
            if sha(base / 'prediction.json') != result['prediction_sha256']:
                raise ValueError('Original prediction changed before reference release')
            row = {'id': expected, 'ordinal': result['ordinal'], 'task_sha256': sha(base / 'task.json'),
                   'prediction_sha256': sha(base / 'prediction.json')}
            (held if expected in withheld else sealed).append(row)
        seal_path = self.lay.seal(block)
        if seal_path.exists(): raise FileExistsError('Block already sealed')
        seal = {'block': block, 'source_arm': self.arm.name, 'predictions_sealed': True,
                'reference_scope': 'completed_batch_only', 'sealed_utc': utc(), 'episodes': sealed,
                'withheld_episodes': held, 'withheld_reason': 'databricks/officeqa#47: reference never released'}
        save(seal_path, seal)
        keys = data.release_references(self.lay.root, [row['id'] for row in sealed]) if sealed else {}
        out = self.lay.evidence(block)
        out.mkdir(parents=True, exist_ok=False)
        episodes, files = [], []
        for row in sealed:
            base = runs / f'{row["ordinal"]:03}'
            destination = out / f'{row["ordinal"]:03}'
            destination.mkdir()
            if sha(base / 'prediction.json') != row['prediction_sha256']:
                raise ValueError('Sealed prediction changed during reference release')
            for name in ('task.json', 'prediction.json', 'summary.json'):
                shutil.copyfile(base / name, destination / name)
                files.append({'path': f'{row["ordinal"]:03}/{name}', 'source': str(base / name), 'sha256': sha(base / name)})
            if keys[row['id']] is None:
                raise ValueError('A withheld reference was about to be released')
            reference = destination / 'reference_answer.json'
            save(reference, {'task_id': row['id'], 'official_answer': keys[row['id']]})
            row['reference_answer_sha256'] = sha(reference)
            files.append({'path': f'{row["ordinal"]:03}/reference_answer.json',
                          'source': 'controller-filtered completed-block official answer', 'sha256': sha(reference)})
            rows = []
            events = base / 'records/events.jsonl'
            for line in (events.read_text().splitlines() if events.is_file() else []):
                try: event = json.loads(line)
                except ValueError: continue
                item = event.get('item', {})
                if event.get('type') in ('turn.completed', 'turn.failed') or (
                        event.get('type') == 'item.completed' and item.get('type') in (
                        'agent_message', 'command_execution', 'mcp_tool_call', 'file_change')):
                    rows.append(broker.public(event))
            target = destination / 'trajectory.jsonl'
            target.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows))
            files.append({'path': f'{row["ordinal"]:03}/trajectory.jsonl', 'source': str(events),
                          'transformation': 'Only public completed messages, tool results and terminal turn events',
                          'sha256': sha(target)})
            summary = read(base / 'summary.json')
            failure = destination / 'failure.json'
            save(failure, {k: summary.get(k) for k in (
                'status', 'failure_kind', 'terminal_event', 'error_text', 'requests', 'wire_attempts',
                'call_failures', 'elapsed_seconds', 'agents_md_bytes', 'project_doc_max_bytes')})
            files.append({'path': f'{row["ordinal"]:03}/failure.json', 'source': str(base / 'summary.json'),
                          'transformation': 'Termination and accounting fields only', 'sha256': sha(failure)})
            episodes.append({'id': row['id'], 'ordinal': row['ordinal'], 'directory': f'{row["ordinal"]:03}',
                             'run_dir': str(base.relative_to(self.lay.root))})
        seal['reference_released_utc'] = utc()
        save(seal_path, seal)
        save(out / 'manifest.json', {
            'source_arm': self.arm.name, 'block': block, 'episodes': episodes,
            'withheld': [{'id': row['id'], 'ordinal': row['ordinal']} for row in held],
            'includes_gold': True, 'includes_grades': False, 'includes_future_questions': False,
            'includes_baseline_traces': False, 'includes_source_hints': False, 'files': files,
            'batch_seal': {'file': seal_path.name, 'sha256': sha(seal_path)}})
        (out / 'README.md').write_text(
            '# One released block of this campaign\n\nmanifest.json indexes the released episodes: question '
            '(task.json), sealed answer (prediction.json), run status (summary.json, failure.json), public '
            'Codex events (trajectory.jsonl) and the official reference (reference_answer.json), released '
            'only after the block was sealed. Questions whose reference is disputed (databricks/officeqa#47) '
            'are listed under "withheld": they have no reference here. The broker records of each episode '
            'are read by the controller-side evidence builder from run_dir.\n')
        return out

    def verification_runner(self, block: int, source: Path | None) -> Callable[..., dict]:
        """learner_common.ActorRunner: actor_runner(adapter_dir, task, run_dir) -> summary. One verification
        episode under a candidate adapter, through the scored episode path (same arm, kit, broker, controller,
        wall clock), in a run_dir inside evolution/BB/verify/ (counted in cost as verification actors).
        Refuses questions that are not released in this campaign, adapters outside this round (other than
        the source) and run dirs outside the round's verify/ directory."""
        # released = the non-withheld questions of blocks 1..block (issue-47 references are never released,
        # so a learner cannot verify on them either)
        withheld = set(data.withheld_ids(self.lay.root))
        released = {self.order[p - 1]: p for b in range(1, block + 1) for p in self.blocks()[b - 1]
                     if self.order[p - 1] not in withheld}
        verify_root = (self.lay.evolution(block) / 'verify').resolve()
        output = self.lay.evolution(block).resolve()
        lock = threading.Lock()

        def actor_runner(adapter, task, run_dir):
            self.check_continue()
            task_id = task['task_id'] if isinstance(task, Mapping) else str(task)
            if task_id not in released:
                raise ValueError('verification is limited to released questions of this campaign')
            if isinstance(task, Mapping) and task.get('question') not in (None, self.tasks[task_id]['question']):
                raise ValueError('verification task text differs from the frozen question')
            base = Path(run_dir).resolve()
            if not base.is_relative_to(verify_root) or base == verify_root:
                raise ValueError('verification runs live under this round\'s evolution/BB/verify/')
            adapter = Path(adapter).resolve()
            if not (adapter == (source.resolve() if source else None) or adapter.is_relative_to(output)):
                raise ValueError('a verification adapter is the source or a candidate inside this round')
            arms.check_adapter(adapter, self.arm)
            with lock:
                if base.exists():
                    raise ValueError('A verification attempt is made once; prior directory exists')
                base.parent.mkdir(parents=True, exist_ok=True)
            summary = self.run_one(self.tasks[task_id], adapter, base, ordinal=released[task_id], role='verify')
            return summary
        return actor_runner

    LEARNER_FILES = ('learner-state.json', 'learning-ledger.jsonl')   # learner_common.STATE_FILE / LEDGER_FILE

    def backup_learner_files(self, block: int) -> dict:
        """Copy the learners' durable root files before a round, so a block-level resume can restore them
        (a round that crashed after saving its state must not leave it advanced)."""
        target = self.lay.root / 'learner-backups' / f'{block:02}'
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        record = {}
        for name in self.LEARNER_FILES:
            path = self.lay.root / name
            if path.is_file():
                shutil.copyfile(path, target / name)
                record[name] = sha(path)
            else:
                record[name] = None
        save(target / 'backup.json', record)
        return record

    def restore_learner_files(self, block: int, into: Path) -> list[str]:
        backup = self.lay.root / 'learner-backups' / f'{block:02}'
        if not (backup / 'backup.json').is_file():
            return []
        record, restored = read(backup / 'backup.json'), []
        for name, digest in record.items():
            path = self.lay.root / name
            current = sha(path) if path.is_file() else None
            if current == digest:
                continue
            if path.is_file():
                into.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path), str(into / name))
            if digest is not None:
                shutil.copyfile(backup / name, path)
            restored.append(name)
        return restored

    def compute_runner(self):
        from harness_core.runner import make_runner
        return make_runner(ACTOR_PYTHON, extra_read=[str(self.lay.corpus.resolve())])

    def grader(self):
        import evidence
        return evidence.Grader(score=self.scorer(), name='prepared/private/reward.py (pinned sha)')

    def learn(self, block: int, evidence: Path) -> dict:
        output = self.lay.evolution(block)
        source = self.adapter
        runner = self.verification_runner(block, source)
        # learner_common.run_round keywords (S5): the pack with this root's corpus and tables index, the
        # controller-side grader, the root, the W0 (source lineage), the withheld ids.
        offered = {'key': self.key, 'source': source, 'evidence': evidence, 'output': output, 'block': block,
                   'stop': self.stop, 'actor_runner': runner,
                   'pack': arms.load_pack(self.arm.pack, self.lay.corpus.resolve(), self.lay.tables_index.resolve()),
                   'grader': self.grader(),
                   'root': self.lay.root, 'w0': arms.w0_dir(self.arm, self.lay.adapters),
                   'exclude': tuple(data.withheld_ids(self.lay.root)),
                   # the gate's compute runner (frozen python, corpus readable), so offline rule admission
                   # replays offers exactly as the live gate executes them (S5 default: no corpus read)
                   'runner': self.compute_runner()}
        # A learner session (model calls, verification actors) can run for hours; without a watcher it
        # would notice a group ABORT only when its next verification actor starts. The watcher sets the
        # stop event the learner checks, so the round ends promptly and is reported as an abort.
        finished = threading.Event()

        def watch():
            poll = self.spec.barrier.poll_s if self.spec.barrier is not None else 5.0
            while not finished.wait(poll):
                if self.barrier is not None and self.barrier.aborted() is not None:
                    self.stop.set()
                    return
        watcher = threading.Thread(target=watch, name=f'abort-watch-{block:02}', daemon=True)
        if self.barrier is not None:
            watcher.start()
        try:
            if self.hooks.learn is not None:
                learned = self.hooks.learn(**offered)
            else:
                learned = call_learner(self.arm, **offered)
        finally:
            finished.set()
        if self.barrier is not None:
            info = self.barrier.aborted()
            if info is not None:
                self.stop.set()
                raise barriers.BarrierAborted(f'barrier aborted by {info.get("root")} during learning round '
                                              f'{block}: {info.get("reason")}')
        self.check_continue()
        if not isinstance(learned, Mapping) or learned.get('status') == 'failed':
            raise LearnerFailed(f'learning round {block} failed: '
                                f'{(learned or {}).get("error") if isinstance(learned, Mapping) else learned!r}')
        candidate = Path(learned['candidate']).resolve()
        if candidate != source.resolve() and not candidate.is_relative_to(output.resolve()):
            raise LearnerFailed(f'round {block} returned an adapter outside its output directory')
        if bool(learned.get('activated')) != (candidate != source.resolve()):
            raise LearnerFailed(f'round {block}: activated={learned.get("activated")} disagrees with the candidate')
        try:
            checked = arms.check_adapter(candidate, self.arm)
        except (OSError, ValueError) as exc:
            raise LearnerFailed(f'round {block} returned an unusable adapter: {exc}') from exc
        return {**{k: v for k, v in learned.items() if isinstance(v, (str, int, float, bool, type(None), list, dict))},
                'candidate': str(candidate), 'candidate_revision': revision(candidate), 'check': checked}

    # -- loop ----------------------------------------------------------------------------------
    def run(self, *, resume: bool = False) -> dict:
        try:
            self.load(resume=resume)
            attempt = {'started_utc': utc(), 'resume': resume}
            start = self.first_open_block()
            if resume and start is not None:
                attempt['moved'] = self.move_partial(start)
            self.state['attempts'].append(attempt)
            self.state['status'] = 'running'
            self.update()
            if start is not None:
                self.run_blocks(start)
            self.finalize()
            return self.state
        except (CampaignStopped, barriers.BarrierStopped) as exc:
            # The group cannot advance without this root: stop the others at their next episode.
            if self.barrier is not None:
                self.barrier.abort(f'{self.spec.root}: stopped ({exc})')
            if self.state:
                self.state['status'] = 'stopped'
                self.state['stop_reason'] = str(exc)
                self.update()
            raise
        except barriers.BarrierAborted as exc:
            if self.state:
                self.state['status'] = 'aborted'
                self.state['error'] = str(exc)
                self.update()
            raise
        except Exception as exc:
            text = (type(exc).__name__ + ': ' + str(exc)).replace(self.key, '[REDACTED]')
            if self.barrier is not None:
                self.barrier.abort(f'{self.spec.root}: {text}')
            if self.state:
                self.state['status'] = 'stopped_on_error'
                self.state['error'] = text
                self.update()
            raise

    def run_blocks(self, start: int) -> None:
        blocks = self.blocks()
        runs = self.lay.runs()
        for number in range(start, len(blocks) + 1):
            if self.barrier is not None:
                self.barrier.pass_block(number - 1)
            self.check_continue()
            verify_freeze(self.lay, self.frozen, self.spec, self.order)
            self.verify_learning_seals()
            record = {'status': 'running', 'start_utc': utc(), 'start_unix': time.time(),
                      'ordinals': list(blocks[number - 1]),
                      'adapter': dict(self.state['adapter']) if self.state.get('adapter') else None,
                      'adapter_revision': self.state['adapter']['revision'] if self.state.get('adapter') else None}
            self.state['blocks'][str(number)] = record
            self.update()
            done = {r['ordinal']: r for r in self.state['results']}
            results = []
            for ordinal in blocks[number - 1]:
                if ordinal in done:                      # kept by a resume (complete, unchanged)
                    results.append(done[ordinal])
                    continue
                self.check_continue()
                task = self.tasks[self.order[ordinal - 1]]
                result = self.run_one(task, self.adapter, runs / f'{ordinal:03}', ordinal=ordinal, role='scored')
                results.append(result)
                with self.lock:
                    self.state['results'].append(result)
                    self.update()
                print(json.dumps({'arm': self.arm.name, 'ordinal': ordinal, 'status': result.get('status'),
                                  'calls': result.get('requests')}), flush=True)
            record['actors_end_utc'] = utc()
            if all(r.get('transport_failed') and not r.get('answer') for r in results):
                warning = {'warning': 'all_actors_failed_transport', 'arm': self.arm.name, 'block': number}
                self.state['warnings'].append(warning)
                print(json.dumps(warning), flush=True)
            if self.arm.learner is not None:
                if number < len(blocks) or LEARN_LAST_BLOCK:
                    self.check_continue()
                    evidence = self.release_block(number, results)
                    record['learner_files'] = self.backup_learner_files(number)
                    self.state['active_learning_block'] = number
                    self.update()
                    learned = self.learn(number, evidence)
                    self.adapter = Path(learned['candidate'])
                    self.state['adapter'] = {'path': learned['candidate'], 'revision': learned['candidate_revision'],
                                             'since_block': number + 1}
                    self.state['learning'].append({**learned, 'block': number, 'output': str(self.lay.evolution(number))})
                    self.state.pop('active_learning_block', None)
                    print(json.dumps({'learning_block': number, 'status': learned.get('status'),
                                      'activated': learned.get('activated')}), flush=True)
                else:
                    self.state['learning'].append({'block': number, 'status': 'skipped_unused', 'activated': False})
            record.update(status='done', end_utc=utc(), end_unix=time.time())
            self.update()
            if self.barrier is not None:
                self.barrier.arrive(number, {'arm': self.arm.name, 'actors_end_utc': record['actors_end_utc']})

    def verify_learning_seals(self) -> None:
        runs = self.lay.runs()
        for path in sorted((self.lay.root / 'private-learning-seals').glob('*.json')):
            seal = read(path)
            for episode_row in seal['episodes'] + seal.get('withheld_episodes', []):
                if sha(runs / f"{episode_row['ordinal']:03}" / 'prediction.json') != episode_row['prediction_sha256']:
                    raise ValueError('A prediction changed after reference release')

    def finalize(self) -> None:
        self.verify_learning_seals()
        evaluation = self.lay.root / 'private-evaluation'
        if not (evaluation / 'predictions-seal.json').is_file():
            rows = sorted(self.state['results'], key=lambda r: r['ordinal'])
            if [r['task_id'] for r in rows] != self.order:
                raise ValueError('Every question of this repeat must have exactly one scored episode')
            target = self.lay.root / f'{self.arm.name}-predictions.jsonl'
            text = ''.join(json.dumps({'task_id': row['task_id'], 'answer': row['answer']}) + '\n' for row in rows)
            if target.exists():
                # a crash between writing and sealing: reuse only an identical file
                if target.read_text() != text:
                    raise ValueError('An unsealed predictions file with different content exists')
            else:
                with target.open('x') as stream:
                    stream.write(text)
            data.seal_predictions(self.lay.root, self.arm.name, target)
        self.state['status'] = 'grading'
        self.update()
        if not (evaluation / 'paired-metrics.json').is_file():
            # production (verify_data): the pinned reward.py after a full data re-verification; tests inject
            self.state['metrics'] = data.grade(self.lay.root, order=self.order,
                                               scorer=None if self.hooks.verify_data else self.hooks.scorer)
        else:
            self.state['metrics'] = read(evaluation / 'paired-metrics.json')
        verify_freeze(self.lay, self.frozen, self.spec, self.order)
        if self.barrier is not None:
            self.barrier.arrive(len(self.blocks()), {'arm': self.arm.name, 'final': True})
        self.state['status'] = 'completed'
        self.state['finished_utc'] = utc()
        self.update()


def run(key, *, resume=False, stop=None):
    """Formal entry point (run.py): this root's campaign spec from .task-paths.json."""
    if CAMPAIGN is None:
        raise ValueError('This root has no v9 campaign spec (.task-paths.json "arm")')
    # v8 FULL: unbounded inference; verification actors may run several at once.
    broker.rate_control.GLOBAL = broker.rate_control.RateScheduler(max_in_flight=float('inf'), min_interval=0)
    return Campaign(key, CAMPAIGN, Layout(), stop=stop).run(resume=resume)
