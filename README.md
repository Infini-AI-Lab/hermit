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

<table align="center">
<thead>
<tr>
<th>Status</th>
<th>Methods</th>
</tr>
</thead>
<tbody>
<tr>
<td>✅ <strong>Supported</strong></td>
<td><a href="docs/methods/gepa.md">GEPA</a> · <a href="docs/methods/skillopt.md">SkillOpt</a> · <a href="docs/methods/mwm.md">MetaWorld Model</a></td>
</tr>
<tr>
<td>🚧 <strong>Coming Soon</strong></td>
<td><a href="https://github.com/google-research/harness2">Harness²</a> · <a href="https://github.com/ace-agent/ace">ACE</a> · <a href="https://github.com/sethkarten/continual-harness">Continual Harness</a> · <a href="https://arxiv.org/abs/2610.12668">ReHarness</a></td>
</tr>
</tbody>
</table>

## Get Started

### Install

Python 3.11+ and Git

```bash
git clone https://github.com/haizhongzheng/MetaHarnessAdapter.git
cd MetaHarnessAdapter
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

### Quick Start

The included **local training walkthrough** connects feedback, an adapter update, evaluation, and deployment. A scripted learner reads the judge's feedback and writes the learned rule.

No model account, GPU, or dataset download required.

#### Train and Evaluate

```bash
harnessadapter check --config docs/_examples/first_training/run.yaml
harnessadapter eval --config docs/_examples/first_training/run.yaml --adapter none
harnessadapter train --config docs/_examples/first_training/run.yaml
harnessadapter eval --config docs/_examples/first_training/run.yaml --adapter final
```

The teaching agent starts by echoing lowercase text. After a failed attempt, the learner writes `Output format: uppercase.` into `ADAPTER.md`. Later executions load the updated adapter.

**Expected in this demo:** baseline `0.0` → final `1.0`, with zero execution errors.

This deterministic example verifies the training contract. Its constructed score gain is not a real-model performance result. The example uses trusted local Python with the learner-program sandbox disabled.

#### Inspect and Deploy

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

### Examples

Runnable recipes, with evaluation details in each adapter’s `CARD.md`.

<table align="center">
<thead>
<tr>
<th>Recipe</th>
<th>Task and methods</th>
<th>Recorded result or evaluation</th>
</tr>
</thead>
<tbody>
<tr>
<td><a href="docs/recipes/officeqa.md">OfficeQA</a></td>
<td>U.S. Treasury Bulletins in the SkillOpt paper's setting: its 50 / 24 / 172 split, source pages, exact match; the agent in direct chat or SkillOpt's Codex harness. GEPA, SkillOpt.</td>
<td>Test accuracy, direct chat: 0.576 (SkillOpt), 0.564 (GEPA), 0.415 without a skill. Codex harness: 0.628 (SkillOpt), 0.595 (GEPA), 0.543 without a skill. 172 questions × 3 repeats. <a href="examples/officeqa/README.md">Results &amp; adapters</a>.</td>
</tr>
<tr>
<td><a href="docs/recipes/lean-collaboration.md">Lean collaboration</a></td>
<td>Five Codex agents proving theorems with a shared team service; a finished adapter.</td>
<td>8/8 development problems; 12/24 held-out problems so far.</td>
</tr>
<tr>
<td><a href="docs/recipes/servelearnbench.md">ServeLearnBench</a></td>
<td>retail_l3: 624 serving cases in eight windows. MWM and prompt-only.</td>
<td>Per-window tests scored with <code>slb score</code>.</td>
</tr>
<tr>
<td><a href="docs/recipes/clbench.md">CL-Bench</a></td>
<td>Database and poker streams. MWM and prompt-only.</td>
<td>Accuracy reported per stream.</td>
</tr>
</tbody>
</table>

[Browse the recipes](docs/recipes/index.md) · [Read the evaluation guide](docs/guides/evaluation.md)

#### More Starting Points

<table align="center">
<thead>
<tr>
<th><a href="docs/tutorials/deploy-an-adapter.md">Use a trained adapter</a></th>
<th><a href="docs/get-started/first-model.md">Train for your tasks</a></th>
<th><a href="docs/framework/custom-evolver.md">Build a new method</a></th>
</tr>
</thead>
<tbody>
<tr>
<td>Deploy to your agent.</td>
<td>Use your tasks and feedback.</td>
<td>Implement and compare your idea.</td>
</tr>
</tbody>
</table>

#### What You Need for Each Starting Point

<table align="center">
<thead>
<tr>
<th>Your goal</th>
<th>What Hermit provides</th>
<th>What you bring</th>
</tr>
</thead>
<tbody>
<tr>
<td><strong>Use an existing adapter</strong><br><a href="docs/tutorials/deploy-an-adapter.md">Use an adapter →</a></td>
<td>Workspace deployment and native plugin conversion for a compatible adapter.</td>
<td>An adapter, a supported agent host, and any dependencies the adapter needs.</td>
</tr>
<tr>
<td><strong>Adapt to your own tasks</strong><br><a href="docs/get-started/first-model.md">Train your adapter →</a></td>
<td>Existing templates and Evolvers, with a Trainer that connects execution, feedback, checks, and versioning.</td>
<td>Your tasks, acting agent, evaluator, and model access.</td>
</tr>
<tr>
<td><strong>Develop a learning method or adapter design</strong><br><a href="docs/framework/custom-evolver.md">Build a learning method →</a></td>
<td>Editable adapter specifications and an Evolver interface for implementing and comparing methods on shared task pipelines.</td>
<td>Your adapter design or update algorithm, plus an evaluation task.</td>
</tr>
</tbody>
</table>

#### Deploy an Adapter Without Training

```bash
mkdir -p .demo
cp -R harnessadapter/algorithms/templates/prompt_only .demo/adapter
harnessadapter deploy --adapter .demo/adapter --workspace .demo/codex-workspace --host codex
cat .demo/codex-workspace/AGENTS.md
```

#### Train on OfficeQA

Prepare the dataset and model endpoint as described in the [OfficeQA recipe](docs/recipes/officeqa.md). The agent is SkillOpt's own rollout, so the `skillopt` installation extra is needed; GEPA also requires `gepa`. Sandboxed agent Evolvers require Linux with `bubblewrap`.

```bash
python -m pip install -e '.[skillopt,gepa]'
python examples/officeqa/utils/prepare.py --csv /path/to/officeqa_full.csv --docs /path/to/treasury_bulletins_parsed
harnessadapter check --config examples/officeqa/gepa/smoke.yaml
harnessadapter train --config examples/officeqa/gepa/smoke.yaml
harnessadapter eval --config examples/officeqa/gepa/smoke.yaml --adapter final
```

#### Use Your Own Model Server

Models use `<backend>/<model id>` references. See [Model backends](docs/guides/model-backends.md) for endpoint configuration and requirements.

```bash
vllm serve Qwen/Qwen3-30B-A3B-Instruct-2507-FP8 --enable-auto-tool-choice --tool-call-parser hermes
harnessadapter backends check vllm/Qwen/Qwen3-30B-A3B-Instruct-2507-FP8
harnessadapter train --config examples/officeqa/gepa/smoke.yaml model=vllm/Qwen/Qwen3-30B-A3B-Instruct-2507-FP8 llm=vllm/Qwen/Qwen3-30B-A3B-Instruct-2507-FP8
```

#### Convert a Trained Adapter into a Plugin

```bash
harnessadapter convert --adapter DIR --output OUT --target codex
# Other targets: claude-code, pi, hermes
```

## Contributing

Help extend Hermit by [implementing an Evolver](docs/framework/custom-evolver.md), [connecting a task](docs/tutorials/your-own-task.md), or [reporting a reproduction or integration issue](https://github.com/haizhongzheng/MetaHarnessAdapter/issues).


## License

[Apache-2.0](LICENSE)
