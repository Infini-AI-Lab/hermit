# Task and acting pipeline

A task integration connects the framework to the problems and acting system you want to improve. You supply data,
a description, a pipeline, and feedback. Read this page to bring your own agent or benchmark.

## Responsibility and boundaries

The pipeline owns the acting model or agents, their execution loop, environment access, and runtime state.
It decides how to deploy the adapter. The Trainer chooses tasks and collects results; the Evolver consumes those
results to improve the adapter.

```{raw} html
:file: ../_includes/task-contract.html
```

This boundary allows a single agent, a group of agents, or a custom workflow to use the same training interface.

## Interface

The current implementation calls two functions:

```python
run(task: dict, adapter: Path | None) -> RunOutput
score(task: dict, output: RunOutput) -> JudgeResult
```

`run` receives one task and an adapter directory. It must make the required exports available to the acting system
before running the task. `adapter=None` means a run without adapter exports, used for baseline comparison.
The pipeline should preserve its ordinary behavior in either case.

`score` evaluates the returned output. It can instead return a dictionary containing `score` and `feedback`, or a
numeric score. Additional dictionary fields are collected under the normalized result's `extra` field.

Scoring is part of collecting learning material. Although the general architecture can use other feedback sources,
the current `train` and `eval` commands require a configured scoring callable.

### Execution result

Only `output` is required when constructing `RunOutput`:

```{literalinclude} ../../harnessadapter/task.py
:language: python
:pyobject: RunOutput
```

| Field | Meaning for the integration |
|---|---|
| `output` | The value the scoring function receives. Choose a JSON-serializable task-specific representation. |
| `log` | Optional file or directory containing the original execution log. The Trainer copies it into learning material. |
| `messages` | Optional normalized trajectory for methods that use model messages. |
| `cost_usd`, `tokens`, `wall_s` | Usage and timing. Unknown cost/tokens should remain null; zero means known zero. |
| `api_speed`, `extra` | Optional measurements and task-specific metadata. |

Concurrent tasks may run against the same adapter path. Treat that adapter as input and keep execution state and
logs outside it. Give each execution its own working paths unless sharing is part of the task design. Keep returned
log paths available until the Trainer has collected them.

### Feedback result

```{literalinclude} ../../harnessadapter/task.py
:language: python
:pyobject: JudgeResult
```

The scoring function decides what feedback to reveal. Reference answers placed in feedback become visible to the
learner. The numeric score can be task-specific; the current `eval` report counts a task as solved at least once
when any repeated score is at least `1.0`.

## Built-in agents

A task whose acting agent is a tool-calling model, or a coding-agent CLI, names a built-in agent as its pipeline
and writes no loop:

```yaml
agent:
  name: react
  params:
    model: {base_url: https://api.fireworks.ai/inference/v1, model: accounts/fireworks/models/glm-5p3-flash,
            api_key_env: FIREWORKS_API_KEY, price: {input: 0.15, cached_input: 0.03, output: 0.5}}
    prompt: utils/agent/prompt.md      # system prompt; `{context}` marks where ADAPTER.md goes (else appended)
    tools: utils/tools.py              # the task's tools: functions marked with @harnessadapter.agents.tool
    user: "{question}"                 # the first user message from the task's fields
    max_turns: 24
    answer_tag: FINAL_ANSWER           # optional: one more call without tools when the tag is missing
```

| `agent.name` | Agent | Settings |
|---|---|---|
| `react` | a chat-completions tool-calling loop over the task's tools and the adapter's tools | `model`, `prompt`, `tools`, `user`, `max_turns`, `max_output_tokens`, `max_tokens`, `temperature`, `answer_tag`, `tool_result_chars`, `logs` |
| `codex`, `claude-code` | Codex or Claude Code run in a fresh workspace the adapter is deployed into, as `deploy` does | `model`, `prompt`, `files`, `args`, `price`, `timeout_s`, `workspaces` |

Every agent has the same interface, the task's own included:

