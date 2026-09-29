"""Tier-0 tests of the v10 arms and adapters (DESIGN §1-§4): arm specs, config parsing, the W0 builders and
checkers, `revision`, `check_adapter` (manifest, tool caps, USAGE line rule, controls, dry-run kit), the
controls schema and the model-visible budget. No model call, no network.

Run:  <venv>/bin/python -m pytest -q tests/test_v10_arms.py
"""
from __future__ import annotations

from dataclasses import asdict, replace
import json
from pathlib import Path
import re
import subprocess
import sys

import pytest

PLUMBING = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUMBING))
import arms  # noqa: E402
import kit  # noqa: E402
import notes_render as NR  # noqa: E402
import paths  # noqa: E402
from harness_core import contracts as C  # noqa: E402
from packs.officeqa.pack import OfficeQAPack  # noqa: E402
from packs.toy.pack import ToyPack  # noqa: E402

PROMPT_W0 = PLUMBING / 'adapters/prompt-w0'
HARNESS_W0 = PLUMBING / 'adapters/harness-w0'
V10_ARMS = ['plain', 'prompt-static', 'prompt-evolve', 'harness-static', 'harness-evolve', 'harness-evolve-notes']
GATE_ARM = replace(C.STANDARD_ARMS['harness-static'], name='harness-gate', gate=True)
CONFIG = {'profile': 'v10', 'arm': 'harness-evolve', 'repeat': 1, 'pack': 'officeqa', 'order_seed': 'v10-order'}


def note(id, triggers, text='Some advice.', block=1):
    return C.NoteSpec(id=id, triggers=triggers, text=text, admitted_block=block, origin='learned')


def with_revision(adapter: Path) -> Path:
    """Stamp adapter.json with the tree's revision (what write_revision does for a candidate)."""
    info = json.loads((adapter / 'adapter.json').read_text())
    info['revision'] = arms.revision(adapter)
    (adapter / 'adapter.json').write_text(json.dumps(info, indent=2, sort_keys=True) + '\n')
    return adapter


def candidate(source: Path, dest: Path) -> Path:
    """A learner-style candidate: a copy of `source` with a note, revision stamped."""
    adapter = arms.copy_adapter(source, dest)
    NR.save_note(adapter, note('learned', ['public debt'], text='Read the whole row.'))
    return with_revision(adapter)

# ---------------------------------------------------------------------------------------------
# Arms and config
# ---------------------------------------------------------------------------------------------


def test_standard_arms_are_the_six_of_design_2():
    assert list(C.STANDARD_ARMS) == V10_ARMS and C.CONTRACT_VERSION == 'v10.0'
    for name, arm in C.STANDARD_ARMS.items():
        assert arm.name == name and arms.check_arm(arm) == arm and arm.gate is False
    a = C.STANDARD_ARMS
    assert (a['plain'].seed, a['plain'].learner, a['plain'].notes, a['plain'].l1_tools, a['plain'].controller) == ('none', None, False, False, False)
    for name in ('prompt-static', 'prompt-evolve'):
        assert (a[name].seed, a[name].l1_tools, a[name].controller, a[name].notes) == ('text', False, False, True)
    for name in ('harness-static', 'harness-evolve', 'harness-evolve-notes'):
        assert (a[name].seed, a[name].l1_tools, a[name].controller, a[name].notes) == ('harness', True, True, True)
    assert [a[n].learner for n in V10_ARMS] == [None, None, 'text', None, 'harness', 'text']
    # the one-factor contrast: harness-evolve-notes differs from harness-evolve only in the learner
    assert replace(a['harness-evolve'], name='x', learner='text') == replace(a['harness-evolve-notes'], name='x')
    assert {arms.architecture(arm) for arm in a.values()} == {'none', 'prompt', 'harness'}
    assert arms.architecture(a['prompt-evolve']) == 'prompt' and arms.architecture(a['harness-static']) == 'harness'
    assert arms.check_arm(GATE_ARM) == GATE_ARM
    for arm in a.values():
        assert 'gate' in asdict(arm) and arms.learning_type(arm)


def test_arm_validation_rules_of_design_2():
    h, p = C.STANDARD_ARMS['harness-static'], C.STANDARD_ARMS['prompt-static']
    bad = [(replace(p, l1_tools=True), 'prompt adapter has no tools'),
           (replace(p, controller=True), 'prompt adapter has no tools'),
           (replace(h, l1_tools=False), 'harness adapter serves tools and controls'),
           (replace(h, controller=False), 'harness adapter serves tools and controls'),
           (replace(C.STANDARD_ARMS['plain'], l1_tools=True), 'seed=none has neither'),
           (replace(C.STANDARD_ARMS['plain'], notes=True), 'has no notes and no learner'),
           (replace(C.STANDARD_ARMS['plain'], learner='text'), 'notes-only learner runs on the prompt or the harness'),
           (replace(C.STANDARD_ARMS['plain'], learner='harness'), 'harness learner'),
           (replace(p, learner='harness'), 'harness learner'),
           (replace(p, gate=True), 'gate needs controller=true'),
           (replace(h, learner='text', notes=False), 'learner arm renders notes'),
           (replace(h, seed='rules'), 'seed must be none, text or harness'),
           (replace(h, learner='mechanism'), 'learner must be null, text or harness'),
           (replace(h, gate=True), 'standard arm name'),
           (replace(h, name='Bad Name'), 'arm.name must match'),
           (replace(h, pack='nope'), 'unknown pack')]
    for arm, message in bad:
        with pytest.raises(ValueError, match=message):
            arms.check_arm(arm)
    with pytest.raises(TypeError):
        arms.check_arm('plain')
    # a custom harness arm with a text learner and the gate on is a valid (non-standard) arm
    arms.check_arm(replace(GATE_ARM, name='gate-notes', learner='text'))


