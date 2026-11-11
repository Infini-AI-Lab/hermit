"""HermitClaude: Claude Code in a Harbor trial, its session launched from Hermit's plan (harborside.hermit).

Imported by Harbor as `harborside.claude:HermitClaude`. Harbor's Claude Code brings the install (its bootstrap script
into ~/.local/bin, npm on Alpine) at `version`, after the apt repair of an image whose release left the mirrors,
unless the binary Hermit's runner mounts (`claude_bin`: the machine's Claude Code, its folder mounted read-only at
/opt/hermit-claude) answers `--version` as `version` and the task's agent user may execute it; then it is linked onto
PATH and nothing is installed.

The session is the plan's, as harborside.hermit runs every CLI's: Hermit's own `claude -p --output-format stream-json
...` (its denied tools, web search either way, on a login the last `--settings` that keeps it on the subscription)
with its own CLAUDE_CONFIG_DIR (/installed-agent/hermit-home), the prompt on stdin, the deployment's CLAUDE.md and
.claude/ in the workdir (hooks run from the adapter's copy under $CLAUDE_PROJECT_DIR), and the stream
agent-claude.jsonl. On the subscription (`login: claude`) the token CLAUDE_CODE_OAUTH_TOKEN is the plan's env_by_fd
secret: passed to `docker compose exec` by name, never on a command line or in a file, then into a pipe on descriptor
3, which the CLI reads once (CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR). In the container only the exec's bash, until it
execs the launch script (the token unset, and `env -u`), and the subshell that writes the token into the pipe (a fork
of that bash), until its printf ends, have it in their environment: the launch script, the CLI and what they start do
not. The task's container is the session's sandbox: its commands run as the CLI's user there, and a command that read
the CLI's memory could still find the token.

Harbor's own run (its command, its claude-code.txt and CLAUDE_CONFIG_DIR under /logs/agent/sessions, its auth
environment) is not used.
"""
from __future__ import annotations

from harbor.agents.installed.claude_code import ClaudeCode, ClaudeCodeOptions
from pydantic import Field

from harborside.hermit import HermitInstalled, HermitOptions


class HermitClaudeOptions(HermitOptions, ClaudeCodeOptions):
    """Harbor's Claude Code kwargs (accepted, unused: the plan holds the session) and Hermit's. `extra="forbid"`."""

    claude_bin: str | None = Field(default=None, description="A Claude Code binary mounted in the container, used when "
                                                             "its --version is `version` and the task's agent user "
                                                             "may execute it, instead of Harbor's per-trial install.")


class HermitClaude(HermitInstalled, ClaudeCode):
    CLI = "claude"
    PROGRAM = "claude"
    BIN_OPTION = "claude_bin"
    RUN_PATH = ("$HOME/.local/bin",)                  # where Harbor's bootstrap install puts `claude`
    options_model = HermitClaudeOptions
    options: HermitClaudeOptions
