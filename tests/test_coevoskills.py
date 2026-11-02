"""Fixed common OfficeQA actor, training-only Skill adapter integration."""
from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

import pytest

from hermit.agent.evolver.methods.coevoskills import evolver as E, runtime
from hermit.agent.evolver.methods.coevoskills.docker import command
from hermit.agent.model.backends import Binding, Backend
from hermit.agent.evolver.methods.coevoskills.transport import CLIModel, CONFIG, transcript
from hermit.spec import check_spec

ROOT = Path(__file__).resolve().parents[1]
HERE = ROOT / 'examples/officeqa/coevoskills'
TEMPLATE = ROOT / 'hermit/adapter/templates/coevoskills'
SKILL = '---\nname: evo-example\ndescription: "Reusable workflow"\n---\nRead runtime inputs and compute the requested artifact.\n'


def context(tmp_path):
    from hermit.agent.evolver.base import Access, Progress, StepContext, Workspace
    for name in ('adapter','learning_material','task_info','history','state','scratch','output'):
        (tmp_path/name).mkdir()
    shutil.copytree(TEMPLATE,tmp_path/'adapter',dirs_exist_ok=True)
    ws=Workspace(**{name:tmp_path/name for name in
        ('adapter','learning_material','task_info','history','state','scratch','output')})
    tasks=ws.learning_material/'current/tasks';tasks.mkdir(parents=True)
    (tasks/'a.json').write_text(json.dumps({'id':'a','input':{'question':'public task'},
        'judge':{'feedback':'PRIVATE-GOLD'},'output':'PRIVATE-OUTPUT','trajectory':'PRIVATE-TRACE'}))
    return StepContext(workspace=ws,access=Access(),tools={},params={},progress=Progress(1,1,False),base=tmp_path)


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    sys.modules[name] = value
    spec.loader.exec_module(value)
    return value


def test_actor_identity_is_pinned_without_requiring_git_history(tmp_path, monkeypatch):
    launcher = module(HERE / 'launch.py', '_coevoskills_launcher')
    def no_git(*args, **kwargs):
        raise AssertionError('a source archive has no Git history')
    monkeypatch.setattr(launcher.subprocess, 'check_output', no_git)
    assert len(launcher.immutable_sources()) == 8
    for name in launcher.ACTOR_SHA256:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / name).read_bytes())
    monkeypatch.setattr(launcher, 'ROOT', tmp_path)
    launcher.immutable_sources()
    target.write_bytes(target.read_bytes() + b'\n')
    with pytest.raises(RuntimeError, match='actor/judge source was modified'):
        launcher.immutable_sources()


def test_adapter_contract_and_query_retrieval(tmp_path):
    assert check_spec(TEMPLATE, TEMPLATE).passed
    adapter = tmp_path / 'adapter'
    shutil.copytree(TEMPLATE, adapter)
    for tid, description in [('a', 'public debt ratios'), ('b', 'gold reserves'), ('c', 'public debt ratios')]:
        skill = adapter / f'skills/{tid}/evo-example'
        skill.mkdir(parents=True)
        (skill / 'SKILL.md').write_text(SKILL + description)
        (skill / 'scripts').mkdir()
        (skill / 'scripts/run.py').write_text('raise RuntimeError("reader must never execute me")\n')
    assert check_spec(adapter, TEMPLATE).passed
    reader = module(adapter / 'interface.py', '_common_reader')
    result = reader.skills('public debt', {'id': 'unseen-test-id'})
    assert result['bank_size'] == 3 and len(result['retrieved']) == 2
    assert result['retrieved'][0]['path'].startswith('skills/a/')
    assert reader.skills('')['text'] == ''
    (adapter / 'skills/a/evo-example/SKILL.md').write_text('broken')
    assert not check_spec(adapter, TEMPLATE).passed


@pytest.mark.parametrize('code', [0, 1])
def test_update_only_adapter_files_no_private_material(tmp_path, monkeypatch, code):
    ctx = context(tmp_path)
    monkeypatch.setenv("HERMIT_COEVOSKILLS_RUNTIME", "unit-test-runtime")
    shutil.rmtree(ctx.workspace.adapter)
    shutil.copytree(TEMPLATE, ctx.workspace.adapter)
    ctx = replace(ctx, tool_url='http://127.0.0.1:9', params={
        'upstream_root': str(ROOT.parent / 'CoEvoSkills'), 'task_packages': 'packages',
        'seed': 'seed', 'completion_timeout_s': 600})
    original = (ctx.workspace.adapter / 'ADAPTER.md').read_bytes()
    def run(command, env, ctx, log, cwd):
        raw = Path(command[-1]).read_text()
        assert 'PRIVATE-' not in raw
        request = json.loads(raw)
        assert request['projection_root'].startswith(str(ctx.workspace.scratch))
        out = Path(request['output'])
        skill = out / 'generated/evo-example'
        skill.mkdir(parents=True)
        (skill / 'SKILL.md').write_text(SKILL)
        if code == 0:
            (out / 'result.json').write_text(json.dumps({'skills': str(skill.parent), 'score': 0}))
        return code
    monkeypatch.setattr(E, '_run', run)
    shutil.copytree(TEMPLATE, tmp_path / 'seed')
    E.CoEvoSkillsEvolver().setup(ctx)
    # A later preparation must not alter this run's fixed metadata/probe seed.
    (tmp_path / 'seed/.hermit/EVOLVER.md').write_text('changed after start')
    assert 'changed after start' not in (ctx.workspace.state/'initial-adapter/.hermit/EVOLVER.md').read_text()
    result = E.CoEvoSkillsEvolver().step(ctx)
    assert result.status == ('done' if code == 0 else 'failed')
    assert (ctx.workspace.adapter / 'ADAPTER.md').read_bytes() == original
    assert (ctx.workspace.adapter / 'skills/a/evo-example/SKILL.md').exists() == (code == 0)


