# Evolving Task

:::{note}
Design, agreed 2026-10-05; not implemented yet.
:::

The evolving task is everything the task user provides once a template and an Evolver have been chosen.

## Design principle: the user knows only their task

Design for the worst case: the user knows only that a template and an Evolver exist and were recommended to them, and
nothing about how either works. They care about their task and the agent they want to improve. So everything here is
about the task and the acting-agent pipeline, and nothing requires knowledge of a template's or an Evolver's
internals. Choosing the template and the Evolver is naming them in the run config; their parameters have defaults.

The task does not include anything whose purpose is to help an Evolver produce a better adapter, such as rule checks.

## Parts

| Part | What it is | Used by |
|---|---|---|
| `task-info/` | The task in words: what the tasks are, the goal, how answers are scored | Evolver |
| Data | `tasks.jsonl` (one task per line) and the train / validation / test split | Trainer |
| Judge | The scoring program | Trainer (rollouts, `eval`), acting-agent runner |
| Pipeline | How the acting agent runs one task, designed by the user | Trainer (rollouts, `eval`), acting-agent runner |
| Environment (optional) | The task's own resources that the acting agent operates on, if the Evolver may inspect them | Evolver |
| Run config `run.yaml` | Connects the above with the template and the Evolver (see [Configuration](configuration.md)) | `check`, `train`, `eval` |

## task-info

What goes into `task-info/` is task-specific. The framework ships a basic template with the files it considers
useful; the user fills it in and may add any other files. The only fixed point is `README.md`, the entry the Evolver
reads first and that lists the other files.

| File in the basic template | Content | Lean example |
|---|---|---|
| `README.md` | Entry: the task in one paragraph, the list of files | — |
| `task.md` | What a task looks like; the acting agent's input and output | A Lean theorem; a proof without `sorry` |
| `goal.md` | What to optimise; priorities when there are several aims | Solved > time > cost |
| `judge.md` | How answers are scored and what the feedback looks like, in words | Correct only if it compiles and the target statement is unchanged |
| `pipeline.md` (optional) | How the acting agent runs: host, agents, limits | Five agents in parallel, 3-hour cap, a Lean compiler |
| `rules.md` (optional) | Constraints the user wants stated | No facts about individual problems in the adapter |

Rules are stated in words only. Whether and how to check an adapter against them (a program, an LLM review) is the
Evolver's business; the Trainer does not enforce task rules. The only binding check is `check_spec`.

## Judge

The judge belongs to the task, and the user defines it:

```python
def score(task: dict, output: RunOutput) -> JudgeResult:   # score, feedback, and anything else worth recording
    ...
```

The Trainer passes everything the judge returns into the learning material unchanged; there is no separate setting
for how much feedback the Evolver sees. The richer the feedback, the more an Evolver can learn from it. Whether the
feedback reveals the reference answer is the user's decision when writing the judge.

## Pipeline

The user designs the complete acting-agent pipeline: how one task is run (host, model, number of agents and how they
work together, launcher, time cap, isolation). The framework is **acting-agent agnostic**: it never modifies the
pipeline, and no template or Evolver is written for a particular one.

To the framework the pipeline is a black box with one interface:

```python
def run(task: dict, adapter: Path | None) -> RunOutput:
    """Run one task with the adapter deployed; adapter=None runs the agent with no adapter at all."""

@dataclass
class RunOutput:
    output: Any                 # what the judge scores
    log: Path                   # the host's original log (the trajectory)
    cost_usd: float | None
    wall_s: float
    api_speed: dict | None      # for example tokens per second, request latency
```

The adapter enters the pipeline only through its standard exports (context, MCP tools, roles, reactions), which the
pipeline deploys into its host as the template's `README.md` describes; `check` verifies the template supports the
pipeline's host. The same `run` serves training rollouts, `eval` and the acting-agent runner. The Evolver learns about
the pipeline only from `task-info/pipeline.md` and from the trajectories.

## Environment

"Environment" means only the task's own resources the acting agent operates on: a code base for bug fixing, the
document collection for document QA, Mathlib for Lean proofs. Listing them under `evolver_access.environment` lets the
Evolver inspect them, for example to check whether a lemma exists before mentioning it in a skill. How the acting
agent runs is the pipeline, not the environment.

## Data

```text
tasks.jsonl          one task per line, with an id
splits/train.txt     task ids, one per line
splits/val.txt       optional
splits/test.txt      optional
```

The splits must not overlap (`check` verifies it). A task without a test split can still train; `eval` then has
nothing to run. The validation split, when given, goes to the Evolver with whatever it contains.
