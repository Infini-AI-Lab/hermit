"""The run config: framework defaults, then run.yaml, then command-line overrides (key.sub=value)."""
from __future__ import annotations

import copy
import os
from pathlib import Path
import re

import yaml

from hermit.adapter.manifest import MANIFEST
from hermit.agent.model.backends import normalize_config

DEFAULTS = Path(__file__).with_name("defaults.yaml")
TEMPLATES = Path(__file__).resolve().parent / "adapter" / "templates"


# Keys of the sandbox modes the refactor removed (docs/developer/sandbox-design.md): where each went.
MOVED = {
    "sandbox.profiles": "the task sandbox is `environment` (read, write, packages, env, network, allowed_hosts); the "
                        "learner's is `sandbox.evolver`; a judge's place is `judge.mode`",
    "sandbox.network": "`environment.network` for the acting agent (no-network | allowlist | public); the learner "
                       "reaches only its models, the tool endpoint and `sandbox.evolver.allowed_hosts`",
    "sandbox.allow": "`environment.allowed_hosts` or `sandbox.evolver.allowed_hosts`",
    "sandbox.read": "`evolver_access.environment` (paths the learner reads) or `environment.read` (the agent)",
    "sandbox.packages": "`environment.packages`",
    "sandbox.env": "`environment.env` or `sandbox.evolver.env`",
    "sandbox.audit": "removed: under an allowlist an undeclared path does not exist; the launch records remain",
}


def _merge(base: dict, extra: dict, prefix: str = "") -> dict:
    out = copy.deepcopy(base)
    for key, value in (extra or {}).items():
        if f"{prefix}{key}" in MOVED:
            raise ValueError(f"config key {prefix}{key} was removed with the old sandbox modes: {MOVED[prefix + key]}")
        if key not in out:
            raise ValueError(f"Unknown config key: {prefix}{key}")
        if isinstance(out[key], dict) and isinstance(value, dict) and key not in FREE:
            out[key] = _merge(out[key], value, f"{prefix}{key}.")
        else:
            out[key] = value
    return out


FREE = ("params", "llm", "backends", "container", "resources",   # sections whose keys the framework does not know
        "model", *("codex", "claude", "pi", "mini-swe-agent", "opencode", "dsh", "react", "direct"))   # a layer's
                                                                                                     # (agent-layers.md)
BASES = {"codex": "codex", "claude": "claude-code", "claude-code": "claude-code", "pi": "pi",
         "mini-swe-agent": "mini-swe-agent", "opencode": "opencode", "dsh": "dsh", "react": "react", "direct": None}
CLI_BASES = ("codex", "claude-code", "pi", "mini-swe-agent", "opencode", "dsh")   # the bases that are a CLI (_model)
MODEL_KEYS = ("login", "effort", "price")           # Layer 1 settings a CLI takes beside its model
# A login's plan settings: Layer 2's (agent.<base>, evolver.<base>), refused beside the model's name.
PLAN_KEYS = {"plan_dir", "plan_max_wait_s", "plan_rerun", "usage_credits", "login_root"}


def _put(params: dict, key: str, value, where: str) -> None:
    if key in params and params[key] != value:
        raise ValueError(f"{where}: {key} is set twice ({params[key]!r} and {value!r})")
    params[key] = value


def _chat_spec(model, where: str):
    """Layer 1's settings for a chat-completions caller (react, direct): the model's spec, with `effort` as the
    request's `reasoning_effort`."""
    if not isinstance(model, dict):
        return model
    spec = dict(model)
    if spec.get("cli"):                  # a model only a CLI reaches: the Base Agent's settings as they are
        return spec
    if "name" in spec or "login" in spec:
        raise ValueError(f"{where}.model: name and login are a CLI's; react and direct take ref, a <backend>/<model> "
                         "reference")
    ref = spec.pop("ref", None)
    effort = spec.pop("effort", None)
    if effort is not None:
        spec["extra_body"] = {**(spec.get("extra_body") or {}), "reasoning_effort": effort}
    if ref is not None:
        spec["model"] = ref
    return spec if set(spec) != {"model"} else spec["model"]


def _model(params: dict, model, where: str, cli: bool = True) -> None:
    """Layer 1's settings into the flat params: the model (a `<backend>/<model>` reference as `ref`, or the CLI's
    own model name as `name`), and login, effort and price beside it for a CLI; any other key goes with the model as
    its spec. For react and direct the whole of it is the model's spec (`_chat_spec`)."""
    if model is None:
        return
    if not cli:
        _put(params, "model", _chat_spec(model, where), where)
        return
    if not isinstance(model, dict):
        _put(params, "model", model, where)
        return
    model = dict(model)
    ref, own = model.pop("ref", None), model.pop("name", None)
    if (ref is None) == (own is None):
        raise ValueError(f"{where}.model needs one of ref (a <backend>/<model> reference) and name (the CLI's own "
                         "model, with login)")
    name = ref if ref is not None else own
    for key in MODEL_KEYS:
        if key in model:
            _put(params, key, model.pop(key), where)
    misplaced = sorted(set(model) & PLAN_KEYS)
    if misplaced:                 # the CLI's, not the model's: beside the name they would reach the CLI as its model
        raise ValueError(f"{where}.model: {', '.join(misplaced)} belong to how the CLI runs, not to the model: give "
                         f"them in {where}.<base> (for example {where}.claude or {where}.codex)")
    _put(params, "model", {"model": name, **model} if model else name, where)


