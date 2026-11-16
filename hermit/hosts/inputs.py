"""Inputs shared by the host deployers and by `convert`: rendered exports, names, roles, stale-role
retirement, skills and stale-skill retirement, the adapter's copy and its hooks."""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import uuid

from hermit.adapter.loader import _plain, inside, is_internal
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


def skill_sources(adapter: Path, manifest: dict) -> list[Path]:
    """The skill directories (one containing SKILL.md) of the adapter's `skills` export."""
    if not manifest.get("skills"):
        return []
    source = (adapter / manifest["skills"]).resolve()
    return [child for child in sorted(source.iterdir()) if (child / "SKILL.md").is_file()] if source.is_dir() else []


def check_skills(adapter: Path, output: Path, manifest: dict, previous: dict, target: Path) -> None:
    """A deployment's skills, checked before anything is written: the skills folder (`target`) and .retired-skills/
    are reached with no link on the way (what a deployment replaces and moves stays in the workspace), the previous
    record names plain skill directories, and no exported skill would replace one no deployment copied there (the
    user's own)."""
    for path in (target, output / ".retired-skills"):
        if _link_on_way(output, path):
            raise ValueError(f"{path.relative_to(output)} is a link or is reached through one; remove it, then deploy "
                             "again")
    owned = _recorded_skills(previous)
    for child in skill_sources(adapter, manifest):
        if os.path.lexists(target / child.name) and child.name not in owned:
            raise ValueError(f"{(target / child.name).relative_to(output)} exists and was not written by a "
                             "deployment; remove it, then deploy again")


def copy_skills(adapter: Path, manifest: dict, target: Path) -> list[str]:
    """Copy every skill directory of the adapter's `skills` export into target, in place of the copy an earlier
    deployment left there (check_skills): no file the skill no longer has survives."""
    names = []
    for child in skill_sources(adapter, manifest):
        _remove(target / child.name)
        shutil.copytree(child, target / child.name, symlinks=True)
        names.append(child.name)
    return names


def retire_skills(output, previous, current, target):
    """Move the skills an earlier deployment copied into `target` (its record's `skills`) that this one does not
    (`current`) out of host discovery; keep their contents. A skill the user put there is never in the record."""
    for name in _recorded_skills(previous):
        path = target / name
        if name in current or not os.path.lexists(path):
            continue
        archive = output / ".retired-skills" / uuid.uuid4().hex
        archive.mkdir(parents=True)
        path.rename(archive / name)


def _recorded_skills(previous: dict) -> list[str]:
    """The skills the previous deployment's record names, each a plain directory name."""
    names = previous.get("skills") or []
    if not isinstance(names, list) or any(not isinstance(name, str) or name in ("", ".", "..") or
                                          Path(name).name != name for name in names):
        raise ValueError("Previous skill metadata points outside the generated host workspace")
    return names


def _link_on_way(output: Path, path: Path) -> bool:
    """Whether a link (or a file) stands on the way from the workspace to `path`, `path` included."""
    here = output
    for part in path.relative_to(output).parts:
        here = here / part
        if here.is_symlink() or (here.exists() and not here.is_dir()):
            return True
    return False


