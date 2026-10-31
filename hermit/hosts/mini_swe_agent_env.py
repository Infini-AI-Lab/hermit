"""HermitEnvironment: mini-swe-agent's local environment with the adapter's tools as one bash command.

mini-swe-agent's model runs bash commands and nothing else, so an adapter tool is a command of its own:

    hermit-tool <name> '<JSON arguments>'      # the result is printed; an error exits 1
    hermit-tool                                # the tools, one per line

The environment answers it in mini-swe-agent's process through a Session, the way the `react` agent answers a tool
call, and runs every other command as LocalEnvironment does. The acting agent passes its Session; a deployed
workspace's config names the adapter (`adapter`, `audit_log`), and the Session is made at the first tool call.

With `inherit_env: false` (the acting agent) a command sees `env`, the basic variables in BASIC_ENV and `extra_env`
only, never the process's environment, where the run's model keys are. `extra_env` (the acting agent's expanded
`params.env`) is not part of the config, which mini-swe-agent writes into the trajectory. This module imports mini-swe-agent; import it through
hermit.hosts.mini_swe_agent.load_minisweagent for a quiet import.
"""
from __future__ import annotations

import json
import os
import shlex
from typing import TYPE_CHECKING, Any

from minisweagent.environments.local import LocalEnvironment, LocalEnvironmentConfig, _run

if TYPE_CHECKING:
    from hermit.adapter.session import Session

TOOL_COMMAND = "hermit-tool"
BASIC_ENV = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR", "TZ")


class HermitEnvironmentConfig(LocalEnvironmentConfig):
    adapter: str = ""
    """The adapter whose tools `hermit-tool` runs, when no Session is passed (a deployed workspace)."""
    audit_log: str = ""
    """Every tool call is appended to this JSONL file (a deployed workspace's tool-calls.jsonl)."""
    learning_material: str = ""
    """Bound to the adapter's tools as their learning material, when set."""
    inherit_env: bool = True
    """False: commands get `env`, BASIC_ENV and `extra_env` only, not this process's environment."""


class HermitEnvironment(LocalEnvironment):
    def __init__(self, *, session: Session | None = None, extra_env: dict[str, str] | None = None,
                 config_class: type = HermitEnvironmentConfig, **kwargs):
        super().__init__(config_class=config_class, **kwargs)
        self.session = session
        self.extra_env = dict(extra_env or {})

    def execute(self, action: dict, cwd: str = "", *, timeout: int | None = None) -> dict[str, Any]:
        command = action.get("command", "")
        try:
            words = shlex.split(command)
        except ValueError:
            words = []
        if words and words[0] == TOOL_COMMAND:
            return self._tool(words[1:])
        if self.config.inherit_env:
            return super().execute(action, cwd, timeout=timeout)
        env = {k: os.environ[k] for k in BASIC_ENV if k in os.environ} | self.config.env | self.extra_env
        try:
            result = _run(command, cwd or self.config.cwd or os.getcwd(), env, timeout or self.config.timeout)
            output = {"output": result.stdout, "returncode": result.returncode, "exception_info": ""}
        except Exception as e:
            raw = getattr(e, "output", None)
            raw = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else (raw or "")
            output = {"output": raw, "returncode": -1,
                      "exception_info": f"An error occurred while executing the command: {e}",
                      "extra": {"exception_type": type(e).__name__, "exception": str(e)}}
        self._check_finished(output)
        return output

    def _tool(self, words: list[str]) -> dict[str, Any]:
        """A `hermit-tool` command's output: the tool list, the tool's result, or the error (exit 1)."""
        session = self._session()
        if session is None:
            return _out("Error: this workspace has no adapter tools", 1)
        if not words:
            return _out("\n".join(f"{t['name']}: {t['description']}" for t in session.tools), 0)
        if len(words) > 2:
            return _out(f"Error: run {TOOL_COMMAND} as a command of its own: {TOOL_COMMAND} <name> '<JSON arguments>', "
                        "with the arguments as one JSON object in single quotes", 1)
        name = words[0]
        try:
            if not session.has_tool(name):       # the context tool included: it is the harness's, not the agent's
                raise ValueError(f"Unknown tool: {name}")
            arguments = json.loads(words[1]) if len(words) > 1 else {}
            if not isinstance(arguments, dict):
                raise ValueError("tool arguments must be a JSON object")
            return _out(session.text(session.call(name, arguments)), 0)
        except Exception as exc:
            return _out(f"Error: {type(exc).__name__}: {exc}", 1)

    def _session(self) -> Session | None:
        if self.session is None and self.config.adapter:
            from hermit.adapter.session import Session
            self.session = Session(self.config.adapter, audit_log=self.config.audit_log or None,
                                   learning_material=self.config.learning_material or None)
        return self.session


def _out(text: str, returncode: int) -> dict[str, Any]:
    return {"output": text if text.endswith("\n") or not text else text + "\n", "returncode": returncode,
            "exception_info": ""}


__all__ = ["BASIC_ENV", "HermitEnvironment", "HermitEnvironmentConfig", "TOOL_COMMAND"]
