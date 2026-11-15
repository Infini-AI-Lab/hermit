"""Upstream Harness²'s benchmark seam (harness2.core.spec.BenchmarkAdapter) over one Trainer step of the framework.

One step holds one task. Executions go through the Trainer's run_agent tool, so they run the task's own pipeline,
are metered against the run's budget and are logged with the other tool calls; the reference execution is the
Trainer's own rollout of the step, which ran the starting adapter on this task. A run's judge result stays in this
object (`private`) and in the step's output (`scores.jsonl`), never where a session can read it: upstream reads it
only through `score`, after selection has committed. A run that is not the agent's result (status infra) has no
grade: `score` gives none for it.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import threading

from harness2.core import tree as T
from harness2.core.spec import BenchPrompts, BenchSpec, ChannelSpec

from hermit.agent.base.trajectory import Step, find_stream, log_steps
from hermit.agent.evolver.methods.harness2_upstream import surface as U

TRUNCATED = "…(+{n} chars — render truncation)"          # upstream's renderer marker (substrate/codex.py)
# A Harbor trial's curated log: what the runner collected of the session's home in the task's container (the CLI's
# last-message file), and the answer-only agent's record (its reply and answer)
COLLECTED_LAST = Path("agent") / "hermit-collect" / "last_message.txt"
ANSWER_RECORD = Path("agent") / "answer.json"


def _final_message(stream: Path) -> str:
    """A Claude Code session's final message, as its Base Agent reads the reply (Claude.outcome): every text block of
    its last turn, a message the CLI continued past its output limit joined where it was cut (the last result's
    `result` holds only the last block); none for a session the provider did not serve (Claude.served: no result, a
    failed one, or one of no turn)."""
    try:
        raw = stream.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    from hermit.agent.base.cli import Claude
    return Claude().outcome(raw, stream, None)["reply"]


def final_message(log: Path) -> str:
    """A Harbor trial's final message, from the session's own records in its curated log (hermit.agent.base.trajectory
    finds the stream: any CLI's agent/agent-<backend>.jsonl, or Harbor's own output of a trial recorded before that):
    Claude Code's reply as its Base Agent reads it (_final_message); another CLI's last message as its Base Agent reads
    it (the last-message file the runner collected, else the stream); else the answer-only agent's answer
    (agent/answer.json); else the last message of Harbor's own ATIF trajectory."""
    found = find_stream(log)
    if found is not None:
        stream, host = found
        if host == "claude":
            return _final_message(stream)
        from hermit.agent.base.cli import CLIS
        last = log / COLLECTED_LAST
        return CLIS[host].last_message(stream, last if last.is_file() else None)
    if (log / ANSWER_RECORD).is_file():
        try:
            record = json.loads((log / ANSWER_RECORD).read_text(encoding="utf-8", errors="replace"))
        except (ValueError, RecursionError):        # no JSON, or nested past the parser's depth
            record = None
        return str(record.get("answer") or record.get("reply") or "") if isinstance(record, dict) else ""
    replies = [s.text for s in log_steps(log) or [] if s.kind == "message" and s.role == "assistant"]
    return replies[-1] if replies else ""


def _action(step: Step) -> str:
    """A file change's or a tool call's summary (its JSON: the tool and what it was given) as upstream's [ACTION] line
    names a call: the tool, then its input."""
    try:
        data = json.loads(step.text)
    except ValueError:
        return step.text
    if not isinstance(data, dict) or not data.get("tool"):
        return step.text
    rest = {k: v for k, v in data.items() if k != "tool"}
    given = rest["input"] if set(rest) == {"input"} else rest
    return f"{data['tool']}: {json.dumps(given, ensure_ascii=False, default=str)}"


