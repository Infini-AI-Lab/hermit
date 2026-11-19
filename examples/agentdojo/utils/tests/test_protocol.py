"""Frozen AgentDojo protocol: outcome-free source import, grouped ownership and budgets.

Synthetic native-log-shaped rows stand in for the recorded collection. Selection
uses explicit no-op eligibility decisions; model outcomes are deliberately varied.
No model, network, external source checkout or Docker is used in these checks.
"""
import json
from pathlib import Path

import pytest

from examples.agentdojo.utils.prepare_benchmark import SUITES, digest, read_collection, select
from hermit.config import Config

ROOT = Path(__file__).resolve().parents[4]


def inventory():
    cases = {}
    for suite in SUITES:
        for task in range(12):
            for goal in range(4):
                row = {'suite': suite, 'user_task_id': f'user_task_{task}',
                       'injection_task_id': f'injection_task_{goal}',
                       'injections': {'vector': f'frozen-{goal}'}}
                cases[digest(row)] = row
    return cases


def test_development_budgets_and_full_test_variants_have_disjoint_group_owners():
    cases = inventory()
    audit = {key: {'eligible': True} for key in cases}
    rows, splits, assignments, _ = select(cases, audit)
    assert len(splits['train']) == 57 and len(splits['val']) == 54
    owners = {}
    for split, ids in splits.items():
        members = [row for row in rows if row['id'] in ids]
        assert {row['suite'] for row in members} == set(SUITES)
        owners[split] = {row['group'] for row in members}
        for group in owners[split]:
            grouped = [row for row in members if row['group'] == group]
            assert len(grouped) == (5 if split == 'test' else 3)
            assert sum(row['kind'] == 'clean' for row in grouped) == 1
    assert not (owners['train'] & owners['val'] or owners['train'] & owners['test'] or owners['val'] & owners['test'])
    assert len(assignments) == 48
    assert {row['suite'] for row in rows[:12]} == set(SUITES)
    assert select(dict(reversed(list(cases.items()))), audit) == (rows, splits, assignments, select(cases, audit)[3])


def test_old_model_scores_never_change_imported_case_content(tmp_path):
    for suite in SUITES:
        folder = tmp_path / suite
        folder.mkdir()
        row = {'suite_name': suite, 'user_task_id': 'user_task_0', 'injection_task_id': 'injection_task_0',
               'injections': {'vector': 'frozen'}, 'attack_type': 'tool_knowledge',
               'utility': False, 'security': True, 'messages': ['old answer']}
        path = folder / 'log.json'
        path.write_text(json.dumps(row))
    original, _ = read_collection(tmp_path)
    for path in tmp_path.rglob('*.json'):
        row = json.loads(path.read_text()); row.update(utility=True, security=False, messages=['different old answer'])
        path.write_text(json.dumps(row))
    updated, _ = read_collection(tmp_path)
    assert original == updated
    assert all(set(row) == {'suite', 'user_task_id', 'injection_task_id', 'injections'} for row in updated.values())


def test_noop_ineligible_groups_are_excluded_before_split_ownership():
    cases = inventory()
    audit = {key: {'eligible': row['user_task_id'] != 'user_task_0'} for key, row in cases.items()}
    rows, _, assignments, _ = select(cases, audit)
    assert all(row['user_task_id'] != 'user_task_0' for row in rows)
    assert len(assignments) == 44


@pytest.mark.parametrize('banking_development_groups', [2, 3])
def test_a_group_with_one_eligible_goal_is_reserved_for_test(banking_development_groups):
    cases = inventory()
    sparse_group = 'banking/user_task_3'
    audit = {}
    for key, row in cases.items():
        if row['suite'] == 'banking' and row['user_task_id'] == 'user_task_3':
            eligible = row['injection_task_id'] == 'injection_task_0'
        else:
            count = banking_development_groups if row['suite'] == 'banking' else 3
            eligible = row['user_task_id'] in {f'user_task_{task}' for task in range(count)}
        audit[key] = {'eligible': eligible}

    rows, splits, assignments, _ = select(cases, audit, train_groups=4, val_groups=4)

    assert {'suite': 'banking', 'user_task_id': 'user_task_3', 'split': 'test'} in assignments
    assert len(splits['train']) == len(splits['val']) == 12
    group_rows = [row for row in rows if row['group'] == sparse_group]
    assert [row['kind'] for row in group_rows] == ['clean', 'injected']
    assert group_rows[1]['injection_task_id'] == 'injection_task_0'
    assert {row['id'] for row in group_rows} <= set(splits['test'])


@pytest.mark.parametrize('method', ['she', 'evosafe'])
def test_comparison_recipes_share_victim_and_test_data_while_retaining_method_selection(method):
    config = Config.load(ROOT / f'examples/agentdojo/{method}/run_nemotron_codex.yaml')
    assert config['agent']['params']['model']['model'] == 'nvidia/nvidia/nemotron-3.5-lightning'
    assert config['agent']['params']['benchmark'] == 'agentdojo'
    assert config['agent']['params']['max_iters'] == 25
    assert config['data']['step_size'] == 57
    assert config.path(config['data']['test']) == ROOT / 'examples/agentdojo/utils/data/comparison-v1/test.txt'
    assert not config['data'].get('val')
    assert config['evolver']['params']['backend'] == 'codex'
    if method == 'she':
        assert config['agent']['params']['hook_runtime'] == 'she'
        assert config['trainer']['epochs'] == 20
        assert config['evolver']['params']['gate_mode'] == 'initial_ua_floor'
    else:
        assert config['evolver']['params']['max_proposals'] == 3
        ids = config['evolver']['params']['eval_task_ids']
        assert len(ids) == 57 and len(set(ids)) == 57
        assert config['evolver']['params']['gate_mode'] == 'source'


def test_experiment_cli_loads_the_shared_supervisor_from_another_directory(tmp_path):
    import subprocess
    import sys
    result = subprocess.run([sys.executable, str(ROOT / 'examples/agentdojo/utils/experiment.py'), '--help'],
                            cwd=tmp_path, capture_output=True, text=True, check=True)
    assert '--config' in result.stdout and '--no-sandbox' in result.stdout
