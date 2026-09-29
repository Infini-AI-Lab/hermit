"""v10 arms (DESIGN §2-§4): the one place where `.task-paths.json` becomes an experimental condition.

A v10 root runs exactly one arm for one repeat. Its `.task-paths.json` names it:

    {"profile": "v10",
     "arm": "harness-evolve" | {"name": ..., "pack": ..., "l1_tools": ..., "controller": ..., "seed": ...,
                                "learner": ..., "notes": ..., "gate": ...},
     "repeat": 1, "pack": "officeqa", "order_seed": "v10-officeqa-pro133-order",
     "barrier": {"dir": "/abs/v10-r1-barrier", "roots": ["v10-r1-a", "v10-r1-b", ...], "timeout_s": 43200},
     # optional:
     "subset": {"name": "pilot-first50", "task_ids": [...], "sha256": subset_sha256(task_ids)},
     "temperature": 0,
     "top_p": 0.95}            # optional; absent = no top_p is sent

`parse_config` turns that into a `CampaignSpec` (contracts.ArmSpec + repeat + order seed + barrier
[+ subset + temperature + top_p]) and validates it once. Profile 'v9' is refused with a message naming the
v9 tree, and the v9 arm names are refused by `parse_arm` with a message naming the v10 arms, so v10 code
pointed at a v9 root fails early. Nothing anywhere infers an arm from a substring of a profile or
directory name.

This module also owns the arm-derived pieces every other module needs:
- `question_order`: the seeded permutation of the task ids for one repeat (identical for every arm of
  that repeat; different between repeats);
- the W0 adapters (DESIGN §4): `adapters/prompt-w0` (SEED.md = pack.seed_text_prompt(), notes/) and
  `adapters/harness-w0` (SEED.md = pack.seed_text_harness(), notes/, tools/ = the pack's W0 tools + calc.py
  + manifest.json + USAGE.md, controls.json = CONTROLS_DEFAULTS); `w0_files` generates them from the pack,
  `build_w0` writes them, `check_w0` pins the committed copies (drift check), asserts the seed-parity
  checklist (pack.SEED_KNOWLEDGE) and, for the prompt W0, that no harness tool is named;
- `revision(tree)`: the identity of an adapter, sha12 of `snapshot(tree minus adapter.json)`, so writing
  the hash into adapter.json does not change it (W0 keeps the literal 'w0');
- `check_adapter`: what every served adapter (W0 or a learner's candidate) must satisfy: architecture,
  pack, revision, manifest (names, scripts, calc pinned to W0), tool caps, USAGE line rule, controls
  (contracts.checked_controls; rules/policy only behind a gate), import closure per script, a dry-run kit;
- `model_visible`: the byte count of adapter text the model can see (SEED.md + notes + tools/USAGE.md +
  rule templates (gate) + the controller's fixed loop templates), checked against contracts.TEXT_BUDGET;
- `revealing_fragments`: arm-identifying fragments in any model-visible name (root, code-root path, tool
  file name, manifest name), refused wherever such a name is created.
This module never imports or executes adapter python: tool files are read as text and parsed with `ast`
(kit._import_closure) only.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields, replace
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from typing import Any, Mapping, Sequence

from harness_core import contracts as C
import kit
from paths import HARNESS_CORE

HERE = Path(__file__).resolve().parent
ADAPTERS = HERE / 'adapters'
PROFILE = 'v10'
V9_TREE = 'officeqa-pro133-v9-plumbing'
W0_NAMES = {'text': 'prompt-w0', 'harness': 'harness-w0'}   # ArmSpec.seed -> adapters/<name>
ARCHITECTURES = C.ARCHITECTURES                              # ArmSpec.seed -> adapter.json architecture
PACKS = ('officeqa', 'toy', 'spreadsheet')                   # known pack names (packs/<name>/)
DEFAULT_PACK = 'officeqa'                                    # the pack whose W0 this tree's adapters/ holds
# packs whose W0 adapters can be built here (`arms.py build --pack <name>` writes adapters/*-w0 of a root's
# code copy for that pack; check_adapter refuses a W0 built for another pack)
W0_PACKS = ('officeqa', 'toy')
CUSTOM_NAME = re.compile(r'^[a-z][a-z0-9-]{0,31}$')
ROOT_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
BARRIER_TIMEOUT_MAX = 7 * 24 * 3600
# `.task-paths.json` keys a v10 root may carry: the campaign spec (this module) and the path overrides
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
# Model-visible names: Codex sends the workspace path (<root>/runs/episodes/NNN/workspace) in every request's
# environment context, README.md prints the corpus path under the root, the kit wrappers embed the code-root
# path of every tool script, and the tool listing shows every tools/ file name. A name that carries the
# arm (v8: officeqa-pro133-cards-v8-work) tells the actor which condition it is in, so root names, the
# code-root path, tool file names and manifest names must not contain these fragments (DESIGN §3, §6-4).
ARM_REVEALING = ('plain', 'text', 'mech', 'static', 'evol', 'rule', 'control', 'prompt', 'card', 'learn',
                 'seed', 'note', 'adapt', 'mwm', 'baseline', 'harness', 'revision')
# The adapter's tools/ directory (DESIGN §3, §5 screen 1): python modules, the manifest and the usage text.
TOOL_FILE = re.compile(r'^[a-z][a-z0-9_]{0,31}\.py$')
TOOLS_DIR = 'tools'
CONTROLS_FILE = 'controls.json'
CALC_SCRIPT = HARNESS_CORE / 'tools/calc.py'         # the W0 copy of tools/calc.py; every revision is pinned to it
ADAPTER_KEYS = {'architecture', 'contract', 'pack', 'revision'}
W0_REVISION = 'w0'
_OPTION = re.compile(r'--[a-z][a-z0-9-]*')
# The calc lines of tools/USAGE.md (every line names `calc`, the USAGE line rule of DESIGN scope decision 6).
CALC_USAGE = '''  calc -c '<python>'  |  calc file.py  |  calc <<'PY' ... PY
      calc runs Python 3.12 (numpy, pandas) and prints your code together with its output; the value of a
      final expression is printed by calc, as in an interactive session
'''


def revealing_fragments(name: str | os.PathLike, arm: 'C.ArmSpec | None' = None) -> list[str]:
    """Arm-identifying fragments in a model-visible name or path (root directory, code-root path, tool
    file name, manifest tool name)."""
    lowered = str(name).lower()
    found = [w for w in ARM_REVEALING if w in lowered]
    if 'none' in re.split(r'[^a-z0-9]+', lowered):
        found.append('none')
    if arm is not None and arm.name.lower() in lowered and arm.name.lower() not in found:
        found.append(arm.name.lower())
    return found


def check_neutral(name: str | os.PathLike, what: str, arm: 'C.ArmSpec | None' = None) -> str:
    """Refuse a model-visible name that reveals the arm; return it as a string."""
    found = revealing_fragments(name, arm)
    if found:
        raise ValueError(f'{what} {str(name)!r} reveals the arm to the actor: contains {found}')
    return str(name)

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
    """A declared question subset: the ids and the sha256 of exactly that list."""
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
        are left out, so a full run at temperature 0 without top_p records exactly the base spec."""
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
    """contracts.ArmSpec.validate plus the orchestration invariants every v10 arm must satisfy."""
    if not isinstance(arm, C.ArmSpec):
        raise TypeError('an arm is a harness_core.contracts.ArmSpec')
    for name in ('l1_tools', 'controller', 'notes', 'gate'):
        _bool(getattr(arm, name), f'arm.{name}')
    if arm.seed not in ('none', 'text', 'harness'):
        raise ValueError('arm.seed must be none, text or harness')
    if arm.learner not in (None, 'text', 'harness'):
        raise ValueError('arm.learner must be null, text or harness')
    if arm.pack not in PACKS:
        raise ValueError(f'unknown pack {arm.pack!r}')
    if not isinstance(arm.name, str) or not CUSTOM_NAME.match(arm.name):
        raise ValueError('arm.name must match ^[a-z][a-z0-9-]{0,31}$')
    arm.validate()
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
        if value in C.RETIRED_ARMS:
            raise ValueError(f'arm {value!r} is a v9 arm name (retired); the v10 arms are '
                             f'{list(C.STANDARD_ARMS)} (a custom arm is given as a full spec object)')
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
    """The campaign spec of one v10 root. Refuses unknown keys, a non-v10 profile and every inconsistency."""
    if not isinstance(config, Mapping):
        raise ValueError('.task-paths.json must be a JSON object')
    unknown = set(config) - CAMPAIGN_KEYS - PATH_KEYS
    if unknown:
        raise ValueError(f'unknown .task-paths.json keys: {sorted(unknown)}')
    profile = config.get('profile', PROFILE)
    if profile == 'v9':
        raise ValueError(f"profile 'v9' is a v9 root: run it with the v9 tree ({V9_TREE}), not this v10 code")
    if profile != PROFILE:
        raise ValueError(f"profile must be {PROFILE!r} (got {profile!r}); other profiles are not v10 arms")
    if 'arm' not in config:
        raise ValueError('a v10 root names its arm (.task-paths.json "arm")')
    if not isinstance(root_name, str) or not ROOT_NAME.match(root_name):
        raise ValueError(f'root name {root_name!r} is not a plain directory name')
    pack = config.get('pack', 'officeqa')
    if pack not in PACKS:
        raise ValueError(f'unknown pack {pack!r}')
    arm = parse_arm(config['arm'], pack=pack)
    revealing = revealing_fragments(root_name, arm)
    if revealing:
        raise ValueError(f'root name {root_name!r} reveals the arm to the actor (the workspace and corpus paths '
                         f'are model-visible): contains {revealing}; use a neutral name such as "v10-r1-a"')
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
    """'none' (no adapter), 'prompt' or 'harness': an explicit function of arm.seed, never a substring."""
    return ARCHITECTURES.get(arm.seed, 'none')


