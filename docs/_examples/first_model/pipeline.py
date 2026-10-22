"""A real model actor; endpoint settings are deliberately separate from learner YAML."""
import os
from pathlib import Path

from hermit.adapter.loader import Adapter
from hermit.llm import ChatClient
from hermit.task import RunOutput


def run(task: dict, adapter: Path | None) -> RunOutput:
    client = ChatClient(
        os.environ.get("ACTOR_BASE_URL") or os.environ["MODEL_BASE_URL"],
        os.environ.get("ACTOR_MODEL") or os.environ["MODEL_NAME"],
        api_key_env="ACTOR_API_KEY" if os.environ.get("ACTOR_API_KEY") else "MODEL_API_KEY",
    )
    context = Adapter(adapter).context() if adapter is not None else ""
    messages = [
        {"role": "system", "content": "Complete the user's request.\n" + context},
        {"role": "user", "content": task["prompt"]},
    ]
    reply, usage = client.chat(messages, max_tokens=256, temperature=0.0)
    answer = reply.get("content")
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError("Actor returned no text; inspect endpoint/model settings and output-token limit")
    return RunOutput(
        output=answer,
        messages=messages + [{"role": "assistant", "content": answer}],
        tokens=usage.input_tokens + usage.output_tokens,
        cost_usd=usage.cost_usd,
    )