def test_parse_arm_refuses_the_v9_names_and_needs_the_gate_field():
    for old in ('text-static', 'text-evolve', 'mech-static', 'mech-evolve'):
        with pytest.raises(ValueError, match=r"v9 arm name.*harness-evolve-notes"):
            arms.parse_arm(old)
    with pytest.raises(ValueError, match='unknown arm'):
        arms.parse_arm('cards')
    spec = asdict(C.STANDARD_ARMS['harness-evolve'])
    assert arms.parse_arm(spec) == C.STANDARD_ARMS['harness-evolve']
    assert arms.parse_arm('harness-evolve', pack='toy').pack == 'toy'
    without_gate = {k: v for k, v in spec.items() if k != 'gate'}
    with pytest.raises(ValueError, match=r"missing \['gate'\]"):
        arms.parse_arm(without_gate)
    with pytest.raises(ValueError, match='differs from the root pack'):
        arms.parse_arm(spec, pack='toy')
    custom = arms.parse_arm({**asdict(GATE_ARM)})
    assert custom.gate and custom.name == 'harness-gate'


def test_parse_config_profile_and_neutral_root_names():
    spec = arms.parse_config(CONFIG, root_name='v10-r1-d')
    assert spec.arm == C.STANDARD_ARMS['harness-evolve'] and spec.repeat == 1 and spec.root == 'v10-r1-d'
    assert arms.parse_config({k: v for k, v in CONFIG.items() if k != 'profile'}, root_name='v10-r1-d') == spec
    with pytest.raises(ValueError, match=r"profile 'v9'.*officeqa-pro133-v9-plumbing"):
        arms.parse_config({**CONFIG, 'profile': 'v9'}, root_name='v10-r1-d')
    with pytest.raises(ValueError, match="profile must be 'v10'"):
        arms.parse_config({**CONFIG, 'profile': 'v8'}, root_name='v10-r1-d')
    with pytest.raises(ValueError, match='v9 arm name'):
        arms.parse_config({**CONFIG, 'arm': 'mech-evolve'}, root_name='v10-r1-d')
    for root in ('v10-harness-d', 'v10-evolve-d', 'v10-adapter-d', 'v10-revision-1', 'v10-prompt-b', 'v10-none-a'):
        with pytest.raises(ValueError, match='reveals the arm'):
            arms.parse_config(CONFIG, root_name=root)
    with pytest.raises(ValueError, match='unknown .task-paths.json keys'):
        arms.parse_config({**CONFIG, 'gate': True}, root_name='v10-r1-d')
    assert spec.to_json()['arm']['gate'] is False


def test_revealing_fragments_over_paths_and_names():
    assert arms.revealing_fragments('/roots/v10-r1-d/runs/code/0123abcdef45/tools') == []
    assert arms.revealing_fragments('/x/adapters/harness-w0/tools') == ['adapt', 'harness']
    assert arms.revealing_fragments('/x/evolution/03/adapter/tools') == ['evol', 'adapt']
    assert arms.revealing_fragments('tools_revision.py') == ['revision']
    assert arms.revealing_fragments('seed_lookup.py') == ['seed']
    for name in ('cli.py', 'otables.py', 'calc.py', 'lookup.py', 'corpus.py', 'manifest.json', 'USAGE.md',
                 'find_tables', 'show_table', 'printings'):
        assert arms.revealing_fragments(name) == [], name
    with pytest.raises(ValueError, match='reveals the arm'):
        arms.check_neutral('/x/runs/code/harness/tools', 'code root')
    assert arms.check_neutral('/x/runs/code/0123abcdef45/tools', 'code root').endswith('/tools')

# ---------------------------------------------------------------------------------------------
# Controls (DESIGN §3)
# ---------------------------------------------------------------------------------------------


