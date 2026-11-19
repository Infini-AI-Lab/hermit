"""Environments: where an agent works and what it may reach (docs/framework/agent-layers.md#environments).

    base.py     the interface (Environment, Limits, Mount, ExecResult) and its implementations on this machine:
                Local (isolation none), Sandboxed (the run's sandbox), Container (a Docker container)
    docker.py   a CLI session in a Docker container of its own (the Container environment's runner)

A Base Agent (hermit.agent.base) works in the environment its call gives; the environment never imports a layer
above it.
"""
from hermit.environment.base import (Container, Environment, ExecResult, Limits, Local, Mount, Sandboxed,
                                           Unsupported, as_environment, placed)

__all__ = ["Container", "Environment", "ExecResult", "Limits", "Local", "Mount", "Sandboxed", "Unsupported",
           "as_environment", "placed"]
