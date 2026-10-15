"""Convert an adapter into a plugin in the native format of Claude Code, Codex, Pi or Hermes."""
from __future__ import annotations

from importlib import import_module
from pathlib import Path

from harnessadapter.plugins.common import Unsupported

EMITTERS = {"claude-code": "claude_plugin", "codex": "codex_plugin", "pi": "pi_package", "hermes": "hermes_plugin"}
TARGETS = tuple(EMITTERS)


def build(target: str, adapter: Path, output: Path, python: str, *, allow_lossy: bool = False) -> dict:
    if target not in EMITTERS:
        raise ValueError(f"Unknown target: {target}")
    module = import_module(f"harnessadapter.plugins.{EMITTERS[target]}")
    return module.build(adapter, output, python, allow_lossy=allow_lossy)


__all__ = ["TARGETS", "Unsupported", "build"]