def test_checked_controls_schema_and_ranges():
    assert C.checked_controls(None) == C.CONTROLS_DEFAULTS == C.checked_controls({})
    assert C.CONTROLS_DEFAULTS == {'loop_guard': True, 'loop_threshold': 3, 'loop_notes_max': 3, 'splice': True,
                                   'ledger_splice_bytes': 8000, 'gate': False, 'enforce_requery_max': 0}
    assert C.checked_controls({'gate': True, 'enforce_requery_max': 1})['enforce_requery_max'] == 1
    assert C.checked_controls({'loop_guard': False})['loop_guard'] is False
    bad = [({'loop_threshold': 1}, r'loop_threshold must be an integer in \[3, 6\]'),
           ({'loop_threshold': 7}, 'loop_threshold'), ({'loop_notes_max': 0}, 'loop_notes_max'),
           ({'ledger_splice_bytes': 100}, 'ledger_splice_bytes'), ({'enforce_requery_max': 2}, 'enforce_requery_max'),
           ({'loop_guard': 1}, 'loop_guard must be true or false'), ({'gate': 'no'}, 'gate must be true or false'),
           ({'loop_threshold': True}, 'loop_threshold'), ({'disabled_rules': []}, 'unknown controls key'),
           ({'enforce_requery_max': 1}, 'needs gate=true'), ([], 'JSON object')]
    for controls, message in bad:
        with pytest.raises(ValueError, match=message):
            C.checked_controls(controls)
    assert set(C.CONTROLS_RANGES) | set(C.CONTROLS_BOOLEANS) == set(C.CONTROLS_DEFAULTS)
    assert C.SUPPORT_MAX == 3
    assert (C.TOOL_FILES_MAX, C.TOOL_FILE_MAX, C.TOOL_TOTAL_MAX, C.TOOL_NEW_FILE_MAX, C.USAGE_MAX) == (12, 196_608, 393_216, 16_384, 3_072)

# ---------------------------------------------------------------------------------------------
# Revision (DESIGN §1)
# ---------------------------------------------------------------------------------------------


def test_revision_is_sha12_of_the_tree_minus_adapter_json(tmp_path):
    a = arms.copy_adapter(PROMPT_W0, tmp_path / 'a')
    r = arms.revision(a)
    assert re.fullmatch(r'[0-9a-f]{12}', r)
    assert r == arms.revision(arms.w0_files('text', OfficeQAPack()))          # the mapping form
    # writing the hash into adapter.json does not change it; changing any other file does
    with_revision(a)
    assert arms.revision(a) == r
    NR.save_note(a, note('n1', ['x']))
    assert arms.revision(a) != r
    assert 'adapter.json' not in arms.snapshot(a) and set(arms.snapshot(a)) == {'SEED.md', 'notes/.keep', 'notes/n1.json'}
    assert arms.revision(HARNESS_W0) != arms.revision(PROMPT_W0)
    (a / 'link').symlink_to(a / 'SEED.md')
    with pytest.raises(ValueError, match='symlinks are refused'):
        arms.revision(a)

# ---------------------------------------------------------------------------------------------
# W0 (DESIGN §4)
# ---------------------------------------------------------------------------------------------


def test_committed_w0_equals_generated_and_passes_every_check():
    prompt = arms.check_w0(C.STANDARD_ARMS['prompt-static'])
    harness = arms.check_w0(C.STANDARD_ARMS['harness-static'])
    assert arms.check_w0(C.STANDARD_ARMS['plain']) == {'adapter': None}
    assert Path(prompt['adapter']) == PROMPT_W0 and Path(harness['adapter']) == HARNESS_W0
    assert prompt['revision'] == harness['revision'] == 'w0'
    assert json.loads((PROMPT_W0 / 'adapter.json').read_text()) == {'architecture': 'prompt', 'contract': 'v10.0',
                                                                    'pack': 'officeqa', 'revision': 'w0'}
    assert json.loads((HARNESS_W0 / 'adapter.json').read_text())['architecture'] == 'harness'
    assert sorted(arms.tree_files(HARNESS_W0)) == ['SEED.md', 'adapter.json', 'controls.json', 'notes/.keep',
                                                   'tools/USAGE.md', 'tools/calc.py', 'tools/cli.py',
                                                   'tools/manifest.json', 'tools/otables.py']
    assert sorted(arms.tree_files(PROMPT_W0)) == ['SEED.md', 'adapter.json', 'notes/.keep']
    pack = OfficeQAPack()
    assert (HARNESS_W0 / 'SEED.md').read_text() == pack.seed_text_harness()
    assert (PROMPT_W0 / 'SEED.md').read_text() == pack.seed_text_prompt()
    # the tools are byte copies of the pack tools and of harness_core/tools/calc.py (calc pinned)
    for name in ('cli.py', 'otables.py'):
        assert (HARNESS_W0 / 'tools' / name).read_bytes() == (paths.PACKS / 'officeqa/tools' / name).read_bytes()
    assert (HARNESS_W0 / 'tools/calc.py').read_bytes() == arms.CALC_SCRIPT.read_bytes()
    manifest = json.loads((HARNESS_W0 / 'tools/manifest.json').read_text())
    assert manifest == {'tools': {'find_tables': {'script': 'cli.py', 'args': ['find_tables']},
                                  'show_table': {'script': 'cli.py', 'args': ['show_table']},
                                  'printings': {'script': 'cli.py', 'args': ['printings']},
                                  'calc': {'script': 'calc.py', 'args': []}}}
    assert json.loads((HARNESS_W0 / 'controls.json').read_text()) == C.CONTROLS_DEFAULTS
    usage = (HARNESS_W0 / 'tools/USAGE.md').read_text()
    assert usage == pack.usage().rstrip() + '\n' + arms.CALC_USAGE and len(usage.encode()) <= C.USAGE_MAX
    assert not arms.usage_violations(usage, list(manifest['tools']), arms.option_strings(
        {n: (HARNESS_W0 / 'tools' / n).read_text() for n in ('cli.py', 'otables.py', 'calc.py')}))
    # model-visible bytes: prompt = SEED only; harness = SEED + USAGE.md + the controller templates (no rules)
    assert prompt['model_visible']['total'] == len(pack.seed_text_prompt().encode()) and prompt['model_visible']['controller'] == 0
    visible = harness['model_visible']
    assert visible['total'] == visible['seed_md'] + visible['usage'] + visible['controller'] and visible['rule_templates'] == 0
    assert visible['usage'] == len(usage.encode()) and visible['controller'] > 0 and visible['within_budget']
    assert harness['checked']['controls'] == C.CONTROLS_DEFAULTS and set(harness['checked']['tools']['tools']) == set(manifest['tools'])
    assert harness['checked']['tools']['closure']['show_table'] == ['cli.py', 'otables.py']
    assert {Path(f).name for f in harness['checked']['code']['files']} == {'cli.py', 'otables.py', 'calc.py'}


