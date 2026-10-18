"""Model backends: one place binds a model to the endpoint that serves it.

A model is named by a reference, `<backend>/<model id>`: the first path segment names a backend, the rest is the
model's id on it. A Fireworks id keeps its own slashes (`fireworks/accounts/fireworks/models/glm-5p3-flash`); a
local server is named like any other (`vllm/Qwen/Qwen3-30B-A3B-Instruct-2507-FP8`). A backend is one endpoint: its
base URL, the environment variable holding its key, the API dialects it serves and, optionally, prices per model.
Three layers declare backends, each overriding the one below by name:

    the run config's `backends:` section   (run.yaml, or `backends.<name>.<field>=...` on the command line)
    the user's file                        ~/.config/harnessadapter/backends.yaml, or the file HA_BACKENDS names
    the framework's built-ins              backends.yaml beside this module

and `<NAME>_BASE_URL` in the environment moves a backend (VLLM_BASE_URL=http://gpu-box:8000/v1). A backend declared
with `from: <other>` starts as a copy of that backend: a metering relay in front of it is declared this way.

Backend fields:
    base_url        the OpenAI-compatible root, normally ending in /v1: chat completions and the Responses API
                    are under it
    api_key_env     the environment variable holding the key (default <NAME>_API_KEY)
    key_required    false for a local server that needs no key (default true)
    apis            the dialects served: chat (chat completions: the framework's own client and the react agent),
                    responses (the OpenAI Responses API: what Codex speaks), messages (the Anthropic Messages API:
                    what Claude Code speaks)
    messages_url    the root the Messages API is under, when it is not base_url without its /v1
    litellm         the litellm route prefix, for task agents that call through litellm (fireworks_ai, hosted_vllm)
    prices          per model id: {input, cached_input, output}, USD per million tokens
    extra_body      fields added to every chat request (for example reasoning_effort)
    context_window  tokens; Codex is told it (model_context_window)
    developer_role  what a relay in front of the backend does with the `developer` role Codex uses: keep (default),
                    or system for an endpoint that refuses the role (the examples' wire meter renames it)
    models          aliases: short names for model ids (`vllm/qwen` for a long id)

A reference resolves to a Binding; `Binding.spec()` is its plain-dictionary form, which the run config carries after
loading (`llm`, `agent.params.model`) and the run's config.yaml records. The Codex and Claude Code agents and
Evolvers get the binding as provider settings: `codex_args()` (the `-c model_providers.*` arguments) and
`claude_env()` (ANTHROPIC_BASE_URL and the token). The examples' shell launchers read the `shell` form of
`python -m harnessadapter.backends`.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import shlex
import sys
from urllib.parse import urlsplit

import yaml

BUILTINS = Path(__file__).with_name("backends.yaml")
USER_FILE = object()        # the user's file, found when a registry is built: HA_BACKENDS, else the XDG path


def user_file() -> Path:
    return Path(os.environ.get("HA_BACKENDS") or Path.home() / ".config" / "harnessadapter" / "backends.yaml")
APIS = ("chat", "responses", "messages")
FIELDS = {"from", "base_url", "api_key_env", "key_required", "apis", "messages_url", "litellm", "prices", "extra_body",
          "context_window", "developer_role", "models", "description"}
DEVELOPER_ROLES = ("keep", "system")
# What each consumer speaks: the dialect a backend must serve for it.
NEEDS = {"chat": "chat", "react": "chat", "llm": "chat", "codex": "responses", "claude": "messages"}
# The variable litellm reads the key from, per route prefix (the examples' CL-Bench host calls through litellm).
LITELLM_KEY_ENV = {"fireworks_ai": "FIREWORKS_AI_API_KEY", "openai": "OPENAI_API_KEY", "hosted_vllm": "HOSTED_VLLM_API_KEY",
                   "deepseek": "DEEPSEEK_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "ollama": "OLLAMA_API_KEY"}
PLACEHOLDER_KEY = "local"      # sent to a server that needs no key, for clients that refuse to send none


@dataclass
class Backend:
    name: str
    base_url: str
    api_key_env: str | None = None
    key_required: bool = True
    apis: tuple[str, ...] = ("chat",)
    messages_url: str | None = None
    litellm: str | None = None
    prices: dict = field(default_factory=dict)
    extra_body: dict = field(default_factory=dict)
    context_window: int | None = None
    developer_role: str = "keep"
    models: dict = field(default_factory=dict)
    description: str = ""

    @classmethod
    def from_dict(cls, name: str, data: dict) -> "Backend":
        unknown = set(data) - FIELDS
        if unknown:
            raise ValueError(f"backend {name!r}: unknown fields {sorted(unknown)}; known: {sorted(FIELDS)}")
        if not data.get("base_url"):
            raise ValueError(f"backend {name!r} has no base_url")
        apis = data.get("apis")
        if apis is None:
            apis = ["chat"]
        if isinstance(apis, str):
            apis = apis.split()
        bad = [a for a in apis if a not in APIS]
        if bad:
            raise ValueError(f"backend {name!r}: unknown apis {bad}; known: {list(APIS)}")
        role = str(data.get("developer_role") or "keep")
        if role not in DEVELOPER_ROLES:
            raise ValueError(f"backend {name!r}: developer_role must be one of {list(DEVELOPER_ROLES)}, not {role!r}")
        key_env = data.get("api_key_env", f"{_env_name(name)}_API_KEY")
        return cls(name=name, base_url=str(data["base_url"]).rstrip("/"), api_key_env=key_env or None,
                   key_required=bool(data.get("key_required", True)), apis=tuple(apis),
                   messages_url=(str(data["messages_url"]).rstrip("/") if data.get("messages_url") else None),
                   litellm=data.get("litellm"), prices=dict(data.get("prices") or {}),
                   extra_body=dict(data.get("extra_body") or {}), context_window=data.get("context_window"),
                   developer_role=role, models=dict(data.get("models") or {}),
                   description=str(data.get("description") or ""))

    def to_dict(self) -> dict:
        return {"base_url": self.base_url, "api_key_env": self.api_key_env, "key_required": self.key_required,
                "apis": list(self.apis), "messages_url": self.messages_url, "litellm": self.litellm,
                "prices": self.prices, "extra_body": self.extra_body, "context_window": self.context_window,
                "developer_role": self.developer_role, "models": self.models, "description": self.description}

    @property
    def messages_root(self) -> str:
        """Where the Anthropic Messages API is: Claude Code appends /v1/messages to it."""
        if self.messages_url:
            return self.messages_url
        return re.sub(r"/v1$", "", self.base_url)

    @property
    def origin(self) -> str:
        """scheme://host[:port] of the base URL: the wire meter's UPSTREAM."""
        u = urlsplit(self.base_url)
        return f"{u.scheme}://{u.netloc}"

    @property
    def path(self) -> str:
        """The base URL's path (/v1, /inference/v1): what follows the bucket on a metered URL."""
        return urlsplit(self.base_url).path.rstrip("/")

    def key(self) -> str | None:
        return os.environ.get(self.api_key_env) if self.api_key_env else None


