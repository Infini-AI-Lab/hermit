# The stage structure follows google-research/harness2 core/recursion.py and core/selection.py, Copyright 2026
# Google LLC, Apache-2.0; adapted to a batch of tasks and to the framework's probes.
"""One contrastive Propose-Probe-Compose step over a batch, its selection, and its reflection.

Parallel (width): k INV + k DOM candidates from the shared reference evidence, all probed on the batch, then k
independent compositions, each probed. Sequential (depth): k chained rounds, each round's composer seeing every
earlier round. Both spend R = 1 + 3k batch rollouts (the Trainer's reference batch counts once) and P = 3k sessions;
the ledger asserts it. An invalid proposal is retried once with the validator's message, then inherits the tree it
started from; the fallback is recorded.
"""
from __future__ import annotations

import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from . import context as C
from . import prompts as P
from . import surface as S
from .ledger import Ledger
from .proposer import Proposer, ProposerError, Reply
from .selection import assign_ids, parse_choice, parse_notes, parse_reflection


@dataclass
class Stage:
    label: str
    dim: str
    tree: S.Tree
    kept_base: bool = False
    fell_back: bool = False
    error: str = ""
    usd: float = 0.0
    attempts: int = 0
    notes: str = ""
    autotriggered: list[str] = field(default_factory=list)

    def record(self) -> dict:
        return {"label": self.label, "dim": self.dim, "kept_base": self.kept_base, "fell_back": self.fell_back,
                "error": self.error[:600], "usd": round(self.usd, 6), "attempts": self.attempts,
                "notes": self.notes, "autotriggered": self.autotriggered}


@dataclass
class Env:
    """Everything a step needs from the framework, as plain values and callables."""
    surface: S.SurfaceSpec
    slots: P.Slots
    proposer: Proposer
    channel: str
    tasks: dict[str, str]                                  # task id -> the task as the agent received it
    reference: list[C.Rollout]                             # one rollout per task under the current harness
    base_tree: S.Tree
    work_dir: Path
    probe: Callable[[str, S.Tree], tuple[list[C.Rollout], float]]   # label, tree -> rollouts on the batch, usd
    check: Callable[[str, S.Tree], str] = lambda label, tree: ""    # extra validation (the template's check_spec)
    library_dir: Path | None = None
    leak_globs: Sequence[str] = ()
    agent_prompt: str = ""
    stage_attempts: int = 2
    workers: int = 4
    log: Callable[[str], None] = print
    stop: Callable[[], None] = lambda: None                # raises when the Trainer stopped the call


@dataclass
class StepResult:
    mode: str
    k: int
    stages: list[Stage] = field(default_factory=list)
    trees: dict[str, S.Tree] = field(default_factory=dict)
    probes: dict[str, list[C.Rollout]] = field(default_factory=dict)
    finals: list[str] = field(default_factory=list)
    ledger: Ledger | None = None
    similarity: dict | None = None

    def record(self) -> dict:
        return {"mode": self.mode, "k": self.k, "finals": self.finals,
                "stages": [s.record() for s in self.stages], "similarity": self.similarity,
                "ledger": self.ledger.to_dict() if self.ledger else None,
                "probes": {label: [{"task": r.task_id, "ok": r.ok, "delivered": r.delivered, "error": r.error}
                                   for r in rolls] for label, rolls in self.probes.items()}}


def _reraise_control(exc: Exception) -> None:
    from harnessadapter.tools import ToolError
    from harnessadapter.trainer.budget import BudgetExceeded
    if isinstance(exc, (BudgetExceeded, ToolError)):
        raise exc


def _accept(env: Env, new: S.Tree, fallback: S.Tree, label: str) -> tuple[bool, str, list[str]]:
    """Repair then validate a proposed tree: (kept_base, error or "", skills auto-triggered)."""
    S.repair_frontmatter(env.surface, new)
    triggered = S.autotrigger(env.surface, new)
    kept = not S.changed(S.diff_summary(env.surface, fallback, new))
    try:
        S.validate(env.surface, new)
        C.assert_tree_has_no_grader_verdicts(new)
    except ValueError as exc:
        return kept, str(exc), triggered
    err = env.check(label, new)
    return kept, err, triggered


