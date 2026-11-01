"""Moved to hermit.agent.acting (the Acting Agents) and hermit.agent.base (Layer 2)."""
from hermit._moved import forward

__getattr__ = forward(__name__, "hermit.agent.acting", {
    "base": "hermit.agent.acting.agent", "cli": "hermit.agent.acting.workspace", "react": "hermit.agent.acting.react",
    "tools": "hermit.agent.acting.tools", "deploy": "hermit.agent.acting.deploy",
    "container": "hermit.agent.base.container", "login": "hermit.agent.base.login"})