def _env_name(name: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")


def _read(path: Path) -> dict:
    data = yaml.safe_load(path.read_text()) if path.is_file() else None
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: a backends file is a mapping of name -> fields")
    return data


def registry(declared: dict | None = None, *, user_file=USER_FILE, builtins: Path = BUILTINS,
             environ: dict | None = None) -> dict[str, Backend]:
    """Every backend by name: the built-ins, under the user's file, under the run config's `backends:` section.
    `from: <name>` copies another backend's fields first; <NAME>_BASE_URL in the environment moves a backend."""
    environ = os.environ if environ is None else environ
    if user_file is USER_FILE:
        user_file = globals()["user_file"]()
    raw: dict[str, dict] = {}
    for layer in (_read(builtins), _read(user_file) if user_file else {}, declared or {}):
        for name, fields in layer.items():
            if not isinstance(fields, dict):
                raise ValueError(f"backend {name!r}: fields must be a mapping, got {type(fields).__name__}")
        # A backend's own fields first, then the copies: `from` sees the layer's other backends whatever the order.
        for name, fields in sorted(layer.items(), key=lambda item: "from" in item[1]):
            fields = dict(fields)
            source = fields.pop("from", None)
            base = dict(raw.get(name) or {})
            if source is not None:
                if source not in raw:
                    raise ValueError(f"backend {name!r}: from {source!r} names no backend; known: {sorted(raw)}")
                base = dict(raw[source])
            raw[name] = {**base, **fields}
    out = {}
    for name, fields in raw.items():
        if not re.fullmatch(r"[A-Za-z][\w.-]*", name):
            raise ValueError(f"backend name {name!r}: letters, digits, '.', '_' and '-' only")
        moved = environ.get(f"{_env_name(name)}_BASE_URL")
        if moved:
            fields = {**fields, "base_url": moved}
        out[name] = Backend.from_dict(name, fields)
    return out


@dataclass
class Binding:
    """A model on a backend: what a consumer needs to call it."""
    backend: Backend
    model: str
    price: dict | None = None
    extra_body: dict = field(default_factory=dict)

    @property
    def ref(self) -> str:
        return f"{self.backend.name}/{self.model}"

    @property
    def base_url(self) -> str:
        return self.backend.base_url

    @property
    def api_key_env(self) -> str | None:
        return self.backend.api_key_env

    def key(self) -> str | None:
        return self.backend.key()

    def key_env_if_set(self) -> str | None:
        """The key variable the client should read: the backend's, unless the key is optional and absent."""
        if self.backend.api_key_env and (self.backend.key_required or self.key()):
            return self.backend.api_key_env
        return None

    def serves(self, api: str) -> bool:
        return api in self.backend.apis

    @property
    def litellm_model(self) -> str:
        """The id litellm routes by: its prefix for an OpenAI-compatible server is `openai`."""
        return f"{self.backend.litellm or 'openai'}/{self.model}"

    @property
    def litellm_key_env(self) -> str:
        prefix = self.backend.litellm or "openai"
        return LITELLM_KEY_ENV.get(prefix, f"{_env_name(prefix)}_API_KEY")

    def spec(self) -> dict:
        """The plain form: what `llm` and `agent.params.model` hold once the config is loaded."""
        return {"ref": self.ref, "backend": self.backend.name, "model": self.model, "base_url": self.base_url,
                "api_key_env": self.api_key_env, "key_required": self.backend.key_required,
                "apis": list(self.backend.apis), "messages_url": self.backend.messages_root,
                "litellm": self.litellm_model, "price": self.price, "extra_body": dict(self.extra_body),
                "context_window": self.backend.context_window}

    @classmethod
    def from_spec(cls, spec: dict) -> "Binding":
        """A binding back from its plain form, or from the older {base_url, model, api_key_env, price} form."""
        if not isinstance(spec, dict) or not spec.get("base_url") or not spec.get("model"):
            raise ValueError("a model spec needs base_url and model")
        name = spec.get("backend") or (urlsplit(spec["base_url"]).hostname or "custom")
        backend = Backend(name=name, base_url=str(spec["base_url"]).rstrip("/"), api_key_env=spec.get("api_key_env"),
                          key_required=bool(spec.get("key_required", True)),
                          apis=tuple(spec.get("apis") or ["chat"]), messages_url=spec.get("messages_url"),
                          litellm=(spec["litellm"].split("/", 1)[0] if spec.get("litellm") else None),
                          context_window=spec.get("context_window"))
        return cls(backend, str(spec["model"]), price=spec.get("price"), extra_body=dict(spec.get("extra_body") or {}))

    # ---- provider settings for the CLI agents ---------------------------------------------------------------
    def codex_args(self, *, base_url: str | None = None, provider: str | None = None) -> list[str]:
        """Codex's provider settings as `-c` arguments: the Responses API at the backend (or at `base_url`, a relay
        in front of it), the key from the backend's variable. Codex speaks the Responses API only."""
        name = re.sub(r"[^a-z0-9_]", "_", (provider or self.backend.name).lower())
        url = (base_url or self.base_url).rstrip("/")
        args = ["-c", f'model_provider="{name}"', "-c", f'model_providers.{name}.name="{name}"',
                "-c", f'model_providers.{name}.base_url="{url}"', "-c", f'model_providers.{name}.wire_api="responses"']
        key_env = self.key_env_if_set()
        if key_env:        # Codex sends no Authorization without env_key; with it, an unset variable is a hard error
            args += ["-c", f'model_providers.{name}.env_key="{key_env}"']
        if self.backend.context_window:
            args += ["-c", f"model_context_window={int(self.backend.context_window)}"]
        if self.backend.name != "openai":      # the hosted web_search tool type exists at OpenAI only
            args += ["-c", 'web_search="disabled"']
        return args

    def claude_env(self, *, base_url: str | None = None, expand: bool = False) -> dict[str, str]:
        """Claude Code's environment: the Anthropic Messages API at the backend (or at `base_url`, a relay in front
        of it), the token from the backend's variable as a bearer token (vLLM and SGLang check nothing else; a
        server that needs no key still gets a placeholder, since without a token Claude Code asks for a login),
        and every model alias mapped to this model (background calls use the Haiku slot). The token is written as
        `$VARIABLE`, which the agent's launcher expands, so a run's records carry the variable's name, never the
        key; `expand` gives the value itself."""
        root = (base_url or self.backend.messages_root).rstrip("/")
        key_env = self.key_env_if_set()
        token = (self.key() or PLACEHOLDER_KEY) if expand else (f"${key_env}" if key_env else PLACEHOLDER_KEY)
        env = {"ANTHROPIC_BASE_URL": root, "ANTHROPIC_AUTH_TOKEN": token,
               "ANTHROPIC_API_KEY": "",       # no x-api-key from an inherited Anthropic key
               "ANTHROPIC_MODEL": self.model}
        for alias in ("ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL",
                      "ANTHROPIC_DEFAULT_FABLE_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL"):
            env[alias] = self.model
        if self.backend.name != "anthropic":
            env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"      # no telemetry or update calls elsewhere
        return env


CLAUDE_ENV = ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL",
              "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL",
              "ANTHROPIC_DEFAULT_FABLE_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC")