def learning_type(arm: C.ArmSpec) -> str:
    return {None: 'static: no release to a learner, no learning',
            'text': 'notes learner: scoped notes admitted after paired actor re-runs (support x2 + controls x2)',
            'harness': 'harness learner: scoped notes and tool revisions (patches to tools/*.py, USAGE.md, an '
                       'optional bundled note), screened and replayed, admitted by paired actor re-runs'}[arm.learner]

# ---------------------------------------------------------------------------------------------
# Question order: randomised per repeat, identical across arms of a repeat
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
# Adapter trees: files, snapshot, revision (DESIGN §1)
# ---------------------------------------------------------------------------------------------


def load_pack(name: str, corpus=None, index=None):
    """The contracts.Pack object of a pack name (explicit table, no import by string)."""
    if name == 'officeqa':
        from packs.officeqa.pack import OfficeQAPack
        return OfficeQAPack(corpus, index)
    if name == 'toy':
        from packs.toy.pack import ToyPack
        return ToyPack()
    raise ValueError(f'pack {name!r} has no Pack implementation in this plumbing')


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


def snapshot(tree: Path | Mapping[str, str]) -> dict[str, str]:
    """{relative path: sha256 of the bytes} of an adapter tree minus adapter.json (a directory, or the
    {path: text} mapping w0_files returns)."""
    files = tree_files(tree) if isinstance(tree, (str, os.PathLike)) else dict(tree)
    return {rel: C.sha256_text(text) for rel, text in sorted(files.items()) if rel != 'adapter.json'}


