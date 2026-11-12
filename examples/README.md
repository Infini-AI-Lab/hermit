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
    benchmark.yaml   a benchmark of Harbor's registry, in place of prepare.py: the params its configs share and a
                     dataset: block, from which python -m hermit.harbor.dataset prepare writes tasks.jsonl, splits/
                     and the rest beside it (Add an example, below)
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
| [`agentdyn/`](agentdyn/) | AgentDyn's frozen benign and injected cases (Shopping, GitHub, DailyLife), clean utility and attack success rate | `bridge.py:run`: AgentDyn's native agent loop, with the defense bundle's hooks around every tool call | [`evosafe`](agentdyn/evosafe/) (EvoSafeHarness) |

## Harbor benchmarks

Five benchmarks of Harbor's registry, each with a baseline and the method whose paper's protocol fits it, packaged so
that any of their runs can be handed to someone with a Docker host. Each folder's README gives the protocol, the
commands and the results table, empty until the runs are made: the configs were checked on each benchmark's two smoke
rows only. Every config comes twice, Codex with GPT-5.6-Luna on a ChatGPT login (`*codex*.yaml`) and Claude Code with
Claude Haiku 5.5 on a Claude subscription (`*claude*.yaml`), both at medium effort, acting and learning.

| Folder | Tasks | Score | Baseline | Method, in its paper's protocol |
|---|---|---|---|---|
| [`terminal_bench_2_0/`](terminal_bench_2_0/) | 89 terminal tasks, an hour each | pass@1 of the tasks' tests | the CLI with no adapter, each task twice (rollouts #3 and #4) | [`ahe_adapter`](terminal_bench_2_0/ahe_adapter/): the AHE paper's own on this benchmark, ten iterations over each task twice (#1, #2), tested on #3 and #4 |
| [`swebench_verified/`](swebench_verified/) | 500 GitHub issues | resolve rate | no adapter, each issue once | nothing trains: Terminal-Bench 2.0's final AHE adapter, frozen, against AHE's seed (the AHE and RRSI papers' transfer test) |
| [`algotune/`](algotune/) | 154 speedup tasks | harmonic mean of the speedups | no adapter, the 108 test tasks | [`skillopt`](algotune/skillopt/): SkillOpt's split 2:1:7, 4 epochs, its gate, slow update and meta skill |
| [`terminal_bench_science/`](terminal_bench_science/) | 70 science workflows, 8 hours each | pass@1 | no adapter, each task twice (#3, #4) | Terminal-Bench 2.0's AHE adapter, frozen; [`ahe_adapter`](terminal_bench_science/ahe_adapter/) in Terminal-Bench 2.0's protocol |
| [`labbench/`](labbench/) | 181 FigQA figure questions | accuracy | no adapter, the 127 test questions | [`skillopt`](labbench/skillopt/), as for AlgoTune |

**What a machine needs.**

- Linux with Docker, which runs every trial in the task's container, and bubblewrap for the learners' sandbox
  ([Installation](../docs/get-started/installation.md)).
- Harbor 0.25.0 in a venv of its own, where the configs look for it:

  ```bash
  uv venv --python 3.13 ~/.cache/hermit/harbor-venv
  uv pip install --python ~/.cache/hermit/harbor-venv/bin/python harbor==0.25.0
  ```

- Hermit from this checkout, with SkillOpt's group for its configs: `python -m pip install -e . --group skillopt`.
- The sign-in of the profile's CLI: `codex login` on a ChatGPT plan for the Codex configs; `claude setup-token`, its
  token exported as `CLAUDE_CODE_OAUTH_TOKEN`, for the Claude Code configs. Harbor installs the CLI in each task's
  container; a method run also needs it on this machine, where its learners run.
- Room. No trial starts while Docker's disk has less than 25 GB free (the benchmark files' `disk_gate_gb`); a trial's
  image is built or pulled for it: Terminal-Bench 2.0's prebuilt images, 120-150 GB in all, each removed after its
  trial; SWE-bench's image of the issue, one for each of the 500, which stays in Docker's build cache; AlgoTune's
  8 GB, the same layers for every task. An AlgoTune container takes 8 CPUs and up to 64 GB, six at a time.

**Run a benchmark** from the repository root:

```bash
python -m hermit.harbor.dataset prepare examples/<folder>/utils/benchmark.yaml   # once: download, check the lock
hermit eval  --config examples/<folder>/baseline/codex.yaml --adapter none      # the baseline
hermit train --config examples/<folder>/<method>/smoke_codex.yaml               # the method on two tasks
hermit train --config examples/<folder>/<method>/run_codex.yaml                 # the method, then its test
```

The same command run again resumes: a finished trial comes from the profile's trial cache (`jobs_dir`), a training
run continues after its last step.

**Hand a run to someone.** Each run below is one command on one machine and shares nothing with the others but its
profile's trial cache, so each can go to a different person; it sends back its eval folder,
`<folder>/<method or baseline>/runs/<run name>/eval/<adapter>/` (`summary.json` and `results.jsonl`, in which no token
or key is written), whose numbers go into the README's results table. The trials are those of one profile.

| Folder | Run | Command | Trials | A trial's limit |
|---|---|---|---|---|
| `terminal_bench_2_0` | baseline | `hermit eval --config baseline/<p>.yaml --adapter none` | 178 | 1 h |
| | AHE's seed | `hermit eval --config ahe_adapter/run_<p>.yaml --adapter root` | 178 | |
| | AHE | `hermit train --config ahe_adapter/run_<p>.yaml` | 1,780, then 178 | |
| `swebench_verified` | baseline | `hermit eval --config baseline/<p>.yaml --adapter none` | 500 | 50 min |
| | AHE's seed | `... --adapter "$PWD/hermit/adapter/templates/ahe"` | 500 | |
| | transfer | `... --adapter "$PWD/ahe-final"`, after Terminal-Bench 2.0's AHE run | 500 | |
| `algotune` | baseline | `hermit eval --config baseline/<p>.yaml --adapter none` | 108 | 1 h, and the timing |
| | the empty skill | `hermit eval --config skillopt/run_<p>.yaml --adapter root` | 108 | |
| | SkillOpt | `hermit train --config skillopt/run_<p>.yaml` | about 400, then 108 | |
| `terminal_bench_science` | baseline | `hermit eval --config baseline/<p>.yaml --adapter none` | 140 | 8 h |
| | AHE's seed | `... --adapter "$PWD/hermit/adapter/templates/ahe"` | 140 | |
| | transfer | `... --adapter "$PWD/ahe-final"`, after Terminal-Bench 2.0's AHE run | 140 | |
| | AHE | `hermit train --config ahe_adapter/run_<p>.yaml` | 1,400, then 140 | |
| `labbench` | baseline | `hermit eval --config baseline/<p>.yaml --adapter none` | 127 | 5 min |
| | the empty skill | `hermit eval --config skillopt/run_<p>.yaml --adapter root` | 127 | |
| | SkillOpt | `hermit train --config skillopt/run_<p>.yaml` | about 450, then 127 | |

`<p>` is `codex` or `claude`, and a config is `examples/<folder>/...`. Before a long run, its `smoke_<p>.yaml`
checks the task set, the CLI in the containers and the method's learners together. The transfer runs take
Terminal-Bench 2.0's final adapter as a directory, which whoever ran that training exports
([Terminal-Bench 2.0](terminal_bench_2_0/README.md#ahe-adapter) says how) and hands on.

**Another model or CLI.** A config's models are its `agent.params.model` (the acting agent), its learners'
(`evolver.model` for AHE, `llm.model` for SkillOpt) and their `login`s: copy the config, change them, and give the
copy its own `trainer.run_name` and `tree_dir`. `acting_agent: {cli: ...}` takes another CLI (`codex`, `claude`,
`pi`, `opencode`, `dsh`, `mini-swe-agent`: [Benchmarks](../docs/framework/task.md#benchmarks)), and a
`<backend>/<model>` reference with no `login` runs on that backend's key
([Model backends](../docs/guides/model-backends.md)); these configs were checked on the two logins only.

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
  loop of its own. A benchmark of Harbor's registry is packaged as `utils/benchmark.yaml`: the params every method's
  config shares (`agent.params.benchmark` names the file) and a `dataset:` block naming the set
  (`ref: <name>@<version>`), from which `python -m hermit.harbor.dataset prepare utils/benchmark.yaml` downloads the
  tasks outside the checkout (by default under `~/.cache/hermit/harbor-tasks`, or `$HERMIT_HARBOR_TASKS`, a relative one
  the working directory's) and writes beside the file the `tasks` link to them (git-ignored: it names a path of this
  machine), `tasks.jsonl`, `dataset.lock` (the tasks' contents, which every later prepare checks), `splits/` and, with
  `image_sizes: true`, `image_sizes.json`. A benchmark of your own has a `utils/prepare.py` that builds the task
  directories (`task.toml`, `environment/`, `tests/test.sh` writing the reward) and indexes them into `tasks.jsonl`. The
  configs name `agent.name: harbor` and `judge.path: hermit.harbor.judge`, and a benchmark whose tasks carry state from
  one to the next (CL-Bench, ServeLearnBench) is one multi-step task per stream. See
  [Benchmarks](../docs/framework/task.md#benchmarks).
  The examples that still own their loop (CL-Bench, ServeLearnBench, OfficeQA's direct harness) predate this rule
  and are to be moved onto it; no new example drives Docker or a model loop itself.
- A method on an existing task: `<method>/smoke.yaml` and `run.yaml` (a stream's launcher may take a prefix split in
  place of a smoke config, `MW_TRAIN_SPLIT=...`, as the CL-Bench folders do), and a row in `docs/methods/<method>.md`.
- A method: `docs/methods/<method>.md` in the shape of the others: what it does, one step, knobs, configs, commands.
