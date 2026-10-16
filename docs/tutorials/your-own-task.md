# 3. Your own task

You have an agent and problems with a way to score the answers, and want an existing method (a template and an
Evolver) to improve the agent's instructions, skills or tools on them. You supply four things; everything else is
the framework's.

| You supply | File | Who reads it |
|---|---|---|
| the tasks and splits | `tasks.jsonl`, `train.txt`, `test.txt` | the Trainer |
| how one task is run with an adapter | `pipeline.py: run(task, adapter) -> RunOutput` | rollouts, eval |
| how an output is scored | `judge.py: score(task, output) -> {score, feedback}` | rollouts, eval |
| what the task is, for the learner | `task-info/README.md` | the Evolver |

The offline example `docs/_examples/interface_task/` is the smallest complete integration (an echo agent, no model
call). Run it first, then replace its parts with yours:

```bash
harnessadapter check --config docs/_examples/interface_task/run.yaml
harnessadapter train --config docs/_examples/interface_task/run.yaml
harnessadapter eval  --config docs/_examples/interface_task/run.yaml --adapter final
```

## The tasks

One JSON object per line with a unique `id`; the other fields are yours (the pipeline and the judge read them).
Splits are one id per line. The learner sees a task's row as the `input` of its learning material, so keep the
answer out of it unless the learner may know it; put answers in the judge's data or in fields the judge alone reads.

```json
{"id": "train-1", "prompt": "first input", "expected": "first input"}
```

## The pipeline

`run` receives one task and an adapter directory (or `None` for a run without adapter, the baseline). It must make
the adapter's exports available to your agent, run the task, and return a `RunOutput` whose `output` is what the
judge scores. For a context-only adapter, that is reading `ADAPTER.md` into the system prompt:

```{literalinclude} ../_examples/interface_task/pipeline.py
:language: python
```

If your acting agent is a tool-calling model or Codex / Claude Code, you need no pipeline at all: name a
[built-in agent](../framework/task.md#built-in-agents) as `agent.name` and give it the prompt and the task's
tools in `agent.params`.

An adapter with tools needs your agent to offer them: `Session(adapter).tool_specs("openai")` gives them in the
model's format and `Session(adapter).respond(tool_call)` answers one call with the tool message to append
(see [Custom acting pipelines](../framework/adapter.md#custom-acting-pipelines)).
`examples/servelearnbench/utils/agent/agent.py` does exactly this inside a benchmark's own agent loop (the context
into the system prompt, the `wm` tool into the tool list).
Concurrent tasks run against the same adapter directory: treat it as input, keep logs and state elsewhere, and
return the log's path in `RunOutput.log` so it lands in the learning material.

## The judge

`score` receives the task and the pipeline's output and returns a score and a feedback string. What the feedback
says is what the learner learns from: a benchmark that discloses only a number gives only the number
(`examples/servelearnbench/utils/judge.py` returns `Score: n/100.` and nothing else); a benchmark whose agent is
shown the correct answer may return it.

```{literalinclude} ../_examples/interface_task/judge.py
:language: python
```

## The task description

`task-info/README.md` is the learner's entry point: what an input is, what a correct output looks like, how to read
the feedback. Written for the learner, never shown to the acting agent, and containing nothing the agent could not
know. The MWM Evolver reads a more structured description, its `deployment` directory (`params.deployment`, default `task-info/mwm/`: the venue's contract slots,
host settings and environment mirror); `examples/servelearnbench/utils/task-info/` is the retail example.

## The run config

```yaml
data: {tasks: tasks.jsonl, train: train.txt, test: test.txt, step_size: 8, shuffle: true}
task_info: task-info
pipeline: {run: pipeline.py:run, concurrency: 4}
judge: {path: judge.py}
template: prompt-only@1.0          # or mwm, or a template directory
evolver: {name: prompt_only}        # or codex, mwm, gepa, skillopt
llm: {base_url: ..., model: ..., api_key_env: FIREWORKS_API_KEY}
trainer: {run_name: my-task, output_dir: runs, budget: {usd: 20, hours: 4}}
```

`step_size` is how many tasks the learner sees per step; `shuffle: false` keeps a stream's order. Every field is in
[Configuration](../framework/configuration.md). Then:

```bash
harnessadapter check --config run.yaml       # runs one task through your pipeline and judge
harnessadapter train --config run.yaml
harnessadapter eval  --config run.yaml --adapter final
harnessadapter eval  --config run.yaml --adapter none
```

A complete real integration, with a benchmark's own agent loop, per-window tests and a launcher, is
`examples/servelearnbench/` ([recipe](../recipes/servelearnbench.md)).

Next: [Customize an adapter](customize-an-adapter.md).
