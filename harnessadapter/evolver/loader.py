"""Find an Evolver by name or path, read its evolver.yaml, and instantiate its class."""
from __future__ import annotations

from dataclasses import dataclass, field
import importlib
import importlib.util
from pathlib import Path
import sys

import yaml

from harnessadapter.evolver.base import Evolver

BUILTIN = Path(__file__).resolve().parents[1] / "algorithms" / "evolvers"
NEEDS = {"validation_data", "environment", "send_material_out"}


@dataclass
class EvolverSpec:
    name: str
    directory: Path
    class_path: str
    needs: list[str]
    params: dict = field(default_factory=dict)


def find(name_or_path: str, base: Path | None = None) -> EvolverSpec:
    candidates = []
    p = Path(name_or_path)
    if base is not None and not p.is_absolute():
        candidates.append(Path(base) / p)
    candidates += [p, BUILTIN / name_or_path]
    for c in candidates:
        manifest = c if c.suffix in (".yaml", ".yml") else c / "evolver.yaml"
        if manifest.is_file():
            data = yaml.safe_load(manifest.read_text()) or {}
            unknown = set(data) - {"class", "needs", "params", "description"}
            if unknown:
                raise ValueError(f"Unknown evolver.yaml keys: {sorted(unknown)}")
            if not isinstance(data.get("class"), str) or ":" not in data["class"]:
                raise ValueError("evolver.yaml needs class: 'module:Class' or 'file.py:Class'")
            needs = list(data.get("needs") or [])
            bad = set(needs) - NEEDS
            if bad:
                raise ValueError(f"Unknown needs {sorted(bad)}; allowed: {sorted(NEEDS)}")
            return EvolverSpec(name=manifest.parent.name, directory=manifest.parent.resolve(),
                               class_path=data["class"], needs=needs, params=dict(data.get("params") or {}))
    raise ValueError(f"No Evolver named {name_or_path!r} (looked for evolver.yaml in {[str(c) for c in candidates]})")


def load_class(spec: EvolverSpec) -> type[Evolver]:
    module_part, _, cls_name = spec.class_path.partition(":")
    if module_part.endswith(".py"):
        path = (spec.directory / module_part).resolve()
        name = f"_evolver_{spec.name.replace('-', '_')}_{path.stem}"
        mod_spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(mod_spec)
        sys.modules[name] = module
        mod_spec.loader.exec_module(module)
    else:
        module = importlib.import_module(module_part)
    cls = getattr(module, cls_name)
    if not (isinstance(cls, type) and issubclass(cls, Evolver)):
        raise ValueError(f"{spec.class_path} is not an Evolver subclass")
    return cls


def instantiate(spec: EvolverSpec, params: dict, *, backends: dict | None = None,
                default_model=None) -> tuple[Evolver, dict]:
    """The Evolver instance and its effective params (manifest defaults overridden by the run config). An Evolver
    that is a Codex or Claude Code agent (`backend` in its params) gets its model bound: a `<backend>/<model>`
    reference in `model` (or the run's default) becomes the provider settings in `args` or `env`."""
    merged = {**spec.params, **(params or {})}
    if merged.get("backend") in ("codex", "claude") and (merged.get("model") or default_model) is not None:
        from harnessadapter.backends import bind_agent_params, registry
        merged = bind_agent_params(merged, merged["backend"], backends if backends is not None else registry(),
                                   default_model)
    cls = load_class(spec)
    evolver = cls()
    evolver.directory = spec.directory      # lets built-ins find their prompt files
    return evolver, merged
