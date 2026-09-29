"""Scripted stand-in for the learner model (no network, no key, $0).

`ScriptedModel(turns)` is called exactly like learner_common.FireworksModel and returns the same
(response, meta) pair shape as client.complete (Fireworks chat/completions): response =
{id, model, choices: [{message: {role, content, tool_calls?, reasoning_content}, finish_reason}], usage},
meta = {http_status, usage, response_model, attempts, wire_attempts, usage_complete, ...}.

A turn is a dict built with `calls(...)`, `say(...)` or `runaway()`, or a callable(messages) -> turn (so a
script can react to earlier tool results), or None (= a transport failure: response None, like
client.complete after exhausted retries). When the script is exhausted the model says 'done'.
Every assistant message carries a private `reasoning_content` so tests can check it never reaches records.
"""
from __future__ import annotations

import copy
import itertools
import json
from typing import Any, Callable

MODEL = 'accounts/fireworks/models/glm-5p3-flash'
PRIVATE_REASONING = 'PRIVATE-REASONING-MUST-NOT-BE-RECORDED'
_ids = itertools.count(1)


def tool_call(name: str, **arguments: Any) -> dict:
    return {'id': f'call_{next(_ids)}', 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(arguments, ensure_ascii=False)}}


def calls(*tool_calls: dict, content: str = '', **usage: int) -> dict:
    return {'tool_calls': list(tool_calls), 'content': content, 'finish': 'tool_calls', **usage}


def say(text: str, finish: str = 'stop', **usage: int) -> dict:
    return {'content': text, 'finish': finish, **usage}


def runaway(**usage: int) -> dict:
    return {'content': '', 'finish': 'length', **usage}


def tool_results(messages: list[dict]) -> list[dict]:
    """The parsed tool results that follow the last assistant message."""
    out = []
    for message in reversed(messages):
        if message.get('role') == 'assistant':
            break
        if message.get('role') == 'tool':
            out.append(json.loads(message['content']))
    return list(reversed(out))


class ScriptedModel:
    def __init__(self, turns: list, *, prompt_tokens: int = 1_000, completion_tokens: int = 200,
                 reasoning_effort: str = 'high', model_name: str = MODEL, http_status: int = 200, name: str = MODEL):
        self.turns = list(turns)
        self.prompt_tokens, self.completion_tokens = prompt_tokens, completion_tokens
        self.reasoning_effort = reasoning_effort
        self.model_name, self.http_status, self.name = model_name, http_status, name
        self.requests: list[dict] = []

    def __call__(self, messages: list[dict], *, tools: list[dict], tool_choice: str | None = None,
                 max_tokens: int = 65_536) -> tuple[dict | None, dict]:
        self.requests.append({'messages': copy.deepcopy(messages), 'tools': [t['function']['name'] for t in tools],
                              'tool_choice': tool_choice, 'max_tokens': max_tokens})
        turn: Any = self.turns.pop(0) if self.turns else say('done')
        if callable(turn):
            turn = turn(messages)
        if turn is None:
            return None, {'http_status': 503, 'usage': {}, 'attempts': [{'status': 'transport', 'dispatched': True}],
                          'usage_complete': False, 'wire_attempts': 1, 'error': 'transport failed after retries'}
        usage = {'prompt_tokens': turn.get('prompt_tokens', self.prompt_tokens),
                 'completion_tokens': turn.get('completion_tokens', self.completion_tokens),
                 'prompt_tokens_details': {'cached_tokens': 0}}
        usage['total_tokens'] = usage['prompt_tokens'] + usage['completion_tokens']
        message = {'role': 'assistant', 'content': turn.get('content', ''), 'reasoning_content': PRIVATE_REASONING}
        if turn.get('tool_calls'):
            message['tool_calls'] = turn['tool_calls']
        number = len(self.requests)
        response = {'id': f'resp_{number}', 'object': 'chat.completion', 'model': self.model_name,
                    'choices': [{'index': 0, 'message': message, 'finish_reason': turn.get('finish', 'stop')}],
                    'usage': usage}
        meta = {'http_status': self.http_status, 'seconds': 0.0, 'usage': usage, 'response_model': self.model_name,
                'response_id': f'resp_{number}', 'attempts': [{'status': 'ok', 'dispatched': True, 'usage': usage}],
                'request_sha256': f'{number:064x}', 'usage_complete': True, 'usage_is_lower_bound': False,
                'wire_attempts': 1, 'completion_usage_valid': True}
        return response, meta


def when_results(check: Callable[[list[dict]], bool], then: dict, otherwise: dict) -> Callable[[list[dict]], dict]:
    """A turn that depends on the previous tool results."""
    return lambda messages: then if check(tool_results(messages)) else otherwise
