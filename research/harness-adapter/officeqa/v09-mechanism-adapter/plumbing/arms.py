"""v9 arms (DESIGN §1, §4): the one place where `.task-paths.json` becomes an experimental condition.

A v9 root runs exactly one arm for one repeat. Its `.task-paths.json` names it:

    {"profile": "v9",
     "arm": "mech-evolve" | {"name": ..., "pack": ..., "l1_tools": ..., "controller": ..., "seed": ...,
                             "learner": ..., "notes": ...},
     "repeat": 2, "pack": "officeqa", "order_seed": "v9-officeqa-pro133-order",
     "barrier": {"dir": "/abs/barrier-r2", "roots": ["v9-r2-a", "v9-r2-b", ...], "timeout_s": 43200},
     # optional (wave 3, pilot):
     "subset": {"name": "pilot-first50", "task_ids": [...], "sha256": subset_sha256(task_ids)},
     "temperature": 0,
     "top_p": 0.95}            # optional; absent = no top_p is sent (v8)

`parse_config` turns that into a `CampaignSpec` (contracts.ArmSpec + repeat + order seed + barrier
[+ subset + temperature + top_p]) and validates it once. A declared `subset` runs only those questions (in
this repeat's seeded order, blocks of 5); `temperature` is the actor temperature the broker sends on every
upstream call (default 0, the v8 value); `top_p`, when declared, is sent on every upstream call too (absent =
the body carries no top_p, as in v8). Without these keys a root is exactly the full-run root.
Nothing anywhere infers an arm from a substring of a profile or directory name (the v8 rule `'cards' in PROFILE else 'mwm'` silently turned unknown names into MWM; engineering review #8).

This module also owns the arm-derived pieces every other module needs:
- `question_order`: the seeded permutation of the task ids for one repeat (identical for every arm of
  that repeat; different between repeats; validity review P5c);
- `w0_dir` / `build_w0` / `check_w0`: the W0 adapters `adapters/text-w0` (seed knowledge as text) and
  `adapters/mech-w0` (the same knowledge as executed rules + a minimal SEED.md), DESIGN §1 seed parity;
- `model_visible`: the byte count of adapter text the model can see (notes + SEED.md + rule finding
  templates + the controller's fixed wording), checked against contracts.TEXT_BUDGET.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields, replace
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from typing import Any, Mapping, Sequence

from harness_core import contracts as C

HERE = Path(__file__).resolve().parent
ADAPTERS = HERE / 'adapters'
W0_NAMES = {'text': 'text-w0', 'rules': 'mech-w0'}          # ArmSpec.seed -> adapters/<name>
ARCHITECTURES = {'text': 'text', 'rules': 'mechanism'}      # ArmSpec.seed -> adapter.json architecture
PACKS = ('officeqa', 'toy', 'spreadsheet')                   # known pack names (packs/<name>/)
# packs whose W0 adapters can be built here (`arms.py build --pack <name>` writes adapters/*-w0 of a root's
# code copy for that pack; check_adapter refuses a W0 built for another pack)
W0_PACKS = ('officeqa', 'toy')
CUSTOM_NAME = re.compile(r'^[a-z][a-z0-9-]{0,31}$')
ROOT_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
BARRIER_TIMEOUT_MAX = 7 * 24 * 3600
# `.task-paths.json` keys a v9 root may carry: the campaign spec (this module) and the path overrides
# paths.py reads. Anything else is refused, so a typo can never silently fall back to a default.
CAMPAIGN_KEYS = {'profile', 'arm', 'repeat', 'pack', 'order_seed', 'barrier', 'subset', 'temperature', 'top_p'}
PATH_KEYS = {'framework', 'data_cache', 'codex', 'frozen_runtime', 'actor_python_prefix', 'actor_rg',
             'tables_index'}
BARRIER_KEYS = {'dir', 'roots', 'timeout_s', 'poll_s'}
SUBSET_KEYS = {'name', 'task_ids', 'sha256'}
SUBSET_NAME = re.compile(r'^[a-z0-9][a-z0-9._-]{0,63}$')
# The actor temperature the broker sends when `.task-paths.json` declares none (the v8 value; PREREG §5).
DEFAULT_TEMPERATURE = 0
TEMPERATURE_MAX = 2.0
# The actor top_p when `.task-paths.json` declares none: None = the broker adds no top_p to the upstream body
# (the v8 behaviour). A declared value is a number in (0, 1], sent on every upstream call.
DEFAULT_TOP_P = None
# The root directory name is model-visible: Codex sends the workspace path (<root>/runs/episodes/NNN/
# workspace) in every request's environment context, and README.md prints the corpus path under the root.
# A root named after its arm (v8: officeqa-pro133-cards-v8-work) tells the actor which condition it is
# in, so root names must not contain these fragments (review S6: arm identity in model-visible text).
ARM_REVEALING = ('plain', 'text', 'mech', 'static', 'evolv', 'rule', 'control', 'prompt', 'card', 'learn',
                 'seed', 'note', 'adapt', 'mwm', 'baseline')


def revealing_fragments(name: str, arm: 'C.ArmSpec | None' = None) -> list[str]:
    """Arm-identifying fragments in a model-visible name (root directory, run path)."""
    lowered = str(name).lower()
    found = [w for w in ARM_REVEALING if w in lowered]
    if 'none' in re.split(r'[^a-z0-9]+', lowered):
        found.append('none')
    if arm is not None and arm.name.lower() in lowered and arm.name.lower() not in found:
        found.append(arm.name.lower())
    return found

# ---------------------------------------------------------------------------------------------
# Spec parsing
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class BarrierSpec:
    dir: Path                     # shared directory of the repeat group (outside every root)
    roots: tuple[str, ...]        # every root of the repeat group (this root included)
    timeout_s: float              # how long a root waits for the slowest one before writing ABORT
    poll_s: float = 5.0


@dataclass(frozen=True)
class SubsetSpec:
    """A declared question subset (wave 3, pilot): the ids and the sha256 of exactly that list."""
    name: str
    task_ids: tuple[str, ...]
    sha256: str

    def to_json(self) -> dict:
        return {'name': self.name, 'task_ids': list(self.task_ids), 'sha256': self.sha256}


@dataclass(frozen=True)
class CampaignSpec:
    arm: C.ArmSpec
    repeat: int
    pack: str
    order_seed: str
    root: str                     # this root's name (its directory name); must be in barrier.roots
    barrier: BarrierSpec | None
    subset: SubsetSpec | None = None           # None = every prepared question (the full run)
    temperature: float = DEFAULT_TEMPERATURE   # sent by the broker on every upstream call
    top_p: float | None = DEFAULT_TOP_P        # None = not sent; else sent by the broker on every upstream call

    def to_json(self) -> dict:
        """The recorded spec (freeze.json "campaign"). `subset`, a default `temperature` and an absent `top_p`
        are left out, so a full run at temperature 0 without top_p records exactly the pre-wave-3 spec."""
        value = asdict(self)
        if self.barrier is not None:
            value['barrier'] = {**asdict(self.barrier), 'dir': str(self.barrier.dir),
                                'roots': list(self.barrier.roots)}
        if self.subset is None:
            value.pop('subset')
        else:
            value['subset'] = self.subset.to_json()
        if self.temperature == DEFAULT_TEMPERATURE:
            value.pop('temperature')
        if self.top_p is None:
            value.pop('top_p')
        return value


def _bool(value, what):
    if not isinstance(value, bool):
        raise ValueError(f'{what} must be true or false')
    return value


def check_arm(arm: C.ArmSpec) -> C.ArmSpec:
    """contracts.ArmSpec.validate plus the orchestration invariants every v9 arm must satisfy."""
    if not isinstance(arm, C.ArmSpec):
        raise TypeError('an arm is a harness_core.contracts.ArmSpec')
    for name in ('l1_tools', 'controller', 'notes'):
        _bool(getattr(arm, name), f'arm.{name}')
    if arm.seed not in ('none', 'text', 'rules'):
        raise ValueError('arm.seed must be none, text or rules')
    if arm.learner not in (None, 'text', 'mechanism'):
        raise ValueError('arm.learner must be null, text or mechanism')
    if arm.pack not in PACKS:
        raise ValueError(f'unknown pack {arm.pack!r}')
    if not isinstance(arm.name, str) or not CUSTOM_NAME.match(arm.name):
        raise ValueError('arm.name must match ^[a-z][a-z0-9-]{0,31}$')
    arm.validate()
    if arm.seed == 'rules' and not arm.controller:
        raise ValueError('rules are executed by the controller: seed=rules needs controller=true')
    if arm.controller and not arm.l1_tools:
        raise ValueError('controller arms also receive the shared L1 tools (the seed rules read calc frames)')
    if arm.seed == 'none' and (arm.notes or arm.learner is not None):
        raise ValueError('an arm without seed knowledge has no adapter: no notes, no learner')
    if arm.learner is not None and not arm.notes:
        raise ValueError('both learners write scoped notes: a learner arm renders notes')
    standard = C.STANDARD_ARMS.get(arm.name)
    if standard is not None and replace(standard, pack=arm.pack) != arm:
        raise ValueError(f'arm {arm.name!r} is a standard arm name; its fields must be the standard ones '
                         f'(use another name for a custom arm)')
    return arm


def parse_arm(value: str | Mapping[str, Any], *, pack: str | None = None) -> C.ArmSpec:
    """A standard arm name, or a full spec object with exactly the ArmSpec fields."""
    if isinstance(value, str):
        if value not in C.STANDARD_ARMS:
            raise ValueError(f'unknown arm {value!r}; standard arms: {sorted(C.STANDARD_ARMS)} '
                             '(a custom arm is given as a full spec object)')
        arm = C.STANDARD_ARMS[value]
        return check_arm(replace(arm, pack=pack) if pack is not None else arm)
    if not isinstance(value, Mapping):
        raise ValueError('arm must be a standard arm name or a full spec object')
    names = [f.name for f in fields(C.ArmSpec)]
    if set(value) != set(names):
        raise ValueError(f'an arm spec object needs exactly the fields {names}; '
                         f'missing {sorted(set(names) - set(value))}, unknown {sorted(set(value) - set(names))}')
    arm = C.ArmSpec(**{name: value[name] for name in names})
    if pack is not None and arm.pack != pack:
        raise ValueError(f'arm.pack {arm.pack!r} differs from the root pack {pack!r}')
    return check_arm(arm)


def parse_barrier(value: Mapping[str, Any] | None, root: str) -> BarrierSpec | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or not {'dir', 'roots', 'timeout_s'} <= set(value) or set(value) - BARRIER_KEYS:
        raise ValueError(f'barrier needs exactly dir, roots, timeout_s (optional poll_s); got {sorted(value)}')
    directory = Path(value['dir'])
    if not directory.is_absolute():
        raise ValueError('barrier.dir must be an absolute path')
    roots = value['roots']
    if (not isinstance(roots, list) or not roots or len(set(roots)) != len(roots)
            or not all(isinstance(r, str) and ROOT_NAME.match(r) for r in roots)):
        raise ValueError('barrier.roots must be a non-empty list of distinct root names')
    if root not in roots:
        raise ValueError(f'this root {root!r} is not listed in barrier.roots')
    timeout = value['timeout_s']
    poll = value.get('poll_s', 5.0)
    for what, number in (('timeout_s', timeout), ('poll_s', poll)):
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not 0 < number <= BARRIER_TIMEOUT_MAX:
            raise ValueError(f'barrier.{what} must be a positive number of seconds')
    if poll > timeout:
        raise ValueError('barrier.poll_s must not exceed timeout_s')
    return BarrierSpec(directory, tuple(roots), float(timeout), float(poll))


def subset_sha256(task_ids: Sequence[str]) -> str:
    """sha256 of the declared id list exactly as given: json.dumps(list, separators=(',', ':'))."""
    return hashlib.sha256(json.dumps(list(task_ids), separators=(',', ':')).encode('utf-8')).hexdigest()


def parse_subset(value: Mapping[str, Any] | None) -> SubsetSpec | None:
    """`.task-paths.json` "subset": {name, task_ids, sha256}; the hash must be the hash of the list."""
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != SUBSET_KEYS:
        raise ValueError(f'subset needs exactly name, task_ids, sha256; got {sorted(value) if isinstance(value, Mapping) else value!r}')
    name, ids, digest = value['name'], value['task_ids'], value['sha256']
    if not isinstance(name, str) or not SUBSET_NAME.match(name):
        raise ValueError('subset.name must match ^[a-z0-9][a-z0-9._-]{0,63}$')
    if (not isinstance(ids, list) or not ids or len(set(ids)) != len(ids)
            or not all(isinstance(i, str) and i.strip() == i and i for i in ids)):
        raise ValueError('subset.task_ids must be a non-empty list of distinct task ids')
    if not isinstance(digest, str) or digest != subset_sha256(ids):
        raise ValueError('subset.sha256 is not the sha256 of subset.task_ids (arms.subset_sha256)')
    return SubsetSpec(name, tuple(ids), digest)


def parse_temperature(value: Any) -> float:
    """`.task-paths.json` "temperature" (when the key is present): a number in [0, TEMPERATURE_MAX]."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= TEMPERATURE_MAX:
        raise ValueError(f'temperature must be a number in [0, {TEMPERATURE_MAX}]')
    return value


