"""The benchmark side of RRSI Adapter: upstream's `rrsi.domain.Domain`, filled from the Trainer's learning material.

Upstream's core never reads a trajectory format or a benchmark directory itself: a domain module supplies the task
sets, the scores, the traces, the gates and the texts (briefs, constitution, critic patterns, component signals).
Here the Trainer runs the evaluations (each step's rollouts are the evaluation of the candidate handed back last),
so a domain is what remains: how a learning-material record becomes a trial (`trial`), how a rollout renders as a
trace (`render_trace`), which trials are missing rather than failed (`missing`), the smoke tasks, the guards, and the
texts. An example subclasses `AdapterDomain` in its own `domain.py` and names it in `evolver.params.domain`.

`texts` is the example's directory holding `SKILL.md` and `PATTERNS.md` (the constitution the proposer follows,
adapted from upstream's domain of the same kind) and `briefs.json` ({analyst, digester, proposer, critic}: the
domain paragraphs of the four roles).
"""
from __future__ import annotations

import json
from pathlib import Path
import re

from rrsi.domain import Domain
from rrsi.evaluate import TaskResult, aggregate

from hermit.agent.evolver.methods.rrsi_adapter import render as render_mod
from hermit.isolate import _safe

# Diff signals for the component tags of an adapter (rrsi.components: a declared tag is kept only when the diff
# carries evidence of it; otherwise the first component whose signals match, then upstream's generic signals).
ADAPTER_SIGNALS = [
    ("memory",          [r"(?m)^(?:\+\+\+|---) [ab]/(?:.*/)?(?:memory|memories|lessons|notes)(?:/|\.\w+$)",
                         r"(?mi)^(?:\+\+\+|---) [ab]/(?:.*/)?(?:MEMORY|NOTES|LESSONS)\.md$"]),
    ("skill",           [r"(?m)^(?:\+\+\+|---) [ab]/(?:.*/)?skills?/", r"(?m)^(?:\+\+\+|---) [ab]/(?:.*/)?SKILL\.md$"]),
    ("subagent",        [r"(?m)^(?:\+\+\+|---) [ab]/(?:.*/)?(?:sub_?agents?|agents|roles)/"]),
    ("context_mgmt",    [r"\"(?:SessionStart|PreCompact|PostCompact)\"\s*:"]),
    ("output_plumbing", [r"\"PostToolUse\"\s*:"]),
    ("control_flow",    [r"\"(?:PreToolUse|Stop|SubagentStop|UserPromptSubmit)\"\s*:",
                         r"(?m)^(?:\+\+\+|---) [ab]/(?:.*/)?hooks?(?:/|\.json$)"]),
    ("client_tool",     [r"(?m)^(?:\+\+\+|---) [ab]/(?:.*/)?(?:scripts?|tools?|bin)/",
                         r"(?m)^(?:\+\+\+|---) [ab]/.*\.(?:py|sh|js|ts|rb|pl)$"]),
    ("config",          [r"(?m)^[+-]\s*[A-Z][A-Z0-9_]{2,}\s*[:=]\s*[\w\"'.-]",
                         r"(?m)^(?:\+\+\+|---) [ab]/(?!(?:.*/)?hooks?\.json$).*\.(?:toml|ya?ml|ini|cfg|json)$"]),
    # last, as upstream's domains list theirs: prose files are evidence of a prompt edit, so that a candidate's prose
    # edit keeps its tag beside another edit's helper (normalize reads the candidate's whole diff for each edit)
    ("prompt",          [r"(?m)^(?:\+\+\+|---) [ab]/.*\.(?:md|txt)$"]),
]

# What each tag needs in the diff, for the proposer (the same rules as ADAPTER_SIGNALS, in words).
ADAPTER_EVIDENCE = (
    "memory = a file under memory/, notes/ or lessons/, or a MEMORY.md, NOTES.md or LESSONS.md; skill = a file under a "
    "skills/ directory or a SKILL.md; subagent = a file under sub_agents/, agents/ or roles/ (or the word 'subagent'); "
    "context_mgmt = a SessionStart, PreCompact or PostCompact hook entry; output_plumbing = a PostToolUse hook entry; "
    "control_flow = a PreToolUse, Stop, SubagentStop or UserPromptSubmit hook entry, or a hooks file; client_tool = a "
    "file under scripts/, tools/ or bin/, or a .py/.sh/.js/.ts/.rb/.pl file; config = an UPPER_CASE constant line, or a "
    ".toml/.yaml/.ini/.cfg/.json settings file; prompt = a .md or .txt file, or anything else (prose, wherever it is).")


def task_of(row: str) -> str:
    """The task a rollout row belongs to: `<task>#<k>` for the k-th rollout of a task, else the row itself."""
    return row.rsplit("#", 1)[0] if re.fullmatch(r".+#\d+", row) else row