def _remove(path: Path) -> None:
    """`path` gone, whatever it is: a link is removed, never followed."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _adapter_sandbox_env(python: str) -> dict:
    """The variables that put a tool server's Session in its own sandbox, when deploy runs inside a run. A host may
    start the server with a cleared environment, so they travel in the server's own `env`. Empty for a standalone
    deploy (no run around it): that server serves the adapter in place, as before."""
    if os.environ.get("HA_ADAPTER_SANDBOX") != "1":
        return {}
    from hermit.adapter.session import ENV_ENFORCE, ENV_PYTHON, ENV_SANDBOX
    from hermit.sandbox import ENV_HIDE
    return {ENV_SANDBOX: "1", ENV_ENFORCE: os.environ.get(ENV_ENFORCE, "required"), ENV_PYTHON: python,
            **({ENV_HIDE: os.environ[ENV_HIDE]} if os.environ.get(ENV_HIDE) else {})}


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
    return (f"This adapter's files are in `{rel}/`. Paths in the instructions below, and in the adapter's skills and "
            f"roles, are relative to that folder: run a command they give from there (`cd {rel} && ...`).")


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
            if name.endswith(".pyc") or (here == adapter and name in NOT_COPIED):
                continue
            if is_internal(source.relative_to(adapter).as_posix(), manifest):
                continue                          # the harness's own: not the agent's
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


# ---- what the host cannot connect: the adapter's files, named in the context -------------------------------------
# A deployment connects each export to the host's own place when the host has one; an export it has no place for is
# still in the adapter's copy, and the context names it, so that nothing the adapter holds is out of the agent's
# reach. The other files of the copy are listed too.
LISTED_FILES = 60                         # the most files the context lists; the rest are counted


def frontmatter(text: str) -> tuple[dict[str, str], str]:
    """A Markdown file's YAML-ish frontmatter as strings, and its body ({} without one)."""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end < 0:
        return {}, text
    head: dict[str, str] = {}
    for line in text[4:end].splitlines():
        key, sep, value = line.partition(":")
        if sep and re.fullmatch(r"[A-Za-z_][\w-]*", key.strip()):
            value = value.strip()
            if value.startswith('"') and value.endswith('"') and len(value) >= 2:   # a double-quoted YAML scalar
                try:
                    value = str(json.loads(value))
                except ValueError:
                    value = value.strip('"')
            else:
                value = value.strip("\"'")
            head[key.strip()] = value
    body = text[end + 4:]
    return head, body[body.find("\n") + 1:] if "\n" in body else ""


def _summary(path: Path) -> str:
    """One line about a file, from what its author wrote for a reader: the frontmatter description, a Markdown or text
    file's first line of prose (comments left out), a Python file's docstring or leading comment; else nothing (the
    first line of code says nothing about the file)."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read(4000)
    except (OSError, UnicodeDecodeError):
        return ""
    head, body = frontmatter(text)
    line = head.get("description", "")
    if not line and path.suffix in (".md", ".txt", ""):
        prose = re.sub(r"<!--.*?(-->|$)", "", body, flags=re.S)
        line = next((s.strip() for s in prose.splitlines() if s.strip()), "")
    elif not line and path.suffix == ".py":
        doc = re.match(r'\s*(?:#![^\n]*\n\s*)?(?:[rRuU]?("""|\'\'\')\s*(?P<doc>[^\n]*)|#\s*(?P<comment>[^\n]*))', text)
        line = (doc.group("doc") or doc.group("comment") or "") if doc else ""
    line = re.sub(r'^[#>*\-\s]+|[`]|("""|\'\'\')$', "", line).strip()
    return line[:117] + "..." if len(line) > 120 else line


def _skill_entries(adapter: Path, manifest: dict) -> list[tuple[str, str, str]]:
    """(name, description, path in the adapter) of every skill in the `skills` export."""
    if not manifest.get("skills"):
        return []
    source = (adapter / manifest["skills"]).resolve()
    out = []
    for child in sorted(source.iterdir()) if source.is_dir() else []:
        if (child / "SKILL.md").is_file():
            out.append((child.name, _summary(child / "SKILL.md"),
                        (child / "SKILL.md").relative_to(adapter.resolve()).as_posix()))
    return out


def native_hooks(hooks: dict | None, types: set[str]) -> dict | None:
    """The hooks the host runs itself: those of the `types` it has (a host skips a type it does not run)."""
    if not hooks:
        return None
    out = {}
    for event, groups in hooks["hooks"].items():
        kept = [{**g, "hooks": [h for h in g["hooks"] if h.get("type") in types]} for g in groups]
        kept = [g for g in kept if g["hooks"]]
        if kept:
            out[event] = kept
    return {"hooks": out} if out else None


def _hook_line(when: str, hook: dict, rel: str) -> str:
    """A hook the host does not run, as the agent can do it: a command to run, a check to make (a prompt or agent
    hook). A type with neither cannot be done from the context and is refused."""
    if hook.get("type") == "command" and "\n" in hook["command"].strip():
        body = "\n".join("  " + line for line in hook["command"].strip().splitlines())
        return f"- On {when}: run from `{rel}`:\n\n  ```sh\n{body}\n  ```"
    if hook.get("type") == "command":
        return f"- On {when}: run `cd {rel} && {hook['command'].strip()}`"
    if isinstance(hook.get("prompt"), str) and hook["prompt"].strip():
        return f"- On {when}: check this yourself before you go on: {' '.join(hook['prompt'].split())}"
    raise ValueError(f"a {hook.get('type')!r} hook of {when.strip('`')} can neither run on this host nor be done "
                     "by the agent from the context: deploy to a host that runs it, or remove it")


