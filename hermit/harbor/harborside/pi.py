"""HermitPi: Pi in a Harbor trial, its session launched from Hermit's plan (harborside.hermit).

Imported by Harbor as `harborside.pi:HermitPi`. Harbor's Pi brings the install: nvm and Node 22, then `npm install -g
--ignore-scripts @earendil-works/pi-coding-agent` at `version` (Hermit's runner gives its tested build when the
config names none; Harbor's own default would be the latest), after the apt repair of an image whose release left the
mirrors; or a Pi binary mounted at /opt/hermit-pi (`pi_bin`) that answers `--version` as `version` and that the task's
agent user may execute.

The session is the plan's, as harborside.hermit runs every CLI's: Hermit's own `pi --mode json --provider hermit ...`
(models.json, mcp.json, settings.json and the command-limit extension in its home, PI_CODING_AGENT_DIR and HOME both
/installed-agent/hermit-home, offline), the deployment's .pi/ in the workdir trusted for the session (--approve), and
the stream agent-pi.jsonl. The plan loads nvm (`nvm: true`) with the agent user's own HOME, before Pi's HOME replaces
it, so the install's Node and `pi` are found. Pi exits 0 however a session ended: Hermit's side reads its verdict from
the stream. Harbor's own run (its --session-dir under /logs/agent, its pi.txt, its models.json) is not used.
"""
from __future__ import annotations

from harbor.agents.installed.pi import Pi, PiOptions
from pydantic import Field

from harborside.hermit import HermitInstalled, HermitOptions


class HermitPiOptions(HermitOptions, PiOptions):
    """Harbor's Pi kwargs (accepted, unused: the plan holds the session) and Hermit's. `extra="forbid"`."""

    pi_bin: str | None = Field(default=None, description="A Pi binary mounted in the container (at /opt/hermit-pi), "
                                                          "used when it runs there as `version`, instead of Harbor's "
                                                          "per-trial install (nvm, Node 22, npm).")


class HermitPi(HermitInstalled, Pi):
    CLI = "pi"
    PROGRAM = "pi"
    BIN_OPTION = "pi_bin"
    options_model = HermitPiOptions
    options: HermitPiOptions