def test_oracle_delegates_original_actor_and_does_not_export_judge(tmp_path, monkeypatch):
    pytest.importorskip('harbor')
    from hermit.agent.evolver.methods.coevoskills import oracle
    seed = tmp_path / 'seed'
    shutil.copytree(TEMPLATE, seed)
    skills = tmp_path / 'candidate/evo-example'
    skills.mkdir(parents=True)
    (skills / 'SKILL.md').write_text(SKILL)
    monkeypatch.setattr(oracle, 'CONFIG', {'seed': str(seed), 'projection_root': str(tmp_path / 'probes'),
                                        'task_id': 'train-id'})
    calls = []
    def post(name, payload):
        calls.append((name, payload))
        if name == 'check_spec':
            return {'passed': True}
        return {'results': [{'status': 'ok', 'output': {'answer': 'prediction'}, 'judge': {'score': 0}}]}
    monkeypatch.setattr(oracle, 'post', post)
    assert oracle.probe(skills.parent)['output'] == {'answer': 'prediction'}
    assert [c[0] for c in calls] == ['check_spec', 'run_agent']
    assert calls[1][1]['task_ids'] == ['train-id']
    assert 'judge' not in calls[1][1]
    projected = Path(calls[1][1]['adapter'])
    assert (projected / 'skills/train-id/evo-example/SKILL.md').read_text() == SKILL


def test_gateway_uses_the_configured_backend_and_rejects_other_operations(tmp_path, monkeypatch):
    from hermit.agent.evolver.methods.coevoskills import gateway as gateway_module
    requests = pytest.importorskip('requests')
    binding = Binding(Backend(name='test', base_url='https://model.example/v1',
                              api_key_env='COEVOSKILLS_TEST_API_KEY', apis=('responses',)), 'fixed-model')
    monkeypatch.delenv('COEVOSKILLS_TEST_API_KEY', raising=False)
    forwarded = []
    class Reply:
        status_code = 200
        headers = {'Content-Type':'application/json'}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def iter_content(self, **kwargs): yield b'{"ok":true}'
    def forward(url, **kwargs):
        forwarded.append((url, kwargs))
        return Reply()
    monkeypatch.setattr(requests, 'post', forward)
    gateway = gateway_module.ModelGateway(binding=binding, client_key='test-key',
        log=tmp_path/'egress.jsonl').start()
    def request(path, data=None, key='test-key', method=None):
        headers = {'Authorization':'Bearer '+key, 'Content-Type':'application/json'}
        req = urllib.request.Request(f'http://127.0.0.1:{gateway.port}'+path,
            data=json.dumps(data).encode() if data is not None else None, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req) as reply:
                return reply.status, json.load(reply)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)
    try:
        assert request('/v1/models')[0] == 200
        assert request('/v1/responses', {'model':'fixed-model'})[0] == 503
        assert request('/v1/responses', [])[0] == 400
        assert request('/v1/models', key='wrong')[0] == 403
        assert request('/anything')[0] == 403
        assert request('/v1/chat/completions', {})[0] == 403
        assert request('/v1/responses', {'model':'another-model'})[0] == 403
        assert request('/v1/responses', {'model':'fixed-model', 'tools':[{'type':'web_search'}]})[0] == 403
        assert request('/v1/responses', {'model':'fixed-model', 'tools':[
            {'type':'namespace','tools':[{'type':'web_search'}]}]})[0] == 403
        assert request('/blocked:443', method='CONNECT')[0] == 403
        assert forwarded == []
        monkeypatch.setenv('COEVOSKILLS_TEST_API_KEY', 'host-private-key')
        body = {'model':'fixed-model', 'store':False, 'input':'public task', 'tools':[{'type':'function'}]}
        assert request('/v1/responses', body) == (200, {'ok':True})
        url, options = forwarded.pop()
        assert url == 'https://model.example/v1/responses'
        assert options['headers']['Authorization'] == 'Bearer host-private-key'
        assert options['allow_redirects'] is False
        assert json.loads(options['data']) == body
    finally:
        gateway.close()
    audit = (tmp_path/'egress.jsonl').read_text()
    assert 'test-key' not in audit and 'host-private-key' not in audit and 'public task' not in audit


def test_configs_use_main_actor_and_officeqa_hooks():
    from hermit.config import Config
    from hermit.agent.evolver.loader import find
    for name in ['run_codex.yaml', 'run_codex_luna.yaml', 'smoke_codex.yaml']:
        data = Config.load(HERE / name)
        params = data['agent']['params']
        assert data['agent']['name'] == 'codex'
        assert params['prepare'] == '../utils/task.py:prepare'
        assert params['answer'] == '../utils/task.py:answer'
        assert params['collect'] == '../utils/task.py:collect'
        assert params['web_search'] is False
        assert params['container']['network'] == 'none'
        assert data['judge']['path'] == '../utils/judge.py'
        assert find(data['evolver']['name']).directory == ROOT/'hermit/agent/evolver/methods/coevoskills'
        assert data.template_dir() == TEMPLATE


def test_paired_comparison_requires_all_intended_tasks(tmp_path):
    compare = module(HERE / 'compare.py', '_common_compare').compare
    for label, score in [('none', 0), ('common_seed', 0), ('final', 1)]:
        path = tmp_path / 'eval' / label
        path.mkdir(parents=True)
        (path / 'results.jsonl').write_text(json.dumps({'id': 'a', 'status': 'ok',
                                                      'judge': {'score': score}}) + '\n')
    incomplete = compare(tmp_path, ['a', 'b'])
    assert not incomplete['complete'] and not incomplete['paired']
    assert incomplete['arms']['final']['accuracy'] == .5
    complete = compare(tmp_path, ['a'])
    assert complete['complete']
    assert complete['paired']['none']['improvement_percentage_points'] == 100
    assert complete['paired']['common_seed']['wins'] == 1


