"""ASPIRE's acting sessions (agent.base: codex): its fix-loop subagent, its evolutionary search and its skill
verifier, as ASPIRE's own prompts run them, on any benchmark of the cap-harness engine. These are the task functions
an ASPIRE config names (agent.params.prompt, prepare, answer, collect); they build on the engine's own
(agent.params.engine: its utils/agent/task.py, e.g. examples/cap_harness/utils/agent/task.py), which give every
method's session its workspace, `bin/sim` and the facts the judge reads. They run in the task sandbox with the
session.

    prepare  the engine's workspace, then ASPIRE's: task.md (the assignment), the search's procedure page, and for a
             verifier the proposals under test as proposed_skills.md
    prompt   the adaptation page (prompts/codex_header.md), the assignment, then ASPIRE's template verbatim; for an
             actor, Part 2: the evolutionary search (prompts/evosearch_header.md and its template)
    answer   the agent's last message (the record's output is the facts collect returns)
    collect  the engine's facts, and ASPIRE's: findings, skill report, the verifier's report, the search's; the
             selected program (the search's best where it ran, else fix_code.py) is the one the judge scores, and a
             verifier's program only with its own verdict `verified`

A task's role is its row's: `verifier` for the verifier's rows, which ASPIRE derives from a benchmark's tasks
(rows.py); an actor otherwise (a rerun is an actor with the final library). ASPIRE's checkout comes from
agent.params.aspire_root (its prompt templates must be declared readable: environment.read).
"""
from __future__ import annotations

from functools import lru_cache
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys

HERE = Path(__file__).resolve().parent
PROMPTS = HERE / "prompts"
TEMPLATES = {"actor": "subagent-prompt.md", "verifier": "verifier-prompt.md"}
SKILLS = Path(".claude/skills/iterative-debugging")
EVOSEARCH = Path(".claude/skills/evosearch")
# The files of ASPIRE's checkout its sessions read beyond the engine's (environment.read): its prompt templates and
# the search's procedure
ASPIRE_FILES = [str(SKILLS / "subagent-prompt.md"), str(SKILLS / "verifier-prompt.md"),
                str(EVOSEARCH / "subagent-prompt.md"), str(EVOSEARCH / "skills" / "evosearch-iteration.md")]
# ASPIRE's files the log keeps beyond the engine's
KEEP = ("findings_report.md", "skill_report.json", "verification_report.json", "verification.md")