def parse_top_p(value: Any) -> float:
    """`.task-paths.json` "top_p" (when the key is present): a number in (0, 1]. Leave the key out to send none."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 1:
        raise ValueError('top_p must be a number in (0, 1] (leave the key out to send no top_p)')
    return value


def selected_ids(base_ids: Sequence[str], subset: SubsetSpec | None) -> list[str]:
    """The questions a root runs, in the base order: all of them, or the declared subset (every subset
    id must be a prepared question)."""
    base = list(base_ids)
    if subset is None:
        return base
    missing = sorted(set(subset.task_ids) - set(base))
    if missing:
        raise ValueError(f'subset {subset.name!r} names ids that are not prepared questions: {missing[:5]}')
    wanted = set(subset.task_ids)
    return [task for task in base if task in wanted]


def campaign_order(base_ids: Sequence[str], spec: 'CampaignSpec') -> list[str]:
    """This root's question order: the seeded order of repeat r over the selected questions. For a subset it
    equals the full repeat-r order with the other questions removed."""
    return question_order(selected_ids(base_ids, spec.subset), spec.order_seed, spec.repeat)


def parse_config(config: Mapping[str, Any], *, root_name: str) -> CampaignSpec:
    """The campaign spec of one v9 root. Refuses unknown keys, a non-v9 profile and every inconsistency."""
    if not isinstance(config, Mapping):
        raise ValueError('.task-paths.json must be a JSON object')
    unknown = set(config) - CAMPAIGN_KEYS - PATH_KEYS
    if unknown:
        raise ValueError(f'unknown .task-paths.json keys: {sorted(unknown)}')
    if config.get('profile', 'v9') != 'v9':
        raise ValueError(f"profile must be 'v9' (got {config.get('profile')!r}); v8 profiles are not v9 arms")
    if 'arm' not in config:
        raise ValueError('a v9 root names its arm (.task-paths.json "arm")')
    if not isinstance(root_name, str) or not ROOT_NAME.match(root_name):
        raise ValueError(f'root name {root_name!r} is not a plain directory name')
    pack = config.get('pack', 'officeqa')
    if pack not in PACKS:
        raise ValueError(f'unknown pack {pack!r}')
    arm = parse_arm(config['arm'], pack=pack)
    revealing = revealing_fragments(root_name, arm)
    if revealing:
        raise ValueError(f'root name {root_name!r} reveals the arm to the actor (the workspace and corpus paths '
                         f'are model-visible): contains {revealing}; use a neutral name such as "v9-r1-a"')
    repeat = config.get('repeat')
    if isinstance(repeat, bool) or not isinstance(repeat, int) or not 1 <= repeat <= 99:
        raise ValueError('repeat must be an integer 1-99')
    seed = config.get('order_seed')
    if not isinstance(seed, str) or not seed.strip() or len(seed) > 200:
        raise ValueError('order_seed must be a non-empty string (shared by every root of the study)')
    barrier = parse_barrier(config.get('barrier'), root_name)
    return CampaignSpec(arm=arm, repeat=repeat, pack=pack, order_seed=seed, root=root_name, barrier=barrier,
                        subset=parse_subset(config.get('subset')),
                        temperature=(parse_temperature(config['temperature']) if 'temperature' in config
                                     else DEFAULT_TEMPERATURE),
                        top_p=parse_top_p(config['top_p']) if 'top_p' in config else DEFAULT_TOP_P)


def architecture(arm: C.ArmSpec) -> str:
    """'none' (no adapter), 'text' or 'mechanism': an explicit function of arm.seed, never a substring."""
    return ARCHITECTURES.get(arm.seed, 'none')


def learning_type(arm: C.ArmSpec) -> str:
    return {None: 'static: no release to a learner, no learning',
            'text': 'text learner: scoped notes admitted after paired actor re-runs (support x2 + 2 controls)',
            'mechanism': 'mechanism learner: rule edits (fixed vocabulary), policy knobs and scoped notes; '
                         'rules admitted by counterfactual + precision replay, notes by paired actor re-runs'}[arm.learner]

# ---------------------------------------------------------------------------------------------
# Question order (validity review P5c): randomised per repeat, identical across arms of a repeat
# ---------------------------------------------------------------------------------------------


def order_key(order_seed: str, repeat: int, task_id: str) -> str:
    return hashlib.sha256(f'{order_seed}\0repeat={repeat}\0{task_id}'.encode('utf-8')).hexdigest()


def question_order(task_ids: Sequence[str], order_seed: str, repeat: int) -> list[str]:
    """Ascending SHA-256(order_seed NUL 'repeat=r' NUL task_id), task-id tie-break."""
    ids = list(task_ids)
    if len(set(ids)) != len(ids) or not all(isinstance(i, str) and i for i in ids):
        raise ValueError('task ids must be distinct non-empty strings')
    return sorted(ids, key=lambda i: (order_key(order_seed, repeat, i), i))


def order_sha256(order: Sequence[str]) -> str:
    return hashlib.sha256(json.dumps(list(order), separators=(',', ':')).encode()).hexdigest()


def blocks_of(n: int, every: int) -> list[range]:
    """1-based positions per block: 133 with every=5 -> 26 blocks of 5 and a final block of 3."""
    return [range(start, min(start + every, n + 1)) for start in range(1, n + 1, every)]

# ---------------------------------------------------------------------------------------------
# W0 adapters (DESIGN §1 seed parity; contracts "Adapter layouts")
# ---------------------------------------------------------------------------------------------

# The benchmark-neutral half of the seed knowledge, written once as text for the text arms. It says in
# words what the mech arms execute: the loop guard (L3), the core seed rules answer_count, scale,
# competing_candidates, grounded and recompute_final (L4), and the provenance advice.
CORE_ADVICE = """# Working advice

