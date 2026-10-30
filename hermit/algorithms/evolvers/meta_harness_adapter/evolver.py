"""MetaHarnessAdapterEvolver: the outer loop of Meta-Harness (Lee et al., arXiv:2603.28052;
github.com/stanford-iris-lab/meta-harness) on the Trainer, with the search confined to the adapter.

The paper's loop (Algorithm 1): a coding-agent proposer reads a filesystem holding every earlier candidate's code,
scores and execution traces, and writes k new harnesses; each that passes interface validation is evaluated on the
search set and logged to the filesystem; after N iterations the best harness on the search set is selected. Here the
harness is the whole adapter (template meta-harness-adapter: every file outside .hermit/ is learnable) and the acting
agent is fixed, where the paper's proposer also rewrites the agent's own loop.

Mapping onto the Trainer, one candidate per step (data.step_size covers the training split, which is the search set):

1. The step's rollouts are the evaluation of the candidate handed back last time (the starting adapter, `seed`, on
   the first step); their scores and trajectories are in the step's learning material. The step records them.
2. When no proposed candidate is left to evaluate, a proposer session (a Codex or Claude Code agent, as the built-in
   `codex` / `claude` Evolvers run it, with check_spec as its only tool) writes k candidates into the scratch
   directory; each that passes check_spec is stored under state/candidates/<id>/ and queued.
3. The next queued candidate is handed back (its files replace the working copy's, `.hermit/` kept), so the next
   step's rollouts evaluate it. With nothing queued (max_rounds reached, or no valid candidate), the best candidate so
   far is handed back and is evaluated again.

finalize hands back the candidate with the best mean score on the search set. The proposer never runs evaluations,
and sees no validation or test results.

Training State: state/meta_harness_adapter.json (every candidate: round, hypothesis, base, where its files are, each
evaluation's step and per-task scores; the queue) and state/evolution_summary.jsonl (one line per candidate, the
paper's summary file, rewritten every step), state/candidates/<id>/ (each candidate's files).
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
import re
import shutil

from hermit.evolver.agent import AgentEvolver
from hermit.evolver.base import FinalResult, StepContext, StepResult

STATE = "meta_harness_adapter.json"
SUMMARY = "evolution_summary.jsonl"
CANDIDATES = "candidates"
PENDING = "pending_eval.json"
KEEP = (".git", ".hermit")                 # what a candidate never brings into the working copy
NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,47}")


def _mean(scores: dict) -> float | None:
    return sum(scores.values()) / len(scores) if scores else None


def _copy_tree(source: Path, target: Path) -> None:
    """`source`'s files at `target`, without its root's .git and .hermit/, or byte code anywhere."""
    if target.exists():
        shutil.rmtree(target)

    def ignore(directory, names):
        skip = {"__pycache__", *(n for n in names if n.endswith(".pyc"))}
        return skip | ({n for n in names if n in KEEP} if Path(directory) == source else set())
    shutil.copytree(source, target, symlinks=False, ignore=ignore)


def _digest(tree: Path) -> str:
    """The content of a stored candidate: every file's relative path and bytes."""
    h = hashlib.sha256()
    for path in sorted(p for p in tree.rglob("*") if p.is_file() or p.is_symlink()):
        h.update(path.relative_to(tree).as_posix().encode() + b"\0")
        h.update(b"link:" + str(path.readlink()).encode() if path.is_symlink() else path.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def _install(candidate: Path, adapter: Path) -> None:
    """The candidate's files in the working copy: everything but .git and .hermit/ replaced."""
    for entry in adapter.iterdir():
        if entry.name in KEEP:
            continue
        shutil.rmtree(entry) if entry.is_dir() and not entry.is_symlink() else entry.unlink()
    for entry in candidate.iterdir():
        if entry.name in KEEP or entry.name == "__pycache__":
            continue
        target = adapter / entry.name
        if entry.is_dir():
            shutil.copytree(entry, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(entry, target)


class MetaHarnessAdapterEvolver(AgentEvolver):
    supports_resume = False             # the paper's proposer starts each iteration afresh, reading the filesystem

    # ---------- state ----------
    @staticmethod
    def _load(ctx) -> dict:
        path = ctx.workspace.state / STATE
        if path.is_file():
            return json.loads(path.read_text())
        return {"candidates": {}, "order": [], "queue": [], "rounds": 0, "handed_back": None}

    @staticmethod
    def _save(ctx, state: dict) -> None:
        (ctx.workspace.state / STATE).write_text(json.dumps(state, ensure_ascii=False, indent=1))
        lines = []
        for cid in state["order"]:
            c = state["candidates"][cid]
            evals = c.get("evaluations") or []
            scores = [s for e in evals for s in e["scores"].values()]
            lines.append({"id": cid, "round": c["round"], "hypothesis": c.get("hypothesis", ""),
                          "base": c.get("base"), "status": c["status"],
                          "mean_score": round(sum(scores) / len(scores), 4) if scores else None,
                          "tasks": len(evals[-1]["scores"]) if evals else 0,
                          "rollouts": [str(ctx.workspace.learning_material / e["material"]) for e in evals],
                          "files": str(ctx.workspace.state / CANDIDATES / cid / "adapter")
                          if c["status"] != "invalid" else None,
                          **({"problems": c["problems"]} if c.get("problems") else {})})
        (ctx.workspace.state / SUMMARY).write_text("".join(json.dumps(l, ensure_ascii=False) + "\n" for l in lines))

    @staticmethod
    def _score(c: dict) -> float | None:
        scores = [s for e in c.get("evaluations") or [] for s in e["scores"].values()]
        return sum(scores) / len(scores) if scores else None

    def _best(self, state: dict) -> str | None:
        scored = [(self._score(state["candidates"][cid]), -i, cid) for i, cid in enumerate(state["order"])
                  if self._score(state["candidates"][cid]) is not None
                  and state["candidates"][cid]["status"] != "tampered"]
        return max(scored)[2] if scored else None

    @staticmethod
    def _intact(ctx, state: dict, cid: str) -> bool:
        """Whether a stored candidate's files are still the ones it was stored (and scored) with. The proposer's
        sandbox can write state/, so a candidate changed in place is marked tampered and never handed back."""
        c = state["candidates"][cid]
        if "digest" not in c:
            return True
        if _digest(ctx.workspace.state / CANDIDATES / cid / "adapter") == c["digest"]:
            return True
        c["status"] = "tampered"
        c.setdefault("problems", []).append("its stored files changed after it was stored")
        return False

    def _next_best(self, ctx, state: dict) -> str | None:
        """The best candidate whose stored files are intact."""
        while (best := self._best(state)) is not None and not self._intact(ctx, state, best):
            pass
        return best

    # ---------- the Trainer's calls ----------
    def step(self, ctx: StepContext) -> StepResult:
        state = self._load(ctx)
        n = ctx.progress.step if ctx.progress else 0
        store = ctx.workspace.state / CANDIDATES
        store.mkdir(parents=True, exist_ok=True)
        if not state["order"]:                        # the starting adapter is the first candidate
            _copy_tree(ctx.workspace.adapter, store / "seed" / "adapter")
            state["candidates"]["seed"] = {"round": 0, "hypothesis": "the starting adapter", "base": None,
                                          "status": "queued", "digest": _digest(store / "seed" / "adapter")}
            state["order"].append("seed")
            state["handed_back"] = "seed"
        lines = [f"# Meta-Harness Adapter step {n}", ""]

        # 1. The step's rollouts evaluated the candidate handed back last time.
        evaluated = state["handed_back"]
        current = ctx.workspace.learning_material / "current"
        scores = {}
        for path in sorted((current / "tasks").glob("*.json")) if (current / "tasks").is_dir() else []:
            record = json.loads(path.read_text())
            scores[str(record["id"])] = float((record.get("judge") or {}).get("score") or 0.0)
        if evaluated and scores:
            c = state["candidates"][evaluated]
            repaired = (current / "check_feedback.json").is_file()
            c.setdefault("evaluations", []).append({"step": n, "material": current.resolve().name, "scores": scores,
                                                    **({"repaired": True} if repaired else {})})
            c["status"] = "evaluated"
            lines.append(f"Evaluated {evaluated}: mean {_mean(scores):.3f} over {len(scores)} tasks"
                         + (" (the Trainer repaired it first; see check_feedback.json)" if repaired else "") + ".")

        # 2. A proposal round when nothing proposed is left to evaluate.
        usage, session_note = [], ""
        limit = ctx.params.get("max_rounds")
        if not state["queue"] and (limit is None or state["rounds"] < int(limit)):
            state["rounds"] += 1
            self._save(ctx, state)                    # the proposer reads the summary with this step's scores
            result, queued = self._propose(ctx, state, n)
            usage = result.usage
            if ctx.cancel is not None and ctx.cancel.is_set():
                # The Trainer keeps the previous adapter; the state stays as the last completed step left it.
                return StepResult("failed", "stopped by the Trainer during the proposal round", usage=usage)
            session_note = result.summary if result.status != "done" else ""
            lines.append(f"Round {state['rounds']}: {len(queued)} candidate(s) queued"
                         + (f" ({', '.join(queued)})" if queued else "")
                         + (f"; the proposer session {result.summary}" if session_note else "") + ".")
            for cid in state["order"]:
                c = state["candidates"][cid]
                if c["round"] == state["rounds"] and c["status"] == "invalid":
                    lines.append(f"- {cid} rejected: {'; '.join(c.get('problems') or [])[:300]}")

        # 3. Hand back the next candidate, or the best so far; a stored candidate changed since it was stored is
        # marked tampered and skipped.
        nxt = None
        while state["queue"] and nxt is None:
            cid = state["queue"].pop(0)
            nxt = cid if self._intact(ctx, state, cid) else None
        why = "the next proposed candidate"
        if nxt is None:
            nxt = self._next_best(ctx, state)
            why = "nothing left to evaluate: the best candidate so far, evaluated again"
        if nxt is None and state["handed_back"] and self._intact(ctx, state, state["handed_back"]):
            nxt = state["handed_back"]                # nothing scored yet: the same candidate again
        if nxt is None:
            return StepResult("failed", "no stored candidate is intact; the working copy is left as it was")
        for cid in state["order"]:
            c = state["candidates"][cid]
            if c["status"] == "tampered" and not c.get("reported"):
                lines.append(f"- {cid} skipped: {'; '.join(c['problems'])}")
                c["reported"] = True
        _install(store / nxt / "adapter", ctx.workspace.adapter)
        state["handed_back"] = nxt
        self._save(ctx, state)
        lines += ["", f"Handed back {nxt} ({why})."]
        (ctx.workspace.output / "report.md").write_text("\n".join(lines) + "\n")
        best = self._best(state)
        summary = f"next: {nxt}; best so far: {best} ({self._score(state['candidates'][best]):.3f})" if best \
            else f"next: {nxt}"
        return StepResult("done", summary[:300], usage=usage,
                          extra={"next": nxt, "rounds": state["rounds"], "candidates": len(state["order"])})

    def finalize(self, ctx) -> FinalResult:
        state = self._load(ctx)
        best = self._next_best(ctx, state)
        self._save(ctx, state)
        if best is None:
            super().finalize(ctx)
            return FinalResult(adapter=ctx.workspace.adapter, summary="no candidate was evaluated; current adapter")
        _install(ctx.workspace.state / CANDIDATES / best / "adapter", ctx.workspace.adapter)
        super().finalize(ctx)
        evaluated = [cid for cid in state["order"] if self._score(state["candidates"][cid]) is not None]
        return FinalResult(adapter=ctx.workspace.adapter,
                           summary=f"{best}, the best mean score on the search set "
                                   f"({self._score(state['candidates'][best]):.3f}; seed "
                                   f"{self._score(state['candidates']['seed']) or 0:.3f}; {len(evaluated)} evaluated)")

    # ---------- the proposer ----------
    def _propose(self, ctx: StepContext, state: dict, n: int) -> tuple[StepResult, list[str]]:
        k = int(ctx.params.get("k", 2))
        out = ctx.workspace.scratch / CANDIDATES
        if out.exists():
            shutil.rmtree(out)
        out.mkdir(parents=True)
        pending = ctx.workspace.scratch / PENDING
        pending.unlink(missing_ok=True)
        values = {"round": state["rounds"], "k": k, "candidates_dir": out, "pending": pending,
                  "summary": ctx.workspace.state / SUMMARY, "store": ctx.workspace.state / CANDIDATES}
        text = (Path(self.directory) / ctx.params.get("prompt", "PROPOSE.md")).read_text()
        for key, value in values.items():
            text = text.replace("{" + key + "}", str(value))
        prompt = ctx.workspace.scratch / "propose_prompt.md"
        prompt.write_text(text)
        result = super().step(dataclasses.replace(ctx, params={**ctx.params, "prompt": str(prompt)},
                                                  session=None))
        report = ctx.workspace.output / "report.md"
        if report.is_file():                          # the proposer's own account; the step's report comes after
            report.rename(ctx.workspace.output / "proposer_report.md")
        queued = []
        proposals = []
        # The candidates' directory and each candidate's root must be real directories inside the scratch
        # directory: the checks and copies below run outside the proposer's sandbox and would follow a link.
        scratch = ctx.workspace.scratch.resolve()
        out_ok = not out.is_symlink() and out.resolve() == scratch / CANDIDATES
        if pending.is_file():
            try:
                proposals = json.loads(pending.read_text()).get("candidates") or []
            except (ValueError, AttributeError):
                proposals = []
        for i, item in enumerate(proposals[:k] if isinstance(proposals, list) else []):
            item = item if isinstance(item, dict) else {}
            name = str(item.get("name") or "")
            # The id carries the round; a name that already starts with one (copied from the summary's ids) keeps
            # its directory, but the prefix is not doubled in the id.
            cid = f"r{state['rounds']:02d}_{re.sub(r'^r[0-9]+_', '', name)}"
            problems = []
            if not NAME.fullmatch(name):
                problems.append(f"name {name!r}: lowercase letters, digits, '_' and '-'")
            elif cid in state["candidates"]:
                problems.append(f"{cid} exists already")
            elif not (out / name).is_dir():
                problems.append(f"no directory {out / name}")
            source = out / name
            if not problems and (not out_ok or source.is_symlink() or source.resolve().parent != scratch / CANDIDATES):
                problems.append(f"{source} is a symbolic link or resolves outside {scratch / CANDIDATES}")
            if not problems:
                # This copy runs outside the proposer's sandbox: a link would bring in whatever it points at.
                links = sorted(str(p.relative_to(source)) for p in source.rglob("*") if p.is_symlink())
                if links:
                    problems.append(f"symbolic links are not allowed in a candidate: {', '.join(links[:5])}")
            if not problems:
                shutil.rmtree(source / ".hermit", ignore_errors=True)       # the template's, never the proposer's
                shutil.copytree(ctx.workspace.adapter / ".hermit", source / ".hermit")
                check = ctx.tools["check_spec"](path=str(source))
                if check.get("passed"):
                    _copy_tree(source, ctx.workspace.state / CANDIDATES / cid / "adapter")
                    digest = _digest(ctx.workspace.state / CANDIDATES / cid / "adapter")
                else:
                    problems += [f"{p.get('path')}: {p.get('problem')}" for p in check.get("issues") or []][:5]
            # An invalid proposal's key has a '#', which no candidate id can have.
            key = cid if NAME.fullmatch(name) and cid not in state["candidates"] \
                else f"r{state['rounds']:02d}_invalid#{i + 1}"
            state["candidates"][key] = {"round": state["rounds"], "hypothesis": str(item.get("hypothesis") or ""),
                                        "base": item.get("base"), "proposed_at_step": n,
                                        "status": "invalid" if problems else "queued",
                                        **({"problems": problems} if problems else {"digest": digest})}
            state["order"].append(key)
            if not problems:
                state["queue"].append(key)
                queued.append(key)
        (ctx.workspace.output / "proposer_prompt.md").write_text(text)
        return result, queued
