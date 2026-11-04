"""AHEAdapterEvolver: Agentic Harness Engineering (arXiv:2604.25850; china-qijizhifeng/agentic-harness-engineering @
8b2a55d, `evolve.py`) as an Evolver on the `ahe` template, confined to the adapter (the acting agent is fixed).

On the agent layers (docs/framework/agent-layers.md) both of its agents are Layer 3 over the `codex` Base Agent: the
Evolve Agent is AgentEvolver's session (the prompt on stdin, the run's sandbox, `check_spec` as its only tool), and
the Agent Debugger is one call of the same Base Agent per attempt of each task (debugger.py), with the Evolve Agent's
settings (the login and its plan, web search, the isolation, the price) and its own model, clock and home.

AHE evolves a harness H (system prompt, tool guidance, tool implementations, middleware, skills, sub-agents, long-term
memory) by iterating: evaluate H on the benchmark with k rollouts per task, classify every task (pass / fail /
exception), diff against the previous iteration (flipped / regressed / rollout moves / infra buckets), score the
previous iteration's change manifest (EFFECTIVE ... HARMFUL), run an Agent Debugger over every task's trajectories,
then let an Evolve Agent read the analysis, edit the harness and declare its changes in a manifest the next iteration
scores. Hermit step N is AHE iteration N: step 1 scores the seed; step N's edits are scored in step N+1. The
"snapshot of iteration N" is the git commit `learning_material/step_N/summary.json["adapter_commit"]` (the Evolver's
adapter copy carries the run's git history), so a rollback is `git show <commit>:<path>` and finalize is `git
archive` of the best-ever commit.

One step (`step`):
  1. deterministic pre-step (spec B.3): the records of `learning_material/current` grouped by `params.task_field`
     (k trials per task) -> `compute_stats` -> `state/stats/step_N.json`; `update_task_history` ->
     `state/task_history.json`, `compute_task_stability`, `compute_iteration_diff` vs step N-1's stats,
     `history_before` -> `state/evolution_history.md`; attribution of manifest N-1 (`evaluate_changes`, gated on its
     `iteration == N-1`) -> `state/evaluations/step_N.json`; `update_best_ever` (strict >, with the rollouts'
     commit) -> `state/best_ever.json`.
  2. early stop: `pass_rate >= target_pass_rate` -> scores recorded, `StepResult("stop", ...)` (the run ends only
     with `trainer.allow_early_stop: true`).
  3. rollback (B.6, `rollback: auto`): the files of every change of manifest N-1 whose verdict is in
     `rollback_verdicts` restored from the adapter before that change (the rollout commit, or what step N-1's own
     rollback left there, kept as `state/rollbacks/step_{N-1}/<path>`), `ADAPTER.md` synced to a restored
     `AGENTS.md`; undone when `check_spec` then fails -> `state/rollbacks/step_N.json`. The framework's own
     `rb-*` entries are never rolled back. `rollback: agent` records that nothing was touched.
  4. the Agent Debugger (B.5): one Codex session per task over the trial's trajectory and verdict
     -> `state/analysis/step_N/{overview.md, detail/<task>.md, jobs.json, usage.json}`, one Usage tagged `debugger`.
  5. the evolution query (`build_evolution_query`, the Debugger overview bounded to OVERVIEW_MAX_BYTES) ->
     `state/queries/step_N.md`; `START.md` + the query -> `scratch/evolve_prompt.md`, given to Codex on stdin; the
     Evolve session through `AgentEvolver.step` (the `tools: [check_spec]` gate, a fresh session, `evolve_timeout_s`
     as a wall clock: the Trainer's cancel for the call, which stops the session).
  6. post-step (B.8): `state/change_manifest.json` validated (`iteration == N`, `changes[]` of objects whose
     `files`, `predicted_fixes` and `risk_tasks` are lists of strings and `description` a string) and archived to
     `state/manifests/step_N.json` (a missing or wrong manifest -> an empty archived manifest and a loud line in
     `evolution_history.md` and `report.md`; upstream silently disables attribution instead), the framework's
     rollbacks prefixed as `type: rollback` entries, the agent's report appended to the history,
     `update_iteration_scores` with the phase timing, the verdicts and the rollbacks, `ADAPTER.md` <- `AGENTS.md`,
     `state/ahe.json`. A session that failed (a non-zero exit that is not the wall clock, or an error before it
     ran) or a Trainer cancel archives an empty manifest and marks the rollback record discarded: the Trainer keeps
     the previous adapter, so nothing of the step is attributable at step N+1. The Debugger's usage is reported
     either way.

`finalize` (B.9): `final: best_ever` hands back `git archive` of `best_ever.commit` into `scratch/best_adapter`
(`output/final_selection.json`); `final: last`, or no best-ever commit, hands back the working copy.

The pure ports live in the sibling modules (analysis.py, debugger.py, rollback.py), imported inside the methods so
this module loads without them. The records handed to them are the step's `tasks/<id>.json` dicts with one extra
key, `_dir` (the step directory the record's `trajectory` path is relative to).
"""
from __future__ import annotations