- Loops: a command repeated with the same output will not change. If a command already showed you its
  output, do something different (another table, file, period or wording) or answer.
- Compute every final number with `calc`, not in your head, and answer with the value it printed,
  rounded as the question asks.
- Provenance: use numbers you have seen printed by a tool. Before you rely on a value, print the line
  it comes from, so the number in your calculation is the number in the source.
- Answer count and format: give exactly as many values as the question asks for, in the order and the
  format it asks for (for example a bracketed list when it asks for one).
- Percent or fraction: when the question asks for a percent, answer in percent (12.5, not 0.125);
  when it asks for a ratio, fraction or decimal, do not multiply by 100.
- Alternatives: when two candidate values fit (two tables, periods, printings or readings), weigh them
  against the question's exact wording and answer the one it asks for; do not average them.
"""

# The mech arms' only seed text: how to finish, and what the gate's messages mean. The knowledge itself
# is in rules/ (executed, not read).
MECH_SEED = """# Finishing a question

- When you have an answer, run `submit --answer "<answer>"` (add `--alt "<other>"` when you also
  considered another value). The harness checks it and prints either `recorded` or one finding.
- A finding names one possible problem with the submitted answer. When it shows `Offer: <value>`, the
  harness computed that alternative itself, from your own calculation with one input replaced.
  Accept: run the `submit --answer "<value>"` line it prints. Keep: run
  `submit --answer "<your answer>" --keep "<reason>"`. Either is fine; the choice is yours.
