"""`mini-swe-agent`: mini-swe-agent (https://mini-swe-agent.com) as a Base Agent (docs/framework/agent-layers.md).

mini-swe-agent's model runs bash commands and nothing else, in a loop its one YAML config describes. One `run` of
the Base Agent (hermit.agent.base.cli.MiniSweAgent) is one task of it in its own process
(hermit.agent.base.mini_swe_agent_run), run as mini-swe-agent's batch runner runs a task: through its Python API,
with its LocalEnvironment in `workspace`. That process writes the session as JSON events on its stdout, which
CliAgent keeps as the stream (`agent-mini-swe-agent.jsonl`) and reads into the reply, the trajectory
(hermit.agent.base.trajectory.mini_swe_agent_steps) and the usage; isolation, the home, the tool server and the clock
are CliAgent's, as for Codex and Claude Code. This module holds what the process and the deploy host share: the
quiet import of mini-swe-agent and the messages.

The messages are Hermit's prompts (mini_swe_agent.yaml beside this module) over mini-swe-agent's own `mini` config
(`mini_textbased` for a text-based model class), then the `config` file's settings. The system message ends with
what the workspace's deployment gives (`hermit deploy --host mini-swe-agent` writes `mini-swe-agent.yaml`, whose
`hermit.context` is the adapter's context), the call's tools and the call's system instructions, under one heading
(`adapter_block`). A deployed workspace run with `mini -c mini-swe-agent.yaml` gets the same system message: the
deployment writes it into its system template (`baked`).

The call's tools are one bash command, `hermit-tool <name> '<JSON arguments>'`, which the process's environment
(hermit.agent.base.mini_swe_agent_env) answers through the tool server's JSON entrance. A command sees `env` and a
few basic variables (PATH, HOME, LANG, ...) only, not the process's environment, where the model's key is (on Linux
the process is non-dumpable, so a command cannot read it from /proc either); the key goes with each model request and
is never written to the stream.
"""
from __future__ import annotations

import atexit
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import threading
from types import ModuleType
from typing import Sequence

import yaml

CONFIG_FILE = "mini-swe-agent.yaml"         # a deployed workspace's config, as `mini -c` reads it
PROMPTS = Path(__file__).with_name("mini_swe_agent.yaml")
CONTEXT_HEADING = "# Instructions from the harness adapter"
PLACEHOLDER = "{{ adapter_context }}"
_PLACEHOLDER = re.compile(r"\{\{-?\s*adapter_context\s*-?\}\}")
_ENDRAW = re.compile(r"\{%[-+]?\s*endraw\s*[-+]?%\}")      # every spelling Jinja ends a raw block with
STEP_LIMIT = 50
INSTALL = ("mini-swe-agent is not installed: python -m pip install 'hermit-harness[mini-swe-agent]' "
           "(from a checkout: python -m pip install -e '.[mini-swe-agent]')")
_LOADING = threading.Lock()


def load_minisweagent() -> ModuleType:
    """Import mini-swe-agent, once per process, without the side effects it would have here: no banner, no
    first-run wizard, and, unless MSWEA_GLOBAL_CONFIG_DIR is set, an empty config directory of its own (removed at
    exit) rather than the user's, whose .env it would load into os.environ. litellm keeps its bundled price map
    instead of fetching one: Hermit prices the tokens itself. Variables already set are kept."""
    with _LOADING:
        if "minisweagent" not in sys.modules:
            os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")
            os.environ.setdefault("MSWEA_CONFIGURED", "true")
            os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
            if not os.environ.get("MSWEA_GLOBAL_CONFIG_DIR"):
                own = tempfile.mkdtemp(prefix="hermit-mini-swe-agent-")
                atexit.register(shutil.rmtree, own, ignore_errors=True)
                os.environ["MSWEA_GLOBAL_CONFIG_DIR"] = own
        try:
            import minisweagent
        except ImportError:
            raise RuntimeError(INSTALL) from None
    return minisweagent


