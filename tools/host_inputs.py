"""Common inputs for host-specific delivery; no method-specific transport code."""
import json
from pathlib import Path
import subprocess
import uuid

ROOT = Path(__file__).resolve().parents[1]


def render_exports(adapter, manifest, python):
    result = subprocess.run([python, str(ROOT / "framework/render_context.py"), "--adapter", str(adapter)],
                            capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def render_context(adapter, manifest, python):
    return render_exports(adapter, manifest, python)["context"]


def retire_roles(output, previous, current):
    """Move stale generated roles out of host discovery; preserve their contents."""
    active = {Path(p).resolve() for p in current.values()}
    allowed = [(output / '.codex/agents').resolve(), (output / '.claude/agents').resolve()]
    for value in previous.get('roles', {}).values():
        path = Path(value).resolve()
        if path in active or not path.is_file():
            continue
        if not any(path.is_relative_to(base) for base in allowed):
            raise ValueError('Previous role metadata points outside the generated host workspace')
        archive = output / '.retired-roles' / uuid.uuid4().hex
        archive.mkdir(parents=True)
        path.rename(archive / path.name)


def adapter_id(manifest):
    return manifest.get("id", manifest["name"].replace("_", "-"))


def server_name(manifest):
    return "ha_demo" if adapter_id(manifest) == "minimal-shop" else adapter_id(manifest).replace("-", "_")


def source_label(manifest):
    return manifest["context"] if isinstance(manifest["context"], str) else manifest["context"]["entrypoint"]


def generic_task(output, manifest):
    if not manifest.get("tools"):
        (output / "PROMPT.md").write_text(
            "Read the adapter context included in the project instructions. Briefly report what it provides. "
            "If it contains no learned instructions yet, say so. No adapter tools or roles are required.\n")
    elif adapter_id(manifest) != "minimal-shop":
        (output / "PROMPT.md").write_text(
            "Use this adapter through its registered MCP tools. For MWM, call wm(name=\"list\"), "
            "read a relevant memory if one exists, and report its scope, last-confirmed time, and source. "
            "If memory is empty, report that honestly. Do not edit files or invent environment facts.\n")