- After `recorded`, end your turn with exactly that answer as your final message.
- Lines starting `[harness:...]` come from the harness (loop notes, a summary of your work after a
  context compaction, submit results); they are not part of the task.
"""


def w0_dir(arm: C.ArmSpec, adapters: Path | None = None) -> Path | None:
    """The arm's W0 adapter directory (None for an arm without seed knowledge)."""
    if arm.seed == 'none':
        return None
    if arm.pack not in W0_PACKS:
        raise ValueError(f'no W0 adapter is built for pack {arm.pack!r}')
    return Path(adapters or ADAPTERS) / W0_NAMES[arm.seed]


def load_pack(name: str, corpus=None, index=None):
    """The contracts.Pack object of a pack name (explicit table, no import by string)."""
    if name == 'officeqa':
        from packs.officeqa.pack import OfficeQAPack
        return OfficeQAPack(corpus, index)
    if name == 'toy':
        from packs.toy.pack import ToyPack
        return ToyPack()
    raise ValueError(f'pack {name!r} has no Pack implementation in this plumbing')


def w0_files(kind: str, pack) -> dict[str, str]:
    """The files of a W0 adapter as {relative path: text}. kind: 'text' | 'rules'."""
    from harness_core import rules as R
    header = {'contract': C.CONTRACT_VERSION, 'pack': pack.name, 'revision': 'w0'}
    if kind == 'text':
        seed = CORE_ADVICE.rstrip() + '\n\n' + pack.seed_text().rstrip() + '\n'
        return {'adapter.json': json.dumps({'architecture': 'text', **header}, indent=2, sort_keys=True) + '\n',
                'SEED.md': seed, 'notes/.keep': ''}
    if kind == 'rules':
        files = {'adapter.json': json.dumps({'architecture': 'mechanism', **header}, indent=2, sort_keys=True) + '\n',
                 'SEED.md': MECH_SEED, 'notes/.keep': '',
                 'policy.json': json.dumps(C.POLICY_DEFAULTS, indent=2, sort_keys=True) + '\n'}
        rules = [*R.core_seed_rules(), *pack.seed_rules()]
        ids = [r.id for r in rules]
        if len(set(ids)) != len(ids):
            raise ValueError(f'duplicate seed rule ids: {ids}')
        for rule in rules:
            files[f'rules/{rule.id}.json'] = json.dumps(R.rule_to_dict(rule), ensure_ascii=False, indent=2,
                                                        sort_keys=True) + '\n'
        return files
    raise ValueError(f'unknown W0 kind {kind!r}')


