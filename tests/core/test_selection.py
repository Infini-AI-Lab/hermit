"""`pytest --changed` (tests/selection.py): the core group runs for every change but documentation, a method's own
change adds its group, a shared layer runs everything, and every method in the repository has a group."""
from pathlib import Path
import subprocess

import pytest

from selection import METHODS, READ_BY, NoBase, changed_paths, groups_for, select

ROOT = Path(__file__).resolve().parents[2]
BUILT_IN = {"codex", "claude", "pi"}             # the generic AgentEvolvers: tests/core
NO_METHOD = {"prompt_only", "lean_collab"}       # templates without an Evolver of their own: tests/core


def test_a_methods_own_files_run_its_group_and_core():
    """The core tests read every method's template, manifest and packaging, so they run with a method's group."""
    for path, group in (("hermit/agent/evolver/methods/rrsi_adapter/evolver.py", "rrsi_adapter"),
                        ("hermit/adapter/templates/ahe/.hermit/check.py", "ahe"),
                        ("examples/officeqa/rrsi_adapter/domain.py", "rrsi_adapter"),
                        ("examples/officeqa/skillopt/run.yaml", "optimizers"),
                        ("tests/methods/ahe/test_ahe_hosts.py", "ahe"),
                        ("tests/fixtures/ahe_hosts/codex/Stop.json", "ahe")):
        assert groups_for(path) - READ_BY.get(group, set()) == {group, "core"}, path
    assert groups_for("docs/methods/ahe_adapter.md") == {"ahe", "core"}          # its capability table is held
    # a pattern's `*` is one directory: a benchmark's shared folder named like a method, or a method's own folder
    # named like another, is not that method's
    assert groups_for("examples/clbench_database/utils/task-info/mwm/host.json") >= {"mwm", "reharness", "core"}
    assert groups_for("examples/officeqa/rrsi_adapter/texts/ace/SKILL.md") == {"rrsi_adapter", "core"}
    assert groups_for("examples/officeqa/gepa/run.yaml") == {"optimizers", "core", "ahe"}     # read by AHE's
    assert groups_for("hermit/adapter/templates/harness2_codex/exports.yaml") == {"harness2", "core",
                                                                                  "continual_harness"}


def test_shared_layers_and_unknown_paths_run_everything():
    for path in ("hermit/agent/base/cli.py", "hermit/agent/evolver/base.py", "hermit/trainer/trainer.py",
                 "hermit/adapter/templates/ace/.hermit/adapter.yaml",      # every `<id>@<version>` is resolved
                                                                           # through all of them
                 "hermit/sandbox.py", "hermit/worker.py", "hermit/hosts/codex.py", "tests/conftest.py",
                 "tests/toy_task.py",
                 "pyproject.toml", "hermit/adapter/templates/prompt_only/ADAPTER.md", "Makefile",
                 ".github/workflows/tests.yml"):
        assert groups_for(path) is None, path
    groups, why = select(["hermit/agent/evolver/methods/ace/evolver.py", "hermit/sandbox.py"])
    assert groups is None and why == {"all": "hermit/sandbox.py"}


def test_the_rest_of_the_framework_runs_core_and_documentation_runs_nothing():
    assert groups_for("hermit/cli.py") == {"core"}
    assert groups_for("hermit/agent/evolver/methods/codex/evolver.yaml") == {"core"}
    assert groups_for("tests/core/test_deploy.py") == {"core"}
    assert groups_for("docs/_examples/file_adapter/ADAPTER.md") == {"core"}      # the core tests read it
    assert groups_for("docs/methods/rrsi_adapter.md") == set()
    assert groups_for("README.md") == set()
    assert groups_for("tests/README.md") == set()
    assert groups_for("examples/officeqa/README.md") == set()


def test_a_benchmarks_shared_files_run_the_methods_with_an_example_there():
    assert groups_for("examples/officeqa/utils/task.py") == {
        "ace", "ahe", "continual_harness", "harness2", "meta_harness_adapter", "optimizers", "reharness",
        "rrsi_adapter", "core"}
    assert groups_for("examples/servelearnbench/utils/x.py") == {"mwm", "core"}
    groups, why = select(["hermit/cli.py", "examples/officeqa/rrsi_adapter/README.md", "docs/index.md"])
    assert groups == {"core", "rrsi_adapter"} and why == {"core": "hermit/cli.py",
                                                         "rrsi_adapter": "examples/officeqa/rrsi_adapter/README.md"}


def test_every_method_and_template_belongs_to_a_group_with_tests():
    """A new method without a row in METHODS would fall to the core group: its own tests would never run with
    --changed."""
    methods = {p.name for p in (ROOT / "hermit/agent/evolver/methods").iterdir() if p.is_dir() and
               not p.name.startswith("__")} - BUILT_IN
    templates = {p.name for p in (ROOT / "hermit/adapter/templates").iterdir() if p.is_dir()} - NO_METHOD
    for name in methods:
        assert groups_for(f"hermit/agent/evolver/methods/{name}/evolver.py") - {"core"}, name
    for name in templates:
        assert groups_for(f"hermit/adapter/templates/{name}/.hermit/check.py") - {"core"}, name
    groups = {p.name for p in (ROOT / "tests/methods").iterdir() if p.is_dir() and not p.name.startswith("__")}
    assert groups == set(METHODS)