def layered(data: dict) -> None:
    """The per-layer form of `agent` and `evolver` (docs/framework/agent-layers.md#settings-by-layer) as the flat
    form the framework reads: `params` (Layer 3), `base` and its section (Layer 2), `model` (Layer 1). A key given in
    two layers is refused."""
    agent = data["agent"]
    base = agent.pop("base", None)
    model = agent.pop("model", None)
    sections = {b: agent.pop(b, None) for b in ("codex", "claude", "pi", "mini-swe-agent", "opencode", "dsh", "react",
                                                 "direct")}
    if base is None:
        if model is not None or any(sections.values()):
            raise ValueError("agent.model and agent.<base> need agent.base (codex, claude, pi, mini-swe-agent, "
                             "opencode, dsh, react or direct)")
    else:
        if base not in BASES:
            raise ValueError(f"agent.base must be one of {sorted(BASES)}, not {base!r}")
        builtin = {"codex": "codex", "claude-code": "claude-code", "claude": "claude-code", "pi": "pi",
                   "mini-swe-agent": "mini-swe-agent", "opencode": "opencode", "dsh": "dsh", "react": "react",
                   "cli": None}
        if not agent.get("name"):
            if BASES[base] is None:
                raise ValueError("agent.base: direct needs agent.name (the task's own loop calls it)")
            agent["name"] = BASES[base]
        params = dict(agent.get("params") or {})
        if agent["name"] in builtin:
            if agent["name"] == "cli" and BASES[base] in CLI_BASES:
                _put(params, "backend", "claude" if BASES[base] == "claude-code" else BASES[base], "agent")
            elif builtin[agent["name"]] != BASES[base]:
                raise ValueError(f"agent.name {agent['name']} and agent.base {base} name different agents: give one")
        else:
            _put(params, "base", base, "agent")                 # a task's own agent picks its Base Agent itself
        _model(params, model, "agent", cli=BASES[base] in CLI_BASES)
        for key, value in (sections.get("claude" if base == "claude-code" else base) or {}).items():
            _put(params, key, value, f"agent.{base}")
        agent["params"] = params
    evolver = data["evolver"]
    base = evolver.pop("base", None)
    model = evolver.pop("model", None)
    sections = {b: evolver.pop(b, None) for b in ("codex", "claude", "pi", "opencode", "dsh", "react", "direct")}
    if base is None:
        if model is not None or any(sections.values()):
            raise ValueError("evolver.model and evolver.<base> need evolver.base (codex, claude, pi, opencode, dsh or "
                             "direct)")
        return
    if base not in ("codex", "claude", "claude-code", "pi", "opencode", "dsh", "direct"):
        raise ValueError(f"evolver.base must be codex, claude, pi, opencode, dsh or direct, not {base!r}")
    params = dict(evolver.get("params") or {})
    if base == "direct":
        if model is not None:
            llm = _chat_spec(model, "evolver")
            if data.get("llm") not in (None, llm):
                raise ValueError("evolver.model and llm both name the Evolver's model: give one")
            data["llm"] = llm
    else:
        _put(params, "backend", "claude" if base == "claude-code" else base, "evolver")
        _model(params, model, "evolver")
    for key, value in (sections.get("claude" if base == "claude-code" else base) or {}).items():
        _put(params, key, value, f"evolver.{base}")
    evolver["params"] = params


def _override(config: dict, item: str) -> None:
    key, sep, raw = item.partition("=")
    if not sep:
        raise ValueError(f"Overrides look like key.sub=value, got {item!r}")
    value_keys = []                                   # a whole section given at once: the keys it names too

    def walk(prefix, node):
        if isinstance(node, dict):
            for k, v in node.items():
                value_keys.append(f"{prefix}.{k}")
                walk(f"{prefix}.{k}", v)
    for moved, where in MOVED.items():
        if key == moved or key.startswith(moved + "."):
            raise ValueError(f"config key {moved} was removed with the old sandbox modes: {where}")
    walk(key, yaml.safe_load(raw) if raw != "" else None)
    for name in value_keys:
        if name in MOVED:
            raise ValueError(f"config key {name} was removed with the old sandbox modes: {MOVED[name]}")
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
        layered(data)               # per-layer settings -> the flat form
        normalize_config(data)      # model references -> the endpoints they name
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
        from hermit.isolate import validate as validate_sandboxes
        validate_sandboxes(self)
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
        if as_path is not None and (as_path / MANIFEST).is_file():
            return as_path
        ident, _, version = value.partition("@")
        for candidate in sorted(TEMPLATES.iterdir()) if TEMPLATES.is_dir() else []:
            manifest = candidate / MANIFEST
            if manifest.is_file():
                tmpl = (yaml.safe_load(manifest.read_text()) or {}).get("template", {})
                if tmpl.get("id") == ident and (not version or tmpl.get("version") == version):
                    return candidate
        raise ValueError(f"No template {value!r} (neither a directory with {MANIFEST} nor a built-in id@version)")

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
