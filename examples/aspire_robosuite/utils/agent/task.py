"""The task functions of the Codex actor (agent.base: codex): ASPIRE's fix-loop subagent and its skill verifier, as
ASPIRE's own prompts run them. They run in the task sandbox with the session; the robot execution engine runs on the
host, as the simulator service (sim_service.py), which the session reaches only through `bin/sim`.

    prepare  the workspace: bin/sim and its registration with the service (the workspace and the task's id: the
             service takes the seeds and the budget from its own copy of the task row), the API and artifact docs, the scene-snapshot program, task.md; for a verifier, the
             proposals under test as proposed_skills.md
    prompt   the adaptation page (prompts/codex_header.md), the assignment, then ASPIRE's template verbatim
    answer   the agent's last message (the record's output is the facts collect returns)
    collect  the actor's files into the log, and the facts the judge and the coordinator read: findings, skill report,
             the selected program (fix_code.py, or the evolutionary search's best); the judge runs Stage 2 on it

ASPIRE's checkout comes from agent.params.aspire_root (its prompt templates, docs and snapshot program must be
declared readable: environment.read); the service's URL from ASPIRE_SIM_URL (environment.env). A task row carries the
seeds: development, unseen-check and held-out (utils/prepare.py).
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

TEMPLATES = {"actor": "subagent-prompt.md", "verifier": "verifier-prompt.md"}
SKILLS = Path(".claude/skills/iterative-debugging")
EVOSEARCH = Path(".claude/skills/evosearch")
DOCS = ("api-reference.md", "run-artifacts.md")           # and the benchmark's own page (sim.PROFILES: `doc`)
# The files of ASPIRE's checkout the task sandbox shows (environment.read): its prompts, docs and snapshot program;
# not its shipped library, worked solutions or examples
ASPIRE_FILES = [*(f"docs/{d}" for d in (*DOCS, *(p["doc"] for p in sim.PROFILES.values()))), str(SKILLS / "subagent-prompt.md"), str(SKILLS / "verifier-prompt.md"),
                str(SKILLS / "scripts" / "scene_snapshot.py"), str(SKILLS / "skills" / "task-exploration.md"),
                str(EVOSEARCH / "subagent-prompt.md"), str(EVOSEARCH / "skills" / "evosearch-iteration.md")]
# The workspace files the log keeps (the run directories stay in the workspace)
KEEP = ("task_analysis.md", "fix_code.py", "findings.md", "findings_report.md", "skill_report.json",
        "verification_report.json", "verification.md", "code", "debug/blocked")


def settings(task: dict, params: dict) -> dict:
    """The session's assignment, from its task row (the seeds; utils/prepare.py) and agent.params (the checkout)."""
    role = "verifier" if task.get("role") == "verifier" else "actor"     # a rerun is an actor with the final library
    root = params.get("aspire_root") or os.environ.get("ASPIRE_ROOT")
    if not root:
        raise ValueError("agent.params.aspire_root (or ASPIRE_ROOT): an ASPIRE checkout with .venv-robosuite")
    benchmark = str(task.get("benchmark") or "robosuite")
    return {"role": role, "root": Path(os.path.expandvars(str(root))).expanduser().resolve(),
            "benchmark": benchmark, "doc": sim.profile(benchmark)["doc"], "suite": task["suite"], "task_id": int(task.get("task_id", 0)),
            "dev": str(task.get("debug_seeds") or ""), "unseen": str(task.get("unseen_seeds") or ""),
            "heldout": str(task.get("eval_seeds") or ""), "max_steps": params.get("max_steps")}


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
    cut = sim.profile(s["benchmark"])["cut"]
    for name in (*DOCS, s["doc"]):
        # the repository's worked programs are not the actor's to read: drop the lines that name them, and the
        # benchmark page's section that lists them per task
        text = (s["root"] / "docs" / name).read_text()
        if name == s["doc"] and cut and f"\n{cut}" in text:
            text = text.split(f"\n{cut}", 1)[0] + "\n"
        (work / "docs" / name).write_text("".join(line for line in text.splitlines(keepends=True)
                                                  if "examples/" not in line))
    shutil.copy2(s["root"] / SKILLS / "skills" / "task-exploration.md", work / "docs" / "task-exploration.md")
    shutil.copy2(s["root"] / EVOSEARCH / "skills" / "evosearch-iteration.md", work / "docs" / "evosearch-iteration.md")
    (work / "scripts").mkdir(exist_ok=True)
    shutil.copy2(s["root"] / SKILLS / "scripts" / "scene_snapshot.py", work / "scripts" / "scene_snapshot.py")
    if s["role"] == "verifier":
        # The coordinator stages the proposals under test as the adapter's skills/proposed.md: the verifier's
        # proposed_skills.md, kept out of the library it reads (interface.py leaves the file out).
        staged = sorted(work.glob("hermit/*/skills/proposed.md"))
        (work / "proposed_skills.md").write_text(staged[0].read_text() if staged else "(no proposals were staged)\n")
    (work / "task.md").write_text(assignment(s))
    return []


