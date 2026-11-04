"""Which tests a change needs (`python -m pytest --changed`): the test groups whose paths it touches.

A group is tests/core/ (the framework) or tests/methods/<name>/ (one method). The core group runs for every change but
documentation: its tests also read every method's template, manifest and packaging. A method's group runs when the
method's own files change: its Evolver, its template, its examples, its tests. The shared layers every method is
built on (the Base Agents, the acting agents, the Evolver base classes, the Trainer, the sandbox, the hosts'
deployment, the model bindings, the test helpers) run every group, as does any path this table does not know.
Documentation runs none (docs/build-pages.sh checks it), but for docs/_examples/, which the core tests read.
"""
from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path
import subprocess

# group -> the paths (glob patterns, matched from the repository root) that are the method's own
METHODS = {
    "ace": ["hermit/agent/evolver/methods/ace/*", "hermit/adapter/templates/ace/*", "examples/*/ace/*"],
    "ahe": ["hermit/agent/evolver/methods/ahe_adapter/*", "hermit/adapter/templates/ahe/*",
            "examples/*/ahe_adapter/*", "tests/fixtures/ahe_hosts/*",
            "docs/methods/ahe_adapter.md"],                    # its capability table, which the hosts' tests hold
    "continual_harness": ["hermit/agent/evolver/methods/continual_harness/*",
                          "hermit/adapter/templates/continual_harness/*", "examples/*/continual_harness/*"],
    "harness2": ["hermit/agent/evolver/methods/harness2_upstream/*", "hermit/adapter/templates/harness2_codex/*",
                 "examples/*/harness2_stream/*"],
    "meta_harness_adapter": ["hermit/agent/evolver/methods/meta_harness_adapter/*",
                             "hermit/adapter/templates/meta_harness_adapter/*", "examples/*/meta_harness_adapter/*"],
    "mwm": ["hermit/agent/evolver/methods/mwm/*", "hermit/adapter/templates/mwm/*", "examples/*/mwm/*"],
    "optimizers": ["hermit/agent/evolver/methods/gepa/*", "hermit/agent/evolver/methods/skillopt/*",
                   "examples/*/gepa/*", "examples/*/skillopt/*"],
    "reharness": ["hermit/agent/evolver/methods/reharness/*", "hermit/adapter/templates/reharness/*",
                  "examples/*/reharness/*"],
    "rrsi_adapter": ["hermit/agent/evolver/methods/rrsi_adapter/*", "hermit/adapter/templates/rrsi_adapter/*",
                     "examples/*/rrsi_adapter/*"],
}
# group -> the other groups whose tests read its files (tests/core/test_selection.py finds a method's tests naming
# another group's directory)
READ_BY = {"optimizers": {"ahe"},                       # the AHE example's configs match GEPA's and SkillOpt's
           "harness2": {"continual_harness"}}           # Continual Harness deploys with the harness2_codex template
# what every method is built on: a change here runs every group (tests/core/test_selection.py checks that every
# framework module these import, or a method's tests import, is here too)
SHARED = ["hermit/agent/__init__.py", "hermit/agent/base/*", "hermit/agent/acting/*", "hermit/agent/evolver/*.py",
          "hermit/agent/model/*", "hermit/trainer/*", "hermit/tree.py", "hermit/evaluate.py",
          "hermit/sandbox.py", "hermit/sandbox_relay.py", "hermit/isolate.py", "hermit/spec.py", "hermit/hosts/*",
          "hermit/runtime/*", "hermit/adapter/*.py", "hermit/config.py", "hermit/defaults.yaml", "hermit/tools.py",
          "hermit/task.py", "hermit/__init__.py", "hermit/adapter/templates/prompt_only/*",   # the toy task's
          "pyproject.toml", "conftest.py", "tests/conftest.py", "tests/fakes.py",
          "tests/toy_task.py", "tests/selection.py"]
# the rest of the framework, the templates no method group owns, the documentation's examples: the core group
CORE = ["hermit/*", "tests/core/*", "docs/_examples/*"]
# nothing to run: documentation, and files no test reads
NONE = ["docs/*", "*.md", "LICENSE", "NOTICE", ".github/*", ".agents/*", "assets/*", ".gitignore"]


def groups_for(path: str) -> set[str] | None:
    """The groups a changed path needs; None: every group."""
    for name, patterns in METHODS.items():
        if path.startswith(f"tests/methods/{name}/") or any(fnmatch(path, p) for p in patterns):
            return {name, "core", *READ_BY.get(name, ())}
    if any(fnmatch(path, p) for p in SHARED):
        return None
    if path.startswith("examples/") and path.count("/") <= 2 and path.endswith("README.md"):
        return set()                                 # the examples' and a benchmark's description
    if path.startswith("examples/"):
        # a benchmark's shared files (utils/, data): every method with an example under that benchmark
        bench = path.split("/")[1]
        users = {name for name, patterns in METHODS.items() for p in patterns
                 if p.startswith("examples/*/") and _has_example(bench, p.split("/")[2])}
        return users | {"core"}
    if any(fnmatch(path, p) for p in CORE):
        return {"core"}
    if any(fnmatch(path, p) for p in NONE):
        return set()
    return None


def _has_example(bench: str, method_dir: str) -> bool:
    return (Path(__file__).resolve().parents[1] / "examples" / bench / method_dir).is_dir()


def select(paths: list[str]) -> tuple[set[str] | None, dict[str, str]]:
    """(the groups, None for all; why each group was chosen: the first path that asked for it)."""
    chosen: set[str] = set()
    why: dict[str, str] = {}
    for path in paths:
        groups = groups_for(path)
        if groups is None:
            return None, {"all": path}
        for g in groups - chosen:
            why[g] = path
        chosen |= groups
    return chosen, why


class NoBase(Exception):
    """The changed paths cannot be read: no merge base with the ref (no such ref, a shallow clone without it), or
    git failed listing them."""


def changed_paths(base: str, cwd) -> list[str]:
    """Every path changed since the merge base with `base`: commits, the working tree and untracked files, a move
    as both its paths. Names as git stores them, unquoted (-z)."""
    def git(*args) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True)
    found = git("merge-base", "HEAD", base)
    if found.returncode != 0:
        raise NoBase(f"no merge base of HEAD and {base!r} ({found.stderr.decode(errors='replace').strip()}): "
                     "fetch it, or name another ref with --base")
    merge_base = found.stdout.decode().strip()
    out = b""
    for args in (("diff", "-z", "--name-only", "--no-renames", merge_base), ("ls-files", "-z", "--others",
                                                                             "--exclude-standard")):
        listed = git(*args)
        if listed.returncode != 0:                  # never a partial list: it would skip what it left out
            raise NoBase(f"git {args[0]} failed: {listed.stderr.decode(errors='replace').strip()}")
        out += listed.stdout
    return sorted({p for p in out.decode("utf-8", errors="surrogateescape").split("\0") if p})
