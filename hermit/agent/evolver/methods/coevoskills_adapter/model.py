"""The upstream roles' model: upstream's LiteLLM with its completion replaced by a call of the Trainer's `llm` tool.

Upstream builds every role's model as `LiteLLM(model_name, temperature, api_base, api_key)` and calls
`call(prompt, message_history, response_format, logging_path, max_tokens=...)` (libs/terminus_agent/llms/lite_llm.py).
The subclass keeps its prompt template for a structured response and its errors (an empty reply, a reply cut at the
length limit) and sends the messages, the temperature and the output cap to the run's `llm` instead of litellm: the
Trainer meters, logs and stops those calls, and the run's `llm` decides the backend (an API model keeps the message
roles; a model only the Codex CLI reaches answers each call in one session of its own, hermit.agent.base.endpoint).
Token counting stays upstream's (litellm's counter). Retries are the run's `llm`'s (hermit.agent.model.llm.ChatClient:
transient errors, four retries, waits doubling from 2 s), not upstream's (ten attempts, waits up to 60 s on a rate
limit, an empty reply retried): the method adds none of its own.
"""
from __future__ import annotations

import json

from libs.terminus_agent.llms.base_llm import OutputLengthExceededError
from libs.terminus_agent.llms.lite_llm import LiteLLM

from hermit.agent.evolver.methods.coevoskills_adapter.client import call_tool

TOOL_URL = ""                        # the Trainer's tool endpoint, set by the worker before the roles are built


class ToolLLM(LiteLLM):
    def call(self, prompt: str, message_history=(), response_format=None, logging_path=None, **kwargs) -> str:
        if response_format is not None:              # upstream's fallback for a model without structured output
            schema = response_format if isinstance(response_format, dict) else response_format.model_json_schema()
            prompt = self._prompt_template.format(schema=json.dumps(schema, indent=2), prompt=prompt)
        messages = [*message_history, {"role": "user", "content": prompt}]
        request = {"messages": messages, "temperature": self._temperature}
        if kwargs.get("max_tokens") is not None:                 # the Generator's output cap (16,384 upstream)
            request["max_tokens"] = int(kwargs["max_tokens"])
        result = call_tool(TOOL_URL, "llm", request)
        content = result.get("content")
        if result.get("finish_reason") == "length":
            raise OutputLengthExceededError(f"the run's model hit its output limit ({self._model_name})",
                                            truncated_response=content)
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError(f"the run's model returned an empty reply ({self._model_name})")
        self.last_usage = dict(result.get("usage") or {})
        return content


def install(tool_url: str) -> None:
    """Every upstream role built after this call uses ToolLLM."""
    global TOOL_URL
    TOOL_URL = tool_url
    from libs.terminus_agent.agents.terminus_2 import harbor_terminus_2_skills, terminus_2
    harbor_terminus_2_skills.LiteLLM = ToolLLM
    terminus_2.LiteLLM = ToolLLM
