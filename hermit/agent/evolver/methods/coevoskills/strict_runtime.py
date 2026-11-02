"""Environment-only seam for the pinned loop and its fresh evaluation fixtures."""
from __future__ import annotations

import json
import os
from pathlib import Path

import yaml
from harbor.environments.docker.docker import DockerEnvironment


def settings():
    path = os.environ.get('HERMIT_COEVOSKILLS_RUNTIME')
    if not path:
        raise RuntimeError('use the OfficeQA launcher and its model-only Docker runtime')
    cfg = json.loads(Path(path).read_text())
    if cfg.get('backend') != 'codex':
        raise RuntimeError('the original OfficeQA acting agent is Codex')
    return cfg


def configure(params):
    cfg = settings()
    if params['cli_backend'] != 'codex' or params['model_name'] != cfg['model']:
        raise RuntimeError('backend/model differs from the fixed OfficeQA runtime')
    if params.get('effort', 'medium') != cfg['effort']:
        raise RuntimeError('model effort differs from the fixed runtime')
    params.update(cfg['transport'])
    # Only the original loop's Docker construction seam changes. Its methods,
    # prompts, validation decisions and rollback remain in the pinned checkout.
    from libs.terminus_agent.agents.terminus_2 import harbor_terminus_2_evolution as evolution
    evolution.DockerEnvironment = ModelOnlyDockerEnvironment


class ModelOnlyDockerEnvironment(DockerEnvironment):
    async def start(self, force_build=False):
        cfg = settings()
        source = self.environment_dir / 'docker-compose.yaml'
        if source.is_file():
            original = yaml.safe_load(source.read_text()) or {}
            if set(original.get('services', {})) - {'main'}:
                raise RuntimeError('only the prepared OfficeQA fixture is supported')
        main = {'build': {'context': '${CONTEXT_DIR}', 'network': 'none'}, 'image': '${MAIN_IMAGE_NAME}',
            'pull_policy': 'never',
            'command': ['python3', '/hermit-model-relay.py', '--forward',
                        str(cfg['model_port']) + '=/hermit-model.sock', '--', 'sleep', 'infinity'],
            'network_mode': 'none',
            'environment': {'TEST_DIR': '${TEST_DIR}', 'COEVOSKILLS_MODEL_KEY': '${COEVOSKILLS_MODEL_KEY}'},
            'volumes': ['${HOST_VERIFIER_LOGS_PATH}:${ENV_VERIFIER_LOGS_PATH}',
                        '${HOST_AGENT_LOGS_PATH}:${ENV_AGENT_LOGS_PATH}',
                        f'{cfg["socket"]}:/hermit-model.sock:ro',
                        f'{cfg["relay"]}:/hermit-model-relay.py:ro'],
            'deploy': {'resources': {'limits': {'cpus': '${CPUS}', 'memory': '${MEMORY}'}}}}
        if self.task_env_config.docker_image:
            main.pop('build')
            main['image'] = self.task_env_config.docker_image
        source.write_text(yaml.safe_dump({'services': {'main': main}}))
        await super().start(force_build=force_build)

    async def stop(self, delete=True):
        try:
            await super().exec(command=f'chown -hR {os.getuid()}:{os.getgid()} /logs/agent /logs/verifier', timeout_sec=30)
        finally:
            # Harbor's default --rmi all deletes shared prepared images. Cleanup
            # owns only this fixture's containers and anonymous volumes.
            if self._keep_containers:
                await super().stop(delete=False)
            else:
                command = ['down', '--volumes', '--remove-orphans'] if delete else ['down']
                await self._run_docker_compose_command(command)
