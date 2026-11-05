"""The task functions of the Codex actor (agent.base: codex): ASPIRE's fix-loop subagent and its skill verifier, as
ASPIRE's own prompts run them, in a workspace that reaches the robot execution engine only through `bin/sim`.

    prepare  the workspace: bin/sim (the engine, held to the assigned seeds), the API and artifact docs, the
             scene-snapshot program, task.md; for a verifier, the proposals under test as proposed_skills.md
    prompt   the adaptation page (prompts/codex_header.md), the assignment, then ASPIRE's template verbatim
    answer   the agent's last message (the record's output is the facts collect returns)
    collect  the actor's files into the log, then the coordinator's Stage 2: fix_code.py once on the held-out seeds
             (the verifier's selected program on its own seeds); the facts the judge and the coordinator read

ASPIRE's checkout comes from agent.params.aspire_root (or ASPIRE_ROOT); the engine's environment from
agent.params.sim_env. A task row may carry its own seeds and role (the verifier's rows do).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "agent"))
import sim  # noqa: E402

ROW_KEYS = ("debug_seeds", "unseen_seeds", "eval_seeds", "role")
TEMPLATES = {"actor": "subagent-prompt.md", "verifier": "verifier-prompt.md"}
SKILLS = Path(".claude/skills/iterative-debugging")
EVOSEARCH = Path(".claude/skills/evosearch")
DOCS = ("api-reference.md", "run-artifacts.md", "robosuite.md")
# The workspace files the log keeps (the run directories stay in the workspace)
KEEP = ("task_analysis.md", "fix_code.py", "findings.md", "findings_report.md", "skill_report.json",
        "verification_report.json", "verification.md", "code", "debug/blocked")


def settings(task: dict, params: dict) -> dict:
    p = {**params, **{k: task[k] for k in ROW_KEYS if k in task}}
    role = "verifier" if p.get("role") == "verifier" else "actor"     # a rerun is an actor with the final library
    defaults = {"actor": ("101-125", "126-130", "1-100"), "verifier": ("101-105", "", "")}[role]
    root = p.get("aspire_root") or os.environ.get("ASPIRE_ROOT")
    if not root:
        raise ValueError("agent.params.aspire_root (or ASPIRE_ROOT): an ASPIRE checkout with .venv-robosuite")
    if role == "verifier":       # its own seeds only: no unseen check, no held-out run
        p["unseen_seeds"] = p["eval_seeds"] = ""
    return {"role": role, "root": Path(os.path.expandvars(str(root))).expanduser().resolve(),
            "suite": task["suite"], "task_id": int(task.get("task_id", 0)),
            "dev": str(p.get("debug_seeds") or defaults[0]),
            "unseen": str(p.get("unseen_seeds") if p.get("unseen_seeds") is not None else defaults[1]),
            "heldout": "" if p.get("eval_seeds") == "none" else str(p.get("eval_seeds") if p.get("eval_seeds")
                                                                     is not None else defaults[2]),
            "env": p.get("sim_env") or {}, "workers": int(p.get("workers", 4)), "max_steps": p.get("max_steps")}


def prepare(task: dict, workspace: Path, params: dict, base: Path) -> list[dict]:
    s = settings(task, params)
    work = Path(workspace)
    bin_dir = work / "bin"
    bin_dir.mkdir(exist_ok=True)
    shutil.copy2(HERE / "agent" / "sim_cli.py", bin_dir / "sim_cli.py")
    (bin_dir / "sim.json").write_text(json.dumps({
        "workspace": str(work.resolve()), "aspire_root": str(s["root"]), "suite": s["suite"], "task_id": s["task_id"],
        "dev_seeds": sim_seeds(s["dev"]), "unseen_seeds": sim_seeds(s["unseen"]), "dev_seeds_text": s["dev"],
        "unseen_seeds_text": s["unseen"], "env": s["env"], "workers": s["workers"], "max_steps": s["max_steps"]},
        indent=1))
    wrapper = bin_dir / "sim"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} \"$(dirname \"$0\")/sim_cli.py\" \"$@\"\n")
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    (work / "docs").mkdir(exist_ok=True)
    for name in DOCS:
        shutil.copy2(s["root"] / "docs" / name, work / "docs" / name)
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


def task_dir(s: dict) -> str:
    """The actor's $TASK_DIR as ASPIRE's template defines it, with $RUN_ROOT the workspace; the verifier's is `.`."""
    return f"robosuite/{s['suite']}/task_{s['task_id']}" if s["role"] == "actor" else "."