def parse_ref(ref: str, backends: dict[str, Backend]) -> tuple[Backend, str] | None:
    """`<backend>/<model>` -> (backend, model id), or None when the first segment names no backend."""
    name, sep, model = str(ref).partition("/")
    if not sep or name not in backends:
        return None
    backend = backends[name]
    return backend, backend.models.get(model, model)


def is_ref(value, backends: dict[str, Backend]) -> bool:
    """Whether a `model` value is a backend reference (a `<backend>/<model>` string or a {backend, model} mapping)
    rather than a plain model id for the agent's own provider or the older {base_url, model} mapping."""
    if isinstance(value, str):
        return parse_ref(value, backends) is not None
    return isinstance(value, dict) and bool(value.get("backend")) and "base_url" not in value


def resolve(value, backends: dict[str, Backend]) -> Binding:
    """A Binding from a reference string, a {backend, model, price?, extra_body?} mapping, a loaded spec, or the
    older {base_url, model, api_key_env?, price?, extra_body?} mapping (an endpoint named by its URL)."""
    if isinstance(value, str):
        parsed = parse_ref(value, backends)
        if parsed is None:
            raise ValueError(f"model {value!r}: the first segment names no backend; known backends: {sorted(backends)}")
        backend, model = parsed
        return Binding(backend, model, price=backend.prices.get(model), extra_body=dict(backend.extra_body))
    if not isinstance(value, dict):
        raise ValueError(f"a model is a '<backend>/<model>' reference or a mapping, got {type(value).__name__}")
    if value.get("base_url"):
        return Binding.from_spec(value)
    if not value.get("backend") or not value.get("model"):
        raise ValueError("a model mapping needs backend and model (or base_url and model)")
    binding = resolve(f"{value['backend']}/{value['model']}", backends)
    if value.get("price") is not None:
        binding.price = dict(value["price"])
    if value.get("extra_body"):
        binding.extra_body = {**binding.extra_body, **value["extra_body"]}
    return binding