def test_seed_parity_checklist_and_prompt_seed_names_no_tool():
    pack = OfficeQAPack()
    parity = arms.seed_parity(pack)
    assert parity['items'] == [i['id'] for i in pack.SEED_KNOWLEDGE] and len(parity['items']) >= 10
    assert all(parity['bytes'][s] <= C.SEED_MAX for s in ('harness', 'prompt'))
    assert arms.harness_tool_names(pack) == ['calc', 'find_tables', 'printings', 'show_table']
    assert not arms.names_tools(pack.seed_text_prompt(), arms.harness_tool_names(pack))
    assert arms.names_tools(pack.seed_text_harness(), arms.harness_tool_names(pack)) == ['calc', 'find_tables', 'printings', 'show_table']
    assert arms.names_tools('calculate the printing', ['calc', 'printings']) == []          # whole words only
    assert arms.knowledge_missing(pack.seed_text_harness(), pack, 'harness') == []
    assert arms.knowledge_missing(pack.seed_text_prompt(), pack, 'prompt') == []
    assert 'latest_printing' in arms.knowledge_missing('nothing here', pack, 'prompt')

    class Broken(OfficeQAPack):
        def seed_text_prompt(self):                      # same size (4 B of headroom) and no checklist sentence touched
            return super().seed_text_prompt().replace('both with python,', 'both with calc,  ')
    with pytest.raises(ValueError, match="names harness tools \\['calc'\\]"):
        arms.seed_parity(Broken())

    class Missing(OfficeQAPack):
        def seed_text_prompt(self):
            return super().seed_text_prompt().replace('exactly 12 month rows', '12 month rows')
    with pytest.raises(ValueError, match=r"prompt SEED lacks the knowledge items \['period_match'\]"):
        arms.seed_parity(Missing())

    class Short(OfficeQAPack):                           # a SEED near the 4,096 B cap cannot grow by 25%: shrink it
        def seed_text_prompt(self):
            return super().seed_text_prompt()[:2500]
    with pytest.raises(ValueError, match='differ by more than 25%'):
        arms.seed_parity(Short())


def test_check_w0_detects_drift(tmp_path):
    adapters = tmp_path / 'adapters'
    for kind, name in arms.W0_NAMES.items():
        arms.build_w0(kind, OfficeQAPack(), adapters / name)
    for name in ('prompt-static', 'harness-evolve'):
        arms.check_w0(C.STANDARD_ARMS[name], adapters)
    (adapters / 'prompt-w0/SEED.md').write_text('drifted\n')
    with pytest.raises(ValueError, match=r"prompt-w0 differs from the generated W0: \['SEED.md'\]"):
        arms.check_w0(C.STANDARD_ARMS['prompt-static'], adapters)
    (adapters / 'harness-w0/tools/cli.py').write_text('print(1)\n')
    with pytest.raises(ValueError, match=r"harness-w0 differs from the generated W0: \['tools/cli.py'\]"):
        arms.check_w0(C.STANDARD_ARMS['harness-static'], adapters)
    with pytest.raises(ValueError, match='not empty'):
        arms.build_w0('text', OfficeQAPack(), adapters / 'prompt-w0')
    with pytest.raises(ValueError, match='unknown W0 kind'):
        arms.w0_files('rules', OfficeQAPack())


