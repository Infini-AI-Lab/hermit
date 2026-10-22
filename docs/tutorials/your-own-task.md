# Bring your own task

Connect an existing acting agent to the training loop by supplying tasks, a pipeline, a judge, and a learner-facing
task description. The Trainer owns batching, learning material, checks, versioning, and recovery.

| You provide | File | Consumer |
|---|---|---|
| Inputs and split ids | `tasks.jsonl`, `train.txt`, optional `val.txt`, `test.txt` | Trainer and evaluator |
| One acting attempt | `pipeline.py:run` | Rollouts and evaluation |
| Scoring and permitted feedback | `judge.py:score` | Trainer and evaluator |
| Task objective and feedback contract | `task-info/README.md` | Learner |
| Experiment settings | `run.yaml` | Framework |

## Start from a working integration

Copy the self-contained [Quick Start](../get-started/quickstart.md) task into a disposable directory:

```bash
mkdir -p .demo
cp -R docs/_examples/first_training .demo/my-task
```

Its config's output path was written for its original directory. Change `trainer.output_dir` to `runs` and
`trainer.run_name` to `my-task` in the copied `run.yaml`. Then run:

```bash
harnessadapter check --config .demo/my-task/run.yaml
harnessadapter train --config .demo/my-task/run.yaml
harnessadapter eval --config .demo/my-task/run.yaml --adapter final
```

Outputs now live under `.demo/my-task/runs/my-task/`. Replace the teaching components one at a time.

## Supply task inputs and splits

Use one JSON object per line, with a unique id and whatever public input fields your pipeline needs:

```json
{"id": "train-1", "prompt": "hello reef"}
{"id": "test-1", "prompt": "a new task"}
```

A split file lists one id per line. Keep training, validation, and test disjoint; `check` reports overlaps.
Train task rows become learner-visible material. Keep private labels in judge-owned storage, not in fields you
merely intend the pipeline to ignore. Restrict learner filesystem grants consistently with that separation.

## Implement the acting pipeline

The pipeline receives a task and an adapter path, or `None` for the no-adapter baseline. It must actually supply
the adapter's exports to the acting agent. Reading a config's template name alone does not do this.

The teaching pipeline shows the boundary without a model dependency:

```{literalinclude} ../_examples/first_training/pipeline.py
:language: python
```

Replace its interpreter with your agent call. Read `Adapter(adapter).context()` into the acting context.
For tools, `Adapter(adapter).manifest["tools"]` provides declarations and `Adapter(adapter).call(name, args)`
executes them. Your loop owns routing model tool requests and returning results.

Return `RunOutput` with the judge's output, usage when known, and messages or a log path for trajectories.
Treat the shared adapter directory as input; keep per-task logs and mutable state elsewhere when tasks run
concurrently. See the [task contract](../framework/task.md) for exact fields.

## Implement the judge

The judge accepts `(task, RunOutput)` and returns a numeric score plus feedback. The learner receives that feedback,
so it is part of the learning protocol, not just logging.

```{literalinclude} ../_examples/first_training/judge.py
:language: python
```

This toy judge computes its target privately. For a real dataset, load labels from judge-owned storage.
Choose the score scale deliberately; the generic evaluator's `solved_once` uses a fixed threshold of 1.0.
Do not reveal a reference solution through feedback unless your protocol permits it.

## Describe the task for the learner

Write `task-info/README.md`: input format, objective, allowed feedback, interpretation of trajectories, and available
resources. It is learner-facing and does not automatically become acting-agent context. Include only information
permitted to that learner. MWM additionally requires a structured deployment description and evidence store;
follow the [MWM contract](../methods/mwm.md) rather than switching names alone.

## Select a real learning method

The copied config uses a one-rule teaching learner. Choose a [built-in method](../methods/index.md) for a real
experiment. A context optimizer might use:

```yaml
template: prompt-only@1.0
evolver: {name: gepa}
data:
  val: val.txt
llm:
  base_url: https://your-endpoint.example/v1
  model: your-learner-model
  api_key_env: LEARNER_API_KEY
```

Configure the acting model in your pipeline independently. GEPA and SkillOpt need a validation split (`data.val`)
and their installation extra.
Keep a finite budget and start with a small batch.

Run `check`, baseline `eval --adapter none`, `train`, then `eval --adapter root` and `eval --adapter final`.
For a complete benchmark-owned acting loop, use the [ServeLearnBench recipe](../recipes/servelearnbench.md)
as a larger reference. Continue with [Evaluation](../guides/evaluation.md).
