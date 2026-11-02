"""Common OfficeQA: fixed actor, unchanged upstream loop, learned adapter only."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from hermit.config import Config
from hermit.evaluate import evaluate
from hermit.trainer.trainer import Trainer
from hermit.tree import Tree
from hermit.agent.model.backends import Binding
from hermit.agent.base.container import settings as container_settings
from hermit.agent.evolver.methods.coevoskills.runtime import model_runtime

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]


# Content pins preserve the exact original actor/judge without requiring Git history.
ACTOR_SHA256 = {
    'hermit/agent/acting/workspace.py': 'a2283f28665c26a0e84ce26a9d746fd8ba63f4c7ee88dec4475a5aa3d75fd265',
    'hermit/agent/base/cli.py': '7c120be60329d294577e42555f20b97726db6229fa3f0c92be218f18f9545401',
    'hermit/agent/base/container.py': 'b93e2c2825ce21eeda5d8db637fd73f33282f164a52de295e0f11f20f2d118e1',
    'hermit/hosts/codex.py': '7e2ba8f5968293d2bae980f5cb79343d5fd5db11c4cb5e1c5c9dac868d753045',
    'hermit/hosts/inputs.py': '31f3cdf8e83474302603d78cac221bf083604bf2e667fdb28191b790f360abac',
    'examples/officeqa/utils/task.py': '70a9c95906a1efc8b9b91e62a61103bef211014979ff57eae211134e55859958',
    'examples/officeqa/utils/prompt.md': '1fb9d223ab879c953944c6246db34e76e3a0863a47203b785aea5cf2f86622fc',
    'examples/officeqa/utils/judge.py': '6f6efce9284026d1edebf00084d70d21e83206db188d5be2e821e4f9c2307c63',
}


def immutable_sources():
    for name, expected in ACTOR_SHA256.items():
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != expected:
            raise RuntimeError(f"fixed OfficeQA actor/judge source was modified: {name}")
    return dict(ACTOR_SHA256)


def configured_model(config):
    params = config['agent']['params']
    return (params.get('binding') or {}).get('model') or params['model']


def run(args):
    initial = Config.load(args.config.resolve())
    model = configured_model(initial)
    if initial['agent']['name'] != 'codex' or initial['evolver']['name'] != 'coevoskills':
        raise ValueError('the original common actor and pinned evolution integration are required')
    params = initial['agent']['params']
    hooks = {'prompt': '../utils/prompt.md', 'prepare': '../utils/task.py:prepare',
             'answer': '../utils/task.py:answer', 'collect': '../utils/task.py:collect'}
    if any(params.get(key) != value for key, value in hooks.items()):
        raise ValueError("use main's unchanged OfficeQA task functions")
    if params.get('web_search') is not False or params.get('isolation') != 'container':
        raise ValueError('the fixed actor requires offline container isolation and web_search: false')
    if initial['judge']['path'] != '../utils/judge.py':
        raise ValueError('the original OfficeQA judge is required')
    if initial['evolver']['params'].get('model_name', model).removeprefix('openai/') != model:
        raise ValueError('actor and evolver must use the same configured model')
    params = initial['evolver']['params']
    if params.get('cli_backend', 'codex') != 'codex':
        raise ValueError('the fixed OfficeQA actor requires Codex')
    acting_effort = initial['agent']['params'].get('effort', 'medium')
    if params.get('effort', acting_effort) != acting_effort:
        raise ValueError('acting and learning effort must match the fixed run settings')
    container = container_settings(initial['agent']['params'])
    if container['network'] != 'none':
        raise ValueError('CoEvoSkills sessions require Docker network none')
    image_id = subprocess.check_output(
        ['docker', 'image', 'inspect', '--format', '{{.Id}}', container['image']], text=True).strip()
    runtime = {'image': container['image'], 'image_id': image_id, 'network': 'none'}
    from hermit.adapter.manifest import read_manifest
    if read_manifest(HERE / 'common_seed')['template'] != read_manifest(initial.template_dir())['template']:
        raise RuntimeError('prepare_seed.py must prepare the current coevoskills seed before execution')
    actor_identity = immutable_sources()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    (output / "launcher.json").write_text(json.dumps({"pid": os.getpid(), "script": "launch.py", "output": str(output)}))
    seed = output / "common_seed"
    shutil.copytree(HERE / "common_seed", seed)
    saved_env = {'HA_BACKENDS': os.environ.get('HA_BACKENDS')}
    try:
        with model_runtime(binding=Binding.from_spec(initial['agent']['params']['binding'])
                           if initial['agent']['params'].get('binding') else None,
                           login=initial['agent']['params'].get('login'), model=model,
                           effort=initial['agent']['params'].get('effort', 'medium'),
                           output=output, docker_image=runtime['image_id']) as (channel, binding):
            return execute(args, initial, output, seed, actor_identity, runtime, binding, channel)
    finally:
        for name, value in saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def execute(args, initial, output, seed, actor_identity, runtime, binding, channel):
    model = configured_model(initial)
    # Normalize the relay through main's model-binding support for both layers.
    if binding:
        backend_file = output / "backends.json"
        backend_file.write_text(json.dumps({binding.backend.name: binding.backend.to_dict()}))
        os.environ['HA_BACKENDS'] = str(backend_file)
    overrides = [
        *([f'agent.model.ref={binding.ref}'] if binding else []),
        f"evolver.params.model_name={model}",
        f"evolver.params.effort={initial['agent']['params'].get('effort', 'medium')}",
        f"trainer.output_dir={output}", f"trainer.tree_dir={output/'trees/coevoskills'}",
        "trainer.run_name=coevoskills",
        f"start_from={seed}", f"evolver.params.seed={seed}",
    ]
    config = Config.load(args.config.resolve(), overrides)
    params = config['agent']['params']
    params['container'] = {**(params.get('container') or {}), 'image': runtime['image_id']}
    config['agent']['concurrency'] = args.eval_concurrency
    if not binding:
        params = config['agent']['params']
        params['args'] = [*params.get('args', []), *channel['transport']['args']]
        params['env'] = {**params.get('env', {}), **channel['transport']['env']}
    (output / "config.json").write_text(json.dumps(config.data, indent=2))
    (output / "identity.json").write_text(json.dumps({
        "actor_and_judge_sha256": actor_identity, "runtime": runtime,
        "model": model, "login": initial["agent"]["params"].get("login"),
        "protocol": "50/24/172 frozen-adapter train/test",
        "upstream_commit": "4380d4bff673dd6e1d58e5babeb2aaa0fe527119",
        "upstream_evolution_algorithm_modified": False,
        "actor_network": "none; model-only Unix socket", "generator_network": "none",
        "learner_cli_network": "none; model-only Unix socket",
        "training_workers": args.train_workers, "evaluation_concurrency": args.eval_concurrency,
        "portability_projection": "deterministic fixture paths to deployed workspace paths",
    }, indent=2))
    from parallel import fingerprints, verify_frozen, train_parallel, audit_final_deployments
    frozen_sources = fingerprints()
    (output / 'source-fingerprints.json').write_text(json.dumps(frozen_sources, indent=2))
    for name in frozen_sources:
        saved = output/'source-snapshot'/name
        saved.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT/name,saved)
    results = {}
    if not args.train_only:
        print('Evaluating the original actor without an adapter', flush=True)
        results['none'] = evaluate(config, 'none')
        print('Evaluating the fixed starting adapter', flush=True)
        results['seed'] = evaluate(config, str(seed))
    print("Training adapter on the common training split", flush=True)
    if args.train_workers > 1:
        results['training'], final_adapter = train_parallel(config, output, seed, args.train_workers, frozen_sources)
    else:
        results["training"] = Trainer(config).train()
        final_adapter = 'final'
    if not args.train_only:
        print("Evaluating frozen final adapter on held-out questions", flush=True)
        verify_frozen(frozen_sources)
        config['agent']['concurrency'] = args.eval_concurrency
        results["final"] = evaluate(config, str(final_adapter))
        verify_frozen(frozen_sources)
        with tempfile.TemporaryDirectory(prefix='coevoskills-final-audit-') as tmp:
            audit_adapter = final_adapter
            if args.train_workers == 1:
                # Audit the exact commit evaluate() used, even if its final tag moved.
                audit_adapter = Tree(Path(results['training']['tree'])).export(
                    results['final']['commit'], Path(tmp) / 'adapter')
            audit_final_deployments(audit_adapter, config.run_dir() / 'eval/final/results.jsonl')
    if not args.train_only:
        from compare import compare
        intended = config.path(config["data"]["test"]).read_text().split()
        results["paired_comparison"] = compare(config.run_dir(), intended)
    if immutable_sources() != actor_identity:
        raise RuntimeError("actor/judge identity changed during this run")
    (output / "comparison.json").write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2), flush=True)
    return results

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=HERE/"run_codex.yaml")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--train-only", action="store_true")
    parser.add_argument('--train-workers', type=int, default=1)
    parser.add_argument('--eval-concurrency', type=int, default=1)
    args = parser.parse_args()
    if args.train_workers < 1 or args.eval_concurrency < 1:
        parser.error('concurrency must be positive')
    run(args)