def as_binding(value, backends: dict[str, Backend]) -> Binding | None:
    """The Binding a `model` value names: a reference string, a {backend, model, ...} mapping, or a {model: <ref>,
    price?, extra_body?, ...} mapping; None for anything else (a plain id for the CLI's own provider, the older
    {base_url, model} endpoint mapping)."""
    if isinstance(value, str):
        return resolve(value, backends) if is_ref(value, backends) else None
    if not isinstance(value, dict) or value.get("base_url"):
        return None
    if value.get("backend") and value.get("model"):
        return resolve(value, backends)
    if isinstance(value.get("model"), str) and is_ref(value["model"], backends):
        return resolve({**value, **_split(value["model"])}, backends)
    return None


def _spec_of(value, binding: Binding) -> dict:
    """The resolved spec, keeping what a mapping said beside the model (a client `timeout`, for one)."""
    spec = binding.spec()
    if isinstance(value, dict):
        spec.update({k: v for k, v in value.items() if k not in ("model", "backend", "price", "extra_body", "from")})
    return spec


def bind_agent_params(params: dict, kind: str, backends: dict[str, Backend], default=None) -> dict:
    """The params of a Codex (`kind` codex) or Claude Code (`kind` claude) agent or Evolver with its model bound:
    `model` becomes the id the endpoint expects, the provider settings go into `args` (Codex) or `env` (Claude
    Code) ahead of the user's own, `price` is filled from the backend, and `binding` records the whole. A `model`
    that is no reference (the CLI's own provider) is left alone."""
    binding = as_binding(params.get("model") or default, backends)
    if binding is None:
        return params
    out = dict(params)
    out["model"] = binding.model
    if kind == "codex":
        out["args"] = [*binding.codex_args(), *(params.get("args") or [])]
    elif kind == "claude":
        out["env"] = {**binding.claude_env(), **(params.get("env") or {})}
    else:
        raise ValueError(f"kind must be codex or claude, not {kind!r}")
    if not out.get("price"):
        out["price"] = binding.price
    out["binding"] = binding.spec()
    return out


