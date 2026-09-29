"""SkillOpt (Yang et al., 2026; github.com/microsoft/SkillOpt) as an optimizer
Evolver.

The upstream trainer does the optimizing: ReflACTTrainer.train() runs its
rollout batches, failure/success minibatch reflection, hierarchical patch
merging, edit ranking under the textual learning rate, bounded
add/delete/replace edits, the strict selection gate on val, the
rejected-edit buffer, the epoch-wise slow update and the meta skill, all
unchanged. This file supplies what SkillOpt asks a new environment to supply,
an EnvAdapter:

- the skill is one text file of the adapter (ADAPTER.md for prompt-only),
  deployed the framework's way (the adapter's context in the host's
  AGENTS.md) rather than as SkillOpt's own .agents/skills/ file;
- rollout() runs the batch with the skill deployed (context.evaluate) and
  writes each trajectory where SkillOpt's reflection reads it
  (predictions/<id>/conversation.json);
- the split loader is SkillOpt's own SplitDataLoader over the train and val
  tasks (ids and task text only; answers stay in the environment, which
  reveals them only as train feedback).

SkillOpt's out_root is the Training State (history.json, runtime_state.json,
skills/, steps/, best_skill.md); the trainer resumes from it. Its own
openai_compatible client reaches the Evolver model through context.model.serve().
The trainer keeps process-global backend state: run one SkillOpt session per process.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import itertools
import json
import os
from pathlib import Path
import threading

from harnessadapter.evolution.optimizer import Context, Outcome


def base_config_path(explicit: str = "") -> Path:
    """SkillOpt's configs/_base_/default.yaml, the defaults every one of its
    benchmark configs inherits. configs/ ships with its source tree, not the
    wheel: install SkillOpt from a checkout or set SKILLOPT_HOME."""
    import skillopt
    home = os.environ.get("SKILLOPT_HOME", "")
    for candidate in (explicit, home and str(Path(home) / "configs/_base_/default.yaml"),
                      str(Path(skillopt.__file__).resolve().parents[1] / "configs/_base_/default.yaml")):
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    raise FileNotFoundError("SkillOpt's configs/_base_/default.yaml was not found: install SkillOpt from a "
                            "source checkout (pip install -e) or set SKILLOPT_HOME to one")


def build_config(settings: dict, *, model: str, reasoning_effort: str | None, seed_path: Path, out_root: Path,
                 split_dir: Path) -> dict:
    """SkillOpt's default recipe with this session's settings on top."""
    from skillopt.config import flatten_config, load_config
    cfg = load_config(str(base_config_path(settings["base_config"])))
    cfg["model"].update(backend="openai_compatible", optimizer=model, target="harnessadapter-environment",
                        optimizer_backend="openai_compatible", target_backend="openai_compatible",
                        reasoning_effort=reasoning_effort)
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
    # every Evolver, so SkillOpt's split has no test items. eval_test stays
    # on, as upstream: it also guards the final val pass that can promote the
    # final (slow-updated) skill to best; its test rollouts are empty.
    cfg["evaluation"].update(use_gate=bool(settings["use_gate"]), eval_test=True, sel_env_num=0, test_env_num=0)
    cfg["env"] = {"name": "harnessadapter", "skill_init": str(seed_path), "out_root": str(out_root),
                  "split_mode": "split_dir", "split_dir": str(split_dir), "split_seed": int(settings["seed"])}
    return flatten_config(cfg)


def write_splits(split_dir: Path, splits: dict) -> None:
    for name in ("train", "val", "test"):
        items = [{"id": t.id, "question": t.prompt, "task_type": t.kind or "task", "split": t.split}
                 for t in splits.get(name, [])]
        (split_dir / name).mkdir(parents=True, exist_ok=True)
        (split_dir / name / "items.json").write_text(json.dumps(items, ensure_ascii=False, indent=1) + "\n")


def conversation(rollout, prompt: str) -> list[dict]:
    """The full trajectory (SkillOpt shows it unclipped) in the record shapes
    its fmt_trajectory renders."""
    steps = [dict(s) for s in rollout.steps if isinstance(s, dict)]
    if not steps and rollout.trace:
        steps = [{"role": "assistant", "content": rollout.trace}]
    return ([{"role": "user", "content": prompt}] + steps +
            [{"role": "assistant", "content": f"Final answer: {rollout.output or '(none)'}"},
             {"role": "system", "content": f"Run status: {rollout.status}; score: {rollout.score:g}"}])


def make_env_adapter(context: Context, component: str):
    from skillopt.datasets.base import SplitDataLoader
    from skillopt.envs.base import EnvAdapter

    settings = context.settings

    class HarnessAdapterSkillOptEnv(EnvAdapter):
        def __init__(self):
            self.analyst_workers = int(settings["analyst_workers"])
            self.failure_only = bool(settings["failure_only"])
            self.minibatch_size = int(settings["minibatch_size"])
            self.edit_budget = int(settings["learning_rate"])
            self.prompt_env = str(settings["prompt_env"] or "")
            self.dataloader = SplitDataLoader(split_mode="split_dir", seed=int(settings["seed"]))
            self._tasks = {t.id: t for t in context.train + context.val}
            self._counter = itertools.count(1)
            self._lock = threading.Lock()

        @property
        def _env_name(self) -> str:
            # "officeqa" selects SkillOpt's own OfficeQA analyst prompts (its
            # failure taxonomy); empty selects the generic prompts.
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
            # As SkillOpt's own run_batch does, a resumed step keeps the
            # episodes it already paid for with this same skill text and runs
            # only the missing ones.
            skill_sha = hashlib.sha256(skill_content.encode()).hexdigest()
            done = {}
            if results_path.is_file():
                for line in results_path.read_text().splitlines():
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if (row.get("skill_sha256") == skill_sha
                            and (where / "predictions" / str(row.get("id")) / "conversation.json").is_file()):
                        done[str(row["id"])] = row
            pending = [self._tasks[str(item["id"])] for item in items if str(item["id"]) not in done]
            if pending:
                with self._lock:
                    number = next(self._counter)
                rollouts = context.evaluate({component: skill_content}, pending,
                                            label=f"skillopt/{number:04d}-{where.parent.name}-{where.name}")
                for task, rollout in zip(pending, rollouts):
                    if rollout.infra:
                        continue
                    hard = int(rollout.score >= 1.0 - 1e-9)
                    record = where / "predictions" / task.id
                    record.mkdir(parents=True, exist_ok=True)
                    (record / "conversation.json").write_text(json.dumps(
                        conversation(rollout, task.prompt), ensure_ascii=False, indent=1) + "\n")
                    done[task.id] = {"id": task.id, "hard": hard, "soft": float(rollout.score),
                                     "fail_reason": "" if hard else (rollout.feedback or f"status {rollout.status}"),
                                     "task_description": task.prompt, "task_type": task.kind or "task",
                                     "n_turns": len(rollout.steps), "agent_ok": rollout.status == "completed",
                                     "status": rollout.status, "predicted_answer": rollout.output,
                                     "prediction": rollout.output, "skill_sha256": skill_sha}
                # Every finished episode is saved before any stop, so a
                # resume pays only for what is missing.
                where.mkdir(parents=True, exist_ok=True)
                with results_path.open("a") as out:
                    out.writelines(json.dumps(done[t.id], ensure_ascii=False) + "\n"
                                   for t, r in zip(pending, rollouts) if not r.infra)
                lost = [r.task_id for r in rollouts if r.infra]
                if lost:
                    # Still lost after the session's re-runs. A 0 would bias
                    # the gate or read as a regression in the slow update, so
                    # stop; resumed from its Training State, the session runs
                    # only these episodes again.
                    raise RuntimeError(f"Episodes lost to the infrastructure after re-runs: {lost[:5]}; "
                                       "resume the session from its training_state")
            return [done[str(item["id"])] for item in items]

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
        parts = Path(runtime.get(key) or "").parts
        anchor = next((i for i in range(len(parts) - 1, -1, -1) if parts[i] in ("skills", "best_skill.md")), None)
        if anchor is not None:
            runtime[key] = str(state.joinpath(*parts[anchor:]))
    path.write_text(json.dumps(runtime, indent=2, ensure_ascii=False) + "\n")


def run(context: Context) -> Outcome:
    from skillopt.engine.trainer import ReflACTTrainer
    from skillopt.model import openai_compatible_backend

    try:
        version = importlib.metadata.version("skillopt")
    except importlib.metadata.PackageNotFoundError:
        version = "unknown (imported from source without metadata)"
    if len(context.components) != 1:
        raise ValueError("SkillOpt optimizes one skill document; give exactly one --component")
    if not hasattr(context.model, "serve"):
        raise ValueError("SkillOpt drives its own OpenAI-compatible client; the model must be a ChatLM")
    component, model, state = context.components[0], context.model, context.state_dir
    seed_path = state / "seed_skill.md"
    if not seed_path.exists():
        seed_path.write_text(context.seed[component])
    relocate_runtime_state(state)
    write_splits(state / "splits", {"train": context.train, "val": context.val})
    cfg = build_config(context.settings, model=model.model, reasoning_effort=model.reasoning_effort,
                       seed_path=seed_path, out_root=state, split_dir=state / "splits")
    context.log(f"[skillopt] train={len(context.train)} val={len(context.val)} epochs={cfg['num_epochs']} "
                f"batch={cfg['batch_size']} lr={cfg['edit_budget']} component={component}")
    # SkillOpt's client reaches the session's model through a local endpoint:
    # the same settings (reasoning effort included, which that backend would
    # not send), the same ledger, and no key in SkillOpt's environment.
    with model.serve() as endpoint:
        openai_compatible_backend.configure_openai_compatible(
            optimizer_base_url=endpoint, optimizer_api_key="local-endpoint", optimizer_model=model.model,
            max_tokens=model.max_tokens, timeout_seconds=model.timeout * model.retries + 900)
        ReflACTTrainer(cfg, make_env_adapter(context, component)).train()
    read = lambda name, default: json.loads((state / name).read_text()) if (state / name).is_file() else default
    trained, history = read("summary.json", {}), read("history.json", [])
    return Outcome(candidate={component: (state / "best_skill.md").read_text()}, summary={
        "algorithm": "skillopt", "skillopt_version": version,
        "seed_val_score": trained.get("baseline_selection_hard"),
        "selected_val_score": trained.get("best_selection_hard"),
        "final_val_score": trained.get("final_selection_hard"),
        **{k: trained.get(k) for k in ("best_step", "best_origin", "total_steps", "total_accepts", "total_rejects",
                                       "total_skips", "epoch_stats", "token_summary")},
        "steps": [{k: h.get(k) for k in ("epoch", "step", "action", "rollout_hard", "n_failure_patches",
                                         "n_success_patches", "n_edits_ranked", "edit_budget", "selection_hard",
                                         "current_score", "best_score")} for h in history]})