import dataclasses
from datetime import datetime
import json
from pathlib import Path
import shutil
import subprocess
import time

from hermit.agent.evolver.agent import AgentEvolver
from hermit.agent.evolver.base import Context, FinalResult, StepContext, StepResult, Usage

ALWAYS_ON = "AGENTS.md"
CONTEXT = "ADAPTER.md"
MANIFEST = "change_manifest.json"
REPORT_CHARS = 2000              # characters of the agent's report appended to evolution_history.md (evolve.py:1959)
EVOLVE_PROMPT = "evolve_prompt.md"
# The Debugger overview the query embeds is bounded (the full overview stays on disk); the prompt goes to Codex on
# stdin, so its length has no argv limit.
OVERVIEW_MAX_BYTES = 40_000
STATE_DIRS = ("stats", "manifests", "evaluations", "rollbacks", "analysis", "queries")
STOPPED = "stopped by the Trainer"
LIST_FIELDS = ("files", "predicted_fixes", "risk_tasks")   # a change's fields read as lists of strings at step N+1
TEXT_FIELDS = ("description",)                             # and as a string (the query slices it)


@dataclasses.dataclass
class RoleUsage(Usage):
    """A Usage that names the role it was spent in (`debugger` or `evolve`); the Trainer records it as a dict."""
    role: str = ""


def role_usage(usage: Usage | dict, role: str) -> RoleUsage:
    u = Usage(**usage) if isinstance(usage, dict) else usage
    return RoleUsage(**{f.name: getattr(u, f.name) for f in dataclasses.fields(Usage)}, role=role)


# ---------- files ----------

def step_name(n: int) -> str:
    return f"step_{n:04d}"


def load_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def dump_json(path: Path, data) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def append_text(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)


def read_records(material: Path) -> list[dict]:
    """The step's task records (`current/tasks/*.json`), each with `_dir` = the step directory (resolved)."""
    current = (Path(material) / "current").resolve()
    out = []
    for f in sorted((current / "tasks").glob("*.json")):
        record = json.loads(f.read_text(encoding="utf-8"))
        record["_dir"] = str(current)
        out.append(record)
    return out


def infer_k(records: list[dict], task_field: str) -> int:
    """Trials per task: the largest group sharing the task field (every task has k rows; a short step has fewer)."""
    from hermit.agent.evolver.methods.ahe_adapter.analysis import task_name_of
    counts: dict[str, int] = {}
    for r in records:
        task = task_name_of(r, task_field)
        counts[task] = counts.get(task, 0) + 1
    return max(counts.values()) if counts else 1


def learnable_paths(adapter: Path) -> list:
    """The template's learnable paths from the adapter's manifest (the rollback never touches anything else)."""
    from hermit.adapter.manifest import read_manifest
    try:
        return list((read_manifest(adapter).get("structure") or {}).get("learnable") or [])
    except (OSError, ValueError):
        return []