def run_stage(env: Env, *, label: str, dim: str, job: str, fallback: S.Tree, harness_tree: S.Tree,
              pool: Sequence[tuple[str, Path, Sequence[C.Rollout]]] = (),
              decisions: dict[str, str] | None = None) -> Stage:
    """Build evidence, propose an edit, retry an invalid proposal once; never raises for a stage failure.

    As upstream: a session that fails outright (a ProposerError, after the transport retries inside the call) ends
    the stage, which inherits the tree it started from. A reply that could not be used, or a proposal that fails
    validation, is retried once with the reason; only a rejected TREE is written back for the retry to fix."""
    env.stop()
    root = env.work_dir / "stages" / label
    harness_dir = env.work_dir / "trees" / f"{label}.start"
    S.write_tree(env.surface, harness_tree, harness_dir, render=False)
    try:
        ev = C.build(root, tasks=env.tasks, rollouts=env.reference, harness_dir=harness_dir,
                     library_dir=env.library_dir, pool=pool, decisions=decisions, leak_globs=env.leak_globs,
                     surface=env.surface, extra=_extras(env))
        mission = C.mission(ev, job=job)
    except Exception as exc:
        _reraise_control(exc)
        return Stage(label, dim, dict(fallback), fell_back=True, attempts=0,
                     error=f"could not build evidence: {type(exc).__name__}: {exc}"[:600])
    err, retry_block, usd, attempts = "", "", 0.0, 0
    for attempt in range(1, env.stage_attempts + 1):
        env.stop()
        try:
            reply = env.proposer.call(mission + retry_block, root, f"{label}.a{attempt}", harvest="context/harness")
        except ProposerError as exc:
            usd += exc.usd
            attempts += 1
            err = f"the session failed: {exc}"
            break
        except Exception as exc:
            _reraise_control(exc)
            attempts += 1
            err = f"the session failed: {type(exc).__name__}: {exc}"
            break
        usd += reply.usd
        attempts += reply.attempts
        if reply.workspace is None:
            err = reply.error or "the harness did not come back from the session"
            retry_block = (f"\n\n# YOUR PREVIOUS REPLY COULD NOT BE USED\n\n{C.safe_text(err)}\n\nNothing from it was "
                           f"applied: `context/harness/` is unchanged. Reply again.")
            continue
        rejected: list[str] = []
        new = S.read_tree(env.surface, reply.workspace, rejected=rejected)
        if not new:
            err = "`context/harness/` came back empty -- edit the files, do not delete them all"
            retry_block = (f"\n\n# YOUR PREVIOUS REPLY COULD NOT BE USED\n\n{err}\n\nNothing from it was applied: "
                           f"`context/harness/` is unchanged. Reply again.")
            continue
        if rejected:
            err = ("these files are off the editable surface and were not applied: " + ", ".join(sorted(rejected)[:6])
                   + ". The editable paths, relative to the harness root, are "
                   + ", ".join(f"`{a}`" for a in env.surface.always_on)
                   + f" and `{env.surface.skill_dir}/<name>/SKILL.md` (skills sit at the root, not under `harness/`)"
                   + (f", `{env.surface.skill_dir}/<name>/scripts/<file>.py`" if env.surface.scripts else "")
                   + (f", `{env.surface.agent_dir}/<name>.md`" if env.surface.agents else "")
                   + "; component names are lowercase letters, digits, `_` or `-`.")
        else:
            kept, err, triggered = _accept(env, new, fallback, label)
            if not err:
                return Stage(label, dim, new, kept_base=kept, usd=usd, attempts=attempts,
                             notes=parse_notes(reply.text), autotriggered=triggered)
        S.write_tree(env.surface, new, root / "context" / "harness", render=False)
        retry_block = (f"\n\n# YOUR PREVIOUS ATTEMPT WAS REJECTED\n\n{C.safe_text(err)}\n\nThe harness under "
                       f"`context/harness/` is that rejected attempt. Fix it and reply again.")
    return Stage(label, dim, dict(fallback), fell_back=True, error=err[:600], usd=usd, attempts=attempts)


def decision(stage: Stage) -> str:
    """What later stages read about an earlier one: its notes, or why it fell back; never a verdict marker."""
    if stage.fell_back:
        return "FELL BACK to its starting harness: " + C.safe_text(stage.error, "its proposal was rejected")
    return C.safe_text(stage.notes or "No decision notes returned.")


def _extras(env: Env) -> list[tuple[str, str | Path, str]]:
    if not env.agent_prompt.strip():
        return []
    return [("agent_prompt.md", env.agent_prompt,
             "the acting agent's system prompt in the reference runs (the harness is rendered into it)")]


def _parallel_map(env: Env, fn, items: list, width: int | None = None) -> list:
    n = max(1, min(len(items), width or env.workers))
    if n == 1:
        return [fn(x) for x in items]
    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(fn, items))