def build_w0(kind: str, pack, destination: Path) -> Path:
    """Write a W0 adapter into an empty/new directory (used to (re)generate adapters/*-w0)."""
    destination = Path(destination)
    if destination.exists() and any(destination.iterdir()):
        raise ValueError(f'W0 destination is not empty: {destination}')
    for relative, text in w0_files(kind, pack).items():
        path = destination / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
    return destination


def tree_files(directory: Path) -> dict[str, str]:
    """{relative path: text} of an adapter directory (no symlinks, no __pycache__)."""
    directory = Path(directory)
    out = {}
    for path in sorted(directory.rglob('*')):
        rel = path.relative_to(directory).as_posix()
        if '__pycache__' in rel.split('/'):
            continue
        if path.is_symlink():
            raise ValueError(f'adapter symlinks are refused: {rel}')
        if path.is_file():
            out[rel] = path.read_text(encoding='utf-8')
    return out


def check_w0(arm: C.ArmSpec, adapters: Path | None = None) -> dict:
    """The committed W0 of an arm equals what the pack generates today (drift check) and fits the budget."""
    directory = w0_dir(arm, adapters)
    if directory is None:
        return {'adapter': None}
    pack = load_pack(arm.pack)
    expected = w0_files(arm.seed, pack)
    actual = tree_files(directory)
    if actual != expected:
        changed = sorted(k for k in set(actual) | set(expected) if actual.get(k) != expected.get(k))
        raise ValueError(f'{directory.name} differs from the generated W0: {changed[:8]}')
    visible = model_visible(directory, arm)
    if not visible['within_budget']:
        raise ValueError(f'{directory.name} model-visible text {visible["total"]} B exceeds {C.TEXT_BUDGET} B')
    return {'adapter': str(directory), 'files': len(actual), 'model_visible': visible}