def revision(tree: Path | Mapping[str, str]) -> str:
    """The identity of an adapter: sha12 of its snapshot (tree minus adapter.json), the one definition
    freeze.json, episode markers and mechanical-admission.json record. W0 trees carry the literal 'w0' in
    adapter.json instead; check_adapter accepts it only for a tree equal to the generated W0."""
    canonical = json.dumps(snapshot(tree), sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:12]


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

# ---------------------------------------------------------------------------------------------
# W0 adapters (DESIGN §4)
# ---------------------------------------------------------------------------------------------


def w0_dir(arm: C.ArmSpec, adapters: Path | None = None) -> Path | None:
    """The arm's W0 adapter directory (None for an arm without an adapter)."""
    if arm.seed == 'none':
        return None
    if arm.pack not in W0_PACKS:
        raise ValueError(f'no W0 adapter is built for pack {arm.pack!r}')
    return Path(adapters or ADAPTERS) / W0_NAMES[arm.seed]


def calc_sha256() -> str:
    return kit._sha_file(CALC_SCRIPT)


def w0_manifest(pack) -> dict[str, dict[str, Any]]:
    """The harness W0 manifest tools: the pack's entries + calc (script calc.py, no fixed args)."""
    manifest = {name: {'script': entry['script'], 'args': list(entry['args'])}
                for name, entry in pack.w0_tools().manifest.items()}
    if C.CALC_TOOL in manifest:
        raise ValueError(f'the pack must not define {C.CALC_TOOL!r}; arms adds it')
    manifest[C.CALC_TOOL] = {'script': 'calc.py', 'args': []}
    return manifest


def harness_tool_names(pack) -> list[str]:
    """The manifest tool names of the pack's harness W0: what a prompt SEED or note must not name."""
    return sorted(w0_manifest(pack))


def w0_usage(pack) -> str:
    """tools/USAGE.md of the harness W0: the pack's usage text + the calc lines."""
    return pack.usage().rstrip() + '\n' + CALC_USAGE