def test_frozen_skill_projection_can_be_cleaned_between_steps(tmp_path, monkeypatch):
    pytest.importorskip('harbor')
    from hermit.agent.evolver.methods.coevoskills import oracle
    seed = tmp_path / 'seed'
    shutil.copytree(TEMPLATE, seed)
    skill = tmp_path / 'source/evo-example'
    skill.mkdir(parents=True)
    (skill/'SKILL.md').write_text(SKILL)
    (skill/'SKILL.md').chmod(0o444)
    skill.chmod(0o555)
    monkeypatch.setattr(oracle, 'CONFIG', {'seed': str(seed), 'projection_root': str(tmp_path/'probes'), 'task_id':'a'})
    for _ in range(2):
        projected = oracle.projection(skill.parent)
        assert (projected/'skills/a/evo-example/SKILL.md').read_text() == SKILL
        shutil.rmtree(projected)
    skill.chmod(0o755)


def test_portable_projection_executes_documented_import_in_public_actor_layout(tmp_path):
    """Regression: the fixed actor used to report 'bundled script path unavailable'."""
    from hermit.agent.evolver.methods.coevoskills.packaging import project
    source = tmp_path/'exported/evo-example'
    (source/'scripts').mkdir(parents=True)
    example = ('import sys\n'
               'sys.path.insert(0, "/app/environment/skills/evo-example/scripts")\n'
               'from utils import solve\n'
               'solve("/root/docs", "/root/answer.txt")\n')
    original = SKILL+'\n```python\n'+example+'```\n'
    (source/'SKILL.md').write_text(original)
    (source/'scripts/utils.py').write_text(
        'from pathlib import Path\n'
        'def solve(docs, output):\n'
        '    Path(output).write_text((Path(docs)/"input.txt").read_text().upper())\n')
    from hermit.agent.acting.workspace import deploy
    from hermit.adapter.loader import load_manifest
    from hermit.hosts.inputs import adapter_folder
    adapter = tmp_path/'adapter'
    shutil.copytree(TEMPLATE, adapter)
    work = tmp_path/'actor-work'
    audit = project(source.parent, adapter/'skills/UID0017', 'UID0017')
    deploy(adapter, work, 'codex', context='Fixed test context', serve_tools=False)
    folder = adapter_folder(work, load_manifest(adapter))
    (work/'docs').mkdir()
    (work/'docs/input.txt').write_text('runtime input')
    rewritten = (folder/'skills/UID0017/evo-example/SKILL.md').read_text()
    command = rewritten.split('```python\n')[1].split('```')[0]
    subprocess.run([sys.executable,'-c',command],cwd=folder,check=True)
    assert (work/'skill-answer.txt').read_text()=='RUNTIME INPUT'
    assert (source/'SKILL.md').read_text()==original
    assert sum(f['path_replacements'] for f in audit['files'])==3
    # Similarly named non-fixture paths and domain data must remain untouched.
    (source/'scripts/utils.py').write_text('value="/root/docs_backup"\nnumber=478\n')
    project(source.parent, tmp_path/'other', 'UID0017')
    assert (tmp_path/'other/evo-example/scripts/utils.py').read_text()=='value="/root/docs_backup"\nnumber=478\n'


def test_parallel_assembly_preserves_disjoint_packages_and_training_order(tmp_path):
    parallel = module(HERE/'parallel.py', '_parallel_common')
    output = tmp_path/'run'
    output.mkdir()
    ids = ['a','b']
    seed = tmp_path/'seed'
    shutil.copytree(TEMPLATE,seed)
    for index,tid in enumerate(reversed(ids)):
        shard=f'shard-{index:02}'
        run=output/'training'/shard
        package=run/f'workspace/adapter/skills/{tid}/evo-example'
        package.mkdir(parents=True)
        (package/'SKILL.md').write_text(SKILL+tid)
        row={'tasks':[tid],'step':1,'evolver':{'status':'done'},'commit':'commit-'+tid}
        (run/'steps.jsonl').write_text(json.dumps(row)+'\n')
    records=parallel.training_progress(output,ids,['shard-00','shard-01'])
    assert [r['tasks'][0] for r in records]==ids
    frozen=parallel.assemble(seed,output,ids,records)
    assert sorted(p.name for p in (frozen/'skills').iterdir())==ids
    assert json.loads((output/'frozen-adapter.json').read_text())['packages'][0]['commit']=='commit-a'


def mutate_deployed_skill(path, mutation):
    if mutation == 'modified':
        path.write_text('changed\n')
        return
    if mutation in ('moved', 'symlink'):
        moved = path.with_name('moved.py')
        path.rename(moved)
    else:
        path.unlink()
    if mutation == 'directory':
        path.mkdir()
    elif mutation == 'symlink':
        path.symlink_to(moved)


@pytest.mark.parametrize('mutation', ['modified', 'deleted', 'moved', 'directory', 'symlink'])
def test_final_deployment_audit_uses_main_copy_integrity(tmp_path, mutation):
    from hermit.agent.acting.workspace import deploy
    from hermit.adapter.loader import load_manifest
    from hermit.hosts.inputs import adapter_folder
    parallel = module(HERE/'parallel.py', '_final_integrity')
    adapter = tmp_path/'frozen'
    shutil.copytree(TEMPLATE, adapter)
    p = adapter/'skills/a/evo-example/scripts/utils.py'
    p.parent.mkdir(parents=True)
    p.write_text('def solve(): return 1\n')
    (p.parent.parent/'SKILL.md').write_text(SKILL)
    rollout = tmp_path/'rollout'
    work = rollout/'work'
    work.mkdir(parents=True)
    deployed = deploy(adapter, work, 'codex', context='Fixed context', serve_tools=False)
    log = rollout/'log'; log.mkdir()
    (log/'deployment.json').write_text(json.dumps(deployed))
    results = tmp_path/'results.jsonl'
    results.write_text(json.dumps({'id':'task','status':'ok','log':str(log)})+'\n')
    parallel.audit_final_deployments(adapter, results)
    mutate_deployed_skill(adapter_folder(work, load_manifest(adapter))/p.relative_to(adapter), mutation)
    with pytest.raises(RuntimeError, match='integrity failed'):
        parallel.audit_final_deployments(adapter, results)
    audit = json.loads((tmp_path/'skill-integrity.json').read_text())
    assert audit['issues'][0]['task'] == 'task'


