"""`pytest --changed` (tests/selection.py): a method's own change runs its group, a shared layer runs everything, and
every method in the repository has a group."""
from pathlib import Path
import subprocess

import pytest

from selection import METHODS, NoBase, changed_paths, groups_for, select

ROOT = Path(__file__).resolve().parents[2]
BUILT_IN = {"codex", "claude"}                   # the generic AgentEvolvers: tests/core
NO_METHOD = {"prompt_only", "lean_collab"}       # templates without an Evolver of their own: tests/core


def test_a_methods_own_files_run_its_group_only():
    assert groups_for("hermit/agent/evolver/methods/rrsi_adapter/evolver.py") == {"rrsi_adapter"}
    assert groups_for("hermit/adapter/templates/ahe/.hermit/check.py") == {"ahe"}
    assert groups_for("examples/officeqa/rrsi_adapter/domain.py") == {"rrsi_adapter"}
    assert groups_for("examples/officeqa/skillopt/run.yaml") == {"optimizers"}
    assert groups_for("tests/methods/ahe/test_ahe_hosts.py") == {"ahe"}
    assert groups_for("tests/fixtures/ahe_hosts/codex/Stop.json") == {"ahe"}


def test_shared_layers_and_unknown_paths_run_everything():
    for path in ("hermit/agent/base/cli.py", "hermit/agent/evolver/base.py", "hermit/trainer/trainer.py",
                 "hermit/sandbox.py", "hermit/hosts/codex.py", "tests/conftest.py", "tests/toy_task.py",
                 "pyproject.toml", "hermit/adapter/templates/prompt_only/ADAPTER.md", "Makefile"):
        assert groups_for(path) is None, path
    groups, why = select(["hermit/agent/evolver/methods/ace/evolver.py", "hermit/sandbox.py"])
    assert groups is None and why == {"all": "hermit/sandbox.py"}


def test_the_rest_of_the_framework_runs_core_and_documentation_runs_nothing():
    assert groups_for("hermit/cli.py") == {"core"}
    assert groups_for("hermit/agent/evolver/methods/codex/evolver.yaml") == {"core"}
    assert groups_for("tests/core/test_deploy.py") == {"core"}
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
    assert groups == {"core", "rrsi_adapter"} and why["rrsi_adapter"] == "examples/officeqa/rrsi_adapter/README.md"


def test_every_method_and_template_belongs_to_a_group_with_tests():
    """A new method without a row in METHODS would fall to the core group: its own tests would never run with
    --changed."""
    methods = {p.name for p in (ROOT / "hermit/agent/evolver/methods").iterdir() if p.is_dir() and
               not p.name.startswith("__")} - BUILT_IN
    templates = {p.name for p in (ROOT / "hermit/adapter/templates").iterdir() if p.is_dir()} - NO_METHOD
    for name in methods:
        assert groups_for(f"hermit/agent/evolver/methods/{name}/evolver.py") not in (None, {"core"}), name
    for name in templates:
        assert groups_for(f"hermit/adapter/templates/{name}/.hermit/adapter.yaml") not in (None, {"core"}), name
    groups = {p.name for p in (ROOT / "tests/methods").iterdir() if p.is_dir() and not p.name.startswith("__")}
    assert groups == set(METHODS)


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
