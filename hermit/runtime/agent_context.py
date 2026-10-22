"""Per-call agent context for adapters served by the standalone service (runtime/service.py).

One service process serves every agent of a run, so an adapter cannot read its caller's identity and settings from
os.environ as a per-agent stdio server does. The service sets this context variable around each tool call and each
reaction; `get(key)` falls back to os.environ, so the same adapter code runs under both runtimes.
"""
import contextvars
import os

AGENT_ENV: contextvars.ContextVar = contextvars.ContextVar("agent_env", default=None)


def get(key: str):
    env = AGENT_ENV.get()
    if env is not None and key in env:
        return env[key]
    return os.environ.get(key)
