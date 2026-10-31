"""Inputs shared by the plugin emitters: the embedded adapter, refusals, roles, the MCP command.

`deploy` prepares a workspace you work in; `convert` emits a plugin in a host's native format, which the host
installs and reads from a directory the emitter does not choose. Nothing an emitter writes may therefore assume
the interpreter, the package location or the working directory of the machine that produced it.

A part of the adapter that a host's plugin format cannot carry is refused, not approximated: the user learns
that the plugin would be incomplete before installing it.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import shutil

from hermit import __version__ as FRAMEWORK_VERSION
from hermit.adapter.loader import agent_tools, hook_definitions, load_manifest
from hermit.hosts.inputs import (copy_skills as _copy_skills, render_exports, role_body, server_name,
                                         subagent_markdown, subagent_name)

# The adapter travels inside the plugin: a plugin that referenced an outside directory would break the moment it
# was installed anywhere else.
ADAPTER_DIR = "adapter"
# `hermit serve`, not an interpreter path: the host resolves it on PATH wherever hermit is installed.
SERVER_COMMAND = "hermit"
AUDIT_LOG = "tool-calls.jsonl"
# Next to the embedded adapter: which adapter the plugin was made from and by which framework version. The
# `hermit` that `serve` resolves on PATH where the plugin is installed may be another version; `serve`
# reads this file and warns when it is.
RECORD = "hermit.json"


class Unsupported(ValueError):
    """Parts of the adapter the target's plugin format cannot carry."""


@dataclass(frozen=True)
class Bundle:
    adapter: Path
    manifest: dict
    context: str
    roles: list
    hooks: dict | None

    @property
    def id(self) -> str:
        return self.manifest["id"]

    @property
    def name(self) -> str:
        return self.manifest.get("name", self.id)

    @property
    def version(self) -> str:
        return self.manifest["version"]

    @property
    def tools(self) -> list:
        return agent_tools(self.manifest)          # the context tool is the harness's: a plugin cannot call it per task

    @property
    def server(self) -> str:
        return server_name(self.manifest)

    @property
    def has_context(self) -> bool:
        return bool(self.context.strip())


def load(adapter: Path, python: str) -> Bundle:
    adapter = adapter.resolve()
    manifest = load_manifest(adapter)
    exports = render_exports(adapter, python)
    return Bundle(adapter, manifest, exports["context"], list(exports["agents"]), hook_definitions(adapter, manifest))


def refuse(bundle: Bundle, target: str, reasons: dict[str, str], *, allow_lossy: bool) -> list[str]:
    """Fail on every part in `reasons` (part -> why the target cannot carry it), or with --allow-lossy return them
    as the parts the plugin leaves out. Hooks are added for every target: no plugin carries them yet."""
    if bundle.hooks:
        events = ", ".join(bundle.hooks["hooks"])
        reasons = {**reasons, "hooks": (f"no plugin carries an adapter's hooks yet ({events}); `hermit deploy --host "
                                        "codex` or `--host claude-code` connects them in a workspace")}
    if reasons and not allow_lossy:
        lines = "\n".join(f"- {part}: {why}" for part, why in reasons.items())
        other = ("Convert with --target claude-code, whose plugins carry context, skills, tools and roles, or pass"
                 if target != "Claude Code" else "Pass")
        raise Unsupported(f"A {target} plugin cannot carry these parts of the {bundle.id} adapter:\n{lines}\n"
                          f"{other} --allow-lossy to emit the plugin without them.")
    return sorted(reasons)


def prepare_output(output: Path, adapter: Path, own_file: str) -> Path:
    """The output directory: outside the adapter, and empty unless an earlier conversion wrote `own_file` there.
    `own_file` is one this framework writes and the host's own packages do not have, so a foreign directory is never
    taken for an earlier conversion and cleared."""
    output = output.resolve()
    if output == adapter or adapter in output.parents or output in adapter.parents:
        raise ValueError("The plugin must be written outside the adapter")
    if output.exists() and any(output.iterdir()) and not (output / own_file).is_file():
        raise ValueError("Refusing to write into a nonempty directory this framework did not create")
    output.mkdir(parents=True, exist_ok=True)
    return output


def clear(root: Path, *names: str) -> None:
    """Remove the generated entries of an earlier conversion, so a part the adapter no longer has (a skill, a
    role, the context) does not survive in the plugin. The host's own files there (its audit log) stay."""
    for name in names:
        path = root / name
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif path.exists() or path.is_symlink():
            path.unlink()


def embed_adapter(bundle: Bundle, plugin_root: Path) -> Path:
    """Copy the adapter into the plugin so the installed plugin is self-contained."""
    target = plugin_root / ADAPTER_DIR
    clear(plugin_root, ADAPTER_DIR)
    shutil.copytree(bundle.adapter, target, ignore=shutil.ignore_patterns("__pycache__", ".git", "*.pyc"))
    return target


def record(bundle: Bundle, plugin_root: Path, **extra) -> dict:
    """Write `hermit.json` next to the embedded adapter: the adapter's id and version, the framework
    version that produced the plugin, and the emitter's own entries."""
    data = {"plugin": bundle.id, "version": bundle.version, "framework": FRAMEWORK_VERSION, **extra}
    write_json(plugin_root / RECORD, data)
    return data


def copy_skills(bundle: Bundle, target: Path) -> list[str]:
    """The adapter's skill directories, copied by the same helper the host deployers use."""
    return _copy_skills(bundle.adapter, bundle.manifest, target)


def subagent_file(bundle: Bundle, role: dict) -> tuple[str, str]:
    """A role in Claude Code's subagent format, which is also what `deploy --host claude-code` writes."""
    native = subagent_name(role["name"])
    return native, subagent_markdown(native, role["description"], role_body(bundle.adapter, role))


def server_args(adapter_path: str, audit_path: str) -> list[str]:
    """The arguments of the `hermit serve` line a host runs to start the adapter's tools."""
    return ["serve", adapter_path, "--audit-log", audit_path]


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