def validate_manifest(data, step: int) -> tuple[dict | None, str]:
    """The Evolve Agent's manifest when it is this step's, else None and why (evolve.py:2222-2236 archives only when
    `iteration` matches; here the mismatch is reported instead of silently dropping attribution)."""
    if not isinstance(data, dict):
        return None, f"no {MANIFEST} (or not a JSON object)"
    if data.get("iteration") != step:
        return None, f"{MANIFEST} has iteration {data.get('iteration')!r}, this step is {step}"
    if not isinstance(data.get("changes"), list):
        return None, f"{MANIFEST} has no changes[] list"
    for i, change in enumerate(data["changes"]):
        # The next step's attribution iterates these lists, and the rollback the files: a null or a string there
        # would fail that step before either agent runs.
        if not isinstance(change, dict):
            return None, f"{MANIFEST} changes[{i}] is not an object"
        for key in LIST_FIELDS:
            value = change.get(key, [])
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                return None, f"{MANIFEST} changes[{i}].{key} is not a list of strings ({value!r:.80})"
        for key in TEXT_FIELDS:
            if not isinstance(change.get(key, ""), str):
                return None, f"{MANIFEST} changes[{i}].{key} is not a string ({change.get(key)!r:.80})"
    return data, ""


def rollbacks_done(record: dict | None) -> list[dict]:
    """The rollback record's changes that the framework acted on, as the query renders them: `{change_id, files,
    ok, error}`; a change whose files were all skipped or already the parent's is not one."""
    out = []
    for change in (record or {}).get("changes") or []:
        done = [f["path"] for f in change.get("files") or [] if f.get("action") in ("restored", "deleted", "synced")]
        undone = [f["path"] for f in change.get("files") or [] if f.get("action") == "undone"]
        if done:
            out.append({"change_id": change.get("id"), "verdict": change.get("verdict"), "files": done, "ok": True})
        elif undone:
            out.append({"change_id": change.get("id"), "verdict": change.get("verdict"), "files": undone, "ok": False,
                        "error": (record or {}).get("note") or "check_spec failed after the rollback"})
    return out


def rollback_entries(done: list[dict]) -> list[dict]:
    """The framework's rollbacks as manifest entries (`type: rollback`), prefixed to the step's manifest so the next
    step's attribution and the agent see them as changes of this step."""
    return [{"id": f"rb-{i}", "type": "rollback",
             "description": f"rolled back {r.get('change_id')} (verdict {r.get('verdict')}) by the framework, file by "
                            f"file, to the adapter before it",
             "files": list(r.get("files") or []), "failure_pattern": "", "predicted_fixes": [], "risk_tasks": [],
             "constraint_level": "", "why_this_component": "automatic rollback (params.rollback: auto)"}
            for i, r in enumerate((r for r in done if r.get("ok", True)), 1)]


def sync_context(adapter: Path) -> None:
    """ADAPTER.md, the framework's context export (what a deployment writes as the host's context file), is
    AGENTS.md."""
    source = adapter / ALWAYS_ON
    if not source.is_file():
        return
    text = source.read_text(encoding="utf-8", errors="replace")
    target = adapter / CONTEXT
    if not target.is_file() or target.read_text(encoding="utf-8", errors="replace") != text:
        target.write_text(text, encoding="utf-8")


def minutes(seconds: float) -> float:
    return round(seconds / 60, 2)


def bounded_overview(overview: str | None, overview_path: Path) -> str | None:
    """The Debugger overview as the query embeds it: cut at OVERVIEW_MAX_BYTES with a pointer to the full file."""
    if not overview or len(overview.encode("utf-8")) <= OVERVIEW_MAX_BYTES:
        return overview
    cut = overview.encode("utf-8")[:OVERVIEW_MAX_BYTES].decode("utf-8", errors="ignore")
    return cut.rsplit("\n", 1)[0] + f"\n\n[... overview truncated here: read `{overview_path}` for the rest]"


def evolve_prompt(start_md: str, query: str, scratch: Path) -> Path:
    """`scratch/evolve_prompt.md`: START.md followed by the query."""
    prompt = scratch / EVOLVE_PROMPT
    prompt.write_text(start_md + "\n\n# Evolution query\n" + query, encoding="utf-8")
    return prompt


# ---------- the Evolver ----------

