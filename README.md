<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/wordmark-dark.svg">
  <img src="assets/wordmark-light-card.svg" alt="Hermit" width="360">
</picture>

### Train/Evolve Your Harness Adapter for Continual Learning and RSI

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-3776AB.svg?logo=python&logoColor=white)](pyproject.toml)
[![Version](https://img.shields.io/badge/version-0.2.0-C6743C.svg)](pyproject.toml)
[![Docs](https://img.shields.io/badge/docs-read-1B1D22.svg)](docs/index.md)

**[Get started](#get-started)** · **[What is an adapter?](#what-is-a-harness-adapter)** · **[Algorithms](#supported-learning-methods)** · **[Tutorials](docs/tutorials/deploy-an-adapter.md)** · **[Recipes](docs/recipes/officeqa.md)** · **[Examples](#examples)** · **[Commands](docs/framework/commands.md)**

</div>

---

Hermit is a unified framework for training, evaluating, and deploying harness adapters. Bring your tasks and feedback; choose an existing learning method or build your own.

## What Is a Harness Adapter?

A **harness adapter** is a **trainable external harness** that adapts an existing agent to new scenarios **without modifying its original harness or model.** Like a **model adapter** for a model, it adapts an agent while keeping the base system fixed.

<p align="center">
<img src="assets/harness-adapter.gif" alt="A trainable external harness evolves from task feedback while the agent&#x27;s original harness and model stay fixed." width="720">
</p>

This design offers three practical benefits:

<table align="center">
  <thead>
    <tr><th>Benefit</th><th>What it means</th></tr>
  </thead>
  <tbody>
    <tr><td><strong>Trainable</strong></td><td>Learn from task feedback.</td></tr>
    <tr><td><strong>No changes to your existing harness</strong></td><td>Keep your acting harness and model fixed.</td></tr>
    <tr><td><strong>Transferable</strong></td><td>Reuse across compatible harnesses.</td></tr>
  </tbody>
</table>

## A Unified Framework for Harness Evolution

Hermit provides an RL-style training loop for evolving harness adapters:

<p align="center">
<picture>
<source media="(prefers-color-scheme: dark)" srcset="assets/training-loop-dark.svg">
<img src="assets/training-loop-light.svg" alt="Animated training flow: tasks run through the acting agent and harness adapter, feedback reaches the Evolver, and checked updates become the next adapter version." width="100%">
</picture>
</p>

### Easy to Use

#### Run from the Command Line:

<p align="center">
<img src="assets/install-and-use.gif" alt="An animated command overview of installing Hermit, training an adapter, deploying it, and converting it into a plugin." width="100%">
</p>

#### Customize Your Training Loop:

<p align="center">
<img src="assets/training-loop.gif" alt="Training pseudocode: initialize the adapter, run and judge tasks, call evolver.step, check and commit the proposal, then finalize." width="100%">
</p>

### Supported Learning Methods

| Status | Methods |
| --- | --- |
| ✅ **Supported** | [GEPA](docs/methods/gepa.md) · [SkillOpt](docs/methods/skillopt.md) · [MetaWorld Model](docs/methods/mwm.md) |
| 🚧 **Coming Soon** | [Harness²](https://github.com/google-research/harness2) · [ACE](https://github.com/ace-agent/ace) · [Continual Harness](https://github.com/sethkarten/continual-harness) |

## Get Started

| [Use a trained adapter](docs/tutorials/deploy-an-adapter.md) | [Train for your tasks](docs/get-started/first-model.md) | [Build a new method](docs/framework/custom-evolver.md) |
| --- | --- | --- |
| Deploy to your agent. | Use your tasks and feedback. | Implement and compare your idea. |

<details>
<summary>Run the Local Training Demo · No API Key Required</summary>

The included **local training walkthrough** connects feedback, an adapter update, evaluation, and deployment. A scripted learner reads the judge's feedback and writes the learned rule.

Python 3.11+ and Git · No model account, GPU, or dataset download required

### 01 Install

```bash
git clone https://github.com/haizhongzheng/MetaHarnessAdapter.git
cd MetaHarnessAdapter
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

### 02 Train and Evaluate

```bash
harnessadapter check --config docs/_examples/first_training/run.yaml
harnessadapter eval --config docs/_examples/first_training/run.yaml --adapter none
harnessadapter train --config docs/_examples/first_training/run.yaml
harnessadapter eval --config docs/_examples/first_training/run.yaml --adapter final
```

The teaching agent starts by echoing lowercase text. After a failed attempt, the learner writes `Output format: uppercase.` into `ADAPTER.md`. Later executions load the updated adapter.

**Expected in this demo:** baseline `0.0` → final `1.0`, with zero execution errors.

This deterministic example verifies the training contract. Its constructed score gain is not a real-model performance result. The example uses trusted local Python with the learner-program sandbox disabled.

### 03 Inspect and Deploy

```bash
cat .demo/first-training/first-training/checkout/ADAPTER.md
harnessadapter deploy \
  --adapter .demo/first-training/first-training/checkout \
  --workspace .demo/first-training/workspace \
  --host codex
cat .demo/first-training/workspace/AGENTS.md
```

The generated `AGENTS.md` contains the learned rule. Start Codex in that workspace to use it.

[Full walkthrough, expected outputs, and repeat-run instructions →](docs/get-started/quickstart.md)

**Ready for a real agent?** [Train with an API model](docs/get-started/first-model.md) · [Run OfficeQA](docs/recipes/officeqa.md) · [Bring your own task](docs/tutorials/your-own-task.md)

</details>

<details>
<summary>What You Need for Each Starting Point</summary>

| Your goal | What Hermit provides | What you bring |
| --- | --- | --- |
| **Use an existing adapter**<br>[Use an adapter →](docs/tutorials/deploy-an-adapter.md) | Workspace deployment and native plugin conversion for a compatible adapter. | An adapter, a supported agent host, and any dependencies the adapter needs. |
| **Adapt to your own tasks**<br>[Train your adapter →](docs/get-started/first-model.md) | Existing templates and Evolvers, with a Trainer that connects execution, feedback, checks, and versioning. | Your tasks, acting agent, evaluator, and model access. |
| **Develop a learning method or adapter design**<br>[Build a learning method →](docs/framework/custom-evolver.md) | Editable adapter specifications and an Evolver interface for implementing and comparing methods on shared task pipelines. | Your adapter design or update algorithm, plus an evaluation task. |

</details>

<details>
<summary>More Ways to Start: Deploy a Seed, Train OfficeQA, or Use Your Own Server</summary>

### Deploy an Adapter Without Training

```bash
mkdir -p .demo
cp -R harnessadapter/algorithms/templates/prompt_only .demo/adapter
harnessadapter deploy --adapter .demo/adapter --workspace .demo/codex-workspace --host codex
cat .demo/codex-workspace/AGENTS.md
```

### Train on OfficeQA

Prepare the dataset and model endpoint as described in the [OfficeQA recipe](docs/recipes/officeqa.md). GEPA requires the `gepa` installation extra; SkillOpt requires `skillopt`. Sandboxed agent Evolvers require Linux with `bubblewrap`.

```bash
python -m pip install -e '.[gepa]'
python examples/officeqa/utils/prepare.py /path/to/officeqa/prepared
harnessadapter check --config examples/officeqa/gepa/smoke.yaml
harnessadapter train --config examples/officeqa/gepa/smoke.yaml
harnessadapter eval --config examples/officeqa/gepa/smoke.yaml --adapter final
```

### Use Your Own Model Server

Models use `<backend>/<model id>` references. See [Model backends](docs/guides/model-backends.md) for endpoint configuration and requirements.

```bash
vllm serve Qwen/Qwen3-30B-A3B-Instruct-2507-FP8 --enable-auto-tool-choice --tool-call-parser hermes
harnessadapter backends check vllm/Qwen/Qwen3-30B-A3B-Instruct-2507-FP8
harnessadapter train --config examples/officeqa/gepa/smoke.yaml model=vllm/Qwen/Qwen3-30B-A3B-Instruct-2507-FP8
```

### Convert a Trained Adapter into a Plugin

```bash
harnessadapter convert --adapter DIR --output OUT --target codex
# Other targets: claude-code, pi, hermes
```

</details>

## Supported Agents and Adapters

| Host | Deploy into a workspace | Convert to a plugin |
| --- | --- | --- |
| Codex | Yes | Yes |
| Claude Code | Yes | Yes |
| Pi | Yes | Yes |
| Hermes | — | Yes |
| Your own agent loop | Consume the adapter's context and tools directly. |  |

<details>
<summary>Included Adapter Templates</summary>

- `prompt_only` — context-only adaptation.

- `lean_collab` — skills, a shared lemma library, and a team service for agents proving Lean theorems.

- `mwm` — memory, a briefing, and callable programs behind a single `wm` tool.

[Customize a template →](docs/tutorials/customize-an-adapter.md)

</details>

<details>
<summary>How Adapters, Plugins, and Learning Methods Relate</summary>

| Concept or approach | What it provides | How it relates to Hermit |
| --- | --- | --- |
| Model adapter<br>e.g. LoRA | Adaptation through additional model parameters. | A harness adapter is a trainable external harness. Its training leaves the original harness and model fixed. |
| Agent plugin | A host-native way to package and load extensions. | A trained harness adapter can be delivered as a plugin. Its training specification also defines what can be learned and checked. |
| A learning-method implementation | A particular update strategy, such as reflective text edits or skill optimization. | The method runs as an Evolver; Hermit supplies the shared training infrastructure. |
| Your own training scripts | A workflow tailored to your task and algorithm. | Hermit lets you supply the acting pipeline, judge, and learning method while reusing records, checks, budgets, recovery, and version history. |

The adapter is the learning artifact. The Evolver is the learning method. Hermit provides the platform that connects them to tasks and deployment.

</details>

## Examples

Runnable recipes, with evaluation details in each adapter’s `CARD.md`.

| Recipe | Task and methods | Recorded result or evaluation |
| --- | --- | --- |
| [OfficeQA](docs/recipes/officeqa.md) | 133 questions on U.S. Treasury Bulletins. Prompt-only, Codex, GEPA, SkillOpt. | Scores and run details in each adapter's card. |
| [OfficeQA · paper setting](docs/recipes/officeqa-paper.md) | SkillOpt's 50 / 24 / 172 split; exact-match scoring. SkillOpt and GEPA. | Test means: 0.576 (SkillOpt), 0.564 (GEPA). 172 questions × 3 repeats. [Adapters & cards](https://github.com/haizhongzheng/MetaHarnessAdapter/tree/main/examples/officeqa_paper). |
| [Lean collaboration](docs/recipes/lean-collaboration.md) | Five Codex agents proving theorems with a shared team service; a finished adapter. | 8/8 development problems; 12/24 held-out problems so far. |
| [ServeLearnBench](docs/recipes/servelearnbench.md) | retail_l3: 624 serving cases in eight windows. MWM and prompt-only. | Per-window tests scored with `slb score`. |
| [CL-Bench](docs/recipes/clbench.md) | Database and poker streams. MWM and prompt-only. | Accuracy reported per stream. |

[Browse the recipes](docs/recipes/index.md) · [Read the evaluation guide](docs/guides/evaluation.md)

## Contributing

Help extend Hermit by [implementing an Evolver](docs/framework/custom-evolver.md), [connecting a task](docs/tutorials/your-own-task.md), or [reporting a reproduction or integration issue](https://github.com/haizhongzheng/MetaHarnessAdapter/issues).

<details>
<summary>Project Updates · October 2026</summary>
：
- `convert` emits installable plugins for Claude Code, Codex, Pi, and Hermes. [#6](https://github.com/haizhongzheng/MetaHarnessAdapter/pull/6)

- MWM is a built-in template and Evolver, with CL-Bench and ServeLearnBench examples. [#7](https://github.com/haizhongzheng/MetaHarnessAdapter/pull/7), [#8](https://github.com/haizhongzheng/MetaHarnessAdapter/pull/8)

- GEPA and SkillOpt run as built-in Evolvers, including OfficeQA in the SkillOpt paper's setting. [#2](https://github.com/haizhongzheng/MetaHarnessAdapter/pull/2)

- v0.2.0 introduces adapter templates, Evolvers, the Trainer, and evolving tasks.

</details>

## Documentation

- [Installation](docs/get-started/installation.md)

- [First training cycle](docs/get-started/quickstart.md)

- [What is a harness adapter?](docs/framework/what-is-a-harness-adapter.md)

- [Use & train adapters](docs/framework/adapter-lifecycle.md)

- [Tutorials](docs/tutorials/deploy-an-adapter.md)

- [Architecture](docs/framework/overview.md)

- [Configuration](docs/framework/configuration.md)

- [Commands](docs/framework/commands.md)

- [Adapter manifest](docs/framework/adapter-manifest.md)

Build the docs with `docs/build.sh` and preview them with `docs/serve.sh`.

## License

[Apache-2.0](LICENSE)
