"""Saved-artifact consistency audit of one v9 root (DESIGN §4; engineering review #5, #7-#9); no model calls.

The arm comes from the root's own records (freeze.json "campaign", checked against .task-paths.json when
present); nothing is inferred from a substring. Checks, by group:

- freeze: code, PROTOCOL/PREREG, harness_core/, packs/, adapters/, codex and settings unchanged; PREREG
  hash recorded; the W0 snapshot unchanged; the seed-parity (model-visible bytes) record within budget.
- completion: status completed; exactly one scored episode per question of this repeat, in its order.
- episodes (scored and verification): invocation.arm == the root's arm; the frozen runtime equals the
  freeze record; kit_after.intact; v9 settings as declared; wall clock; retries; AGENTS.md size;
  prediction unchanged; the served notes re-derive from (adapter, question) and are at most
  NOTES_PER_EPISODE; the adapter revision is the one served in that block.
- records: every api call has the fixed model, forwarded reasoning, no output caps, the fixed upstream;
  controller=None arms carry none of the v9 record fields (byte-identical v8 layout); controller arms have
  origin/kind/codex_input_sha256 on every call, every controller call has a codex parent and copies its
  native request settings, and a ledger.
- learning: learner arms have a round for every block but the last (never 'failed'), seals before
  release, exact scope (released + withheld = the block), no withheld reference in evidence, every served
  adapter within the text budget, every learned rule served admitted (mechanical-admission.json with
  replay numbers); static arms have no seals, evidence or evolution.
- model-visible composition (review S6): AGENTS.md re-derives exactly from COMMON + the served adapter +
  the chosen notes; README.md carries the L1 tools section iff l1_tools and the submit section iff
  controller; the scored workspace path (from the root's parent) names no arm; every request carries the
  frozen temperature.
- learned text (review S6): every learned note served was admitted by a paired re-run round (a
  `learning-ledger.jsonl` row kind=note, action=add, admitted=true, t <= the round); learned notes and
  changed rule templates pass the pack leak screen and the question-copy check against the references
  and questions released up to that round.
- barrier: this root arrived at every block, and each block started after every root of the repeat group
  finished the previous one (start(b+1) >= max end(b)).
- wave 3 (pilot): a declared subset is the prepared manifest's (same hash), the order is the subset in this
  repeat's seeded order, and the seal and the grades cover exactly it; the declared temperature is the
  frozen one, the broker's, every invocation's and every request's; so is the declared top_p (absent =
  frozen null, and no request body carries a top_p key).
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
V9_RECORD_FIELDS = ('origin', 'kind', 'parent', 'codex_request', 'codex_input_sha256', 'forwarded')
V9_SUMMARY_FIELDS = ('codex_requests', 'controller_calls', 'inserts_by_reason', 'gate_rounds', 'enforce_requeries',
                     'submits', 'controller_errors')
CAPS = ('max_output_tokens', 'max_tokens', 'max_completion_tokens', 'thinking', 'previous_response_id')
RUNTIME_KEYS = ('manifest_sha256', 'tree_sha256', 'python_sha256', 'rg_sha256')


def _read(path):
    return json.loads(Path(path).read_text())


def _default_policy():
    from harness_core import contracts as C
    return dict(C.POLICY_DEFAULTS)


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _admission_matches(record, before, after) -> bool:
    """A served rule change is covered by its admission record (mechanism_learning modes; 'full' when absent)."""
    import mechanism_learning as ML

    def bare(rule):
        return {k: v for k, v in (rule or {}).items() if k != 'stats'}
    mode = record.get('mode', 'full')
    if mode == 'priority':
        return (before is not None and record.get('admitted') is True
                and bare(after) == {**bare(before), 'priority': after.get('priority')})
    if mode == 'disable':
        precision = record.get('precision') or {}
        n_right, rate = precision.get('n_right'), precision.get('right_fire_rate')
        return (before is not None and bare(after) == {**bare(before), 'enabled': False}
                and isinstance(n_right, int) and n_right >= ML.MIN_RIGHT
                and isinstance(rate, (int, float)) and rate > ML.MAX_RIGHT_FIRE)
    # S8 review: a full admission needs a counterfactual target that admitted the rule and a precision record
    # that did not fail (a recorded but failed verdict is not an admission).
    counterfactual, precision = record.get('counterfactual') or [], record.get('precision') or {}
    return (record.get('admitted') is not False and bool(precision) and precision.get('ok') is not False
            and any(isinstance(r, dict) and r.get('admit') is True for r in counterfactual))


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
        for name, digest in frozen['files'].items():
            self.check('frozen ' + name, (self.code / name).is_file() and _sha(self.code / name) == digest)
        for name, tree in frozen['trees'].items():
            self.check(f'frozen tree {name}/', campaign.snapshot(self.code / name) == tree)
        self.check('PREREG.md hash recorded before the campaign', bool(frozen.get('prereg_sha256'))
                   and (self.code / 'PREREG.md').is_file() and _sha(self.code / 'PREREG.md') == frozen['prereg_sha256'])
        self.check('codex binary unchanged', Path(frozen['codex']).is_file() and _sha(frozen['codex']) == frozen['codex_sha256'])
        self.check('v9 settings as frozen', campaign.settings_record(spec.temperature, spec.top_p) == frozen['settings'])
        self.check('declared temperature = frozen = what the broker sends',
                   frozen['settings'].get('temperature') == spec.temperature
                   == frozen['settings'].get('broker_temperature') == campaign.broker_temperature(spec.temperature))
        self.check('declared top_p = frozen = what the broker sends (null = not sent)',
                   'top_p' in frozen['settings'] and frozen['settings']['top_p'] == spec.top_p
                   == frozen['settings'].get('broker_top_p') == campaign.broker_top_p(spec.top_p))
        self.check('settings: multi-agent off, explicit compaction 244800, permissions text off, temperature recorded',
                   frozen['settings']['v9_settings'].get('features.multi_agent') is False
                   and frozen['settings']['v9_settings'].get('agents.enabled') is False
                   and frozen['settings']['v9_settings'].get('model_auto_compact_token_limit') == 244800
                   and frozen['settings']['permissions_instructions'] is False
                   and frozen['settings']['requery_when_clean'] is False)
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
                block_revision=None, status=None, scored=True):
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
        self.check(f'{label} v9 settings', settings.get('features.multi_agent') is False
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
        if (workspace / 'README.md').is_file():
            readme = (workspace / 'README.md').read_text()
            self.check(f'{label} README sections match the arm (tools iff l1, submit iff controller)',
                       ('# Table and calculation tools' in readme) == arm.l1_tools
                       and ('# Submitting your answer' in readme) == arm.controller
                       and '# Task conventions' in readme
                       and hashlib.sha256(readme.encode('utf-8')).hexdigest() == extra.get('readme_sha256'))
        else:
            self.check(f'{label} README.md present', False)
        if scored:
            shown = Path(marker.get('workspace') or workspace)
            try:
                visible = shown.relative_to(self.root.resolve().parent)
            except ValueError:
                visible = shown                      # moved root: check the whole recorded path
            self.check(f'{label} workspace path names no arm (model-visible)',
                       not arms.revealing_fragments(str(visible), arm), f'{visible}')
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
                self.check(f'{name} no v9 record fields (controller=None)', not set(meta) & set(V9_RECORD_FIELDS)
                           and not (call / 'controller.json').exists())
        summary = _read(api / 'summary.json') if (api / 'summary.json').is_file() else {}
        limits = summary.get('limits') or {}
        self.check(f'{label} no transport budgets', all(k in limits and limits[k] is None for k in
                   ('requests', 'completion_tokens', 'per_response', 'attempts_per_request', 'request_timeout_seconds')))
        if not arm.controller:
            self.check(f'{label} no controller records (controller=None)',
                       not any((api / n).exists() for n in ('controller.jsonl', 'ledger.jsonl', 'checks'))
                       and not set(summary) & set(V9_SUMMARY_FIELDS))
            return
        self.check(f'{label} ledger recorded', (api / 'ledger.jsonl').is_file())
        self.check(f'{label} controller summary fields', set(V9_SUMMARY_FIELDS) <= set(summary))
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
        for number, candidate in served:
            try:
                visible = arms.model_visible(candidate, spec.arm)
                ok = visible['within_budget']
            except (OSError, ValueError) as exc:
                ok, visible = False, str(exc)
            self.check(f'round {number} served adapter within the text budget', ok, visible)
        self.learned_text(served, spec)
        if spec.arm.controller:
            self.admitted_rules(served, spec)

    def learned_text(self, served, spec):
        """Both evolve arms under one standard: every learned note served was admitted by a paired re-run
        round (S5 learning-ledger row kind=note, action=add, admitted=true, t <= round), and every learned
        model-visible string (notes not in W0, finding templates of rules changed from W0) passes the pack
        leak screen and the question-copy check against what was released up to that round."""
        import arms
        import notes_render
        from harness_core import rules as R
        import learner_common as LC
        rows = []
        ledger = self.root / 'learning-ledger.jsonl'
        if ledger.is_file():
            for line in ledger.read_text().splitlines():
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    self.check('learning-ledger.jsonl readable', False, line[:200])
        w0 = arms.w0_dir(spec.arm, self.code / 'adapters')
        base_notes = {n.id: n for n in notes_render.load_notes(w0)}
        base_rules = {r.id: R.rule_to_dict(r) for r in arms.load_adapter_rules(w0)} if spec.arm.controller else {}
        pack = arms.load_pack(spec.arm.pack)
        for number, candidate in served:
            try:
                notes = notes_render.load_notes(candidate)
                rules = arms.load_adapter_rules(candidate) if spec.arm.controller else []
            except (OSError, ValueError) as exc:
                self.check(f'round {number} served notes/rules readable', False, exc)
                continue
            references, questions = [], []
            for block in sorted((self.root / 'evidence').glob('[0-9][0-9]')):
                if int(block.name) > number:
                    continue
                for path in sorted(block.glob('*/reference_answer.json')):
                    references.append(str(_read(path).get('official_answer', '')))
                for path in sorted(block.glob('*/task.json')):
                    questions.append(str(_read(path).get('question', '')))
            learned = [n for n in notes if base_notes.get(n.id) != n]
            for note in learned:
                admitted = any(r.get('kind') == 'note' and r.get('action') == 'add' and r.get('admitted') is True
                               and r.get('id') == note.id and isinstance(r.get('t'), int) and r['t'] <= number
                               for r in rows)
                self.check(f'round {number} served note {note.id} admitted by paired re-runs', admitted)
            texts = [('note ' + n.id, n.text) for n in learned]
            texts += [('rule ' + r.id, r.finding) for r in rules if base_rules.get(r.id) != R.rule_to_dict(r)]
            for what, text in texts:
                findings = list(pack.leak_screen(text, references))
                gram = LC.shared_run(text, questions)
                if gram:
                    findings.append(f'copies released question wording: {gram!r}')
                self.check(f'round {number} served {what} passes the leak screen', not findings, findings)

    def admitted_rules(self, served, spec):
        """Every rule a round serves that differs from the revision served before it (W0 for the first round:
        new, modified, disabled) was admitted by that round (evolution/NN/**/mechanical-admission.json
        `admitted_rules`), with the record of its admission mode (S5 mechanism_learning.modify_rule):
        'full' (new rules and every other modification) -> counterfactual + precision replay numbers;
        'priority' -> the rule differs from its predecessor only in priority (and stats); 'disable' -> the
        predecessor with enabled=false, admitted on a measured right-episode fire rate above MAX_RIGHT_FIRE on
        at least MIN_RIGHT right episodes. Every served policy value that differs from the previous revision
        has a `policy_replay` record of that round (S5 learner_common admission format)."""
        import arms
        from harness_core import rules as R
        w0 = arms.w0_dir(spec.arm, self.code / 'adapters')
        base_ids = {r.id for r in arms.load_adapter_rules(w0)}
        previous_rules = {r.id: R.rule_to_dict(r) for r in arms.load_adapter_rules(w0)}
        previous_policy = {**_default_policy(), **arms.load_policy(w0)}
        for position, (number, candidate) in enumerate(served):
            admitted, policy = {}, {}
            for path in sorted((self.root / 'evolution' / f'{number:02}').rglob('mechanical-admission.json')):
                try:
                    value = _read(path)
                except ValueError:
                    self.check(f'{path.relative_to(self.root)} readable', False)
                    continue
                numbers = value.get('rule_admission') or {}
                for rule_id in value.get('admitted_rules') or []:
                    admitted[rule_id] = numbers.get(rule_id) or {}
                for key in value.get('policy_changes') or {}:
                    policy[key] = (value.get('policy_replay') or {}).get(key) or {}
            try:
                rules = {r.id: R.rule_to_dict(r) for r in arms.load_adapter_rules(candidate)}
                served_policy = {**_default_policy(), **arms.load_policy(candidate)}
            except (OSError, ValueError) as exc:
                self.check(f'round {number} served rules readable', False, exc)
                continue
            before_name = 'W0' if position == 0 else 'the previous revision'
            for rule_id, rule in rules.items():
                before = previous_rules.get(rule_id)
                if before == rule:
                    continue
                record = admitted.get(rule_id)
                self.check(f'round {number} served rule {rule_id} (changed from {before_name}) admitted with replay numbers',
                           record is not None and _admission_matches(record, before, rule), record)
            removed = sorted(base_ids - set(rules))
            self.check(f'round {number} no W0 rule file removed (rules are disabled, not deleted)', not removed, removed)
            for key, value in served_policy.items():
                if previous_policy.get(key) != value:
                    self.check(f'round {number} served policy {key} admitted with a replay record', bool(policy.get(key)))
            previous_rules, previous_policy = rules, served_policy

    def verification(self, arm, frozen):
        """Every verification episode (any depth under evolution/BB/verify/, e.g. verify/<note>/q003-k1)."""
        count = 0
        for invocation in sorted((self.root / 'evolution').glob('*/verify/**/records/invocation.json')):
            base = invocation.parent.parent
            summary = _read(base / 'summary.json') if (base / 'summary.json').is_file() else None
            self.check(f'verify {base.relative_to(self.root)} summary recorded', summary is not None)
            self.episode(base, label=f'verify {base.relative_to(self.root)}', arm=arm, frozen=frozen, summary=summary,
                         scored=False)
            count += 1
        started = sorted(p for p in (self.root / 'evolution').glob('*/verify/**/workspace') if p.is_dir())
        self.check(f'every verification workspace has records ({count}/{len(started)})', count == len(started))

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
        blocks = arms.blocks_of(len(order), LEARN_EVERY)
        block_of = {p: b for b, positions in enumerate(blocks, start=1) for p in positions}
        for row in rows:
            record = state['blocks'].get(str(block_of.get(row['ordinal'])), {})
            adapter = (record.get('adapter') or {}).get('path')
            self.episode(self.root / 'runs' / campaign.RUNS_DIR / f'{row["ordinal"]:03}', label=f'{arm.name}/{row["ordinal"]:03}',
                         arm=arm, frozen=frozen, summary=row, adapter=adapter,
                         question=tasks.get(row['task_id'], {}).get('question'),
                         block_revision=record.get('adapter_revision'))
        self.learning(state, frozen, spec, rows)
        self.verification(arm, frozen)
        self.barrier(state, spec)
        counts = Counter('passed' if c['passed'] else 'failed' for c in self.checks)
        return {'created_utc': datetime.now(timezone.utc).isoformat(), 'passed': not counts['failed'],
                'profile': 'v9', 'arm': asdict(arm), 'repeat': spec.repeat, 'counts': dict(counts),
                'failed': [c for c in self.checks if not c['passed']], 'checks': self.checks,
                'scope': 'Saved source/request/seal/admission/barrier consistency; OS enforcement is separately '
                         'tested (tests/test_v9_kit.py); no independent network attestation.'}


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
