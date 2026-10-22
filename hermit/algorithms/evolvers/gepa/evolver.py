"""GEPAEvolver: GEPA (Agrawal et al., ICLR 2026; github.com/gepa-ai/gepa), one GEPA iteration per training step.

The step's rollouts are GEPA's run of the selected parent on a training minibatch, with trajectories
(data.step_size is GEPA's reflection minibatch size). The step does the rest of the iteration:

1. reflect: GEPA's instruction-proposal prompt, over the parent's trajectories and the judge's feedback, proposes a
   new text for one learnable file (round robin over `files`);
2. run the child on the same minibatch (run_agent) and keep it only if its total score is higher;
3. score a kept child on the validation tasks and add it to the candidate pool;
4. select the next parent from the pool (GEPA's Pareto-front sampling, or the best mean) and hand it back, so the
   next step's rollouts run it on the next minibatch.

finalize hands back the candidate with the best mean validation score.
Training State (state/gepa.json): the pool (texts, parent, validation score per task) and one record per step.
"""
from __future__ import annotations

import json
from pathlib import Path
import random
import shutil

from hermit.evolver.base import Evolver, FinalResult, StepContext, StepResult


def clip_middle(text: str, limit: int) -> str:
    """Keep the beginning (the approach) and the end (how it finished)."""
    if limit <= 0 or len(text) <= limit:
        return text
    head = limit * 2 // 5
    return text[:head] + f"\n[... {len(text) - limit} characters left out ...]\n" + text[-(limit - head):]


def task_text(task: dict) -> str:
    fields = {k: v for k, v in task.items() if k not in ("id", "task_id")}
    if len(fields) == 1:
        return str(next(iter(fields.values())))
    return json.dumps(fields, ensure_ascii=False)


def trajectory_text(record: dict, step_dir: Path) -> str:
    """The run as text: the messages the pipeline returned, else its log files. The system prompt (the instructions
    under test) and the first user message (the task) are left out: the reflection prompt shows both already."""
    lines, task_seen = [], False
    for m in record.get("messages") or []:
        if m.get("role") == "system":
            continue
        if m.get("role") == "user" and not task_seen:
            task_seen = True
            continue
        content = m.get("content")
        if isinstance(content, list):
            content = " ".join(part.get("text", "") for part in content if isinstance(part, dict))
        if content:
            lines.append(f"[{m.get('role')}] {content}")
        for call in m.get("tool_calls") or []:
            fn = call.get("function") or {}
            lines.append(f"[tool call] {fn.get('name')}({fn.get('arguments')})")
    if not lines and record.get("trajectory"):
        root = step_dir / record["trajectory"]
        for f in sorted(p for p in root.rglob("*") if p.is_file()):
            lines.append(f.read_text(errors="replace"))
    return "\n".join(lines)


