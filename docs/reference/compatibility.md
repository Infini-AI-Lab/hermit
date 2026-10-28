# Compatibility and current limits

These docs target framework **0.1.0** in this checkout. Preserve the exact Git revision for experiments; version
numbers alone may not distinguish unreleased changes.

## Execution support

| Capability | Current support |
|---|---|
| Core package | Python 3.12+ and Git |
| Local deterministic Quick Start | No model account, GPU, external dataset, or host CLI |
| Context-only workspace generation | macOS and Linux |
| Sandboxed learner programs | Linux with bubblewrap and permitted namespaces |
| Built-in methods | Prompt-only (the Codex learner on the prompt-only template), Codex, Claude, GEPA, SkillOpt, MWM |
| Workspace deployment | Codex, Claude Code, and Pi |
| Native plugin conversion | Claude Code, Codex, Pi, Hermes, with export restrictions |
| Stdio tool server | `hermit serve DIR`; normally started by the host |
| Generic manifest-driven standalone service lifecycle | Proposed; not implemented |

Recipes can require newer Python versions and additional checkouts. The core package's minimum version does not
guarantee compatibility with every external benchmark.

## Export compatibility

Workspace and plugin paths have different capabilities. A target plugin may refuse context or roles that the same
host accepts through project files. Read the [conversion matrix](../framework/commands.md#what-each-hosts-plugin-carries).
Use the output report to inspect omissions; `--allow-lossy` deliberately changes what the consumer receives.

Workspace deployment uses local source paths for tools. Conversion embeds an adapter copy and uses the installed
framework's stdio server. Neither command guarantees that arbitrary tool dependencies exist on another machine.

## Known limitations

- `agent.name` is the current acting-agent selector. Legacy `pipeline.run` settings remain supported when no agent is named; function-based agents receive `params` and `base` when their signatures declare those keywords.
- Trainer acceptance checks structure; performance selection belongs to the Evolver or your experiment protocol.
- The sandbox covers launched learner programs, not the whole Python process or the acting pipeline.
- Restricted network mode uses a proxy that a program can bypass by ignoring proxy settings.
- Budget observation can overshoot with in-flight work; missing usage remains unknown.
- Crash recovery restores the last completed framework step, not arbitrary external service state.
- The evaluator's `solved_once` threshold is fixed at score 1.0.
- Dynamic per-call adapter context requires a custom consuming pipeline; ordinary exported instructions are static.
- The proposed manifest `serve` section, health checks, standalone startup, and relay lifecycle are not supported.

## Earlier command interfaces

Older `evolve` / `run` instructions do not describe this interface. The current experiment commands are
`check`, `train`, and `eval`; consumption uses `deploy` and `convert`. There is no documented automatic
migration of old configurations or learner-state formats. Preserve an old experiment and create a separate
0.1.0 configuration, validating the pipeline, judge, manifest, and splits before spending on a new run.
