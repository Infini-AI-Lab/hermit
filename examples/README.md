# Examples

Each folder is one task. Inside it the three things a task owner provides are kept apart, and the configs of every
method that has been run on the task sit beside them:

```text
examples/<task>/
  README.md          the task, how to prepare it, which methods run on it
  utils/             everything the task itself needs: the agent, the data preparation, the judge, the texts
    agent/           the acting agent: a prompt for a built-in agent, or agent.py with the task's own Agent
                     class when a benchmark owns the loop; the config names it in agent.name
    prepare.py       writes tasks.jsonl and splits/ beside itself, from the task's data (which stays where it is)
    judge.py         scores one output and writes the feedback
    tools.py         the task's tools for a built-in agent, where it has any
    task-info/       what the learner is told: README.md, and mwm/ with what MWM needs to know about the task
    tests/           the task's own tests, where it has any
  <method>/          one folder per method run on the task: smoke.yaml, run.yaml, a launcher where a run needs
                     one, a trained adapter/ where included
```

| Folder | Task | Agent | Methods |
|---|---|---|---|
| [`officeqa/`](officeqa/) | OfficeQA Full in the SkillOpt paper's setting: questions about U.S. Treasury Bulletins, its 50 / 24 / 172 split, exact match | `OfficeQAAgent`: SkillOpt's own rollout, GLM 5.3 Flash | [`prompt_only`](officeqa/prompt_only/), [`codex`](officeqa/codex/), [`gepa`](officeqa/gepa/), [`skillopt`](officeqa/skillopt/) |
| [`clbench_database/`](clbench_database/) | The CL-Bench database exploration stream (accuracy) | `CLBenchDatabaseAgent`: metaworld's host seat in CL-Bench's loop | [`prompt_only`](clbench_database/prompt_only/), [`mwm`](clbench_database/mwm/) |
| [`clbench_poker/`](clbench_poker/) | The CL-Bench exploitable-poker stream (bb/hand) | `CLBenchPokerAgent` | [`prompt_only`](clbench_poker/prompt_only/), [`mwm`](clbench_poker/mwm/) |
| [`servelearnbench/`](servelearnbench/) | A ServeLearnBench scenario: serving stream, per-window tests | `ServeLearnBenchAgent`: the benchmark's own loop | [`prompt_only`](servelearnbench/prompt_only/), [`mwm`](servelearnbench/mwm/) |
| [`lean_swarm/`](lean_swarm/) | Lean swarm: five Codex agents prove one formal-conjectures theorem together; a finished adapter, check and deploy only | Codex | — |

The methods themselves are described in [`docs/methods/`](../docs/methods/).

## Run an example

Run from the repository root. Prepare the task once, then check, train and evaluate with a method's config:

```bash
pip install -e '.[skillopt,gepa]'
python examples/officeqa/utils/prepare.py --csv /path/to/officeqa_full.csv --docs /path/to/treasury_bulletins_parsed
export FIREWORKS_API_KEY=...                   # the configs name GLM models on Fireworks
harnessadapter check --config examples/officeqa/gepa/smoke.yaml
harnessadapter train --config examples/officeqa/gepa/smoke.yaml
harnessadapter eval  --config examples/officeqa/gepa/smoke.yaml --adapter final
```

Paths inside a config, and a path given to `--adapter`, are relative to the config's folder: a config reaches its
task as `../utils/...` and `../utils/agent/...`. A run writes to `<task>/<method>/runs/`. Run
`smoke.yaml` before `run.yaml`: it checks the data, pipeline, judge and Evolver together on a few questions.

Models are `<backend>/<model id>` references; the same config runs against another provider or a local vLLM or
SGLang server with one override, `model=vllm/Qwen/Qwen3-30B-A3B-Instruct-2507-FP8` (the launchers take
`MW_MODEL=...`). See [Model backends](../docs/guides/model-backends.md).

## Add an example

- A task: `utils/prepare.py`, the task's tools or an `utils/agent/agent.py` with an `Agent` subclass, `utils/judge.py`,
  `utils/task-info/README.md` (see [Task integration](../docs/framework/task.md) and the
  [built-in agents](../docs/framework/task.md#built-in-agents)), and a row in the table above.
- A method on an existing task: `<method>/smoke.yaml` and `run.yaml`, and a row in `docs/methods/<method>.md`.
- A method: `docs/methods/<method>.md` in the shape of the others: what it does, one step, knobs, configs, commands.
