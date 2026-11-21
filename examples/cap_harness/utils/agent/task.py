"""The engine's task functions for a coding-agent session (agent.base: codex, or claude): what every method's session
on a benchmark of this engine gets, and nothing of a method. They run in the task sandbox with the session; the robot
execution engine runs on the host, as the simulator service (sim_service.py), which the session reaches only through
`bin/sim`. A benchmark gives the prompt (LIBERO's ../../../libero/utils/agent/task.py); a method that brings its own
session (ASPIRE: hermit/agent/evolver/methods/aspire/actor.py) builds on these.

    prepare  the workspace: bin/sim and its registration with the service (the workspace and the task's id: the
             service takes the seeds and the budget from its own copy of the task row), the API and artifact docs,
             the benchmark's page, the scene-exploration procedure and its snapshot program
    answer   the agent's last message (the record's output is the facts collect returns)
    collect  the session's files into the log, and the facts the judge reads: the scored program
             (`<task_dir>/fix_code.py`) and the unseen check (`<task_dir>/debug/holdout`); the judge runs the program
             on the held-out seeds, on the host

The checkout of ASPIRE, whose `cap-harness` this engine is, comes from agent.params.aspire_root (its snapshot program
and exploration page must be declared readable: environment.read); its docs are read from agent.params.aspire_docs,
the staged copies; the service's URL from ASPIRE_SIM_URL (environment.env). A task row carries the seeds:
development, unseen-check and held-out (../benchmark/tasks.py).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import sys
import urllib.request

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import sim  # noqa: E402

SKILLS = Path(".claude/skills/iterative-debugging")
# The files of ASPIRE's checkout the task sandbox shows (environment.read) for every session: the scene-exploration
# procedure and its snapshot program; not its shipped library, worked solutions or examples, nor its docs, which the
# sandbox is shown as staged (agent.params.aspire_docs: stage_docs.py's copies, without what names the worked programs)
ASPIRE_FILES = [str(SKILLS / "scripts" / "scene_snapshot.py"), str(SKILLS / "skills" / "task-exploration.md")]
# The workspace files the log keeps (the run directories stay in the workspace)
KEEP = ("task_analysis.md", "fix_code.py", "findings.md", "code", "debug/blocked")


def settings(task: dict, params: dict) -> dict:
    """The session's task, from its row (the seeds; ../benchmark/tasks.py) and agent.params (the checkout)."""
    root = params.get("aspire_root") or os.environ.get("ASPIRE_ROOT")
    if not root:
        raise ValueError("agent.params.aspire_root (or ASPIRE_ROOT): an ASPIRE checkout with its benchmark's venv")
    benchmark = str(task.get("benchmark") or "robosuite")
    return {"root": Path(os.path.expandvars(str(root))).expanduser().resolve(),
            "benchmark": benchmark, "doc": sim.profile(benchmark)["doc"], "suite": task["suite"],
            "task_id": int(task.get("task_id", 0)), "dev": str(task.get("debug_seeds") or ""),
            "unseen": str(task.get("unseen_seeds") or ""), "heldout": str(task.get("eval_seeds") or ""),
            "max_steps": params.get("max_steps")}