def render_steps(steps: list[Step], *, obs_trunc: int, max_chars: int, args_trunc: int = 2000) -> str:
    """A session's steps as hermit.agent.base.trajectory reads them (a CLI this method has no renderer of its own for,
    OpenCode, dsh or mini-swe-agent, or Harbor's own ATIF trajectory) in upstream's grammar: [ASSISTANT], [USER] and
    [REASONING] lines, an [ACTION] per command (`shell: ...`), file change or tool call with its [OBSERVATION]
    ([OBSERVATION ERROR] and the exit code for a command that failed), [ERROR]; an action's input clipped at
    `args_trunc`, each observation at `obs_trunc` with upstream's marker, the whole text at `max_chars`."""
    lines: list[str] = []
    for step in steps:
        if step.kind == "message":
            lines.append(f"[{'ASSISTANT' if step.role == 'assistant' else 'USER'}] {_clip(step.text, obs_trunc)}")
        elif step.kind == "reasoning":
            lines.append(f"[REASONING] {_clip(step.text, obs_trunc)}")
        elif step.kind == "command":
            failed = step.exit_code not in (None, 0)
            lines.append(f"[ACTION] shell: {_clip(step.command, args_trunc)}")
            lines.append(f"[OBSERVATION{' ERROR' if failed else ''}] {_clip(step.output, obs_trunc)}"
                         + (f" | exit_code={step.exit_code}" if failed else ""))
        elif step.kind in ("file_change", "tool_call"):
            lines.append(f"[ACTION] {_clip(_action(step), args_trunc)}")
            if step.output:
                lines.append(f"[OBSERVATION] {_clip(step.output, obs_trunc)}")
        elif step.kind == "error":
            lines.append(f"[ERROR] {_clip(step.text, 1000)}")
    rendered = "\n".join(lines).strip()
    if not rendered:
        return "(no parseable events in trajectory)"
    if len(rendered) > max_chars:
        rendered = rendered[:max_chars] + "\n" + TRUNCATED.format(n=len(rendered) - max_chars)
    return rendered


class Substrate:
    """The part of a substrate upstream's core uses: a name for its namespaces and the editable surface."""

    def __init__(self, layout) -> None:
        self.layout = layout
        self.name = layout.name

    def surface(self) -> T.SurfaceSpec:
        return self.layout.surface


