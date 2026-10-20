"""Harness2Evolver: Harness² (google-research/harness2) as a training-loop Evolver (`mode: exec`).

Each step:

1. Evidence. The step's rollouts (one per task, under the current adapter) become the reference set: their
   trajectories rendered in one grammar, what each delivered, the task texts, the current harness, the library of
   this domain, and the acting agent's prompt. Judge scores and feedback are never read; a tripwire refuses any
   evidence that carries them.
2. Propose. An INV editor and a DOM editor (k of each) edit copies of the harness.
3. Probe. Each candidate runs on the same tasks through `run_agent` (cost counted in the run's budget).
4. Compose. A composer reads the reference set and every candidate's probes and composes the adopted harness, which
   is probed too.
5. Select. At k = 1 the composition is adopted; at k > 1 a blind selector picks among the finals' deliverables.
6. Reflect. Edit verdicts and at most one transferable lesson; with a lesson, the adopted harness enters
   `state/library/<domain>/`.
7. Hand back. The adopted tree is written into the adapter and ADAPTER.md is rendered from it.

Deviation from the paper, stated in the docs: the adopted harness is carried to the next step (the paper resets to
the base harness for each task), and a step's reference set is one rollout of each task of the batch.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import shutil
import sys
import time
from collections import Counter
from pathlib import Path

from harnessadapter.evolver.base import Evolver, StepContext, StepResult

HERE = Path(__file__).resolve().parent
if __package__ in (None, ""):
    # Loaded by file path (evolver.yaml `class: evolver.py:...`): make the package importable by its name.
    root = HERE.parents[3]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
from harnessadapter.algorithms.evolvers.harness2.core import context as C        # noqa: E402
from harnessadapter.algorithms.evolvers.harness2.core import prompts as P        # noqa: E402
from harnessadapter.algorithms.evolvers.harness2.core import render as R         # noqa: E402
from harnessadapter.algorithms.evolvers.harness2.core import surface as S        # noqa: E402
from harnessadapter.algorithms.evolvers.harness2.core import step as STEP        # noqa: E402
from harnessadapter.algorithms.evolvers.harness2.core.library import Library     # noqa: E402
from harnessadapter.algorithms.evolvers.harness2.core.proposer import LlmProposer  # noqa: E402


def deliverable_text(output, *, messages: list | None = None, log=None) -> str:
    """What a rollout delivered, as text: the extracted answer (outputs with an `answer` field), the text, or the
    whole output; plus the agent's final message. Never a verdict."""
    final = R.final_message(messages, log)
    if isinstance(output, dict) and "answer" in output:
        answer = output.get("answer")
        head = (f"Final answer: {answer}" if answer not in (None, "")
                else f"No answer was produced ({output.get('fail_reason') or 'no reason given'}).")
    elif isinstance(output, str):
        head = output if output.strip() else "(empty output)"
    elif output is None:
        head = "(no output: the run failed)"
    else:
        head = json.dumps(output, ensure_ascii=False, default=str)
    if final and final.strip() and final.strip() not in head:
        head += "\n\nThe agent's final message:\n" + R.clip(final, 6000)
    return head + "\n"


def delivered(output) -> bool:
    """Whether a rollout produced the deliverable, by the same rule as deliverable_text: an `answer` that is not
    None or empty; a non-blank text; any other output that is not None."""
    if isinstance(output, dict) and "answer" in output:
        return output.get("answer") not in (None, "")
    if isinstance(output, str):
        return bool(output.strip())
    return output is not None


def task_text(record: dict) -> str:
    """The task as the agent received it: its first user message, else the task's own fields."""
    for m in record.get("messages") or []:
        if m.get("role") == "user" and m.get("content"):
            return str(m["content"])
    task = dict(record.get("input") or {})
    for key in ("question", "prompt", "task", "instruction", "input"):
        if isinstance(task.get(key), str) and task[key].strip():
            return task[key]
    task.pop("id", None)
    return json.dumps(task, ensure_ascii=False, indent=1, default=str)