# ---------- the messages: Hermit's prompts over mini-swe-agent's config, the adapter's block ----------
def base_config(model_class: str) -> str:
    """The mini-swe-agent config a model class goes with: `mini_textbased` for the text-based classes, else `mini`."""
    return "mini_textbased" if "textbased" in str(model_class) else "mini"


def render_config(base: str = "mini", *overrides: dict) -> dict:
    """mini-swe-agent's own `base` config as the package ships it (its model and environment sections; never a
    `mini.yaml` in the working directory or MSWEA_CONFIG_DIR), Hermit's prompts for it, then `overrides` (a user's
    config), merged the way mini-swe-agent merges its -c files. The package is found, not imported: importing it
    would change this process's environment (load_minisweagent), and a deployment only reads its files."""
    spec = importlib.util.find_spec("minisweagent")
    if spec is None or not spec.submodule_search_locations:
        raise RuntimeError(INSTALL)
    prompts = yaml.safe_load(PROMPTS.read_text())
    if base not in prompts:
        raise ValueError(f"no Hermit prompts for mini-swe-agent's {base!r} config; known: {sorted(prompts)}")
    shipped = Path(list(spec.submodule_search_locations)[0]) / "config" / f"{base}.yaml"
    return _merge(yaml.safe_load(shipped.read_text()), {"agent": prompts[base]}, *(o for o in overrides if o))


def _merge(*configs: dict) -> dict:
    """mini-swe-agent's recursive_merge: later configs win, mappings are merged key by key."""
    out: dict = {}
    for config in configs:
        for key, value in config.items():
            if isinstance(value, dict) and isinstance(out.get(key), dict):
                out[key] = _merge(out[key], value)
            else:
                out[key] = _merge(value) if isinstance(value, dict) else value
    return out


def tools_section(tools: Sequence[dict]) -> str:
    """How to run the tools, and each one's name, description and argument schema; empty without tools."""
    if not tools:
        return ""
    lines = ["## Tools", "",
             "Run a tool as a command of its own: `hermit-tool <name> '<JSON arguments>'`, the arguments one JSON "
             "object in single quotes. Its result is printed; `hermit-tool` alone lists the tools.", ""]
    lines += [f"- `{t['name']}`: {t.get('description', '')} Arguments: "
              f"`{json.dumps(t.get('input_schema') or {'type': 'object'}, ensure_ascii=False)}`" for t in tools]
    return "\n".join(lines)


def adapter_block(*parts: str) -> str:
    """What takes `{{ adapter_context }}`'s place: the non-empty parts (the adapter's context, the tools, a call's
    system instructions) under one heading; empty when there are none."""
    parts = tuple(p.strip() for p in parts if p and p.strip())
    return f"\n{CONTEXT_HEADING}\n\n" + "\n\n".join(parts) + "\n" if parts else ""


def with_placeholder(template: str) -> str:
    """A system template with `{{ adapter_context }}` in it: a template without one (a user's own) gets it at its
    end."""
    return template if _PLACEHOLDER.search(template) else template.rstrip("\n") + "\n" + PLACEHOLDER + "\n"


def baked(template: str, block: str) -> str:
    """The system template with the block in place of the placeholder as literal text (a Jinja raw block), for a
    deployed config: mini-swe-agent renders it to what the Base Agent gets with `adapter_context=block`."""
    text = _ENDRAW.sub(lambda m: "{% endraw %}{{ " + json.dumps(m[0]) + " }}{% raw %}", block)
    raw = "{% raw %}" + text + "{% endraw %}"
    return _PLACEHOLDER.sub(lambda _: raw, with_placeholder(template))


__all__ = ["CONFIG_FILE", "STEP_LIMIT", "adapter_block", "base_config", "baked", "load_minisweagent",
           "render_config", "tools_section", "with_placeholder"]