def _redacted(text: str) -> str:
    """`text` without a key value of this process (the login's token, a binding's key: hermit/redact.py's rule): a
    command of the execution can read the token from its parent process, and the trajectory goes to the editor
    sessions and into the step's records."""
    from hermit.redact import redact_text
    return redact_text(text)


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
        """The task as the agent received it: its first user prompt (a Harbor task's instruction), else the task's own
        fields."""
        if self.task.get("harbor_task") and self.task.get("instruction"):    # a Harbor task: its instruction.md
            return str(self.task["instruction"])
        log = self._material_log()
        if log is not None and (log / "task.md").is_file():            # a CLI session: its prompt and task.md
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
        if run.get("status") == "error":   # it scores 0, as in the Trainer's records, and says why (not a wrong answer)
            self.private[run_id]["error"] = str(run.get("error") or "the execution failed")
        elif run.get("status") == "infra" or (self.private[run_id].get("extra") or {}).get("unscored"):
            # not the agent's result (the machine, the provider): no grade, as the Trainer's records give it none
            self.private[run_id] = {"score": None, "unscored": str(run.get("error") or "infra")}
        (out / "run.json").write_text(json.dumps({"run_id": run_id, **run}, ensure_ascii=False, indent=1,
                                                 default=str))
        with self._lock, open(self.ctx.workspace.output / "scores.jsonl", "a") as f:   # survives a stopped step
            failed = {k: self.private[run_id][k] for k in ("error", "unscored") if k in self.private[run_id]}
            f.write(json.dumps({"run_id": run_id, "answered": self.delivered(out),
                                "score": self.private[run_id].get("score"), **failed}) + "\n")
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
        for kept in (out / "log").rglob("*.jsonl"):     # the step's records keep no token a command printed
            if kept.is_file() and not kept.is_symlink():
                text = kept.read_text(encoding="utf-8", errors="replace")
                if _redacted(text) != text:
                    kept.write_text(_redacted(text), encoding="utf-8")

    @staticmethod
    def _run(run_dir: Path) -> dict:
        try:
            return json.loads((Path(run_dir) / "run.json").read_text())
        except (OSError, ValueError):
            return {}

    def delivered(self, run_dir: Path) -> bool:
        return bool(self.deliverable_text(run_dir).strip())

    def deliverable_text(self, run_dir: Path) -> str:
        """The answer as the editors see it (the token redacted, as in the trajectory); the judge scored the original.
        A Harbor trial delivers the state it leaves in the task's environment, which nothing outside it can show: its
        text is the session's final message, read from the session's own records, whichever CLI ran it
        (final_message; never the trial's output, which holds the verifier's reward)."""
        output = self._run(run_dir).get("output")
        if isinstance(output, dict) and output.get("harbor_task"):
            return _redacted(final_message(Path(run_dir) / "log"))
        if isinstance(output, dict):
            return _redacted(str(output.get("answer") or ""))
        text = "" if output is None else output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
        return _redacted(text)

    def deliverable_files(self, run_dir: Path) -> dict[str, str]:
        text = self.deliverable_text(run_dir)
        output = self._run(run_dir).get("output")
        name = "final_message.txt" if isinstance(output, dict) and output.get("harbor_task") else "answer.txt"
        return {name: text} if text.strip() else {}

    def trajectory(self, run_dir: Path, *, obs_trunc: int) -> str:
        """The execution's trajectory in upstream's grammar, from its session's stream (hermit.agent.base.trajectory
        finds it: the log's own, a Harbor trial's agent/agent-<backend>.jsonl, or Harbor's own output of the CLI):
        Codex's by upstream's own renderer, Claude Code's and Pi's by this method's, any other's (and Harbor's ATIF
        trajectory, without a stream) from the steps the common reader gives (render_steps)."""
        run, log = self._run(run_dir), Path(run_dir) / "log"
        limit = max(obs_trunc * 40, 2_000_000)
        stream, host = find_stream(log) or (None, None)
        if host == "codex":                                            # a Codex session: upstream's own renderer
            from harness2.substrate.codex import render_events
            text = render_events(stream, obs_trunc=obs_trunc, max_chars=limit)
        elif host == "claude":                                         # a Claude Code session, in upstream's grammar
            from hermit.agent.evolver.methods.harness2_upstream.render import render_claude_events
            text = render_claude_events(stream, obs_trunc=obs_trunc, max_chars=limit)
        elif host == "pi":                                             # a Pi session, in upstream's grammar
            from hermit.agent.evolver.methods.harness2_upstream.render import render_pi_events
            text = render_pi_events(stream, obs_trunc=obs_trunc, max_chars=limit)
        else:
            steps = log_steps(log)
            if steps is None:
                return f"(no trajectory recorded for {Path(run_dir).name})"
            text = render_steps(steps, obs_trunc=obs_trunc, max_chars=limit)
        output = run.get("output") if isinstance(run.get("output"), dict) else {}
        reason = (output or {}).get("fail_reason") or ""
        if reason:
            text = text.rstrip() + f"\n[ERROR] {_clip(reason, 1000)}"
        return _redacted(text.strip()) or "(no trajectory events recorded)"

    # --- the judge (upstream calls it only after selection) --------------------------------------------------------
    def score(self, run_dir: Path, task_id: str) -> dict | None:
        judged = self.private.get(self._run(run_dir).get("run_id", ""))
        if not judged or judged.get("score") is None:
            return None
        score = float(judged["score"])
        return {"n_passed": int(score >= 1.0), "n_criteria": 1, "all_pass": score >= 1.0, "metric": "score",
                "score": score}