def prepare(task: dict, workspace: Path, params: dict, base: Path) -> list[dict]:
    s = settings(task, params)
    work = Path(workspace)
    url = os.environ.get("ASPIRE_SIM_URL")
    if not url:
        raise ValueError("ASPIRE_SIM_URL: the simulator service's URL (launch.sh starts it; environment.env)")
    # The session, registered before the agent starts: the service takes its seeds and budget from the task's row
    register(url, {"workspace": str(work.resolve()), "task": task["id"], "max_steps": s["max_steps"]})
    bin_dir = work / "bin"
    bin_dir.mkdir(exist_ok=True)
    shutil.copy2(HERE / "sim_cli.py", bin_dir / "sim_cli.py")
    (bin_dir / "sim.json").write_text(json.dumps({"workspace": str(work.resolve()), "url": url}, indent=1))
    wrapper = bin_dir / "sim"
    wrapper.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} \"$(dirname \"$0\")/sim_cli.py\" \"$@\"\n")
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    (work / "docs").mkdir(exist_ok=True)
    # The repository's worked programs are not the session's to read: the docs are the staged copies (launch.sh:
    # stage_docs.py), or, where the checkout's own pages are what the sandbox shows, the same cut made here
    staged = params.get("aspire_docs")
    pages = {name: (Path(os.path.expandvars(str(staged))).expanduser() / name).read_text()
             for name in (*sim.DOCS, s["doc"])} if staged else sim.workspace_docs(s["root"], s["benchmark"])
    for name, text in pages.items():
        (work / "docs" / name).write_text(text)
    shutil.copy2(s["root"] / SKILLS / "skills" / "task-exploration.md", work / "docs" / "task-exploration.md")
    (work / "scripts").mkdir(exist_ok=True)
    shutil.copy2(s["root"] / SKILLS / "scripts" / "scene_snapshot.py", work / "scripts" / "scene_snapshot.py")
    return []


def register(url: str, body: dict) -> None:
    request = urllib.request.Request(url.rstrip("/") + "/register", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=60) as reply:
        answer = json.loads(reply.read())
    if not answer.get("ok"):
        raise RuntimeError(f"the simulator service refused the session: {answer.get('error')}")


def task_dir(s: dict) -> str:
    """Where the session's task files go, relative to its workspace: `<benchmark>/<suite>/task_<id>` (ASPIRE's
    $TASK_DIR, with $RUN_ROOT the workspace). The scored program is fix_code.py there."""
    return f"{s['benchmark']}/{s['suite']}/task_{s['task_id']}"


def sim_seeds(text: str) -> list[int]:
    return sim.seeds_of(text)


def answer(last_message: str, task: dict) -> dict:
    return {"answer": last_message.strip()[-4000:]}


def collect(task: dict, workspace: Path, log: Path, params: dict) -> dict:
    """The session's files into the log and the facts for the judge, which runs the scored program on the held-out
    seeds, on the host."""
    s = settings(task, params)
    work = Path(workspace) / task_dir(s)
    facts = collect_files(task, work, log, KEEP)
    program = work / "fix_code.py"
    facts.update({"benchmark": s["benchmark"], "suite": s["suite"], "workspace": str(Path(workspace).resolve()),
                  "max_steps": s["max_steps"],
                  "scored_program": str(program.relative_to(workspace)) if program.is_file() else None,
                  "fix_code": program.read_text(errors="replace") if program.is_file() else None})
    if params.get("prune_media", True):
        prune(Path(workspace).parent)
    return facts


def collect_files(task: dict, work: Path, log: Path, keep) -> dict:
    """The files of `keep` (under `work`) copied into the log's files/, and the facts every session's record has:
    its notes (findings.md) and its unseen check."""
    files = Path(log) / "files"
    for name in keep:
        source = work / name
        if source.is_dir():
            shutil.copytree(source, files / name, dirs_exist_ok=True)
        elif source.is_file():
            files.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, files / name)
    findings = work / "findings.md"
    return {"findings": findings.read_text(errors="replace") if findings.is_file() else None, "unseen": unseen(work)}


def prune(run_dir: Path) -> None:
    """The heavy part of a finished session's simulator runs: every run's media/ (keyframes, overlays, videos; a
    session runs hundreds of episodes). The records, traces and programs stay."""
    for media in run_dir.rglob("media"):
        if media.is_dir() and (media.parent / "run.json").is_file():
            shutil.rmtree(media, ignore_errors=True)


def unseen(work: Path) -> dict | None:
    """The unseen check a session ran into `<work>/debug/holdout`: how many of its episodes succeeded."""
    rows = work / "debug" / "holdout" / "results.jsonl"
    if not rows.is_file():
        return None
    results = [json.loads(l) for l in rows.read_text().splitlines() if l.strip()]
    passed = sum(bool((sim._read_json(Path(r["run_dir"]) / "outcome.json") or {}).get("task_success"))
                 for r in results if r.get("run_dir"))
    return {"passed": passed, "total": len(results)}
