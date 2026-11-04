"""A package exported by the loop, made a skill of the adapter's `skills` export.

The loop writes its packages for its task container: `/app/environment/skills/evo-<name>/` with a SKILL.md whose
frontmatter `name` is the directory's, and the task's files at the container's paths (OfficeQA: the corpus at
`/root/docs`). The adapter keeps one directory per package, `skills/<task>-evo-<name>/`, which the host installs as a
native skill (Codex: `.codex/skills/`); upstream's own executors find their Skills the same way. So the package gets
that name (directory and frontmatter, upstream's naming rule: lowercase letters, digits, single hyphens, at most 64
characters), and its paths are rewritten to the deployment's convention, which the deployed context states: paths in
the adapter's skills are relative to the adapter's folder (`hermit/<template id>/` of the workspace). Its own location
becomes `skills/<task>-evo-<name>`, the task's paths what `path_map` names ({container path: path relative to the
workspace}, from the example's config), seen from that folder. Nothing else is edited; a file that is not UTF-8 text
is copied as it is. The record lists each file's hash before and after.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import shutil

from hermit.agent.evolver.methods.coevoskills_adapter.upstream import real_tree, safe_name
from hermit.hosts.inputs import adapter_folder

TEMPLATE = "coevoskills-adapter"      # the template's id: a deployment puts the adapter under hermit/<id>/
SKILLS = "/app/environment/skills/"   # where upstream's container keeps the packages
TEXT = {".md", ".py", ".sh", ".txt", ".json", ".yaml", ".yml", ".toml"}
NAME_LIMIT = 64


def prefix(task_id: str) -> str:
    """The task's part of its skills' names: the id in upstream's name alphabet."""
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", safe_name(task_id).lower())).strip("-") or "task"


def skill_name(task_id: str, package: str) -> str:
    """`<task>-<package>`, cut to the name limit."""
    return f"{prefix(task_id)}-{package}"[:NAME_LIMIT].rstrip("-")


def project(source: Path, destination: Path, task_id: str, path_map: dict | None = None) -> dict:
    """Write the packages under `source` (evo-*/) as skills under `destination`; returns the record."""
    real_tree(source)
    packages = sorted(p.parent for p in source.glob("evo-*/SKILL.md"))
    if not packages:
        raise ValueError("the loop exported no Skill package")
    names = {p.name: skill_name(task_id, p.name) for p in packages}
    if len(set(names.values())) != len(names):
        raise ValueError(f"two packages of {task_id} get the same skill name: {sorted(names)}")
    workspace = Path(os.path.relpath(".", adapter_folder(Path("."), {"id": TEMPLATE})))   # the folder -> workspace
    mapping = {SKILLS + old: f"skills/{new}" for old, new in names.items()}
    mapping.update({str(k): (workspace / str(v)).as_posix() for k, v in (path_map or {}).items()})
    # A path matches only as a whole path: a similarly named path or another literal is left alone.
    rules = [(re.compile(re.escape(a) + r'''(?=$|[/\s'"`),;\]])'''), b)
             for a, b in sorted(mapping.items(), key=lambda x: -len(x[0]))]
    files = []
    destination.mkdir(parents=True, exist_ok=True)
    for package in packages:
        target = destination / names[package.name]
        shutil.copytree(package, target)
        for path in sorted(target.rglob("*")):
            if not path.is_file():
                continue
            path.chmod(path.stat().st_mode | 0o600)
            original = path.read_bytes()
            output, replacements = original, 0
            try:
                text = original.decode("utf-8") if path.suffix.lower() in TEXT else None
            except UnicodeDecodeError:
                text = None                           # not UTF-8 text: copied as it is
            if text is not None:
                for pattern, replacement in rules:
                    text, n = pattern.subn(replacement, text)
                    replacements += n
                if path == target / "SKILL.md":       # the frontmatter's name is the skill's directory
                    text = text.replace("\r\n", "\n")  # upstream's schema reads universal newlines
                    text, n = re.subn(rf"(\A---\n(?:.*\n)*?name:\s*[\"']?){re.escape(package.name)}([\"']?\s*\n)",
                                      rf"\g<1>{names[package.name]}\g<2>", text, count=1)
                    replacements += n
                output = text.encode("utf-8")
                if output != original:
                    path.write_bytes(output)
            files.append({"path": str(path.relative_to(destination)),
                          "source_sha256": hashlib.sha256(original).hexdigest(),
                          "deployed_sha256": hashlib.sha256(output).hexdigest(), "path_replacements": replacements})
        for path in [target, *target.rglob("*")]:
            if path.is_dir():
                path.chmod(path.stat().st_mode | 0o700)
    return {"version": 2, "task_id": task_id, "skills": sorted(names.values()), "mapping": mapping, "files": files}
