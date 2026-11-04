"""The test layers (tests/README.md): core/ for the framework, methods/<name>/ for each method. A test marked
`integration` needs something outside the repository (an agent CLI installed on the machine, whose release it
depends on) and runs only with `--integration`."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))         # the shared helpers: fakes.py, toy_task.py

import pytest


TESTS = Path(__file__).resolve().parent


def pytest_addoption(parser):
    parser.addoption("--integration", action="store_true", default=False,
                     help="also run the tests marked integration (an agent CLI installed on the machine)")
    parser.addoption("--changed", action="store_true", default=False,
                     help="run only the test groups the change since --base touches (tests/selection.py)")
    parser.addoption("--base", default="origin/main", help="what --changed compares with (default: origin/main)")


def pytest_configure(config):
    config.addinivalue_line("markers", "integration: needs an agent CLI installed on the machine; runs with --integration")
    config._changed_groups = None                   # None: every group
    if config.getoption("--changed"):
        from selection import NoBase, changed_paths, select
        try:
            groups, why = select(changed_paths(config.getoption("--base"), TESTS.parent))
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
        return groups_for(path.relative_to(TESTS.parent).as_posix()) or set()
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
        items[:] = kept
    if config.getoption("--integration"):
        return
    skip = pytest.mark.skip(reason="an integration test: run with --integration")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def shared_login(tmp_path_factory, monkeypatch):
    """The shared copy of a ChatGPT login (hermit.agent.base.login.ROOT) in a directory of the test's own, never the
    user's ~/.cache."""
    from hermit.agent.base import login
    root = tmp_path_factory.mktemp("codex-login")
    monkeypatch.setattr(login, "ROOT", root)
    monkeypatch.setattr(login, "PLAN", root / "plan")         # and its plan
    monkeypatch.setenv("HERMIT_HOMES", str(root / "homes"))   # and the CLI homes, never the user's ~/.cache
    return root


# Legacy API fixture loader; production runs generated code only in isolated_hooks.
from types import ModuleType
import uuid

def load_defense(adapter: Path | None):
    if adapter is None:
        return None
    # Compile source directly: no stale import cache or writes into the adapter.
    path = adapter / "defense.py"
    module_name = "_evosafe_candidate_" + uuid.uuid4().hex
    module = ModuleType(module_name)
    module.__file__ = str(path)
    # dataclasses with postponed annotations resolve their defining module at
    # class creation. A unique temporary module also avoids concurrent clashes.
    sys.modules[module_name] = module
    try:
        exec(compile(path.read_text(), str(path), "exec"), module.__dict__)
        defense = module.build()
    finally:
        sys.modules.pop(module_name, None)
    if getattr(defense, "hook_api", "dict-v1") not in {"dict-v1", "structured-v1"}:
        raise ValueError("unsupported defense hook_api")
    for name in ("system_prompt_transform", "on_pre_tool_call", "on_post_tool_call"):
        if not callable(getattr(defense, name, None)):
            raise ValueError(f"defense is missing {name}")
    return defense