# ---- the run config -------------------------------------------------------------------------------------------

CLI_AGENTS = {"codex": "codex", "claude-code": "claude", "claude": "claude", "cli": None}


def normalize_config(data: dict) -> None:
    """Resolve the model references of a loaded run config in place: `llm` and `agent.params.model` become specs
    (the top-level `model` is the default for both), a Codex or Claude Code acting agent gets its provider
    settings. The Evolver's model is bound when the Evolver is instantiated (its kind is in its manifest)."""
    backends = registry(data.get("backends"))
    default = data.get("model")
    if default is not None and not is_ref(default, backends):
        raise ValueError(f"model {default!r}: the first segment names no backend; known backends: {sorted(backends)}")
    llm = data.get("llm")
    if llm is None and default is not None:
        llm = default
    binding = as_binding(llm, backends) if llm is not None else None
    if binding is not None:
        data["llm"] = _spec_of(llm, binding)
    elif llm is not None:
        data["llm"] = llm
    agent = data.get("agent") or {}
    params = agent.get("params")
    if not isinstance(params, dict):
        params = {}
    kind = CLI_AGENTS.get(str(agent.get("name") or ""), "")
    if kind is None:                                        # `cli`: the backend is a param
        kind = {"codex": "codex", "claude": "claude", "claude-code": "claude"}.get(params.get("backend", "codex"), "")
    model = params.get("model")
    if model is None and default is not None:
        model = default
    if model is not None and kind in ("codex", "claude"):
        params = bind_agent_params({**params, "model": model}, kind, backends)
    elif model is not None:
        binding = as_binding(model, backends)
        params = {**params, "model": _spec_of(model, binding) if binding is not None else model}
    if params:
        agent["params"] = params


