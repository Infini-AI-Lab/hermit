"""check_spec: does an adapter conform to its template's specification and deploy?  And the Trainer's repair.

Checks 1–5 are the framework library; check 6 is the template's own function (`check: file.py:function` in
adapter.yaml), run in a subprocess without network. The template function receives the adapter path and returns a
list of issues ({path, problem}) or {"passed": bool, "issues": [...]}.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import yaml

from harnessadapter.adapter.manifest import (CONTEXT, FIXED_SECTIONS, FRAMEWORK_FILES, MANIFEST, files_of, read_manifest,
                                             structure_of)

DEPLOY_TIMEOUT = 60
TEMPLATE_CHECK_TIMEOUT = 120


@dataclass
class Issue:
    path: str          # the file concerned ("" when the issue is not about one file)
    check: int         # 1..6, numbered as in the documentation
    problem: str


@dataclass
class CheckResult:
    passed: bool
    issues: list[Issue] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"passed": self.passed, "issues": [asdict(i) for i in self.issues]}


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def library_checks(adapter: Path, root: Path, *, deploy: bool = True, python: str | None = None) -> list[Issue]:
    """Checks 1–5. `root` is a copy of the tree's root (the template) used as the reference."""
    adapter, root = Path(adapter), Path(root)
    issues: list[Issue] = []
    try:
        reference = read_manifest(root)
    except ValueError as exc:
        return [Issue(MANIFEST, 3, f"the template's own manifest is invalid: {exc}")]
    structure = structure_of(reference)
    # 3. the manifest parses and its fixed sections equal the root's.
    try:
        manifest = read_manifest(adapter)
        for section in FIXED_SECTIONS:
            if manifest.get(section) != reference.get(section):
                issues.append(Issue(MANIFEST, 3, f"section `{section}` differs from the template's"))
    except (ValueError, yaml.YAMLError) as exc:
        issues.append(Issue(MANIFEST, 3, f"adapter.yaml does not parse: {exc}"))
    have = set(files_of(adapter))
    root_files = set(files_of(root))
    if CONTEXT not in have:
        issues.append(Issue(CONTEXT, 3, f"{CONTEXT} is missing: every adapter has one, its context for the agent"))
    # 1. fixed parts unchanged.
    for rel in sorted(root_files):
        if rel in FRAMEWORK_FILES or not structure.is_fixed(rel):
            continue
        if rel not in have:
            issues.append(Issue(rel, 1, "fixed file is missing"))
        elif _digest(adapter / rel) != _digest(root / rel):
            issues.append(Issue(rel, 1, "fixed file was changed"))
    # 2. every file lies in a learnable path (or is a fixed file of the template).
    for rel in sorted(have - set(FRAMEWORK_FILES)):
        if structure.is_fixed(rel):
            if rel not in root_files:
                issues.append(Issue(rel, 2, "file added under a fixed path"))
        elif structure.learnable_for(rel) is None:
            issues.append(Issue(rel, 2, "file outside every learnable path"))
    # 4. learnable parts follow their formats.
    counts: dict[str, int] = {}
    for rel in sorted(have):
        item = structure.learnable_for(rel)
        if item is None:
            continue
        counts[item.path] = counts.get(item.path, 0) + 1
        fmt = item.format
        data = (adapter / rel).read_bytes()
        if fmt.get("max_bytes") is not None and len(data) > int(fmt["max_bytes"]):
            issues.append(Issue(rel, 4, f"{len(data)} bytes exceeds max_bytes {fmt['max_bytes']} of `{item.path}`"))
        if fmt.get("text") or fmt.get("frontmatter"):
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                issues.append(Issue(rel, 4, f"not UTF-8 text, as `{item.path}` requires"))
                continue
            if fmt.get("frontmatter"):
                head = _frontmatter(text)
                if head is None:
                    issues.append(Issue(rel, 4, "missing YAML frontmatter"))
                else:
                    missing = [k for k in fmt["frontmatter"] if k not in head]
                    if missing:
                        issues.append(Issue(rel, 4, f"frontmatter lacks {missing}"))
    for item in structure.learnable:
        if item.format.get("max_files") is not None and counts.get(item.path, 0) > int(item.format["max_files"]):
            issues.append(Issue(item.path, 4, f"{counts[item.path]} files exceed max_files {item.format['max_files']}"))
    # 5. deploys: context renders, tool entry points import, roles convert. Only when the manifest itself is sound.
    if deploy and not any(i.check == 3 for i in issues):
        issues.extend(_deploy_check(adapter, python or sys.executable))
    return issues


def _frontmatter(text: str) -> dict | None:
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---", 4)
    if end < 0:
        return None
    try:
        head = yaml.safe_load(text[4:end])
    except yaml.YAMLError:
        return None
    return head if isinstance(head, dict) else None


# Checks 5 and 6 run code: the adapter's modules (the learner's work) and the template's check. They run in a
# sandbox of their own (#11): the adapter and the template read-only, no network, none of the environment's keys.
# `required` refuses to run without bubblewrap; a run sets its sandbox.enforce here (the Trainer does).
SANDBOX_ENFORCE = "best_effort"


def _run_sandboxed(command: list[str], readable: list[Path], timeout: float) -> subprocess.CompletedProcess:
    # SANDBOX_ENFORCE is None when the run's sandbox is off (sandbox.enabled: false): run unconfined, as the rest of
    # that run does. Otherwise the adapter's modules run in a strict sandbox under the run's enforce.
    from harnessadapter.sandbox import framework_env, run_in_strict_sandbox
    if SANDBOX_ENFORCE is None:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout,
                              env=framework_env(no_bytecode=True))
    return run_in_strict_sandbox(command, role="spec", readable=readable, enforce=SANDBOX_ENFORCE, timeout=timeout,
                                 no_bytecode=True)