def _load_object(spec: str, base: Path | None):
    module_part, _, name = str(spec).partition(":")
    if module_part.endswith(".py"):
        path = Path(module_part)
        path = path if path.is_absolute() else (Path(base or ".") / path).resolve()
        mod_spec = importlib.util.spec_from_file_location(f"_harness2_proposer_{path.stem}", path)
        module = importlib.util.module_from_spec(mod_spec)
        mod_spec.loader.exec_module(module)
    else:
        module = importlib.import_module(module_part)
    return getattr(module, name)


class Harness2Evolver(Evolver):
    supports_resume = False

    def step(self, ctx: StepContext) -> StepResult:
        started = time.time()
        p = dict(ctx.params)
        ws = ctx.workspace
        mode = str(p.get("mode", "exec"))
        if mode != "exec":
            return StepResult("failed", f"harness2: mode {mode!r} is not implemented yet (exec only)")
        surface = S.load_surface(ws.adapter)
        if not (ws.adapter / S.SURFACE_FILE).is_file():
            return StepResult("failed", "harness2 needs an adapter of the `harness2` template (no harness2.yaml)")
        slots_path = self._path(ctx, p.get("slots"))
        if slots_path is not None and not slots_path.is_file():
            return StepResult("failed", f"harness2: the slots file {slots_path} does not exist")
        slots = P.load_slots(slots_path or ws.task_info / "harness2.yaml")
        C.EXTRA_MARKERS[:] = [str(m) for m in (p.get("verdict_markers") or [])]
        records = [json.loads(f.read_text()) for f in sorted((ws.learning_material / "current" / "tasks").glob("*.json"))]
        if not records:
            return StepResult("done", "no rollouts in this step: nothing to learn from")
        step_no = ctx.progress.step if ctx.progress else 0
        work = ws.scratch / "harness2"
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True)
        out = ws.output
        log_lines: list[str] = []

        def log(msg: str) -> None:
            line = f"[harness2 step {step_no}] {msg}"
            log_lines.append(line)
            print(line, flush=True)

        obs_trunc = int(p.get("obs_trunc", 50000))
        reference, tasks = [], {}
        agent_prompt = ""
        for r in records:
            tid = str(r["id"])
            tasks[tid] = task_text(r)
            traj_dir = ws.learning_material / "current" / r["trajectory"] if r.get("trajectory") else None
            if r.get("messages"):
                trajectory = R.render_messages(r["messages"], obs_trunc=obs_trunc)
            elif traj_dir is not None and traj_dir.exists():
                trajectory = R.render_log(traj_dir, obs_trunc=obs_trunc)
            else:
                trajectory = ""
            if not agent_prompt:
                agent_prompt = next((m.get("content") or "" for m in r.get("messages") or [] if m.get("role") == "system"),
                                    "") or (R.system_prompt_of(traj_dir) if traj_dir is not None else "")
            reference.append(C.Rollout(label=tid, task_id=tid, trajectory=trajectory,
                                       deliverable=deliverable_text(r.get("output"), messages=r.get("messages"),
                                                                    log=traj_dir),
                                       ok=r.get("status") != "error", delivered=delivered(r.get("output")),
                                       # only a pipeline failure's own text; a judge failure's text is the judge's
                                       error=str(r.get("error") or "") if r.get("status") == "error" else ""))
        field = str(p.get("domain_field") or "domain")
        domains = Counter(str((r.get("input") or {}).get(field) or "default") for r in records)
        domain = domains.most_common(1)[0][0]
        if len(domains) > 1:
            # One harness serves the whole batch in this mode, so the step reads and fills one library: the
            # batch's majority domain. For per-domain libraries, train one run per domain.
            print(f"[harness2 step] mixed domains {dict(domains)}: using the library of {domain!r}", flush=True)
        library = Library(ws.state / "library", domain, surface)
        base_tree = S.read_tree(surface, ws.adapter)
        proposer = self._proposer(ctx, p, out / "sessions")
        ids = [str(r["id"]) for r in records]
        adapter_copy_ignore = shutil.ignore_patterns(".git", "__pycache__")

        def probe(label: str, tree: S.Tree):
            d = work / "candidates" / label
            if d.exists():
                shutil.rmtree(d)
            shutil.copytree(ws.adapter, d, ignore=adapter_copy_ignore)
            S.write_tree(surface, tree, d, render=True)
            results = ctx.tools["run_agent"](task_ids=ids, adapter=str(d))["results"]
            rolls, usd = [], 0.0
            for res in results:          # judge is in each result; it is never read here
                lg = res.get("log")
                traj = R.render_log(lg, obs_trunc=obs_trunc) if lg and Path(lg).exists() else ""
                rolls.append(C.Rollout(label=str(res["id"]), task_id=str(res["id"]), trajectory=traj,
                                       deliverable=deliverable_text(res.get("output"), log=lg),
                                       ok=res.get("status") != "error",
                                       delivered=delivered(res.get("output")),
                                       error="" if traj else "the run left no trajectory"))
                usd += float(res.get("cost_usd") or 0.0)
            return rolls, usd

        def check(label: str, tree: S.Tree) -> str:
            d = work / "checks" / label
            if d.exists():
                shutil.rmtree(d)
            shutil.copytree(ws.adapter, d, ignore=adapter_copy_ignore)
            S.write_tree(surface, tree, d, render=True)
            result = ctx.tools["check_spec"](path=str(d))
            if result.get("passed"):
                return ""
            return "the adapter fails its specification: " + "; ".join(
                f"{i.get('path') or '(adapter)'}: {i.get('problem')}" for i in result.get("issues", [])[:8])

        def stop() -> None:
            if ctx.cancel is not None and ctx.cancel.is_set():
                from harnessadapter.tools import ToolError
                raise ToolError("the call was stopped by the Trainer (budget or timeout)")

        env = STEP.Env(surface=surface, slots=slots, proposer=proposer, channel=str(p.get("channel", "llm")),
                       tasks=tasks, reference=reference, base_tree=base_tree, work_dir=work, probe=probe, check=check,
                       library_dir=library.context_dir(), leak_globs=tuple(p.get("leak_globs") or ()),
                       agent_prompt=agent_prompt, stage_attempts=int(p.get("stage_attempts", 2)),
                       workers=int(p.get("workers", 4)), log=log, stop=stop)
        k = int(p.get("k", 1))
        recursion = str(p.get("recursion", "parallel"))
        log(f"{len(ids)} tasks, domain {domain!r}, k={k} {recursion}; library entries: {len(library.entries())}")
        try:
            STEP.preflight(env)
        except ValueError as exc:      # the shared evidence is refused: every stage would fall back
            log(f"skipped: the shared evidence was refused ({exc})")
            with open(ws.state / "harness2.jsonl", "a") as f:
                f.write(json.dumps({"step": step_no, "tasks": ids, "skipped": str(exc)}) + "\n")
            return StepResult("done", f"skipped, no change: the step's evidence was refused by the tripwire ({exc})"[:300])
        result = (STEP.run_sequential if recursion == "sequential" else STEP.run_parallel)(env, k)
        how = str(p.get("select", "auto"))
        how = ("composed" if k == 1 else "blind") if how == "auto" else how
        sel = STEP.select(env, result, how, step_key=f"step{step_no}")
        adopted = base_tree if sel.chosen == "base" else result.trees[sel.chosen]
        rows, experience, reflect_usd = ("", {}, 0.0)
        if p.get("reflect", True) and sel.ok:          # as upstream: no reflection without a delivered selection
            rows, experience, reflect_usd = STEP.reflect(env, result, sel)
        S.write_tree(surface, adopted, ws.adapter, render=True)
        check_result = ctx.tools["check_spec"]()
        stop()                      # a call the Trainer stopped must not leave a library entry behind
        entry = None
        if experience and check_result["passed"]:
            try:
                entry = library.add(f"step_{step_no:04d}", adopted, why=C.safe_text(sel.why or ""),
                                    label=sel.chosen, experience=experience)
            except ValueError as exc:
                rows += f"\n(lesson not stored: {exc})"
        changed = S.diff_summary(surface, base_tree, adopted)
        summary = (("" if sel.ok else f"selection failed ({sel.error}); ") + f"adopted {sel.chosen}: "
                   + (f"+{len(changed['added'])} ~{len(changed['modified'])} "
                   f"-{len(changed['removed'])} files, always-on {changed['always_on_char_delta']:+d} chars"
                   if S.changed(changed) else "no change (KEEP_BASE or fallback)")
                   + ("; lesson stored" if entry else "; no lesson") + ("" if check_result["passed"] else
                                                                       "; FAILED check_spec"))
        record = {"step": step_no, "domain": domain, "tasks": ids, "k": k, "recursion": recursion,
                  "selection": sel.record(), "result": result.record(), "changed": changed,
                  "experience": experience, "edit_rows": rows, "library_entry": str(entry) if entry else None,
                  "reflect_usd": round(reflect_usd, 6), "wall_s": round(time.time() - started, 1),
                  "check_passed": check_result["passed"]}
        with open(ws.state / "harness2.jsonl", "a") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self._report(out, record, result, sel, rows, experience, log_lines, work)
        notes = "\n\n".join(f"[{s.label}] {s.notes or s.error}" for s in result.stages if s.notes or s.error)
        return StepResult("done", summary[:300], rationale=notes[:20000],
                          extra={"harness2": {k2: record[k2] for k2 in ("domain", "k", "recursion", "changed")}
                                 | {"adopted": sel.chosen, "lesson": bool(entry)}})

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _path(ctx: StepContext, value) -> Path | None:
        if not value:
            return None
        path = Path(str(value)).expanduser()
        return path if path.is_absolute() else (Path(ctx.base or ".") / path).resolve()

    def _proposer(self, ctx: StepContext, p: dict, log_dir: Path):
        if p.get("proposer"):
            cls = _load_object(p["proposer"], ctx.base)
            return cls(ctx=ctx, log_dir=log_dir) if isinstance(cls, type) else cls
        channel = str(p.get("channel", "llm"))
        if channel != "llm":
            raise ValueError(f"harness2: channel {channel!r} is not implemented yet; use channel: llm")
        if "llm" not in ctx.tools:
            raise ValueError("harness2 with channel llm needs the run's llm tool: set `model` or `llm` in run.yaml")
        return LlmProposer(ctx.tools["llm"], max_tokens=int(p.get("max_tokens", 32000)),
                           temperature=p.get("temperature"), inline_chars=int(p.get("inline_chars", 600000)),
                           log_dir=log_dir)

    @staticmethod
    def _report(out: Path, record: dict, result, sel, rows: str, experience: dict, log_lines: list[str],
                work: Path) -> None:
        out.mkdir(parents=True, exist_ok=True)
        (out / "harness2_step.json").write_text(json.dumps(record, indent=1, ensure_ascii=False, default=str))
        trees = out / "trees"
        if (work / "trees").is_dir():
            shutil.copytree(work / "trees", trees, dirs_exist_ok=True)
        lines = [f"# Harness² step {record['step']}", "", f"- tasks: {', '.join(record['tasks'])}",
                 f"- domain: {record['domain']}, k={record['k']} {record['recursion']}",
                 f"- adopted: `{sel.chosen}` ({sel.why.splitlines()[0] if sel.why else ''})",
                 f"- changed: {json.dumps(record['changed'])}", f"- check_spec passed: {record['check_passed']}", "",
                 "## Stages", ""]
        for s in result.stages:
            lines += [f"### {s.label} ({s.dim})" + (" — fell back" if s.fell_back else "")
                      + (" — KEEP_BASE" if s.kept_base and not s.fell_back else ""),
                      "", s.notes or s.error or "(no notes)", ""]
        lines += ["## Ledger", "", "```json", json.dumps(record["result"]["ledger"], indent=1), "```", "",
                  "## Edit verdicts", "", rows or "(none)", "", "## Lesson", "",
                  json.dumps(experience, indent=1) if experience else "NO_ENTRY", "", "## Log", "", *log_lines]
        (out / "report.md").write_text("\n".join(lines) + "\n")