def test_cli_refuses_another_pack_into_the_tree_adapters(tmp_path):
    """`arms.py build --pack toy` without --adapters would overwrite adapters/*-w0 (W0_NAMES are pack-independent)
    with the toy W0: refused; an unknown pack name is refused by the choices."""
    for command in ('build', 'check'):
        done = subprocess.run([sys.executable, '-B', str(PLUMBING / 'arms.py'), command, '--pack', 'toy'],
                              capture_output=True, text=True, cwd=str(PLUMBING), timeout=120)
        assert done.returncode == 2 and 'pass --adapters DIR' in done.stderr and 'officeqa' in done.stderr
    done = subprocess.run([sys.executable, '-B', str(PLUMBING / 'arms.py'), 'build', '--pack', 'nosuch',
                           '--adapters', str(tmp_path / 'x')], capture_output=True, text=True, cwd=str(PLUMBING), timeout=120)
    assert done.returncode == 2 and 'invalid choice' in done.stderr
    assert sorted(p.name for p in (PLUMBING / 'adapters').iterdir()) == ['harness-w0', 'prompt-w0']
    assert not (tmp_path / 'x').exists()


def test_toy_w0_is_built_by_the_same_builder_and_serves_its_tools(tmp_path):
    """packs/toy gets a W0 tools set (lookup.py = its tools.py, corpus.py, calc.py, manifest, USAGE.md) so the
    toy E2E can serve tools from the adapter; `arms.py build --pack toy` writes it."""
    adapters = tmp_path / 'adapters'
    done = subprocess.run([sys.executable, '-B', str(PLUMBING / 'arms.py'), 'build', '--pack', 'toy', '--adapters',
                           str(adapters)], capture_output=True, text=True, cwd=str(PLUMBING), timeout=120)
    assert done.returncode == 0, done.stderr
    toy_prompt, toy_harness = (arms.parse_arm(n, pack='toy') for n in ('prompt-static', 'harness-static'))
    assert arms.w0_dir(toy_harness, adapters) == adapters / 'harness-w0'
    checked = {arm.name: arms.check_w0(arm, adapters) for arm in (toy_prompt, toy_harness)}
    assert all(c['model_visible']['within_budget'] for c in checked.values())
    tools = adapters / 'harness-w0/tools'
    assert sorted(p.name for p in tools.iterdir()) == ['USAGE.md', 'calc.py', 'corpus.py', 'lookup.py', 'manifest.json']
    assert (tools / 'lookup.py').read_bytes() == (paths.PACKS / 'toy/tools.py').read_bytes()
    assert json.loads((tools / 'manifest.json').read_text())['tools'] == {'lookup': {'script': 'lookup.py', 'args': []},
                                                                          'calc': {'script': 'calc.py', 'args': []}}
    assert [p.name for p in kit._import_closure(tools / 'lookup.py')] == ['corpus.py', 'lookup.py']
    run = subprocess.run([str(paths.ACTOR_PYTHON), '-I', '-B', str(tools / 'lookup.py'), 'widgets_2020_b.txt', 'North'],
                         capture_output=True, text=True, timeout=60, cwd='/', env={})
    assert run.returncode == 0 and '⟦toy widgets_2020_b.txt:5|Q2 2020|North|131.5r⟧' in run.stdout, run.stderr
    assert arms.seed_parity(ToyPack())['items'] == ['latest_printing', 'period', 'decimal_form', 'answer_count', 'compute_with_code']
    assert not arms.names_tools(ToyPack().seed_text_prompt(), arms.harness_tool_names(ToyPack()))
    with pytest.raises(ValueError, match="adapter pack 'officeqa' is not the arm pack 'toy'"):
        arms.check_adapter(HARNESS_W0, toy_harness)
    with pytest.raises(ValueError, match='no W0 adapter is built for pack'):
        arms.w0_dir(replace(toy_harness, pack='spreadsheet'), adapters)

# ---------------------------------------------------------------------------------------------
# check_adapter (DESIGN §3): candidates, manifest, caps, controls, revision
# ---------------------------------------------------------------------------------------------


def test_check_adapter_accepts_w0_and_revised_candidates(tmp_path):
    prompt, harness, notes = (C.STANDARD_ARMS[n] for n in ('prompt-evolve', 'harness-evolve', 'harness-evolve-notes'))
    assert arms.check_adapter(PROMPT_W0, prompt)['architecture'] == 'prompt'
    assert arms.check_adapter(HARNESS_W0, harness)['architecture'] == 'harness'
    assert arms.check_adapter(HARNESS_W0, notes)['revision'] == 'w0'
    a = candidate(PROMPT_W0, tmp_path / 'a')
    checked = arms.check_adapter(a, prompt)
    assert checked['notes'] == 1 and checked['revision'] == arms.revision(a) != 'w0'
    b = candidate(HARNESS_W0, tmp_path / 'b')
    checked = arms.check_adapter(b, harness)
    assert checked['notes'] == 1 and checked['model_visible']['notes'] > 0 and checked['controls']['gate'] is False
    # architecture / pack mismatches
    with pytest.raises(ValueError, match='architecture'):
        arms.check_adapter(HARNESS_W0, prompt)
    with pytest.raises(ValueError, match='architecture'):
        arms.check_adapter(PROMPT_W0, harness)
    with pytest.raises(ValueError, match='adapter pack'):
        arms.check_adapter(HARNESS_W0, replace(harness, pack='toy'))


