"""The run config: framework defaults, then run.yaml, then command-line overrides (key.sub=value)."""
from __future__ import annotations

import copy
import os
from pathlib import Path
import re

import yaml

DEFAULTS = Path(__file__).with_name("defaults.yaml")
TEMPLATES = Path(__file__).resolve().parent / "algorithms" / "templates"


def _merge(base: dict, extra: dict, prefix: str = "") -> dict:
    out = copy.deepcopy(base)
    for key, value in (extra or {}).items():
        if key not in out:
            raise ValueError(f"Unknown config key: {prefix}{key}")
        if isinstance(out[key], dict) and isinstance(value, dict) and key not in ("params", "llm"):
            out[key] = _merge(out[key], value, f"{prefix}{key}.")
        else:
            out[key] = value
    return out


FREE = ("params", "llm")   # sections whose keys the framework does not know in advance


def _override(config: dict, item: str) -> None:
    key, sep, raw = item.partition("=")
    if not sep:
        raise ValueError(f"Overrides look like key.sub=value, got {item!r}")
    value = yaml.safe_load(raw) if raw != "" else None
    parts = key.split(".")
    node, free = config, False
    for part in parts[:-1]:
        if part not in node or node[part] is None:
            if not (free or part in FREE):
                raise ValueError(f"Unknown config key: {key}")
            node[part] = {}
        if not isinstance(node[part], dict):
            raise ValueError(f"{key}: {part} is not a section")
        free = free or part in FREE
        node = node[part]
    if parts[-1] not in node and not free:
        raise ValueError(f"Unknown config key: {key}")
    node[parts[-1]] = value


class Config:
    def __init__(self, data: dict, base: Path):
        self.data = data
        self.base = Path(base).resolve()     # relative paths in the config are relative to its directory

    @classmethod
    def load(cls, path: Path, overrides: list[str] = ()) -> "Config":
        defaults = yaml.safe_load(DEFAULTS.read_text())
        user = yaml.safe_load(Path(path).read_text()) or {}
        data = _merge(defaults, user)
        for item in overrides:
            _override(data, item)
        if data["pipeline"].get("run") and not data["agent"].get("name"):     # the older spelling
            data["agent"]["name"] = data["pipeline"]["run"]
            if data["pipeline"].get("params"):
                data["agent"]["params"] = {**data["agent"].get("params", {}), **data["pipeline"]["params"]}
            if data["pipeline"].get("concurrency") is not None:
                data["agent"]["concurrency"] = data["pipeline"]["concurrency"]
        config = cls(data, Path(path).parent)
        config.validate()
        return config

    def __getitem__(self, key):
        return self.data[key]

    def path(self, value) -> Path | None:
        if value in (None, ""):
            return None
        p = Path(os.path.expandvars(str(value))).expanduser()
        return p if p.is_absolute() else (self.base / p).resolve()

    def validate(self) -> None:
        d = self.data
        for key in ("tasks", "train"):
            if not d["data"][key]:
                raise ValueError(f"data.{key} is required")
        if not d["agent"]["name"]:
            raise ValueError("agent.name is required")
        if not d["judge"]["path"]:
            raise ValueError("judge.path is required")
        if not d["template"]:
            raise ValueError("template is required")
        if not d["evolver"]["name"]:
            raise ValueError("evolver.name is required")
        if d["sandbox"]["network"] not in ("none", "open", "restricted"):
            raise ValueError("sandbox.network must be none, open or restricted")
        if not isinstance(d["data"]["shuffle"], bool):
            raise ValueError("data.shuffle must be true or false")
        if int(d["data"]["step_size"]) < 1 or int(d["trainer"]["epochs"]) < 1:
            raise ValueError("data.step_size and trainer.epochs must be positive")
        if d["trainer"]["run_name"] and not re.fullmatch(r"[A-Za-z0-9][\w.-]{0,80}", str(d["trainer"]["run_name"])):
            raise ValueError("trainer.run_name may use letters, digits, '.', '_' and '-'")

    def template_dir(self) -> Path:
        """`template` is a path to a template directory, or `id@version` / `id` of a built-in template."""
        value = str(self.data["template"])
        as_path = self.path(value)
        if as_path is not None and (as_path / "adapter.yaml").is_file():
            return as_path
        ident, _, version = value.partition("@")
        for candidate in sorted(TEMPLATES.iterdir()) if TEMPLATES.is_dir() else []:
            manifest = candidate / "adapter.yaml"
            if manifest.is_file():
                tmpl = (yaml.safe_load(manifest.read_text()) or {}).get("template", {})
                if tmpl.get("id") == ident and (not version or tmpl.get("version") == version):
                    return candidate
        raise ValueError(f"No template {value!r} (neither a directory with adapter.yaml nor a built-in id@version)")

    def run_name(self) -> str:
        if self.data["trainer"]["run_name"]:
            return str(self.data["trainer"]["run_name"])
        return f"{self.data['evolver']['name']}-{Path(str(self.data['template'])).name.split('@')[0]}".replace("/", "-")

    def run_dir(self) -> Path:
        return self.path(self.data["trainer"]["output_dir"]) / self.run_name()

    def tree_dir(self, template_id: str) -> Path:
        if self.data["trainer"]["tree_dir"]:
            return self.path(self.data["trainer"]["tree_dir"])
        return self.path(self.data["trainer"]["output_dir"]) / "trees" / template_id