# ---------------------------------------------------------------------------------------------
# Adapters as served (W0 or a learned revision)
# ---------------------------------------------------------------------------------------------


def adapter_info(directory: Path) -> dict:
    data = json.loads((Path(directory) / 'adapter.json').read_text(encoding='utf-8'))
    if data.get('architecture') not in ('text', 'mechanism') or data.get('contract') != C.CONTRACT_VERSION:
        raise ValueError(f'adapter.json must declare architecture text|mechanism and contract {C.CONTRACT_VERSION}')
    return data


def check_adapter(directory: Path, arm: C.ArmSpec) -> dict:
    """Refuse an adapter that does not belong to this arm (architecture, pack, rule schema, policy ranges,
    note schema, text budget). Used on W0 and on every candidate a learner returns."""
    from harness_core import rules as R
    import notes_render
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError('an adapter is a real directory')
    info = adapter_info(directory)
    if info['architecture'] != architecture(arm):
        raise ValueError(f'adapter architecture {info["architecture"]!r} is not the arm\'s {architecture(arm)!r}')
    if info.get('pack') != arm.pack:
        raise ValueError(f'adapter pack {info.get("pack")!r} is not the arm pack {arm.pack!r}')
    if not (directory / 'SEED.md').is_file():
        raise ValueError('adapter SEED.md missing')
    notes = notes_render.load_notes(directory)
    result = {'architecture': info['architecture'], 'notes': len(notes)}
    if arm.controller:
        pack = load_pack(arm.pack)
        from harness_core import predicates as core_predicates, offers as core_offers
        preds = {*core_predicates.REGISTRY, *pack.predicates()}
        offs = {*core_offers.REGISTRY, *pack.offers()}
        rules = R.load_rules(directory / 'rules')
        for rule in rules:
            errors = R.validate_rule(rule, predicates=preds, offers=offs)
            if errors:
                raise ValueError(f'rule {rule.id}: {errors}')
        R.effective_policy(load_policy(directory))
        result['rules'] = len(rules)
        result['rules_enabled'] = sum(r.enabled for r in rules)
    elif (directory / 'rules').exists() or (directory / 'policy.json').exists():
        raise ValueError('a text adapter carries no rules/ or policy.json')
    visible = model_visible(directory, arm)
    if not visible['within_budget']:
        raise ValueError(f'adapter model-visible text {visible["total"]} B exceeds {C.TEXT_BUDGET} B')
    result['model_visible'] = visible
    return result