```python
from harnessadapter.agents import Agent

class MyAgent(Agent):                     # agent.name: agent/agent.py:MyAgent
    def run(self, task, adapter):         # self.params, self.base (the config's directory), self.session(adapter)
        ...
        return RunOutput(output=...)
```

A plain `run(task, adapter)` function is accepted too (`agent.name: agent/pipeline.py:run`); it receives
`params` and `base` when its signature names them. A task tool is a plain function:

```python
from harnessadapter.agents import tool

@tool("Read lines of one bulletin file.", {"type": "object", "properties": {
      "filename": {"type": "string"}, "start_line": {"type": "integer"}}, "required": ["filename"]})
def read(filename: str, start_line: int = 1) -> dict:
    ...
```

A function that declares `_task` receives the task row. The result is JSON; an exception becomes an error
message the model reads. The adapter's own tools are offered beside these and answered through
[`Session`](adapter.md#custom-acting-pipelines). A benchmark that owns its agent loop is an `Agent` subclass
that runs that loop with `Session` plugged in; `examples/clbench_poker/utils/agent/agent.py` is one.

## What the task owner provides

| Input | Purpose | Used by |
|---|---|---|
| `tasks.jsonl` | Task dictionaries with unique IDs. | Pipeline and scoring function. |
| Split files | One task ID per line for train, optional validation, and test. | Trainer and evaluation. |
| `task-info/` | Description of the task, objective, feedback, and execution. | Evolver. |
| Pipeline callable | Adapter integration and acting execution. | Training, evaluation, and candidate experiments. |
| Scoring callable | Score and feedback for a result. | The same execution paths. |
| Optional resources | Environment paths or task tools granted to the learner. | Evolver. |
| Run configuration | Connect these inputs with a template and Evolver. | Framework commands. |

A task dictionary needs a unique `id` (`task_id` is also accepted); its other fields belong to your integration.
Split IDs must refer to existing tasks. `check` detects overlapping splits. A missing test split permits training,
but `eval` needs test tasks.

### task-info

Provide a `README.md` as the learner's entry point. Explain what an input means, what a correct result looks like,
the optimization objective, and how to interpret feedback. Larger tasks can split this into `task.md`, `goal.md`,
`judge.md`, and `pipeline.md`; these are organizational conventions.

Written task rules inform the learner. They are not automatically enforced by the Trainer's specification check.
Requirements that must be enforced need an appropriate task judge or template check.

## Interaction with other components

The Trainer calls the pipeline on each training task, passes its result to the scoring function, and writes the
combined record as [learning material](trainer.md#learning-material). The same functions serve test evaluation and
candidate runs requested by the Evolver. They should not rely on one particular caller or call order.

The adapter reaches the acting system through its [exports](adapter.md#acting-interface). A context-only integration
may read text into its prompt. A tool or service adapter needs the corresponding host integration and resources.
`check` does not prove that a host supports every export.

The task environment and learner access are separate. A pipeline may use resources that the Evolver cannot inspect.
`evolver_access.environment` grants the learner additional paths; `evolver_access.tools` declares capabilities.
Neither setting configures the acting pipeline for you.

These choices keep task execution reusable when the template or learning method changes, and keep the pipeline's
original execution available as the `adapter=None` baseline.

## Minimal example

This complete offline stub loads adapter context and echoes the task input. Its messages demonstrate the exchange;
it does not call a model. Replace the echo with your acting system when integrating a real task.

```{literalinclude} ../_examples/interface_task/pipeline.py
:language: python
```

Its scoring function compares the output with the expected string:

```{literalinclude} ../_examples/interface_task/judge.py
:language: python
```

The example config binds these functions as:

```yaml
pipeline:
  name: pipeline.py:run
judge:
  path: judge.py
  name: score
```

Paths are relative to the config file. `agent.name` includes the class or function name; `judge.path` names the file
or module and `judge.name` names the function (default `score`). `agent.params` is the agent's to read.

Run the [offline example](trainer.md#minimal-example) to exercise these interfaces. For a real model and document
environment, see [OfficeQA](../recipes/officeqa.md).
