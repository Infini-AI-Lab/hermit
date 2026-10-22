"""Acting agents: one interface, built-in ones and the task's own.

| `agent.name` | The agent |
|---|---|
| `react` | a chat-completions tool-calling loop over the task's @tool functions and the adapter's tools |
| `codex`, `claude-code` | the agent CLI in a workspace the adapter is deployed into |
| `agent/agent.py:MyAgent` | the task's own `Agent` subclass (a benchmark's loop with the adapter plugged in, ...) |
| `agent/pipeline.py:run` | a plain `run(task, adapter)` function, wrapped |

Every agent is `Agent(params, base).run(task, adapter) -> RunOutput`; `hermit.Session` gives it the
adapter's exports. Settings come from `agent.params` (see each module).
"""
from .base import Agent, FunctionAgent, load_agent
from .tools import tool, load_tools, openai_specs

__all__ = ["Agent", "FunctionAgent", "load_agent", "tool", "load_tools", "openai_specs"]
