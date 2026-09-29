"""SkillOpt (Yang et al., 2026; github.com/microsoft/SkillOpt) as an optimizer
Evolver.

The upstream trainer does the optimizing: ReflACTTrainer.train() runs its
rollout batches, failure/success minibatch reflection, hierarchical patch
merging, edit ranking under the textual learning rate, bounded
add/delete/replace edits, the strict held-out selection gate, the
rejected-edit buffer, the epoch-wise slow update and the optimizer-side meta
skill, all unchanged. This module supplies what SkillOpt asks a new
environment to supply, an EnvAdapter:

- the skill document is one text component of the adapter (ADAPTER.md for
  the prompt-only architecture), deployed the framework's way (it becomes
  the adapter's context in the host's AGENTS.md) rather than as SkillOpt's
  own .agents/skills/ file;
- rollout() materializes the skill as an adapter and runs the batch through
  the Environment, then writes each trajectory where SkillOpt's reflection
  reads it (predictions/<id>/conversation.json);
- the split loader is SkillOpt's own SplitDataLoader over the environment's
  train and val tasks (ids and task text only; answers stay in the
  environment, which reveals them only as train feedback).

SkillOpt's out_root is the session's Training State (history.json,
runtime_state.json, skills/, steps/, best_skill.md); the trainer resumes from
it. The optimizer model is SkillOpt's own openai_compatible backend pointed at
the session's optimizer model. The trainer keeps process-global backend state,
so run one SkillOpt session per process.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import itertools
import json
import os
from pathlib import Path
import threading

from harnessadapter.optimize.environment import Task
from harnessadapter.optimize.plugin import OptimizeContext, Outcome


def base_config_path(explicit: str = "") -> Path:
    """SkillOpt's configs/_base_/default.yaml: the defaults every one of its
    benchmark configs inherits. configs/ ships with the source tree, not the
    wheel, so install SkillOpt from a checkout or set SKILLOPT_HOME."""
    import skillopt
    candidates = [explicit, os.environ.get("SKILLOPT_HOME", "") and str(Path(os.environ["SKILLOPT_HOME"]) / "configs/_base_/default.yaml"),
                  str(Path(skillopt.__file__).resolve().parents[1] / "configs/_base_/default.yaml")]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    raise FileNotFoundError("SkillOpt's configs/_base_/default.yaml was not found: install SkillOpt from a "
                            "source checkout (pip install -e) or set SKILLOPT_HOME to one")


def build_config(settings: dict, *, optimizer_model: str, seed_path: Path, out_root: Path, split_dir: Path) -> dict:
    """SkillOpt's default recipe with this session's settings on top."""
    from skillopt.config import flatten_config, load_config
    cfg = load_config(str(base_config_path(settings.get("base_config", ""))))
    cfg["model"].update(backend="openai_compatible", optimizer=optimizer_model, target="harnessadapter-environment",
                        optimizer_backend="openai_compatible", target_backend="openai_compatible",
                        reasoning_effort=settings["reasoning_effort"])
    cfg["train"].update(num_epochs=int(settings["num_epochs"]), batch_size=int(settings["batch_size"]),
                        accumulation=int(settings["accumulation"]), seed=int(settings["seed"]), train_size=0)
    cfg["gradient"].update(minibatch_size=int(settings["minibatch_size"]), merge_batch_size=int(settings["merge_batch_size"]),
                           analyst_workers=int(settings["analyst_workers"]), failure_only=bool(settings["failure_only"]))
    cfg["optimizer"].update(learning_rate=int(settings["learning_rate"]), min_learning_rate=int(settings["min_learning_rate"]),
                            lr_scheduler=settings["lr_scheduler"], skill_update_mode=settings["skill_update_mode"],
                            use_slow_update=bool(settings["use_slow_update"]),
                            slow_update_samples=int(settings["slow_update_samples"]),
                            use_meta_skill=bool(settings["use_meta_skill"]))
    # The held-out test split is the framework's, evaluated identically for
    # every method: SkillOpt's split has no test items. eval_test stays on,
    # as upstream: it also guards the final val pass that can promote the
    # final (slow-updated) skill to best; its test rollouts are empty.
    cfg["evaluation"].update(use_gate=bool(settings["use_gate"]), eval_test=True, sel_env_num=0, test_env_num=0)
    cfg["env"] = {"name": "harnessadapter", "skill_init": str(seed_path), "out_root": str(out_root),
                  "split_mode": "split_dir", "split_dir": str(split_dir), "split_seed": int(settings["seed"])}
    return flatten_config(cfg)


def write_splits(split_dir: Path, splits: dict[str, list[Task]]) -> None:
    for name in ("train", "val", "test"):
        items = [{"id": t.id, "question": t.prompt, "task_type": t.kind or "task", "split": t.split}
                 for t in splits.get(name, [])]
        (split_dir / name).mkdir(parents=True, exist_ok=True)
        (split_dir / name / "items.json").write_text(json.dumps(items, ensure_ascii=False, indent=1) + "\n")


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = limit * 2 // 5
    return text[:head] + f" [... {len(text) - limit} characters left out ...] " + text[-(limit - head):]


def budget_steps(steps: list[dict], limit: int) -> list[dict]:
    """Keep the first and last steps within about `limit` characters (0: no
    limit, SkillOpt's own choice: its reflection shows full trajectories).
    A single step larger than its share is clipped, so the ending survives."""
    size = lambda step: len(json.dumps(step, ensure_ascii=False))
    if limit <= 0 or sum(size(s) for s in steps) <= limit:
        return steps
    share = max(200, limit // 8)
    steps = [{k: _clip(v, share) if isinstance(v, str) else v for k, v in s.items()} for s in steps]
    head, tail, used = [], [], 0
    for step in steps:                       # beginning: the approach
        if used + size(step) > limit * 2 // 5:
            break
        head.append(step)
        used += size(step)
    for step in reversed(steps[len(head):]):  # end: how it finished
        if used + size(step) > limit:
            break
        tail.insert(0, step)
        used += size(step)
    omitted = len(steps) - len(head) - len(tail)
    return head + ([{"role": "system", "content": f"[{omitted} steps of the trajectory left out]"}] if omitted else []) + tail


def conversation(rollout, prompt: str, limit: int = 0) -> list[dict]:
    """The trajectory in the record shapes SkillOpt's fmt_trajectory renders."""
    steps = [dict(s) for s in rollout.steps if isinstance(s, dict)]
    if not steps and rollout.trace:
        steps = [{"role": "assistant", "content": rollout.trace}]
    steps = budget_steps(steps, limit)
    return ([{"role": "user", "content": prompt}] + steps +
            [{"role": "assistant", "content": f"Final answer: {rollout.output or '(none)'}"},
             {"role": "system", "content": f"Run status: {rollout.status}; score: {rollout.score:g}"}])


def make_env_adapter(context: OptimizeContext, settings: dict, component: str):
    from skillopt.datasets.base import SplitDataLoader
    from skillopt.envs.base import EnvAdapter

    class TaskSplitLoader(SplitDataLoader):
        """SkillOpt's split loader over the split directory written above."""

    class HarnessAdapterSkillOptEnv(EnvAdapter):
        def __init__(self):
            self.analyst_workers = int(settings["analyst_workers"])
            self.failure_only = bool(settings["failure_only"])
            self.minibatch_size = int(settings["minibatch_size"])
            self.edit_budget = int(settings["learning_rate"])
            self.prompt_env = str(settings["prompt_env"] or "")
            self.trace_chars = int(settings["trace_chars"])
            self.dataloader = TaskSplitLoader(split_mode="split_dir", seed=int(settings["seed"]))
            self._tasks = {t.id: t for split in ("train", "val") for t in context.environment.tasks(split)}
            self._counter = itertools.count(1)
            self._lock = threading.Lock()

        @property
        def _env_name(self) -> str:
            # "officeqa" selects SkillOpt's own OfficeQA analyst prompts
            # (its failure taxonomy); empty selects the generic prompts.
            return self.prompt_env

        def setup(self, cfg: dict) -> None:
            super().setup(cfg)
            self.dataloader.setup(cfg)

        def get_dataloader(self):
            return self.dataloader

        def build_env_from_batch(self, batch, **kwargs):
            return list(batch.payload or [])

        def build_train_env(self, batch_size: int, seed: int, **kwargs):
            return self.build_env_from_batch(self.dataloader.build_train_batch(batch_size=batch_size, seed=seed))

        def build_eval_env(self, env_num: int, split: str, seed: int, **kwargs):
            return self.build_env_from_batch(self.dataloader.build_eval_batch(env_num=env_num, split=split, seed=seed))

        def rollout(self, env_manager, skill_content: str, out_dir: str, **kwargs) -> list[dict]:
            items = list(env_manager)
            where = Path(out_dir)
            results_path = where / "results.jsonl"
            # As SkillOpt's own run_batch does: a resumed step keeps the
            # episodes it already paid for and runs only the missing ones,
            # provided they were run with this same skill text.
            skill_sha = hashlib.sha256(skill_content.encode()).hexdigest()
            done = {}
            if results_path.is_file():
                for line in results_path.read_text().splitlines():
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if (row.get("skill_sha256") == skill_sha and not row.get("infra")
                            and (where / "predictions" / str(row.get("id")) / "conversation.json").is_file()):
                        done[str(row["id"])] = row
            pending = [self._tasks[str(item["id"])] for item in items if str(item["id"]) not in done]
            fresh = {}
            if pending:
                with self._lock:
                    number = next(self._counter)
                adapter = context.store.path({component: skill_content})
                label = f"skillopt/{number:04d}-{where.parent.name}-{where.name}"
                rollouts = context.environment.evaluate(adapter, pending, label=label)
                for task, rollout in zip(pending, rollouts):
                    if rollout.infra:
                        continue
                    hard = int(rollout.score >= 1.0 - 1e-9)
                    record = where / "predictions" / task.id
                    record.mkdir(parents=True, exist_ok=True)
                    (record / "conversation.json").write_text(json.dumps(
                        conversation(rollout, task.prompt, self.trace_chars), ensure_ascii=False, indent=1) + "\n")
                    fresh[task.id] = {"id": task.id, "hard": hard, "soft": float(rollout.score),
                                      "fail_reason": "" if hard else (rollout.feedback or f"status {rollout.status}"),
                                      "task_description": task.prompt, "task_type": task.kind or "task",
                                      "n_turns": len(rollout.steps), "agent_ok": rollout.status == "completed",
                                      "status": rollout.status, "infra": False,
                                      "predicted_answer": rollout.output, "prediction": rollout.output,
                                      "skill_sha256": skill_sha}
                # Every finished episode is saved before any stop, so a resume
                # pays only for what is missing.
                where.mkdir(parents=True, exist_ok=True)
                with results_path.open("a") as out:
                    out.writelines(json.dumps(fresh[t.id], ensure_ascii=False) + "\n" for t in pending if t.id in fresh)
                lost = [r.task_id for r in rollouts if r.infra]
                if lost:
                    # Still lost after the session's re-runs. A 0 here would
                    # bias the selection gate or read as a regression in the
                    # slow update, so stop; resumed from its Training State,
                    # the session runs only these episodes again.
                    raise RuntimeError(f"Episodes lost to the infrastructure after re-runs: {lost[:5]}; "
                                       "resume the session from its training_state")
            return [done.get(str(item["id"])) or fresh[str(item["id"])] for item in items]

        def reflect(self, results, skill_content, out_dir, **kwargs):
            # Upstream reflection, unchanged, over the episodes that are
            # evidence about the skill.
            return super().reflect([r for r in results if not r.get("infra")], skill_content, out_dir, **kwargs)

        def get_task_types(self) -> list[str]:
            return sorted({t.kind or "task" for t in self._tasks.values()})

    return HarnessAdapterSkillOptEnv()


def relocate_runtime_state(state: Path) -> None:
    """SkillOpt records absolute skill paths in runtime_state.json and opens
    them on resume. A resumed session works on a copy of that state, so point
    them into the copy."""
    path = state / "runtime_state.json"
    if not path.is_file():
        return
    runtime = json.loads(path.read_text())
    for key in ("current_skill_path", "best_skill_path"):
        old = runtime.get(key)
        if not old:
            continue
        parts = Path(old).parts
        anchor = next((i for i in range(len(parts) - 1, -1, -1) if parts[i] in ("skills", "best_skill.md")), None)
        if anchor is not None:
            runtime[key] = str(state.joinpath(*parts[anchor:]))
    path.write_text(json.dumps(runtime, indent=2, ensure_ascii=False) + "\n")


class SkillOptPlugin:
    name = "skillopt"

    def run(self, context: OptimizeContext) -> Outcome:
        from skillopt.engine.trainer import ReflACTTrainer
        from skillopt.model import openai_compatible_backend

        s = context.settings
        try:
            version = importlib.metadata.version("skillopt")
        except importlib.metadata.PackageNotFoundError:
            version = "unknown (imported from source without metadata)"
        if len(context.surface.components) != 1:
            raise ValueError("SkillOpt optimizes one skill document; give the surface exactly one component")
        component = context.surface.components[0]
        if not hasattr(context.lm, "serve"):
            raise ValueError("SkillOpt drives its own OpenAI-compatible client; pass a ChatLM optimizer model")
        lm = context.lm
        state = context.state_dir
        seed_path = state / "seed_skill.md"
        if not seed_path.exists():
            seed_path.write_text(context.seed()[component])
        relocate_runtime_state(state)
        split_dir = state / "splits"
        write_splits(split_dir, {"train": context.environment.tasks("train"), "val": context.environment.tasks("val")})
        cfg = build_config(s, optimizer_model=lm.model, seed_path=seed_path, out_root=state, split_dir=split_dir)
        env_adapter = make_env_adapter(context, s, component)
        context.log(f"[skillopt] train={len(context.environment.tasks('train'))} val={len(context.environment.tasks('val'))} "
                    f"epochs={cfg['num_epochs']} batch={cfg['batch_size']} lr={cfg['edit_budget']} component={component}")
        # SkillOpt's own openai_compatible client talks to the session's
        # model through a local endpoint: the same model settings (reasoning
        # effort included, which that backend would not send), the same
        # ledger, and no key in SkillOpt's environment variables.
        with lm.serve() as endpoint:
            openai_compatible_backend.configure_openai_compatible(
                optimizer_base_url=endpoint, optimizer_api_key="local-endpoint", optimizer_model=lm.model,
                max_tokens=lm.max_tokens, timeout_seconds=lm.timeout * lm.retries + 900)
            ReflACTTrainer(cfg, env_adapter).train()
        summary_path = state / "summary.json"
        trained = json.loads(summary_path.read_text()) if summary_path.is_file() else {}
        best = (state / "best_skill.md").read_text()
        history = json.loads((state / "history.json").read_text()) if (state / "history.json").is_file() else []
        summary = {"algorithm": "skillopt", "skillopt_version": version,
                   # Sent by the session's endpoint (SkillOpt's own backend would not).
                   "optimizer_reasoning_effort_sent": getattr(lm, "reasoning_effort", None),
                   "seed_val_score": trained.get("baseline_selection_hard"),
                   "selected_val_score": trained.get("best_selection_hard"),
                   "final_val_score": trained.get("final_selection_hard"),
                   "best_step": trained.get("best_step"), "best_origin": trained.get("best_origin"),
                   "total_steps": trained.get("total_steps"), "total_accepts": trained.get("total_accepts"),
                   "total_rejects": trained.get("total_rejects"), "total_skips": trained.get("total_skips"),
                   "epoch_stats": trained.get("epoch_stats"), "token_summary": trained.get("token_summary"),
                   "steps": [{k: h.get(k) for k in ("epoch", "step", "action", "rollout_hard", "n_failure_patches", "n_success_patches",
                                                        "n_edits_ranked", "edit_budget", "selection_hard", "current_score", "best_score")}
                             for h in history]}
        return Outcome(candidate={component: best}, summary=summary)