class AdapterDomain(Domain):
    name = "adapter"
    harness_path = "."
    source_exts = {".md", ".py", ".sh", ".json", ".toml", ".yaml", ".yml", ".txt"}
    critic_patterns: list = []
    component_signals: list = ADAPTER_SIGNALS
    smoke_tasks: list = []           # task ids (rows) the liveness smoke runs, one rollout each
    texts: Path | None = None        # SKILL.md, PATTERNS.md, briefs.json
    carriers: list | None = None     # the components of K this benchmark's agent can carry (None: all of K); the
                                     # others are never offered as unexplored (a reserved slot could not be met)
    evidence: str = ADAPTER_EVIDENCE  # what each tag needs in the diff, in words, for the proposer

    def __init__(self):
        if self.texts is not None:
            briefs = json.loads((Path(self.texts) / "briefs.json").read_text())
            self.briefs = {k: str(briefs[k]) for k in ("analyst", "digester", "proposer", "critic")}

    # ---- texts -------------------------------------------------------------
    def constitution(self, root=None) -> tuple[str, str]:
        d = Path(self.texts)
        return (d / "SKILL.md").read_text(), (d / "PATTERNS.md").read_text()

    # ---- task sets ---------------------------------------------------------
    def smoke_ids(self, incumbent_per_task: dict | None = None) -> list[str]:
        return list(self.smoke_tasks)

    # ---- trials from the learning material ------------------------------------
    def missing(self, record: dict) -> bool:
        """A trial that never ran (the environment or the infrastructure failed), counted as missing; a run that
        ran and lost (a wrong answer, a timeout) is a failure with reward 0."""
        return record.get("status") not in (None, "ok")

    def halted_rows(self, material: Path) -> list[str]:
        """Rows of that material the benchmark refused to run for a reason outside the candidate (a model plan's
        usage limit): such an evaluation is never credited. Default: none."""
        return []

    def reward(self, record: dict) -> float:
        return float((record.get("judge") or {}).get("score") or 0.0)

    def tokens(self, record: dict) -> int | None:
        value = record.get("tokens")
        return int(value) if isinstance(value, (int, float)) and value > 0 else None

    def smoke_failure(self, result: dict) -> bool:
        """Whether a smoke rollout (run_agent's result: id, status, output, judge) shows the candidate broken
        although the pipeline returned: upstream fails its smoke on any trial that ended with an exception
        (timeouts included). Default: an output that carries a status (Harbor's) other than ok, or a rollout
        `run_agent` reports as timed out."""
        out = result.get("output") if isinstance(result.get("output"), dict) else {}
        return ("status" in out and out.get("status") != "ok") or bool(result.get("timed_out"))

    def halted_result(self, result: dict) -> bool:
        """Whether a smoke rollout was refused for a reason outside the candidate (a plan's usage limit): the
        smoke is then not judged at all. Default: never."""
        return False

    def grading(self, record: dict, trajectory: Path | None) -> list[str]:
        """Extra lines for the trace's GRADING block (a verifier's output)."""
        return []

    def evaluation(self, material: Path, job: str, k: int, ids: list[str] | None = None):
        """An evaluation (upstream's EvalResult) from one step's learning material: the rows of a task grouped
        into its k trials, in row order; a task with fewer rows than k is padded with missing trials. With `ids`,
        only those tasks (and every one of them, missing when absent)."""
        rows: dict[str, list[tuple[str, dict]]] = {}
        for path in sorted((material / "tasks").glob("*.json")):
            record = json.loads(path.read_text())
            row = str(record.get("id") or path.stem)
            rows.setdefault(task_of(row), []).append((row, record))
        per_task = {}
        for task in (ids if ids is not None else sorted(rows)):
            trials = sorted(rows.get(task, []), key=lambda x: x[0])[:k]
            rewards, tokens, names, missing = [], [], [], 0
            for row, record in trials:
                names.append(row)
                if self.missing(record):
                    missing += 1
                    rewards.append(0.0)
                    tokens.append(None)
                else:
                    rewards.append(self.reward(record))
                    tokens.append(self.tokens(record))
            pad = k - len(trials)
            per_task[task] = TaskResult(rewards=rewards + [0.0] * pad, tokens=tokens + [None] * pad,
                                        missing=missing + pad, extra={"rows": names + [None] * pad,
                                                                      "material": material.name})
        passes = sum(1 for t in per_task.values() for r in t.rewards if r >= 1.0)
        n = len(per_task) * k
        return aggregate(job, k, per_task, {"total_passes": passes, "n_trials": n,
                                            "pass_rate": passes / max(1, n), "material": material.name})

    def trace(self, material: Path, task: str, trial: int, tr: TaskResult) -> dict | None:
        """The rollout behind trial `trial` of `task` in that material, as `render_trace` takes it."""
        rows = (tr.extra or {}).get("rows") or []
        row = rows[trial] if trial < len(rows) else None
        if row is None:
            return None
        path = material / "tasks" / f"{_safe(row)}.json"
        if not path.is_file():
            hits = [p for p in (material / "tasks").glob("*.json") if json.loads(p.read_text()).get("id") == row]
            if not hits:
                return None
            path = hits[0]
        record = json.loads(path.read_text())
        trajectory = material / record["trajectory"] if record.get("trajectory") else None
        return {"task_id": task, "trial": trial + 1, "row": row, "record": record,
                "trajectory": str(trajectory) if trajectory else None,
                "grading": self.grading(record, trajectory), "reward": tr.rewards[trial] if trial < len(tr.rewards) else None}

    # ---- upstream's evidence hooks -------------------------------------------
    def render_trace(self, rec, detail: bool = False) -> str:
        return render_mod.render(rec, detail=detail)

    def task_row(self, task_id: str, rec, tr) -> str:
        record = rec.get("record") or {}
        return (f"{task_id} | rewards={tr.rewards if tr else '?'} | rendered_trial_reward={rec.get('reward')} | "
                f"status={record.get('status')} | tokens={record.get('tokens')}")

    # ---- the critic's own denylist ----------------------------------------------
    def leak_patterns(self, records: list[dict]) -> list:
        """Patterns built from what the run has seen (for example the answers a judge's feedback reveals), added
        to `critic_patterns` before each review. Default: none."""
        return []

    def screen(self, adapter: Path) -> list[str]:
        """Problems in the candidate as it would be stored (`adapter`, the staged copy), refused before the critic's
        review as its precheck refuses a pattern, for what a pattern over the diff cannot judge. Default: none."""
        return []
