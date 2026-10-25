"""Upstream Harness²'s benchmark seam (harness2.core.spec.BenchmarkAdapter) over one Trainer step of the framework.

One step holds one task. Executions go through the Trainer's run_agent tool, so they run the task's own pipeline,
are metered against the run's budget and are logged with the other tool calls; the reference execution is the
Trainer's own rollout of the step, which ran the starting adapter on this task. A run's judge result stays in this
object (`private`) and in the step's output (`scores.jsonl`), never where a session can read it: upstream reads it
only through `score`, after selection has committed.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import threading

from harness2.core import tree as T
from harness2.core.spec import BenchPrompts, BenchSpec, ChannelSpec

from hermit.algorithms.evolvers.harness2_upstream import surface as U

TRUNCATED = "…(+{n} chars — render truncation)"          # upstream's renderer marker (substrate/codex.py)


class Substrate:
    """The part of a substrate upstream's core uses: a name for its namespaces and the editable surface."""

    def __init__(self, layout) -> None:
        self.layout = layout
        self.name = layout.name

    def surface(self) -> T.SurfaceSpec:
        return self.layout.surface


def _clip(text, limit: int) -> str:
    text = "" if text is None else str(text).replace("\x00", "")
    return text if limit <= 0 or len(text) <= limit else text[:limit] + TRUNCATED.format(n=len(text) - limit)