@pytest.mark.parametrize('mutation', [None, 'modified', 'deleted', 'moved', 'directory', 'symlink'])
def test_oracle_checks_the_complete_deployed_skill(tmp_path, monkeypatch, mutation):
    pytest.importorskip('harbor')
    from hermit.agent.acting.workspace import deploy
    from hermit.adapter.loader import load_manifest
    from hermit.hosts.inputs import adapter_folder
    from hermit.agent.evolver.methods.coevoskills import oracle
    seed = tmp_path/'seed'
    shutil.copytree(TEMPLATE, seed)
    source = tmp_path/'source/evo-example'
    script = source/'scripts/solve.py'
    script.parent.mkdir(parents=True)
    script.write_text('def solve(): return 1\n')
    (source/'SKILL.md').write_text(SKILL)
    monkeypatch.setattr(oracle, 'CONFIG', {'seed': str(seed), 'projection_root': str(tmp_path/'probes'),
                                        'task_id': 'a'})
    def post(name, payload):
        if name == 'check_spec':
            return {'passed': True}
        assert name == 'run_agent'
        adapter = Path(payload['adapter'])
        work = tmp_path/'actor/work'
        work.mkdir(parents=True)
        deployed = deploy(adapter, work, 'codex', context='Fixed context', serve_tools=False)
        log = work.parent/'log'
        log.mkdir()
        (log/'deployment.json').write_text(json.dumps(deployed))
        if mutation:
            mutate_deployed_skill(adapter_folder(work, load_manifest(adapter))/'skills/a/evo-example/scripts/solve.py', mutation)
        return {'results': [{'status': 'ok', 'output': {'answer': 'prediction'},
                             'judge': {'score': 1}, 'log': str(log)}]}
    monkeypatch.setattr(oracle, 'post', post)
    if mutation:
        with pytest.raises(RuntimeError, match='integrity failed'):
            oracle.probe(source.parent)
    else:
        assert oracle.probe(source.parent)['judge']['score'] == 1



def test_full_history_and_verifier_independence():
    a = transcript("next", [{"role": "user", "content": "first"},
                            {"role": "assistant", "content": "commands"}])
    b = transcript("verify public artifact", [])
    assert "first" in a and "commands" in a and "next" in a
    assert "first" not in b and "commands\"" not in b





@pytest.mark.parametrize('native_login', [False, True])
def test_cli_transport_uses_main_base_agent_with_full_stdin(tmp_path, monkeypatch, native_login):
    from hermit.agent.base.interface import Result
    from hermit.agent.model.usage import Usage
    from hermit.agent.evolver.methods.coevoskills import transport
    seen = {}
    class Agent:
        def run(self, prompt, **kwargs):
            seen.update(kwargs, prompt=prompt)
            return Result(reply='{"task_complete":true}', exit_code=0,
                          usage=[Usage('fixed-model', input_tokens=10, calls=1)], extra={'served':True})
    def base(name, settings):
        seen.update(name=name, settings=settings)
        return Agent()
    monkeypatch.setattr(transport, 'make', base)
    binding = Binding(Backend(name='relay', base_url='http://127.0.0.1:48157/v1',
        api_key_env='COEVOSKILLS_MODEL_KEY', apis=('responses',)), 'fixed-model')
    monkeypatch.setattr(transport, 'CONFIG', {
        **({'login':'chatgpt', 'model':'fixed-model', 'args':['-c', 'chatgpt_base_url="http://model-relay"'],
            'env':{'CODEX_REFRESH_TOKEN_URL_OVERRIDE':'http://model-relay/oauth/token'}}
           if native_login else {'binding':binding.spec()}), 'container':{'image':'fixed-officeqa','network':'none'},
        'transport_logs':str(tmp_path/'logs')})
    history = [{'role':'user','content':'x' * (1 << 20)}]
    assert json.loads(transport.CLIModel('fixed-model').call('public-only', history))['task_complete']
    assert seen['settings'].get('login') == ('chatgpt' if native_login else None)
    if native_login:
        assert 'binding' not in seen['settings']
        assert seen['settings']['env']['CODEX_REFRESH_TOKEN_URL_OVERRIDE'].endswith('/oauth/token')
    assert seen['name'] == 'codex' and seen['settings']['isolation'] == 'container'
    assert seen['settings']['stdin'] is True and seen['settings']['codex_sandbox'] == 'read-only'
    assert seen['settings']['web_search'] is False and seen['settings']['home_files'] == []
    assert seen['limits'].network == [] and seen['limits'].timeout_s == 600
    assert seen['prompt'] == transcript('public-only', history)
    assert 'features.shell_tool=false' in seen['settings']['args']
    assert list((tmp_path/'logs').glob('*/usage.json'))


@pytest.mark.parametrize('config', [
    {'cli_backend':'codex'},
    {'cli_backend':'codex','binding':{'model':'m'},'container':{'network':'host'}},
])
def test_unrestricted_model_transport_is_rejected(config):
    CONFIG.clear(); CONFIG.update(config)
    with pytest.raises(RuntimeError, match='isolated model gateway'):
        CLIModel('fixed-model')


def test_context_count_uses_the_original_upstream_token_counter(monkeypatch):
    litellm = pytest.importorskip('litellm')
    calls = []
    def count(**kwargs):
        calls.append(kwargs)
        return 17
    monkeypatch.setattr(litellm, 'token_counter', count)
    CONFIG.clear(); CONFIG.update(binding={'model':'fixed-model'}, container={'network':'none'})
    history = [{'role':'user','content':'original history'}]
    assert CLIModel('openai/fixed-model').count_tokens(history) == 17
    assert calls == [{'model':'openai/fixed-model','messages':history}]



