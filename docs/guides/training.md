# Train an experiment

A useful training run starts with a fixed task protocol and an interpretable baseline. Complete the
[Quick Start](../get-started/quickstart.md) first to verify the local framework, then choose a
[API-only real-model example](../get-started/first-model.md), a
[recipe](../recipes/index.md), or [connect your task](../tutorials/your-own-task.md).

## 1. Fix the experiment inputs

Record the task dataset and split revision, acting model and settings, judge, template, and learner.
Choose a [method](../methods/index.md) whose editing behavior fits the template.

The pipeline owns the acting model. The run's `llm` section configures a learner tool; changing it does not
automatically change the agent that answers tasks. Agent-backed and MWM learners also have backend-specific
model settings.

## 2. Make a small config

Start from a recipe rather than filling every default. A smoke run should have few tasks, a short iteration
schedule, and a time or usage budget. It establishes that the integration works; it cannot establish a reliable gain.

```bash
harnessadapter check --config run.yaml --no-probe
harnessadapter check --config run.yaml
```

The first command skips the task execution probe. The second runs one training task without an adapter and may
spend model credits. Check that output, judge feedback, and logs contain what you intend the learner to see.

## 3. Run baselines and train

```bash
harnessadapter eval --config run.yaml --adapter none
harnessadapter train --config run.yaml
harnessadapter eval --config run.yaml --adapter root
harnessadapter eval --config run.yaml --adapter final
```

`root` and `final` require the relevant adapter tree. Use the same run-name and output overrides on the related
commands. Baselines and evaluation are separate from the training budget.

## 4. Inspect one update before scaling

Open `steps.jsonl`, the step's learning material, and the adapter diff. Confirm:

- the actor actually consumed the adapter's context and tools;
- feedback identifies useful failure information without unintended answer disclosure;
- the learner changed permitted files, and the update passed the template checks;
- recorded usage includes acting calls, learner calls, and candidate experiments where applicable.

Step rollout scores measure the adapter **before** that step's update. A successful learner status means the
method completed; it does not imply an improved held-out score. See [Run records](runs.md).

## 5. Scale with a comparison plan

Increase dataset coverage and repetitions before interpreting small score differences. Fix held-out data and the
evaluation protocol. Compare methods with their full call and cost records, since a validation-search method
performs more work per step than a single rewrite.

`pipeline.concurrency` controls concurrent task executions. It does not set every learner's internal parallelism.
For endpoint throttling, also inspect method-specific controls such as SkillOpt's `analyst_workers`.

Budget limits are observed at framework boundaries and can overshoot during in-flight work. Unknown usage cannot
be turned into a reliable dollar cap. See [budget semantics](../framework/trainer.md#budgets).

Continue with [Evaluate an adapter](evaluation.md) or [deploy the result](../tutorials/deploy-an-adapter.md).
