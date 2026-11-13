"""Which tests a change needs (`python -m pytest --changed`): the test groups whose paths it touches.

A group is tests/core/ (the framework) or tests/methods/<name>/ (one method). The core group runs for every change but
documentation: its tests also read every method's template, manifest and packaging. A method's group runs when the
method's own files change: its Evolver, its template, its examples, its tests. The shared layers every method is
built on (the Base Agents, the acting agents, the Evolver base classes, the Trainer, the sandbox, the hosts'
deployment, the model bindings, the test helpers) run every group, as does any path this table does not know.
Documentation runs none (docs/build-pages.sh checks it), but for docs/_examples/, which the core tests read.
"""
from __future__ import annotations

from pathlib import Path
import re
import subprocess

# Patterns are matched from the repository root: `*` stays within one path component, and a trailing `/*` is
# everything below that directory.


def fnmatch(path: str, pattern: str) -> bool:
    below = pattern.endswith("/*")
    head = pattern[:-2] if below else pattern
    regex = "".join("[^/]*" if c == "*" else re.escape(c) for c in head) + ("/.+" if below else "")
    return re.fullmatch(regex, path) is not None


# group -> the paths (patterns, as `fnmatch` above reads them) that are the method's own
METHODS = {
    "aspire": ["hermit/agent/evolver/methods/aspire/*", "hermit/adapter/templates/aspire/*",
               "examples/aspire_robosuite/*"],
    "she": ["hermit/agent/evolver/methods/she/*", "hermit/adapter/templates/she/*", "examples/*/she/*"],
    "ace": ["hermit/agent/evolver/methods/ace/*", "hermit/adapter/templates/ace/*", "examples/*/ace/*"],
    "ahe": ["hermit/agent/evolver/methods/ahe_adapter/*", "hermit/adapter/templates/ahe/*",
            "examples/*/ahe_adapter/*", "tests/fixtures/ahe_hosts/*",
            "docs/methods/ahe_adapter.md"],                    # its capability table, which the hosts' tests hold
    "continual_harness": ["hermit/agent/evolver/methods/continual_harness/*",
                          "hermit/adapter/templates/continual_harness/*", "examples/*/continual_harness/*"],
    "evosafe": ["hermit/agent/evolver/methods/evosafe/*", "hermit/adapter/templates/evosafe/*",
                "examples/*/evosafe/*"],
    "harness2": ["hermit/agent/evolver/methods/harness2_upstream/*", "hermit/adapter/templates/harness2_codex/*",
                 "examples/*/harness2_stream/*"],
    "meta_harness_adapter": ["hermit/agent/evolver/methods/meta_harness_adapter/*",
                             "hermit/adapter/templates/meta_harness_adapter/*", "examples/*/meta_harness_adapter/*"],
    "mwm": ["hermit/agent/evolver/methods/mwm/*", "hermit/adapter/templates/mwm/*", "examples/*/mwm/*"],
    "opro": ["hermit/agent/evolver/methods/opro/*", "examples/*/opro/*"],
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
           "she": {"evosafe"},                         # AgentDyn runner tests freeze SHE configs
           "evosafe": {"she"},                         # SHE reuses the CLI roles and verdict contract
           "harness2": {"continual_harness"}}           # Continual Harness deploys with the harness2_codex template
# what every method is built on: a change here runs every group (tests/core/test_selection.py checks that every
# framework module these import, or a method's tests import, is here too)
SHARED = ["hermit/agent/__init__.py", "hermit/agent/base/*", "hermit/agent/acting/*", "hermit/harbor/*",
          "hermit/agent/evolver/*.py",
          "hermit/agent/evolver/methods/__init__.py",
          "hermit/agent/model/*", "hermit/trainer/*", "hermit/tree.py", "hermit/evaluate.py",
          "hermit/sandbox.py", "hermit/sandbox_relay.py", "hermit/isolate.py", "hermit/worker.py", "hermit/spec.py",
          "hermit/redact.py",
          "hermit/hosts/*",
          "hermit/runtime/*", "hermit/adapter/*.py", "hermit/config.py", "hermit/defaults.yaml", "hermit/tools.py",
          "hermit/task.py", "hermit/__init__.py", "hermit/adapter/templates/prompt_only/*",   # the toy task's
          "pyproject.toml", "conftest.py", "tests/conftest.py", "tests/fakes.py",
          "tests/toy_task.py", "tests/selection.py",
          "tests/pi_stand.py", "tests/fixtures/pi/*",                   # the Pi stand-in: the methods'
          "tests/claude_stand.py", "tests/fixtures/claude_streams/*",   # the Claude stand-in: core's and the methods'
          ".github/workflows/tests.yml"]                    # how CI runs them: a change to it runs them all
# the rest of the framework, the templates no method group owns, the documentation's examples: the core group
CORE = ["hermit/*", "tests/core/*", "tests/fixtures/opencode/*", "tests/fixtures/dsh/*", "docs/_examples/*"]
# nothing to run: documentation, and files no test reads
NONE = ["docs/*", "*.md", "tests/README.md", "LICENSE", "NOTICE", ".github/*", ".agents/*", "assets/*", ".gitignore"]


def groups_for(path: str) -> set[str] | None:
    """The groups a changed path needs; None: every group."""
    if fnmatch(path, "hermit/adapter/templates/*/.hermit/adapter.yaml"):
        return None                                 # a template's id and version: Config.template_dir reads them all
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