def test_original_evolution_methods_are_unchanged_by_environment_seam(tmp_path, monkeypatch):
    import inspect
    pytest.importorskip('harbor')
    if not os.environ.get('COEVOSKILLS_ROOT'):
        pytest.skip('set COEVOSKILLS_ROOT for the pinned upstream integration check')
    from hermit.agent.evolver.methods.coevoskills.upstream import checkout
    checkout(os.environ['COEVOSKILLS_ROOT'])
    from libs.terminus_agent.agents.terminus_2 import harbor_terminus_2_evolution as upstream
    from hermit.agent.evolver.methods.coevoskills import strict_runtime, oracle
    cls=upstream.HarborTerminus2Evolution
    before={name: value for name,value in vars(cls).items() if inspect.isfunction(value)}
    monkeypatch.setattr(strict_runtime,'settings',lambda:{
        'backend':'codex','model':'fixed-model','effort':'medium','transport':{}})
    original_environment=upstream.DockerEnvironment
    try:
        strict_runtime.configure({'cli_backend':'codex','model_name':'fixed-model','effort':'medium'})
        assert upstream.HarborTerminus2Evolution is cls
        assert {name:value for name,value in vars(cls).items() if inspect.isfunction(value)}==before
        assert upstream.DockerEnvironment is strict_runtime.ModelOnlyDockerEnvironment
        monkeypatch.setenv('COEVOSKILLS_CUSTOM_AGENT_IMPORT_PATHS',json.dumps({
            'hermit-officeqa':'hermit.agent.evolver.methods.coevoskills.oracle:FixedActorOracle'}))
        subject=object.__new__(cls)
        callback=subject._create_oracle_agent('hermit-officeqa',tmp_path,'fixed-model')
        assert isinstance(callback,oracle.FixedActorOracle)
    finally:
        upstream.DockerEnvironment=original_environment


# Docker seams specific to the pinned CoEvoSkills integration.
@pytest.fixture
def docker_runtime():
    return {'docker_bin': '/usr/bin/docker', 'docker_image': 'fixed-officeqa', 'model_port': 48157,
            'socket': '/tmp/model.sock', 'relay': '/repo/sandbox_relay.py'}


@pytest.mark.parametrize('network', ['none', 'host', 'bridge', 'old-internal'])
def test_original_actor_command_only_gets_offline_model_channel(docker_runtime, network):
    actor = ['sh', '-c', 'exec codex "$@"', 'sh', 'exec', '--json', '-m', 'fixed-model']
    original = ['run', '--rm', '--network', network, '--user', '1000:1000', '--memory', '6g',
                '--cap-add', 'SYS_ADMIN', '--mount', 'type=bind,src=/tmp/work,dst=/tmp/work',
                'fixed-officeqa', *actor]
    result = command(original, docker_runtime)
    assert result[result.index('--network')+1] == 'none'
    assert result.count('--network') == 1
    assert result[result.index('--pull')+1] == 'never'
    assert result[result.index('--user')+1] == '1000:1000'
    assert result[result.index('--memory')+1] == '6g'
    assert result[result.index('--')+1:] == actor
    assert '/hermit-model-relay.py' in result and '48157=/hermit-model.sock' in result
    assert original[original.index('--network')+1] == network


@pytest.mark.parametrize('options', [
    ['--privileged'], ['--pid', 'host'], ['--cap-add', 'NET_ADMIN'],
    ['--publish', '8000:8000'], ['--volume', '/:/host'],
    ['--mount', 'type=bind,src=/var/run/docker.sock,dst=/docker.sock'],
    ['--mount', 'type=bind,src=/proc,dst=/host-proc'], ['--entrypoint', 'other'],
])
def test_container_escape_options_fail_closed(docker_runtime, options):
    with pytest.raises(ValueError):
        command(['run', *options, 'fixed-officeqa', 'codex'], docker_runtime)


def test_unexpected_image_fails_without_calling_docker(docker_runtime):
    with pytest.raises(ValueError, match='original OfficeQA image'):
        command(['run', 'other-image', 'codex'], docker_runtime)
    assert command(['image', 'inspect', 'fixed-officeqa'], docker_runtime) == [
        '/usr/bin/docker', 'image', 'inspect', 'fixed-officeqa']


@pytest.mark.parametrize('native_login', [False, True])
def test_shared_runtime_restores_host_state_and_never_uploads_credentials(tmp_path, monkeypatch, native_login):
    monkeypatch.setenv('COEVOSKILLS_TEST_API_KEY', 'host-api-key')
    binding = Binding(Backend(name='test', base_url='https://model.example/v1',
                              api_key_env='COEVOSKILLS_TEST_API_KEY', apis=('responses',)), 'fixed-model')
    monkeypatch.setenv('COEVOSKILLS_MODEL_KEY', 'original-key')
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'host-api-key')
    monkeypatch.delenv('HERMIT_COEVOSKILLS_RUNTIME', raising=False)
    original_path = os.environ['PATH']
    monkeypatch.setattr(runtime, 'binary', lambda name, configured=None: '/installed/' + name)
    class Gateway:
        port = 23456
        def __init__(self, **kwargs):
            assert kwargs['binding'] is (None if native_login else binding)
            if native_login: assert kwargs['login'] == 'chatgpt' and kwargs['model'] == 'fixed-model'
        def start(self): return self
        def close(self): pass
    class Bridges:
        def socket_for(self, port): return tmp_path / 'model.sock'
        def close(self): pass
    monkeypatch.setattr(runtime, 'ModelGateway', Gateway)
    monkeypatch.setattr(runtime, '_Bridges', Bridges)
    with pytest.raises(RuntimeError, match='cancel'):
        with runtime.model_runtime(binding=None if native_login else binding,
                                   login='chatgpt' if native_login else None, model='fixed-model', effort='medium',
                                   output=tmp_path, docker_image='sha256:prepared') as (channel, binding):
            assert channel['docker_image'] == 'sha256:prepared'
            assert channel['transport']['container']['image'] == 'sha256:prepared'
            if native_login:
                assert binding is None
                assert channel['transport']['login'] == 'chatgpt'
                assert 'model_providers.coevoskills_login.requires_openai_auth=true' in channel['transport']['args']
                assert 'binding' not in channel['transport']
            else:
                assert binding.base_url == 'http://127.0.0.1:48157/v1'
            assert channel['docker_bin'] == '/installed/docker'
            assert 'HOST_PRIVATE_AUTH' not in json.dumps(channel)
            assert 'host-api-key' not in json.dumps(channel)
            assert Path(os.environ['PATH'].split(os.pathsep)[0], 'docker').is_file()
            raise RuntimeError('cancel')
    assert os.environ['COEVOSKILLS_MODEL_KEY'] == 'original-key'
    assert os.environ['ANTHROPIC_API_KEY'] == 'host-api-key'
    assert os.environ['PATH'] == original_path
    assert 'HERMIT_COEVOSKILLS_RUNTIME' not in os.environ