def test_check_adapter_revision_rules(tmp_path):
    prompt = C.STANDARD_ARMS['prompt-evolve']
    # a changed tree still claiming 'w0'
    a = arms.copy_adapter(PROMPT_W0, tmp_path / 'a')
    NR.save_note(a, note('n1', ['x']))
    with pytest.raises(ValueError, match="revision 'w0' on a tree that is not the generated W0"):
        arms.check_adapter(a, prompt)
    with_revision(a)
    assert arms.check_adapter(a, prompt)['revision'] == arms.revision(a)
    # a stale sha after a further edit
    NR.save_note(a, note('n2', ['y']))
    with pytest.raises(ValueError, match='is not the tree revision'):
        arms.check_adapter(a, prompt)
    info = json.loads((a / 'adapter.json').read_text())
    for bad, message in ((dict(info, revision='abc'), "'w0' or a 12-hex sha"), (dict(info, contract='v9.1'), 'contract v10.0'),
                         (dict(info, architecture='mechanism'), 'architecture prompt|harness'),
                         ({k: v for k, v in info.items() if k != 'pack'}, 'exactly the keys')):
        (a / 'adapter.json').write_text(json.dumps(bad))
        with pytest.raises(ValueError, match=message):
            arms.check_adapter(a, prompt)


def test_check_adapter_prompt_architecture_rules(tmp_path):
    prompt = C.STANDARD_ARMS['prompt-evolve']
    for extra in ('tools', 'controls.json', 'rules', 'policy.json'):
        a = arms.copy_adapter(PROMPT_W0, tmp_path / extra.replace('.', '_'))
        if extra.endswith('.json'):
            (a / extra).write_text('{}\n')
        else:
            (a / extra).mkdir()
        with_revision(a)
        with pytest.raises(ValueError, match=f'a prompt adapter carries no {extra}'):
            arms.check_adapter(a, prompt)
    # a note (learner-written) that names a harness tool is refused, the check runs on every note
    b = arms.copy_adapter(PROMPT_W0, tmp_path / 'b')
    NR.save_note(b, note('bad', ['debt'], text='Run show_table on the row first.'))
    with_revision(b)
    with pytest.raises(ValueError, match=r"must not name the harness tools \(note bad names \['show_table'\]\)"):
        arms.check_adapter(b, prompt)
    c = arms.copy_adapter(PROMPT_W0, tmp_path / 'c')
    (c / 'SEED.md').write_text((c / 'SEED.md').read_text() + '\nUse printings.\n')
    with_revision(c)
    with pytest.raises(ValueError, match=r"SEED.md names \['printings'\]"):
        arms.check_adapter(c, prompt)


def test_check_adapter_controls_and_gate_rules(tmp_path):
    harness = C.STANDARD_ARMS['harness-evolve']
    a = arms.copy_adapter(HARNESS_W0, tmp_path / 'a')
    (a / 'controls.json').unlink()
    with_revision(a)
    with pytest.raises(ValueError, match='controls.json missing'):
        arms.check_adapter(a, harness)
    (a / 'controls.json').write_text(json.dumps({**C.CONTROLS_DEFAULTS, 'loop_threshold': 1}))
    with_revision(a)
    with pytest.raises(ValueError, match='loop_threshold'):
        arms.check_adapter(a, harness)
    (a / 'controls.json').write_text(json.dumps({**C.CONTROLS_DEFAULTS, 'gate': True}))
    with_revision(a)
    with pytest.raises(ValueError, match="gate=True but the arm 'harness-evolve' has gate=False"):
        arms.check_adapter(a, harness)
    # a gate-on adapter belongs to a gate arm; rules/ and policy.json are valid there and refused otherwise
    checked = arms.check_adapter(a, GATE_ARM)
    assert checked['controls']['gate'] is True and checked['rules'] == 0
    assert {Path(f).name for f in checked['code']['files']} == {'cli.py', 'otables.py', 'calc.py', 'submit.py'}
    assert checked['model_visible']['gate_frame'] > 0
    (a / 'rules').mkdir()
    (a / 'policy.json').write_text(json.dumps({**C.POLICY_DEFAULTS, 'loop_threshold': 1}))
    with_revision(a)
    with pytest.raises(ValueError, match='loop_threshold'):
        arms.check_adapter(a, GATE_ARM)
    (a / 'policy.json').write_text(json.dumps(C.POLICY_DEFAULTS))
    with_revision(a)
    assert arms.check_adapter(a, GATE_ARM)['rules'] == 0
    b = arms.copy_adapter(HARNESS_W0, tmp_path / 'b')
    (b / 'rules').mkdir()
    with_revision(b)
    with pytest.raises(ValueError, match='refused when the gate is off'):
        arms.check_adapter(b, harness)
    c = arms.copy_adapter(HARNESS_W0, tmp_path / 'c')
    (c / 'policy.json').write_text('{}')
    with_revision(c)
    with pytest.raises(ValueError, match='refused when the gate is off'):
        arms.check_adapter(c, harness)
    assert arms.load_controls(HARNESS_W0) == C.CONTROLS_DEFAULTS


def _harness_copy(tmp_path, name):
    return arms.copy_adapter(HARNESS_W0, tmp_path / name)


