"""Convert an adapter into the installable form of a host: Claude Code, Codex, Pi or Hermes."""
from __future__ import annotations

from pathlib import Path

from harnessadapter.plugins.common import Unsupported

TARGETS = ("claude-code", "codex", "pi", "hermes")
# Claude Code and Codex install the same directory, so one artifact covers both.
SHARED = ("claude-code", "codex")


def build(target: str, adapter: Path, output: Path, python: str, *, allow_lossy: bool = False) -> dict:
    if target in SHARED:
        from harnessadapter.plugins.agent_plugin import build as shared
        return shared(adapter, output, python, target=target, allow_lossy=allow_lossy)
    if target == "pi":
        from harnessadapter.plugins.pi_package import build as run
        return run(adapter, output, python, allow_lossy=allow_lossy)
    if target == "hermes":
        from harnessadapter.plugins.hermes_import import build as run
        return run(adapter, output, python, allow_lossy=allow_lossy)
    raise ValueError(f"Unknown target: {target}")


__all__ = ["TARGETS", "SHARED", "Unsupported", "build"]