def test_evolution_fixture_has_no_network_even_if_task_requests_one(tmp_path, monkeypatch):
    import asyncio
    import yaml
    pytest.importorskip('harbor')
    from harbor.environments.docker.docker import DockerEnvironment
    from harbor.models.trial.paths import TrialPaths
    from harbor.models.task.config import EnvironmentConfig
    from hermit.agent.evolver.methods.coevoskills import strict_runtime
    environment = tmp_path/'environment';environment.mkdir()
    (environment/'Dockerfile').write_text('FROM fixed-officeqa\n')
    paths=TrialPaths(trial_dir=tmp_path/'trial');paths.mkdir()
    monkeypatch.setattr(strict_runtime,'settings',lambda:{'model_port':48157,'socket':'/model.sock','relay':'/relay.py'})
    async def start(self, **kwargs): pass
    monkeypatch.setattr(DockerEnvironment,'start',start)
    fixture=strict_runtime.ModelOnlyDockerEnvironment(environment_dir=environment,
        environment_name='officeqa',session_id='unit-test',trial_paths=paths,
        task_env_config=EnvironmentConfig(allow_internet=True,docker_image='fixed-officeqa'))
    asyncio.run(fixture.start())
    main=yaml.safe_load((environment/'docker-compose.yaml').read_text())['services']['main']
    assert main['network_mode']=='none' and main['image']=='fixed-officeqa'
    assert not {'ports','privileged','networks'} & main.keys()
    assert set(main['environment'])=={'TEST_DIR','COEVOSKILLS_MODEL_KEY'}


def test_fixture_cleanup_never_removes_prepared_images(tmp_path, monkeypatch):
    import asyncio
    pytest.importorskip('harbor')
    from harbor.environments.docker.docker import DockerEnvironment
    from harbor.models.task.config import EnvironmentConfig
    from harbor.models.trial.paths import TrialPaths
    from hermit.agent.evolver.methods.coevoskills.strict_runtime import ModelOnlyDockerEnvironment
    env=tmp_path/'environment';env.mkdir();(env/'Dockerfile').write_text('FROM fixed-officeqa\n')
    paths=TrialPaths(trial_dir=tmp_path/'trial');paths.mkdir()
    fixture=ModelOnlyDockerEnvironment(environment_dir=env,environment_name='officeqa',
        session_id='cleanup-test',trial_paths=paths,task_env_config=EnvironmentConfig())
    calls=[]
    async def execute(self, **kwargs):return None
    async def compose(self, command, **kwargs):calls.append(command)
    monkeypatch.setattr(DockerEnvironment,'exec',execute)
    monkeypatch.setattr(ModelOnlyDockerEnvironment,'_run_docker_compose_command',compose)
    asyncio.run(fixture.stop(delete=True))
    assert calls==[['down','--volumes','--remove-orphans']]


@pytest.fixture
def officeqa_preparation(tmp_path, monkeypatch):
    pytest.importorskip('skillopt')
    import hermit.task
    from skillopt.envs.officeqa import tool_runtime
    prepare = module(HERE / 'prepare.py', '_coevoskills_prepare')
    utils = tmp_path / 'utils'
    (utils / 'splits').mkdir(parents=True)
    (utils / 'task-info').mkdir()
    docs = tmp_path / 'corpus'
    docs.mkdir()
    (docs / 'source.txt').write_text('PUBLIC CORPUS')
    answers = tmp_path / 'answers.jsonl'
    answers.write_text(''.join(json.dumps({'id': tid, 'answer': 'PRIVATE-' + tid}) + '\n'
                               for tid in ('train-a', 'val-a', 'test-a')))
    (utils / 'data.json').write_text(json.dumps({'docs': str(docs), 'answers': str(answers)}))
    (utils / 'tasks.jsonl').write_text(''.join(json.dumps({'id': tid, 'question': 'PUBLIC-' + tid}) + '\n'
                                         for tid in ('train-a', 'val-a', 'test-a')))
    (utils / 'splits/train.txt').write_text('train-a\n')
    (utils / 'task-info/README.md').write_text('PUBLIC WORKFLOW')
    monkeypatch.setattr(prepare, 'UTILS', utils)
    monkeypatch.setattr(tool_runtime, 'resolve_docs_roots', lambda _: [docs])
    monkeypatch.setattr(hermit.task, 'load_callable', lambda *_: lambda row, _: (row['question'], []))
    return prepare, utils, tmp_path / 'prepared'