def w0_files(kind: str, pack) -> dict[str, str]:
    """The files of a W0 adapter as {relative path: text}. kind: 'text' (prompt W0) | 'harness'."""
    header = {'contract': C.CONTRACT_VERSION, 'pack': pack.name, 'revision': W0_REVISION}
    if kind == 'text':
        return {'adapter.json': json.dumps({'architecture': 'prompt', **header}, indent=2, sort_keys=True) + '\n',
                'SEED.md': pack.seed_text_prompt(), 'notes/.keep': ''}
    if kind == 'harness':
        tools = pack.w0_tools()
        if 'calc.py' in tools.files or C.MANIFEST_FILE in tools.files or C.USAGE_FILE in tools.files:
            raise ValueError('the pack W0 tools must not include calc.py, manifest.json or USAGE.md (arms adds them)')
        files = {'adapter.json': json.dumps({'architecture': 'harness', **header}, indent=2, sort_keys=True) + '\n',
                 'SEED.md': pack.seed_text_harness(), 'notes/.keep': '',
                 CONTROLS_FILE: json.dumps(C.CONTROLS_DEFAULTS, indent=2, sort_keys=True) + '\n'}
        for name, text in tools.files.items():
            files[f'{TOOLS_DIR}/{name}'] = text
        files[f'{TOOLS_DIR}/calc.py'] = CALC_SCRIPT.read_text(encoding='utf-8')
        files[f'{TOOLS_DIR}/{C.MANIFEST_FILE}'] = json.dumps({'tools': w0_manifest(pack)}, indent=2, sort_keys=True) + '\n'
        files[f'{TOOLS_DIR}/{C.USAGE_FILE}'] = w0_usage(pack)
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


def _normalised(text: str) -> str:
    return ' '.join(str(text).split())


def knowledge_missing(seed: str, pack, side: str) -> list[str]:
    """Ids of pack.SEED_KNOWLEDGE items whose `side` ('harness' | 'prompt') sentence is absent from `seed`
    (whitespace-normalised substring)."""
    haystack = _normalised(seed)
    missing = []
    for item in pack.SEED_KNOWLEDGE:
        if not {'id', 'harness', 'prompt'} <= set(item):
            raise ValueError('a SEED_KNOWLEDGE item has id, harness and prompt')
        if _normalised(item[side]) not in haystack:
            missing.append(item['id'])
    return missing


def names_tools(text: str, names: Sequence[str]) -> list[str]:
    """The tool names (whole words) that occur in `text`."""
    return [n for n in names if re.search(r'(?<![\w-])' + re.escape(n) + r'(?![\w-])', text)]


def seed_parity(pack) -> dict:
    """DESIGN §4 parity checklist over the pack's two W0 seed texts: every knowledge item present on each
    side, both ≤ SEED_MAX, within ±SEED_PARITY_TOLERANCE of each other in bytes, the prompt seed naming no
    harness tool. Returns the byte counts and hashes PREREG records; raises on any violation."""
    harness, prompt = pack.seed_text_harness(), pack.seed_text_prompt()
    sizes = {'harness': len(harness.encode('utf-8')), 'prompt': len(prompt.encode('utf-8'))}
    for side, size in sizes.items():
        if size > C.SEED_MAX:
            raise ValueError(f'{side} SEED is {size} B, over {C.SEED_MAX} B')
    if abs(sizes['harness'] - sizes['prompt']) > C.SEED_PARITY_TOLERANCE * max(sizes.values()):
        raise ValueError(f'the W0 SEEDs differ by more than {C.SEED_PARITY_TOLERANCE:.0%} in bytes: {sizes}')
    for side, seed in (('harness', harness), ('prompt', prompt)):
        missing = knowledge_missing(seed, pack, side)
        if missing:
            raise ValueError(f'{side} SEED lacks the knowledge items {missing}')
    named = names_tools(prompt, harness_tool_names(pack))
    if named:
        raise ValueError(f'the prompt SEED names harness tools {named} (they do not exist in that workspace)')
    return {'items': [item['id'] for item in pack.SEED_KNOWLEDGE], 'bytes': sizes,
            'sha256': {'harness': C.sha256_text(harness), 'prompt': C.sha256_text(prompt)}}