def test_every_test_file_under_tests_is_in_a_group():
    """A test file outside core/ and methods/<name>/ (a new tests/test_x.py) is in no group: --changed would never
    run it."""
    parts = [p.relative_to(ROOT / "tests").parts for p in (ROOT / "tests").rglob("test_*.py")]
    assert [p for p in parts if not (p[0] == "core" or p[0] == "methods" and len(p) > 2)] == []


def test_the_changed_paths_are_read_from_git_and_a_missing_base_is_said(tmp_path):
    def git(*args):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=tmp_path, check=True,
                       capture_output=True)
    git("init", "-q", "-b", "main")
    (tmp_path / "a.py").write_text("a\n")
    (tmp_path / "old.py").write_text("o\n")
    git("add", "-A")
    git("commit", "-qm", "base")
    git("switch", "-qc", "work")
    git("mv", "old.py", "new name \u00e9.py")
    git("commit", "-qm", "move")
    (tmp_path / "a.py").write_text("b\n")                     # uncommitted
    (tmp_path / "untracked.md").write_text("u\n")
    assert changed_paths("main", tmp_path) == ["a.py", "new name \u00e9.py", "old.py", "untracked.md"]
    with pytest.raises(NoBase, match="no-such-ref"):
        changed_paths("no-such-ref", tmp_path)


def _imports(path: Path, modules: dict) -> set[str]:
    """The framework modules a file imports (absolute and relative, `from x import y` as x and x.y) or runs (a
    module's name, a sibling's file name)."""
    import ast
    name = ".".join(path.relative_to(ROOT).with_suffix("").parts).removesuffix(".__init__")
    package = name if path.name == "__init__.py" else name.rpartition(".")[0]
    out = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = package.split(".")[:len(package.split(".")) - node.level + 1]
                base = ".".join(parts + ([node.module] if node.module else []))
            out |= {base} | {f"{base}.{a.name}" for a in node.names}
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # a module run by name (`-m hermit.x`) or by its file (`PACKAGE / "worker.py"`) is used as an import is
            if node.value.endswith(".py") and "/" not in node.value and (path.parent / node.value).is_file():
                out.add(f"{package}.{node.value[:-3]}")
            else:
                out.add(node.value)
    return {m for m in out if m in modules}


def test_what_the_shared_layers_and_the_method_tests_import_is_shared():
    """A framework module that a shared layer or a method's tests import (statically) runs every group when it
    changes: in the core group alone, a change to it would skip the methods it can break."""
    modules = {".".join(p.relative_to(ROOT).with_suffix("").parts).removesuffix(".__init__"): p
               for p in (ROOT / "hermit").rglob("*.py")}
    start = {m for m, p in modules.items() if groups_for(p.relative_to(ROOT).as_posix()) is None}
    for test in (ROOT / "tests" / "methods").rglob("test_*.py"):
        start |= _imports(test, modules)
    seen, todo = set(), list(start)
    while todo:
        m = todo.pop()
        if m in seen:
            continue
        seen.add(m)
        parts = m.split(".")
        todo += [".".join(parts[:i]) for i in range(1, len(parts)) if ".".join(parts[:i]) in modules]
        todo += _imports(modules[m], modules)
    core_only = sorted(modules[m].relative_to(ROOT).as_posix() for m in seen
                       if groups_for(modules[m].relative_to(ROOT).as_posix()) == {"core"})
    assert core_only == []


def test_a_method_whose_tests_read_another_groups_files_is_its_reader():
    """A method's tests that name another group's directory (its template, its example) run when that group's files
    change (READ_BY)."""
    import re
    owner = {}
    for group, patterns in METHODS.items():
        for pattern in patterns:
            parts = pattern.split("/")
            owner.setdefault(parts[-2] if parts[-1] == "*" else parts[-1], set()).add(group)
    missing = []
    for test in sorted((ROOT / "tests" / "methods").rglob("test_*.py")):
        me, text = test.parent.name, test.read_text()
        for key, groups in owner.items():
            if me not in groups and re.search(r"[\"'/]%s[\"'/]" % re.escape(key), text):
                missing += [f"{test.name} reads {key} ({g})" for g in groups if me not in READ_BY.get(g, ())]
    assert missing == []


def test_a_git_failure_is_said_never_a_short_list(tmp_path, monkeypatch):
    import selection
    real = subprocess.run

    def failing(args, **kw):
        if args[:2] == ["git", "diff"]:
            return subprocess.CompletedProcess(args, 128, b"", b"fatal: bad config")
        return real(args, **kw)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "b"],
                   cwd=tmp_path, check=True)
    monkeypatch.setattr(selection.subprocess, "run", failing)
    with pytest.raises(NoBase, match="git diff failed: fatal: bad config"):
        changed_paths("main", tmp_path)
