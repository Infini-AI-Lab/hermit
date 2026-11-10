"""AdapterEnvironment: a deployed mini-swe-agent workspace's environment, its adapter's tools as one bash command.

`hermit deploy --host mini-swe-agent` names this class in the workspace's config when the adapter has tools; it is
hermit.agent.base.mini_swe_agent_env.HermitEnvironment with the tools answered in mini-swe-agent's process through a
Session of the deployed adapter (`adapter`), made at the first tool call, each call appended to `audit_log`.
Commands run as mini-swe-agent's LocalEnvironment runs them. This module imports mini-swe-agent: mini-swe-agent
imports it from the config, so `mini` must run in the Python environment Hermit is installed in.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from hermit.agent.base.mini_swe_agent_env import HermitEnvironment, HermitEnvironmentConfig, text

if TYPE_CHECKING:
    from hermit.adapter.session import Session


class AdapterEnvironmentConfig(HermitEnvironmentConfig):
    adapter: str = ""
    """The deployed adapter whose tools `hermit-tool` runs."""
    audit_log: str = ""
    """Every tool call is appended to this JSONL file (the workspace's tool-calls.jsonl)."""
    learning_material: str = ""
    """Bound to the adapter's tools as their learning material, when set."""


class AdapterEnvironment(HermitEnvironment):
    def __init__(self, *, config_class: type = AdapterEnvironmentConfig, **kwargs):
        super().__init__(config_class=config_class, **kwargs)
        self._adapter_session: Session | None = None

    def tool_list(self) -> list[dict]:
        return self._session().tools if self.config.adapter else []

    def call_tool(self, name: str, arguments: dict) -> str:
        session = self._session()
        if not session.has_tool(name):       # the context tool included: it is the harness's, not the agent's
            raise ValueError(f"Unknown tool: {name}")
        return text(session.call(name, arguments))

    def _session(self) -> Session:
        if self._adapter_session is None:
            from hermit.adapter.session import Session
            self._adapter_session = Session(self.config.adapter, audit_log=self.config.audit_log or None,
                                            learning_material=self.config.learning_material or None)
        return self._adapter_session


__all__ = ["AdapterEnvironment", "AdapterEnvironmentConfig"]
