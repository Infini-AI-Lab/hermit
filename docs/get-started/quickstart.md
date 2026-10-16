# Quick Start

First, deploy a small instruction adapter and inspect what the agent will receive. This takes no model calls or
dataset. Then, if you have the OfficeQA data and model access, run the existing training example to produce an
adapter from task feedback.

## Prerequisites

- Complete [Installation](installation.md) and activate the virtual environment.
- Run the commands below from the repository root.

## 1. Make an adapter you can inspect

Copy the prompt-only template into a working directory:

```bash
mkdir -p .demo
cp -R harnessadapter/algorithms/templates/prompt_only .demo/adapter
```

Use a fresh `.demo/adapter` directory for this walkthrough. It contains:

```text
.demo/adapter/
  adapter.yaml    declares exports and allowed changes
  ADAPTER.md      instructions given to the acting agent
  EVOLVER.md      describes the editable files to a learner
  README.md      explains the template to people
```

The template starts with a placeholder in `ADAPTER.md`. Replace it with a short instruction so its effect on
deployment is visible:

```bash
cat > .demo/adapter/ADAPTER.md <<'EOF'
# Working with numerical documents

Before calculating, check the date, unit, and scale of each source figure.
Show the calculation and identify the source used for each input.
EOF
```

This is a manually written adapter. Training will automate edits to the same file.

## 2. Deploy it into a host workspace

```bash
harnessadapter deploy \
  --adapter .demo/adapter \
  --workspace .demo/codex-workspace \
  --host codex
cat .demo/codex-workspace/AGENTS.md
```

The command prints a JSON deployment report. `AGENTS.md` should contain the instructions you just wrote. Open this
workspace in your acting host to use them on a task. Generating the workspace does not launch Codex or call a model.

For Claude Code, generate a separate workspace:

```bash
harnessadapter deploy \
  --adapter .demo/adapter \
  --workspace .demo/claude-workspace \
  --host claude-code
cat .demo/claude-workspace/CLAUDE.md
```

The prompt-only template exports just text. A template with tools can also generate an MCP server configuration;
roles and skills produce additional host files. A custom agent loop can instead read the adapter's context and add
it to its model messages, as OfficeQA does. See [Using and training adapters](../framework/adapter-lifecycle.md).

## 3. Train an adapter from feedback

To exercise the full interface without model access or an external dataset, run the
[offline interface example](../framework/trainer.md#minimal-example). It verifies the component handoffs and state
storage; its recording-only Evolver does not learn new instructions.

The [OfficeQA recipe](../recipes/officeqa.md) is the existing end-to-end training example. It needs a prepared
OfficeQA dataset and model API access; complete its preparation steps before running:

```bash
harnessadapter check --config examples/officeqa/prompt_only/smoke.yaml
harnessadapter train --config examples/officeqa/prompt_only/smoke.yaml
harnessadapter eval --config examples/officeqa/prompt_only/smoke.yaml --adapter final
```

Training replaces manual edits with learner edits to `ADAPTER.md`. The final adapter is under
`examples/officeqa/prompt_only/runs/smoke/checkout/`; it can be deployed with the same command used above.

To understand what happens between those commands, read [Using and training adapters](../framework/adapter-lifecycle.md).
To supply your own problems and agent, read [Task integration](../framework/task.md).