def _deploy_check(adapter: Path, python: str) -> list[Issue]:
    try:
        run = _run_sandboxed([python, "-m", "harnessadapter.adapter.render_context", "--adapter", str(adapter)],
                             [adapter], DEPLOY_TIMEOUT)
    except subprocess.TimeoutExpired:
        return [Issue("", 5, f"rendering the exports took longer than {DEPLOY_TIMEOUT} s")]
    if run.returncode != 0:
        tail = (run.stderr or run.stdout).strip().splitlines()[-6:]
        return [Issue(_path_in(adapter, "\n".join(tail)), 5, "does not deploy: " + " | ".join(tail))]
    return []


def _path_in(adapter: Path, text: str) -> str:
    """The adapter file a deploy error names, if any (so repair can revert just that file)."""
    for rel in files_of(adapter):
        if rel in text or str(adapter / rel) in text:
            return rel
    return ""


def template_check(adapter: Path, root: Path, python: str | None = None) -> list[Issue]:
    """Check 6: the template's own function, if declared; run without network when bubblewrap is available."""
    manifest = read_manifest(root)
    declaration = manifest.get("check")
    if not declaration:
        return []
    filename, _, function = declaration.partition(":")
    script = (
        "import importlib.util, json, sys\n"
        f"spec = importlib.util.spec_from_file_location('template_check', {json.dumps(str(Path(root) / filename))})\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        f"r = m.{function}(sys.argv[1])\n"
        "print(json.dumps(r))\n")
    command = [python or sys.executable, "-c", script, str(adapter)]
    try:
        run = _run_sandboxed(command, [adapter, root], TEMPLATE_CHECK_TIMEOUT)
    except subprocess.TimeoutExpired:
        return [Issue("", 6, f"the template check took longer than {TEMPLATE_CHECK_TIMEOUT} s")]
    if run.returncode != 0:
        return [Issue("", 6, "the template check failed to run: " + run.stderr.strip()[-600:])]
    try:
        result = json.loads(run.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return [Issue("", 6, "the template check printed no JSON result")]
    raw = result.get("issues", []) if isinstance(result, dict) else result
    return [Issue(str(i.get("path", "")), 6, str(i.get("problem", i))) for i in raw or []]


def check_spec(adapter: Path, root: Path, *, deploy: bool = True, python: str | None = None) -> CheckResult:
    issues = library_checks(adapter, root, deploy=deploy, python=python)
    if not any(i.check == 3 for i in issues):
        issues.extend(template_check(adapter, root, python))
    return CheckResult(not issues, issues)


def repair(adapter: Path, root: Path, parent: Path, *, python: str | None = None,
           max_rounds: int = 20) -> tuple[CheckResult, list[dict]]:
    """Make the adapter conform with the smallest change; return the final result and what was repaired.

    fixed file → root's version; learnable file in a wrong format → parent's version (removed if new); file outside
    every path → removed; deploy / template-check failure → the named file to the parent's version, otherwise every
    file changed since the parent. The worst case is the parent unchanged.
    """
    adapter, root, parent = Path(adapter), Path(root), Path(parent)
    repaired: list[dict] = []
    result = check_spec(adapter, root, python=python)
    rounds = 0
    while not result.passed and rounds < max_rounds:
        rounds += 1
        targets: dict[str, tuple[str, str]] = {}
        for issue in result.issues:
            rel = issue.path
            if rel == CONTEXT and not (adapter / CONTEXT).exists():
                targets[rel] = ("parent", issue.problem)
            elif issue.check == 3 or rel == MANIFEST:
                targets[MANIFEST] = ("root", issue.problem)
            elif issue.check == 1:
                targets[rel] = ("root", issue.problem)
            elif issue.check == 2:
                targets[rel] = ("parent", issue.problem)
            elif rel and (adapter / rel).exists() or rel and (parent / rel).exists():
                targets[rel] = ("parent", issue.problem)
            else:   # not attributable to one file: revert everything changed since the parent
                for changed in _changed(adapter, parent):
                    targets.setdefault(changed, ("parent", issue.problem))
        if not targets:
            break
        for rel, (source, why) in targets.items():
            _restore(adapter, root if source == "root" else parent, rel)
            repaired.append({"path": rel, "restored_from": source, "reason": why})
        result = check_spec(adapter, root, python=python)
    if not result.passed:
        # Last resort: the parent itself (always valid: it was committed only after passing).
        for rel in _changed(adapter, parent):
            _restore(adapter, parent, rel)
            repaired.append({"path": rel, "restored_from": "parent", "reason": "repair did not converge"})
        result = check_spec(adapter, root, python=python)
    return result, repaired


def _changed(adapter: Path, parent: Path) -> list[str]:
    a, p = set(files_of(adapter)), set(files_of(parent))
    out = sorted((a ^ p))
    out += sorted(rel for rel in a & p if _digest(adapter / rel) != _digest(parent / rel))
    return out


def _restore(adapter: Path, source: Path, rel: str) -> None:
    target = adapter / rel
    if (source / rel).is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / rel, target)
    elif target.exists():
        target.unlink()
        # Remove directories the deletion left empty.
        for directory in list(target.parents):
            if directory == adapter or not directory.is_relative_to(adapter):
                break
            try:
                directory.rmdir()
            except OSError:
                break


def main(argv=None) -> int:
    """`python -m harnessadapter.spec ADAPTER --root TEMPLATE`: print the check result as JSON."""
    import argparse
    parser = argparse.ArgumentParser(description="Run check_spec on an adapter against its template root.")
    parser.add_argument("adapter", type=Path)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args(argv)
    result = check_spec(args.adapter, args.root)
    print(json.dumps(result.to_dict(), indent=2))
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
