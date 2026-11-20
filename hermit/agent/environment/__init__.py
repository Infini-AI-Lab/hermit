"""Moved to hermit.environment (the environments every layer shares: the task's, the agent's, the judge's)."""
from hermit._moved import forward

__getattr__ = forward(__name__, "hermit.environment", {"base": "hermit.environment.base",
                                                       "docker": "hermit.environment.docker"})
