"""The test options for every test root (tests/ and the examples' own tests; tests/README.md): `--changed` runs the
groups a change touches (tests/selection.py), `--integration` also runs the tests marked `integration` (they need an
agent CLI installed on the machine, whose release they depend on)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TESTS = ROOT / "tests"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS))                       # the shared helpers: fakes.py, toy_task.py, selection.py

import pytest

# A Harbor example's task set (`utils/tasks`, the link `python -m hermit.harbor.dataset prepare` writes) holds the
# benchmark's own tests, which are no tests of Hermit's
collect_ignore_glob = ["examples/*/utils/tasks"]


def pytest_addoption(parser):
    parser.addoption("--integration", action="store_true", default=False,
                     help="also run the tests marked integration (an agent CLI installed on the machine)")
    parser.addoption("--changed", action="store_true", default=False,
                     help="run only the test groups the change since --base touches (tests/selection.py)")
    parser.addoption("--base", default="origin/main", help="what --changed compares with (default: origin/main)")


def pytest_configure(config):
    config.addinivalue_line("markers",
                            "integration: needs an agent CLI installed on the machine; runs with --integration")
    config._changed_groups = None                    # None: every group
    if config.getoption("--changed"):
        from selection import NoBase, changed_paths, select
        try:
            groups, why = select(changed_paths(config.getoption("--base"), ROOT))
        except NoBase as exc:
            raise pytest.UsageError(f"--changed: {exc}") from None
        config._changed_groups, config._changed_why = groups, why


def pytest_report_header(config):
    if not config.getoption("--changed"):
        return None
    if config._changed_groups is None:
        return [f"--changed: every group ({config._changed_why['all']} is shared or unknown)"]
    if not config._changed_groups:
        return ["--changed: no test group is touched"]
    return [f"--changed: {g} ({config._changed_why[g]})" for g in sorted(config._changed_groups)]


def _groups(path: Path) -> set[str]:
    """The groups a test file belongs to: its directory's under tests/; an example's own test (examples/*/utils/tests/)
    belongs to the groups a change to it would run."""
    from selection import groups_for
    path = path.resolve()
    if not path.is_relative_to(TESTS):
        return groups_for(path.relative_to(ROOT).as_posix()) or set()
    rel = path.relative_to(TESTS)
    if rel.parts[0] == "core":
        return {"core"}
    if rel.parts[0] == "methods" and len(rel.parts) > 2:
        return {rel.parts[1]}
    return set()


def pytest_collection_modifyitems(config, items):
    groups = config._changed_groups
    if groups is not None:
        kept = [i for i in items if _groups(Path(str(i.path))) & groups]
        config.hook.pytest_deselected(items=[i for i in items if i not in kept])
        config._changed_none = not kept              # nothing to run: the change needs no test
        items[:] = kept
    if config.getoption("--integration"):
        return
    skip = pytest.mark.skip(reason="an integration test: run with --integration")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


def pytest_sessionfinish(session, exitstatus):
    """A change that needs no test (documentation alone) passes, rather than pytest's "no tests ran"."""
    if getattr(session.config, "_changed_none", False) and exitstatus == pytest.ExitCode.NO_TESTS_COLLECTED:
        session.exitstatus = pytest.ExitCode.OK