def check_w0(arm: C.ArmSpec, adapters: Path | None = None) -> dict:
    """The committed W0 of an arm equals what the pack generates today (drift check), satisfies the seed
    parity checklist, names no harness tool when it is the prompt W0, and passes check_adapter."""
    directory = w0_dir(arm, adapters)
    if directory is None:
        return {'adapter': None}
    pack = load_pack(arm.pack)
    expected = w0_files(arm.seed, pack)
    actual = tree_files(directory)
    if actual != expected:
        changed = sorted(k for k in set(actual) | set(expected) if actual.get(k) != expected.get(k))
        raise ValueError(f'{directory.name} differs from the generated W0: {changed[:8]}')
    parity = seed_parity(pack)
    checked = check_adapter(directory, arm)
    return {'adapter': str(directory), 'files': len(actual), 'revision': W0_REVISION, 'parity': parity,
            'model_visible': checked['model_visible'], 'checked': checked}

# ---------------------------------------------------------------------------------------------
# Adapters as served (W0 or a learned revision): tools, controls, validation (DESIGN §3)
# ---------------------------------------------------------------------------------------------


def adapter_info(directory: Path) -> dict:
    data = json.loads((Path(directory) / 'adapter.json').read_text(encoding='utf-8'))
    if not isinstance(data, dict) or set(data) != ADAPTER_KEYS:
        raise ValueError(f'adapter.json has exactly the keys {sorted(ADAPTER_KEYS)}')
    if data['architecture'] not in ARCHITECTURES.values() or data['contract'] != C.CONTRACT_VERSION:
        raise ValueError(f'adapter.json must declare architecture prompt|harness and contract {C.CONTRACT_VERSION}')
    if not isinstance(data['revision'], str) or not (data['revision'] == W0_REVISION
                                                      or re.fullmatch(r'[0-9a-f]{12}', data['revision'])):
        raise ValueError("adapter.json revision is 'w0' or a 12-hex sha (arms.revision)")
    return data


def load_manifest(tools_dir: Path) -> dict[str, dict[str, Any]]:
    """tools/manifest.json -> {name: {'script': str, 'args': [str]}}, refused (ValueError) when malformed:
    names per kit.check_tool_name and with no revealing fragment, scripts regular python files directly in
    tools/, args plain tokens, calc present and sha-pinned to the W0 copy (harness_core/tools/calc.py)."""
    tools_dir = Path(tools_dir)
    path = tools_dir / C.MANIFEST_FILE
    if path.is_symlink() or not path.is_file():
        raise ValueError(f'{TOOLS_DIR}/{C.MANIFEST_FILE} missing')
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or set(data) != {'tools'} or not isinstance(data['tools'], dict) or not data['tools']:
        raise ValueError('manifest.json is {"tools": {<name>: {"script": ..., "args": [...]}, ...}}')
    out = {}
    for name, entry in data['tools'].items():
        kit.check_tool_name(name)
        check_neutral(name, 'manifest tool name')
        if not isinstance(entry, dict) or set(entry) != {'script', 'args'}:
            raise ValueError(f'manifest tool {name!r} has exactly the keys script and args')
        script, args = entry['script'], entry['args']
        if not isinstance(script, str) or not TOOL_FILE.match(script):
            raise ValueError(f'manifest tool {name!r}: script must be a python file name in {TOOLS_DIR}/ (got {script!r})')
        target = tools_dir / script
        if target.is_symlink() or not target.is_file():
            raise ValueError(f'manifest tool {name!r}: script {script} is not a regular file in {TOOLS_DIR}/')
        if not isinstance(args, list) or not all(isinstance(a, str) and kit.FIXED_ARG.match(a) for a in args):
            raise ValueError(f'manifest tool {name!r}: args must be plain tokens')
        out[name] = {'script': script, 'args': list(args)}
    calc = out.get(C.CALC_TOOL)
    if calc is None:
        raise ValueError(f'manifest must define {C.CALC_TOOL!r}')
    if calc != {'script': 'calc.py', 'args': []}:
        raise ValueError(f'{C.CALC_TOOL!r} is {{"script": "calc.py", "args": []}}')
    if kit._sha_file(tools_dir / 'calc.py') != calc_sha256():
        raise ValueError('tools/calc.py differs from the W0 copy (calc is sha-pinned this round)')
    return out


def manifest_wrappers(tools_dir: Path, manifest: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, list[str]]:
    """kit.build_kit `tool_wrappers` of a tools/ directory: {name: [<tools_dir>/<script>, *args]}. The caller
    passes the neutral code root it copied tools/ to (campaign.adapter_tool_wrappers), never adapters/…"""
    tools_dir = Path(tools_dir).resolve()
    manifest = load_manifest(tools_dir) if manifest is None else manifest
    return {name: [str(tools_dir / entry['script']), *entry['args']] for name, entry in sorted(manifest.items())}


def option_strings(sources: Mapping[str, str]) -> set[str]:
    """Every `--option` token in the tool sources (argparse option strings and any other mention)."""
    return {opt for text in sources.values() for opt in _OPTION.findall(text)}


