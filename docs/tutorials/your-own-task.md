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
hermit check --config .demo/my-task/run.yaml
hermit train --config .demo/my-task/run.yaml
hermit eval --config .demo/my-task/run.yaml --adapter final
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

Replace its interpreter with your agent call. `Session(adapter).context()` reads the adapter's context into the
acting system prompt, and `Session(adapter).tool_specs("openai")` declares its tools in the model's own format.
`Session(adapter).call(name, arguments)` executes one, and `respond()` turns a tool call the model returns into the
message that answers it, with errors as text. Your loop owns routing model tool requests and returning results. The
[adapter interface](../framework/adapter.md) documents the other exports and the sandboxed form.

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
Choose the score scale deliberately; the generic evaluator's `solved_once` and `pass_at_k` count a score of at least
1.0 as solved, unless the judge returns `passed` as a bool, which decides; a judge that returns `passed: None` (a score
scale with no pass) leaves both uncomputed.
Do not reveal a reference solution through feedback unless your protocol permits it.

## Describe the task for the learner

Write `task-info/README.md`: input format, objective, allowed feedback, interpretation of trajectories, and available
resources. It is learner-facing and does not automatically become acting-agent context. Include only information
permitted to that learner. MWM additionally requires a structured deployment description and evidence store;
follow the [MWM contract](../methods/mwm.md) rather than switching names alone.

## Declare the task sandbox

The acting agent runs in the task sandbox, which shows only what the config's `environment` declares
([sandboxes](../framework/configuration.md#sandboxes)). Keep the agent's code, its helpers and the data it reads in
directories of their own, apart from the answers, the judge and the runs, and declare them:

```yaml
sandbox:
  enforce: required                         # the first-training config runs best_effort
environment:
  read: [../utils/agent, ../utils/corpus]   # never a directory holding the data, the judge or the runs
  packages: [your-benchmark]                # the distributions the agent imports, with their dependencies
  env: ["YOURTASK_*"]                       # the variables your launcher exports (no model keys)
judge:
  path: ../utils/judge.py
  mode: host                                # or shared / separate, when the judge reads the environment's state
```

An agent that calls a model takes it from `agent.params.model` (a `<backend>/<model>` reference): the framework
resolves it and passes its key variable and its provider's host into the sandbox, as Harbor gives its agents their
models. A model the agent's code picks by itself (a default in the code) finds neither its key nor its host inside.

Before a reported run, check the isolation: `sandbox.enforce: required` (never a `best_effort` run that fell back to
running unconfined); `hermit check` passes, which refuses a declared path that holds a secret; the launch records
(`runs/<run>/sandbox/*.json`) show the isolation check passed, the answers and the judge absent, and only the
variables you expect; the network reaches only the models' providers and the hosts you declared; `web_search` is set
explicitly.
A benchmark whose scoring lives in the package the agent imports shows its scoring to the agent: say so in the
results.

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
and their dependency groups (`--group gepa`, `--group skillopt`).
Keep a finite budget and start with a small batch.

Run `check`, baseline `eval --adapter none`, `train`, then `eval --adapter root` and `eval --adapter final`.
For a complete benchmark-owned acting loop, use the [ServeLearnBench recipe](../recipes/servelearnbench.md)
as a larger reference. Continue with [Evaluation](../guides/evaluation.md).
