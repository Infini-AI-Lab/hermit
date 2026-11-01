"""Forwarding for import paths that moved in the agent layers (docs/framework/agent-layers.md), kept for one release."""
from __future__ import annotations

import importlib
import sys
import warnings


def forward(old: str, new: str, submodules: dict[str, str] | None = None):
    """Make the module `old` forward to the module `new`, and `old.<name>` to each `submodules` target, with a
    DeprecationWarning that names the new path. A package's `__getattr__` is what this returns."""
    warnings.warn(f"{old} moved to {new}; the old path works for one release", DeprecationWarning, stacklevel=3)
    module = importlib.import_module(new)
    targets = dict(submodules or {})
    for name, target in targets.items():
        sys.modules[f"{old}.{name}"] = importlib.import_module(target)
    if not targets:
        sys.modules[old] = module
        return None

    def __getattr__(name: str):
        if name in targets:
            return sys.modules[f"{old}.{name}"]
        return getattr(module, name)
    return __getattr__