def test_preparation_stages_only_training_fixtures_without_duplicate_dataset(officeqa_preparation):
    prepare, _, output = officeqa_preparation
    assert prepare.prepare(output, build_image=False) == 1
    assert sorted(p.name for p in (output / 'packages').iterdir()) == ['train-a']
    package = output / 'packages/train-a'
    assert (package / 'task.toml').read_text().find('benchmark = "officeqa"') >= 0
    assert (package / 'instruction.md').read_text().startswith('PUBLIC-train-a\n')
    assert 'Evolution fixture interface:' in (package / 'instruction.md').read_text()
    assert (package / 'environment/doc/runtime-policy.md').is_file()
    assert json.loads((package / 'tests/answer.json').read_text()) == 'PRIVATE-train-a'
    public = [output / 'image', package / 'environment']
    assert all('PRIVATE-' not in path.read_text() for root in public for path in root.rglob('*') if path.is_file())
    assert not (output / 'tasks.jsonl').exists()
    assert json.loads((output / 'revision.json').read_text())['training_ids'] == ['train-a']


@pytest.mark.parametrize('ids', ['train-a\ntrain-a\n', 'unknown\n'])
def test_preparation_rejects_invalid_training_split_before_staging(officeqa_preparation, ids):
    prepare, utils, output = officeqa_preparation
    (utils / 'splits/train.txt').write_text(ids)
    with pytest.raises(ValueError, match='duplicate or unknown OfficeQA IDs'):
        prepare.prepare(output, build_image=False)
    assert not output.exists()


def test_worker_rejects_skillsbench_tasks_before_any_trial(tmp_path, monkeypatch):
    import asyncio
    pytest.importorskip('toml')
    pytest.importorskip('harbor')
    from hermit.agent.evolver.methods.coevoskills import strict_runtime, worker
    source = tmp_path / 'task'
    source.mkdir()
    (source / 'task.toml').write_text('[metadata]\nbenchmark = "skillsbench"\n')
    monkeypatch.setattr(worker, 'checkout', lambda _: tmp_path / 'pinned')
    monkeypatch.setattr(strict_runtime, 'configure', lambda _: None)
    monkeypatch.setenv('SKILLSBENCH_ROOT', 'previous-value')
    output = tmp_path / 'result'
    request = {'params': {'upstream_root': 'pinned'}, 'task_path': str(source), 'output': str(output)}
    with pytest.raises(ValueError, match='only prepared OfficeQA fixtures'):
        asyncio.run(worker.run(request))
    assert not (output / 'evolution').exists()


@pytest.mark.parametrize('native_login,config_style', [(False, 'layered'), (True, 'layered'), (True, 'flat'), (True, 'env')])
@pytest.mark.parametrize('train_workers', [1, 4])
@pytest.mark.parametrize('tampered', [False, True])
def test_launcher_binds_main_actor_to_the_scoped_relay(
        tmp_path, monkeypatch, native_login, config_style, train_workers, tampered):
    from contextlib import contextmanager
    from types import SimpleNamespace
    from hermit.agent.acting.workspace import deploy
    from hermit.adapter.loader import load_manifest
    from hermit.hosts.inputs import adapter_folder
    launcher = module(HERE/'launch.py', '_coevoskills_binding_launcher')
    parallel = module(HERE/'parallel.py', '_coevoskills_binding_parallel')
    example = tmp_path/'coevoskills'
    example.mkdir()
    config = example/'run.yaml'
    config.write_text((HERE/('run_codex_luna.yaml' if native_login else 'run_codex.yaml')).read_text())
    if config_style in ('flat', 'env'):
        import yaml
        from hermit.config import Config
        raw = yaml.safe_load(config.read_text())
        raw['agent'] = Config.load(config)['agent']
        if config_style == 'env':
            raw['agent']['params'].pop('container')
            monkeypatch.setenv('HERMIT_CODEX_IMAGE', 'hermit-codex:0.162.0')
            monkeypatch.setenv('HERMIT_CODEX_NETWORK', 'none')
        config.write_text(yaml.safe_dump(raw))
    (tmp_path/'utils/splits').mkdir(parents=True)
    (tmp_path/'utils/splits/test.txt').write_text('test-a\n')
    shutil.copytree(TEMPLATE, example/'common_seed')
    frozen = tmp_path/'frozen/final'
    shutil.copytree(TEMPLATE, frozen)
    script = frozen/'skills/a/evo-example/scripts/utils.py'
    script.parent.mkdir(parents=True)
    script.write_text('def solve(): return 1\n')
    (script.parent.parent/'SKILL.md').write_text(SKILL)
    monkeypatch.setattr(launcher, 'HERE', example)
    image_tag = 'hermit-codex:0.162.0'
    image_id = 'sha256:' + 'a' * 64
    images = {image_tag: image_id}
    inspected = []
    def inspect(command, **kwargs):
        assert command == ['docker', 'image', 'inspect', '--format', '{{.Id}}', image_tag]
        inspected.append(command[-1])
        return images[command[-1]] + '\n'
    monkeypatch.setattr(launcher.subprocess, 'check_output', inspect)
    inside = Binding(Backend(name='coevoskills_model', base_url='http://127.0.0.1:48157/v1',
        api_key_env='COEVOSKILLS_MODEL_KEY', apis=('responses',)), 'gpt-5.6-luna')
    @contextmanager
    def runtime(**kwargs):
        if native_login:
            assert kwargs['binding'] is None and kwargs['login'] == 'chatgpt'
        else:
            assert kwargs['binding'].api_key_env == 'OPENAI_API_KEY'
        assert kwargs['docker_image'] == image_id
        yield {'transport': {'args':['-c','chatgpt_base_url="http://local-relay"'],
                             'env':{'CODEX_REFRESH_TOKEN_URL_OVERRIDE':'http://local-relay/oauth/token'}}}, None if native_login else inside
    monkeypatch.setattr(launcher, 'model_runtime', runtime)
    monkeypatch.syspath_prepend(str(HERE))
    def train_parallel(config, *args):
        assert config['agent']['params']['container']['image'] == image_id
        return {}, frozen
    monkeypatch.setitem(sys.modules, 'parallel', SimpleNamespace(
        fingerprints=lambda: {}, verify_frozen=lambda _: None,
        train_parallel=train_parallel,
        audit_final_deployments=parallel.audit_final_deployments))
    exports = []
    class AuditTree:
        def __init__(self, path):
            assert path == tmp_path/'tree'
        def export(self, commit, target):
            exports.append(commit)
            shutil.copytree(frozen, target)
            return target
    monkeypatch.setattr(launcher, 'Tree', AuditTree)
    seen = []
    def evaluate(config, selector):
        params = config['agent']['params']
        assert config['agent']['name'] == 'codex'
        assert config['agent']['concurrency'] == 4
        if native_login:
            assert params['login'] == 'chatgpt' and not params.get('binding')
            assert 'chatgpt_base_url="http://local-relay"' in params['args']
            assert params['env']['CODEX_REFRESH_TOKEN_URL_OVERRIDE'].endswith('/oauth/token')
        else:
            assert params['binding']['base_url'] == inside.base_url
            assert params['binding']['api_key_env'] == 'COEVOSKILLS_MODEL_KEY'
        from hermit.agent.base.container import settings as container_settings
        assert container_settings(params)['network'] == 'none'
        assert params['container']['image'] == image_id
        # Re-tagging during the first baseline cannot change subsequent sessions.
        images[image_tag] = 'sha256:' + 'b' * 64
        assert 'OPENAI_API_KEY' not in ' '.join(params['args'])
        seen.append(selector)
        if len(seen) == 3:
            assert selector == ('final' if train_workers == 1 else str(frozen))
            work = tmp_path/'actor/work'
            deployed = deploy(frozen, work, 'codex', context='Fixed context', serve_tools=False)
            log = work.parent/'log'
            log.mkdir()
            (log/'deployment.json').write_text(json.dumps(deployed))
            results = config.run_dir()/'eval/final/results.jsonl'
            results.parent.mkdir(parents=True)
            results.write_text(json.dumps({'id':'test-a', 'status':'ok', 'log':str(log),
                                           'judge':{'score':0}})+'\n')
            if tampered:
                (adapter_folder(work, load_manifest(frozen))/script.relative_to(frozen)).write_text('changed\n')
            return {'mean_score':0, 'commit':'evaluated-commit' if train_workers == 1 else None}
        return {'mean_score':0, 'commit':None}
    monkeypatch.setattr(launcher, 'evaluate', evaluate)
    def trainer(config):
        assert config['agent']['params']['container']['image'] == image_id
        return SimpleNamespace(train=lambda: {'tree': str(tmp_path/'tree'), 'final_commit': 'training-commit'})
    monkeypatch.setattr(launcher, 'Trainer', trainer)
    args = SimpleNamespace(config=config, output=tmp_path/'run', train_workers=train_workers,
                           eval_concurrency=4, train_only=False)
    if tampered:
        with pytest.raises(RuntimeError, match='integrity failed'):
            launcher.run(args)
        assert not (args.output/'comparison.json').exists()
    else:
        launcher.run(args)
        assert (args.output/'comparison.json').exists()
    audit = json.loads((args.output/'coevoskills/eval/final/skill-integrity.json').read_text())
    assert audit['checked_tasks'] == 1
    assert bool(audit['issues']) == tampered
    assert len(seen) == 3
    assert inspected == [image_tag]
    assert images[image_tag] != image_id
    assert exports == (['evaluated-commit'] if train_workers == 1 else [])
    assert script.read_text() == 'def solve(): return 1\n'