def _split(ref: str) -> dict:
    """`<backend>/<model>` as the mapping's two fields; the alias, if any, resolves once, in `resolve`."""
    name, _, model = str(ref).partition("/")
    return {"backend": name, "model": model}


def bindings_of(data: dict, evolver_params: dict | None = None) -> list[tuple[str, Binding, str]]:
    """Every model a run calls, from the normalized config: (who, binding, the dialect it needs)."""
    out = []
    if isinstance(data.get("llm"), dict) and data["llm"].get("base_url") and data["llm"].get("model"):
        out.append(("llm tool", Binding.from_spec(data["llm"]), "chat"))
    agent = data.get("agent") or {}
    params = agent.get("params") or {}
    kind = CLI_AGENTS.get(str(agent.get("name") or ""), "")
    if kind is None:
        kind = {"codex": "codex", "claude": "claude", "claude-code": "claude"}.get(params.get("backend", "codex"), "")
    if kind in ("codex", "claude") and isinstance(params.get("binding"), dict):
        out.append((f"agent ({agent['name']})", Binding.from_spec(params["binding"]), NEEDS[kind]))
    elif isinstance(params.get("model"), dict) and params["model"].get("base_url") and params["model"].get("model"):
        out.append((f"agent ({agent['name']})", Binding.from_spec(params["model"]), "chat"))
    if evolver_params and isinstance(evolver_params.get("binding"), dict):
        kind = evolver_params.get("backend", "codex")
        out.append(("evolver", Binding.from_spec(evolver_params["binding"]), NEEDS.get(kind, "responses")))
    return out


# ---- checks -----------------------------------------------------------------------------------------------------

def problems_of(binding: Binding, api: str) -> list[str]:
    """What stops a consumer before any request: a dialect the backend does not serve, a key that is not set."""
    out = []
    if not binding.serves(api):
        hint = {"responses": "Codex speaks the OpenAI Responses API only (wire_api=\"chat\" was removed): serve the model "
                             "from a backend with `responses` among its apis (vLLM and SGLang serve /v1/responses) "
                             "or put a translating proxy in front of it",
                "messages": "Claude Code speaks the Anthropic Messages API only: serve the model from a backend with "
                            "`messages` among its apis (vLLM serves /v1/messages) or put a translating proxy in front "
                            "of it",
                "chat": "the framework's own client speaks chat completions"}[api]
        out.append(f"backend {binding.backend.name!r} serves {list(binding.backend.apis)}, not {api!r}: {hint}")
    if binding.backend.key_required and binding.api_key_env and not binding.key():
        out.append(f"{binding.api_key_env} is not set (the key of backend {binding.backend.name!r})")
    return out


def warnings_of(binding: Binding, api: str) -> list[str]:
    """What may go wrong but did not stop a run here: nothing blocks, the caller is told."""
    out = []
    if not binding.price and binding.backend.key_required:
        out.append(f"no price for {binding.model!r} on backend {binding.backend.name!r}: its cost is recorded as unknown "
                   "(set `prices` on the backend or `price` on the model)")
    return out