def _probe_all(env: Env, result: StepResult, ledger: Ledger, stages: list[Stage]) -> None:
    def one(stage: Stage):
        env.stop()
        S.write_tree(env.surface, stage.tree, env.work_dir / "trees" / stage.label, render=True)
        rolls, usd = env.probe(stage.label, stage.tree)
        return stage.label, rolls, usd
    for label, rolls, usd in _parallel_map(env, one, stages):
        result.probes[label] = rolls
        ledger.rollout(label, usd)


def _pool(env: Env, result: StepResult, labels: Sequence[str]) -> list[tuple[str, Path, list[C.Rollout]]]:
    return [(label, env.work_dir / "trees" / label, result.probes.get(label, [])) for label in labels]


def run_parallel(env: Env, k: int) -> StepResult:
    """WIDTH: 2k candidates from shared evidence, then k INDEPENDENT compositions."""
    n = len(env.tasks)
    ledger = Ledger(mode="agg", unit="step", k=k)
    ledger.rollout("base", cached=True)
    result = StepResult(mode="agg", k=k, ledger=ledger)
    specs = [(f"{d.lower()}{i}", d, i) for i in range(1, k + 1) for d in ("INV", "DOM")]
    stages = _parallel_map(env, lambda s: run_stage(
        env, label=s[0], dim=s[1], fallback=env.base_tree, harness_tree=env.base_tree,
        job=P.propose(env.slots, s[1], env.surface, channel=env.channel, n_tasks=n, k=k, index=s[2])), specs)
    for st in stages:
        ledger.session(st.label, st.usd, attempts=st.attempts)
        result.stages.append(st)
        result.trees[st.label] = st.tree
    env.log("candidates: " + ", ".join(_describe(env, s) for s in stages))
    _probe_all(env, result, ledger, stages)
    pool = _pool(env, result, [s.label for s in stages])
    decisions = {s.label: decision(s) for s in stages}
    aggs = _parallel_map(env, lambda i: run_stage(
        env, label=f"agg{i}", dim="AGG", fallback=env.base_tree, harness_tree=env.base_tree, pool=pool,
        decisions=decisions,
        job=P.aggregate(env.slots, env.surface, channel=env.channel, n_tasks=n, k=k, index=i)),
        list(range(1, k + 1)))
    for st in aggs:
        ledger.session(st.label, st.usd, attempts=st.attempts)
        result.stages.append(st)
        result.trees[st.label] = st.tree
        result.finals.append(st.label)
    env.log("compositions: " + ", ".join(_describe(env, s) for s in aggs))
    _probe_all(env, result, ledger, aggs)
    result.similarity = S.diversity({s.label: s.tree for s in aggs})
    ledger.assert_identity()
    return result


def run_sequential(env: Env, k: int) -> StepResult:
    """DEPTH: k nested rounds over the same shared reference evidence."""
    n = len(env.tasks)
    ledger = Ledger(mode="seq", unit="step", k=k)
    ledger.rollout("base", cached=True)
    result = StepResult(mode="seq", k=k, ledger=ledger)
    current = dict(env.base_tree)
    history: list[str] = []
    for t in range(1, k + 1):
        decisions = {s.label: decision(s) for s in result.stages}
        specs = [(f"inv{t}", "INV"), (f"dom{t}", "DOM")]
        stages = _parallel_map(env, lambda s: run_stage(
            env, label=s[0], dim=s[1], fallback=current, harness_tree=current, pool=_pool(env, result, history),
            decisions=decisions,
            job=P.propose(env.slots, s[1], env.surface, channel=env.channel, n_tasks=n, k=k, index=t,
                          round_t=(t if k > 1 else None))), specs)
        for st in stages:
            ledger.session(st.label, st.usd, attempts=st.attempts)
            result.stages.append(st)
            result.trees[st.label] = st.tree
        _probe_all(env, result, ledger, stages)
        round_labels = history + [s.label for s in stages]
        ck = f"c{t}"
        job = (P.aggregate(env.slots, env.surface, channel=env.channel, n_tasks=n, k=1, index=1) if k == 1 else
               P.compose_round(env.slots, env.surface, channel=env.channel, n_tasks=n, t=t, k=k))
        comp = run_stage(env, label=ck, dim="COMPOSE", job=job, fallback=current, harness_tree=current,
                         pool=_pool(env, result, round_labels),
                         decisions={s.label: decision(s) for s in result.stages})
        ledger.session(comp.label, comp.usd, attempts=comp.attempts)
        result.stages.append(comp)
        result.trees[ck] = comp.tree
        _probe_all(env, result, ledger, [comp])
        env.log(f"round {t}: " + ", ".join(_describe(env, s) for s in [*stages, comp]))
        history = round_labels + [ck]
        current = comp.tree
    result.finals = [f"c{i}" for i in range(1, k + 1)]
    ledger.assert_identity()
    return result


