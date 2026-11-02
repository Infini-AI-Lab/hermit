"""Validate executable packages using the authors' unmodified, vendored schema."""
from pathlib import Path
import importlib.util
import sys


def check(adapter_path: str) -> list[dict]:
    root = Path(adapter_path)
    spec = importlib.util.spec_from_file_location("_coevoskills_skill_schema", Path(__file__).with_name("skill_schema.py"))
    schema = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = schema
    spec.loader.exec_module(schema)
    issues = []
    for path in root.rglob("*"):
        if path.is_symlink():
            issues.append({"path": str(path.relative_to(root)), "problem": "symbolic links are not allowed"})
    bank = root / "skills"
    if bank.exists() and not bank.is_symlink():
        for task in bank.iterdir():
            if not task.is_dir():
                issues.append({"path": str(task.relative_to(root)), "problem": "expected a task package directory"})
                continue
            manifests = list(task.glob("evo-*/SKILL.md"))
            if not manifests:
                issues.append({"path": str(task.relative_to(root)), "problem": "a task must contain an evo-* Skill"})
            for manifest in manifests:
                if manifest.is_symlink() or any(p.is_symlink() for p in manifest.parents if p != root):
                    continue
                issues += [{"path": str(manifest.relative_to(root)), "problem": i.message}
                           for i in schema.validate_skill_path(manifest)]
    return issues