def load_policy(directory: Path) -> dict:
    path = Path(directory) / 'policy.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}


def load_adapter_rules(directory: Path) -> list[C.Rule]:
    from harness_core import rules as R
    return R.load_rules(Path(directory) / 'rules')


def harness_fixed_text() -> dict[str, int]:
    """Bytes of the fixed model-visible wording the controller arms add: the controller's loop and
    enforcement templates (counted in the budget, as learner_common.controller_template_bytes does) and the
    gate's message frame and usage line (recorded, not counted: they carry no knowledge). Computed from the
    modules that print them, so the numbers follow the code."""
    import controller_runtime as CR
    from harness_core import gate as G
    blank = C.Finding(rule='x', text='', offer=C.OfferResult(answer='', derivation=''))
    parts = {'loop_template': CR.LOOP_TEMPLATE, 'loop_options': CR.LOOP_OPTIONS,
             'loop_escalate': CR.LOOP_ESCALATE, 'enforce_template': CR.ENFORCE_TEMPLATE,
             'gate_finding_frame': G.finding_message(blank, ''), 'gate_accepted': G.accepted_message('', False),
             'gate_usage': G.USAGE}
    return {k: len(v.encode('utf-8')) for k, v in parts.items()}


CONTROLLER_COUNTED = ('loop_template', 'loop_options', 'loop_escalate', 'enforce_template')


