<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/wordmark-dark.svg">
  <img src="assets/wordmark-light-card.svg" alt="MetaHarnessAdapter" width="572">
</picture>

### Train harness adapters for existing agents

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-3776AB.svg?logo=python&logoColor=white)](pyproject.toml)
[![Version](https://img.shields.io/badge/version-0.2.0-C6743C.svg)](pyproject.toml)
[![Docs](https://img.shields.io/badge/docs-read-1B1D22.svg)](docs/index.md)

**[Get started](docs/get-started/quickstart.md)** · **[What is an adapter?](docs/framework/what-is-a-harness-adapter.md)** · **[Tutorials](docs/tutorials/deploy-an-adapter.md)** · **[Recipes](docs/recipes/officeqa.md)** · **[Examples](examples/README.md)** · **[Commands](docs/framework/commands.md)**

</div>

---

MetaHarnessAdapter improves the instructions, skills, roles and tools an existing AI agent works with, using
feedback from tasks the agent has attempted. The agent is not retrained. What it learns is kept in a directory it
loads from the outside, a **harness adapter**, and the framework trains that directory the way a training framework
trains weights: you bring tasks and a way to score the answers; the framework runs the agent, collects feedback, and
lets a learning method revise the adapter, one checked and versioned step at a time.

## News

- **[2026/10]** `harnessadapter convert` emits an adapter as an installable plugin for **Claude Code, Codex, Pi
  and Hermes** ([#6](https://github.com/haizhongzheng/MetaHarnessAdapter/pull/6)).
- **[2026/10]** **MetaWorld Model (MWM)** is a built-in template and Evolver, with CL-Bench and ServeLearnBench
  examples ([#7](https://github.com/haizhongzheng/MetaHarnessAdapter/pull/7),
  [#8](https://github.com/haizhongzheng/MetaHarnessAdapter/pull/8)).
- **[2026/10]** **GEPA** and **SkillOpt** run as built-in Evolvers, with OfficeQA in the SkillOpt paper's setting
  ([#2](https://github.com/haizhongzheng/MetaHarnessAdapter/pull/2)).
- **[2026/10]** **v0.2.0**: the evolution framework, with adapter templates, Evolvers, the Trainer and evolving tasks.

## Why MetaHarnessAdapter

| You want to... | MetaHarnessAdapter gives you |
|---|---|
| Make Codex, Claude Code or your own agent better at *your* tasks without touching its weights | An adapter (context, skills, roles, MCP tools) learned from the agent's own attempts and your judge |
| Compare learning methods on the same task | One task interface; swap `evolver.name` between `prompt_only`, `codex`, `claude`, `gepa`, `skillopt`, `mwm` |
| Know exactly what changed and why | Every accepted step is a Git commit; runs are branches, checkpoints and finals are tags; each final adapter carries a `CARD.md` with its scores |
| Ship what was learned | `deploy` writes it into a project workspace; `convert` packages it as a native plugin for four hosts |

## How it works

```text
             ┌──────────── Trainer: data, rollouts, checks, budgets, Git ────────────┐
             │                                                                       │
 tasks ──▶  acting agent + adapter  ──▶  answers  ──▶  judge  ──▶  learning material
             ▲                                                          │
             │                                                          ▼
             └──────── checked, committed new adapter version  ◀──  Evolver (learner)
```

The four parts map onto the parts of ordinary model training:

| Part | Role | Like |
|---|---|---|
| **Adapter template** | The structure and its specification: which files are learnable, which are fixed, and a checker | the initial model |
| **Evolver** | The learning algorithm: a learner's harness (one model call, a Codex or Claude agent, GEPA, SkillOpt, MWM) | the optimizer |
| **Trainer** | The outer loop: data, rollouts, learning material, checks, Git, budgets and records | the trainer |
| **Evolving task** | Your task description, data, judge and acting-agent pipeline | data + reward + rollout |

An adapter is one directory with two ends. The **consumption end** is what an acting agent receives, in the shape of a
plugin: `ADAPTER.md`, `skills/`, `agents/` and tools behind an MCP entry, optionally backed by a standalone service.
The **training end** is what a learner works within: `adapter.yaml` (learnable and fixed files, the check),
`EVOLVER.md`, and the Git tree of every version. See [What is a harness adapter?](docs/framework/what-is-a-harness-adapter.md)

## Installation

```bash
git clone https://github.com/haizhongzheng/MetaHarnessAdapter.git
cd MetaHarnessAdapter
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -e .              # add '.[gepa]' or '.[skillopt]' for those Evolvers
harnessadapter --help
```

Python 3.11+ and Git are required. Sandboxed agent Evolvers need Linux with `bubblewrap`; inspecting and deploying
adapters works on macOS too. Per-integration requirements are in [Installation](docs/get-started/installation.md).

## Quick start

**Deploy an adapter** (no model calls, no dataset):

```bash
mkdir -p .demo && cp -R harnessadapter/algorithms/templates/prompt_only .demo/adapter
harnessadapter deploy --adapter .demo/adapter --workspace .demo/codex-workspace --host codex
cat .demo/codex-workspace/AGENTS.md     # the adapter's context, as Codex will read it
```

**Train one** from task feedback:

```bash
pip install -e '.[skillopt,gepa]'
python examples/officeqa/utils/prepare.py --csv /path/to/officeqa_full.csv --docs /path/to/treasury_bulletins_parsed
harnessadapter check --config examples/officeqa/gepa/smoke.yaml    # catch setting mistakes before spending
harnessadapter train --config examples/officeqa/gepa/smoke.yaml    # resumes after a crash
harnessadapter eval  --config examples/officeqa/gepa/smoke.yaml --adapter final
```

**Ship it** into a project, or as a plugin:

```bash
harnessadapter deploy  --adapter DIR --workspace WORKSPACE --host claude-code
harnessadapter convert --adapter DIR --output OUT --target codex   # or claude-code, pi, hermes
```

The full walkthrough is in the [Quick Start](docs/get-started/quickstart.md).

## What's included

**Evolvers** (learning methods)

| `evolver.name` | Method |
|---|---|
| `prompt_only` | One model call per step rewrites `ADAPTER.md` from the step's results |
| `codex` / `claude` | A Codex or Claude Code agent edits the adapter from the step's learning material |
| `gepa` | [GEPA](https://github.com/gepa-ai/gepa): reflective text evolution with Pareto selection |
| `skillopt` | [SkillOpt](https://github.com/microsoft/SkillOpt): a few validation-gated skill edits per step, under a learning rate |
| `mwm` | MetaWorld Model: a learner maintains a world model of the environment after every block of episodes |

To write your own, subclass `Evolver` and implement `step`; see [Custom Evolver](docs/framework/custom-evolver.md).

**Adapter templates**: `prompt_only` (context only), `lean_collab` (skills, a shared lemma library and a team service
for agents proving Lean theorems together), `mwm` (memory, briefing and callable programs behind one `wm` tool).

**Hosts**

| Host | `deploy` into a workspace | `convert` to a plugin |
|---|---|---|
| Codex | ✓ | ✓ |
| Claude Code | ✓ | ✓ |
| Pi | | ✓ |
| Hermes | | ✓ |
| Your own agent loop | reads the adapter's context and tools directly | |

## Recipes and results

| Recipe | Task | Evolvers | Result |
|---|---|---|---|
| [OfficeQA](examples/officeqa/README.md) | U.S. Treasury Bulletins in the SkillOpt paper's setting: 50 / 24 / 172 split, source pages, exact match | `prompt_only`, `codex`, `gepa`, `skillopt` | test accuracy 0.576 (`skillopt`), 0.564 (`gepa`) vs 0.415 without a skill; 172 questions × 3 |
| [Lean collaboration](docs/recipes/lean-collaboration.md) | Five Codex agents proving Lean theorems with one shared team service | (finished adapter) | 8/8 on the development set; 12/24 held-out problems so far |
| [ServeLearnBench](docs/recipes/servelearnbench.md) | retail_l3: 624 serving cases in 8 windows | `mwm`, `prompt_only` | per-window tests with `slb score` |
| [CL-Bench](docs/methods/mwm.md) | database and poker streams | `mwm`, `prompt_only` | accuracy per stream |

Every trained adapter in [`examples/`](examples/README.md) comes with its `CARD.md`: how it was trained, how it
scored, and where it has not been tested.

## Documentation

1. [What is a harness adapter?](docs/framework/what-is-a-harness-adapter.md): the package from both ends
2. [Installation](docs/get-started/installation.md) and [Quick Start](docs/get-started/quickstart.md)
3. [Using and training adapters](docs/framework/adapter-lifecycle.md): files, deployment and handoffs
4. [Tutorials](docs/tutorials/deploy-an-adapter.md): deploy an adapter, reproduce a result, bring your own task,
   customize an adapter, write your own Evolver
5. [Overview](docs/framework/overview.md) and the components: [Adapter](docs/framework/adapter.md),
   [Evolver](docs/framework/evolver.md), [Trainer](docs/framework/trainer.md), [Task](docs/framework/task.md)
6. Reference: [Configuration](docs/framework/configuration.md), [Commands](docs/framework/commands.md),
   [Adapter manifest](docs/framework/adapter-manifest.md)

Build the docs site locally with `docs/build.sh`, and serve it with `docs/serve.sh`.

## License

[Apache-2.0](LICENSE)