class FrameworkBench:
    """The BenchmarkAdapter for the step's one task (`record`, the Trainer's rollout of it)."""

    def __init__(self, ctx, record: dict, *, prompts: BenchPrompts, channel: ChannelSpec, runs_root: Path,
                 domain: str, name: str, obs_trunc: int, leak_globs: tuple[str, ...] = (), sources=None,
                 layout=None):
        self.ctx, self.record, self.task = ctx, record, dict(record.get("input") or {"id": record["id"]})
        self.tid = record["id"]
        self.domain, self.sources, self.obs_trunc = domain, sources, obs_trunc
        self.root = Path(runs_root)
        self.layout = layout if layout is not None else U.layout_of(ctx.workspace.adapter)
        self.spec = BenchSpec(name=name, runs_root=self.root, install_mode=self.layout.install_mode, prompts=prompts,
                              channels={"proposer": channel, "selector": channel},
                              domain_word=prompts.domain_word, leak_globs=tuple(leak_globs),
                              allow_subagents=self.layout.allow_subagents, obs_trunc=obs_trunc)
        self.substrate = Substrate(self.layout)
        self.private: dict[str, dict] = {}          # run_id -> judge result; read only by score()
        self._lock = threading.Lock()
        self._h0: Path | None = None
        self._sources: Path | None = None

    # --- the task -------------------------------------------------------------------------------------------------
    def tasks(self, split: str = "all") -> list[str]:
        return [self.tid]

    def domain_of(self, task_id: str) -> str:
        return self.domain

    def reference_domain_order(self) -> list[str]:
        return [self.domain]

    def task_prompt(self, task_id: str) -> str:
        """The task as the agent received it: its first user prompt, else the task's own fields."""
        log = self._material_log()
        if log is not None and (log / "task.md").is_file():            # a Codex session: its prompt and task.md
            prompt = (log / "prompt.txt").read_text(errors="replace") if (log / "prompt.txt").is_file() else ""
            return (prompt.strip() + "\n\n" if prompt.strip() else "") + "# task.md\n\n" + \
                (log / "task.md").read_text(errors="replace")
        for message in self.record.get("messages") or []:
            if message.get("role") == "user" and message.get("content"):
                return str(message["content"])
        return json.dumps({k: v for k, v in self.task.items() if k != "id"}, ensure_ascii=False, indent=1)

    def task_files(self, task_id: str) -> Path | None:
        """The task's source material, gathered once: the INV and DOM stages build their evidence concurrently."""
        if self.sources is None:
            return None
        with self._lock:
            if self._sources is None:
                self._sources = self.sources(dict(self.task), self.root / "_sources" / self.tid)
        return self._sources

    # --- the harness ----------------------------------------------------------------------------------------------
    def base_harness(self) -> Path:
        """H0: the starting adapter's tree (the Trainer never changes the adapter in this protocol)."""
        with self._lock:
            if self._h0 is None:
                self._h0 = T.write_tree(self.layout.surface, self.layout.from_adapter(self.ctx.workspace.adapter),
                                        self.root / "_h0")
        return self._h0

    def validate_tree(self, tree: T.Tree) -> None:
        """The template's whole check on the adapter this tree becomes; every problem in one message, which upstream
        hands back to the session as its retry feedback."""
        off = self.layout.off_surface(tree)
        problems = [f"{rel}: this agent cannot use it ({self.layout.off_surface_hint})" for rel in off]
        with self._lock:
            probe = self.layout.to_adapter({k: v for k, v in tree.items() if k not in off},
                                           self.ctx.workspace.adapter, self.root / "_validate")
            result = self.ctx.tools["check_spec"](path=str(probe))
        for issue in result.get("issues") or []:
            rel = self.layout.upstream_path(issue.get("path") or "")
            problems.append(f"{rel or 'the harness'}: {issue.get('problem')}")
        if problems:
            raise ValueError("; ".join(problems))

    # --- executions -----------------------------------------------------------------------------------------------
    def execute(self, task_id: str, harness_dir: Path | None, run_id: str, *, timeout: int | None = None) -> Path:
        if task_id != self.tid:
            raise ValueError(f"this step runs {self.tid}, not {task_id}")
        out = self.root / "executions" / run_id
        if (out / "run.json").is_file():
            return out
        if out.exists():
            shutil.rmtree(out)
        out.mkdir(parents=True)
        if harness_dir is None and self.record.get("status") == "ok":
            # The reference execution: the Trainer's rollout of the starting adapter on this task.
            run = {k: self.record.get(k) for k in ("status", "error", "output", "cost_usd", "wall_s")}
            self.private[run_id] = dict(self.record.get("judge") or {})
            self._keep_log(self._material_log(), out)
        else:
            adapter = self.ctx.workspace.adapter if harness_dir is None else \
                self.layout.to_adapter(T.read_tree(self.layout.surface, harness_dir), self.ctx.workspace.adapter,
                                       out / "adapter")
            result = self.ctx.tools["run_agent"](task_ids=[task_id], adapter=str(adapter))["results"][0]
            run = {k: result.get(k) for k in ("status", "error", "output", "cost_usd", "wall_s")}
            self.private[run_id] = dict(result.get("judge") or {})
            self._keep_log(Path(result["log"]) if result.get("log") else None, out)
        (out / "run.json").write_text(json.dumps({"run_id": run_id, **run}, ensure_ascii=False, indent=1,
                                                 default=str))
        with self._lock, open(self.ctx.workspace.output / "scores.jsonl", "a") as f:   # survives a stopped step
            f.write(json.dumps({"run_id": run_id, "answered": self.delivered(out),
                                "score": self.private[run_id].get("score")}) + "\n")
        return out

    def _material_log(self) -> Path | None:
        rel = self.record.get("trajectory")
        if not rel:
            return None
        log = self.ctx.workspace.learning_material / "current" / rel
        return log if log.exists() else None

    @staticmethod
    def _keep_log(log: Path | None, out: Path) -> None:
        if log is None or not log.exists():
            return
        if log.is_dir():
            shutil.copytree(log, out / "log", symlinks=True)
        else:
            (out / "log").mkdir()
            shutil.copy2(log, out / "log" / log.name)

    @staticmethod
    def _run(run_dir: Path) -> dict:
        try:
            return json.loads((Path(run_dir) / "run.json").read_text())
        except (OSError, ValueError):
            return {}

    def delivered(self, run_dir: Path) -> bool:
        return bool(self.deliverable_text(run_dir).strip())

    def deliverable_text(self, run_dir: Path) -> str:
        output = self._run(run_dir).get("output")
        if isinstance(output, dict):
            return str(output.get("answer") or "")
        return "" if output is None else output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)

    def deliverable_files(self, run_dir: Path) -> dict[str, str]:
        text = self.deliverable_text(run_dir)
        return {"answer.txt": text} if text.strip() else {}

    def trajectory(self, run_dir: Path, *, obs_trunc: int) -> str:
        run, log = self._run(run_dir), Path(run_dir) / "log"
        if (log / "agent-codex.jsonl").is_file():                      # a Codex session: upstream's own renderer
            from harness2.substrate.codex import render_events
            text = render_events(log / "agent-codex.jsonl", obs_trunc=obs_trunc, max_chars=max(obs_trunc * 40, 2_000_000))
            output = run.get("output") if isinstance(run.get("output"), dict) else {}
            reason = (output or {}).get("fail_reason") or ""
            if reason:
                text = text.rstrip() + f"\n[ERROR] {_clip(reason, 1000)}"
            return text.strip() or "(no trajectory events recorded)"
        return f"(no trajectory recorded for {Path(run_dir).name})"

    # --- the judge (upstream calls it only after selection) --------------------------------------------------------
    def score(self, run_dir: Path, task_id: str) -> dict | None:
        judged = self.private.get(self._run(run_dir).get("run_id", ""))
        if not judged or judged.get("score") is None:
            return None
        score = float(judged["score"])
        return {"n_passed": int(score >= 1.0), "n_criteria": 1, "all_pass": score >= 1.0, "metric": "score",
                "score": score}
