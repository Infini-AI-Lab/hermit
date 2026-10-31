#!/usr/bin/env python3
"""Deploy an adapter's exports into a mini-swe-agent workspace: one config file, the tools as a bash command, skills.

mini-swe-agent (https://mini-swe-agent.com) reads no instructions file and has no plugins, subagents or MCP client:
one YAML config's Jinja templates make its first two messages, and its model runs bash commands only. So the
workspace gets

- `mini-swe-agent.yaml`: Hermit's prompts (mini_swe_agent.yaml beside this module) over mini-swe-agent's own `mini`
  config, the adapter's context in the system template, with an index of its tools and skills;
- the tools as one command, `hermit-tool <name> '<JSON arguments>'`, answered in mini-swe-agent's process by
  HermitEnvironment (hermit.hosts.mini_swe_agent_env), which the config names as its environment class: mini-swe-agent
  must then run in the Python environment hermit is installed in. An adapter without tools keeps the `local` one;
- the skills under `.hermit/skills/`, which the index names.

An adapter that exports roles is refused: mini-swe-agent has no subagents. Run it from the workspace with
`mini -c mini-swe-agent.yaml -m <model>`. The `mini-swe-agent` acting agent (hermit.agents.mini_swe_agent) builds its
messages from the same `render_config` and `adapter_block`, so an agent in training and a deployed one read the same
text. Global mini-swe-agent settings are never changed.
"""
from __future__ import annotations

import argparse
import atexit
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import threading

from types import ModuleType

import yaml

from hermit.adapter.loader import agent_tools, load_manifest, not_portable
from hermit.hosts.inputs import MARKER, check_workspace, copy_skills, render_exports

HOST = "mini-swe-agent"
CONFIG_FILE = "mini-swe-agent.yaml"
SKILLS_DIR = ".hermit/skills"
PROMPTS = Path(__file__).with_name("mini_swe_agent.yaml")
ENVIRONMENT_CLASS = "hermit.hosts.mini_swe_agent_env.HermitEnvironment"
CONTEXT_HEADING = "# Instructions from the harness adapter"
PLACEHOLDER = "{{ adapter_context }}"
_PLACEHOLDER = re.compile(r"\{\{-?\s*adapter_context\s*-?\}\}")
_ENDRAW = re.compile(r"\{%[-+]?\s*endraw\s*[-+]?%\}")      # every spelling Jinja ends a raw block with
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
            from hermit.evolver.dependencies import missing_dependency
            raise missing_dependency("mini-swe-agent", "mini-swe-agent", "mini-swe-agent", kind="agent") from None
    return minisweagent


def base_config(model_class: str) -> str:
    """The mini-swe-agent config a model class goes with: `mini_textbased` for the text-based classes, else `mini`."""
    return "mini_textbased" if "textbased" in str(model_class) else "mini"


def render_config(base: str = "mini", *overrides: dict) -> dict:
    """mini-swe-agent's own `base` config as the package ships it (its model and environment sections; never a
    `mini.yaml` in the working directory or MSWEA_CONFIG_DIR), Hermit's prompts for it, then `overrides` (a user's
    config), merged the way mini-swe-agent merges its -c files."""
    load_minisweagent()
    from minisweagent.config import builtin_config_dir
    from minisweagent.utils.serialize import recursive_merge
    prompts = yaml.safe_load(PROMPTS.read_text())
    if base not in prompts:
        raise ValueError(f"no Hermit prompts for mini-swe-agent's {base!r} config; known: {sorted(prompts)}")
    shipped = yaml.safe_load((builtin_config_dir / f"{base}.yaml").read_text())
    return recursive_merge(shipped, {"agent": prompts[base]}, *(o for o in overrides if o))


def skill_index(root: Path, names: list[str]) -> list[tuple[str, str]]:
    """(name, description) of each copied skill, the description from its SKILL.md frontmatter."""
    out = []
    for name in names:
        text = (root / name / "SKILL.md").read_text()
        meta: object = {}
        if text.startswith("---"):
            try:
                meta = yaml.safe_load(text.split("---", 2)[1]) or {}
            except yaml.YAMLError:
                meta = {}
        description = meta.get("description") if isinstance(meta, dict) else None
        out.append((name, str(description).strip() if description else ""))
    return out


