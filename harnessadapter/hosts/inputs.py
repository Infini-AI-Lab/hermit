"""Inputs shared by the host deployers and by `convert`: rendered exports, names, roles, stale-role
retirement, skills."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import uuid

from harnessadapter.adapter.loader import inside

MARKER = ".harnessadapter-deploy.json"
# The directory holding this harnessadapter package: child processes must import this copy, not another installed one.
PACKAGE_ROOT = str(Path(__file__).resolve().parents[2])


def child_env() -> dict:
    """Environment for framework subprocesses: this package first on PYTHONPATH."""
    env = dict(os.environ)
    env["PYTHONPATH"] = PACKAGE_ROOT + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


def render_exports(adapter, python):
    """Render the context and the roles in a subprocess with the framework's interpreter."""
    result = subprocess.run([python, "-m", "harnessadapter.adapter.render_context", "--adapter", str(adapter)],
                            capture_output=True, text=True, env=child_env())
    if result.returncode != 0:
        raise RuntimeError(f"rendering the adapter's exports failed: {result.stderr.strip()[-1500:]}")
    return json.loads(result.stdout)


def retire_roles(output, previous, current):
    """Move stale generated roles out of host discovery; keep their contents."""
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


def server_name(manifest):
    return manifest["id"].replace("-", "_")


def source_label(manifest):
    context = manifest.get("context")
    return context if isinstance(context, str) else (context or {}).get("entrypoint", "adapter")


def copy_skills(adapter: Path, manifest: dict, target: Path) -> list[str]:
    """Copy every skill directory (one containing SKILL.md) of the adapter's `skills` export into target."""
    if not manifest.get("skills"):
        return []
    source = (adapter / manifest["skills"]).resolve()
    names = []
    if source.is_dir():
        for child in sorted(source.iterdir()):
            if (child / "SKILL.md").is_file():
                shutil.copytree(child, target / child.name, dirs_exist_ok=True)
                names.append(child.name)
    return names


def check_workspace(adapter: Path, output: Path, host: str) -> dict:
    if output == adapter or adapter in output.parents or output in adapter.parents:
        raise ValueError("The host workspace must be separate from the adapter")
    if output.exists() and any(output.iterdir()) and not (output / MARKER).is_file():
        raise ValueError("Refusing to deploy into a nonempty workspace this framework did not create")
    previous = json.loads((output / MARKER).read_text()) if (output / MARKER).is_file() else {}
    if previous and previous.get("host") != host:
        raise ValueError("This workspace was generated for another host")
    return previous


def role_body(adapter: Path, role: dict) -> str:
    """A role's instructions, inline or from the file it names."""
    return role.get("body") or inside(adapter, role["instructions"]).read_text()


def subagent_name(name: str) -> str:
    """A role name as the hosts that use Claude Code's subagent format spell it."""
    native = name.replace("_", "-")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", native):
        raise ValueError(f"Invalid native role name: {native}")
    return native


def subagent_markdown(native: str, description: str, instructions: str) -> str:
    """One subagent file: YAML frontmatter with the name and description, the body is the instructions."""
    return f"---\nname: {json.dumps(native)}\ndescription: {json.dumps(description)}\n---\n\n{instructions}"