def register(url: str, body: dict) -> None:
    request = urllib.request.Request(url.rstrip("/") + "/register", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=60) as reply:
        answer = json.loads(reply.read())
    if not answer.get("ok"):
        raise RuntimeError(f"the simulator service refused the session: {answer.get('error')}")


def task_dir(s: dict) -> str:
    """The actor's $TASK_DIR as ASPIRE's template defines it, with $RUN_ROOT the workspace; the verifier's is `.`."""
    return f"{s['benchmark']}/{s['suite']}/task_{s['task_id']}" if s["role"] == "actor" else "."


def sim_seeds(text: str) -> list[int]:
    return sim.seeds_of(text) if text else []


def assignment(s: dict) -> str:
    if s["role"] == "verifier":
        return (f"## Your assignment\n\n- BENCHMARK: `{s['benchmark']}`\n- SUITE: `{s['suite']}`\n"
                f"- TASK_ID: `{s['task_id']}`\n- GPU: handled by `bin/sim`\n- VERIFY_DIR: `.` (this workspace)\n"
                f"- Development seeds: **{s['dev']}** — wherever the template says 51, 52, 53, 54, 55 (or 51-55, or "
                f"seed 51), use these.\n- The proposals under test: `proposed_skills.md` in this workspace.\n")
    return (f"## Task Assignment\n\nBENCHMARK: {s['benchmark']}\nSUITE:     {s['suite']}\nTASK_ID:   {s['task_id']}\n"
            f"GPU:       handled by bin/sim\nRUN_ROOT:  . (this workspace)\n\n"
            f"MAX_STEPS: {s['max_steps'] or 1000}\nDEV_SEEDS: {s['dev']}\nUNSEEN_SEEDS: {s['unseen'] or 'none'}\n"
            f"HELDOUT_COUNT: {len(sim_seeds(s['heldout']))} (coordinator-owned; you never run them)\n")


def prompt(task: dict, params: dict, base: Path) -> str:
    s = settings(task, params)
    template = (s["root"] / SKILLS / TEMPLATES[s["role"]]).read_text()
    body = template.split("<!-- ==================== TEMPLATE START ==================== -->", 1)[1]
    body = body.split("<!-- ==================== TEMPLATE END ==================== -->", 1)[0].strip()
    if s["role"] == "actor":
        body = body.split("\n---\n", 1)[1] if body.startswith("## Task Assignment") else body   # ours replaces it
    header = (HERE / "prompts" / "codex_header.md").read_text().format(
        role_name="fix-loop subagent" if s["role"] == "actor" else "skill verifier",
        template=TEMPLATES[s["role"]], benchmark=s["benchmark"], benchmark_doc=s["doc"],
        your_dir=(f"`$RUN_ROOT` is this workspace (`.`), so `$TASK_DIR` is `{task_dir(s)}`, as the template "
                  f"defines it." if s["role"] == "actor" else "`$VERIFY_DIR` is this workspace (`.`)."),
        proposed_line=("The proposals under test are `proposed_skills.md` in this workspace (the template's "
                       "`$VERIFY_DIR/proposed_skills.md`); they are not in the library." if s["role"] == "verifier"
                       else ""),
        finish_line=("only after both parts of this session: Part 1, the template below, and Part 2 after it, the "
                     "evolutionary search, which runs only when Part 1 leaves `fix_code.py` below 90% of the "
                     "development seeds. Reply with the return message of each part you ran."
                     if s["role"] == "actor" and params.get("evosearch", True) else
                     "by replying with the template's return message."),
        heldout_line=("is not yours: the harness scores your selected program on the held-out seeds after you "
                      "finish (the template's Stage 2)." if s["role"] == "actor" else
                      "does not apply: your verdict is scored on the program you select, on your seeds; write it "
                      "as `fix_code.py` in this workspace as well."))
    text = f"{header}\n\n{assignment(s)}\n---\n\n{body}\n"
    if s["role"] == "actor" and params.get("evosearch", True):
        text += "\n---\n\n" + evosearch_part(s)
    return text


