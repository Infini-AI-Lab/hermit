"""Harbor's Codex agent with a Hermit adapter deployed into the task container.

Hermit's side (`hermit.harbor.staging`) prepares a Codex workspace from the adapter as `hermit deploy` does and
names it in `deploy_dir`. This agent, imported by Harbor as `harborside.codex:HermitCodex`, puts that workspace
where Codex runs in the container, after Harbor's own Codex install and config upload:

    <WORKDIR>/AGENTS.md              the deployed context block, before an AGENTS.md the image already had
    <WORKDIR>/hermit/<adapter id>/   the adapter's copy (every file of it), as a host deployment lays it out
    <WORKDIR>/.codex/hooks.json      the adapter's hooks, each command run from hermit/<adapter id>/
    $CODEX_HOME/skills/<name>/       the adapter's skills
    $CODEX_HOME/agents/<name>.toml   the adapter's roles, with [agents.<name>] in config.toml and multi_agent on

`<WORKDIR>` is the task's working directory, the project Codex is started in, so the adapter's files sit where
the context block says they are. The MCP server of the adapter's tools is not started: there is no Hermit in the
container; the config's `mcp_servers` are dropped with a log line. `python3` is installed with the image's package
manager when the deployment has hooks and the image lacks it (best effort; recorded as `python3` in the trial's
agent metadata, with `hooks_active` and `adapter_id`).

A stream (a multi-step task) keeps one agent across steps; the driver calls `set_deploy_dir` with each step's
staging, and the next `run` replaces the copy, the context, the skills and the roles. Harbor's `AgentTimeoutError`
terminates only the `docker compose exec` client; the Codex process inside the container may go on editing while
the verifier runs, so `run()` kills it in a `finally`.
"""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import shlex
import tempfile
import tomllib
from typing import Any

from harbor.agents.installed.codex import Codex, CodexOptions
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from pydantic import Field

DEPLOY_MARKER = ".hermit-deploy.json"
ADAPTER_DIR = "hermit"
NOT_UPLOADED = {DEPLOY_MARKER, "tool-calls.jsonl", ".retired-roles"}
# Kills the Codex binary and its npm wrapper; the bracket keeps the pattern from matching this shell's own command.
KILL_CODEX = "command -v pkill >/dev/null 2>&1 && pkill -f '[c]odex' 2>/dev/null; true"


class HermitCodexOptions(CodexOptions):
    """Harbor's Codex kwargs plus Hermit's. `extra="forbid"` is inherited: anything else is rejected at preflight."""

    deploy_dir: str | None = Field(default=None, description="The Codex workspace Hermit deployed the adapter into "
                                                             "(AGENTS.md, .codex/, hermit/<adapter id>/). Omit for "
                                                             "plain Codex.")
    image_repairs: bool = Field(default=True, description="Repair the apt sources of images whose release left the "
                                                          "mirrors (Debian bullseye) before Harbor installs Codex.")
    ensure_python3: bool = Field(default=True, description="Install python3 when the deployment has hooks and the "
                                                           "image lacks it.")


