# Evolution Framework Overview

:::{note}
This section describes the evolution framework, agreed on 2026-10-05 and implemented in 0.2.0. The
[prototype differences](prototype-differences.md) page lists what changed relative to the 0.1 prototype.
:::

The evolution framework trains **harness adapters** the way a training framework trains model weights. An adapter is
the trainable artifact; an **Evolver** is the learning algorithm that changes it; the **Trainer** runs the loop around
the Evolver; an **evolving task** supplies the data, the scoring and the acting-agent pipeline the adapter is deployed
into.

## Components

The framework has four definitions:

1. **Adapter template and adapter** — A template is a structure plus its specification (which parts are learnable,
   which are fixed, in what format). An adapter is a concrete, deployable artifact built on a template. Templates and
   adapters form a tree kept in git. See [Adapter Template and Adapter](adapter.md).
2. **Evolver** — The learning algorithm (for example prompt-only, GEPA, SkillOpt, MWM). It is the harness of a learner
   agent: its model, prompts, tools, control flow and state. It is task-agnostic and plugs in through one uniform class
   interface. See [Evolver](evolver.md) and [Custom Evolver Integration](custom-evolver.md).
3. **Trainer** — The framework's outer loop: data, training rollouts, learning material, the binding conformance check,
   git, records, budgets, checkpoints and resume. See [Trainer](trainer.md).
4. **Evolving task** — What the user provides: the task description, data, a judge and the acting-agent pipeline.
   See [Evolving Task](task.md).

The parts correspond to a model-training framework such as verl:

| verl | This framework |
|---|---|
| Model weights | Adapter |
| Initial model | Adapter template (or any earlier adapter, `start_from`) |
| Optimizer / `algorithm` | Evolver |
| Trainer | Trainer |
| Rollout worker (`actor_rollout_ref`) | The user's acting-agent pipeline |
| Reward function | Judge |
| Training config (YAML) | Run config `run.yaml` |

## Roles

Three kinds of users work with the framework, and the framework provides one component.

| Role | Provides | Needs to know |
|---|---|---|
| Template provider | An adapter template | How adapters of this kind work |
| Evolver provider | An Evolver | The Evolver contract; nothing about any particular task |
| Task user | An evolving task, and the names of one template and one Evolver | Only their own task and pipeline |
| Framework | The Trainer and the commands `check`, `train`, `eval` | — |

The task user is designed for the worst case: they know only that a template and an Evolver exist and were
recommended to them, and nothing about how either works. Nothing a task user must provide requires knowledge of a
template's or an Evolver's internals.

## Architecture

```
                        run.yaml
                           │
                ┌──────────▼──────────┐
                │       Trainer        │
                │  data · rollouts ·   │
                │  check · git · budget│
                └──┬──────────┬────────┘
     run(task,     │          │  step(ctx) → StepResult
     adapter)      │          │
        ┌──────────▼───┐   ┌──▼──────────────┐
        │  Acting-agent│   │     Evolver      │
        │  pipeline    │   │ (learner harness)│
        │  (user's)    │   │  reads material, │
        └──────┬───────┘   │  edits adapter/  │
               │           └──┬───────────────┘
          output, log         │ adapter/ handed back
               │              │
        ┌──────▼───────┐   ┌──▼──────────────┐
        │    Judge     │   │   check_spec     │
        │   (user's)   │   │  (template's)    │
        └──────┬───────┘   └──┬───────────────┘
               │ score,       │ pass / repair
               │ feedback     │
               ▼              ▼
     learning_material/    git commit (adapter = commit)
```

## Training loop

Each step proceeds as follows:

1. **Data** — Take the next `step_size` tasks from the training split.
2. **Rollout** — Run the current adapter on them through the user's pipeline and score them with the judge.
3. **Material** — Write the results to `learning_material/step_NNNN/` in the framework's standard format.
4. **Workspace** — Prepare the Evolver's workspace: a working copy of the adapter, the material, `task-info/`, a
   read-only history view, the Evolver's state, and its tools.
5. **Evolver** — Call `step(ctx)`. The Evolver changes the adapter (or leaves it unchanged) and returns a
   `StepResult`.
6. **Check** — Run `check_spec` on the adapter handed back. If it fails, repair it with the smallest change that makes
   it conform.
7. **Commit** — If the adapter changed, commit it to git as a new node.
8. **Record** — Store usage, time, check results and the Evolver's output in the run records.

`setup` is called once before the first step and `finalize` once after the last. The final adapter is then evaluated
on the test split with `eval`.

## Design principles

- **Acting-agent agnostic.** The user designs the acting-agent pipeline; the framework never modifies it, and no
  template or Evolver is written for a particular one.
- **Adapters work with any agent.** Like a Claude Code plugin, an adapter is a package of exports (context, tools,
  roles, skills) that any acting agent can install: Codex and Claude Code through `harnessadapter deploy`, a custom
  agent loop through its own deployment (for example tools as function calls). Moving an adapter to another agent or
  model may cost some effectiveness (a transfer tax); `CARD.md` states where it was trained and evaluated.
- **Task-agnostic Evolvers.** An Evolver contains no task content. Everything task-specific reaches it through
  `task-info/`, the learning material and the tools of a run, so one Evolver applies to any task.
- **Uniform protocol, free inside.** An Evolver may do anything inside as long as it meets the input, output and
  protocol requirements and the adapter's specification.
- **The next adapter is always valid.** The Trainer guarantees that every adapter entering a step conforms to its
  template; everything else about quality is the Evolver's business.
- **Warn, do not forbid.** Sandboxing, network access and data exposure are the user's settings. Risky choices produce
  warnings that name the guarantees no longer held; they are not blocked.
- **The user decides how long a run lasts.** Epochs and budgets are set by the user; an Evolver may stop early only if
  the user allows it.
