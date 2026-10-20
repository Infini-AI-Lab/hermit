"""The task tool `test_tools`: the self-test of the toolsmith adapter's tool bundles, for an agent learner (the built-in
Codex Evolver on the toolsmith template, ../toolsmith_codex/). The scripted toolsmith Evolver runs the same test after
every reply (hermit/algorithms/evolvers/toolsmith/evolver.py); an agent learner calls it when it wants to.

Why a tool: the learner's own sandbox has no GPU, no browser and no Node, so it cannot run what the acting agent runs.
This tool runs each named bundle through the agent's path (the template's `tool_call`, the runner confine.run_tool:
bubblewrap, no network, the GPU) exactly as the scripted Evolver's self-test does: the bundle's own `test` in a scratch
workspace, one call with its `example_args` in a workspace holding the agent's best recent page, and, when that call
returned images, one call per probe page of the task (a page whose correct picture is known). It answers with the same
text report the scripted Evolver's model reads: exit codes, timings, output tails, image measures and probe colours.
The images themselves stay outside the learner's sandbox: like the scripted learner (`images: 0`), it is blind.

Nothing the learner wrote runs in the Trainer's process: the test uses a fresh copy of the template's own files (from
the run's root, not the working copy) with only the learnable files (ADAPTER.md, the bundles under tools/) taken from
the working copy, symbolic links skipped. The learner's programs run only inside the confined runner. Nothing is
restored or changed in the working copy. Records: <run>/test_tools/calls.jsonl, one line per call.

Entrypoints per case (the task-info directory with the probe pages): waves, silk, lava.
"""
from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import importlib.util
import json
from pathlib import Path
import re
import shutil
from types import SimpleNamespace

import hermit

UTILS = Path(__file__).resolve().parent
EVOLVER = Path(hermit.__file__).resolve().parent / "algorithms" / "evolvers" / "toolsmith" / "evolver.py"
NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
LEARNABLE = ("ADAPTER.md",)
# The scripted Evolver's self-test settings (its evolver.yaml defaults, as ../toolsmith/run4_*.yaml uses them).
PARAMS = {"test_timeout_s": 300, "max_tests_per_round": 4, "test_total_s": 1200, "require_image": True}


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _copy_learned(source: Path, target: Path) -> None:
    """Regular files of a bundle directory, no symbolic links and no dot names, at any depth."""
    for path in sorted(source.rglob("*")):
        rel = path.relative_to(source)
        if any(part.startswith(".") for part in rel.parts) or "__pycache__" in rel.parts:
            continue
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            continue
        if any(p.is_symlink() for p in list(path.parents)[: len(rel.parts) - 1]):
            continue
        if path.is_dir():
            (target / rel).mkdir(parents=True, exist_ok=True)
        else:
            (target / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target / rel)


def candidate(adapter: Path, root: Path, target: Path) -> Path:
    """The adapter to test: the template's files from the run's root, the learnable files from the working copy."""
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(root, target, symlinks=True)
    tools = target / "tools"
    for entry in list(tools.iterdir()) if tools.is_dir() else []:
        if entry.name != "README.md":                    # tools/README.md is the template's own fixed text
            shutil.rmtree(entry) if entry.is_dir() and not entry.is_symlink() else entry.unlink()
    for name in LEARNABLE:
        path = adapter / name
        if path.is_file() and not path.is_symlink():
            shutil.copyfile(path, target / name)
    learned = adapter / "tools"
    if learned.is_dir() and not learned.is_symlink():
        for entry in sorted(learned.iterdir()):
            if entry.is_dir() and not entry.is_symlink() and not entry.name.startswith((".", "_")):
                _copy_learned(entry, tools / entry.name)
    return target


def run(names: list | None, adapter: Path, root: Path, task_info: Path, runner=None) -> dict:
    """The report of one test round; `runner` replaces confine.run_tool (tests)."""
    run_dir = adapter.parent.parent                      # <run>/workspace/adapter
    home = run_dir / "test_tools"
    home.mkdir(parents=True, exist_ok=True)
    with open(home / ".lock", "w") as lock:              # one round at a time: the rounds share the scratch directory
        fcntl.flock(lock, fcntl.LOCK_EX)
        evolver_module = _load("_visual_test_tools_evolver", EVOLVER)
        runner = runner or _load("_visual_test_tools_confine", UTILS / "confine.py").run_tool
        target = candidate(adapter, root, home / "adapter")
        notices: list[str] = []
        evolver = object.__new__(evolver_module.ToolsmithEvolver)
        iface = evolver._interface(target, notices)
        bundles = sorted(d.name for d in (target / "tools").iterdir()
                         if d.is_dir() and (d / "TOOL.json").is_file() and NAME.fullmatch(d.name))
        asked = [str(n) for n in names] if names else bundles
        unknown = [n for n in asked if n not in bundles]
        chosen = [n for n in asked if n in bundles]
        limit = PARAMS["max_tests_per_round"]
        if unknown:
            notices.append("no bundle (a tools/<name>/ directory with TOOL.json) named: " + ", ".join(unknown))
        if len(chosen) > limit:
            notices.append(f"only the first {limit} named bundles are tested in one call: " + ", ".join(chosen[limit:])
                           + " not tested")
            chosen = chosen[:limit]
        results: dict = {}
        if iface is not None and chosen:
            ws = SimpleNamespace(adapter=target, learning_material=run_dir / "learning_material", task_info=task_info,
                                 state=home / "state", scratch=home / "scratch", output=home / "output")
            ctx = SimpleNamespace(params=dict(PARAMS), workspace=ws, cancel=None, progress=None)
            seed = evolver._seed(ctx)
            results = evolver._test_round(ctx, iface, runner, seed, chosen, notices)
        elif not chosen:
            notices.append("nothing to test")
        report = evolver_module.ToolsmithEvolver._format_tests(results) if results else ""
        if notices:
            report = (report + "\n\n" if report else "") + "\n".join(f"note: {n}" for n in notices)
        working = {n: bool(r.get("working")) for n, r in results.items()}
        with open(home / "calls.jsonl", "a") as f:
            f.write(json.dumps({"time": datetime.now(timezone.utc).isoformat(), "asked": asked, "tested": chosen,
                                "working": working, "notices": notices, "results": results},
                               ensure_ascii=False, default=str) + "\n")
        return {"report": report or "nothing to test", "working": working}


def _for(case: str):
    def test_tools(names: list | None = None, _adapter=None, _root=None) -> dict:
        return run(names, Path(_adapter), Path(_root), UTILS / "task-info-generic" / case)
    test_tools.__name__ = case
    return test_tools


waves, silk, lava = _for("waves"), _for("silk"), _for("lava")