def _describe(env: Env, st: Stage) -> str:
    if st.fell_back:
        return f"{st.label} fell back ({st.error[:80]})"
    if st.kept_base:
        return f"{st.label} KEEP_BASE"
    d = S.diff_summary(env.surface, env.base_tree, st.tree)
    return f"{st.label} +{len(d['added'])}/~{len(d['modified'])}/-{len(d['removed'])} files"


# ---------------------------------------------------------------- selection -------------------------------------

@dataclass
class Selection:
    chosen: str
    ok: bool = False                                             # False: no lesson, nothing enters the library
    why: str = ""
    trivial: bool = False
    picks: dict[str, str] = field(default_factory=dict)          # task id -> chosen label (real selector picks only)
    id_maps: dict[str, dict[str, str]] = field(default_factory=dict)   # task id -> anon id -> label
    fallbacks: list[str] = field(default_factory=list)
    error: str = ""
    usd: float = 0.0

    def record(self) -> dict:
        return {"chosen": self.chosen, "ok": self.ok, "why": self.why, "trivial": self.trivial, "picks": self.picks,
                "id_maps": self.id_maps, "fallbacks": self.fallbacks, "error": self.error, "usd": round(self.usd, 6)}


def _delivered_any(result: StepResult, label: str) -> bool:
    return any(r.ok and r.delivered for r in result.probes.get(label, []))


def select(env: Env, result: StepResult, how: str, step_key: str) -> Selection:
    """Which final to carry. `composed`: the last final (the paper at k = 1, adopted without a session). `blind`:
    for each task a selector sees the finals' deliverables, anonymised, and picks one; the final with the most
    real picks is carried (ties: the earlier label). A selector reply that cannot be parsed is recorded, not
    counted. As upstream, a selection is `ok` only when the adopted final delivered; when it did not deliver on
    any task, the step keeps the current harness (`base`) and stores no lesson."""
    finals = [f for f in result.finals if f in result.trees]
    if how == "composed" or len(finals) <= 1:
        chosen = finals[-1]
        sel = Selection(chosen=chosen, trivial=True,
                        why="single final at k=1; adopted without a selector session" if len(finals) <= 1
                        else "select: composed")
    else:
        tally = {label: 0 for label in finals}
        sel = Selection(chosen=finals[0])
        whys = []
        for tid in env.tasks:
            env.stop()
            produced = {label: next((r for r in result.probes.get(label, []) if r.task_id == tid), None)
                        for label in finals}
            pool = {label: r for label, r in produced.items() if r is not None and r.ok and r.delivered}
            if not pool:
                sel.fallbacks.append(f"{tid}: no final delivered")
                continue
            if len(pool) == 1:
                only = next(iter(pool))
                sel.picks[tid] = only
                tally[only] += 1
                whys.append(f"{tid} -> {only}: the only final that delivered")
                continue
            id_map = assign_ids(list(pool), seed_key=f"{step_key}|{tid}|{len(pool)}")
            sel.id_maps[tid] = id_map
            root = env.work_dir / "selection" / C._safe(tid)
            if root.exists():
                shutil.rmtree(root)
            (root / "context" / "candidates").mkdir(parents=True)
            (root / "context" / "task.md").write_text(env.tasks[tid], encoding="utf-8")
            for anon, label in id_map.items():
                (root / "context" / "candidates" / f"{anon}.md").write_text(pool[label].deliverable, encoding="utf-8")
            try:
                C.assert_no_grader_verdicts(root / "context")
            except ValueError as exc:
                sel.fallbacks.append(f"{tid}: candidates refused by the tripwire ({exc})"[:300])
                continue
            ids = ", ".join(sorted(id_map))
            mission = (P.select(env.slots, len(pool)) + "\n# YOUR EVIDENCE\n\nIt is on disk. Read it -- do not answer "
                       "from this message alone.\n\ncontext/task.md                 the instructions\n"
                       f"context/candidates/<id>.md      one file per candidate: {ids}\n\nOpen every candidate before "
                       f"choosing. Choose exactly one of: {ids}.")
            try:
                reply = env.proposer.call(mission, root, f"select.{C._safe(tid)}", expect=r"```choice")
                sel.usd += reply.usd
                anon, why = parse_choice(reply.text, id_map)
            except (ProposerError, ValueError) as exc:
                sel.usd += getattr(exc, "usd", 0.0)
                sel.fallbacks.append(f"{tid}: {exc}"[:300])
                continue
            label = id_map[anon]
            sel.picks[tid] = label
            tally[label] += 1
            whys.append(f"{tid} -> {label} (chose {anon}): {why}")
        if sel.picks:
            sel.chosen = max(finals, key=lambda label: (tally[label], -finals.index(label)))
        else:
            sel.chosen = finals[0]
            sel.error = "no task produced a usable selector choice"
        sel.why = f"real picks per final: {tally}\n" + "\n".join(whys)
    if not _delivered_any(result, sel.chosen):
        sel.error = (sel.error + "; " if sel.error else "") + f"{sel.chosen} delivered on no task"
        sel.chosen = "base"
        sel.ok = False
        sel.why = (sel.why + "\n" if sel.why else "") + "the adopted final delivered on no task: the current harness is kept"
    else:
        sel.ok = not sel.error
    return sel