def test_native_login_relay_preserves_requests_and_rejects_web_access(tmp_path, monkeypatch):
    from hermit.agent.evolver.methods.coevoskills.gateway import ModelGateway
    requests = pytest.importorskip('requests')
    forwarded = []
    class Reply:
        status_code = 200
        headers = {'Content-Type':'application/json'}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def iter_content(self, **kwargs): yield b'{"ok":true}'
    def forward(url, **kwargs):
        forwarded.append((url, kwargs))
        return Reply()
    monkeypatch.setattr(requests, 'post', forward)
    monkeypatch.setattr(requests, 'get', forward)
    gateway = ModelGateway(login='chatgpt', model='fixed-model', log=tmp_path/'egress.jsonl').start()
    def request(path, data=None, method=None):
        raw = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(f'http://127.0.0.1:{gateway.port}'+path, data=raw,
            headers={'Authorization':'Bearer private-access', 'ChatGPT-Account-Id':'private-account',
                     'Content-Type':'application/json'}, method=method)
        try:
            with urllib.request.urlopen(req) as response: return response.status
        except urllib.error.HTTPError as exc: return exc.code
    try:
        assert request('/v1/models?client_version=0.162.0') == 200
        assert forwarded.pop()[0] == 'https://chatgpt.com/backend-api/codex/models?client_version=0.162.0'
        assert request('/backend-api/wham/accounts/check') == 403
        for route in ['/v1/responses', '/v1/responses/compact']:
            body = {'model':'fixed-model', 'input':'public-input', 'store':False}
            assert request(route, body) == 200
            url, options = forwarded.pop()
            assert url == 'https://chatgpt.com/backend-api/codex' + route[3:]
            assert options['data'] == json.dumps(body).encode()
            assert options['headers']['Authorization'] == 'Bearer private-access'
            assert options['allow_redirects'] is False
        refresh = {'grant_type':'refresh_token','refresh_token':'private-refresh','client_id':'native-client'}
        assert request('/oauth/token', refresh) == 200
        assert forwarded.pop()[0] == 'https://auth.openai.com/oauth/token'
        assert request('/oauth/token', {'grant_type':'password'}) == 400
        assert request('/v1/responses', {'model':'another-model'}) == 403
        assert request('/v1/responses', {'model':'fixed-model','tools':[{'type':'web_search'}]}) == 403
        assert request('/backend-api/codex/usage') == 403
        assert request('/anything') == 403
        assert request('/example.com:443', method='CONNECT') == 403
        assert not forwarded
    finally:
        gateway.close()
    audit = (tmp_path/'egress.jsonl').read_text()
    assert not any(secret in audit for secret in ['private-access','private-account','private-refresh','public-input'])