@lru_cache(maxsize=None)
def _load(path: str):
    module_path = Path(path)
    if str(module_path.parent) not in sys.path:
        sys.path.insert(0, str(module_path.parent))
    spec = importlib.util.spec_from_file_location(f"_cap_engine_{abs(hash(path))}", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def engine(params: dict, base: Path):
    """The engine's task functions: agent.params.engine, a path relative to the config's folder."""
    path = params.get("engine")
    if not path:
        raise ValueError("agent.params.engine: the engine's task functions (e.g. "
                         "../../cap_harness/utils/agent/task.py)")
    return _load(str((Path(base) / os.path.expandvars(str(path))).resolve()))


def settings(task: dict, params: dict, base: Path) -> dict:
    """The engine's settings of the task, with ASPIRE's role."""
    role = "verifier" if task.get("role") == "verifier" else "actor"     # a rerun is an actor with the final library
    return {**engine(params, base).settings(task, params), "role": role}


def task_dir(s: dict, eng) -> str:
    """The actor's $TASK_DIR as ASPIRE's template defines it, with $RUN_ROOT the workspace; the verifier's is `.`."""
    return eng.task_dir(s) if s["role"] == "actor" else "."


def evo_dir(s: dict) -> str:
    """The evolutionary search's $TASK_DIR, as its template defines it, with EVOSEARCH_DIR `evosearch`."""
    return f"evosearch/{s['benchmark']}/{s['suite']}/task_{s['task_id']}"


def assignment(s: dict, eng) -> str:
    if s["role"] == "verifier":
        return (f"## Your assignment\n\n- BENCHMARK: `{s['benchmark']}`\n- SUITE: `{s['suite']}`\n"
                f"- TASK_ID: `{s['task_id']}`\n- GPU: handled by `bin/sim`\n- VERIFY_DIR: `.` (this workspace)\n"
                f"- Development seeds: **{s['dev']}** — wherever the template says 51, 52, 53, 54, 55 (or 51-55, or "
                f"seed 51), use these.\n- The proposals under test: `proposed_skills.md` in this workspace.\n")
    return (f"## Task Assignment\n\nBENCHMARK: {s['benchmark']}\nSUITE:     {s['suite']}\nTASK_ID:   {s['task_id']}\n"
            f"GPU:       handled by bin/sim\nRUN_ROOT:  . (this workspace)\n\n"
            f"MAX_STEPS: {s['max_steps'] or 1000}\nDEV_SEEDS: {s['dev']}\nUNSEEN_SEEDS: {s['unseen'] or 'none'}\n"
            f"HELDOUT_COUNT: {len(eng.sim_seeds(s['heldout']))} (coordinator-owned; you never run them)\n")


def prompt(task: dict, params: dict, base: Path) -> str:
    eng = engine(params, base)
    s = settings(task, params, base)
    template = (s["root"] / SKILLS / TEMPLATES[s["role"]]).read_text()
    body = template.split("<!-- ==================== TEMPLATE START ==================== -->", 1)[1]
    body = body.split("<!-- ==================== TEMPLATE END ==================== -->", 1)[0].strip()
    if s["role"] == "actor":
        body = body.split("\n---\n", 1)[1] if body.startswith("## Task Assignment") else body   # ours replaces it
    header = (PROMPTS / "codex_header.md").read_text().format(
        role_name="fix-loop subagent" if s["role"] == "actor" else "skill verifier",
        template=TEMPLATES[s["role"]], benchmark=s["benchmark"], benchmark_doc=s["doc"],
        your_dir=(f"`$RUN_ROOT` is this workspace (`.`), so `$TASK_DIR` is `{task_dir(s, eng)}`, as the template "
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
    text = f"{header}\n\n{assignment(s, eng)}\n---\n\n{body}\n"
    if s["role"] == "actor" and params.get("evosearch", True):
        text += "\n---\n\n" + evosearch_part(s, eng)
    return text


def evosearch_part(s: dict, eng) -> str:
    """Part 2 of the actor's session: ASPIRE's evolutionary-search subagent (§2.3), for a task the fix loop left below
    90% of its development seeds. Its template is the fenced block of evosearch/subagent-prompt.md, verbatim after
    its assignment block, which the page before it replaces."""
    template = (s["root"] / EVOSEARCH / "subagent-prompt.md").read_text()
    body = template[template.index("## Task Assignment"):template.rindex("\n```")]
    body = body.split("\n---\n", 1)[1].strip()
    header = (PROMPTS / "evosearch_header.md").read_text().format(
        benchmark=s["benchmark"], suite=s["suite"], task_id=s["task_id"], dev=s["dev"], task_dir=task_dir(s, eng),
        evo_dir=evo_dir(s))
    return f"{header}\n---\n\n{body}\n"


def prepare(task: dict, workspace: Path, params: dict, base: Path) -> list[dict]:
    eng = engine(params, base)
    s = settings(task, params, base)
    mounts = eng.prepare(task, workspace, params, base)
    work = Path(workspace)
    if params.get("evosearch", True):                   # the search's procedure, for a prompt that runs it
        shutil.copy2(s["root"] / EVOSEARCH / "skills" / "evosearch-iteration.md",
                     work / "docs" / "evosearch-iteration.md")
    if s["role"] == "verifier":
        # The coordinator stages the proposals under test as the adapter's skills/proposed.md: the verifier's
        # proposed_skills.md, kept out of the library it reads (interface.py leaves the file out).
        staged = sorted(work.glob("hermit/*/skills/proposed.md"))
        (work / "proposed_skills.md").write_text(staged[0].read_text() if staged else "(no proposals were staged)\n")
    (work / "task.md").write_text(assignment(s, eng))
    return mounts


def answer(last_message: str, task: dict) -> dict:
    return {"answer": last_message.strip()[-4000:]}


def collect(task: dict, workspace: Path, log: Path, params: dict, base: Path) -> dict:
    """The engine's files and facts, and ASPIRE's: what the judge and the coordinator read."""
    eng = engine(params, base)
    s = settings(task, params, base)
    work = Path(workspace) / task_dir(s, eng)
    facts = eng.collect_files(task, work, log, (*eng.KEEP, *KEEP))
    files = Path(log) / "files"
    read = lambda name: (work / name).read_text(errors="replace") if (work / name).is_file() else None
    facts = {"role": s["role"], "benchmark": s["benchmark"], "suite": s["suite"],
             "findings": facts["findings"] or read("findings_report.md"),
             "skill_report": _json(read("skill_report.json")), "verification": _json(read("verification_report.json")),
             "unseen": facts["unseen"], "workspace": str(Path(workspace).resolve()), "max_steps": s["max_steps"]}
    evo = Path(workspace) / evo_dir(s)
    search = s["role"] == "actor" and params.get("evosearch", True)     # a prompt that runs the search
    if search and evo.is_dir():
        for name in ("evosearch_best_code.py", "findings.md", "task_analysis.md", "BLOCKED"):
            if (evo / name).is_file():
                (files / "evosearch").mkdir(parents=True, exist_ok=True)
                shutil.copy2(evo / name, files / "evosearch" / name)
        facts["evosearch"] = {"findings": (evo / "findings.md").read_text(errors="replace")
                              if (evo / "findings.md").is_file() else None,
                              "best_code": (evo / "evosearch_best_code.py").is_file()}
    # The selected program: the search's best when the search ran (it falls back to fix_code.py itself), else the fix
    # loop's; with `evosearch: false`, fix_code.py alone. The judge scores it.
    best = evo / "evosearch_best_code.py"
    program = best if search and best.is_file() else work / "fix_code.py"
    facts["scored_program"] = str(program.relative_to(workspace)) if program.is_file() else None
    facts["fix_code"] = program.read_text(errors="replace") if program.is_file() else None
    # A verifier's program counts only with its own verdict `verified` (ASPIRE: `refuted` and `void` are never
    # promoted): without it, nothing is scored.
    if s["role"] == "verifier" and (facts["verification"] or {}).get("verdict") != "verified":
        facts["not_scored"] = f"verifier verdict {(facts['verification'] or {}).get('verdict') or 'missing'}"
        facts["scored_program"] = facts["fix_code"] = None
    if params.get("prune_media", True):
        eng.prune(Path(workspace).parent)
    return facts


def _json(text):
    try:
        return json.loads(text) if text else None
    except ValueError:
        return {"unparsed": text[-4000:]}
