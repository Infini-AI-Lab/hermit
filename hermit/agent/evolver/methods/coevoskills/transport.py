"""Model seam for the original CoEvoSkills Chat, using Hermit's Codex Base Agent.

The upstream parser executes returned commands in its own evolution fixture.
Model completions run in separate, empty, read-only Docker sessions.
"""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import uuid

from hermit.agent.base.interface import Limits, make
from hermit.agent.model.backends import Binding

CONFIG: dict = {}


def transcript(prompt: str, history: list[dict]) -> str:
    return (
        "Continue the following conversation as the assistant. Return ONLY the next "
        "assistant message in the exact JSON format requested in the conversation. "
        "Do not execute commands yourself. All terminal commands in the conversation "
        "refer to a REMOTE task container and are executed by its controller.\n\n"
        + json.dumps([*history, {"role": "user", "content": prompt}], ensure_ascii=False)
    )


class CLIModel:
    def __init__(self, model_name: str, **kwargs):
        self._model_name = model_name
        self.config = dict(CONFIG)
        if self.config.get('cli_backend', 'codex') != 'codex':
            raise ValueError('the fixed OfficeQA integration uses Codex')
        if (not self.config.get('binding') and self.config.get('login') != 'chatgpt') or self.config.get('container', {}).get('network') != 'none':
            raise RuntimeError('only the isolated model gateway transport is supported')

    def count_tokens(self, messages: list[dict]) -> int:
        from litellm import token_counter
        return token_counter(model=self._model_name, messages=messages)

    def call(self, prompt: str, message_history=None, logging_path=None, **kwargs) -> str:
        audit = Path(self.config['transport_logs']) / uuid.uuid4().hex
        audit.mkdir(parents=True)
        binding = Binding.from_spec(self.config['binding']) if self.config.get('binding') else None
        timeout = float(self.config.get('completion_timeout_s', 600))
        settings = {
            **({'model': binding.model, 'binding': binding.spec(), 'price': binding.price} if binding
               else {'model': self.config['model'], 'login': 'chatgpt'}),
            'args': [*(binding.codex_args() if binding else self.config.get('args', [])), '-c', 'features.shell_tool=false',
                     '-c', 'features.unified_exec=false', '-c', 'features.multi_agent=false'],
            'effort': self.config.get('effort', 'medium'), 'web_search': False,
            'isolation': 'container', 'container': self.config['container'],
            'codex_sandbox': 'read-only', 'home_files': [], 'stdin': True,
            'env': {**self.config.get('env', {}), 'NO_PROXY': '127.0.0.1,localhost'}, 'log': audit / 'events.jsonl',
        }
        with tempfile.TemporaryDirectory(prefix='hermit-coevoskills-model-') as tmp:
            result = make('codex', settings).run(
                transcript(prompt, list(message_history or [])), workspace=Path(tmp),
                limits=Limits(network=[], timeout_s=timeout))
        (audit / 'usage.json').write_text(json.dumps([u.__dict__ for u in result.usage]))
        if result.exit_code or result.timed_out or not result.extra.get('served') or not result.reply.strip():
            raise RuntimeError(f'Codex completion failed; see {audit}')
        (audit / 'response.txt').write_text(result.reply, encoding='utf-8')
        return result.reply


def install(config: dict) -> None:
    """Replace only model construction, before the original G/V roles are created."""
    CONFIG.clear()
    CONFIG.update(config)
    from libs.terminus_agent.agents.terminus_2 import harbor_terminus_2_skills as agents
    agents.LiteLLM = CLIModel