def _manifest(adapter):
    return json.loads((adapter / 'tools/manifest.json').read_text())


def _write_manifest(adapter, data):
    (adapter / 'tools/manifest.json').write_text(json.dumps(data, indent=2, sort_keys=True) + '\n')


def test_check_adapter_manifest_rules(tmp_path):
    harness = C.STANDARD_ARMS['harness-evolve']
    base = _manifest(HARNESS_W0)['tools']
    cases = [
        ({**base, 'sed': {'script': 'cli.py', 'args': ['show_table']}}, 'shadow the system command'),
        ({**base, 'python3': {'script': 'cli.py', 'args': []}}, 'reserved'),
        ({**base, 'seed_tool': {'script': 'cli.py', 'args': []}}, 'reveals the arm'),
        ({**base, 'harness_tool': {'script': 'cli.py', 'args': []}}, 'reveals the arm'),
        ({**base, 'Bad': {'script': 'cli.py', 'args': []}}, 'Invalid or reserved'),
        ({**base, 'extra': {'script': 'missing.py', 'args': []}}, 'not a regular file'),
        ({**base, 'extra': {'script': '../cli.py', 'args': []}}, 'python file name'),
        ({**base, 'extra': {'script': 'cli.py', 'args': ['two words']}}, 'plain tokens'),
        ({**base, 'extra': {'script': 'cli.py'}}, 'exactly the keys script and args'),
        ({k: v for k, v in base.items() if k != 'calc'}, "manifest must define 'calc'"),
        ({**base, 'calc': {'script': 'cli.py', 'args': ['calc']}}, 'calc.*script.*calc.py'),
    ]
    for number, (tools, message) in enumerate(cases):
        a = _harness_copy(tmp_path, f'm{number}')
        _write_manifest(a, {'tools': tools})
        with_revision(a)
        with pytest.raises(ValueError, match=message):
            arms.check_adapter(a, harness)
    for number, bad in enumerate(('[]', '{"tools": {}}', '{"tools": {}, "x": 1}')):
        a = _harness_copy(tmp_path, f'j{number}')
        (a / 'tools/manifest.json').write_text(bad)
        with_revision(a)
        with pytest.raises(ValueError, match='manifest.json is'):
            arms.check_adapter(a, harness)
    # calc is sha-pinned to the W0 copy this round
    a = _harness_copy(tmp_path, 'calc')
    (a / 'tools/calc.py').write_text((a / 'tools/calc.py').read_text() + '\n# changed\n')
    with_revision(a)
    with pytest.raises(ValueError, match='calc is sha-pinned'):
        arms.check_adapter(a, harness)
    # manifest_wrappers: the kit form, keyed by the (neutral) tools dir the caller passes
    wrappers = arms.manifest_wrappers(HARNESS_W0 / 'tools')
    assert wrappers == {'calc': [str(HARNESS_W0 / 'tools/calc.py')],
                        'find_tables': [str(HARNESS_W0 / 'tools/cli.py'), 'find_tables'],
                        'printings': [str(HARNESS_W0 / 'tools/cli.py'), 'printings'],
                        'show_table': [str(HARNESS_W0 / 'tools/cli.py'), 'show_table']}


def test_check_adapter_tool_files_caps_and_usage_rule(tmp_path):
    harness = C.STANDARD_ARMS['harness-evolve']

    def refused(name, mutate, message, *, stamp=True):
        a = _harness_copy(tmp_path, name)
        mutate(a)
        if stamp:
            with_revision(a)
        with pytest.raises(ValueError, match=message):
            arms.check_adapter(a, harness)

    refused('usage_big', lambda a: (a / 'tools/USAGE.md').write_text('calc ' * 700), 'USAGE.md is .* B, over 3072')
    refused('usage_rule', lambda a: (a / 'tools/USAGE.md').write_text(
        (a / 'tools/USAGE.md').read_text() + 'Always answer 42 when unsure.\n'), 'naming no manifest tool and no tool option')
    refused('usage_opt', lambda a: (a / 'tools/USAGE.md').write_text(
        (a / 'tools/USAGE.md').read_text() + 'use --nonexistent for speed\n'), 'naming no manifest tool')
    refused('usage_missing', lambda a: (a / 'tools/USAGE.md').unlink(), 'USAGE.md missing')
    refused('stdlib', lambda a: (a / 'tools/json.py').write_text('x = 1\n'), 'shadows the stdlib module')
    refused('subdir', lambda a: (a / 'tools/pkg').mkdir(), 'only regular files')
    # a symlink is refused by the revision hash itself (tree_files), before any tools check
    refused('symlink', lambda a: (a / 'tools/other.py').symlink_to(a / 'tools/cli.py'), 'symlinks are refused', stamp=False)
    refused('oddfile', lambda a: (a / 'tools/notes.txt').write_text('x'), 'allowed files are')
    refused('revealing', lambda a: (a / 'tools/seed_helper.py').write_text('x = 1\n'), 'reveals the arm')
    refused('many', lambda a: [(a / f'tools/m{i:02}.py').write_text('x = 1\n') for i in range(10)], 'python files in tools/, over 12')
    refused('big', lambda a: (a / 'tools/big.py').write_text('# ' + 'x' * C.TOOL_FILE_MAX + '\n'), 'over 196608 B')
    refused('total', lambda a: [(a / f'tools/b{i}.py').write_text('# ' + 'x' * 150_000 + '\n') for i in range(2)], 'over 393216 B')
    refused('syntax', lambda a: (a / 'tools/cli.py').write_text('def broken(:\n'), 'does not parse')
    refused('package', lambda a: ((a / 'tools/helpers').mkdir(), (a / 'tools/cli.py').write_text('import helpers\n')), 'only regular files')
    refused('no_tools', lambda a: __import__('shutil').rmtree(a / 'tools'), 'tools/ must be a real directory')
    # a legal addition: a new sibling module the CLI imports, and USAGE lines that name a real new option
    a = _harness_copy(tmp_path, 'ok')
    (a / 'tools/extra.py').write_text('def hint():\n    return "--grep"\n')
    (a / 'tools/cli.py').write_text((a / 'tools/cli.py').read_text() + '\nimport extra\n')
    (a / 'tools/USAGE.md').write_text((a / 'tools/USAGE.md').read_text() + '      --grep also matches section labels\n')
    with_revision(a)
    checked = arms.check_adapter(a, harness)
    assert checked['tools']['python_files'] == 4 and checked['tools']['closure']['show_table'] == ['cli.py', 'extra.py', 'otables.py']
    assert {Path(f).name for f in checked['code']['files']} == {'cli.py', 'otables.py', 'calc.py', 'extra.py'}


