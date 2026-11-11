"""The run config: framework defaults, then run.yaml, then command-line overrides (key.sub=value).

The `run:` block says how the run is done (decided with the Harbor foundation: Hermit owns the run modes, a task set
only supplies tasks, an index and split files):

    eval     `hermit eval` only: one adapter on a split, N repeats; `hermit train` refuses the config
    split    train on data.train (epochs, shuffle, val as the method uses it), then the final adapter on data.test
    stream   one ordered pass: each row is scored with the adapter learned from the rows before it, then learned
             from; one epoch, no shuffle, no final selection; `orders` runs several orders, `checkpoints` tests the
             commit a step left on a split of their own
    null     as before this block existed: train only

`feedback` (every mode) is what the learner is shown of each verdict: `full` (as before), `score` (the score alone)
or `none` (no score, no feedback); a stream must name it, and hermit train refuses one below the least the Evolver
works with (its class's `min_feedback`, or `min_feedback_for` its params). An Evolver class with `hides_labels =
True` hides the labels from its sessions itself and is given the full records in its own process.
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
import re

import yaml

from hermit.adapter.manifest import MANIFEST
from hermit.agent.model.backends import harbor_cli, normalize_config

DEFAULTS = Path(__file__).with_name("defaults.yaml")
TEMPLATES = Path(__file__).resolve().parent / "adapter" / "templates"
RUN_MODES = ("eval", "split", "stream")
FEEDBACK = ("full", "score", "none")
CHECKPOINT_KEYS = ("name", "after", "split", "adapters")
CHECKPOINT_ADAPTERS = ("checkpoint", "final", "root")
NAME = r"[A-Za-z0-9][\w.-]{0,80}"            # a run's or a checkpoint's name: it names a directory and a git ref


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


def _calls_the_runs_model(name, base: Path | None) -> bool:
    """Whether the Evolver `name` decides each step in its own code and calls the run's model (`ctx.agent`, the `llm`
    tool): its manifest says so (`needs: [llm]`). Any other Evolver takes `evolver.base` as its own CLI."""
    from hermit.agent.evolver.loader import find
    try:
        return "llm" in find(str(name), base).needs
    except ValueError:                                     # no such Evolver: said where the Evolver is loaded
        return False


def layered(data: dict, base_dir: Path | None = None) -> None:
    """The per-layer form of `agent` and `evolver` (docs/framework/agent-layers.md#settings-by-layer) as the flat
    form the framework reads: `params` (Layer 3), `base` and its section (Layer 2), `model` (Layer 1). A key given in
    two layers is refused. `evolver.base: pi` on a method that calls the run's model makes that model Pi: `llm: {cli:
    pi}`, one session per call."""
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
    if base == "pi" and _calls_the_runs_model(evolver.get("name"), base_dir):
        if data.get("llm") is not None:
            raise ValueError("evolver.base: pi makes the run's model Pi, and llm names it too: give one")
        llm: dict = {"cli": "pi"}
        _model(llm, model if model is not None else data.get("model"), "evolver")
        for key, value in (sections.get("pi") or {}).items():
            _put(llm, key, value, "evolver.pi")
        data["llm"] = llm
        evolver["params"] = params
        return
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


def _empty_as_defaults(data: dict, defaults: dict) -> None:
    """`run:` or `eval:` left empty (null), in the file or by an override: that section's defaults; a key of either
    left empty or left out (a whole section given at once, `eval={repeats: 3}`): its default; a key the section does
    not have is refused, left empty or not, as in the file."""
    for key in ("run", "eval"):
        if data.get(key) is None:
            data[key] = copy.deepcopy(defaults[key])
        elif isinstance(data[key], dict):            # anything else is refused by name (validate)
            given = _merge(defaults[key], data[key], f"{key}.")        # a key the section does not have: refused
            data[key] = {k: copy.deepcopy(defaults[key][k]) if v is None else v for k, v in given.items()}


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
    def __init__(self, data: dict, base: Path, source: Path | None = None):
        self.data = data
        self.base = Path(base).resolve()     # relative paths in the config are relative to its directory
        self.source = Path(source).resolve() if source is not None else None    # the run.yaml it was read from
        self.notes: list[str] = []           # what validation set itself, and says so (a stream's data.shuffle)
        self.order: dict | None = None       # one order's run of a config with run.orders (for_order)

    @classmethod
    def load(cls, path: Path, overrides: list[str] = (), command: str | None = None) -> "Config":
        """The config at `path` with `overrides`, validated for `command` (train | eval | check; None, the Python
        API's: by run.mode, as `hermit check` does: an eval config as hermit eval needs it, any other as hermit train
        does, its eval section only where its run tests an adapter)."""
        defaults = yaml.safe_load(DEFAULTS.read_text())
        user = yaml.safe_load(Path(path).read_text()) or {}
        data = _merge(defaults, user)
        _empty_as_defaults(data, defaults)
        for item in overrides:
            _override(data, item)
            _empty_as_defaults(data, defaults)          # for the next override too, which may name a key it left out
        if data["pipeline"].get("run") and not data["agent"].get("name"):     # the older spelling
            data["agent"]["name"] = data["pipeline"]["run"]
            if data["pipeline"].get("params"):
                data["agent"]["params"] = {**data["agent"].get("params", {}), **data["pipeline"]["params"]}
            if data["pipeline"].get("concurrency") is not None:
                data["agent"]["concurrency"] = data["pipeline"]["concurrency"]
        layered(data, Path(path).parent)               # per-layer settings -> the flat form
        normalize_config(data, base=Path(path).parent)      # model references -> the endpoints they name
        config = cls(data, Path(path).parent, source=Path(path))
        config.validate(command)
        return config

    def __getitem__(self, key):
        return self.data[key]

    def path(self, value) -> Path | None:
        if value in (None, ""):
            return None
        p = Path(os.path.expandvars(str(value))).expanduser()
        return p if p.is_absolute() else (self.base / p).resolve()

    # ---------- the run block ----------
    @property
    def mode(self) -> str | None:
        return (self.data.get("run") or {}).get("mode")

    @property
    def feedback(self) -> str:
        """What the learner is shown of each verdict: full (the default), score or none."""
        return (self.data.get("run") or {}).get("feedback") or "full"

    def orders(self) -> list:
        return list((self.data.get("run") or {}).get("orders") or [])

    def harbor_builtin(self) -> str | None:
        """The acting agent of a Harbor task set when it is one of Harbor's own (agent.params.acting_agent by `name`,
        or a class that is no HermitXXX, the benchmark file's read in: harbor_cli is None), which takes no adapter:
        its name or class. None for any other agent."""
        agent = self.data["agent"]
        params = agent.get("params") or {}
        if str(agent.get("name") or "") != "harbor" or harbor_cli(params) is not None:
            return None
        spec = params.get("acting_agent") or {}
        return str(spec.get("import_path") or spec.get("name"))

    def checkpoints(self) -> list[dict]:
        """run.checkpoints with their defaults: `split` an absolute path (data.test when none is named), `adapters`
        [checkpoint] when none are named."""
        out = []
        for c in (self.data.get("run") or {}).get("checkpoints") or []:
            out.append({"name": str(c["name"]), "after": str(c["after"]),
                        "split": self.path(c.get("split") or self.data["data"].get("test")),
                        "adapters": list(c.get("adapters") or ["checkpoint"])})
        return out

    def for_order(self, index: int) -> "Config":
        """The config of order `index`'s own run: trainer.run_name `<run_name>-o<index>`, and an order file as its
        data.train (a seed orders data.train when the Trainer takes it: `order`)."""
        orders = self.orders()
        data = copy.deepcopy(self.data)
        data["trainer"]["run_name"] = f"{self.run_name()}-o{index}"
        order: dict = {"index": index, "of": len(orders)}
        if isinstance(orders[index], str):
            order["file"] = str(self.path(orders[index]))
            data["data"]["train"] = order["file"]
        else:
            order["seed"] = int(orders[index])
        config = Config(data, self.base, source=self.source)
        config.notes, config.order = list(self.notes), order
        return config

    def order_runs(self) -> list["Config"]:
        """The runs this config makes: one per order of run.orders, else itself."""
        if self.order is not None or not self.orders():
            return [self]
        return [self.for_order(i) for i in range(len(self.orders()))]

    def validate(self, command: str | None = None) -> None:
        """What `command` needs (train | eval | check | None, see `load`): `hermit eval` needs no training keys
        (data.train, template, evolver), `hermit train` does and refuses run.mode: eval. The eval section is checked
        where an eval runs: hermit eval, an eval config (for check and None too), and a training run that tests an
        adapter (split's test of the final adapter on data.test, a stream's checkpoint tests), for any command."""
        d = self.data
        for key in ("run", "eval"):
            if d.get(key) is not None and not isinstance(d[key], dict):
                raise ValueError(f"{key} is a section of settings ({key}.<name>: <value>, as hermit/defaults.yaml "
                                 f"lists them), not {d[key]!r}")
        mode = self.mode
        if mode not in (None, *RUN_MODES):
            raise ValueError(f"run.mode is eval, split or stream (or null: train only, as before), not {mode!r}")
        if command not in (None, "train", "eval", "check"):
            raise ValueError(f"no command {command!r} to validate the config for")
        if command == "train" and mode == "eval":
            raise ValueError("run.mode is eval: this config is for hermit eval; to train, set run.mode to split or "
                             "stream")
        need = command if command in ("train", "eval") else ("eval" if mode == "eval" else "train")
        orders = self.orders()
        order_files = bool(orders) and all(isinstance(o, str) for o in orders)   # each order its own data.train
        if not d["data"]["tasks"]:
            raise ValueError("data.tasks is required")
        if need == "train" and not d["data"]["train"] and not order_files:
            raise ValueError("data.train is required")
        if not d["agent"]["name"]:
            raise ValueError("agent.name is required")
        if not d["judge"]["path"]:
            raise ValueError("judge.path is required")
        if need == "train" and not d["template"]:
            raise ValueError("template is required")
        if need == "train" and not d["evolver"]["name"]:
            raise ValueError("evolver.name is required")
        from hermit.isolate import validate as validate_sandboxes
        validate_sandboxes(self)
        if not isinstance(d["data"]["shuffle"], bool):
            raise ValueError("data.shuffle must be true or false")
        if int(d["data"]["step_size"]) < 1 or int(d["trainer"]["epochs"]) < 1:
            raise ValueError("data.step_size and trainer.epochs must be positive")
        if d["trainer"]["run_name"] and not re.fullmatch(NAME, str(d["trainer"]["run_name"])):
            raise ValueError("trainer.run_name may use letters, digits, '.', '_' and '-'")
        self._validate_run()
        if need == "eval" or (mode == "split" and d["data"].get("test")) or self.checkpoints():
            self._validate_eval()

    def _validate_run(self) -> None:
        d, run = self.data, self.data.get("run") or {}
        mode = run.get("mode")
        if run.get("feedback") not in (None, *FEEDBACK):
            raise ValueError(f"run.feedback is full, score or none, not {run.get('feedback')!r}")
        if not isinstance(run.get("checkpoints_during", False), bool):
            raise ValueError("run.checkpoints_during must be true or false")
        if mode != "stream":
            for key in ("orders", "checkpoints"):
                if run.get(key):
                    raise ValueError(f"run.{key} is for run.mode: stream (the mode is {mode or 'null'})")
            if run.get("checkpoints_during"):
                raise ValueError("run.checkpoints_during is for run.mode: stream with run.checkpoints")
        if mode == "stream":
            if run.get("feedback") is None:
                raise ValueError("run.mode: stream needs run.feedback (full, score or none): what the learner is shown "
                                 "of each row's verdict has no default in a stream")
            if int(d["trainer"]["epochs"]) != 1:
                raise ValueError("run.mode: stream scores each row once, before it is learned from: trainer.epochs "
                                 "must be 1")
            if d["data"]["shuffle"]:
                d["data"]["shuffle"] = False
                note = ("run.mode: stream takes its rows in their order (the train split's, or each order's): "
                        "data.shuffle was set to false")
                if note not in self.notes:
                    self.notes.append(note)
        orders = run.get("orders")
        if orders is not None:
            seeds = isinstance(orders, list) and all(isinstance(o, int) and not isinstance(o, bool) for o in orders)
            files = isinstance(orders, list) and all(isinstance(o, str) and o for o in orders)
            if not orders or not (seeds or files):
                raise ValueError("run.orders is a list of seeds (whole numbers: Hermit orders the train split) or of "
                                 "order files (each an order of its own), not a mix")
            if len(set(orders)) != len(orders):
                raise ValueError("run.orders names an order twice")
        checkpoints = run.get("checkpoints")
        if checkpoints is not None:
            if not isinstance(checkpoints, list) or not checkpoints:
                raise ValueError("run.checkpoints is a list of {name, after, split, adapters}")
            names = set()
            for i, c in enumerate(checkpoints):
                where = f"run.checkpoints[{i}]"
                if not isinstance(c, dict):
                    raise ValueError(f"{where} is {{name, after, split, adapters}}, not {c!r}")
                unknown = sorted(set(c) - set(CHECKPOINT_KEYS))
                if unknown:
                    raise ValueError(f"{where}: unknown keys {unknown} (it takes {', '.join(CHECKPOINT_KEYS)})")
                name = c.get("name")
                if not isinstance(name, str) or not re.fullmatch(NAME, name):
                    raise ValueError(f"{where}.name names its results' directory: letters, digits, '.', '_' and '-', "
                                     f"not {name!r}")
                if name in names:
                    raise ValueError(f"run.checkpoints names {name!r} twice")
                names.add(name)
                after = c.get("after")
                if not isinstance(after, str) or not after:
                    number = isinstance(after, (int, float)) and not isinstance(after, bool)   # YAML's: after: 42
                    quote = (f", not the number {after!r} (YAML read the id as a number): write the task id in "
                             "quotes, as the split file spells it" if number else "")
                    raise ValueError(f"run.checkpoints {name}: after is a task id of the train split (the last task of "
                                     f"a step), or end{quote}")
                if c.get("split") is not None and not isinstance(c["split"], str):
                    raise ValueError(f"run.checkpoints {name}: split is a split file")
                if not c.get("split") and not d["data"].get("test"):
                    raise ValueError(f"run.checkpoints {name} names no split, and data.test is not set")
                adapters = c.get("adapters", ["checkpoint"])
                if not isinstance(adapters, list) or not adapters or len(set(adapters)) != len(adapters) or \
                        any(a not in CHECKPOINT_ADAPTERS for a in adapters):
                    raise ValueError(f"run.checkpoints {name}: adapters is a list of {', '.join(CHECKPOINT_ADAPTERS)} "
                                     f"(each once), not {adapters!r}")
        for key in ("state", "score"):
            value = run.get(key)
            if value is None:
                continue
            if not isinstance(value, str) or ":" not in value:
                raise ValueError(f"run.{key} is 'file.py:function' or 'module:function', not {value!r}")
            if not checkpoints:
                raise ValueError(f"run.{key} serves run.checkpoints, which are not set")

    def _validate_eval(self) -> None:
        ev = self.data["eval"]
        try:
            repeats = int(ev.get("repeats"))          # as hermit eval has always read it: "3" and 2.0 are 3 and 2
        except (TypeError, ValueError):
            repeats = 0
        if repeats < 1:
            raise ValueError(f"eval.repeats must be a whole number of at least 1, not {ev.get('repeats')!r}")
        ev["repeats"] = repeats
        value = ev.get("infra_retries")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"eval.infra_retries must be a whole number of at least 0, not {value!r}")
        wait = ev.get("infra_wait_s")
        if isinstance(wait, bool) or not isinstance(wait, (int, float)) or wait < 0:
            raise ValueError(f"eval.infra_wait_s must be a number of seconds, not {wait!r}")

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
        """trainer.run_name; else `<evolver>-<template>`; else (an eval config, which names neither) the config
        file's stem."""
        if self.data["trainer"]["run_name"]:
            return str(self.data["trainer"]["run_name"])
        if (self.data["evolver"]["name"] and self.data["template"]) or self.source is None:
            return f"{self.data['evolver']['name']}-{Path(str(self.data['template'])).name.split('@')[0]}".replace("/",
                                                                                                                   "-")
        return re.sub(r"[^\w.-]", "-", self.source.stem).lstrip(".-_") or "eval"

    def run_dir(self) -> Path:
        return self.path(self.data["trainer"]["output_dir"]) / self.run_name()

    def tree_dir(self, template_id: str) -> Path:
        if self.data["trainer"]["tree_dir"]:
            return self.path(self.data["trainer"]["tree_dir"])
        return self.path(self.data["trainer"]["output_dir"]) / "trees" / template_id
