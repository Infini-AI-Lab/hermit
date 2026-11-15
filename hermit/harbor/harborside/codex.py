"""HermitCodex: Codex in a Harbor trial, its session launched from Hermit's plan (harborside.hermit).

Imported by Harbor as `harborside.codex:HermitCodex`. Harbor's Codex brings the install (apt curl nodejs npm ripgrep,
then `npm install -g @openai/codex` at `version`: on a glibc image under the agent user's nvm with Node 22, on musl
into npm's global bin), after the apt repair of an image whose release left the mirrors, unless a Codex binary is
mounted (`codex_bin`: the standalone release's bin/codex, its release folder mounted read-only at /opt/hermit-codex by
Hermit's runner), answers `--version` as `version` and may be executed by the task's agent user. Harbor's link of codex
into /usr/local/bin looks for it without nvm, so on glibc it finds none: the plan loads nvm (`nvm: true`) with the agent
user's own HOME, and a mount's bin/ goes on PATH after that, ahead of nvm's. A mount that serves is linked onto PATH
with the release's rg (when the image has none) and, for an image with no CA bundle at all (TB2.0's bare ubuntu:24.04),
the host's bundle where Debian keeps one: Codex trusts the system's roots, and without them every request fails ("error
sending request", then "workspace routing discovery failed") and the trial ends in seconds as an agent error. Harbor's
install would have brought ca-certificates with curl.

The session is the plan's, as harborside.hermit runs every CLI's: Hermit's own `codex exec --json ...` with its home
(CODEX_HOME=/installed-agent/hermit-home: config.toml, roles, a sign-in), the deployment's .codex/ in the workdir
(trusted as a project there, its hooks run from hermit/<adapter id>/), and the stream agent-codex.jsonl. Harbor's own
run (its config.toml, its codex.txt, its deletion of CODEX_HOME after the session) is not used.
"""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import shlex
import ssl

from harbor.agents.installed.codex import Codex, CodexOptions
from harbor.environments.base import BaseEnvironment
from pydantic import Field

from harborside.hermit import HermitInstalled, HermitOptions

# Where TLS clients find the system's CA bundle. Codex trusts the system's roots (unless SSL_CERT_FILE or
# CODEX_CA_CERTIFICATE names a bundle); Harbor's install brings them (ca-certificates, with curl), a bare image has
# none.
CA_BUNDLES = ("/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt", "/etc/ssl/ca-bundle.pem",
              "/etc/pki/tls/cacert.pem", "/etc/ssl/cert.pem")


def host_ca_bundle() -> Path | None:
    """The CA bundle OpenSSL trusts on this host, else certifi's; None when there is neither."""
    paths = ssl.get_default_verify_paths()
    for name in (os.environ.get(paths.openssl_cafile_env or ""), paths.cafile, paths.openssl_cafile):
        if name and Path(name).is_file():
            return Path(name).resolve()
    try:
        import certifi                                     # Harbor's own dependency; optional here
    except ImportError:
        return None
    return Path(certifi.where())


class HermitCodexOptions(HermitOptions, CodexOptions):
    """Harbor's Codex kwargs (accepted, unused: the plan holds the session) and Hermit's. `extra="forbid"`."""

    codex_bin: str | None = Field(default=None, description="A Codex binary mounted in the container (the standalone "
                                                            "release's bin/codex, its release folder mounted "
                                                            "read-only), used when it runs there as `version`, instead "
                                                            "of Harbor's per-trial install (apt curl nodejs npm "
                                                            "ripgrep, nvm, npm).")


class HermitCodex(HermitInstalled, Codex):
    CLI = "codex"
    PROGRAM = "codex"
    BIN_OPTION = "codex_bin"
    options_model = HermitCodexOptions
    options: HermitCodexOptions

    @staticmethod
    def name() -> str:
        return "codex"                                    # Harbor keeps treating the trial as a Codex one

    async def _after_mount(self, environment: BaseEnvironment, binary: PurePosixPath) -> None:
        """The release's rg when the image has none (Codex's file search runs it), and a CA bundle."""
        rg = shlex.quote(str(binary.parent.parent / "codex-path" / "rg"))
        await self.exec_as_root(environment, command=f"if ! command -v rg >/dev/null 2>&1 && [ -x {rg} ]; then "
                                                     f"mkdir -p /usr/local/bin && ln -sf {rg} /usr/local/bin/rg; fi")
        await self._ensure_ca_bundle(environment)

    async def _ensure_ca_bundle(self, environment: BaseEnvironment) -> None:
        """TLS roots for the mounted binary: the image's own bundle, else the host's copied to the Debian path."""
        check = " || ".join(f"[ -s {path} ]" for path in CA_BUNDLES)
        if (await environment.exec(command=check, user="root")).return_code == 0:
            return
        source = host_ca_bundle()
        if source is None:
            self.logger.warning("The image has no CA bundle and neither has the host: Codex may not reach its API")
            return
        target = PurePosixPath(CA_BUNDLES[0])
        await self.exec_as_root(environment, command=f"mkdir -p {shlex.quote(str(target.parent))}")
        await environment.upload_file(source, str(target))
        await self.exec_as_root(environment, command=f"chmod 644 {shlex.quote(str(target))}")
        self.logger.info("The image has no CA bundle: copied the host's %s to %s", source, target)