def test_usage_violations_unit():
    names, options = ['calc', 'show_table'], {'--grep', '--long'}
    text = '# Tools\n\n  show_table <file> <line>\n      pages of 8 KB\n      use --grep to filter\n\nAlways say 42.\n  calc -c 1+1\n'
    assert arms.usage_violations(text, names, options) == ['      pages of 8 KB', 'Always say 42.']
    assert arms.usage_violations('  --nope is fast\n', names, options) == ['  --nope is fast']
    assert arms.option_strings({'a.py': 'p.add_argument("--from-row")\n# --x-1 --Y', 'b.py': ''}) == {'--from-row', '--x-1'}


def test_model_visible_budget_and_note_overflow(tmp_path):
    prompt, harness = C.STANDARD_ARMS['prompt-evolve'], C.STANDARD_ARMS['harness-evolve']
    assert arms.model_visible(None, prompt)['total'] == 0
    a = arms.copy_adapter(PROMPT_W0, tmp_path / 'a')
    for i in range(30):
        NR.save_note(a, note(f'n{i:02}', ['x'], text='y' * C.NOTE_MAX))
    with_revision(a)
    with pytest.raises(ValueError, match='exceeds 16384'):
        arms.check_adapter(a, prompt)
    b = arms.copy_adapter(HARNESS_W0, tmp_path / 'b')
    NR.save_note(b, note('share', ['percent'], text='A note with   spaces.'))
    visible = arms.model_visible(b, harness)
    rendered = len(NR.render_note(note('share', ['percent'], text='A note with   spaces.')).encode())
    assert visible['notes'] == rendered and visible['notes_count'] == 1
    assert visible['total'] == visible['seed_md'] + rendered + visible['usage'] + visible['controller']
    fixed = arms.harness_fixed_text()
    assert visible['controller'] == sum(fixed[k] for k in arms.CONTROLLER_COUNTED)
    # a prompt adapter never counts USAGE.md or controller text, whatever files it has
    assert arms.model_visible(PROMPT_W0, prompt)['usage'] == 0 and arms.model_visible(PROMPT_W0, prompt)['controller'] == 0


def test_dry_run_kit_refuses_a_broken_revision_before_any_episode(tmp_path):
    """A candidate whose manifest wraps fine but whose script is not a regular file at kit time fails in
    check_adapter (dry-run kit), never in prepare_episode."""
    a = _harness_copy(tmp_path, 'a')
    with_revision(a)
    code = arms.dry_run_kit(a, C.STANDARD_ARMS['harness-evolve'])
    assert code['dirs'] == [str((a / 'tools').resolve())] and len(code['files']) == 3
    assert not list(tmp_path.glob('kit-dry-run-*'))            # the scratch workspace is gone


def test_plumbing_never_imports_adapter_code():
    """DESIGN §1: the controller process never imports or executes adapter python (it reads it with ast only)."""
    pattern = re.compile(r'^\s*(?:from|import)\s+(?:adapters|evolution|runs)\b', re.M)
    hits = []
    for path in [*PLUMBING.glob('*.py'), *(PLUMBING / 'harness_core').rglob('*.py'), *(PLUMBING / 'packs').rglob('*.py')]:
        if pattern.search(path.read_text(encoding='utf-8')):
            hits.append(path.name)
    assert not hits, hits
    assert 'import' not in ''.join(l for l in Path(arms.__file__).read_text().splitlines()
                                   if 'adapters/' in l and l.strip().startswith(('from ', 'import ')))
