"""HermitDsh: DeepSeek Harness (dsh) in a Harbor trial, its session launched from Hermit's plan (harborside.hermit).

Imported by Harbor as `harborside.dsh:HermitDsh` (Harbor ships no dsh agent). The install is the one Hermit documents
(docs/get-started/installation.md): Node.js 24.9 or later, here from Harbor's nvm snippet with Node 24 (checked to be
at least 24.9; nvm's builds need glibc, so a musl image is refused with that reason), then `npm install -g
@deepseek-ai/dsh` at `version` (Hermit's runner gives its tested build, a prerelease that npm's `latest` tag may not
name, when the config names none), as the agent user; or a dsh mounted at /opt/hermit-dsh (`dsh_bin`) that answers
`--version` as `version` and that the task's agent user may execute. Setup may take longer than Harbor's 360 s default
under load (600 s here; the runner's setup_timeout_sec overrides it).

The session is the plan's, as harborside.hermit runs every CLI's: Hermit's own `dsh --profile headless ... --patch
<home>/hermit.patch.yml --json --permission-mode danger-full-access --approval-policy never` (the task's container is
the session's sandbox: dsh's own needs privileges a Harbor container lacks), its home's DSH_HOME and HOME under
/installed-agent/hermit-home, the deployment's AGENTS.md (within dsh's 65,536-byte limit once the image's own follows
it, or the trial fails saying so) and .dsh/ in the workdir, stderr in the stream agent-dsh.jsonl (where dsh says a
tool server did not start). The plan loads nvm with the agent user's own HOME before dsh's HOME replaces it. dsh exits
0 when SIGTERM stops it: Hermit's side reads its verdict from the stream, not its exit code.
"""
from __future__ import annotations

from harbor.agents.installed.node_install import nvm_node_install_snippet
from harbor.environments.base import BaseEnvironment
from pydantic import Field

from harborside.hermit import MUSL, VERSION, HermitInstalled, HermitOptions

PACKAGE = "@deepseek-ai/dsh"
NODE_MAJOR = 24
NODE_MIN = (24, 9)
NODE_CHECK = ("node -e 'const [a, b] = process.versions.node.split(\".\").map(Number); "
              f"if (a < {NODE_MIN[0]} || (a === {NODE_MIN[0]} && b < {NODE_MIN[1]})) {{ console.error(\"hermit: dsh "
              f"needs Node {NODE_MIN[0]}.{NODE_MIN[1]} or later, not \" + process.versions.node); process.exit(1); }}'")


class HermitDshOptions(HermitOptions):
    """Hermit's kwargs for dsh (Harbor has no options of its own for it). `extra="forbid"`."""

    dsh_bin: str | None = Field(default=None, description="A dsh mounted in the container (at /opt/hermit-dsh), used "
                                                           "when it runs there as `version`, instead of the per-trial "
                                                           "install (nvm, Node 24, npm).")


class HermitDsh(HermitInstalled):
    CLI = "dsh"
    PROGRAM = "dsh"
    BIN_OPTION = "dsh_bin"
    DEFAULT_SETUP_TIMEOUT_SEC = 600.0
    options_model = HermitDshOptions
    options: HermitDshOptions

    @staticmethod
    def name() -> str:
        return "dsh"

    def get_version_command(self) -> str | None:
        return "if [ -s ~/.nvm/nvm.sh ]; then . ~/.nvm/nvm.sh; fi; dsh --version"

    def parse_version(self, stdout: str) -> str:
        found = VERSION.search(stdout or "")
        return found.group(1) if found else (stdout or "").strip()

    def install_command(self) -> str:
        spec = f"@{self._version}" if self._version else "@latest"
        return ("set -euo pipefail; "
                f"if {MUSL}; then echo 'hermit: dsh needs Node {NODE_MIN[0]}.{NODE_MIN[1]} or later from nvm, whose "
                "builds need glibc, and this image is musl' >&2; exit 1; fi; "
                f"{nvm_node_install_snippet(node_major=NODE_MAJOR)} && {NODE_CHECK} && "
                f"npm install -g {PACKAGE}{spec} && dsh --version")

    async def install_cli(self, environment: BaseEnvironment) -> None:
        """curl (nvm's installer), then Node 24 and dsh as the agent user (the module's docstring)."""
        await self.ensure_system_dependencies(environment, ("curl",))
        await self.exec_as_agent(environment, command=self.install_command())
