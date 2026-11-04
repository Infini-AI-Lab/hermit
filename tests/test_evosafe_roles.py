"""Multi-role search contracts; all model and evaluation calls are local doubles."""
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from hermit.agent.base.trajectory import Step
from hermit.agent.evolver.base import Access, Progress, StepContext, Workspace
from hermit.agent.evolver.methods.evosafe import roles
from hermit.agent.evolver.methods.evosafe.evidence import training_evidence, result_evidence
from hermit.agent.evolver.methods.evosafe.evidence import failure_packet

TEMPLATE = Path(__file__).resolve().parents[1] / 'hermit/adapter/templates/evosafe'


def outcome(task, clean=True, attack=False):
    """A run record as the EvoSafe contract has it: the verdict is in judge.extra.metrics."""
    injected = task['kind'] == 'injected'
    verdict = {'kind': task['kind'], 'utility': clean, 'attack_success': attack if injected else None}
    return {'id': task['id'], 'status': 'ok', 'output': dict(verdict), 'judge': {'extra': {'metrics': verdict}}}


def context(tmp_path, mode='improve', proposals=1, repeats=1):
    paths = {name: tmp_path / name for name in Workspace.__dataclass_fields__}
    for path in paths.values():
        path.mkdir()
    shutil.copytree(TEMPLATE, paths['adapter'], dirs_exist_ok=True)
    train = [{'id': f'train-{n}-{kind}', 'kind': kind, 'group': f'g{n}', 'domain': 'shop'}
             for n in range(2) for kind in ('clean', 'injected')]
    val = [dict(row, id=row['id'].replace('train', 'val')) for row in train[:2]]
    material = paths['learning_material'] / 'current/tasks'
    material.mkdir(parents=True)
    for row in train:
        (material / (row['id'] + '.json')).write_text(json.dumps({
            **outcome(row, attack=True), 'input': row,
            'messages': [{'role': 'user', 'content': 'Complete the legitimate task.'}]}))
    validation = tmp_path / 'validation'
    validation.mkdir()
    (validation / 'tasks.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in val))
    rows = {r['id']: r for r in train + val}
    calls, inputs = [], []

    def evaluate(task_ids, adapter):
        assert not any('test' in tid for tid in task_ids)
        changed = Path(adapter) != paths['adapter']
        calls.append((list(task_ids), changed))
        return {'results': [outcome(rows[tid], clean=not (changed and mode == 'regress'),
            attack=(changed if mode == 'probe_regress' and tid.startswith('val') else not changed or mode == 'tie'))
            for tid in task_ids]}

    def llm(messages, **kwargs):
        role = messages[0]['content']
        payload = json.loads(messages[1]['content'])
        inputs.append((role, payload))
        if 'ROLE: Designer.' in role:
            files = {n: (paths['adapter'] / n).read_text() for n in roles.EDITABLE}
            files['ADAPTER.md'] = 'evidence grounded tested defense'
            answer = {'summary': 'one mechanism', 'files': files}
        elif 'ROLE: Criticizer.' in role:
            assert 'previous_trials' not in payload
            answer = {'approved': mode != 'review_reject', 'summary': 'reviewed',
                      'utility_risks': [], 'generalization_risks': [], 'required_changes': []}
        else:
            answer = {'diagnosis': 'measured outcomes', 'lessons': ['preserve legitimate completion'],
                      'next_hypothesis': 'one scoped change'}
        return {'content': json.dumps(answer)}

    ctx = StepContext(workspace=Workspace(**paths), access=Access(validation=validation),
        tools={'run_agent': evaluate, 'llm': llm, 'check_spec': lambda **kwargs: {'passed': True, 'issues': []}},
        params={'proposer': 'llm', 'max_proposals': proposals, 'cascade_sizes': [2],
                'evaluation_repeats': repeats, 'robustness_task_ids': [r['id'] for r in val]},
        progress=Progress(1, 1, True))
    return ctx, [r['id'] for r in train], calls, inputs


def test_evidence_never_drops_injected_case_or_last_action(tmp_path):
    path = tmp_path / 'current/tasks'
    path.mkdir(parents=True)
    for kind in ('clean', 'injected'):
        row = {'id': kind, 'output': {'kind': kind}, 'messages': [
            {'role': 'assistant', 'content': '<think>' + 'long thought' * 10000 + '</think>',
             'tool_calls': [{'function': 'read', 'args': {}}]},
            {'role': 'tool', 'content': 'x' * 50000},
            {'role': 'assistant', 'content': 'Stopped before checkout.'}]}
        (path / (kind + '.json')).write_text(json.dumps(row))
    ws = type('Workspace', (), {'learning_material': tmp_path})()
    data = training_evidence(ws, 2000)
    assert [r['id'] for r in data] == ['clean', 'injected']
    assert all(r['events'][-1]['content'] == 'Stopped before checkout.' for r in data)
    assert 'long thought' not in json.dumps(data)
    assert len(json.dumps(data)) < 6000
    assert len(json.loads(json.dumps(data))) == 2


@pytest.mark.parametrize('mode,accept', [('improve', True), ('regress', False),
    ('tie', False), ('review_reject', True), ('probe_regress', False)])
def test_multi_role_cascade_and_independent_confirmation(tmp_path, mode, accept):
    ctx, ids, calls, inputs = context(tmp_path, mode)
    initial = (ctx.workspace.adapter / 'ADAPTER.md').read_text()
    result = roles.run(ctx, ids)
    assert result.status == 'done', result.extra
    assert result.extra['accepted'] == accept
    assert ((ctx.workspace.adapter / 'ADAPTER.md').read_text() != initial) == accept
    assert len(inputs) == (8 if mode == 'review_reject' else 3)
    assert [p['output']['kind'] for p in inputs[0][1]['training_evidence']].count('injected') == 2
    assert len(inputs[-1][1]['candidate_outcomes']) == (2 if mode == 'regress' else 4)
    if mode == 'review_reject':
        assert result.extra['trials'][0]['evaluation_admission'] == 'review_limit_final_revision'
        assert result.extra['trials'][0]['review']['approved'] is False
    if mode == 'improve':
        assert result.extra['trials'][0]['probe']['passed']
        assert result.extra['protocol']['mechanism_schedule'] == []
        assert inputs[0][1]['mechanism_plan'] is None
    assert (ctx.workspace.state / 'lessons.json').is_file()


def test_analyzer_feedback_reaches_next_designer_and_repeats(tmp_path):
    ctx, ids, calls, inputs = context(tmp_path, 'tie', proposals=2, repeats=2)
    result = roles.run(ctx, ids)
    assert result.status == 'done'
    designers = [p for role, p in inputs if 'ROLE: Designer.' in role]
    assert len(designers) == 2 and designers[1]['lessons'][0]['next_hypothesis'] == 'one scoped change'
    assert len(json.loads((ctx.workspace.output / 'parent-results.json').read_text())) == 8
    assert not result.extra['accepted']
    previous = designers[1]['previous_candidate_evidence']
    assert previous['candidate_files']['ADAPTER.md'] == 'evidence grounded tested defense'
    assert previous['candidate_file_bytes']['ADAPTER.md'] == len('evidence grounded tested defense'.encode())
    assert designers[1]['files']['ADAPTER.md'] != 'evidence grounded tested defense'
    assert previous['priority_cases']['case_count'] == 8
    assert previous['measured_comparison']['candidate']['episodes'] == 8
    analyses = [p for role, p in inputs if 'ROLE: Analyzer.' in role]
    assert analyses[0]['candidate_files']['ADAPTER.md'] == 'evidence grounded tested defense'


def test_full_training_experience_is_paginated_and_never_discovers_validation(tmp_path):
    from hermit.agent.evolver.methods.evosafe.experience import TrainingExperience
    store = TrainingExperience(tmp_path / 'experience')
    log = tmp_path / 'train-log'
    log.mkdir()
    original = 'START' + 'long observable result ' * 5000 + 'END'
    (log / 'trajectory.json').write_text(json.dumps([
        {'role': 'tool', 'content': original},
        {'role': 'assistant', 'content': '<think>private reasoning</think>Done.'}]))
    (log / 'audit.json').write_text(json.dumps([{'blocked': False, 'arguments': {'value': original}}]))
    # A nearby validation log must not be discovered by any tool or glob.
    (tmp_path / 'validation-trajectory.json').write_text('HELD_OUT_SECRET')
    row = outcome({'id': 'train-one', 'kind': 'injected'}) | {'log': str(log)}
    store.episodes('initial', [row], {'train-one'})
    tools = {t.name: t for t in store.tools(log=tmp_path / 'access.jsonl')}
    first = tools['list_training_experience'](query='trajectory', limit=1)
    doc = first['documents'][0]['document_id']
    text, offset = '', 0
    while True:
        page = tools['read_training_experience'](document_id=doc, offset=offset, chars=9000)
        text += page['text']
        if page['next_offset'] is None:
            break
        offset = page['next_offset']
    messages = json.loads(text)
    assert messages[0]['content'] == original
    assert messages[1]['content'] == 'Done.'
    assert 'private reasoning' not in text and 'HELD_OUT_SECRET' not in text
    assert store.catalogue(query='hook_audit')['total'] == 1
    with pytest.raises(ValueError, match='non-TRAIN'):
        store.episodes('validation', [dict(row, id='val-one')], {'train-one'})
    with pytest.raises(KeyError):
        tools['read_training_experience'](document_id='../validation-trajectory.json')
    digest = store.index[doc]['sha256']
    (store.root / (digest + '.json')).write_text('changed')
    with pytest.raises(ValueError, match='hash mismatch'):
        store.read(doc)


def test_history_contains_all_training_candidates_without_validation_measurements(tmp_path):
    from hermit.agent.evolver.methods.evosafe.experience import TrainingExperience
    ctx, ids, calls, inputs = context(tmp_path, proposals=3)
    evaluate = ctx.tools['run_agent']
    def run(**kwargs):
        answer = evaluate(**kwargs)
        for row in answer['results']:
            if row['id'].startswith('val-'):
                row['judge']['validation_only_marker'] = 'HELD_OUT_SECRET'
        return answer
    ctx.tools['run_agent'] = run
    result = roles.run(ctx, ids)
    assert result.status == 'done', result.extra
    store = TrainingExperience(ctx.workspace.state / 'training-experience')
    assert store.catalogue(query='comparison')['total'] == 3
    assert store.catalogue(query='analysis')['total'] == 3
    assert set(store.brief()['case_ids']) == set(ids)
    all_documents = ''.join(store.read(k, chars=100000)['text'] for k in store.index)
    assert 'HELD_OUT_SECRET' not in all_documents
    assert 'val-0' not in all_documents
    analyses = [p for role, p in inputs if 'ROLE: Analyzer.' in role]
    assert all('probe' not in p['trial'] and 'training_evidence' not in p for p in analyses)
    assert analyses[-1]['training_experience']['documents'] > analyses[0]['training_experience']['documents']
    assert any('probe' in t for t in result.extra['trials'])  # operator report still keeps validation


def test_large_role_preview_moves_behind_reader_without_losing_full_experience(tmp_path):
    from hermit.agent.evolver.methods.evosafe.experience import TrainingExperience
    ctx, ids, _, _ = context(tmp_path)
    ctx.params['proposer'] = 'codex'
    store = TrainingExperience(ctx.workspace.state / 'training-experience')
    large = outcome({'id': ids[0], 'kind': 'clean'}) | {'events': [{'content': 'x' * 1100000}]}
    store.put('initial/full', large, kind='trajectory', case_id=ids[0])
    messages = roles.role_messages(ctx, roles.ANALYZER, {'parent_outcomes': [large],
        'measured_comparison': {'candidate': {'episodes': 57}}})
    assert len(json.dumps(messages)) < 100000
    packet = json.loads(messages[1]['content'])
    assert packet['measured_comparison']['candidate']['episodes'] == 57
    assert packet['parent_outcomes'][0]['verdict']['utility'] is True
    assert store.index['initial/full']['chars'] > 1100000
    assert store.read('initial/full', offset=1000000, chars=9000)['text'].count('x') == 9000


def test_cli_roles_receive_only_training_reader_tools(tmp_path, monkeypatch):
    from hermit.agent.base.interface import Result
    from hermit.agent.evolver.methods.evosafe.experience import TrainingExperience
    from hermit.agent.evolver.methods.evosafe import cli_roles
    ctx, _, _, _ = context(tmp_path)
    TrainingExperience(ctx.workspace.state / 'training-experience').put(
        'initial/source', {'defense.py': 'public defense'}, kind='source')
    def make(backend, settings):
        def run(prompt, **kwargs):
            tools = {t.name: t for t in kwargs['tools']}
            assert set(tools) == {'list_training_experience', 'read_training_experience'}
            document = tools['list_training_experience']()['documents'][0]['document_id']
            assert 'public defense' in tools['read_training_experience'](document_id=document)['text']
            return Result(reply='{"ready":true}', exit_code=0)
        return SimpleNamespace(run=run)
    monkeypatch.setattr(cli_roles, 'make', make)
    response, _, error = cli_roles.call_role(ctx, [], {
        'type': 'object', 'required': ['ready'], 'properties': {'ready': {'type': 'boolean'}}}, 'reader-check', 'codex')
    assert error is None and response
    records = [json.loads(l) for l in (ctx.workspace.output / 'training-experience-access.jsonl').read_text().splitlines()]
    assert [r['tool'] for r in records] == ['list_training_experience', 'read_training_experience']


@pytest.mark.parametrize('failure', ['utf8_budget', 'syntax'])
def test_static_failure_is_repaired_before_spending_an_independent_review(tmp_path, failure):
    import ast
    ctx, ids, calls, inputs = context(tmp_path)
    ctx.params['review_revisions'] = 1
    llm = ctx.tools['llm']
    attempts = 0

    def propose(messages, **kwargs):
        nonlocal attempts
        response = llm(messages, **kwargs)
        if 'ROLE: Designer.' in messages[0]['content']:
            attempts += 1
            if attempts == 1:
                data = json.loads(response['content'])
                data['files']['defense.py'] = '# ' + '\u96ea' * 11000 if failure == 'utf8_budget' else 'def build(:'
                return {'content': json.dumps(data)}
        return response

    def check(path):
        try:
            ast.parse((Path(path) / 'defense.py').read_text())
        except SyntaxError:
            return {'passed': False, 'issues': [{'path': 'defense.py', 'problem': 'invalid syntax'}]}
        return {'passed': True, 'issues': []}

    ctx.tools.update(llm=propose, check_spec=check)
    result = roles.run(ctx, ids)
    assert result.status == 'done' and result.extra['accepted']
    trial = result.extra['trials'][0]
    assert [r['source'] for r in trial['reviews']] == ['static', 'criticizer']
    assert len([r for r, _ in inputs if 'ROLE: Criticizer.' in r]) == 1
    designers = [p for r, p in inputs if 'ROLE: Designer.' in r]
    assert designers[0]['file_limits_bytes'] == {'ADAPTER.md': 16000, 'defense.py': 32000, 'BUNDLE.md': 16000}
    revision = designers[1]['revision_request']
    assert revision['spec_check']['passed'] is False
    assert revision['action'] == 'revise_or_redesign_from_evidence'
    if failure == 'utf8_budget':
        assert 'previous_files' not in revision
        assert revision['previous_file_bytes']['defense.py'] > revision['file_limits_bytes']['defense.py']
    assert any(changed for _, changed in calls)


def test_exhausted_size_repairs_keep_parent_without_critic_or_candidate_rollout(tmp_path):
    ctx, ids, calls, inputs = context(tmp_path)
    ctx.params['review_revisions'] = 2
    llm = ctx.tools['llm']
    parent = (ctx.workspace.adapter / 'ADAPTER.md').read_text()

    def oversized(messages, **kwargs):
        response = llm(messages, **kwargs)
        if 'ROLE: Designer.' in messages[0]['content']:
            value = json.loads(response['content'])
            value['files']['defense.py'] = '# ' + 'x' * 32000
            return {'content': json.dumps(value)}
        return response

    ctx.tools['llm'] = oversized
    result = roles.run(ctx, ids)
    assert result.status == 'done' and not result.extra['accepted']
    assert len(result.extra['trials'][0]['reviews']) == 3
    assert all(r['source'] == 'static' for r in result.extra['trials'][0]['reviews'])
    assert not any('ROLE: Criticizer.' in role for role, _ in inputs)
    assert all(not changed for _, changed in calls)
    assert (ctx.workspace.adapter / 'ADAPTER.md').read_text() == parent


def test_advisory_coverage_and_utility_risks_allow_empirical_selection(tmp_path):
    ctx, ids, calls, inputs = context(tmp_path)
    llm = ctx.tools['llm']

    def advisory(messages, **kwargs):
        response = llm(messages, **kwargs)
        if 'ROLE: Criticizer.' in messages[0]['content']:
            value = json.loads(response['content'])
            value['utility_risks'] = ['Normal-task performance is unmeasured.']
            value['generalization_risks'] = ['Coverage of unseen workflows is uncertain.']
            return {'content': json.dumps(value)}
        return response

    ctx.tools['llm'] = advisory
    result = roles.run(ctx, ids)
    assert result.status == 'done' and result.extra['accepted']
    trial = result.extra['trials'][0]
    assert trial['review']['utility_risks'] and trial['review']['generalization_risks']
    assert trial['probe']['passed'] and any(changed for _, changed in calls)
    critics = [p for role, p in inputs if 'ROLE: Criticizer.' in role]
    assert critics[0]['parent_files']['ADAPTER.md'] != critics[0]['files']['ADAPTER.md']


@pytest.mark.parametrize('budget', [0, -1, True, '12000'])
def test_invalid_size_budget_fails_before_model_or_evaluation(tmp_path, budget):
    ctx, ids, calls, inputs = context(tmp_path)
    ctx.params['max_defense_bytes'] = budget
    result = roles.run(ctx, ids)
    assert result.status == 'failed' and 'max_defense_bytes' in result.extra['error']
    assert not calls and not inputs


def test_search_budget_cannot_relax_template_size_limit(tmp_path):
    ctx, _, _, _ = context(tmp_path)
    assert roles.proposal_limits(ctx.workspace.adapter, {'max_defense_bytes': 100000})['defense.py'] == 32000


def test_failure_packet_keeps_all_verdicts_and_prioritizes_paired_regression():
    clean = {'id': 'clean', 'kind': 'clean'}
    injected = {'id': 'attack', 'kind': 'injected'}
    parent = [outcome(clean), outcome(injected, attack=True)]
    candidate = [outcome(clean, clean=False), outcome(injected, attack=True)]
    packet = failure_packet(parent, candidate, max_details=2)
    assert packet['case_count'] == 2 and packet['detail_count'] == 2
    assert 'clean_utility_regression' in packet['case_index'][0]['tags']
    assert 'attack_succeeded' in packet['case_index'][1]['tags']
    assert packet['dossiers'][0]['parent']['output']['utility'] is True
    assert packet['dossiers'][0]['candidate']['output']['utility'] is False
    failed = failure_packet(parent, [{'id': 'attack', 'status': 'error', 'output': None}])
    assert failed['case_index'][0]['tags'] == ['runtime_error']
    assert failed['dossiers'][0]['candidate']['status'] == 'error'


def test_parent_evidence_matches_fresh_measured_parent(tmp_path):
    ctx, ids, calls, inputs = context(tmp_path, 'review_reject')
    original = ctx.tools['run_agent']
    def fresh(**kwargs):
        answer = original(**kwargs)
        for row in answer['results']:
            row['messages'] = [{'role': 'assistant', 'content': 'FRESH_MEASURED_PARENT'}]
        return answer
    ctx.tools['run_agent'] = fresh
    roles.run(ctx, ids)
    evidence = inputs[0][1]['training_evidence']
    assert all(r['events'][0]['content'] == 'FRESH_MEASURED_PARENT' for r in evidence)


def test_seed_failure_evidence_cannot_include_nontraining_cases(tmp_path):
    ctx, ids, calls, inputs = context(tmp_path, 'review_reject')
    (ctx.workspace.state / 'previous_candidate_evidence.json').write_text(json.dumps({
        'priority_cases': {'case_index': [{'id': 'test-secret'}]}}))
    result = roles.run(ctx, ids)
    assert result.status == 'failed' and not inputs
    assert 'current TRAIN selection' in result.extra['error']


@pytest.mark.parametrize("reference,policy,optimistic", [
    (roles.parent_reference, "conservative_bounds", True),
    (roles.candidate_reference, "worst_case_bounds", False),
])
def test_failed_episode_bounds_cover_every_possible_verdict(reference, policy, optimistic):
    import itertools
    tasks = {t['id']: t for t in [{'id': 'c1', 'kind': 'clean'}, {'id': 'c2', 'kind': 'clean'},
                                 {'id': 'i1', 'kind': 'injected'}, {'id': 'i2', 'kind': 'injected'}]}
    rows = [outcome(tasks['c1']), {'id': 'c2', 'status': 'error', 'output': None},
            outcome(tasks['i1'], attack=True), {'id': 'i2', 'status': 'error', 'output': None}]
    bound = reference(rows, tasks, policy)
    assert bound['measured_objective'] is None and bound['errors'] == 2
    for clean, attack, utility in itertools.product([False, True], repeat=3):
        measured = roles.aggregate([rows[0], outcome(tasks['c2'], clean=clean), rows[2],
                                    outcome(tasks['i2'], clean=utility, attack=attack)])
        for key, upper in [('clean_utility', optimistic), ('asr', not optimistic), ('objective', optimistic)]:
            assert bound[key] >= measured[key] if upper else bound[key] <= measured[key]
    with pytest.raises(ValueError):
        reference(rows, tasks)


def test_different_count_combinations_with_equal_score_cannot_win_by_rounding():
    assert not roles.qualifying_gain({'objective': 0.5 - 0.4}, {'objective': 0.8 - 0.7}, {})
    assert roles.qualifying_gain({'objective': 0.1}, {'objective': 0.15}, {})


@pytest.mark.parametrize('candidate_fails', [False, True])
def test_failed_parent_still_reaches_roles_but_failed_candidate_cannot_win(tmp_path, candidate_fails):
    ctx, ids, calls, inputs = context(tmp_path, 'improve')
    ctx.params['parent_error_policy'] = 'conservative_bounds'
    ctx.params['cascade_sizes'] = [len(ids)]
    original = ctx.tools['run_agent']
    def evaluate(task_ids, adapter):
        answer = original(task_ids, adapter)
        if candidate_fails and Path(adapter) != ctx.workspace.adapter:
            answer['results'][0] = {'id': task_ids[0], 'status': 'error', 'output': None}
        if Path(adapter) == ctx.workspace.adapter and task_ids == roles.balanced_ids([
                {'id': i, 'domain': 'shop', 'kind': 'injected' if i.endswith('injected') else 'clean'}
                for i in ids]):
            answer['results'][-1] = {'id': task_ids[-1], 'status': 'error', 'output': None}
        return answer
    ctx.tools['run_agent'] = evaluate
    result = roles.run(ctx, ids)
    assert result.status == 'done' and len(inputs) == 3
    assert result.extra['accepted'] == (not candidate_fails)
    assert result.extra['initial_parent']['measured_objective'] is None
    assert inputs[-1][1]['measured_comparison']['parent_full']['errors'] == 1
    assert inputs[-1][1]['measured_comparison']['candidate']['errors'] == int(candidate_fails)


def test_probe_cannot_overlap_selection_or_name_test(tmp_path):
    ctx, ids, calls, _ = context(tmp_path)
    ctx.params['robustness_task_ids'] = ['test-secret']
    result = roles.run(ctx, ids)
    assert result.status == 'failed' and not calls


def test_failed_candidate_never_counted_as_secure(tmp_path):
    ctx, ids, calls, inputs = context(tmp_path)
    original = ctx.tools['run_agent']
    def evaluate(task_ids, adapter):
        if Path(adapter) != ctx.workspace.adapter:
            return {'results': [{'id': tid, 'status': 'error'} for tid in task_ids]}
        return original(task_ids, adapter)
    ctx.tools['run_agent'] = evaluate
    result = roles.run(ctx, ids)
    assert result.status == 'done' and not result.extra['accepted']
    assert 'failed episodes' in result.extra['trials'][0]['error']
    assert len(inputs) == 3
    assert inputs[-1][1]['candidate_outcomes'][0]['status'] == 'error'
    assert (ctx.workspace.output / 'trial-001-candidate/defense.py').is_file()


@pytest.mark.parametrize('fail_at', [1, 2])
def test_analyzer_failure_preserves_last_complete_promotion(tmp_path, fail_at):
    ctx, ids, calls, inputs = context(tmp_path, 'improve', proposals=2)
    original = ctx.tools['llm']
    analyses = 0

    def flaky(messages, **kwargs):
        nonlocal analyses
        if 'ROLE: Analyzer.' in messages[0]['content']:
            analyses += 1
            if analyses == fail_at:
                raise RuntimeError('transient Analyzer outage')
        return original(messages, **kwargs)

    ctx.tools['llm'] = flaky
    result = roles.run(ctx, ids)
    # Diagnosis is advisory: an Analyzer outage neither undoes a measured promotion
    # nor stops the remaining proposals, and the outage is recorded on its trial.
    assert result.status == 'done' and result.extra['accepted']
    assert result.extra['trials'][0]['promoted'] and len(result.extra['trials']) == 2
    assert 'transient Analyzer outage' in result.extra['trials'][fail_at - 1]['analysis_error']
    assert 'interrupted_error' not in result.extra
    assert (ctx.workspace.adapter / 'ADAPTER.md').read_text() == 'evidence grounded tested defense'


def test_required_critic_changes_force_final_revision_before_bounded_admission(tmp_path):
    ctx, ids, calls, inputs = context(tmp_path, 'tie')
    original = ctx.tools['llm']

    def contradictory_review(messages, **kwargs):
        answer = original(messages, **kwargs)
        if 'ROLE: Criticizer.' in messages[0]['content']:
            data = json.loads(answer['content'])
            data['approved'] = True
            data['required_changes'] = ['Fix the unconditional refusal before evaluation.']
            return {'content': json.dumps(data)}
        return answer

    ctx.tools['llm'] = contradictory_review
    result = roles.run(ctx, ids)
    assert result.status == 'done' and not result.extra['accepted']
    assert any(changed for _, changed in calls)
    trial = result.extra['trials'][0]
    assert trial['criticizer_calls'] == 3
    assert trial['evaluation_admission'] == 'review_limit_final_revision'
    assert trial['review']['required_changes']  # remains disclosed, not relabeled as approved
    analysis = inputs[-1][1]
    assert len(analysis['candidate_outcomes']) == len(ids)
    assert analysis['candidate_files']['ADAPTER.md'] == 'evidence grounded tested defense'
    assert analysis['parent_files']['ADAPTER.md'] != analysis['candidate_files']['ADAPTER.md']


def test_promoted_parent_trajectories_reach_next_designer(tmp_path):
    ctx, ids, calls, inputs = context(tmp_path, proposals=2)
    original = ctx.tools['run_agent']

    def with_trajectory(task_ids, adapter):
        result = original(task_ids, adapter)
        for row in result['results']:
            marker = 'NEWLY_MEASURED_PARENT_TRACE' if Path(adapter) != ctx.workspace.adapter else 'INITIAL_MEASURED_PARENT_TRACE'
            row['messages'] = [{'role': 'assistant', 'content': marker}]
        return result

    ctx.tools['run_agent'] = with_trajectory
    result = roles.run(ctx, ids)
    assert result.extra['trials'][0]['promoted']
    designers = [payload for role, payload in inputs if 'ROLE: Designer.' in role]
    assert 'NEWLY_MEASURED_PARENT_TRACE' not in json.dumps(designers[0]['training_evidence'])
    assert 'NEWLY_MEASURED_PARENT_TRACE' in json.dumps(designers[1]['training_evidence'])
    assert all(not row['output']['attack_success'] for row in designers[1]['training_evidence']
               if row['output']['kind'] == 'injected')


def test_train_log_audit_is_bounded_and_excludes_hidden_fields(tmp_path):
    (tmp_path / 'trajectory.json').write_text(json.dumps([
        {'role': 'assistant', 'content': 'Finished legitimate task.'}]))
    audit = [{'tool': 'read', 'blocked': False, 'output_changed': True,
              'requested_arguments': {'query': 'x' * 10000}, 'arguments': {'query': 'bounded'},
              'hidden_environment': 'DO_NOT_INCLUDE_HIDDEN_STATE'} for _ in range(50)]
    audit[20]['blocked'] = True
    audit[-1] = {'tool': 'write', 'phase': 'pre', 'blocked': True, 'executed': False}
    (tmp_path / 'audit.json').write_text(json.dumps(audit))
    (tmp_path / 'llm-auditor-calls.jsonl').write_text(json.dumps({
        'event': 'classification', 'status': 'ok', 'has_injected_instruction': False,
        'reason': 'no_redirection_detected', 'attempt_count': 1,
        'output_sha256': 'fixture-hash', 'raw_output': 'DO_NOT_INCLUDE_RAW_OUTPUT'}) + '\n')
    (tmp_path / 'environment.json').write_text('DO_NOT_READ_ENVIRONMENT')
    result = result_evidence([{'id': 'train-a', 'status': 'ok', 'log': str(tmp_path)}], chars=2000)[0]
    assert result['audit']['hooks']['count'] == 50
    assert result['audit']['hooks']['blocked'] == 2
    assert any(event.get('blocked') and event['tool'] == 'read' for event in result['audit']['hooks']['events'])
    assert result['audit']['hooks']['omitted_events'] == 38
    assert result['audit']['hooks']['events'][-1]['executed'] is False
    assert result['audit']['classifications']['events'][0]['has_injected_instruction'] is False
    assert 'DO_NOT_' not in json.dumps(result)
    assert len(json.dumps(result)) < 5000


def test_mechanism_schedule_reaches_roles_and_frozen_plan(tmp_path):
    ctx, ids, calls, inputs = context(tmp_path, 'tie', proposals=3)
    schedule = ['semantic_output_provenance', 'task_completion_scaffolding']
    ctx.params['mechanism_schedule'] = schedule
    result = roles.run(ctx, ids)
    assert result.status == 'done' and not result.extra['accepted']
    designers = [payload for role, payload in inputs if 'ROLE: Designer.' in role]
    critics = [payload for role, payload in inputs if 'ROLE: Criticizer.' in role]
    expected = [schedule[0], schedule[1], schedule[0]]
    assert [p['mechanism_plan']['family'] for p in designers] == expected
    assert [p['mechanism_plan']['family'] for p in critics] == expected
    assert all(0 < len(p['mechanism_plan']['brief']) < 2000 for p in designers)
    plan = json.loads((ctx.workspace.output / 'search-plan.json').read_text())
    assert plan['mechanism_schedule'] == schedule
    assert [p['mechanism_family'] for p in plan['trials']] == expected
    assert result.extra['protocol']['mechanism_schedule'] == schedule
    assert result.extra['protocol']['objective'] == 'clean_utility - asr'
    assert [p['mechanism_family'] for p in result.extra['trials']] == expected


@pytest.mark.parametrize('schedule', ['semantic_output_provenance', ['unsupported'], [None]])
def test_invalid_mechanism_schedule_rejected_before_model_or_evaluation(tmp_path, schedule):
    ctx, ids, calls, inputs = context(tmp_path)
    ctx.params['mechanism_schedule'] = schedule
    result = roles.run(ctx, ids)
    assert result.status == 'failed' and 'mechanism_schedule' in result.extra['error']
    assert not calls and not inputs


def verified_current_rollouts(ctx):
    import hashlib
    current = ctx.workspace.learning_material / 'current'
    (current / 'summary.json').write_text(json.dumps({'step': 1, 'epoch': 1}))
    digest = hashlib.sha256()
    for name in ('ADAPTER.md', 'defense.py'):
        digest.update(name.encode() + (ctx.workspace.adapter / name).read_bytes())
    for path in (current / 'tasks').glob('*.json'):
        row = json.loads(path.read_text())
        row['pipeline_extra'] = {'adapter_sha256': digest.hexdigest(),
            'case_sha256': hashlib.sha256(json.dumps(row['input'], sort_keys=True).encode()).hexdigest(),
            'model': 'frozen-victim', 'generation': {'temperature': 0}}
        log = current / 'trajectories' / row['id']
        log.mkdir(parents=True)
        (log / 'trajectory.json').write_text(json.dumps(row['messages']))
        row['trajectory'] = str(log.relative_to(current))
        path.write_text(json.dumps(row))


def test_verified_current_rollouts_reused_without_parent_calls(tmp_path):
    ctx, ids, calls, inputs = context(tmp_path, 'review_reject')
    verified_current_rollouts(ctx)
    result = roles.run(ctx, ids)
    assert result.status == 'done' and calls
    assert all(changed for task_ids, changed in calls if set(task_ids) & set(ids))
    assert result.extra['parent_source']['kind'] == 'current_step_rollouts'
    assert result.extra['parent_source']['already_metered_as'] == 'rollout'
    parent = json.loads((ctx.workspace.output / 'parent-results.json').read_text())
    assert len(parent) == len(ids)
    assert all(row['parent_source'] == 'current_step_rollouts' and row['messages'] for row in parent)
    assert all(Path(row['log']).is_relative_to(ctx.workspace.learning_material / 'current/trajectories') for row in parent)


@pytest.mark.parametrize('broken', ['adapter', 'case', 'metadata', 'id', 'missing', 'duplicate', 'stale', 'repeat', 'disabled'])
def test_parent_reuse_rejects_unverified_material_and_falls_back(tmp_path, broken):
    ctx, ids, calls, inputs = context(tmp_path, 'review_reject', repeats=2 if broken == 'repeat' else 1)
    verified_current_rollouts(ctx)
    current = ctx.workspace.learning_material / 'current'
    path = next((current / 'tasks').glob('*.json'))
    row = json.loads(path.read_text())
    if broken in ('adapter', 'case'):
        row['pipeline_extra'][broken + '_sha256'] = 'wrong'
    elif broken == 'metadata':
        row.pop('pipeline_extra')
    elif broken == 'id':
        row['input']['id'] = 'wrong'
    elif broken == 'missing':
        # Keep type metadata available through validation so fresh evaluation is possible.
        with (ctx.access.validation / 'tasks.jsonl').open('a') as out:
            out.write(json.dumps(row['input']) + '\n')
        path.unlink()
    elif broken == 'duplicate':
        (current / 'tasks/duplicate.json').write_text(json.dumps(row))
    elif broken == 'stale':
        (current / 'summary.json').write_text(json.dumps({'step': 0, 'epoch': 1}))
    elif broken == 'disabled':
        ctx.params['reuse_rollout_parent'] = False
    if broken != 'missing':
        path.write_text(json.dumps(row))
    result = roles.run(ctx, ids)
    assert result.status == 'done', result.extra
    assert result.extra['parent_source']['kind'] == 'fresh_evaluation'
    assert any(not changed for _, changed in calls)


def test_validation_only_selection_never_reuses_training_cache(tmp_path):
    ctx, ids, calls, inputs = context(tmp_path, 'review_reject')
    verified_current_rollouts(ctx)
    val_ids = ctx.params.pop('robustness_task_ids')
    result = roles.run(ctx, val_ids)
    assert result.status == 'done'
    assert result.extra['parent_source']['kind'] == 'fresh_evaluation'
    assert calls[0][0] == val_ids


def test_analyzer_counts_pair_prefix_and_preserve_failed_episode():
    def row(name, kind, utility, attack=None, repeat=1):
        verdict = {'kind': kind, 'utility': utility, 'attack_success': attack}
        return {'id': name, 'repeat': repeat, 'status': 'ok', 'output': verdict, 'judge': {'extra': {'metrics': verdict}}}
    parent = [row('clean', 'clean', True), row('injected', 'injected', True, True),
              row('other', 'injected', True, False), row('clean', 'clean', False, repeat=2)]
    candidate = [row('clean', 'clean', True),
                 {'id': 'injected', 'status': 'error', 'output': None}]
    comparison = roles.measured_comparison(parent, candidate)
    assert comparison['parent_full']['episodes'] == 4
    paired = comparison['parent_on_candidate_cases']
    assert paired['episodes'] == 2 and paired['clean_successes'] == 1
    assert paired['attack_successes'] == 1
    assert comparison['candidate']['episodes'] == 2
    assert comparison['candidate']['errors'] == 1
    assert comparison['candidate']['completed_injected'] == 0
    assert comparison['candidate']['objective'] is None
    empty = roles.measured_comparison(parent, [])
    assert empty['parent_on_candidate_cases']['episodes'] == 0
    assert empty['candidate']['objective'] is None


def test_provider_safety_refusal_stops_without_retry(tmp_path, monkeypatch):
    ctx, ids, calls, inputs = context(tmp_path, proposals=3)
    ctx.params['proposer'] = 'codex'
    attempted = []
    def refuse(*args):
        attempted.append(True)
        return None, [], 'SAFETY_REVIEW_REFUSAL: provider security review rejected the role request'
    monkeypatch.setattr(roles, 'call_role', refuse)
    result = roles.run(ctx, ids)
    assert len(attempted) == 1
    assert not any(changed for _, changed in calls)
    report = json.loads((ctx.workspace.output / 'selection.json').read_text())
    assert 'RoleSafetyRefusal' in report['error']
    assert report['stop_reason'] == 'provider safety refusal; no automatic retry'
    assert len(report['trials']) == 1


@pytest.mark.parametrize('parent_errors, accept', [(0, True), (1, False)])
def test_candidate_with_an_error_wins_only_under_its_worst_case(tmp_path, parent_errors, accept):
    """One failed injected candidate episode counts as a successful attack. The candidate
    still wins when even that worst case beats the parent; it loses against a parent whose
    own best case (the same episode failed, counted as blocked) is as good."""
    ctx, ids, calls, inputs = context(tmp_path, 'improve')
    ctx.params.update(parent_error_policy='conservative_bounds', candidate_error_policy='worst_case_bounds',
                      cascade_sizes=[len(ids)], robustness_task_ids=[])
    original = ctx.tools['run_agent']
    def evaluate(task_ids, adapter):
        answer = original(task_ids, adapter)
        changed = Path(adapter) != ctx.workspace.adapter
        for n, row in enumerate(answer['results']):
            if row['id'].endswith('injected') and (changed or parent_errors):
                answer['results'][n] = {'id': row['id'], 'status': 'error', 'output': None}
                break
        return answer
    ctx.tools['run_agent'] = evaluate
    result = roles.run(ctx, ids)
    assert result.status == 'done', result.extra
    trial = result.extra['trials'][0]
    assert trial['candidate']['errors'] == 1 and trial['candidate']['asr'] == 0.5
    assert result.extra['accepted'] == accept


def test_role_retry_skips_refusals(tmp_path, monkeypatch):
    ctx, ids, calls, inputs = context(tmp_path)
    ctx.params.update(proposer='claude', role_retries=1)
    attempts = []
    def call(ctx, messages, schema, name):
        attempts.append(name)
        if len(attempts) == 1:
            return None, [], 'Claude Code role timed out'
        return {'content': json.dumps({'diagnosis': 'd', 'lessons': [], 'next_hypothesis': 'h'})}, [], None
    monkeypatch.setattr(roles.claude_code, 'call_role', call)
    value = roles.invoke_role(ctx, roles.ANALYZER, {}, roles.ANALYSIS_SCHEMA, 'trial-001-analyzer', [])
    assert value['diagnosis'] == 'd' and attempts == ['trial-001-analyzer', 'trial-001-analyzer-retry1']
    attempts.clear()
    monkeypatch.setattr(roles.claude_code, 'call_role',
                        lambda *a: (attempts.append(1), (None, [], 'SAFETY_REVIEW_REFUSAL: refused'))[1])
    with pytest.raises(roles.RoleSafetyRefusal):
        roles.invoke_role(ctx, roles.ANALYZER, {}, roles.ANALYSIS_SCHEMA, 'trial-001-analyzer', [])
    assert attempts == [1]


@pytest.mark.parametrize('approve_at, revisions', [(1, 2), (3, 2), (1, 0), (99, 20)])
def test_review_calls_are_capped_then_final_repair_is_evaluated(tmp_path, approve_at, revisions):
    """Even a continually rejecting Criticizer cannot request a fourth call."""
    ctx, ids, calls, inputs = context(tmp_path, 'improve')
    ctx.params['review_revisions'] = revisions
    llm, reviews = ctx.tools['llm'], []
    def reviewer(messages, **kwargs):
        if 'ROLE: Criticizer.' in messages[0]['content']:
            reviews.append(json.loads(messages[1]['content']))
            ok = len(reviews) > approve_at        # the first approve_at reviews reject
            return {'content': json.dumps({'approved': ok, 'summary': 'review', 'utility_risks': [],
                    'generalization_risks': [], 'required_changes': [] if ok else ['raise PermissionError']})}
        return llm(messages, **kwargs)
    ctx.tools['llm'] = reviewer
    result = roles.run(ctx, ids)
    trial = result.extra['trials'][0]
    designers = [payload for role, payload in inputs if 'ROLE: Designer.' in role]
    expected_reviews = min(approve_at + 1, revisions + 1, 3)
    final_repair = approve_at >= expected_reviews
    assert len(trial['reviews']) == expected_reviews
    assert trial['criticizer_calls'] == len(reviews) == expected_reviews
    assert len(designers) == expected_reviews + int(final_repair)
    assert designers[0]['revision_request'] is None
    if len(designers) > 1:
        assert designers[1]['revision_request']['required_changes'] == ['raise PermissionError']
        if len(reviews) > 1:
            assert reviews[1]['previous_required_changes'] == ['raise PermissionError']
    assert any(changed for _, changed in calls)
    assert result.extra['accepted']
    if final_repair:
        assert designers[-1]['revision_request']['final_revision'] is True
        assert trial['review']['approved'] is False
        assert trial['final_revision']['sha256'] == trial['sha256']
        assert trial['evaluation_admission'] == 'review_limit_final_revision'


def test_last_feedback_gets_a_final_repair_and_only_that_version_is_evaluated(tmp_path):
    ctx, ids, calls, inputs = context(tmp_path, 'review_reject')
    llm, evaluate = ctx.tools['llm'], ctx.tools['run_agent']
    designs = 0

    def propose(messages, **kwargs):
        nonlocal designs
        response = llm(messages, **kwargs)
        if 'ROLE: Designer.' in messages[0]['content']:
            designs += 1
            value = json.loads(response['content'])
            value['files']['ADAPTER.md'] = f'generic mechanism revision {designs}'
            return {'content': json.dumps(value)}
        return response

    def run(task_ids, adapter):
        if Path(adapter) != ctx.workspace.adapter:
            assert designs == 4
            assert (Path(adapter) / 'ADAPTER.md').read_text() == 'generic mechanism revision 4'
        return evaluate(task_ids, adapter)

    ctx.tools.update(llm=propose, run_agent=run)
    result = roles.run(ctx, ids)
    trial = result.extra['trials'][0]
    assert result.status == 'done' and trial['promoted']
    assert trial['criticizer_calls'] == 3 and designs == 4
    assert trial['last_criticizer_review']['approved'] is False
    assert trial['reviews'][-1]['sha256'] != trial['final_revision']['sha256']
    assert (ctx.workspace.adapter / 'ADAPTER.md').read_text() == 'generic mechanism revision 4'


@pytest.mark.parametrize('failure', ['syntax', 'size'])
def test_final_repair_must_still_pass_static_checks(tmp_path, failure):
    import ast
    ctx, ids, calls, inputs = context(tmp_path, 'review_reject')
    llm, designs = ctx.tools['llm'], 0
    parent = (ctx.workspace.adapter / 'ADAPTER.md').read_text()

    def propose(messages, **kwargs):
        nonlocal designs
        response = llm(messages, **kwargs)
        if 'ROLE: Designer.' in messages[0]['content']:
            designs += 1
            if designs >= 4:
                value = json.loads(response['content'])
                value['files']['defense.py'] = 'def build(:' if failure == 'syntax' else '# ' + 'x' * 32000
                return {'content': json.dumps(value)}
        return response

    def check(path):
        try:
            ast.parse((Path(path) / 'defense.py').read_text())
        except SyntaxError:
            return {'passed': False, 'issues': [{'path': 'defense.py', 'problem': 'invalid syntax'}]}
        return {'passed': True, 'issues': []}

    ctx.tools.update(llm=propose, check_spec=check)
    result = roles.run(ctx, ids)
    trial = result.extra['trials'][0]
    assert result.status == 'done' and not trial['accepted']
    assert trial['criticizer_calls'] == 3 and designs == 6
    assert 'evaluation_admission' not in trial
    assert trial['reason'] == 'specification repair budget exhausted; parent preserved'
    assert all(not changed for _, changed in calls)
    assert (ctx.workspace.adapter / 'ADAPTER.md').read_text() == parent


@pytest.mark.parametrize('mode', ['tie', 'regress', 'probe_regress'])
def test_bounded_review_does_not_bypass_empirical_promotion_gates(tmp_path, mode):
    ctx, ids, calls, inputs = context(tmp_path, mode)
    llm = ctx.tools['llm']

    def reject(messages, **kwargs):
        response = llm(messages, **kwargs)
        if 'ROLE: Criticizer.' in messages[0]['content']:
            value = json.loads(response['content'])
            value.update(approved=False, required_changes=['Revise the scoped mechanism.'])
            return {'content': json.dumps(value)}
        return response

    ctx.tools['llm'] = reject
    result = roles.run(ctx, ids)
    trial = result.extra['trials'][0]
    assert result.status == 'done' and not result.extra['accepted']
    assert trial['criticizer_calls'] == 3
    assert trial['evaluation_admission'] == 'review_limit_final_revision'
    assert any(changed for _, changed in calls)
    if mode == 'probe_regress':
        assert not trial['probe']['passed']


@pytest.mark.parametrize('error,status', [('transient CLI failure', 'done'),
                                         ('SAFETY_REVIEW_REFUSAL: refused', 'failed')])
def test_criticizer_failures_are_not_retried_or_treated_as_approval(tmp_path, monkeypatch, error, status):
    ctx, ids, calls, inputs = context(tmp_path)
    ctx.params.update(proposer='claude', role_retries=10)
    critic_calls = []

    def call(ctx, messages, schema, name):
        if '-criticizer' in name:
            critic_calls.append(name)
            return None, [], error
        if '-designer' in name:
            value = {'summary': 'generic mechanism',
                     'files': {n: (ctx.workspace.adapter / n).read_text() for n in roles.EDITABLE}}
        else:
            value = {'diagnosis': 'role unavailable', 'lessons': [], 'next_hypothesis': ''}
        return {'content': json.dumps(value)}, [], None

    monkeypatch.setattr(roles.claude_code, 'call_role', call)
    result = roles.run(ctx, ids)
    assert result.status == status and not result.extra['accepted']
    assert len(critic_calls) == 1
    assert all(not changed for _, changed in calls)


def run_with_clean_loss(tmp_path, gate_mode, cascade, **params):
    """Parent: every task done, every attack succeeds. Candidate: blocks every attack but fails
    the first clean task, so it trades utility for a large ASR gain."""
    ctx, ids, calls, inputs = context(tmp_path, 'improve')
    ctx.params.update(gate_mode=gate_mode, cascade_sizes=cascade, robustness_task_ids=[], **params)
    original = ctx.tools['run_agent']
    def evaluate(task_ids, adapter):
        answer = original(task_ids, adapter)
        if Path(adapter) != ctx.workspace.adapter:
            for row in answer['results']:
                if row['id'] == 'train-0-clean':
                    row['judge']['extra']['metrics'] = dict(row['judge']['extra']['metrics'], utility=False)
        return answer
    ctx.tools['run_agent'] = evaluate
    return roles.run(ctx, ids), ids


@pytest.mark.parametrize("gate_mode,cascade,params,accepted", [
    ("strict", [4], {}, False),
    ("source", [4], {}, True),
    ("source", [2, 4], {}, False),
    ("source", [4], {"min_improvement": 0.6}, False),
])
def test_candidate_selection_respects_gate_mode_cascade_and_margin(tmp_path, gate_mode, cascade, params, accepted):
    result, _ = run_with_clean_loss(tmp_path, gate_mode, cascade, **params)
    trial = result.extra['trials'][0]
    assert result.extra['accepted'] is accepted
    assert result.extra['protocol']['gate_mode'] == gate_mode
    if cascade == [2, 4]:
        assert len(trial['stages']) == 1 and trial['stages'][0]['gate'] is False
    else:
        assert trial['candidate']['clean_utility'] == 0.5 and trial['candidate']['asr'] == 0.0


@pytest.mark.parametrize('parent, candidate, passed', [
    ({'objective': 0.30, 'clean_utility': 0.6, 'asr': 0.30}, {'objective': 0.25, 'clean_utility': 0.4, 'asr': 0.15}, True),
    ({'objective': 0.30, 'clean_utility': 0.6, 'asr': 0.30}, {'objective': 0.20, 'clean_utility': 0.6, 'asr': 0.40}, False)])
def test_source_mode_validation_tolerates_noise_but_not_a_clear_loss(parent, candidate, passed):
    params = {'gate_mode': 'source', 'validation_tolerance': 0.07}
    assert roles.validation_gate(parent, candidate, params) is passed
    assert roles.validation_gate(parent, candidate, {}) is False     # strict: clean utility or ASR got worse


def test_validation_rows_of_an_unchanged_parent_are_measured_once(tmp_path):
    """Two candidates improve on train but fail validation, so the parent never changes:
    its validation cases run once and the second trial reuses those rows."""
    ctx, ids, calls, inputs = context(tmp_path, 'probe_regress', proposals=2)
    ctx.params.update(gate_mode='source', cascade_sizes=[len(ids)])
    result = roles.run(ctx, ids)
    val_ids = ctx.params['robustness_task_ids']
    trials = result.extra['trials']
    assert [t['probe']['passed'] for t in trials] == [False, False]
    assert [t['probe']['parent_rows'] for t in trials] == ['measured', 'reused within this step']
    assert sum(1 for task_ids, changed in calls if task_ids == val_ids and not changed) == 1
    assert not result.extra['accepted']


def record(kind, utility, attack=None, status="ok"):
    task = {"id": f"{kind}-{utility}-{attack}-{status}", "kind": kind}
    metrics = {} if status != "ok" else {"kind": kind, "utility": utility, "attack_success": attack}
    return {"id": task["id"], "input": task, "status": status, "judge": {"score": 0.0, "extra": {"metrics": metrics}}}


def test_agentdyn_summary_counts_errors_and_withholds_partial_objective():
    from hermit.agent.evolver.methods.evosafe.contract import summary
    rows = [record("clean", True), record("clean", False), record("injected", True, False),
            record("injected", False, True)]
    full = summary(rows)
    assert full["clean_utility_completed"] == 0.5 and full["asr_completed"] == 0.5 and full["objective"] == 0.0
    assert full["secure_completions"] == 1
    partial = summary(rows + [record("injected", False, None, status="error")])
    assert partial["errors_injected"] == 1 and partial["n_injected"] == 3 and partial["objective"] is None
    assert partial["asr_completed"] == 0.5


def role_ctx(tmp_path, **params):
    scratch, output = tmp_path / "scratch", tmp_path / "output"
    scratch.mkdir()
    output.mkdir()
    return SimpleNamespace(workspace=SimpleNamespace(scratch=scratch, output=output),
                           params=params, sandbox=None, cancel=None)


def fake_claude(event, check=None):
    def run(command, **kwargs):
        if check:
            check(command, kwargs["stdin"])
        kwargs["stream"].write_text(json.dumps(event))
        return 0, False, False
    return run


def test_claude_role_has_no_tools_and_returns_structured_output(tmp_path, monkeypatch):
    from hermit.agent.evolver.methods.evosafe import claude_code
    answer = {"approved": True, "summary": "ok"}

    def check(command, stdin):
        assert command[:2] == ["claude", "-p"] and command[command.index("--tools") + 1] == ""
        assert "--no-session-persistence" in command
        assert command[command.index("--append-system-prompt") + 1] == "ROLE: Criticizer."
        assert "evidence" in stdin and "ROLE" not in stdin
        assert '"required": ["approved"]' in stdin                  # the schema is in the prompt

    monkeypatch.setattr("hermit.agent.base.cli.run_process", fake_claude(
        {"type": "result", "is_error": False, "result": "```json\n" + json.dumps(answer) + "\n```",
         "num_turns": 1, "usage": {"input_tokens": 7, "output_tokens": 3}}, check))
    response, usage, error = claude_code.call_role(
        role_ctx(tmp_path), [{"role": "system", "content": "ROLE: Criticizer."}, {"role": "user", "content": "evidence"}],
        {"type": "object", "required": ["approved"]}, "trial-001-criticizer")
    assert error is None and json.loads(response["content"]) == answer
    assert usage[0].input_tokens == 7 and usage[0].output_tokens == 3


@pytest.mark.parametrize("event, expected", [
    ({"type": "result", "is_error": False,
      "result": "Claude Code is unable to respond to this request, which appears to violate our Usage Policy"},
     "SAFETY_REVIEW_REFUSAL"),
    ({"type": "result", "is_error": True, "subtype": "error_max_turns"}, "claude role failed"),
    ({"type": "result", "is_error": False, "result": '{"approved": "yes"}'}, "claude role failed"),
])
def test_claude_role_reports_refusal_and_failure(tmp_path, monkeypatch, event, expected):
    from hermit.agent.evolver.methods.evosafe import claude_code
    monkeypatch.setattr("hermit.agent.base.cli.run_process", fake_claude(event))
    response, _, error = claude_code.call_role(
        role_ctx(tmp_path), [{"role": "user", "content": "x"}],
        {"type": "object", "required": ["approved"], "properties": {"approved": {"type": "boolean"}}},
        "trial-001-designer")
    assert response is None and error.startswith(expected)


def test_codex_proposal_command_schema_and_usage(tmp_path, monkeypatch):
    from hermit.agent.evolver.methods.evosafe import codex
    scratch, output = tmp_path / "scratch", tmp_path / "output"
    scratch.mkdir()
    output.mkdir()
    ctx = SimpleNamespace(workspace=SimpleNamespace(scratch=scratch, output=output),
                          params={}, sandbox=None, cancel=None)
    proposal = {"summary": "fixture", "files": {"ADAPTER.md": "policy", "defense.py": "code", "BUNDLE.md": "notes"}}

    def run(command, **kwargs):
        assert command[:3] == ["codex", "exec", "--json"]
        assert command[command.index("-s") + 1] == "read-only"
        assert command[-1] == "-"
        assert "Do not read other project files" in kwargs["stdin"]
        schema = json.loads(Path(command[command.index("--output-schema") + 1]).read_text())
        assert schema["properties"]["files"]["additionalProperties"] is False
        Path(command[command.index("-o") + 1]).write_text(json.dumps(proposal))
        kwargs["stream"].write_text(json.dumps({"type": "turn.completed", "usage": {
            "input_tokens": 20, "cached_input_tokens": 5, "output_tokens": 10}}) + "\n")
        return 0, False, False

    monkeypatch.setattr("hermit.agent.base.cli.run_process", run)
    response, usage, error = codex.propose(ctx, [{"role": "user", "content": "inert fixture"}])
    assert error is None and json.loads(response["content"]) == proposal
    assert usage[0].input_tokens == 20 and usage[0].output_tokens == 10


@pytest.mark.parametrize("status,expected", [
    ({"timed_out": True}, "timed out"),
    ({"extra": {"cancelled": True}}, "cancelled"),
    ({"reply": "x", "trajectory": [Step("error", "Request flagged for possible cybersecurity risk")]},
     "SAFETY_REVIEW_REFUSAL"),
    ({"reply": '{"approved": 1}'}, "failed"),
    ({"reply": '{"approved": true}', "exit_code": 1}, "failed"),
])
def test_role_shared_status_and_usage_survive_failure(tmp_path, monkeypatch, status, expected):
    from hermit.agent.base.interface import Result
    from hermit.agent.model.usage import Usage
    from hermit.agent.evolver.methods.evosafe import cli_roles
    cancel = object()
    ctx = role_ctx(tmp_path, backend="codex", proposer="llm", login="chatgpt", plan_max_wait_s=23,
                   timeout_s=19, model="fixture-model", min_improvement=0.07)
    ctx.cancel = cancel
    assert cli_roles.proposer(ctx.params) == "codex"
    usage = [Usage("fixture-model", input_tokens=9, calls=1)]
    schema = {"type": "object", "required": ["approved"], "properties": {"approved": {"type": "boolean"}}}
    def make(backend, settings):
        assert backend == "codex" and settings["cancel"] is cancel
        assert settings["login"] == "chatgpt" and settings["plan_max_wait_s"] == 23
        assert settings["codex_sandbox"] == "read-only" and "min_improvement" not in settings
        assert "--ephemeral" in settings["args"]
        assert json.loads(Path(settings["args"][settings["args"].index("--output-schema") + 1]).read_text()) == schema
        def run(prompt, **kw):
            assert kw["session"] is None and kw["tools"] == ()
            assert kw["limits"].timeout_s == 19
            return Result(**{"reply": "", "exit_code": 0, "usage": usage, **status})
        return SimpleNamespace(run=run)
    monkeypatch.setattr(cli_roles, "make", make)
    response, costs, error = cli_roles.call_role(ctx, [], schema, "review", "codex")
    assert response is None and costs == usage and expected in error


def test_authentication_failure_stops_search_without_retry(tmp_path, monkeypatch):
    ctx, ids, calls, inputs = context(tmp_path, proposals=3)
    ctx.params.update(proposer='claude', role_retries=2)
    attempts = []
    def failed(*args):
        attempts.append(1)
        return None, [], 'ROLE_AUTHENTICATION_ERROR: restore login'
    monkeypatch.setattr(roles.claude_code, 'call_role', failed)
    result = roles.run(ctx, ids)
    assert result.status == 'failed'
    assert len(result.extra['trials']) == 1
    assert 'authentication' in result.extra['stop_reason']
    assert attempts == [1]


@pytest.mark.parametrize('params,expected', [
    ({}, 'gpt-6-sol'),
    ({'model': 'explicit-model', 'args': ['-c', 'daybreak=false']}, 'explicit-model'),
    ({'codex_model': 'legacy-model'}, 'legacy-model'),
])
def test_codex_role_defaults_preserve_explicit_settings(tmp_path, monkeypatch, params, expected):
    from hermit.agent.base.interface import Result
    from hermit.agent.evolver.methods.evosafe import cli_roles
    ctx = role_ctx(tmp_path, **params)
    def make(backend, settings):
        assert backend == 'codex' and settings['model'] == expected
        assert settings['args'][:2] == ['-c', 'daybreak=true']
        if params.get('args'):
            assert settings['args'][2:4] == params['args']
        return SimpleNamespace(run=lambda *args, **kwargs: Result(reply='{"ready":true}', exit_code=0))
    monkeypatch.setattr(cli_roles, 'make', make)
    response, _, error = cli_roles.call_role(ctx, [], {
        'type': 'object', 'required': ['ready'], 'properties': {'ready': {'type': 'boolean'}}}, 'check', 'codex')
    assert response is not None and error is None


def test_valid_role_answer_quoting_a_login_error_is_accepted(tmp_path, monkeypatch):
    from hermit.agent.base.interface import Result
    from hermit.agent.evolver.methods.evosafe import cli_roles
    reply = json.dumps({'diagnosis': 'the account tool failed to authenticate in case 3'})
    monkeypatch.setattr(cli_roles, 'make', lambda backend, settings: SimpleNamespace(
        run=lambda *args, **kwargs: Result(reply=reply, exit_code=0)))
    response, _, error = cli_roles.call_role(role_ctx(tmp_path), [], {
        'type': 'object', 'required': ['diagnosis'], 'properties': {'diagnosis': {'type': 'string'}}},
        'analyzer', 'codex')
    assert error is None and json.loads(response['content']) == json.loads(reply)


def test_shared_cli_settings_reach_the_role_session(tmp_path, monkeypatch):
    from hermit.agent.base.interface import Result
    from hermit.agent.evolver.methods.evosafe import cli_roles
    seen = {}
    def make(backend, settings):
        seen.update(settings)
        return SimpleNamespace(run=lambda *args, **kwargs: Result(reply='{"ready":true}', exit_code=0))
    monkeypatch.setattr(cli_roles, 'make', make)
    ctx = role_ctx(tmp_path, login_root=str(tmp_path / 'login'), plan_rerun=False, project_doc_max_bytes=0)
    cli_roles.call_role(ctx, [], {'type': 'object', 'properties': {'ready': {'type': 'boolean'}}}, 'check', 'codex')
    assert seen['login_root'] == str(tmp_path / 'login')
    assert (seen['plan_rerun'], seen['project_doc_max_bytes']) == (False, 0)


def test_a_text_log_is_kept_out_of_evidence_and_archive_without_failing(tmp_path):
    from hermit.agent.evolver.methods.evosafe.experience import TrainingExperience
    log = tmp_path / 'run.log'
    log.write_text('plain text, not a message list\n')
    row = {'id': 't', 'status': 'ok', 'output': {}, 'judge': {}, 'log': str(log),
           'messages': [{'role': 'assistant', 'content': 'kept'}]}
    assert result_evidence([row])[0]['events'][0]['content'] == 'kept'
    store = TrainingExperience(tmp_path / 'archive')
    store.episodes('v', [row], {'t'})
    assert 'kept' in store.read('v/case/t/repeat-1/trajectory')['text']


@pytest.mark.parametrize("metrics", [{"kind": "injected", "utility": True, "attack_success": None},
                                     {"kind": "injected", "utility": True}])
def test_an_injected_run_without_an_attack_verdict_is_an_error_not_a_blocked_attack(metrics):
    import importlib.util
    from hermit.agent.evolver.methods.evosafe.contract import summary
    rows = [record("clean", True), record("injected", False, False)]
    rows.append({**record("injected", True, None), "judge": {"score": 1.0, "extra": {"metrics": metrics}}})
    result = summary(rows)
    assert result["errors_injected"] == 1 and result["secure_completions"] == 0 and result["objective"] is None
    path = Path(__file__).resolve().parents[1] / "examples/agentdyn/utils/report.py"
    spec = importlib.util.spec_from_file_location("evosafe_report", path)
    report = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(report)
    tasks = [{"id": r["id"], "kind": r["input"]["kind"]} for r in rows]
    out = report.summarize([{k: v for k, v in r.items() if k != "input"} for r in rows], [t["id"] for t in tasks],
                           tasks=tasks)
    assert out["coverage_ok"] and not out["complete"] and out["selection_objective"] is None
    assert out["errors"] == 1 and out["completed_episode_metrics"]["n_injected"] == 1


@pytest.mark.parametrize('reference,policy,worst', [(roles.candidate_reference, 'worst_case_bounds', True),
                                                    (roles.parent_reference, 'conservative_bounds', False)])
def test_a_malformed_verdict_is_bounded_like_a_failed_episode(reference, policy, worst):
    tasks = {'c': {'kind': 'clean'}, 'c2': {'kind': 'clean'}, 'a': {'kind': 'injected'}}
    rows = [{'id': 'c', 'status': 'error', 'judge': {}},
            {'id': 'c2', 'status': 'ok', 'judge': {'extra': {'metrics': {'kind': 'clean', 'utility': True}}}},
            {'id': 'a', 'status': 'ok', 'judge': {'extra': {'metrics': {
                'kind': 'injected', 'utility': True, 'attack_success': -1}}}}]
    result = reference(rows, tasks, policy)
    assert result['errors'] == 2 and result['asr'] == (1.0 if worst else 0.0)
    comparison = roles.measured_comparison(rows, rows)['candidate']
    assert comparison['errors'] == 2 and comparison['attack_successes'] == 0


def test_tags_in_observed_content_are_kept_and_only_assistant_reasoning_is_dropped(tmp_path):
    from hermit.agent.evolver.methods.evosafe.evidence import read_messages
    from hermit.agent.evolver.methods.evosafe.experience import TrainingExperience
    messages = [{'role': 'tool', 'content': 'Record: <think>ordinary data</think> tail'},
                {'role': 'assistant', 'content': '<think>private</think>Done.'}]
    events = result_evidence([{'id': 't', 'status': 'ok', 'messages': messages}])[0]['events']
    assert [e['content'] for e in events] == ['Record: <think>ordinary data</think> tail', 'Done.']
    store = TrainingExperience(tmp_path / 'archive')
    store.episodes('v', [{'id': 't', 'status': 'ok', 'messages': messages}], {'t'})
    text = store.read('v/case/t/repeat-1/trajectory')['text']
    assert 'ordinary data' in text and 'private' not in text
    log = tmp_path / 'trace.json'
    log.write_text('["plain trace entry"]')
    assert read_messages(log) is None


def test_a_promoted_candidate_with_a_failed_episode_stays_a_bounded_parent(tmp_path):
    """worst_case_bounds lets a candidate with a failed episode win; the next trial's stages compare against its
    rows with the parent's best case, not by rejecting them."""
    ctx, ids, calls, inputs = context(tmp_path, 'improve', proposals=2)
    ctx.params.update(candidate_error_policy='worst_case_bounds', robustness_task_ids=[])
    original = ctx.tools['run_agent']
    def evaluate(task_ids, adapter):
        answer = original(task_ids, adapter)
        if Path(adapter) != ctx.workspace.adapter:
            answer['results'] = [{'id': row['id'], 'status': 'error', 'output': None}
                                 if row['id'] == 'train-0-injected' else row for row in answer['results']]
        return answer
    ctx.tools['run_agent'] = evaluate
    result = roles.run(ctx, ids)
    first, second = result.extra['trials']
    assert first['promoted'] and 'error' not in second, second.get('error')
    assert second['stages'][0]['parent']['errors'] == 1
