"""One model-only channel for the fixed OfficeQA actor and evolution workers."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import shutil
import sys
import tempfile

from hermit.agent.model.backends import Binding, Backend, problems_of
from hermit.sandbox import _Bridges, RELAY
from hermit.agent.evolver.methods.coevoskills.gateway import ModelGateway


def binary(name, configured=None):
    value = shutil.which(os.path.expanduser(str(configured or name)))
    if not value:
        raise RuntimeError(name + ' must be installed and available on PATH')
    return str(Path(value).resolve())


@contextmanager
def model_runtime(*, binding: Binding | None = None, login=None, model=None, effort, output, docker_image):
    if binding:
        issues = problems_of(binding, 'responses')
        if issues:
            raise ValueError('; '.join(issues))
        model = binding.model
    elif login != 'chatgpt' or not model:
        raise ValueError('configure a model binding or login: chatgpt with a model')
    names = ('COEVOSKILLS_MODEL_KEY', 'HERMIT_COEVOSKILLS_RUNTIME', 'PATH')
    saved = {name: os.environ.get(name) for name in names}
    bridges, gateway = _Bridges(), None
    try:
        os.environ['COEVOSKILLS_MODEL_KEY'] = secrets.token_urlsafe(32)
        def relay_binding(url):
            backend = Backend(name='coevoskills_model', base_url=url,
                              api_key_env='COEVOSKILLS_MODEL_KEY', apis=('responses',),
                              context_window=binding.backend.context_window)
            return Binding(backend, model, price=binding.price, extra_body=dict(binding.extra_body))

        with tempfile.TemporaryDirectory(prefix='hermit-coevoskills-runtime-') as tmp:
            directory = Path(tmp)
            gateway = ModelGateway(binding=binding, client_key=os.environ['COEVOSKILLS_MODEL_KEY'],
                **({'login': login, 'model': model} if not binding else {}),
                log=output / 'model-egress.jsonl').start()
            inside = relay_binding('http://127.0.0.1:48157/v1') if binding else None
            native = inside or Binding(
                Backend(name='coevoskills_login', base_url='http://127.0.0.1:48157/v1',
                        api_key_env=None, key_required=False, apis=('responses',)), model)
            transport = ({'binding': inside.spec()} if inside else {
                'login': 'chatgpt', 'model': model,
                'args': [*native.codex_args(), '-c', 'model_providers.coevoskills_login.requires_openai_auth=true',
                         '-c', 'model_providers.coevoskills_login.supports_websockets=false',
                         '-c', 'chatgpt_base_url="http://127.0.0.1:48157/backend-api"'],
                'env': {'CODEX_REFRESH_TOKEN_URL_OVERRIDE': 'http://127.0.0.1:48157/oauth/token'}})
            transport['container'] = {'image': docker_image, 'network': 'none'}
            runtime = {'backend': 'codex', 'model': model, 'effort': effort, 'model_port': 48157,
                'socket': str(bridges.socket_for(gateway.port)),
                'relay': str(RELAY),
                'docker_image': docker_image, 'docker_bin': binary('docker'),
                'transport': transport}
            path = directory / 'runtime.json'
            path.write_text(json.dumps(runtime, indent=2))
            os.environ['HERMIT_COEVOSKILLS_RUNTIME'] = str(path)
            shim = directory / 'bin/docker'
            shim.parent.mkdir()
            shim.write_text('#!' + sys.executable + '\nfrom hermit.agent.evolver.methods.coevoskills.docker import main\nmain()\n')
            shim.chmod(0o755)
            os.environ['PATH'] = str(shim.parent) + os.pathsep + os.environ['PATH']
            yield runtime, inside
    finally:
        if gateway:
            gateway.close()
        bridges.close()
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
