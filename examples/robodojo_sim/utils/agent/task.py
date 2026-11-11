"""The task functions of the Codex actor (agent.base: codex) on RoboDojo-Sim: the session writes `policy.py`, a
program that controls the robot, and develops it against the simulator, which runs on the host as the simulator
service (dojo_service.py) and which the session reaches only through `bin/sim`.

    prepare  the workspace: bin/sim and its registration with the service (the workspace and the task's id: the
             service takes the layouts from its own copy of the task row), docs/interface.md
    prompt   prompts/actor.md for the task
    answer   the agent's last message
    collect  the program and the notes into the log and the record's facts; the judge scores the program on the
             held-out layout sets, on the host

The service's URL comes from ROBODOJO_SIM_URL (environment.env).
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
KEEP = ("policy.py", "notes.md")


def prepare(task: dict, workspace: Path, params: dict, base: Path) -> list[dict]:
    work = Path(workspace)
    url = os.environ.get("ROBODOJO_SIM_URL")
    if not url:
        raise ValueError("ROBODOJO_SIM_URL: the simulator service's URL (launch.sh starts it; environment.env)")
    request = urllib.request.Request(url.rstrip("/") + "/register", headers={"Content-Type": "application/json"},
                                     data=json.dumps({"workspace": str(work.resolve()), "task": task["id"]}).encode())
    with urllib.request.urlopen(request, timeout=60) as reply:
        answer = json.loads(reply.read())
    if not answer.get("ok"):
        raise RuntimeError(f"the simulator service refused the session: {answer.get('error')}")
    bin_dir = work / "bin"
    bin_dir.mkdir(exist_ok=True)
    shutil.copy2(HERE / "sim_cli.py", bin_dir / "sim_cli.py")
    (bin_dir / "sim.json").write_text(json.dumps({"workspace": str(work.resolve()), "url": url}, indent=1))
    wrapper = bin_dir / "sim"
    wrapper.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} \"$(dirname \"$0\")/sim_cli.py\" \"$@\"\n")
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    (work / "docs").mkdir(exist_ok=True)
    shutil.copy2(HERE / "docs" / "interface.md", work / "docs" / "interface.md")
    return []


def prompt(task: dict, params: dict, base: Path) -> str:
    lo, _, hi = str(task.get("debug_seeds") or "0").partition("-")
    return (HERE / "prompts" / "actor.md").read_text().format(
        task=task["suite"], dimension=str(task.get("dimension") or "").replace("_", " "),
        dev=int(hi or lo) - int(lo) + 1,
        library="What earlier tasks of this benchmark taught is in your AGENTS.md; use it.")


def answer(last_message: str, task: dict) -> dict:
    return {"answer": last_message.strip()[-4000:]}


def collect(task: dict, workspace: Path, log: Path, params: dict) -> dict:
    work = Path(workspace)
    files = Path(log) / "files"
    for name in KEEP:
        if (work / name).is_file() and not (work / name).is_symlink():
            files.mkdir(parents=True, exist_ok=True)
            shutil.copy2(work / name, files / name)
    read = lambda name: (files / name).read_text(errors="replace") if (files / name).is_file() else None
    return {"task": task["suite"], "program": read("policy.py"), "notes": read("notes.md"),
            "development": _development(work)}


def _development(work: Path) -> list[dict]:
    """The session's own evaluations as its workspace states them (for the learner, not for the score: a session
    can write these files itself): where, how many layouts, how many succeeded."""
    runs = []
    for path in sorted(p for p in work.rglob("records.json") if p.is_file() and not p.is_symlink()):
        try:                                # the session's own files: read as data, whatever they hold
            data = json.loads(path.read_text(errors="replace"))
            records = [r for r in data["records"] if isinstance(r, dict)]
            runs.append({"dir": str(path.parent.relative_to(work)), "layouts": int(data["layouts"]),
                         "passed": sum(bool(r.get("task_success")) for r in records), "scored": len(records)})
        except Exception:                   # noqa: BLE001 - not one of bin/sim's records
            continue
    return runs[-20:]
