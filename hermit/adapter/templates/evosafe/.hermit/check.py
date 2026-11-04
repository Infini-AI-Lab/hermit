"""Syntax/shape checks only; runtime hook validation happens in the rollout sandbox."""
import ast
from pathlib import Path


def check(adapter_path):
    root = Path(adapter_path)
    issues = []
    for name in ("ADAPTER.md", "BUNDLE.md", "defense.py"):
        if not (root / name).is_file() or not (root / name).read_text().strip():
            issues.append({"path": name, "problem": "required nonempty file"})
    if issues:
        return issues
    try:
        tree = ast.parse((root / "defense.py").read_text())
    except SyntaxError as exc:
        return [{"path": "defense.py", "problem": str(exc)}]
    if not any(isinstance(node, ast.FunctionDef) and node.name == "build" for node in tree.body):
        issues.append({"path": "defense.py", "problem": "missing synchronous build() factory"})
    return issues