def evosearch_part(s: dict) -> str:
    """Part 2 of the actor's session: ASPIRE's evolutionary-search subagent (§2.3), for a task the fix loop left below
    90% of its development seeds. Its template is the fenced block of evosearch/subagent-prompt.md, verbatim after
    its assignment block, which the page before it replaces."""
    template = (s["root"] / EVOSEARCH / "subagent-prompt.md").read_text()
    body = template[template.index("## Task Assignment"):template.rindex("\n```")]
    body = body.split("\n---\n", 1)[1].strip()
    header = (HERE / "prompts" / "evosearch_header.md").read_text().format(
        benchmark=s["benchmark"], suite=s["suite"], task_id=s["task_id"], dev=s["dev"], task_dir=task_dir(s), evo_dir=evo_dir(s))
    return f"{header}\n---\n\n{body}\n"


def evo_dir(s: dict) -> str:
    """The evolutionary search's $TASK_DIR, as its template defines it, with EVOSEARCH_DIR `evosearch`."""
    return f"evosearch/{s['benchmark']}/{s['suite']}/task_{s['task_id']}"


def answer(last_message: str, task: dict) -> dict:
    return {"answer": last_message.strip()[-4000:]}


def collect(task: dict, workspace: Path, log: Path, params: dict) -> dict:
    """The actor's files into the log and the facts for the judge (which runs Stage 2 on `fix_code`, on the host) and
    the coordinator."""
    s = settings(task, params)
    work = Path(workspace) / task_dir(s)
    files = Path(log) / "files"
    for name in KEEP:
        source = work / name
        if source.is_dir():
            shutil.copytree(source, files / name, dirs_exist_ok=True)
        elif source.is_file():
            files.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, files / name)
    read = lambda name: (work / name).read_text(errors="replace") if (work / name).is_file() else None
    facts = {"role": s["role"], "benchmark": s["benchmark"], "suite": s["suite"], "findings": read("findings.md") or read("findings_report.md"),
             "skill_report": _json(read("skill_report.json")), "verification": _json(read("verification_report.json")),
             "unseen": _unseen(work), "workspace": str(Path(workspace).resolve()), "max_steps": s["max_steps"]}
    evo = Path(workspace) / evo_dir(s)
    if s["role"] == "actor" and evo.is_dir():
        for name in ("evosearch_best_code.py", "findings.md", "task_analysis.md", "BLOCKED"):
            if (evo / name).is_file():
                (files / "evosearch").mkdir(parents=True, exist_ok=True)
                shutil.copy2(evo / name, files / "evosearch" / name)
        facts["evosearch"] = {"findings": (evo / "findings.md").read_text(errors="replace")
                              if (evo / "findings.md").is_file() else None,
                              "best_code": (evo / "evosearch_best_code.py").is_file()}
    # The selected program: the search's best when the search ran (it falls back to fix_code.py itself), else the fix
    # loop's. The judge scores it.
    program = evo / "evosearch_best_code.py" if (evo / "evosearch_best_code.py").is_file() else work / "fix_code.py"
    facts["scored_program"] = str(program.relative_to(workspace)) if program.is_file() else None
    facts["fix_code"] = program.read_text(errors="replace") if program.is_file() else None
    if params.get("prune_media", True):
        prune(Path(workspace).parent)
    return facts


def prune(run_dir: Path) -> None:
    """The heavy part of a finished session's simulator runs: every run's media/ (keyframes, overlays, videos; a
    session runs hundreds of episodes). The records, traces and programs stay."""
    for media in run_dir.rglob("media"):
        if media.is_dir() and (media.parent / "run.json").is_file():
            shutil.rmtree(media, ignore_errors=True)


def _json(text):
    try:
        return json.loads(text) if text else None
    except ValueError:
        return {"unparsed": text[-4000:]}


def _unseen(work: Path) -> dict | None:
    rows = work / "debug" / "holdout" / "results.jsonl"
    if not rows.is_file():
        return None
    results = [json.loads(l) for l in rows.read_text().splitlines() if l.strip()]
    passed = sum(bool((sim._read_json(Path(r["run_dir"]) / "outcome.json") or {}).get("task_success"))
                 for r in results if r.get("run_dir"))
    return {"passed": passed, "total": len(results)}