def adapter_block(context: str, tools: list[dict], skills: list[tuple[str, str]]) -> str:
    """What takes `{{ adapter_context }}`'s place: the adapter's context, how to run its tools and where its skills
    are. Empty without an adapter, or for an adapter with none of the three."""
    parts = [context.strip()] if context.strip() else []
    if tools:
        lines = ["## Adapter tools", "",
                 "Run an adapter tool as a command of its own: `hermit-tool <name> '<JSON arguments>'`, the arguments "
                 "one JSON object in single quotes. Its result is printed; `hermit-tool` alone lists the tools.", ""]
        lines += [f"- `{t['name']}`: {t['description']} Arguments: `{json.dumps(t['input_schema'], ensure_ascii=False)}`"
                  for t in tools]
        parts.append("\n".join(lines))
    if skills:
        lines = ["## Skills", "", f"Each skill is a directory under `{SKILLS_DIR}/`; read its SKILL.md before you use it.",
                 ""]
        lines += [f"- `{name}`: {description}" if description else f"- `{name}`" for name, description in skills]
        parts.append("\n".join(lines))
    return f"\n{CONTEXT_HEADING}\n\n" + "\n\n".join(parts) + "\n" if parts else ""


def with_placeholder(template: str) -> str:
    """A system template with `{{ adapter_context }}` in it: a template without one (a user's own) gets it at the end."""
    return template if _PLACEHOLDER.search(template) else template.rstrip("\n") + "\n" + PLACEHOLDER + "\n"


def baked(template: str, block: str) -> str:
    """The system template with the block in place of the placeholder as literal text (a Jinja raw block), for a
    deployed config: mini-swe-agent renders it to what the acting agent gets with `adapter_context=block`."""
    raw = "{% raw %}" + _ENDRAW.sub(lambda m: "{% endraw %}{{ " + json.dumps(m[0]) + " }}{% raw %}", block) + "{% endraw %}"
    return _PLACEHOLDER.sub(lambda _: raw, with_placeholder(template))


class _Dumper(yaml.SafeDumper):
    pass


_Dumper.add_representer(str, lambda dumper, value: dumper.represent_scalar(
    "tag:yaml.org,2002:str", value, style="|" if "\n" in value else None))


def prepare(adapter: Path, output: Path, python: str, *, learning_material: Path | None = None) -> dict:
    adapter, output = adapter.resolve(), output.resolve()
    refused = not_portable(load_manifest(adapter), "A mini-swe-agent workspace")   # before anything is written
    if refused:
        raise ValueError(refused)
    check_workspace(adapter, output, HOST)
    manifest = load_manifest(adapter)
    exports = render_exports(adapter, python)
    if exports["agents"]:
        names = ", ".join(r["name"] for r in exports["agents"])
        raise ValueError(f"mini-swe-agent has no subagents, so this adapter's roles cannot be deployed ({names}); "
                         "deploy with --host codex or --host claude-code")
    try:
        config = render_config("mini")
    except RuntimeError as error:                 # mini-swe-agent is not installed
        raise ValueError(str(error)) from None
    tools = agent_tools(manifest)                 # the context tool is the harness's, not served
    output.mkdir(parents=True, exist_ok=True)
    skills = copy_skills(adapter, manifest, output / SKILLS_DIR)
    block = adapter_block(exports["context"], tools, skill_index(output / SKILLS_DIR, skills))
    config["agent"]["system_template"] = baked(config["agent"]["system_template"], block)
    audit = output / "tool-calls.jsonl"
    if tools:
        config["environment"] = {**config.get("environment", {}), "environment_class": ENVIRONMENT_CLASS,
                                 "adapter": str(adapter), "audit_log": str(audit)}
        if learning_material is not None:
            config["environment"]["learning_material"] = str(Path(learning_material).resolve())
    header = (f"# Generated by hermit for the adapter {manifest['id']} {manifest['version']}. No global settings are "
              "changed.\n# Run from this directory: mini -c mini-swe-agent.yaml -m <model>\n")
    (output / CONFIG_FILE).write_text(header + yaml.dump(config, Dumper=_Dumper, sort_keys=False, allow_unicode=True,
                                                         width=10_000))
    result = {"host": HOST, "adapter_id": manifest["id"], "version": manifest["version"], "adapter": str(adapter),
              "workspace": str(output), "context_injected_via": CONFIG_FILE,
              "environment_class": ENVIRONMENT_CLASS if tools else "local", "tools": [t["name"] for t in tools],
              "roles": {}, "skills_dir": str(output / SKILLS_DIR) if skills else None, "skills": skills,
              "audit_log": str(audit) if tools else None,
              "command": [python, "-m", "minisweagent", "-c", CONFIG_FILE, "-m", "<model>"]}
    (output / MARKER).write_text(json.dumps(result, indent=2) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter", type=Path, required=True)
    parser.add_argument("--workspace", "--output", dest="workspace", type=Path, required=True)
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args()
    print(json.dumps(prepare(args.adapter, args.workspace, str(Path(args.python).absolute())), indent=2))
