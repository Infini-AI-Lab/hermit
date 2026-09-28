"""Optimizer Evolvers: the plugin contract and its registry.

A session Evolver (basic, mwm, ...) is a prompt for a learner session that
edits the candidate with file tools. An optimizer Evolver is an algorithm
that searches over adapter texts by rolling candidates out in an Environment
and keeping what scores better. Both are Evolver definition directories:

    my-optimizer/
      evolver.toml      kind = "optimizer", plugin = "module:Class" | "file.py:Class"
      README.md

    [settings]          defaults for the plugin; a run's config overrides them

The plugin class has a name, and run(context) -> Outcome. Everything else
(snapshotting the source, materializing and committing the candidate, the
Training State directory, the ledger of optimizer-model calls, the result
record) is the framework's, in harnessadapter.optimize.session.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import importlib
import importlib.util
from pathlib import Path
import sys
import tomllib
from typing import Callable, Protocol

from harnessadapter.optimize.environment import Environment
from harnessadapter.optimize.surface import CandidateStore, TextSurface
from harnessadapter.paths import resolve_evolver


@dataclass
class OptimizeContext:
    """What a plugin receives. The plugin must not write outside state_dir
    and work_dir, must evaluate only through environment, and must never ask
    it for the test split."""
    surface: TextSurface
    environment: Environment
    lm: Callable
    state_dir: Path
    work_dir: Path
    settings: dict
    log: Callable[[str], None]
    store: CandidateStore = field(init=False)

    def __post_init__(self):
        self.store = CandidateStore(self.surface, self.work_dir / "candidates")

    def seed(self) -> dict[str, str]:
        return self.surface.read()


@dataclass
class Outcome:
    """The plugin's answer: its selected candidate texts, and a JSON summary
    (scores, lineage, budget used) that the framework records verbatim."""
    candidate: dict[str, str]
    summary: dict


class OptimizerPlugin(Protocol):
    name: str

    def run(self, context: OptimizeContext) -> Outcome: ...


@dataclass
class OptimizerDefinition:
    root: Path
    name: str
    plugin: OptimizerPlugin
    settings: dict

    def merged(self, overrides: dict | None) -> dict:
        unknown = set(overrides or {}) - set(self.settings)
        if unknown:
            raise ValueError(f"Unknown {self.name} settings: {', '.join(sorted(unknown))}")
        return {**self.settings, **(overrides or {})}


def is_optimizer(reference: str) -> bool:
    root = resolve_evolver(reference)
    config = root / "evolver.toml"
    return config.is_file() and tomllib.loads(config.read_text()).get("kind") == "optimizer"


def _load_class(spec: str, root: Path):
    target, sep, attribute = spec.partition(":")
    if not sep or not attribute.isidentifier():
        raise ValueError("plugin must be 'module:Class' or 'file.py:Class'")
    if target.endswith(".py"):
        path = (root / target).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError("A file plugin must live inside its Evolver definition directory")
        name = f"harnessadapter_optimizer_{root.name.replace('-', '_')}"
        module_spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(module_spec)
        sys.modules[name] = module
        module_spec.loader.exec_module(module)
    else:
        module = importlib.import_module(target)
    return getattr(module, attribute)


def load_optimizer(reference: str) -> OptimizerDefinition:
    root = resolve_evolver(reference).resolve()
    config_path = root / "evolver.toml"
    if not config_path.is_file():
        raise ValueError(f"No Evolver definition at {root}")
    config = tomllib.loads(config_path.read_text())
    if config.get("kind") != "optimizer":
        raise ValueError(f"{reference} is a session Evolver; run it with `harnessadapter evolve`")
    if set(config) - {"kind", "plugin", "settings", "description"}:
        raise ValueError("Optimizer Evolver configuration supports kind, plugin, description and settings")
    plugin_class = _load_class(config.get("plugin", ""), root)
    plugin = plugin_class()
    return OptimizerDefinition(root=root, name=getattr(plugin, "name", root.name), plugin=plugin,
                               settings=dict(config.get("settings", {})))
