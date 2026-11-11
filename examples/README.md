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

## Recipes

Each of these runs a task end to end and reports a number: its README gives the commands, the result table and
how the number was measured, and the method folders hold the trained adapters where they are included, each with
a `.hermit/CARD.md`.

| Folder | Task | Agent | Methods |
|---|---|---|---|
| [`officeqa/`](officeqa/) | OfficeQA Full in the SkillOpt paper's setting: questions about U.S. Treasury Bulletins, its 50 / 24 / 172 split, exact match | `OfficeQAAgent`: SkillOpt's own rollout, GLM 5.3 Flash, in direct chat or SkillOpt's Codex harness; a second results column with Codex + GPT-5.6-Luna acting and learning on a ChatGPT login (`*_luna.yaml`) | [`gepa`](officeqa/gepa/), [`skillopt`](officeqa/skillopt/), [`harness2_stream`](officeqa/harness2_stream/) (Harness², the paper's per-question protocol, with Codex as the acting agent), [`continual_harness`](officeqa/continual_harness/) (Continual Harness, Codex as the acting agent), [`reharness`](officeqa/reharness/) (ReHarness, Codex as the acting agent), [`meta_harness_adapter`](officeqa/meta_harness_adapter/) (Meta-Harness Adapter, Codex as the acting agent), [`ace`](officeqa/ace/) (ACE, Codex + GPT-5.6-Luna), [`ahe_adapter`](officeqa/ahe_adapter/) (AHE Adapter, Codex as the acting agent), [`rrsi_adapter`](officeqa/rrsi_adapter/) (RRSI Adapter, Codex + GPT-5.6-Luna acting and learning) |
| [`clbench_database/`](clbench_database/) | The CL-Bench database exploration stream (accuracy) | `CLBenchDatabaseAgent`: metaworld's host seat in CL-Bench's loop | [`prompt_only`](clbench_database/prompt_only/), [`mwm`](clbench_database/mwm/), [`reharness`](clbench_database/reharness/) |
| [`clbench_poker/`](clbench_poker/) | The CL-Bench exploitable-poker stream (bb/hand) | `CLBenchPokerAgent` | [`prompt_only`](clbench_poker/prompt_only/), [`mwm`](clbench_poker/mwm/), [`reharness`](clbench_poker/reharness/) |
| [`servelearnbench/`](servelearnbench/) | A ServeLearnBench scenario: serving stream, per-window tests | `ServeLearnBenchAgent`: the benchmark's own loop | [`prompt_only`](servelearnbench/prompt_only/), [`mwm`](servelearnbench/mwm/) |
| [`aspire_robosuite/`](aspire_robosuite/) | The Robosuite tasks of ASPIRE's Code-as-Policy harness: development and held-out seeds, task success | Codex with ASPIRE's fix-loop subagent and verifier prompts (`utils/agent/task.py`), GLM 5.3 Flash, ASPIRE's robot execution engine in its own venv through `bin/sim` | [`aspire`](aspire_robosuite/aspire/) |
| [`aspire_libero/`](aspire_libero/) | LIBERO's Object, Spatial, Goal, Long and LIBERO-90 suites in ASPIRE's Code-as-Policy harness: development and held-out seeds, task success | As `aspire_robosuite/`, whose engine it runs on (ASPIRE's LIBERO venv, seeded initial states) | [`aspire`](aspire_libero/aspire/) |
| [`robodojo_sim/`](robodojo_sim/) | RoboDojo-Sim's forty-two bimanual manipulation tasks: a program that controls the robot, developed on one layout set and scored on two others, task success (a first implementation, not yet run against RoboDojo) | Codex writing `policy.py` (`utils/agent/task.py`), GLM 5.3 Flash, RoboDojo evaluating it as an XPolicyLab policy whose program runs in a sandbox of its own | [`prompt_only`](robodojo_sim/prompt_only/) |
| [`agentdyn/`](agentdyn/) | AgentDyn's frozen benign and injected cases (Shopping, GitHub, DailyLife), clean utility and attack success rate | `bridge.py:run`: AgentDyn's native agent loop, with the defense bundle's hooks around every tool call | [`evosafe`](agentdyn/evosafe/) (EvoSafeHarness) |

## Demos

These show one part of the framework and report no number.

| Folder | What it shows |
|---|---|
| [`lean_swarm/`](lean_swarm/) | Check and deploy a finished adapter: five Codex agents prove one formal-conjectures theorem together; no training config |
| [`docs/_examples/first_training/`](../docs/_examples/first_training/) | The whole loop offline, with a scripted learner and no model ([Quick Start](../docs/get-started/quickstart.md)) |
| [`docs/_examples/first_model/`](../docs/_examples/first_model/) | A first training with a real model, on included data, without benchmark assets ([First real-model training](../docs/get-started/first-model.md)) |
| [`claude_subscription/`](claude_subscription/) | Acting and learning on your Claude subscription with no API key: a text-reply learner, an acting pipeline on the same endpoint, and a workspace learner ([Claude subscription](../docs/guides/claude-subscription.md)) |

The methods themselves are described in [`docs/methods/`](../docs/methods/).

## Run an example

Run from the repository root (dependency groups need pip 25.1+). Prepare the task once, then check, train and
evaluate with a method's config:

```bash
python -m pip install -e . --group skillopt --group gepa
python examples/officeqa/utils/prepare.py --csv /path/to/officeqa_full.csv --docs /path/to/treasury_bulletins_parsed
export FIREWORKS_API_KEY=...                   # the configs name GLM models on Fireworks
hermit check --config examples/officeqa/gepa/smoke.yaml
hermit train --config examples/officeqa/gepa/smoke.yaml
hermit eval  --config examples/officeqa/gepa/smoke.yaml --adapter final
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
- A benchmark comes as a task set in Harbor's format, the one protocol every task enters by, not as Docker code or a
  loop of its own: `utils/prepare.py` downloads or builds the task directories (`task.toml`, `environment/`,
  `tests/test.sh` writing the reward) and indexes them into `tasks.jsonl`, the configs name `agent.name: harbor` and
  `judge.path: hermit.harbor.judge`, and a benchmark whose tasks carry state from one to the next (CL-Bench,
  ServeLearnBench) is one multi-step task per stream. See [Benchmarks](../docs/framework/task.md#benchmarks).
  The examples that still own their loop (CL-Bench, ServeLearnBench, OfficeQA's direct harness) predate this rule
  and are to be moved onto it; no new example drives Docker or a model loop itself.
- A method on an existing task: `<method>/smoke.yaml` and `run.yaml` (a stream's launcher may take a prefix split in
  place of a smoke config, `MW_TRAIN_SPLIT=...`, as the CL-Bench folders do), and a row in `docs/methods/<method>.md`.
- A method: `docs/methods/<method>.md` in the shape of the others: what it does, one step, knobs, configs, commands.