def sim_seeds(text: str) -> list[int]:
    return sim.seeds_of(text) if text else []


def assignment(s: dict) -> str:
    if s["role"] == "verifier":
        return (f"## Your assignment\n\n- BENCHMARK: `robosuite`\n- SUITE: `{s['suite']}`\n"
                f"- TASK_ID: `{s['task_id']}`\n- GPU: handled by `bin/sim`\n- VERIFY_DIR: `.` (this workspace)\n"
                f"- Development seeds: **{s['dev']}** — wherever the template says 51, 52, 53, 54, 55 (or 51-55, or "
                f"seed 51), use these.\n- The proposals under test: `proposed_skills.md` in this workspace.\n")
    return (f"## Task Assignment\n\nBENCHMARK: robosuite\nSUITE:     {s['suite']}\nTASK_ID:   {s['task_id']}\n"
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
        template=TEMPLATES[s["role"]],
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
        heldout_line=("is not yours: the harness scores `fix_code.py` on the held-out seeds after you finish "
                      "(the template's Stage 2)." if s["role"] == "actor" else
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
        suite=s["suite"], task_id=s["task_id"], dev=s["dev"], task_dir=task_dir(s), evo_dir=evo_dir(s))
    return f"{header}\n---\n\n{body}\n"


def evo_dir(s: dict) -> str:
    """The evolutionary search's $TASK_DIR, as its template defines it, with EVOSEARCH_DIR `evosearch`."""
    return f"evosearch/robosuite/{s['suite']}/task_{s['task_id']}"


def answer(last_message: str, task: dict) -> dict:
    return {"answer": last_message.strip()[-4000:]}


def collect(task: dict, workspace: Path, log: Path, params: dict) -> dict:
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
    facts = {"role": s["role"], "suite": s["suite"], "findings": read("findings.md") or read("findings_report.md"),
             "skill_report": _json(read("skill_report.json")), "fix_code": read("fix_code.py"),
             "verification": _json(read("verification_report.json")), "unseen": _unseen(work)}
    evo = Path(workspace) / evo_dir(s)
    if s["role"] == "actor" and evo.is_dir():
        for name in ("evosearch_best_code.py", "findings.md", "task_analysis.md", "BLOCKED"):
            if (evo / name).is_file():
                (files / "evosearch").mkdir(parents=True, exist_ok=True)
                shutil.copy2(evo / name, files / "evosearch" / name)
        facts["evosearch"] = {"findings": (evo / "findings.md").read_text(errors="replace")
                              if (evo / "findings.md").is_file() else None,
                              "best_code": (evo / "evosearch_best_code.py").is_file()}
    # Stage 2 scores the search's best program when the search ran (it falls back to fix_code.py itself), else
    # the fix loop's
    program = evo / "evosearch_best_code.py" if (evo / "evosearch_best_code.py").is_file() else work / "fix_code.py"
    facts["scored_program"] = str(program.relative_to(workspace)) if program.is_file() else None
    seeds = sim_seeds(s["heldout"] if s["role"] == "actor" else s["dev"])
    if program.is_file() and seeds:
        engine = sim.Engine(s["root"], env=s["env"], workers=s["workers"], max_steps=s["max_steps"],
                            timeout_s=float(params.get("sweep_timeout_s", 7200)))
        refused = engine.validate(program)
        records = [] if refused else engine.run(program, suite=s["suite"], seeds=seeds, out=work / "validation",
                                                 task_id=s["task_id"])
        (Path(log) / "stage2.json").write_text(json.dumps({"refused": refused, "records": records}, indent=1))
        facts.update(scored_seeds=seeds, scored_passed=sum(r["task_success"] for r in records),
                     scored_refused=refused)
    else:
        facts.update(scored_seeds=seeds, scored_passed=0,
                     scored_refused="no fix_code.py" if seeds else "no seeds to score")
    return facts


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