def usage_violations(text: str, names: Sequence[str], options: set[str]) -> list[str]:
    """USAGE line rule (DESIGN scope decision 6): the non-empty, non-heading lines that name no manifest tool
    and no `--option` occurring in the tool sources."""
    bad = []
    for line in text.split('\n'):
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        if names_tools(line, names) or any(opt in options for opt in _OPTION.findall(line)):
            continue
        bad.append(line)
    return bad


def check_tools(tools_dir: Path) -> dict:
    """tools/ of a harness adapter: only regular files (python modules, manifest.json, USAGE.md), neutral
    names, no stdlib shadowing, the caps of DESIGN §3, the manifest (load_manifest), the USAGE line rule and
    the sibling-import closure of every script (kit._import_closure: parsed with ast, never imported)."""
    tools_dir = Path(tools_dir)
    if tools_dir.is_symlink() or not tools_dir.is_dir():
        raise ValueError(f'{TOOLS_DIR}/ must be a real directory')
    sources, files = {}, {}
    for path in sorted(tools_dir.iterdir()):
        name = path.name
        if name == '__pycache__':
            continue
        if path.is_symlink() or not path.is_file():
            raise ValueError(f'{TOOLS_DIR}/{name}: only regular files are allowed (no subdirectories, no symlinks)')
        if name not in (C.MANIFEST_FILE, C.USAGE_FILE) and not TOOL_FILE.match(name):
            raise ValueError(f'{TOOLS_DIR}/{name}: allowed files are <module>.py, {C.MANIFEST_FILE} and {C.USAGE_FILE}')
        check_neutral(name, 'tool file name')
        files[name] = kit._sha_file(path)
        if name.endswith('.py'):
            if name[:-3] in sys.stdlib_module_names:
                raise ValueError(f'{TOOLS_DIR}/{name} shadows the stdlib module {name[:-3]!r} (tools/ is first on sys.path)')
            size = path.stat().st_size
            if size > C.TOOL_FILE_MAX:
                raise ValueError(f'{TOOLS_DIR}/{name} is {size} B, over {C.TOOL_FILE_MAX} B')
            sources[name] = path.read_text(encoding='utf-8')
    if len(sources) > C.TOOL_FILES_MAX:
        raise ValueError(f'{len(sources)} python files in {TOOLS_DIR}/, over {C.TOOL_FILES_MAX}')
    total = sum(len(text.encode('utf-8')) for text in sources.values())
    if total > C.TOOL_TOTAL_MAX:
        raise ValueError(f'{TOOLS_DIR}/ python is {total} B, over {C.TOOL_TOTAL_MAX} B')
    manifest = load_manifest(tools_dir)
    usage_path = tools_dir / C.USAGE_FILE
    if usage_path.is_symlink() or not usage_path.is_file():
        raise ValueError(f'{TOOLS_DIR}/{C.USAGE_FILE} missing')
    usage = usage_path.read_text(encoding='utf-8')
    if len(usage.encode('utf-8')) > C.USAGE_MAX:
        raise ValueError(f'{TOOLS_DIR}/{C.USAGE_FILE} is {len(usage.encode("utf-8"))} B, over {C.USAGE_MAX} B')
    bad = usage_violations(usage, list(manifest), option_strings(sources))
    if bad:
        raise ValueError(f'{C.USAGE_FILE} lines naming no manifest tool and no tool option: {bad[:3]}')
    closure = {}
    for name, entry in manifest.items():
        try:
            granted = kit._import_closure(tools_dir / entry['script'])
        except SyntaxError as exc:
            raise ValueError(f'tool {name!r}: {exc.filename and Path(exc.filename).name} does not parse: {exc.msg} (line {exc.lineno})') from exc
        closure[name] = [p.name for p in granted]
        outside = [p for p in granted if p.parent != tools_dir]
        if outside:
            raise ValueError(f'tool {name!r} imports outside {TOOLS_DIR}/: {outside}')
    return {'files': files, 'python_files': len(sources), 'python_bytes': total, 'usage_bytes': len(usage.encode('utf-8')),
            'tools': manifest, 'closure': closure}


def load_controls(directory: Path) -> dict[str, Any]:
    """controls.json of a harness adapter, validated (contracts.checked_controls)."""
    path = Path(directory) / CONTROLS_FILE
    if path.is_symlink() or not path.is_file():
        raise ValueError(f'{CONTROLS_FILE} missing (a harness adapter declares its controls)')
    return C.checked_controls(json.loads(path.read_text(encoding='utf-8')))