class HermitCodex(Codex):
    options_model = HermitCodexOptions
    options: HermitCodexOptions

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._deploy: Path | None = None
        self._python3_ok: bool | None = None
        self._hooks_active = False
        self._agent_identity: tuple[str, str] | None = None
        self._original_agents_md: str | None = None       # the image's own AGENTS.md, read once per container
        self._task_text = ""
        if self.options.deploy_dir:
            self.set_deploy_dir(self.options.deploy_dir)

    @staticmethod
    def name() -> str:
        return "codex"                                    # Harbor keeps treating the trial as a Codex one

    # ----- the deployment --------------------------------------------------------------------------------------------

    def set_deploy_dir(self, path: str | None) -> None:
        """The workspace the next run deploys (a stream changes it between steps); None for plain Codex."""
        if not path:
            self._deploy = None
            return
        deploy = Path(path).expanduser()
        if not deploy.is_dir():
            raise ValueError(f"deploy_dir is not a directory: {deploy}")
        self._deploy = deploy

    def _deploy_info(self) -> dict:
        path = self._deploy / DEPLOY_MARKER if self._deploy else None
        if not path or not path.is_file():
            return {}
        try:
            return json.loads(path.read_text())
        except ValueError:
            return {}

    @property
    def _hooks_file(self) -> Path | None:
        path = self._deploy / ".codex" / "hooks.json" if self._deploy else None
        return path if path and path.is_file() else None

    def _deploy_config(self) -> dict:
        """The deployed workspace's config.toml; only its [agents.*] tables apply in the container."""
        path = self._deploy / ".codex" / "config.toml" if self._deploy else None
        if not path or not path.is_file():
            return {}
        config = tomllib.loads(path.read_text())
        if config.get("mcp_servers"):
            self.logger.warning("Ignoring the deployed workspace's mcp_servers (%s): no Hermit tool server in the "
                                "container", ", ".join(config["mcp_servers"]))
        return config

    def _role_files(self) -> list[Path]:
        agents = self._deploy / ".codex" / "agents" if self._deploy else None
        return sorted(agents.glob("*.toml")) if agents and agents.is_dir() else []

    def _skill_dirs(self) -> list[Path]:
        skills = self._deploy / ".codex" / "skills" if self._deploy else None
        return sorted(p for p in skills.iterdir() if (p / "SKILL.md").is_file()) if skills and skills.is_dir() else []

    def _adapter_copies(self) -> list[Path]:
        root = self._deploy / ADAPTER_DIR if self._deploy else None
        return sorted(p for p in root.iterdir() if p.is_dir()) if root and root.is_dir() else []

    # ----- install -------------------------------------------------------------------------------------------------

    async def install(self, environment: BaseEnvironment) -> None:
        if self.options.image_repairs:
            await self._repair_apt_sources(environment)
        await super().install(environment)

    async def _repair_apt_sources(self, environment: BaseEnvironment) -> None:
        """Debian 11 (bullseye) images: the bullseye pools on deb.debian.org no longer serve the release's packages,
        so Harbor's `apt-get install curl nodejs npm ripgrep` fails and the trial dies before Codex starts. Point
        main and security at archive.debian.org, accept its expired Release files, refresh the lists. Other images
        are untouched; a failure here is left to Harbor's own install to report."""
        command = ("if grep -qs 'VERSION_CODENAME=bullseye' /etc/os-release && command -v apt-get >/dev/null 2>&1; then "
                   "sed -i -e 's|http://deb.debian.org/debian-security|http://archive.debian.org/debian-security|g' "
                   "-e 's|http://security.debian.org/debian-security|http://archive.debian.org/debian-security|g' "
                   "-e 's|http://deb.debian.org/debian |http://archive.debian.org/debian |g' /etc/apt/sources.list "
                   "/etc/apt/sources.list.d/*.list 2>/dev/null; "
                   "echo 'Acquire::Check-Valid-Until \"false\";' > /etc/apt/apt.conf.d/99hermit-archive; apt-get update; fi")
        try:
            await self.exec_as_root(environment, command=command)
        except Exception as exc:                           # noqa: BLE001 - reported by Harbor's install if it matters
            self.logger.warning("apt source repair skipped: %s", exc)

    async def _ensure_python3(self, environment: BaseEnvironment) -> bool:
        """python3 on PATH, installed with the image's package manager when there is one. False, never an
        exception: a container without Python runs the trial with hooks that may fail open."""
        check = "command -v python3 >/dev/null 2>&1"
        if (await environment.exec(command=check)).return_code == 0:
            return True
        try:
            await self.ensure_system_dependencies(environment, ("python3",))
        except Exception as exc:                           # apt failure, no network: fall back, do not fail the trial
            self.logger.warning("Installing python3 failed: %s", exc)
            return False
        return (await environment.exec(command=check)).return_code == 0

    # ----- config.toml and the files beside it ----------------------------------------------------------------------

    def _build_effective_config(self, openai_base_url: str | None = None) -> dict[str, Any]:
        config = super()._build_effective_config(openai_base_url)
        roles = {path.stem for path in self._role_files()}
        agents = {name: table for name, table in (self._deploy_config().get("agents") or {}).items() if name in roles}
        if agents:
            config["agents"] = {**config.get("agents", {}), **agents}
            config.setdefault("features", {})["multi_agent"] = True
        return config

    async def _ensure_agent_owned_dir(self, environment: BaseEnvironment, path: PurePosixPath) -> tuple[str, str]:
        self._agent_identity = await super()._ensure_agent_owned_dir(environment, path)
        return self._agent_identity

    async def _workdir(self, environment: BaseEnvironment) -> str:
        """Where `codex exec` runs: the task's workdir (Harbor runs every exec there)."""
        workdir = getattr(getattr(environment, "task_env_config", None), "workdir", None)
        if not workdir:
            workdir = ((await self.exec_as_agent(environment, command="pwd")).stdout or "/").strip()
        return workdir

    async def _upload_text(self, environment: BaseEnvironment, text: str, remote_path: str) -> None:
        with tempfile.TemporaryDirectory(prefix="hermit-deploy-") as tmp:
            local = Path(tmp) / PurePosixPath(remote_path).name
            local.write_text(text)
            await environment.upload_file(local, remote_path)

    async def _upload_effective_config(self, environment: BaseEnvironment, config: dict[str, Any], remote_path: str) -> None:
        """Harbor's config.toml, then the deployed workspace. Runs inside run() after $CODEX_HOME exists and is the
        agent user's, before `codex exec`."""
        await super()._upload_effective_config(environment, config, remote_path)
        if self._deploy is None:
            return
        if self._agent_identity is None:
            await self._ensure_agent_owned_dir(environment, self._REMOTE_CODEX_HOME)
        home = PurePosixPath(remote_path).parent
        workdir = await self._workdir(environment)
        hooks = self._hooks_file
        if hooks is not None and self.options.ensure_python3 and self._python3_ok is None:
            self._python3_ok = await self._ensure_python3(environment)
        self._hooks_active = hooks is not None
        owned = []
        await self.exec_as_agent(environment, command=f"mkdir -p {shlex.quote(workdir + '/.codex')}")
        # The adapter's copy: hermit/<id>/ under the workdir, replaced whole (a stream's later step).
        copies = self._adapter_copies()
        if copies:
            remote_root = f"{workdir.rstrip('/')}/{ADAPTER_DIR}"
            await self.exec_as_root(environment, command="rm -rf " + " ".join(shlex.quote(f"{remote_root}/{c.name}") for c in copies))
            await environment.upload_dir(self._deploy / ADAPTER_DIR, remote_root)
            owned.append(remote_root)
        # AGENTS.md: Hermit's context first, then whatever the image shipped (Codex reads the file whole).
        context = (self._deploy / "AGENTS.md").read_text() if (self._deploy / "AGENTS.md").is_file() else ""
        if context:
            if self._original_agents_md is None:
                self._original_agents_md = (await self.exec_as_agent(
                    environment, command=f"cat {shlex.quote(workdir + '/AGENTS.md')} 2>/dev/null || true")).stdout or ""
            if self._original_agents_md.strip():
                context = context.rstrip("\n") + "\n\n" + self._original_agents_md
            await self._upload_text(environment, context, workdir + "/AGENTS.md")
            owned.append(workdir + "/AGENTS.md")
        if self._skill_dirs():
            await self.exec_as_root(environment, command=f"rm -rf {shlex.quote((home / 'skills').as_posix())}")
            await environment.upload_dir(self._deploy / ".codex" / "skills", (home / "skills").as_posix())
            owned.append((home / "skills").as_posix())
        if self._role_files():
            await self.exec_as_root(environment, command=f"rm -rf {shlex.quote((home / 'agents').as_posix())}")
            await environment.upload_dir(self._deploy / ".codex" / "agents", (home / "agents").as_posix())
            owned.append((home / "agents").as_posix())
        remote_hooks = workdir + "/.codex/hooks.json"
        if hooks is not None:
            await environment.upload_file(hooks, remote_hooks)
            owned.append(remote_hooks)
        else:
            await self.exec_as_root(environment, command=f"rm -f {shlex.quote(remote_hooks)}")
        if owned:
            uid, gid = self._agent_identity
            await self.exec_as_root(environment, command=f"chown -R {uid}:{gid} {shlex.join(owned)} && chmod -R u+rwX "
                                                         f"{shlex.join(owned)}")

    # ----- the session -----------------------------------------------------------------------------------------------

    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        self._task_text = instruction
        try:
            await super().run(instruction, environment, context)
        finally:
            if self._deploy is not None:
                try:                                      # a timed-out Codex must not keep editing under the verifier
                    await environment.exec(command=KILL_CODEX, user="root", timeout_sec=30)
                except Exception as exc:
                    self.logger.debug("Killing Codex after the session failed: %s", exc)

    def populate_context_post_run(self, context: AgentContext) -> None:
        super().populate_context_post_run(context)
        context.metadata = {**(context.metadata or {}), "hooks_active": self._hooks_active,
                            "adapter_deployed": self._deploy is not None, "python3": self._python3_ok,
                            "adapter_id": self._deploy_info().get("adapter_id")}
