"""Moved to hermit.agent.evolver."""
from hermit._moved import forward

__getattr__ = forward(__name__, "hermit.agent.evolver", {name: f"hermit.agent.evolver.{name}"
                                           for name in ("base", "agent", "loader", "dependencies")})