def load_policy(directory: Path) -> dict:
    path = Path(directory) / 'policy.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}


def load_adapter_rules(directory: Path) -> list[C.Rule]:
    from harness_core import rules as R
    return R.load_rules(Path(directory) / 'rules')


def dry_run_kit(directory: Path, arm: C.ArmSpec) -> dict:
    """kit.build_kit into a scratch workspace with the adapter's own tools/ as code root, so a broken
    revision fails at staging and never aborts a scored episode. Returns the kit's code grants."""
    tools_dir = (Path(directory) / TOOLS_DIR).resolve()
    with tempfile.TemporaryDirectory(prefix='kit-dry-run-') as scratch:
        workspace = Path(scratch).resolve() / 'ws'
        workspace.mkdir()
        meta = kit.build_kit(workspace, arm, manifest_wrappers(tools_dir), None, submit=arm.gate, code_roots=[tools_dir])
    return meta['code']


def check_adapter(directory: Path, arm: C.ArmSpec) -> dict:
    """Refuse an adapter that does not belong to this arm or is not servable: architecture, pack, contract,
    revision (adapter.json equals arms.revision(tree), or 'w0' for the generated W0 tree), SEED.md, note
    schema, prompt adapter without tools/controls (and naming no harness tool), harness adapter with a valid
    tools/ (check_tools), controls (checked_controls, gate equal to the arm's), rules/policy only behind a
    gate, a dry-run kit, and the model-visible text budget. Used on W0 and on every candidate a learner
    returns, before any actor run."""
    import notes_render
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError('an adapter is a real directory')
    info = adapter_info(directory)
    if info['architecture'] != architecture(arm):
        raise ValueError(f'adapter architecture {info["architecture"]!r} is not the arm\'s {architecture(arm)!r}')
    if info['pack'] != arm.pack:
        raise ValueError(f'adapter pack {info["pack"]!r} is not the arm pack {arm.pack!r}')
    if not (directory / 'SEED.md').is_file() or (directory / 'SEED.md').is_symlink():
        raise ValueError('adapter SEED.md missing')
    pack = load_pack(arm.pack)
    computed = revision(directory)
    if info['revision'] == W0_REVISION:
        if snapshot(directory) != snapshot(w0_files(arm.seed, pack)):
            raise ValueError("adapter.json revision 'w0' on a tree that is not the generated W0")
    elif info['revision'] != computed:
        raise ValueError(f'adapter.json revision {info["revision"]} is not the tree revision {computed} (arms.revision)')
    notes = notes_render.load_notes(directory)
    result = {'architecture': info['architecture'], 'revision': info['revision'], 'notes': len(notes)}
    if arm.seed == 'text':
        for name in (TOOLS_DIR, CONTROLS_FILE, 'rules', 'policy.json'):
            if (directory / name).exists():
                raise ValueError(f'a prompt adapter carries no {name}')
        forbidden = harness_tool_names(pack)
        named = names_tools((directory / 'SEED.md').read_text(encoding='utf-8'), forbidden)
        if named:
            raise ValueError(f'a prompt adapter must not name the harness tools (SEED.md names {named})')
        for note in notes:
            named = names_tools(note.text, forbidden)
            if named:
                raise ValueError(f'a prompt adapter must not name the harness tools (note {note.id} names {named})')
    else:
        result['tools'] = check_tools(directory / TOOLS_DIR)
        controls = load_controls(directory)
        if controls['gate'] != arm.gate:
            raise ValueError(f'controls.json gate={controls["gate"]} but the arm {arm.name!r} has gate={arm.gate}')
        result['controls'] = controls
        if arm.gate:
            from harness_core import rules as R
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
            raise ValueError('rules/ and policy.json are refused when the gate is off (controls.json gate=false)')
        result['code'] = dry_run_kit(directory, arm)
    visible = model_visible(directory, arm)
    if not visible['within_budget']:
        raise ValueError(f'adapter model-visible text {visible["total"]} B exceeds {C.TEXT_BUDGET} B')
    result['model_visible'] = visible
    return result