def resources(adapter: Path, output: Path, manifest: dict, exports: dict, hooks: dict | None,
              native: set[str], hook_types: set[str] = frozenset()) -> tuple[str, dict]:
    """The part of the context that names what the host does not connect (`native`: the exports it does, of
    skills and agents; `hook_types`: the hook types it runs) and the adapter's other files, with paths in the
    workspace; and the record of where each export went ({export: "native" | "files" | "native and files"})."""
    adapter = adapter.resolve()
    rel = adapter_folder(output, manifest).relative_to(output).as_posix()
    sections, record, covered = [], {}, {manifest.get("context") or "ADAPTER.md", "exports.yaml"}
    skills = _skill_entries(adapter, manifest)
    if skills:
        record["skills"] = "native" if "skills" in native else "files"
        covered |= {f"{_plain(manifest['skills']).rstrip('/')}/"}
        if "skills" not in native:
            sections.append("Skills (this agent does not load them itself: read a skill's SKILL.md before you use "
                            "it):\n" + "\n".join(f"- `{n}`: {d} (`{rel}/{p}`)" for n, d, p in skills))
    if exports.get("agents"):
        record["agents"] = "native" if "agents" in native else "files"
        if isinstance(manifest.get("agents"), str):
            covered |= {f"{_plain(manifest['agents']).rstrip('/')}/"}
        if "agents" not in native:
            lines = []
            for role in exports["agents"]:
                description = role.get("description", "").strip()
                if role.get("instructions"):
                    lines.append(f"- `{role['name']}`: {description} (instructions: `{rel}/{role['instructions']}`)")
                else:
                    lines.append(f"- `{role['name']}`: {description} Instructions:\n\n{role.get('body', '').strip()}\n")
            sections.append("Roles (this agent starts no sub-agents: when a step fits a role, do it yourself as its "
                            "instructions say):\n" + "\n".join(lines))
    if hooks and hooks.get("hooks"):
        lines, ran = [], False
        for event, groups in hooks["hooks"].items():
            for group in groups:
                when = f"`{event}`" + (f" for `{group['matcher']}`" if group.get("matcher") else "")
                for hook in group["hooks"]:
                    if hook.get("type") in hook_types:
                        ran = True
                    else:
                        lines.append(_hook_line(when, hook, rel))
        record["hooks"] = "native and files" if ran and lines else "native" if ran else "files"
        covered |= {_plain(manifest["hooks"])}
        if lines:
            sections.append(f"Hooks this agent does not run by itself (from `{rel}/{_plain(manifest['hooks'])}`): "
                            "at each of these moments, do it yourself:\n" + "\n".join(lines))
    doc = adapter / (manifest.get("context") or "ADAPTER.md")
    context = doc.read_bytes() if doc.is_file() else None
    others = []
    for path in sorted(p for p in adapter.rglob("*") if p.is_file()):
        if context is not None and path.stat().st_size == len(context) and path.read_bytes() == context:
            continue                              # a copy of the context (a tree's AGENTS.md): already in it
        name = path.relative_to(adapter).as_posix()
        if (name.split("/")[0] in NOT_COPIED or any(part in CACHES for part in path.parts) or name.endswith(".pyc")
                or name in covered or any(name.startswith(c) for c in covered if c.endswith("/"))
                or is_internal(name, manifest)):
            continue
        others.append(name)
    if others:
        listed = [f"- `{rel}/{n}`" + (f": {s}" if (s := _summary(adapter / n)) else "") for n in others[:LISTED_FILES]]
        if len(others) > LISTED_FILES:
            listed.append(f"- and {len(others) - LISTED_FILES} more files under `{rel}/`")
        sections.append("Other files of this adapter (read one when it is relevant):\n" + "\n".join(listed))
    text = ("### Adapter resources\n\n" + "\n\n".join(sections)) if sections else ""
    return text, record


def check_context_size(path: Path, text: str, limit: int | None, setting: str) -> None:
    """Refuse a context the host would cut: past `limit` bytes the host reads no further, silently."""
    size = len(text.encode("utf-8"))
    if limit and size > limit:
        raise ValueError(f"{path.name} would be {size} bytes, past the {limit} bytes the host reads ({setting}); "
                         f"the rest would be cut without a word. Raise {setting} and deploy with that limit")
