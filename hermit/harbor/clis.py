"""The CLIs a Harbor trial runs Hermit's way, one row each: the one place the runner learns what differs per CLI.

    acting_agent: {cli: codex}            # or claude, pi, opencode, dsh, mini-swe-agent, answer

A row names the HermitXXX class Harbor imports (`harborside.<module>:<Class>`, under Harbor's interpreter), the CLI
Base Agent (`hermit.agent.base.cli.CLIS`) whose home, command, environment and stream reader the trial uses, the
deployment's context file and the limit on it once merged with the image's own (less a byte for each occurrence in
Hermit's text of `context_escaped`, which the CLI escapes by one more), and the settings a container session needs
besides the run's (Codex's own sandbox off: the task's container is the boundary, as Harbor's own
`--dangerously-bypass-approvals-and-sandbox`; dsh's likewise). `version` is what an in-container install installs
when the config names none (the builds Hermit's tests were recorded on, docs/get-started/installation.md), not a
pin: a mounted binary (`<cli>_bin`) runs whatever it is. `nvm`: the CLI is an npm program the install put under the
agent user's nvm, sourced before the session's own HOME is set. Every CLI's home in the container is CONTAINER_HOME.

harborside never imports this module (it runs under Harbor's interpreter: harbor, two of Harbor's own dependencies
and the standard library only): what a trial needs of a row reaches it in the trial's launch plan
(hermit.harbor.launch).
"""
from __future__ import annotations

from hermit.agent.base.cli import DSH_INSTRUCTION_BYTES, DSH_INSTRUCTION_FRAME

CONTAINER_HOME = "/installed-agent/hermit-home"          # the CLI's home in the container, for every CLI
BIN_MOUNT = "/opt/hermit-{cli}"                          # where `<cli>_bin`'s folder is mounted, read-only
DEFAULT_CLI = "codex"

HARBOR_CLIS: dict[str, dict] = {
    # Harbor's install puts codex under the agent user's nvm on a glibc image (it links none into /usr/local/bin); a
    # mounted codex_bin's folder still comes first on PATH, put there after nvm is loaded
    "codex": {"import_path": "harborside.codex:HermitCodex", "backend": "codex", "context_file": "AGENTS.md",
              "context_limit": None,      # Codex's own cap is project_doc_max_bytes (launch: the limit after the merge)
              "nvm": True, "settings": {"codex_sandbox": "danger-full-access", "project_doc_max_bytes": 262144}},
    "claude": {"import_path": "harborside.claude:HermitClaude", "backend": "claude", "context_file": "CLAUDE.md",
               "context_limit": None,
               # bypassPermissions as the container's root, and no update of the CLI mid-trial
               "env": {"IS_SANDBOX": "1", "DISABLE_AUTOUPDATER": "1"}},
    "pi": {"import_path": "harborside.pi:HermitPi", "backend": "pi", "context_file": "AGENTS.md", "context_limit": None,
           "nvm": True, "version": "1.5.0"},
    "opencode": {"import_path": "harborside.opencode:HermitOpenCode", "backend": "opencode",
                 "context_file": "AGENTS.md", "context_limit": None, "version": "2.2.6"},
    # dsh cuts the project's instructions at DSH_INSTRUCTION_BYTES, frame included, and tells only the model; in its
    # frame each `</system-reminder>` of the text is escaped by one more byte (hermit.hosts.dsh counts them too)
    "dsh": {"import_path": "harborside.dsh:HermitDsh", "backend": "dsh", "context_file": "AGENTS.md",
            "context_limit": DSH_INSTRUCTION_BYTES - DSH_INSTRUCTION_FRAME, "context_escaped": "</system-reminder>",
            "nvm": True, "version": "0.2.2-rc.1", "settings": {"permission_mode": "danger-full-access"}},
    "mini-swe-agent": {"import_path": "harborside.mini_swe_agent:HermitMiniSweAgent", "backend": "mini-swe-agent",
                       "context_file": None, "context_limit": None, "version": "2.4.6"},
    # The answer-only agent: one model call from Harbor's own process, its answer written into the container
    "answer": {"import_path": "harborside.answer:HermitAnswer", "backend": None, "context_file": None,
               "context_limit": None},
}


def entry(cli: str) -> dict:
    """The row of `cli`, every key present (absent: none, false, empty)."""
    row = HARBOR_CLIS[cli]
    return {"settings": {}, "env": {}, "nvm": False, "version": None, "context_escaped": None, **row}


def cli_of(acting_agent: dict | None) -> str | None:
    """The table's CLI an `acting_agent` spec names: `{cli: <key>}`, `{import_path: <a row's class>}` (a harborside
    class by its name), the default `codex` for a spec that names no agent; None for a Harbor built-in by `name` or
    another class, which runs as Harbor installs it (no adapter, no launch plan)."""
    spec = dict(acting_agent or {})
    cli, path, name = spec.get("cli"), spec.get("import_path"), spec.get("name")
    if name and path:
        raise ValueError("agent.params.acting_agent names both a name and an import_path")
    if cli and (name or path):
        raise ValueError(f"agent.params.acting_agent names cli {cli!r} and {'name' if name else 'import_path'} "
                         f"{name or path!r}: give one (cli names the class itself)")
    if cli:
        if cli not in HARBOR_CLIS:
            raise ValueError(f"agent.params.acting_agent.cli is one of {', '.join(HARBOR_CLIS)}, not {cli!r}")
        return str(cli)
    if path:
        module, _, cls = str(path).partition(":")
        for key, row in HARBOR_CLIS.items():
            own_class = row["import_path"].partition(":")[2]
            if path == row["import_path"] or (module.startswith("harborside.") and cls == own_class):
                return key
        return None
    if name:
        return None
    return DEFAULT_CLI


def bin_mount(cli: str) -> str:
    """Where a trial's container sees the folder of `<cli>_bin`."""
    return BIN_MOUNT.format(cli=cli)