def harness_fixed_text(gate: bool = False) -> dict[str, int]:
    """Bytes of the fixed model-visible wording the controller arms add: the controller's loop (and, gate
    on, enforcement; gate off, hard block) templates, `controller_runtime.fixed_templates(gate)` (counted in
    the budget, as learner_common.controller_template_bytes must: a test pins the equality), and, recorded but
    not counted (RECORDED_ONLY), the gate's message frame and usage line (they carry no knowledge) and, gate
    off, the cap of the hard block's echo (`hard_block_echo_cap` = controller_runtime.HARD_BLOCK_ECHO_BYTES:
    the echo repeats the model's own tool output, already in its context). Computed from the modules that
    print them, so the numbers follow the code."""
    import controller_runtime as CR
    from harness_core import gate as G
    blank = C.Finding(rule='x', text='', offer=C.OfferResult(answer='', derivation=''))
    parts = {**CR.fixed_templates(gate),
             'gate_finding_frame': G.finding_message(blank, ''), 'gate_accepted': G.accepted_message('', False),
             'gate_usage': G.USAGE}
    out = {k: len(v.encode('utf-8')) for k, v in parts.items()}
    if not gate:
        out['hard_block_echo_cap'] = CR.HARD_BLOCK_ECHO_BYTES
    return out


GATE_FRAME = ('gate_finding_frame', 'gate_accepted', 'gate_usage')      # recorded, never counted
RECORDED_ONLY = GATE_FRAME + ('hard_block_echo_cap',)                   # every recorded-not-counted key
# The counted keys of harness_fixed_text for the standard (gate-off) arms; gate on adds enforce_template.
CONTROLLER_COUNTED = ('loop_template', 'loop_options', 'loop_escalate', 'hard_block')


def model_visible(directory: Path | None, arm: C.ArmSpec) -> dict:
    """Model-visible adapter text in bytes (DESIGN §3: 16 KB per arm), with the same formula as the
    learners' admission (learner_common.text_budget; a test pins the equality): SEED.md + every note as
    rendered ('- ' + text + newline; all notes, not only the ≤ 3 an episode shows) + tools/USAGE.md (harness)
    + every enabled rule's finding template (gate) + (controller arms) the controller's loop and enforcement
    templates. The gate frame is recorded separately (`gate_frame`), not counted."""
    import notes_render
    empty = {'seed_md': 0, 'notes': 0, 'notes_count': 0, 'usage': 0, 'rule_templates': 0, 'controller': 0,
             'gate_frame': 0, 'total': 0, 'budget': C.TEXT_BUDGET, 'within_budget': True}
    if directory is None:
        return empty
    directory = Path(directory)
    seed = len((directory / 'SEED.md').read_bytes()) if (directory / 'SEED.md').is_file() else 0
    notes = notes_render.load_notes(directory)
    note_bytes = sum(len(notes_render.render_note(n).encode('utf-8')) for n in notes)
    usage_path = directory / TOOLS_DIR / C.USAGE_FILE
    usage = len(usage_path.read_bytes()) if arm.l1_tools and usage_path.is_file() else 0
    templates = controller = gate_frame = 0
    if arm.controller:
        fixed = harness_fixed_text(arm.gate)
        controller = sum(v for k, v in fixed.items() if k not in RECORDED_ONLY)
    if arm.gate:
        from harness_core import rules as R
        templates = R.rendered_text_bytes(load_adapter_rules(directory))
        gate_frame = sum(fixed[k] for k in GATE_FRAME)
    total = seed + note_bytes + usage + templates + controller
    return {'seed_md': seed, 'notes': note_bytes, 'notes_count': len(notes), 'usage': usage,
            'rule_templates': templates, 'controller': controller, 'gate_frame': gate_frame, 'total': total,
            'budget': C.TEXT_BUDGET, 'within_budget': total <= C.TEXT_BUDGET}


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Generate or check the W0 adapters under adapters/.')
    parser.add_argument('command', choices=('build', 'check'))
    parser.add_argument('--pack', default=DEFAULT_PACK, choices=PACKS)
    parser.add_argument('--adapters', default=None, help='adapters directory (default: this tree\'s adapters/)')
    args = parser.parse_args()
    adapters_dir = Path(args.adapters).resolve() if args.adapters else ADAPTERS
    if args.pack != DEFAULT_PACK and adapters_dir == ADAPTERS:
        # W0_NAMES are pack-independent: building another pack here would overwrite the tree's officeqa W0
        parser.error(f'pack {args.pack!r}: pass --adapters DIR (this tree\'s adapters/ holds the {DEFAULT_PACK} W0)')
    if args.command == 'build':
        for seed, name in W0_NAMES.items():
            target = adapters_dir / name
            if target.exists():
                shutil.rmtree(target)
            build_w0(seed, load_pack(args.pack), target)
            print(json.dumps({'built': str(target)}))
    else:
        for name in ('prompt-static', 'harness-static'):
            print(json.dumps(check_w0(parse_arm(name, pack=args.pack), adapters_dir), indent=1))
