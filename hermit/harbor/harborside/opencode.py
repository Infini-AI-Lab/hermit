"""HermitOpenCode: OpenCode 2.2 in a Harbor trial, its session launched from Hermit's plan (harborside.hermit).

Imported by Harbor as `harborside.opencode:HermitOpenCode`. Harbor's OpenCode installs the npm package `opencode-ai`,
which is OpenCode's 1.x line; Hermit's configs (its home's opencode.json, the inline config, `run --standalone`) are
OpenCode 2.2's. So the install here is the GitHub release Hermit documents (docs/get-started/installation.md): the
`anomalyco/opencode` release v<version>'s `opencode-linux-x64.tar.gz`, or `-musl.tar.gz` on a musl image (the test
Harbor's own installs use), downloaded with curl, else wget, else python3's urllib (curl installed first when the
image has none of them), unpacked into /installed-agent/opencode/bin and linked as /usr/local/bin/opencode; or an
OpenCode binary mounted at /opt/hermit-opencode (`opencode_bin`) that answers `--version` as `version` and that the
task's agent user may execute. The version is the runner's (Hermit's tested build when the config names none); without
one, the latest release. The release is not checked against a digest here: pinning a CLI's build is not this class's
business.

The session is the plan's, as harborside.hermit runs every CLI's: Hermit's own `opencode run --standalone --format
json -m hermit/<model> ...` with the prompt on stdin, its home's HOME and XDG folders under /installed-agent/hermit-home
(its session database is collected for Hermit's usage reader), the deployment's opencode.json and .opencode/skills in
the workdir, and the stream agent-opencode.jsonl. Harbor's own run (its opencode.txt, its config) is not used.
"""
from __future__ import annotations

from harbor.agents.installed.opencode import OpenCode, OpenCodeOptions
from harbor.environments.base import BaseEnvironment
from pydantic import Field

from harborside.hermit import MUSL, VERSION, HermitInstalled, HermitOptions

REPOSITORY = "anomalyco/opencode"
INSTALL_DIR = "/installed-agent/opencode"
DOWNLOADERS = "command -v curl >/dev/null 2>&1 || command -v wget >/dev/null 2>&1 || command -v python3 >/dev/null 2>&1"


class HermitOpenCodeOptions(HermitOptions, OpenCodeOptions):
    """Harbor's OpenCode kwargs (accepted, unused: the plan holds the session) and Hermit's. `extra="forbid"`."""

    opencode_bin: str | None = Field(default=None, description="An OpenCode binary mounted in the container (at "
                                                                "/opt/hermit-opencode), used when it runs there as "
                                                                "`version`, instead of the per-trial download of the "
                                                                "GitHub release.")


class HermitOpenCode(HermitInstalled, OpenCode):
    CLI = "opencode"
    PROGRAM = "opencode"
    BIN_OPTION = "opencode_bin"
    RUN_PATH = (f"{INSTALL_DIR}/bin",)
    options_model = HermitOpenCodeOptions
    options: HermitOpenCodeOptions

    def parse_version(self, stdout: str) -> str:
        found = VERSION.search(stdout or "")
        return found.group(1) if found else (stdout or "").strip()

    def release_url(self) -> str:
        """The release's download folder: v<version>'s, or the latest's when no version is given."""
        tail = f"download/v{self._version}" if self._version else "latest/download"
        return f"https://github.com/{REPOSITORY}/releases/{tail}"

    def install_command(self) -> str:
        """Download, unpack and link the release's binary (as root: /installed-agent and /usr/local/bin are root's)."""
        d = INSTALL_DIR
        return "\n".join([
            "set -eu",
            f"d={d}",
            'arch=x64; case "$(uname -m)" in aarch64|arm64) arch=arm64;; esac',
            f'if {MUSL}; then asset="opencode-linux-$arch-musl.tar.gz"; else asset="opencode-linux-$arch.tar.gz"; fi',
            f'url="{self.release_url()}/$asset"',
            'rm -rf "$d" && mkdir -p "$d/bin" "$d/unpack"',
            'if command -v curl >/dev/null 2>&1; then curl -fsSL --retry 3 -o "$d/$asset" "$url"',
            'elif command -v wget >/dev/null 2>&1; then wget -q -O "$d/$asset" "$url"',
            "elif command -v python3 >/dev/null 2>&1; then python3 -c 'import sys, urllib.request; "
            "urllib.request.urlretrieve(sys.argv[1], sys.argv[2])' \"$url\" \"$d/$asset\"",
            "else echo 'hermit: no curl, wget or python3 here to download OpenCode' >&2; exit 1; fi",
            'tar -xzf "$d/$asset" -C "$d/unpack"',
            'b=$(find "$d/unpack" -type f -name opencode | head -n 1)',
            '[ -n "$b" ] || { echo "hermit: $asset holds no opencode binary" >&2; exit 1; }',
            'mv "$b" "$d/bin/opencode" && chmod 755 "$d/bin/opencode" && rm -rf "$d/unpack" "$d/$asset"',
            'mkdir -p /usr/local/bin && ln -sf "$d/bin/opencode" /usr/local/bin/opencode',
            '"$d/bin/opencode" --version'])

    async def install_cli(self, environment: BaseEnvironment) -> None:
        """The GitHub release (the module's docstring); curl and its CA roots first on an image with no downloader."""
        if (await environment.exec(command=DOWNLOADERS, user="root")).return_code != 0:
            await self.ensure_system_dependencies(environment, ("curl", "ca_certificates"))
        await self.exec_as_root(environment, command=self.install_command())