class GEPAEvolver(Evolver):
    def setup(self, ctx) -> None:
        try:
            import gepa  # noqa: F401
        except ImportError:
            raise RuntimeError("the gepa Evolver needs the GEPA library: pip install -e '.[gepa]'") from None

    def step(self, ctx: StepContext) -> StepResult:
        from gepa.strategies.instruction_proposal import InstructionProposalSignature, parse_proposal

        p = ctx.params
        strategy = p.get("candidate_selection_strategy", "pareto")
        criterion = p.get("acceptance_criterion", "strict_improvement")
        if strategy not in ("pareto", "current_best") or criterion not in ("strict_improvement", "improvement_or_equal"):
            raise ValueError(f"unknown candidate_selection_strategy {strategy!r} or acceptance_criterion {criterion!r}")
        if "llm" not in ctx.tools:
            return StepResult("failed", "gepa needs the run's llm tool: set `llm` in run.yaml")
        if ctx.access.validation is None:
            return StepResult("failed", "gepa needs validation tasks: set data.val")
        files = list(p.get("files") or ["ADAPTER.md"])
        n = ctx.progress.step if ctx.progress else 0
        state_file = ctx.workspace.state / "gepa.json"
        state = json.loads(state_file.read_text()) if state_file.exists() else {"pool": [], "steps": []}
        pool = state["pool"]
        adapter = ctx.workspace.adapter
        texts = {f: (adapter / f).read_text() if (adapter / f).exists() else "" for f in files}
        val_ids = [json.loads(l)["id"] for l in (ctx.access.validation / "tasks.jsonl").read_text().splitlines() if l]

        # The parent: the candidate these rollouts ran. The seed is scored on validation first, as in GEPA.
        parent = next((i for i, c in enumerate(pool) if c["files"] == texts), None)
        if parent is None:
            pool.append({"files": texts, "parent": None, "step": n, "val": self._scores(ctx, val_ids)})
            parent = len(pool) - 1
        current = ctx.workspace.learning_material / "current"
        records = [json.loads(f.read_text()) for f in sorted((current / "tasks").glob("*.json"))]
        minibatch = {r["id"]: float(r["judge"]["score"]) for r in records}
        record = {"step": n, "parent": parent, "minibatch": minibatch}

        perfect = float(p.get("perfect_score", 1.0))
        if p.get("skip_perfect_score", True) and all(s >= perfect for s in minibatch.values()):
            record["outcome"] = "skipped: the parent is perfect on this minibatch"
        else:
            name = files[pool[parent].get("next_file", 0) % len(files)]      # GEPA's round robin, per candidate
            pool[parent]["next_file"] = pool[parent].get("next_file", 0) + 1
            dataset = [{"Inputs": {"Task given to the agent": task_text(r["input"])},
                        "Generated Outputs": {
                            "Final answer": str(r.get("output") or "(no final answer)"),
                            "Run status": r.get("status", ""),
                            "Agent trajectory": clip_middle(trajectory_text(r, current),
                                                            int(p.get("trace_chars", 12000)))},
                        "Feedback": f"Score {r['judge']['score']:g}. {r['judge'].get('feedback', '')}"}
                       for r in records]
            prompt = InstructionProposalSignature.prompt_renderer({
                "current_instruction_doc": texts[name], "dataset_with_feedback": dataset,
                "prompt_template": p.get("reflection_prompt_template") or None})
            messages = [{"role": "user", "content": prompt}]
            sampling = {k: p[k] for k in ("max_tokens", "temperature") if p.get(k) is not None}
            replies = []
            new_text = None
            error = None
            for _ in range(2):      # a reasoning model can spend its tokens thinking: ask once more
                try:
                    reply = ctx.tools["llm"](messages=messages, **sampling)
                except Exception as exc:                    # noqa: BLE001 — as GEPA, the iteration is skipped
                    error = f"{type(exc).__name__}: {exc}"
                    break
                content = reply["content"] or ""
                replies.append(content)
                # A reply cut off at max_tokens is used only if it already holds a complete ``` block (as GEPA).
                complete = content.find("```") + 3 < content.rfind("```")
                new_text = None if not content or (reply.get("finish_reason") == "length" and not complete) else \
                    parse_proposal(InstructionProposalSignature, content).text
                if new_text:
                    break
                messages += [{"role": "assistant", "content": content},
                             {"role": "user", "content": "Reply now with only the new instructions within ``` blocks."}]
            for i, text in enumerate(replies):
                (ctx.workspace.output / f"reflection.{i}.txt").write_text(text)
            (ctx.workspace.output / "reflection_prompt.md").write_text(prompt)
            record["file"] = name
            if error:
                record["outcome"] = f"skipped: the reflection call failed ({error})"
            elif not new_text or new_text.strip() == texts[name].strip():
                record["outcome"] = "skipped: the model proposed no new text"
            else:
                child = {**texts, name: new_text.strip() + "\n"}
                candidate = self._write(ctx.workspace.scratch / "candidate", adapter, child)
                check = ctx.tools["check_spec"](path=str(candidate))
                if not check["passed"]:
                    record["outcome"] = "rejected: " + "; ".join(i["problem"] for i in check["issues"][:3])
                else:
                    scores = self._scores(ctx, list(minibatch), candidate)
                    record["child_minibatch"] = scores
                    better = sum(scores.values()) > sum(minibatch.values())
                    if criterion == "improvement_or_equal":
                        better = sum(scores.values()) >= sum(minibatch.values())
                    if not better:
                        record["outcome"] = (f"rejected: {sum(scores.values()):g} <= {sum(minibatch.values()):g} "
                                             "on the minibatch")
                    else:
                        pool.append({"files": child, "parent": parent, "step": n,
                                     "val": self._scores(ctx, val_ids, candidate)})
                        record["outcome"] = (f"accepted as candidate {len(pool) - 1}, validation mean "
                                             f"{_mean(pool[-1]['val']):.3f}")

        if ctx.cancel is not None and ctx.cancel.is_set():
            return StepResult("failed", "stopped by the Trainer")
        # The next parent, handed back for the next step's rollouts.
        rng = random.Random(int(p.get("seed", 0)) * 100003 + n)
        means = [_mean(c["val"]) for c in pool]
        if strategy == "current_best":
            nxt = max(range(len(pool)), key=lambda i: (means[i], -i))
        else:
            from gepa.gepa_utils import select_program_candidate_from_pareto_front
            front = {}
            for tid in val_ids:
                best = max(c["val"].get(tid, 0.0) for c in pool)
                front[tid] = {i for i, c in enumerate(pool) if c["val"].get(tid, 0.0) == best}
            nxt = select_program_candidate_from_pareto_front(front, means, rng)
        self._write(adapter, adapter, pool[nxt]["files"])
        record["next_parent"] = nxt
        state["steps"].append(record)
        state_file.write_text(json.dumps(state, ensure_ascii=False, indent=1))
        summary = f"{record['outcome']}; next parent: candidate {nxt} of {len(pool)}"
        (ctx.workspace.output / "report.md").write_text(summary + "\n")
        return StepResult("done", summary[:300], extra={"pool_size": len(pool), "parent": parent, "next": nxt})

    def finalize(self, ctx) -> FinalResult:
        state_file = ctx.workspace.state / "gepa.json"
        pool = json.loads(state_file.read_text())["pool"] if state_file.exists() else []
        if not pool:
            return FinalResult(adapter=ctx.workspace.adapter, summary="no GEPA iteration ran; current adapter")
        best = max(range(len(pool)), key=lambda i: (_mean(pool[i]["val"]), -i))
        self._write(ctx.workspace.adapter, ctx.workspace.adapter, pool[best]["files"])
        return FinalResult(adapter=ctx.workspace.adapter, summary=f"candidate {best} of {len(pool)}, the best "
                           f"validation mean ({_mean(pool[best]['val']):.3f}; seed {_mean(pool[0]['val']):.3f})")

    @staticmethod
    def _scores(ctx, task_ids: list, adapter: Path | None = None) -> dict:
        args = {"task_ids": task_ids} | ({"adapter": str(adapter)} if adapter else {})
        return {r["id"]: float(r["judge"]["score"]) for r in ctx.tools["run_agent"](**args)["results"]}

    @staticmethod
    def _write(target: Path, source: Path, files: dict) -> Path:
        """`source` with `files` in place, at `target`."""
        if target.resolve() != source.resolve():
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(source, target, symlinks=True)
        for name, text in files.items():
            (target / name).parent.mkdir(parents=True, exist_ok=True)
            (target / name).write_text(text)
        return target


def _mean(scores: dict) -> float:
    return sum(scores.values()) / len(scores) if scores else 0.0
