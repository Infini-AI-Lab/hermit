"""Saved-artifact consistency audit of one v10 root (DESIGN §3, §6; S3); no model calls.

The arm comes from the root's own records (freeze.json "campaign", checked against .task-paths.json when
present); nothing is inferred from a substring. Checks, by group:

- freeze: code, PROTOCOL/PREREG, harness_core/, packs/, adapters/, codex and settings unchanged; PREREG
  hash recorded; the W0 snapshot unchanged and equal to the arm's W0; the model-visible record within budget.
- completion: status completed; exactly one scored episode per question of this repeat, in its order; a
  declared subset is the prepared manifest's and the seal and grades cover exactly it.
- episodes (scored and verification): invocation.arm == the root's arm; the frozen runtime equals the
  freeze record; kit_after.intact; v10 settings as declared; wall clock; retries; AGENTS.md size;
  prediction unchanged; AGENTS.md re-derives from COMMON + the served adapter + the chosen notes; the served
  notes re-derive from (adapter, question) and are at most NOTES_PER_EPISODE; the adapter revision is the
  one served in that block and equals arms.revision(tree); the temperature / top_p of every invocation and
  every request body equal the frozen ones; every workspace path (scored and verification) names no arm, and
  a verification episode lives under runs/verify/BB/<token>/<NN> (opaque names, never under evolution/).
- served tools (harness arms; DESIGN §3 neutral code root, §6-6; `code_root_findings`): the kit records
  exactly one code root, <root>/runs/code/<revision>/tools, whose path reveals no arm; every kit tool script
  starts with it; every granted code file lives in it and its sha256 equals the same file of the served
  revision's tools/; the granted files and listing dirs are exactly the import closure of the manifest
  scripts re-derived from the code root (harness_core.runner.import_closure); the marker carries code_root
  and code_sha12; the code root's revision segment is arms.revision(adapter); no tool file or manifest
  name reveals the arm; controls.json of the served revision equals the W0's (no control evolution this
  round). Arms without adapter tools record no code root, no code files and no wrappers.
- submit iff gate (DESIGN §3; `submit_findings`): the kit has a submit wrapper, README.md a submit section
  and the summary submit / gate-round / re-query counters and api/checks only when the arm's gate is on;
  README has the `# Tools` section (campaign.TOOLS_HEADING) iff the arm serves tools; `# Task conventions`
  always; a gate-off controller arm has no enforcement re-query call.
- records: every api call has the fixed model, forwarded reasoning, no output caps, the fixed upstream;
  controller=None arms carry none of the controller record fields (byte-identical v8 layout); controller
  arms have origin/kind/codex_input_sha256 on every call, every controller call has a codex parent and copies
  its native request settings, a ledger and the observation_coverage record.
- learning: learner arms have a round for every block but the last (never 'failed'), seals before
  release, exact scope (released + withheld = the block), no withheld reference in evidence, every served
  adapter within the text budget; the adapter of block b is the round b-1 candidate when that round
  activated one, else the block b-1 adapter, block 1 serves the W0, every recorded revision equals
  arms.revision(tree) and every activated round has a mechanical-admission.json naming its candidate
  revision (`block_adapters`); every learned note served was admitted by a paired re-run round
  (`learning-ledger.jsonl` row kind=note | revision | bundle, admitted=true, t <= the round, naming the
  note with the same text and triggers); every served tool revision (tools/ differing from the revision served before it) has an admitting
  ledger row (kind revision | bundle) and a `mechanical-admission.json` of that round whose admitted revision
  staged exactly those files and whose candidate revision is the served tree, and passes check_adapter; learned notes and the USAGE.md lines added
  since W0 pass the pack leak screen and the question-copy check against what was released up to that
  round; SEED.md and controls.json never change from W0 (fixed this round); rules/ and policy.json exist
  only behind a gate and never change either; a prompt arm never serves tools; static arms have no seals,
  evidence or evolution.
- barrier: this root arrived at every block, and each block started after every root of the repeat group
  finished the previous one (start(b+1) >= max end(b)).

Records read from the learners (learner_common, DESIGN §5): `mechanical-admission.json` carries
`candidate_revision` (arms.revision of the written tree), `admitted_revision` (its id or null) and `revision`
(the record: id, kind, files = the staged relative paths, tier, controls, replay, admitted, ...);
`learning-ledger.jsonl` rows carry kind note | revision | bundle, action, id, admitted, t (the round), files.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

MODEL = 'accounts/fireworks/models/glm-5p3-flash'
UPSTREAM = 'https://api.fireworks.ai/inference/v1/responses'
CONTROLLER_RECORD_FIELDS = ('origin', 'kind', 'parent', 'codex_request', 'codex_input_sha256', 'forwarded')
CONTROLLER_SUMMARY_FIELDS = ('codex_requests', 'controller_calls', 'inserts_by_reason', 'gate_rounds', 'enforce_requeries',
                             'submits', 'controller_errors')
# v9 names kept for tests and callers that still import them
V9_RECORD_FIELDS, V9_SUMMARY_FIELDS = CONTROLLER_RECORD_FIELDS, CONTROLLER_SUMMARY_FIELDS
CAPS = ('max_output_tokens', 'max_tokens', 'max_completion_tokens', 'thinking', 'previous_response_id')
RUNTIME_KEYS = ('manifest_sha256', 'tree_sha256', 'python_sha256', 'rg_sha256')
SUBMIT_HEADING = '# Submitting your answer'
CONVENTIONS_HEADING = '# Task conventions'
REVISION_KINDS = ('revision', 'bundle')
FIXED_FROM_W0 = ('SEED.md', 'controls.json')       # never changed by a v10 learner (DESIGN §5)
GATE_COUNTERS = ('submits', 'gate_rounds', 'enforce_requeries', 'submit_rejected', 'submit_invalid')


def _read(path):
    return json.loads(Path(path).read_text())


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _tools_snapshot(adapter: Path) -> dict[str, str]:
    """{relative path: sha256} of <adapter>/tools (empty when the adapter has no tools/)."""
    import arms
    return {rel: digest for rel, digest in arms.snapshot(Path(adapter)).items() if rel.startswith(arms.TOOLS_DIR + '/')}


def code_root_findings(marker: dict, adapter: Path | None, root: Path, arm) -> list[tuple[str, bool, str]]:
    """The served-tools checks of one episode marker (DESIGN §3 neutral code root; §6-6): (name, passed,
    detail) rows. Pure: reads only the marker, the served adapter and the granted code files."""
    import arms
    import campaign
    rows = []
    kit_meta = marker.get('kit') or {}
    roots = [Path(p) for p in (kit_meta.get('code_roots') or [])]
    tools = {k: v for k, v in (kit_meta.get('tools') or {}).items() if k != 'submit'}
    code_files = ((kit_meta.get('code') or {}).get('files') or {})
    if not arm.l1_tools:
        rows.append(('no code root, code files or tool wrappers without adapter tools',
                     not roots and not code_files and not tools, f'roots {roots} files {sorted(code_files)[:3]} tools {sorted(tools)}'))
        return rows
    if len(roots) != 1:
        rows.append(('exactly one code root', False, f'{roots}'))
        return rows
    code_root = roots[0]
    root = Path(root).resolve()
    try:
        relative = code_root.resolve().relative_to(root)
        under_runs_code = relative.parts[:2] == ('runs', campaign.CODE_DIR) and relative.name == arms.TOOLS_DIR \
            and len(relative.parts) == 4
    except ValueError:
        under_runs_code = False
    rows.append(('code root under <root>/runs/code/<revision>/tools', under_runs_code, str(code_root)))
    try:
        visible = code_root.resolve().relative_to(root.parent)
    except ValueError:
        visible = code_root
    rows.append(('code root path names no arm (model-visible)', not arms.revealing_fragments(str(visible), arm), str(visible)))
    rows.append(('every kit tool script under the code root',
                 bool(tools) and all(str(v.get('script', '')).startswith(str(code_root) + '/') for v in tools.values()),
                 json.dumps({k: v.get('script') for k, v in tools.items()})))
    rows.append(('manifest tool names reveal no arm', not any(arms.revealing_fragments(name) for name in tools),
                 json.dumps(sorted(tools))))
    # review fix: the marker must carry both fields (a marker that omits them is not a pass)
    rows.append(('marker code_sha12 is the code root revision segment',
                 (marker.get('extra') or {}).get('code_sha12') == code_root.parent.name
                 and ((marker.get('extra') or {}).get('code_root') == str(code_root)),
                 json.dumps({k: (marker.get('extra') or {}).get(k) for k in ('code_root', 'code_sha12')})))
    if adapter is None:
        rows.append(('served adapter recorded for the code root', False, 'no adapter in the marker'))
        return rows
    adapter = Path(adapter)
    served = _tools_snapshot(adapter)
    granted = {}
    for path, digest in code_files.items():
        try:
            rel = Path(path).resolve().relative_to(code_root.resolve()).as_posix()
        except ValueError:
            rel = None
        granted[path] = (rel, digest)
    rows.append(('every granted code file lives in the code root', bool(granted) and all(rel is not None for rel, _ in granted.values()),
                 json.dumps([p for p, (rel, _) in granted.items() if rel is None][:3])))
    mismatched = [p for p, (rel, digest) in granted.items() if rel is not None and served.get(f'{arms.TOOLS_DIR}/{rel}') != digest]
    rows.append(('every granted code file sha equals the served revision tools/', bool(granted) and not mismatched,
                 json.dumps(mismatched[:3])))
    rows.append(('tool file names reveal no arm', not any(arms.revealing_fragments(Path(rel).name) for rel in served),
                 json.dumps(sorted(served)[:6])))
    # review fix (DESIGN §6-6): re-derive the kit's code grants from the served revision's manifest applied to
    # the code root (kit._import_closure = harness_core.runner.import_closure) and compare them with the marker
    try:
        from harness_core.runner import import_closure
        manifest = arms.load_manifest(adapter / arms.TOOLS_DIR)
        expected_files, expected_dirs = set(), set()
        for entry in manifest.values():
            closure = import_closure(code_root / entry['script'])
            expected_files.update(str(p) for p in closure)
            if len(closure) > 1:
                expected_dirs.add(str(code_root))
        in_root = {p for p, (rel, _) in granted.items() if rel is not None}
        outside = sorted(p for p, (rel, _) in granted.items() if rel is None)
        submit_ok = not outside or (arm.gate and all(Path(p).name == 'submit.py' or Path(p).parent.name == 'tools' for p in outside))
        rows.append(('granted code files are exactly the import closure of the manifest scripts',
                     in_root == expected_files and submit_ok,
                     json.dumps({'extra': sorted(in_root - expected_files)[:3], 'missing': sorted(expected_files - in_root)[:3],
                                 'outside': outside[:3]})))
        rows.append(('granted code dirs are exactly the tools listing the closure needs',
                     set((kit_meta.get('code') or {}).get('dirs') or []) == expected_dirs,
                     json.dumps({'granted': (kit_meta.get('code') or {}).get('dirs'), 'expected': sorted(expected_dirs)})))
    except (OSError, ValueError, SyntaxError) as exc:
        rows.append(('granted code files are exactly the import closure of the manifest scripts', False, str(exc)))
    try:
        info = arms.adapter_info(adapter)
        expected = arms.revision(adapter)
        rows.append(('code root revision segment is arms.revision(adapter)', code_root.parent.name == expected,
                     f'segment {code_root.parent.name} revision {expected}'))
        rows.append(('adapter.json revision is w0 or arms.revision(tree)',
                     info['revision'] in (arms.W0_REVISION, expected), f'{info["revision"]} vs {expected}'))
    except (OSError, ValueError) as exc:
        rows.append(('served adapter readable', False, str(exc)))
    return rows


def submit_findings(marker: dict, readme: str | None, summary: dict | None, api: Path, arm) -> list[tuple[str, bool, str]]:
    """Submit iff gate (DESIGN §3) and the README sections of one episode: (name, passed, detail) rows."""
    import campaign
    rows = []
    kit_meta = marker.get('kit') or {}
    files = kit_meta.get('files') or {}
    rows.append(('kit submit wrapper iff gate', (kit_meta.get('submit') is not None) == arm.gate == ('bin/submit' in files),
                 f'submit {kit_meta.get("submit") is not None} files {"bin/submit" in files} gate {arm.gate}'))
    if readme is not None:
        rows.append(('README sections match the arm (tools iff l1_tools, submit iff gate, conventions)',
                     (campaign.TOOLS_HEADING + '\n' in readme) == arm.l1_tools and (SUBMIT_HEADING in readme) == arm.gate
                     and CONVENTIONS_HEADING in readme,
                     f'tools {campaign.TOOLS_HEADING + chr(10) in readme} submit {SUBMIT_HEADING in readme}'))
    if arm.controller and not arm.gate:
        counters = (summary or {}).get('controller') or {}
        rows.append(('gate off: no submits, gate rounds, re-queries, check files or submit route',
                     not any((counters.get(k) or 0) for k in GATE_COUNTERS) and not (Path(api) / 'checks').exists()
                     and counters.get('submit_enabled') is not True and counters.get('gate') is not True,
                     json.dumps({k: counters.get(k) for k in (*GATE_COUNTERS, 'submit_enabled', 'gate')})))
    return rows


class Audit:
    def __init__(self, root: Path, code: Path, *, upstream: str | None = UPSTREAM):
        self.root, self.code = Path(root), Path(code)
        self.upstream = upstream
        self.temperature = None                  # the frozen settings' temperature (set in run)
        self.top_p = None                        # the frozen settings' top_p (set in run; None = never sent)
        self.checks: list[dict] = []

    def check(self, name, passed, detail=None):
        entry = {'name': name, 'passed': bool(passed)}
        if detail is not None and not passed:
            entry['detail'] = str(detail)[:500]
        self.checks.append(entry)
        return bool(passed)

    # -- spec -------------------------------------------------------------------------------------
    def spec(self, frozen):
        import arms
        recorded = frozen['campaign']
        config_path = self.code / '.task-paths.json'
        if config_path.is_file():
            spec = arms.parse_config(_read(config_path), root_name=recorded['root'])
            self.check('campaign spec equals the frozen spec', spec.to_json() == recorded)
            return spec
        barrier = recorded.get('barrier')
        config = {'arm': recorded['arm'], 'repeat': recorded['repeat'], 'pack': recorded['pack'],
                  'order_seed': recorded['order_seed']}
        for key in ('subset', 'temperature', 'top_p'):  # recorded only when declared (arms.CampaignSpec.to_json)
            if key in recorded:
                config[key] = recorded[key]
        if barrier:
            config['barrier'] = {'dir': barrier['dir'], 'roots': barrier['roots'], 'timeout_s': barrier['timeout_s'],
                                 'poll_s': barrier['poll_s']}
        return arms.parse_config(config, root_name=recorded['root'])

    # -- groups -----------------------------------------------------------------------------------
    def freeze(self, frozen, spec):
        import arms
        import campaign
        from harness_core import contracts as C
        for name, digest in frozen['files'].items():
            self.check('frozen ' + name, (self.code / name).is_file() and _sha(self.code / name) == digest)
        for name, tree in frozen['trees'].items():
            self.check(f'frozen tree {name}/', campaign.snapshot(self.code / name) == tree)
        self.check('PREREG.md hash recorded before the campaign', bool(frozen.get('prereg_sha256'))
                   and (self.code / 'PREREG.md').is_file() and _sha(self.code / 'PREREG.md') == frozen['prereg_sha256'])
        self.check('codex binary unchanged', Path(frozen['codex']).is_file() and _sha(frozen['codex']) == frozen['codex_sha256'])
        self.check('settings as frozen', campaign.settings_record(spec.temperature, spec.top_p) == frozen['settings'])
        self.check('declared temperature = frozen = what the broker sends',
                   frozen['settings'].get('temperature') == spec.temperature
                   == frozen['settings'].get('broker_temperature') == campaign.broker_temperature(spec.temperature))
        self.check('declared top_p = frozen = what the broker sends (null = not sent)',
                   'top_p' in frozen['settings'] and frozen['settings']['top_p'] == spec.top_p
                   == frozen['settings'].get('broker_top_p') == campaign.broker_top_p(spec.top_p))
        declared = frozen['settings'].get('v10_settings') or frozen['settings'].get('v9_settings') or {}
        self.check('settings: multi-agent off, explicit compaction 244800, permissions text off, temperature recorded',
                   declared.get('features.multi_agent') is False and declared.get('agents.enabled') is False
                   and declared.get('model_auto_compact_token_limit') == 244800
                   and frozen['settings']['permissions_instructions'] is False
                   and frozen['settings']['requery_when_clean'] is False)
        self.check('contract version recorded', frozen.get('contract') == C.CONTRACT_VERSION, frozen.get('contract'))
        w0 = frozen.get('w0')
        if spec.arm.seed == 'none':
            self.check('plain arm has no W0 adapter', w0 is None)
        else:
            self.check('W0 adapter unchanged', w0 is not None and campaign.snapshot(Path(w0['dir'])) == w0['snapshot'])
            self.check('W0 model-visible text within the budget', w0 is not None and w0['model_visible']['within_budget'])
            self.check('W0 matches the arm', w0 is not None and Path(w0['dir']) == arms.w0_dir(spec.arm, self.code / 'adapters'))

    def completion(self, state, frozen, spec):
        from experiment import LEARN_EVERY
        order = frozen['order']
        rows = sorted(state.get('results', []), key=lambda r: r['ordinal'])
        self.check('campaign completed', state.get('status') == 'completed')
        self.check('state arm is the frozen arm', state.get('arm') == asdict(spec.arm))
        self.check(f'one scored episode per question, in this repeat\'s order ({len(order)})',
                   [r['task_id'] for r in rows] == order and [r['ordinal'] for r in rows] == list(range(1, len(order) + 1)))
        import arms
        import data
        base = _read(self.root / 'prepared/manifest.json')['task_ids']
        try:
            expected = arms.campaign_order(base, spec)
        except ValueError as exc:
            expected = f'error: {exc}'
        self.check('order is the seeded permutation of this repeat' + (' (declared subset)' if spec.subset else ''),
                   order == expected)
        if spec.subset is not None:
            try:
                subset = data.check_subset(self.root, spec)
            except (OSError, ValueError, KeyError) as exc:
                subset = {'error': str(exc)}
            self.check('subset: hash of the declared ids, recorded in the prepared manifest, run exactly',
                       (subset or {}).get('sha256') == spec.subset.sha256 == arms.subset_sha256(spec.subset.task_ids)
                       and sorted(order) == sorted(spec.subset.task_ids), subset)
        seal_path = self.root / 'private-evaluation/predictions-seal.json'
        metrics_path = self.root / 'private-evaluation/paired-metrics.json'
        # a completed campaign has sealed and graded (Campaign.finalize): a missing seal or grade file fails
        # this check instead of skipping it
        seal = _read(seal_path) if seal_path.is_file() else {}
        metrics = _read(metrics_path) if metrics_path.is_file() else {}
        self.check('seal and grades cover exactly this root\'s questions',
                   bool(seal) and bool(metrics)
                   and seal.get('total_predictions') == len(order) == metrics.get('denominator')
                   and sorted(r.get('task_id') for r in metrics.get('results', [])) == sorted(order)
                   and (seal.get('subset') or {}).get('sha256') == (spec.subset.sha256 if spec.subset else None)
                   and (metrics.get('subset') or {}).get('sha256') == (spec.subset.sha256 if spec.subset else None))
        self.check('blocks all done', all(state['blocks'].get(str(b), {}).get('status') == 'done'
                                           for b in range(1, len(arms.blocks_of(len(order), LEARN_EVERY)) + 1)))
        return rows

    def episode(self, base: Path, *, label, arm, frozen, summary=None, adapter=None, question=None,
                block_revision=None, status=None, scored=True, w0=None):
        import arms
        import campaign
        import notes_render
        from experiment import ACTOR_SECONDS
        records = base / 'records'
        invocation = _read(records / 'invocation.json') if (records / 'invocation.json').is_file() else {}
        result = _read(records / 'result.json') if (records / 'result.json').is_file() else {}
        self.check(f'{label} invocation.arm == root arm', invocation.get('arm') == asdict(arm))
        runtime = invocation.get('frozen_runtime') or {}
        self.check(f'{label} frozen runtime equals the freeze',
                   all(runtime.get(k) == frozen['frozen_runtime'].get(k) for k in RUNTIME_KEYS))
        self.check(f'{label} kit intact after the run', (result.get('kit_after') or {}).get('intact') is True)
        self.check(f'{label} actor wall clock', invocation.get('timeout_seconds') == ACTOR_SECONDS)
        if self.temperature is not None:
            self.check(f'{label} invocation temperature as frozen', invocation.get('temperature') == self.temperature)
        self.check(f'{label} invocation top_p as frozen', 'top_p' in invocation and invocation['top_p'] == self.top_p)
        settings = invocation.get('settings', {})
        self.check(f'{label} v10 settings', settings.get('features.multi_agent') is False
                   and settings.get('agents.enabled') is False
                   and settings.get('model_auto_compact_token_limit') == 244800
                   and settings.get('include_permissions_instructions') is False
                   and invocation.get('permissions_instructions') is False)
        provider = next((v for k, v in settings.items() if k.startswith('model_providers.')), {})
        self.check(f'{label} retries with accounting, never a cut',
                   (provider.get('request_max_retries') or 0) > 0 and (provider.get('stream_max_retries') or 0) > 0)
        if result.get('agents_md_bytes') is not None:
            self.check(f'{label} AGENTS.md within the byte limit',
                       (settings.get('project_doc_max_bytes') or 0) >= result['agents_md_bytes'])
        if summary is not None:
            self.check(f'{label} prediction unchanged', (base / 'prediction.json').is_file()
                       and _sha(base / 'prediction.json') == summary.get('prediction_sha256'))
        marker = _read(base / 'workspace/.acting-prepared.json') if (base / 'workspace/.acting-prepared.json').is_file() else {}
        extra = marker.get('extra') or {}
        if adapter is None and not scored:
            adapter = extra.get('adapter')           # verification: the candidate the learner ran it under
        if question is None and (base / 'task.json').is_file():
            question = _read(base / 'task.json').get('question')
        workspace = base / 'workspace'
        if (workspace / 'AGENTS.md').is_file() and question is not None:
            try:
                expected_agents = notes_render.compose_agents(
                    campaign.COMMON, Path(adapter) if adapter else None, question)[0].rstrip() + '\n'
            except (OSError, ValueError) as exc:
                expected_agents = f'error: {exc}'
            actual_agents = (workspace / 'AGENTS.md').read_text()
            self.check(f'{label} AGENTS.md re-derives from COMMON + adapter + chosen notes',
                       actual_agents == expected_agents and marker.get('agents_sha256') == _sha(workspace / 'AGENTS.md'))
        else:
            self.check(f'{label} AGENTS.md and question present', False)
        readme = (workspace / 'README.md').read_text() if (workspace / 'README.md').is_file() else None
        if readme is not None:
            self.check(f'{label} README.md hash recorded',
                       hashlib.sha256(readme.encode('utf-8')).hexdigest() == extra.get('readme_sha256'))
        else:
            self.check(f'{label} README.md present', False)
        episode_summary = _read(base / 'summary.json') if (base / 'summary.json').is_file() else summary
        for name, passed, detail in submit_findings(marker, readme, episode_summary, base / 'api', arm):
            self.check(f'{label} {name}', passed, detail)
        for name, passed, detail in code_root_findings(marker, Path(adapter) if adapter else None, self.root, arm):
            self.check(f'{label} {name}', passed, detail)
        if arm.seed == 'harness' and adapter is not None and w0 is not None:
            self.check(f'{label} served controls.json equals the W0 (no control evolution this round)',
                       (Path(adapter) / 'controls.json').is_file() and (Path(w0) / 'controls.json').is_file()
                       and (Path(adapter) / 'controls.json').read_bytes() == (Path(w0) / 'controls.json').read_bytes())
        # every episode, scored or verification (review fix: a verification cwd is model-visible too)
        shown = Path(marker.get('workspace') or workspace)
        try:
            visible = shown.relative_to(self.root.resolve().parent)
        except ValueError:
            visible = shown                      # moved root: check the whole recorded path
        self.check(f'{label} workspace path names no arm (model-visible)',
                   not arms.revealing_fragments(str(visible), arm), f'{visible}')
        if not scored:
            self.check(f'{label} verification workspace under runs/verify with an opaque name',
                       'verify' in visible.parts and 'evolution' not in visible.parts
                       and not any(part.startswith('q0') or part.startswith('q1') for part in visible.parts), f'{visible}')
        served = extra.get('notes')
        self.check(f'{label} served notes recorded and at most {3}', isinstance(served, list) and len(served) <= 3)
        if adapter is not None and question is not None and isinstance(served, list):
            try:
                expected = notes_render.choose_ids(Path(adapter), question)
            except (OSError, ValueError) as exc:
                expected = f'error: {exc}'
            self.check(f'{label} served notes re-derive from the adapter and question', served == expected,
                       f'served {served} expected {expected}')
        if block_revision is not None:
            self.check(f'{label} adapter revision is the one served in its block', extra.get('adapter_revision') == block_revision)
        if adapter is not None:
            try:
                self.check(f'{label} adapter revision is arms.revision(tree)',
                           extra.get('adapter_revision') == arms.revision(Path(adapter)),
                           f'{extra.get("adapter_revision")} vs {arms.revision(Path(adapter))}')
            except (OSError, ValueError) as exc:
                self.check(f'{label} adapter revision is arms.revision(tree)', False, exc)
        self.records(base / 'api', label=label, arm=arm, status=status or (summary or {}).get('status'))

    def records(self, api: Path, *, label, arm, status):
        calls = sorted(p for p in api.glob('[0-9][0-9][0-9]') if p.is_dir())
        metas = {}
        for call in calls:
            request = call / 'request.json'
            if not request.is_file():
                continue
            name = f'{label} api/{call.name}'
            try:
                value = _read(request)
            except ValueError:
                self.check(f'{name} request.json readable', False)
                continue
            # sampling of every body that was sent, including a call still in flight at the actor kill
            if self.temperature is not None:
                self.check(f'{name} temperature as frozen', value.get('temperature') == self.temperature)
            # top_p: the frozen value in every body, or (not declared) no top_p key at all, as v8
            self.check(f'{name} top_p as frozen', ('top_p' in value) == (self.top_p is not None)
                       and value.get('top_p') == self.top_p)
            if not (call / 'transport.json').is_file():
                self.check(f'{name} unfinished at actor kill (usage unknown)', status == 'timeout')
                continue
            meta = _read(call / 'transport.json')
            metas[int(call.name)] = meta
            self.check(f'{name} fixed unbounded model, forwarded reasoning', value.get('model') == MODEL
                       and value.get('reasoning') == (meta.get('native_request_settings') or {}).get('reasoning')
                       and not set(value) & set(CAPS))
            if self.upstream is not None:
                self.check(f'{name} fixed upstream', meta.get('upstream') == self.upstream)
            if not arm.controller:
                self.check(f'{name} no controller record fields (controller=None)', not set(meta) & set(CONTROLLER_RECORD_FIELDS)
                           and not (call / 'controller.json').exists())
        summary = _read(api / 'summary.json') if (api / 'summary.json').is_file() else {}
        limits = summary.get('limits') or {}
        self.check(f'{label} no transport budgets', all(k in limits and limits[k] is None for k in
                   ('requests', 'completion_tokens', 'per_response', 'attempts_per_request', 'request_timeout_seconds')))
        if not arm.controller:
            self.check(f'{label} no controller records (controller=None)',
                       not any((api / n).exists() for n in ('controller.jsonl', 'ledger.jsonl', 'checks'))
                       and not set(summary) & set(CONTROLLER_SUMMARY_FIELDS))
            return
        self.check(f'{label} ledger recorded', (api / 'ledger.jsonl').is_file())
        self.check(f'{label} controller summary fields', set(CONTROLLER_SUMMARY_FIELDS) <= set(summary))
        self.check(f'{label} observation coverage recorded (parser mode per invocation)',
                   isinstance(summary.get('observation_coverage'), dict), sorted(summary)[:12])
        if not arm.gate:
            self.check(f'{label} gate off: no enforcement re-query call',
                       not any(m.get('origin') == 'controller' for m in metas.values()))
        for number, meta in metas.items():
            name = f'{label} api/{number:03}'
            self.check(f'{name} origin/kind/codex_input_sha256', meta.get('origin') in ('codex', 'controller')
                       and meta.get('kind') in ('turn', 'compaction', 'resume') and bool(meta.get('codex_input_sha256')))
            if meta.get('origin') == 'controller':
                parent = metas.get(meta.get('parent'))
                self.check(f'{name} controller call has a codex parent', parent is not None and parent.get('origin') == 'codex')
                self.check(f'{name} controller call copies the parent native request settings',
                           parent is not None and meta.get('native_request_settings') == parent.get('native_request_settings'))

    def learning(self, state, frozen, spec, rows):
        import arms
        from experiment import LEARN_EVERY
        order = frozen['order']
        blocks = arms.blocks_of(len(order), LEARN_EVERY)
        rounds = {r['block']: r for r in state.get('learning', [])}
        if spec.arm.learner is None:
            self.check('static arm: no learning rounds', not state.get('learning'))
            self.check('static arm: no seals, evidence or evolution', not any(
                (self.root / d).exists() and any((self.root / d).iterdir())
                for d in ('private-learning-seals', 'evidence', 'evolution')))
            return
        withheld = set(_read(self.root / 'prepared/manifest.json')['learner_policy']['withheld_reference_ids'])
        import campaign
        runs = self.root / 'runs' / campaign.RUNS_DIR
        served = []
        for number, positions in enumerate(blocks, start=1):
            round_ = rounds.get(number)
            if number == len(blocks):
                self.check(f'round {number} skipped (last block)', round_ is not None and round_.get('status') == 'skipped_unused'
                           and not (self.root / 'private-learning-seals' / f'{number:02}.json').exists()
                           and not (self.root / 'evidence' / f'{number:02}').exists())
                continue
            self.check(f'round {number} ran and did not fail', round_ is not None and round_.get('status') not in (None, 'failed'))
            seal_path = self.root / 'private-learning-seals' / f'{number:02}.json'
            if not self.check(f'round {number} sealed', seal_path.is_file()):
                continue
            seal = _read(seal_path)
            ids = [order[p - 1] for p in positions]
            scope = sorted([e['id'] for e in seal['episodes']] + [e['id'] for e in seal.get('withheld_episodes', [])])
            self.check(f'round {number} exact answer scope', scope == sorted(ids))
            self.check(f'round {number} withheld = issue-47 ids of the block',
                       sorted(e['id'] for e in seal.get('withheld_episodes', [])) == sorted(i for i in ids if i in withheld))
            self.check(f'round {number} answers released after sealing',
                       seal['sealed_utc'] <= seal.get('reference_released_utc', ''))
            evidence = self.root / 'evidence' / f'{number:02}'
            for entry in seal['episodes'] + seal.get('withheld_episodes', []):
                original = runs / f'{entry["ordinal"]:03}' / 'prediction.json'
                self.check(f'round {number} sealed prediction {entry["ordinal"]}',
                           original.is_file() and _sha(original) == entry['prediction_sha256'])
            for entry in seal['episodes']:
                reference = evidence / f'{entry["ordinal"]:03}' / 'reference_answer.json'
                self.check(f'round {number} released reference {entry["ordinal"]}',
                           reference.is_file() and _sha(reference) == entry.get('reference_answer_sha256'))
            for entry in seal.get('withheld_episodes', []):
                self.check(f'round {number} withheld reference {entry["ordinal"]} never released',
                           not (evidence / f'{entry["ordinal"]:03}').exists()
                           and not any(p.name == 'reference_answer.json' and _read(p).get('task_id') == entry['id']
                                       for p in evidence.rglob('reference_answer.json')))
            if round_ is not None and round_.get('candidate'):
                served.append((number, Path(round_['candidate'])))
        self.block_adapters(state, rounds, blocks, spec)
        for number, candidate in served:
            try:
                visible = arms.model_visible(candidate, spec.arm)
                ok = visible['within_budget']
            except (OSError, ValueError) as exc:
                ok, visible = False, str(exc)
            self.check(f'round {number} served adapter within the text budget', ok, visible)
        self.learned_text(served, spec)
        self.fixed_from_w0(served, spec)
        self.served_revisions(served, spec)

    def block_adapters(self, state, rounds, blocks, spec):
        """review fix (DESIGN §6-6): the adapter served in block b is the round b-1 candidate when that round
        activated one, else the adapter of block b-1; block 1 serves the W0; every recorded revision equals
        arms.revision(path); every activated round has a mechanical-admission.json naming that revision."""
        import arms
        w0 = arms.w0_dir(spec.arm, self.code / 'adapters')
        for b in range(1, len(blocks) + 1):
            record = state.get('blocks', {}).get(str(b)) or {}
            adapter = (record.get('adapter') or {}).get('path')
            revision = record.get('adapter_revision')
            if b == 1:
                self.check('block 1 served the W0', adapter is not None and Path(adapter) == w0
                           and revision == (record.get('adapter') or {}).get('revision') == arms.revision(w0), adapter)
                continue
            previous = (state['blocks'].get(str(b - 1)) or {}).get('adapter') or {}
            round_ = rounds.get(b - 1) or {}
            if round_.get('activated'):
                expected_path, expected_revision = round_.get('candidate'), round_.get('candidate_revision')
            else:
                expected_path, expected_revision = previous.get('path'), previous.get('revision')
            self.check(f'block {b} adapter is the round {b - 1} candidate (or the block {b - 1} adapter)',
                       adapter is not None and adapter == expected_path, f'{adapter} vs {expected_path}')
            try:
                computed = arms.revision(Path(adapter)) if adapter else None
            except (OSError, ValueError) as exc:
                computed = f'error: {exc}'
            self.check(f'block {b} adapter revision equals the round record and arms.revision(tree)',
                       revision is not None and revision == expected_revision == computed
                       == (record.get('adapter') or {}).get('revision'),
                       f'block {revision} round {expected_revision} tree {computed}')
            if round_.get('activated'):
                admissions = []
                for path in sorted((self.root / 'evolution' / f'{b - 1:02}').rglob('mechanical-admission.json')):
                    try:
                        admissions.append(_read(path).get('candidate_revision'))
                    except ValueError:
                        continue
                self.check(f'round {b - 1} mechanical-admission.json names the activated candidate revision',
                           computed in admissions, f'{computed} vs {admissions}')

    def ledger_rows(self) -> list[dict]:
        rows = []
        ledger = self.root / 'learning-ledger.jsonl'
        if ledger.is_file():
            for line in ledger.read_text().splitlines():
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    self.check('learning-ledger.jsonl readable', False, line[:200])
        return rows

    def released_up_to(self, number: int) -> tuple[list[str], list[str]]:
        references, questions = [], []
        for block in sorted((self.root / 'evidence').glob('[0-9][0-9]')):
            if int(block.name) > number:
                continue
            for path in sorted(block.glob('*/reference_answer.json')):
                references.append(str(_read(path).get('official_answer', '')))
            for path in sorted(block.glob('*/task.json')):
                questions.append(str(_read(path).get('question', '')))
        return references, questions

    @staticmethod
    def _names_note(row: dict, note_id: str) -> bool:
        return (row.get('id') == note_id or note_id in (row.get('notes') or [])
                or (isinstance(row.get('note'), dict) and row['note'].get('id') == note_id))

    def learned_text(self, served, spec):
        """Every learned note served was admitted by a paired re-run round (learning-ledger row kind=note |
        revision | bundle, admitted=true, t <= round, naming the note), and every learned model-visible string
        (notes not in W0, the USAGE.md lines added since W0) passes the pack leak screen and the question-copy
        check against what was released up to that round."""
        import arms
        import notes_render
        import learner_common as LC
        rows = self.ledger_rows()
        w0 = arms.w0_dir(spec.arm, self.code / 'adapters')
        base_notes = {n.id: n for n in notes_render.load_notes(w0)}
        usage_w0 = w0 / arms.TOOLS_DIR / 'USAGE.md'
        base_usage = set(usage_w0.read_text(encoding='utf-8').splitlines()) if usage_w0.is_file() else set()
        pack = arms.load_pack(spec.arm.pack)
        for number, candidate in served:
            try:
                notes = notes_render.load_notes(candidate)
            except (OSError, ValueError) as exc:
                self.check(f'round {number} served notes readable', False, exc)
                continue
            references, questions = self.released_up_to(number)
            learned = [n for n in notes if base_notes.get(n.id) != n]
            for note in learned:
                # review fix: the admitting row must carry this note's text and triggers, not only its id
                admitted = any(r.get('kind') in ('note', *REVISION_KINDS) and r.get('action') in ('add', None)
                               and r.get('admitted') is True and isinstance(r.get('t'), int) and r['t'] <= number
                               and self._names_note(r, note.id)
                               and ' '.join(str(r.get('text') or '').split()) == ' '.join(note.text.split())
                               and sorted(r.get('triggers') or []) == sorted(note.triggers) for r in rows)
                self.check(f'round {number} served note {note.id} admitted by paired re-runs (same text and triggers)', admitted)
            texts = [('note ' + n.id, n.text) for n in learned]
            usage = candidate / arms.TOOLS_DIR / 'USAGE.md'
            if usage.is_file():
                added = [line for line in usage.read_text(encoding='utf-8').splitlines() if line not in base_usage and line.strip()]
                if added:
                    texts.append(('USAGE.md added lines', '\n'.join(added)))
            for what, text in texts:
                findings = list(pack.leak_screen(text, references))
                gram = LC.shared_run(text, questions)
                if gram:
                    findings.append(f'copies released question wording: {gram!r}')
                self.check(f'round {number} served {what} passes the leak screen', not findings, findings)

    def fixed_from_w0(self, served, spec):
        """SEED.md and controls.json are fixed in every evolve arm this round (DESIGN §5); rules/ and
        policy.json exist only behind a gate and never change either (no rule evolution in v10)."""
        import arms
        import campaign
        w0 = arms.w0_dir(spec.arm, self.code / 'adapters')
        for number, candidate in served:
            for name in FIXED_FROM_W0:
                base, now = w0 / name, Path(candidate) / name
                if base.is_file() or now.is_file():
                    self.check(f'round {number} served {name} equals the W0 (fixed this round)',
                               base.is_file() and now.is_file() and base.read_bytes() == now.read_bytes())
            for name in ('rules', 'policy.json'):
                base, now = w0 / name, Path(candidate) / name
                if base.exists() or now.exists():
                    same = (base.is_dir() and now.is_dir() and campaign.snapshot(base) == campaign.snapshot(now)) or (
                        base.is_file() and now.is_file() and base.read_bytes() == now.read_bytes())
                    self.check(f'round {number} served {name} equals the W0 (no rule/policy evolution in v10)',
                               same and spec.arm.gate)

    def served_revisions(self, served, spec):
        """Every served revision whose tools/ differs from the revision served before it (W0 for the first
        round) was admitted by that round: a learning-ledger row of kind revision | bundle with admitted=true,
        and a mechanical-admission.json under evolution/NN/ whose admitted revision staged the changed files and
        whose candidate_revision is the served tree (DESIGN §6-6); it passes check_adapter. A prompt-architecture
        arm never serves tools."""
        import arms
        w0 = arms.w0_dir(spec.arm, self.code / 'adapters')
        rows = self.ledger_rows()
        previous = _tools_snapshot(w0) if spec.arm.seed == 'harness' else {}
        for number, candidate in served:
            now = _tools_snapshot(Path(candidate))
            if spec.arm.seed != 'harness':
                self.check(f'round {number} prompt adapter serves no tools', not now)
                continue
            changed = sorted(rel for rel in set(previous) | set(now) if previous.get(rel) != now.get(rel))
            if not changed:
                previous = now
                continue
            self.check(f'round {number} tool revision served only by the harness learner', spec.arm.learner == 'harness', changed)
            admitted_row = any(r.get('kind') in REVISION_KINDS and r.get('admitted') is True and r.get('action') != 'void'
                               and isinstance(r.get('t'), int) and r['t'] <= number for r in rows)
            self.check(f'round {number} served tool revision admitted by paired re-runs (ledger row)', admitted_row, changed)
            recorded, candidate_revisions = set(), set()
            for path in sorted((self.root / 'evolution' / f'{number:02}').rglob('mechanical-admission.json')):
                try:
                    value = _read(path)
                except ValueError:
                    self.check(f'{path.relative_to(self.root)} readable', False)
                    continue
                record = value.get('revision') or {}
                if value.get('admitted_revision') and record.get('admitted') is True:
                    recorded.update(record.get('files') or [])
                    candidate_revisions.add(value.get('candidate_revision'))
            self.check(f'round {number} served tool files are the admitted revision\'s staged files',
                       bool(recorded) and set(changed) <= recorded, f'changed {changed[:4]} recorded {sorted(recorded)[:4]}')
            self.check(f'round {number} admission record names the served candidate revision',
                       arms.revision(Path(candidate)) in candidate_revisions,
                       f'{arms.revision(Path(candidate))} vs {sorted(map(str, candidate_revisions))}')
            try:
                self.check(f'round {number} served tools pass check_adapter', bool(arms.check_adapter(Path(candidate), spec.arm)))
            except (OSError, ValueError) as exc:
                self.check(f'round {number} served tools pass check_adapter', False, exc)
            previous = now

    def verification(self, arm, frozen, w0):
        """Every verification episode (any depth under evolution/BB/verify/, e.g. verify/<note>/q003-k1)."""
        import campaign
        verify = self.root / 'runs' / campaign.VERIFY_DIR
        count = 0
        for invocation in sorted(verify.glob('*/**/records/invocation.json')):
            base = invocation.parent.parent
            summary = _read(base / 'summary.json') if (base / 'summary.json').is_file() else None
            self.check(f'verify {base.relative_to(self.root)} summary recorded', summary is not None)
            self.episode(base, label=f'verify {base.relative_to(self.root)}', arm=arm, frozen=frozen, summary=summary,
                         scored=False, w0=w0)
            count += 1
        started = sorted(p for p in verify.glob('*/**/workspace') if p.is_dir())
        self.check(f'every verification workspace has records ({count}/{len(started)})', count == len(started))
        self.check('no verification episode lives under evolution/',
                   not list((self.root / 'evolution').glob('*/**/records/invocation.json')))

    def barrier(self, state, spec):
        if spec.barrier is None:
            self.check('no repeat group: no barrier', True)
            return
        from barrier import FileBarrier
        group = FileBarrier(spec.barrier.dir, spec.barrier.roots, spec.root, timeout_s=spec.barrier.timeout_s)
        order = _read(spec.barrier.dir / 'order.json') if (spec.barrier.dir / 'order.json').is_file() else {}
        self.check('barrier order.json is this root\'s order', order.get('order_sha256') == state.get('order_sha256'))
        blocks = sorted(int(b) for b in state['blocks'])
        for b in blocks:
            self.check(f'barrier block {b:02} arrival recorded', group.done_path(b).is_file())
        for b in blocks:
            previous = group.done(b - 1)
            start = state['blocks'][str(b)].get('start_unix')
            complete = len(previous) == len(spec.barrier.roots)
            latest = max((v.get('end_unix') or 0) for v in previous.values()) if previous else None
            self.check(f'barrier block {b:02} started after every root finished block {b - 1:02}',
                       complete and start is not None and start >= latest,
                       f'start {start} latest end {latest} arrived {sorted(previous)}')

    # -- entry ------------------------------------------------------------------------------------
    def run(self) -> dict:
        frozen = _read(self.root / 'freeze.json')
        state = _read(self.root / 'state.json')
        self.temperature = (frozen.get('settings') or {}).get('temperature')
        self.top_p = (frozen.get('settings') or {}).get('top_p')
        spec = self.spec(frozen)
        arm = spec.arm
        self.freeze(frozen, spec)
        rows = self.completion(state, frozen, spec)
        order = frozen['order']
        # the adapter served in each block (state blocks record it)
        tasks = {t['task_id']: t for t in (json.loads(line) for line in
                 (self.root / 'prepared/agent/tasks.jsonl').read_text().splitlines() if line.strip())}
        from experiment import LEARN_EVERY
        import arms
        import campaign
        w0 = arms.w0_dir(arm, self.code / 'adapters')
        blocks = arms.blocks_of(len(order), LEARN_EVERY)
        block_of = {p: b for b, positions in enumerate(blocks, start=1) for p in positions}
        for row in rows:
            record = state['blocks'].get(str(block_of.get(row['ordinal'])), {})
            adapter = (record.get('adapter') or {}).get('path')
            self.episode(self.root / 'runs' / campaign.RUNS_DIR / f'{row["ordinal"]:03}', label=f'{arm.name}/{row["ordinal"]:03}',
                         arm=arm, frozen=frozen, summary=row, adapter=adapter,
                         question=tasks.get(row['task_id'], {}).get('question'),
                         block_revision=record.get('adapter_revision'), w0=w0)
        self.learning(state, frozen, spec, rows)
        self.verification(arm, frozen, w0)
        self.barrier(state, spec)
        counts = Counter('passed' if c['passed'] else 'failed' for c in self.checks)
        return {'created_utc': datetime.now(timezone.utc).isoformat(), 'passed': not counts['failed'],
                'profile': arms.PROFILE, 'arm': asdict(arm), 'repeat': spec.repeat, 'counts': dict(counts),
                'failed': [c for c in self.checks if not c['passed']], 'checks': self.checks,
                'scope': 'Saved source/request/seal/admission/barrier consistency; OS enforcement is separately '
                         'tested (tests/test_v10_kit.py); no independent network attestation.'}


def build(root, *, code=None, upstream=UPSTREAM, write=True):
    root = Path(root)
    result = Audit(root, Path(code) if code else root, upstream=upstream).run()
    if write:
        path = root / 'preflight/final-audit.json'
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(result, indent=2) + '\n')
        return {'passed': result['passed'], 'counts': result['counts'], 'path': str(path)}
    return result


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--root', type=Path, required=True)
    result = build(p.parse_args().root)
    print(json.dumps(result))
    raise SystemExit(0 if result['passed'] else 1)