def model_visible(directory: Path | None, arm: C.ArmSpec) -> dict:
    """Model-visible adapter text in bytes (DESIGN §1: 16 KB per arm counting notes and rule message
    templates), with the same formula as the learners' admission (learner_common.text_budget; a test pins
    the equality): SEED.md + every note as rendered ('- ' + text + newline; all notes, not only the ≤ 3 an
    episode shows) + every enabled rule's finding template + (controller arms) the controller's loop and
    enforcement templates. The gate frame is recorded separately (`gate_frame`), not counted."""
    import notes_render
    empty = {'seed_md': 0, 'notes': 0, 'notes_count': 0, 'rule_templates': 0, 'controller': 0, 'gate_frame': 0,
             'total': 0, 'budget': C.TEXT_BUDGET, 'within_budget': True}
    if directory is None:
        return empty
    directory = Path(directory)
    seed = len((directory / 'SEED.md').read_bytes()) if (directory / 'SEED.md').is_file() else 0
    notes = notes_render.load_notes(directory)
    note_bytes = sum(len(notes_render.render_note(n).encode('utf-8')) for n in notes)
    templates = controller = gate_frame = 0
    if arm.controller:
        from harness_core import rules as R
        templates = R.rendered_text_bytes(load_adapter_rules(directory))
        fixed = harness_fixed_text()
        controller = sum(fixed[k] for k in CONTROLLER_COUNTED)
        gate_frame = sum(v for k, v in fixed.items() if k not in CONTROLLER_COUNTED)
    total = seed + note_bytes + templates + controller
    return {'seed_md': seed, 'notes': note_bytes, 'notes_count': len(notes), 'rule_templates': templates,
            'controller': controller, 'gate_frame': gate_frame, 'total': total, 'budget': C.TEXT_BUDGET,
            'within_budget': total <= C.TEXT_BUDGET}


def copy_adapter(source: Path, destination: Path) -> Path:
    """Copy an adapter tree (regular files only) into a new directory."""
    source, destination = Path(source), Path(destination)
    if destination.exists():
        raise ValueError(f'adapter copy destination exists: {destination}')
    for rel, text in tree_files(source).items():
        path = destination / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
    return destination


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Generate or check the W0 adapters under adapters/.')
    parser.add_argument('command', choices=('build', 'check'))
    parser.add_argument('--pack', default='officeqa')
    args = parser.parse_args()
    if args.command == 'build':
        for seed, name in W0_NAMES.items():
            target = ADAPTERS / name
            if target.exists():
                shutil.rmtree(target)
            build_w0(seed, load_pack(args.pack), target)
            print(json.dumps({'built': str(target)}))
    else:
        for name in ('text-static', 'mech-static'):
            print(json.dumps(check_w0(parse_arm(name, pack=args.pack)), indent=1))
