"""Inputs shared by the host deployers and by `convert`: rendered exports, names, roles, stale-role
retirement, skills, the adapter's copy and its hooks."""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import uuid

from hermit.adapter.loader import inside
from hermit.sandbox import FRAMEWORK, framework_env, run_in_strict_sandbox

MARKER = ".hermit-deploy.json"
# A deployment copies the whole adapter to <workspace>/hermit/<adapter id>/ (all but these), and connects what
# exports.yaml declares to the host's own places.
ADAPTER_DIR = "hermit"
NOT_COPIED = (".hermit", ".git")          # at the adapter's root
CACHES = ("__pycache__",)
# The directory holding this hermit package: child processes must import this copy, not another installed one.
PACKAGE_ROOT = str(FRAMEWORK)


def render_exports(adapter, python):
    """Render the context and the roles in a subprocess with the framework's interpreter. Inside a run (an acting
    agent's worker sets HA_ADAPTER_SANDBOX), the adapter's modules (the learner's work) are imported in a strict
    sandbox: the adapter read-only, no network, none of the agent's keys, so they never run with the agent's login."""
    command = [python, "-m", "hermit.adapter.render_context", "--adapter", str(adapter)]
    if os.environ.get("HA_ADAPTER_SANDBOX") == "1":
        from hermit.adapter.session import ENV_ENFORCE
        result = run_in_strict_sandbox(command, role="render", readable=[Path(adapter)],
                                       enforce=os.environ.get(ENV_ENFORCE, "required"))
    else:
        result = subprocess.run(command, capture_output=True, text=True, env=framework_env())
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
                shutil.copytree(child, target / child.name, dirs_exist_ok=True, symlinks=True)
                names.append(child.name)
    return names


def _adapter_sandbox_env(python: str) -> dict:
    """The variables that put a tool server's Session in its own sandbox, when deploy runs inside a run. A host may
    start the server with a cleared environment, so they travel in the server's own `env`. Empty for a standalone
    deploy (no run around it): that server serves the adapter in place, as before."""
    if os.environ.get("HA_ADAPTER_SANDBOX") != "1":
        return {}
    from hermit.adapter.session import ENV_ENFORCE, ENV_PYTHON, ENV_SANDBOX
    return {ENV_SANDBOX: "1", ENV_ENFORCE: os.environ.get(ENV_ENFORCE, "required"), ENV_PYTHON: python}


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


def adapter_folder(output: Path, manifest: dict) -> Path:
    """Where a deployment puts the adapter's copy."""
    return output / ADAPTER_DIR / manifest["id"]


def folder_note(output: Path, manifest: dict) -> str:
    """The line above the adapter's context that tells the agent where the adapter's files are."""
    rel = adapter_folder(output, manifest).relative_to(output).as_posix()
    return f"This adapter's files are in `{rel}/`. Paths in the instructions below are relative to that folder."


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _in_copy_area(output: Path, rel: str) -> bool:
    """Whether a workspace path lies under hermit/ with no link on the way: the deployment record lives in the
    workspace, so a path it names is checked before anything is removed or written there."""
    parts = Path(rel).parts
    if Path(rel).is_absolute() or len(parts) < 3 or parts[0] != ADAPTER_DIR or ".." in parts:
        return False
    here = output
    for part in parts[:-1]:
        here = here / part
        if here.is_symlink() or (here.exists() and not here.is_dir()):
            return False
    return True


def adapter_files(adapter: Path, output: Path, manifest: dict, previous: dict) -> dict:
    """The adapter's files as the deployment will copy them, {workspace path: digest}, checked before anything is
    written: no link may lead outside the adapter, no file the previous deployment copied may have been changed
    or replaced since (a redeployment replaces them), and no other file may be in the way."""
    adapter = adapter.resolve()
    folder = adapter_folder(output, manifest)
    files = {}
    for top, dirs, names in os.walk(adapter):
        here = Path(top)
        dirs[:] = sorted(d for d in dirs if d not in CACHES and not (here == adapter and d in NOT_COPIED))
        for d in dirs:
            if (here / d).is_symlink():
                raise ValueError(f"{(here / d).relative_to(adapter)} is a link to a directory; an adapter's copy "
                                 "holds files and directories only")
        for name in sorted(names):
            source = here / name
            if name.endswith(".pyc"):
                continue
            if not source.resolve().is_relative_to(adapter) or not source.resolve().is_file():
                raise ValueError(f"{source.relative_to(adapter)} leads outside the adapter or to no file")
            rel = (folder / source.relative_to(adapter)).relative_to(output).as_posix()
            files[rel] = _digest(source)
    earlier = previous.get("files") or {}
    if not isinstance(earlier, dict):
        raise ValueError(f"{MARKER}'s files are not a mapping; remove the workspace and deploy again")
    for rel in [*earlier, *files]:
        if not _in_copy_area(output, rel):
            raise ValueError(f"{rel} is not a path under {ADAPTER_DIR}/ in the workspace, or a link is on its way; "
                             "remove it, then deploy again")
    for rel, digest in earlier.items():
        path = output / rel
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError(f"{rel} is no longer the file the last deployment copied; remove it, then deploy again")
        if path.is_file() and _digest(path) != digest:
            raise ValueError(f"{rel} was changed since the last deployment; move the change elsewhere or remove the "
                             "file, then deploy again")
    for rel in files:
        path = output / rel
        if (path.exists() or path.is_symlink()) and rel not in earlier:
            raise ValueError(f"{rel} exists and was not written by a deployment; remove it, then deploy again")
    return files


def copy_adapter(adapter: Path, output: Path, manifest: dict, files: dict, previous: dict) -> None:
    """Replace the previous deployment's copy with the adapter's files (as `adapter_files` listed them)."""
    adapter, folder = adapter.resolve(), adapter_folder(output, manifest)
    for rel in previous.get("files") or {}:
        path = output / rel
        if path.is_file() or path.is_symlink():
            path.unlink()
        parent = path.parent
        while parent != output and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
    for rel in files:
        target = output / rel
        source = adapter / (output / rel).relative_to(folder)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source.resolve(), target)


def hook_commands(hooks: dict, folder: Path) -> dict:
    """The adapter's hooks with every command run from the adapter's copy, so that a hook finds the files it
    names where it found them in training (a session's hooks run in the directory the session started in)."""
    out = json.loads(json.dumps(hooks))
    for groups in out["hooks"].values():
        for hook in (h for g in groups for h in g["hooks"]):
            if hook.get("type") == "command":
                hook["command"] = f"cd {shlex.quote(str(folder))} || exit 1\n{hook['command']}"
    return out