# Codex (0.162 and later) groups its sub-agent tools and every MCP server's tools as `namespace` tools in each
# Responses request; a server that rejects the type fails every Codex session (vLLM 0.31's gpt-oss path does).
CODEX_NAMESPACE_TOOL = {"type": "namespace", "name": "mcp__probe", "description": "Tools of the probe's MCP server.",
                        "tools": [{"type": "function", "name": "noop", "description": "Does nothing.", "strict": False,
                                   "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}]}


def probe(binding: Binding, api: str, *, timeout: float = 60) -> dict:
    """One request in the dialect: {ok, status, detail}. The Responses request is shaped as Codex sends one: streamed,
    unstored, with a `namespace` tool; when only the namespace tool is refused, the detail says so."""
    result = _probe(binding, api, timeout=timeout)
    if api == "responses" and not result["ok"] and 400 <= int(result["status"] or 0) < 500:
        plain = _probe(binding, api, timeout=timeout, tools=False)
        if plain["ok"]:
            result["detail"] = ("the server answers the Responses API but refuses a `namespace` tool, which Codex sends "
                                "for its sub-agent and MCP tools in every session (vLLM 0.31 does so for gpt-oss models), "
                                "so Codex cannot run on it: bind a model the server handles on its standard path (on vLLM "
                                "0.31, any but gpt-oss; see docs/guides/model-backends.md). Server said: "
                                + result["detail"][:200])
    return result


def _probe(binding: Binding, api: str, *, timeout: float = 60, tools: bool = True) -> dict:
    import urllib.error
    import urllib.request
    key = binding.key()
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if api == "messages":
        url = binding.backend.messages_root + "/v1/messages"
        body = {"model": binding.model, "max_tokens": 1, "messages": [{"role": "user", "content": "ping"}]}
        headers["anthropic-version"] = "2023-06-01"
        if key:
            headers["x-api-key"] = key
    elif api == "responses":       # as Codex calls it: streamed, unstored; the stream must end with a terminal event
        url = binding.base_url + "/responses"
        body = {"model": binding.model, "input": "ping", "max_output_tokens": 32, "store": False, "stream": True,
                **({"tools": [CODEX_NAMESPACE_TOOL], "tool_choice": "auto"} if tools else {})}
    else:
        url = binding.base_url + "/chat/completions"
        body = {"model": binding.model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}
    body = {**body, **binding.extra_body} if api == "chat" else body
    request = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read().decode(errors="replace")
            if api == "responses" and not any(f"response.{end}" in payload for end in ("completed", "incomplete")):
                return {"ok": False, "status": response.status, "url": url,
                        "detail": "the stream ended without a response.completed event (Codex retries, then fails): "
                                  + payload[-300:]}
            return {"ok": True, "status": response.status, "detail": payload[:300], "url": url}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "status": exc.code, "detail": exc.read()[:300].decode(errors="replace"), "url": url}
    except Exception as exc:        # noqa: BLE001 — a probe reports, it does not raise
        return {"ok": False, "status": 0, "detail": f"{type(exc).__name__}: {exc}", "url": url}


def list_models(binding: Binding, *, timeout: float = 20) -> list[str] | str:
    """The ids GET /models lists, or the error text."""
    import urllib.error
    import urllib.request
    headers = {}
    if binding.key():
        headers["Authorization"] = f"Bearer {binding.key()}"
    request = urllib.request.Request(binding.base_url + "/models", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return f"HTTP {exc.code}: {exc.read()[:200].decode(errors='replace')}"
    except Exception as exc:        # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"
    items = payload.get("data") if isinstance(payload, dict) else payload
    return [str(m.get("id")) for m in items or [] if isinstance(m, dict)]


# ---- the command line: what the examples' launchers read ----------------------------------------------------------

def shell_lines(binding: Binding) -> list[str]:
    """`NAME=value` lines a shell launcher evals: the endpoint split for a wire meter in front of it (HA_ORIGIN is
    the meter's UPSTREAM, HA_PATH follows the bucket, HA_REWRITE_DEVELOPER_ROLE its REWRITE_DEVELOPER_ROLE), the key
    variable, the litellm id and the prices."""
    b = binding.backend
    price = binding.price or {}
    values = [("HA_BACKEND", b.name), ("HA_MODEL_ID", binding.model), ("HA_REF", binding.ref),
              ("HA_BASE_URL", b.base_url), ("HA_ORIGIN", b.origin), ("HA_PATH", b.path),
              ("HA_MESSAGES_URL", b.messages_root), ("HA_API_KEY_ENV", b.api_key_env or ""),
              ("HA_KEY_REQUIRED", "1" if b.key_required else "0"), ("HA_APIS", " ".join(b.apis)),
              ("HA_LITELLM_MODEL", binding.litellm_model), ("HA_LITELLM_KEY_ENV", binding.litellm_key_env),
              ("HA_PRICE_INPUT", str(price.get("input", ""))), ("HA_PRICE_CACHED_INPUT", str(price.get("cached_input", ""))),
              ("HA_PRICE_OUTPUT", str(price.get("output", ""))),
              ("HA_CONTEXT_WINDOW", str(b.context_window or "")),
              ("HA_REWRITE_DEVELOPER_ROLE", "1" if b.developer_role == "system" else "0")]
    return [f"{k}={shlex.quote(v)}" for k, v in values]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="harnessadapter backends", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="every backend: name, base URL, dialects, key variable")
    for name, text in (("show", "a model reference resolved, as JSON"), ("shell", "the same as NAME=value lines"),
                       ("check", "whether the backend serves the model: GET /models, and one request per dialect")):
        p = sub.add_parser(name, help=text)
        p.add_argument("ref", help="<backend>/<model id>")
        p.add_argument("--backends", help="a YAML mapping of backends declared for this call (as run.yaml's section)")
        if name == "check":
            p.add_argument("--api", action="append", choices=APIS, help="dialect(s) to call; default: all it declares")
            p.add_argument("--no-probe", action="store_true", help="only GET /models, no model call")
    args = parser.parse_args(argv)
    declared = yaml.safe_load(args.backends) if getattr(args, "backends", None) else None
    backends = registry(declared)
    if args.command == "list":
        width = max(len(n) for n in backends) if backends else 8
        for name, b in backends.items():
            key = f"{b.api_key_env}{'' if b.key_required else ' (optional)'}" if b.api_key_env else "no key"
            print(f"{name:<{width}}  {b.base_url}  [{' '.join(b.apis)}]  key: {key}")
        return 0
    try:
        binding = resolve(args.ref, backends)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.command == "show":
        print(json.dumps(binding.spec(), indent=1))
        return 0
    if args.command == "shell":
        print("\n".join(shell_lines(binding)))
        return 0
    ok = True
    print(f"{binding.ref}: {binding.base_url} [{' '.join(binding.backend.apis)}]")
    for api in args.api or binding.backend.apis:
        for p in problems_of(binding, api):
            print(f"  [PROBLEM] {p}")
            ok = False
        for w in warnings_of(binding, api):
            print(f"  [warning] {w}")
    models = list_models(binding)
    if isinstance(models, str):
        print(f"  [warning] GET /models: {models}")
    elif binding.model not in models:
        print(f"  [warning] GET /models does not list {binding.model!r}: {models[:12]}{' ...' if len(models) > 12 else ''}")
    else:
        print(f"  [ok] GET /models lists {binding.model!r}")
    if not args.no_probe:
        for api in args.api or binding.backend.apis:
            if not binding.serves(api):
                continue
            result = probe(binding, api)
            ok = ok and result["ok"]
            print(f"  [{'ok' if result['ok'] else 'PROBLEM'}] {api}: HTTP {result['status']} {result['url']}"
                  + ("" if result["ok"] else f"\n      {result['detail']}"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