# ---------------------------------------------------------------- reflection ------------------------------------

def reflect(env: Env, result: StepResult, sel: Selection) -> tuple[str, dict, float]:
    """Edit rows and at most one transferable lesson from the whole step record. Never raises for a model failure."""
    env.stop()
    root = env.work_dir / "reflection"
    labels = [s.label for s in result.stages]
    try:
        S.write_tree(env.surface, env.base_tree, env.work_dir / "trees" / "base.start", render=False)
        adopted = env.work_dir / "trees" / (sel.chosen if sel.chosen != "base" else "base.start")
        ev = C.build(root, tasks=env.tasks, rollouts=env.reference, harness_dir=adopted, library_dir=None,
                     pool=_pool(env, result, labels), decisions={s.label: decision(s) for s in result.stages},
                     leak_globs=env.leak_globs, surface=env.surface,
                     extra=[*_extras(env),
                            ("base_harness", env.work_dir / "trees" / "base.start", "the harness this step started from"),
                            ("adoption.json", json.dumps({"chosen_label": sel.chosen, "candidate_ids": sel.id_maps,
                                                          "reason": C.safe_text(sel.why)}, indent=2),
                             "which harness was adopted, and why")])
    except Exception as exc:
        _reraise_control(exc)
        return f"(no reflection: {type(exc).__name__}: {exc})", {}, 0.0
    how = ("Selection compared DELIVERABLES with the harnesses hidden, so the choice says which output read best -- "
           "not which edit worked." if sel.id_maps else "")
    mission = C.mission(ev, job=P.experience(env.slots, blind=bool(sel.id_maps)) + (
        "\n# WHAT HAPPENED\n\n"
        f"Harness {sel.chosen} was adopted and is carried forward. {how} The reason recorded was:\n\n"
        f"{C.safe_text(sel.why)}\n\ncontext/adoption.json maps any anonymous IDs in that reasoning to rollout labels "
        "(per task). Compare context/harness/ (the adopted harness) with context/base_harness/ and the candidates in "
        "context/pool/. Read the decisions and execution contrasts before writing."))
    try:
        reply = env.proposer.call(mission, root, "experience", expect=r"</experience>|NO_ENTRY")
    except ProposerError as exc:
        return f"(no reflection: {exc})", {}, exc.usd
    except Exception as exc:
        _reraise_control(exc)
        return f"(no reflection: {type(exc).__name__}: {exc})", {}, 0.0
    rows, experience = parse_reflection(reply.text)
    if experience:
        try:   # exactly what Library.add stores
            C.assert_tree_has_no_grader_verdicts({"EXPERIENCE.md": str(experience) + (sel.why or "")})
        except ValueError as exc:
            return C.safe_text(rows) + f"\n(experience refused: {exc})", {}, reply.usd
    return C.safe_text(rows), experience, reply.usd


def preflight(env: Env) -> None:
    """Build the evidence every stage shares (tasks, reference rollouts, harness, library) once, before any probe:
    a refusal here would make every stage fall back, so the step should stop before spending anything."""
    harness_dir = env.work_dir / "trees" / "preflight"
    S.write_tree(env.surface, env.base_tree, harness_dir, render=False)
    C.build(env.work_dir / "stages" / "preflight", tasks=env.tasks, rollouts=env.reference, harness_dir=harness_dir,
            library_dir=env.library_dir, leak_globs=env.leak_globs, surface=env.surface, extra=_extras(env))