class AHEAdapterEvolver(AgentEvolver):
    supports_resume = False          # a fresh Codex session per step, as run_evolve_agent builds a new agent each loop

    def setup(self, ctx: Context) -> None:
        if ctx.params.get("backend", "codex") != "codex":
            raise ValueError("the AHE Adapter's Evolve Agent and Agent Debugger are sessions of the codex Base Agent: "
                             f"evolver.params.backend is codex, not {ctx.params.get('backend')!r}")
        if not isinstance(ctx.params.get("web_search"), bool):
            raise ValueError("evolver.params.web_search must be set, true or false")
        from hermit.agent.evolver.methods.ahe_adapter import debugger as D
        own = D.settings(ctx.params).get("model")
        if own != ctx.params.get("model") and (ctx.params.get("binding") or ctx.params.get("price")):
            # The Debugger's sessions take the Evolve Agent's binding (its provider and key) and price: under another
            # model their usage would be recorded as the Evolve Agent's model and priced at its price.
            raise ValueError(f"evolver.params.debugger.model is {own!r}, not the Evolve Agent's model "
                             f"{ctx.params.get('model')!r}: with a bound model or a price, the Agent Debugger runs on "
                             f"the Evolve Agent's model (debugger.model: null)")
        state = ctx.workspace.state
        if not (state / "ahe.json").is_file():
            dump_json(state / "ahe.json", {"k": ctx.params.get("k"), "steps": {}})

    def settings(self, ctx: Context, backend: str) -> dict:
        """AgentEvolver's settings for the codex Base Agent, with the prompt on stdin (the evolve prompt and a
        Debugger question can be long) and the program `bin` names; the Agent Debugger starts from these too."""
        params = ctx.params
        return {**super().settings(ctx, backend), "stdin": True, **({"bin": params["bin"]} if params.get("bin") else {})}

    # ---- one step ----

    def step(self, ctx: StepContext) -> StepResult:
        """One AHE iteration (`_step`). A step that raises fails as a failed session does, with the usage spent so
        far (the Debugger's sessions are paid for whatever comes after them; a step that raised has no usage)."""
        usage: list[Usage] = []
        try:
            return self._step(ctx, usage)
        except Exception as exc:                        # noqa: BLE001 - the Trainer would record it as raised
            n = ctx.progress.step if ctx.progress else 0
            summary = f"step {n} raised {type(exc).__name__}: {exc}"[:300]
            rollbacks_file = ctx.workspace.state / "rollbacks" / f"{step_name(n)}.json"
            rollbacks = load_json(rollbacks_file)
            warning = (f"WARNING: {summary}; the Trainer discards this step's edits and rollbacks, nothing of step {n} "
                       f"ships")
            if isinstance(rollbacks, dict) and not rollbacks.get("discarded"):
                dump_json(rollbacks_file, {**rollbacks, "discarded": True, "note": warning})
            append_text(ctx.workspace.state / "evolution_history.md", f"**{warning}**\n\n---\n\n")
            return StepResult("failed", summary, usage=usage)

    def _step(self, ctx: StepContext, usage: list[Usage]) -> StepResult:
        from hermit.agent.evolver.methods.ahe_adapter import analysis as A
        p, ws = ctx.params, ctx.workspace
        n = ctx.progress.step if ctx.progress else 0
        name = step_name(n)
        started = time.monotonic()
        records = read_records(ws.learning_material)
        if not records:
            return StepResult("failed", "no learning material for this step")
        summary = load_json(ws.learning_material / "current" / "summary.json", {}) or {}
        commit = summary.get("adapter_commit")
        task_field = str(p.get("task_field") or "task")
        k = int(p.get("k") or infer_k(records, task_field))
        state = ws.state
        for sub in STATE_DIRS:
            (state / sub).mkdir(parents=True, exist_ok=True)
        run_name = ws.state.parent.parent.name
        scores_kw = {"experiment": run_name, "model": str(p.get("model") or ""), "source": f"learning_material/{name}"}

        # 1. the deterministic pre-step: classify, diff, attribute, best-ever.
        stats = A.compute_stats(records, k=k, timeout_as=str(p.get("timeout_as") or "exception"), task_field=task_field)
        dump_json(state / "stats" / f"{name}.json", stats)
        history = A.update_task_history(load_json(state / "task_history.json", {}) or {}, n, stats["task_results"],
                                        per_task_rollouts=stats.get("per_task_rollouts"))
        dump_json(state / "task_history.json", history)
        stability = A.compute_task_stability(history, min_iterations=int(p.get("stability_min_iterations") or 3))
        prev_stats = load_json(state / "stats" / f"{step_name(n - 1)}.json") if n > 1 else None
        diff = A.compute_iteration_diff(stats, prev_stats)
        A.history_before(state / "evolution_history.md", n, stats, diff=diff, source=scores_kw["source"])
        prev_manifest, evaluation = None, None
        if n > 1 and diff:
            candidate = load_json(state / "manifests" / f"{step_name(n - 1)}.json")
            if isinstance(candidate, dict) and candidate.get("iteration") == n - 1:       # the gate of evolve.py:2215
                prev_manifest = candidate
                evaluation = A.evaluate_changes(prev_manifest, diff, stats["task_results"])
                dump_json(state / "evaluations" / f"{name}.json", evaluation)
        best = A.update_best_ever(load_json(state / "best_ever.json"), n, stats, commit=commit)
        dump_json(state / "best_ever.json", best)
        verdicts = [{"change_id": e.get("change_id"), "verdict": e.get("verdict")}
                    for e in (evaluation or {}).get("change_evaluations") or []]
        timing = {"eval_min": minutes(float(summary.get("wall_s") or 0))}   # the rollouts' wall clock, from the material
        pass_rate = float(stats.get("pass_rate") or 0.0)
        extra = {"pass_rate": pass_rate, "k": k, "best_ever": best, "verdicts": verdicts, "rollbacks": None,
                 "manifest": None, "tokens_per_trial_k": stats.get("tokens_per_trial_k"), "counts": stats.get("counts")}

        # 2. early stop (the Trainer ends the run only with trainer.allow_early_stop).
        target = float(p.get("target_pass_rate") if p.get("target_pass_rate") is not None else 0.95)
        if pass_rate >= target:
            timing["total_min"] = minutes(time.monotonic() - started)
            A.update_iteration_scores(state, n, stats, timing=timing, extra=self._scores_extra(verdicts, []), **scores_kw)
            self._record(state, n, k, commit, stats, timing)
            note = f"pass@1 {pass_rate:.1%} >= target {target:.0%}: nothing to evolve"
            (ws.output / "report.md").write_text(f"{note}\n", encoding="utf-8")
            append_text(state / "evolution_history.md", f"**Target reached at step {n}:** {note}\n\n---\n\n")
            return StepResult("stop", note, extra={**extra, "timing": timing})

        # 3. rollback of the previous step's harmful changes, before the agent sees the adapter.
        rollbacks, done = None, []
        mode = str(p.get("rollback") or "auto")
        parent = (load_json(ws.learning_material / step_name(n - 1) / "summary.json", {}) or {}).get("adapter_commit") \
            if n > 1 else None
        rollbacks_file = state / "rollbacks" / f"{name}.json"
        if prev_manifest and evaluation:
            from hermit.agent.evolver.methods.ahe_adapter import rollback as R
            rollbacks = R.rollback(ws.adapter, prev_manifest, evaluation, parent, learnable_paths(ws.adapter),
                                   ctx.tools.get("check_spec"), p,
                                   previous=load_json(state / "rollbacks" / f"{step_name(n - 1)}.json"),
                                   snapshots=state / "rollbacks" / name)
            dump_json(rollbacks_file, rollbacks)
            done = rollbacks_done(rollbacks)
            extra["rollbacks"] = rollbacks

        def discard(reason: str) -> str:
            """The Trainer discards this step's adapter, its rollbacks included: mark the rollback record so the next
            step does not take it as what shipped, and return the warning."""
            warning = f"WARNING: {reason}; the Trainer discards this step's edits and rollbacks, nothing of step {n} ships"
            if rollbacks:
                dump_json(rollbacks_file, {**rollbacks, "discarded": True, "note": warning})
            return warning

        # 4. the Agent Debugger: one call of the codex Base Agent per attempt, on the Evolve Agent's settings.
        overview = None
        analysis_dir = state / "analysis" / name
        from hermit.agent.evolver.methods.ahe_adapter import debugger as D
        dbg = D.settings(p)
        debugger_info = {"enabled": bool(dbg.get("enabled", True)), "jobs": 0}
        if dbg.get("enabled", True):
            jobs = D.build_jobs(records, {**dbg, "task_field": task_field}, base=Path(records[0]["_dir"]))
            results, dbg_usage = D.run_jobs(jobs, ctx, dbg, base=self.settings(ctx, "codex"), scratch=ws.scratch)
            usage.append(role_usage(dbg_usage, "debugger"))
            _, overview = A.write_debugger_analyse(results, analysis_dir, n, detail_label=str(analysis_dir / "detail"))
            dump_json(analysis_dir / "jobs.json", results)
            dump_json(analysis_dir / "usage.json", dataclasses.asdict(dbg_usage))
            debugger_info.update({"jobs": len(results), "fallbacks": sum(1 for r in results if r.get("status") == "fallback"),
                                  "debug": sum(1 for r in results if r.get("mode") == "debug"),
                                  "summary": sum(1 for r in results if r.get("mode") == "summary")})
            if ctx.cancel is not None and ctx.cancel.is_set():
                append_text(state / "evolution_history.md", f"**{discard(STOPPED + ' during the Debugger')}**\n\n---\n\n")
                return StepResult("failed", STOPPED, usage=usage, extra={**extra, "debugger": debugger_info})
        timing["analysis_min"] = minutes(time.monotonic() - started)

        # 5. the query and the Evolve session.
        query = A.build_evolution_query(
            {"learning_material": ws.learning_material, "state": state, "adapter": ws.adapter}, stats, step=n,
            diff=diff, prev_stats=prev_stats, stability=stability, best_ever=best,
            scores_trend=A.load_iteration_scores(state), evaluation=evaluation,
            adb_overview=bounded_overview(overview, analysis_dir / "overview.md"),
            analysis_dir=analysis_dir, rollbacks=done, rollback_mode=mode, prev_commit=parent,
            history_md=state / "evolution_history.md", commit=commit)
        stream = D.rollout_stream(records)            # the acting agent's event stream, as the texts name it
        query = D.for_stream(query, stream)
        (state / "queries" / f"{name}.md").write_text(query, encoding="utf-8")
        start_md = (Path(getattr(self, "directory", ".")) / str(p.get("prompt") or "START.md")).read_text(encoding="utf-8")
        start_md = start_md.replace("{date}", datetime.now().strftime("%Y-%m-%d")).replace("{step_dir}", name)
        start_md = D.for_stream(start_md, stream)
        prompt = evolve_prompt(start_md, query, ws.scratch)
        e0 = time.monotonic()
        local, stop = D.deadline_cancel(ctx.cancel, p.get("evolve_timeout_s"))
        try:
            result = super().step(dataclasses.replace(ctx, params={**p, "prompt": str(prompt.resolve()), "resume_prompt": None},
                                                      cancel=local, session=None))
        except Exception as exc:                        # noqa: BLE001 - a session that could not run (no program, no
            # login, a limit the isolation refuses) fails the step as a failed session does; the Debugger's usage stays
            result = StepResult("failed", f"the Evolve session raised {type(exc).__name__}: {exc}"[:300])
        finally:
            stop.set()
        cancelled = ctx.cancel is not None and ctx.cancel.is_set()
        timed_out = local.cause == "deadline"
        timing["evolve_min"] = minutes(time.monotonic() - e0)
        timing["total_min"] = minutes(time.monotonic() - started)
        usage += [role_usage(u, "evolve") for u in result.usage]
        if cancelled:                                   # the Trainer replaces the result and discards the adapter
            append_text(state / "evolution_history.md", f"**{discard(STOPPED + ' during the Evolve session')}**\n\n---\n\n")
            return StepResult("failed", STOPPED, usage=usage, extra={**extra, "debugger": debugger_info, "timing": timing})

        # 6. post-step: the manifest, the history, the scores, the context file.
        manifest, note = validate_manifest(load_json(state / MANIFEST), n)
        warnings = []
        failed = result.status == "failed" and not timed_out
        if failed:                                      # the Trainer discards the edits: nothing of this step ships
            note = discard(f"the Evolve session failed ({result.summary})")
            warnings.append(note)
            manifest = {"iteration": n, "changes": [], "note": note}
        elif manifest is None:
            warnings.append(f"WARNING: {note}; the step's changes are unattributable (an empty manifest is archived)")
            manifest = {"iteration": n, "changes": [], "note": note}
        if timed_out:
            warnings.append(f"WARNING: the Evolve session was killed after evolve_timeout_s={p.get('evolve_timeout_s')}; "
                            f"its edits so far are kept")
        if not failed:
            manifest["changes"] = rollback_entries(done) + list(manifest["changes"])
        dump_json(state / "manifests" / f"{name}.json", manifest)
        report = ws.output / "report.md"
        text = report.read_text(encoding="utf-8", errors="replace").strip() if report.is_file() else ""
        if not text:
            text = result.summary
            report.write_text(text + "\n", encoding="utf-8")
        if warnings:
            append_text(report, "\n\n" + "\n".join(warnings) + "\n")
        append_text(state / "evolution_history.md",
                    "".join(f"**{w}**\n\n" for w in warnings) + f"**Evolve Agent Output:**\n\n{text[:REPORT_CHARS]}\n\n---\n\n")
        A.update_iteration_scores(state, n, stats, timing=timing, extra=self._scores_extra(verdicts, [] if failed else done),
                                  **scores_kw)
        sync_context(ws.adapter)
        self._record(state, n, k, commit, stats, timing)
        status = "done" if timed_out and result.status == "failed" else result.status
        summary_line = result.summary if not timed_out else f"evolve session timed out; {result.summary}"
        extra.update({"manifest": {"iteration": n, "changes": [c.get("id") for c in manifest["changes"]],
                                   "note": manifest.get("note")},
                      "debugger": debugger_info, "timing": timing, "evolve_timed_out": timed_out, "warnings": warnings})
        return StepResult(status, f"step {n}: pass@1 {pass_rate:.1%}; {summary_line}"[:300], rationale=result.rationale,
                          session=None, usage=usage, extra=extra)

    # ---- finalize ----

    def finalize(self, ctx: Context) -> FinalResult:
        ws = ctx.workspace
        best = load_json(ws.state / "best_ever.json", {}) or {}
        final = ws.adapter
        summary = "the last adapter"
        if str(ctx.params.get("final") or "best_ever") == "best_ever" and best.get("commit"):
            dest = ws.scratch / "best_adapter"
            shutil.rmtree(dest, ignore_errors=True)
            dest.mkdir(parents=True)
            archive = subprocess.run(["git", "-C", str(ws.adapter), "archive", "--format=tar", str(best["commit"])],
                                     capture_output=True)
            if archive.returncode == 0:
                subprocess.run(["tar", "-x", "-C", str(dest)], input=archive.stdout, check=True)
                final = dest
                summary = (f"best-ever adapter from step {best.get('step')} (pass@1 {float(best.get('pass_rate') or 0):.1%}, "
                           f"commit {str(best['commit'])[:12]})")
            else:
                summary = (f"the last adapter (best-ever commit {str(best['commit'])[:12]} of step {best.get('step')} is "
                           f"not in the adapter's history: {archive.stderr.decode(errors='replace').strip()[:200]})")
        ws.output.mkdir(parents=True, exist_ok=True)
        dump_json(ws.output / "final_selection.json",
                  {"commit": best.get("commit") if final is not ws.adapter else None, "step": best.get("step"),
                   "pass_rate": best.get("pass_rate"), "selection": summary})
        return FinalResult(adapter=final, summary=summary, extra={"best_ever": best})

    # ---- helpers ----

    @staticmethod
    def _scores_extra(verdicts: list[dict], done: list[dict]) -> dict:
        """What `iteration_scores.yaml` records of the step beyond the scores: the verdicts of the previous step's
        changes ({change id: verdict}) and the files the framework rolled back (the report's two columns)."""
        return {"verdicts": {str(v["change_id"]): str(v["verdict"]) for v in verdicts},
                "rollbacks": [f"{r['change_id']}: {', '.join(r.get('files') or [])}" for r in done if r.get("ok", True)]}

    @staticmethod
    def _record(state: Path, n: int, k: int, commit: str | None, stats: dict, timing: dict) -> None:
        """`state/ahe.json`: {k, steps: {N: {rollout_commit, pass_rate, tokens_per_trial_k, timing}}}."""
        data = load_json(state / "ahe.json", {}) or {}
        data["k"] = k
        data.setdefault("steps", {})[str(n)] = {"rollout_commit": commit, "pass_rate": stats.get("pass_rate"),
                                                "tokens_per_trial_k": stats.get("tokens_per_trial_k"), "timing": timing}
        dump_json(state / "ahe.json", data)
