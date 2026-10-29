# Codex and Claude agent learners

Use `evolver.name: codex` or `claude` when a learner should inspect trajectories, use tools, and edit an adapter
directory over several actions. This is useful for skills, multiple files, and tool code that a single text rewrite
does not capture well. On the single-file prompt-only template, the Codex learner is the [prompt-only](prompt-only.md)
method.

## Requirements and setup

Install and authenticate the selected learner CLI in the environment where training runs. Sandboxed program
execution uses Linux and bubblewrap. The acting pipeline can use a different model or host.

```yaml
evolver:
  name: codex
  resume: false
sandbox:
  enabled: true
  network: restricted
```

Complete the endpoint grants, environment, and task resources for your learner; the fragment alone is not a
working model configuration. Use [Installation](../get-started/installation.md) and
[configuration boundaries](../framework/configuration.md#sandbox-network-and-warnings).

## Update contract

The learner edits the working adapter using the step's learning material and `.hermit/EVOLVER.md`. The Trainer owns
acceptance and Git history. Files outside the learnable rules are rejected or repaired. For a performance gate,
the method must explicitly evaluate and select candidates; file checks alone do not provide one.

Persistent learner state, host conversation resume, and Trainer crash recovery are separate mechanisms.
Only enable `evolver.resume` for a backend that declares support; `check` reports unsupported requests.

## Defaults

Codex:

```{literalinclude} ../../hermit/algorithms/evolvers/codex/evolver.yaml
:language: yaml
```

Claude:

```{literalinclude} ../../hermit/algorithms/evolvers/claude/evolver.yaml
:language: yaml
```

`prompt` and `resume_prompt` name learner instructions; `args` passes CLI arguments. Inspect the built-in
definition before replacing prompts. For a custom learner harness, use
[AgentEvolver or CommandEvolver](../framework/custom-evolver.md#agent-and-command-implementations).

Report usage outside the framework's metered tools so budget records remain meaningful. A learner's direct
external calls are not automatically accounted for merely because the Trainer launched it.

## Learner tool access

Agent-backed learners can set `evolver.params.tools` to an allowlist such as `[check_spec]`; null exposes all
Trainer endpoint tools. The gate refuses calls outside that list. Inspect the selected method's defaults and
`hermit/evolver/gate.py`. This controls the learner's Trainer-tool endpoint, separately from the acting
agent's task/adapter tools. It does not make arbitrary shell commands safe or impose a mandatory check order.

For a complete task-specific Codex configuration, see `examples/clbench_poker/prompt_only/run.yaml`. Acting agents are configured under `agent`; `evolver.name: codex` selects the
learner, not the acting host.
