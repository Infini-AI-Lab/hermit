#!/usr/bin/env python3
"""`codex` for SkillOpt's Codex harness, with reads limited to what the rollout may see.

SkillOpt runs `codex exec ... --sandbox workspace-write ... -C <rollout>/predictions/<id>/codex_exec ...`
(CODEX_EXEC_PATH names this script). Codex's legacy workspace-write sandbox limits writes and the network but lets a
session read the whole disk, so it could read the answers, the records of other runs and other sessions. This script
runs the same command with Codex's own sandbox set by a permission profile instead of `--sandbox`: the same
workspace-write sandbox (the workspace and TMPDIR writable, no network), but readable are only the platform's
defaults (less /private/tmp, /private/var/folders and /Volumes), the command-line developer tools, the workspace and
the paths in OFFICEQA_CODEX_READ (the corpus). Each
rollout gets a fresh CODEX_HOME and TMPDIR next to `predictions/`, so no session sees another; the session's shell
gets no API keys.

CODEX_HOME names the home whose config.toml (the provider settings) each rollout's home starts from.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

READ = ["/Library/Developer/CommandLineTools"]                  # /usr/bin/python3 and other developer tools
DENY = ["/private/tmp", "/private/var/folders", "/Volumes"]     # readable by the platform's defaults otherwise


def settings(template: str, readable: list[str]) -> str:
    """The template's settings (projects Codex trusted dropped) with a permission profile as the default."""
    lines, skip = [], False
    for line in template.splitlines():
        if line.startswith("["):
            skip = line.startswith("[projects.")
        if not skip:
            lines.append(line)
    return "\n".join([
        'default_permissions = "officeqa"',
        *lines,
        "",
        "[shell_environment_policy]",
        'exclude = ["*KEY*", "*TOKEN*", "*SECRET*"]',
        "",
        "[permissions.officeqa]",
        'description = "workspace-write with reads limited to the system, the workspace and the corpus"',
        "",
        "[permissions.officeqa.filesystem]",
        '":minimal" = "read"',
        *(f"{json.dumps(p)} = \"read\"" for p in readable),
        *(f"{json.dumps(p)} = \"deny\"" for p in DENY),
        '":tmpdir" = "write"',
        "",
        '[permissions.officeqa.filesystem.":workspace_roots"]',
        '"." = "write"',
        "",
    ])


def strip_sandbox(args: list[str]) -> list[str]:
    """The arguments without `--sandbox`/`-s` and the legacy sandbox's settings (the profile replaces them)."""
    out, i = [], 0
    while i < len(args):
        if args[i] in ("--sandbox", "-s"):
            i += 2
            continue
        if args[i] == "-c" and i + 1 < len(args) and args[i + 1].startswith("sandbox_workspace_write."):
            i += 2
            continue
        out.append(args[i])
        i += 1
    return out


def main() -> None:
    args = sys.argv[1:]
    work = Path(args[args.index("-C") + 1]).resolve()
    rollout = work.parents[2]                                 # <rollout>/predictions/<id>/codex_exec
    home, tmp = rollout / "codex_home", rollout / "tmp"
    home.mkdir(exist_ok=True)
    tmp.mkdir(exist_ok=True)
    if not (home / "config.toml").exists():
        template = (Path(os.environ["CODEX_HOME"]) / "config.toml").read_text()
        readable = READ + [str(Path(p).resolve()) for p in os.environ.get("OFFICEQA_CODEX_READ", "").split(os.pathsep) if p]
        (home / "config.toml").write_text(settings(template, readable))
    me = Path(__file__).resolve()
    codex = next(str(Path(p) / "codex") for p in os.environ["PATH"].split(os.pathsep)
                 if (Path(p) / "codex").is_file() and (Path(p) / "codex").resolve() != me)
    env = dict(os.environ, CODEX_HOME=str(home), TMPDIR=str(tmp), xcrun_nocache="1")
    os.execvpe(codex, [codex, *strip_sandbox(args)], env)


if __name__ == "__main__":
    main()
