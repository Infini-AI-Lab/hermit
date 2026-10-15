# Prompt-only

The simplest Evolver. At each training step one model call reads the current `ADAPTER.md` and the step's results
(each task's input, the agent's answer, the judge's score and feedback) and writes a new `ADAPTER.md`. It needs no
agent and no validation split; the run's `llm` is the optimizer.

## One step

1. The Trainer runs the current adapter on `data.step_size` training tasks and scores them.
2. The `llm` sees up to `max_examples` of them, lowest scores first, with the task's `task-info/README.md`, the
   adapter's `EVOLVER.md` and its own summaries of the last five steps.
3. It replies with the complete new file and a one-line summary, kept in `state/notes.jsonl`.
4. The Trainer checks the new adapter against the template's specification and commits it.

## Knobs

| Setting | Meaning |
|---|---|
| `llm` | The optimizer's endpoint and model |
| `data.step_size` | Training tasks per step |
| `evolver.params.files` | The Markdown files it rewrites (default `[ADAPTER.md]`) |
| `evolver.params.max_examples`, `max_chars` | Tasks shown per step (8) and characters per field (3000) |
| `evolver.params.max_tokens`, `temperature` | The model call (32000, 0.3) |

All parameters and defaults: [`harnessadapter/algorithms/evolvers/prompt_only/evolver.yaml`](../../harnessadapter/algorithms/evolvers/prompt_only/evolver.yaml).

## Examples

| Folder | Task | Configs |
|---|---|---|
| [`officeqa/`](officeqa/) | [OfficeQA](../tasks/officeqa/); GLM 5.3 Flash acts and optimizes | `smoke.yaml`: 4 training questions in 2 steps, 2 test questions<br>`try.yaml`: 12 training questions in 3 steps, 10 test questions<br>`run.yaml`: 60 training questions in steps of 10, 2 epochs, 60 test questions |

## Run

```bash
python examples/tasks/officeqa/prepare.py /path/to/officeqa/prepared
export FIREWORKS_API_KEY=...
harnessadapter check --config examples/prompt_only/officeqa/smoke.yaml
harnessadapter train --config examples/prompt_only/officeqa/smoke.yaml
harnessadapter eval  --config examples/prompt_only/officeqa/smoke.yaml --adapter final
harnessadapter eval  --config examples/prompt_only/officeqa/smoke.yaml --adapter none
```

`--adapter none` runs the agent without an adapter, the baseline to compare with.
