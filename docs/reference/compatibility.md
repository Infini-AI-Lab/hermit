# Compatibility and current limits

These docs target framework **0.2.0** in this checkout. Preserve the exact Git revision for experiments; version
numbers alone may not distinguish unreleased changes.

## Execution support

| Capability | Current support |
|---|---|
| Core package | Python 3.12+ and Git |
| Local deterministic Quick Start | No model account, GPU, external dataset, or host CLI |
| Context-only workspace generation | macOS and Linux |
| Sandboxed learner programs | Linux with bubblewrap and permitted namespaces |
| Built-in methods | Prompt-only (the Codex learner on the prompt-only template), Codex, Claude, Pi, DeepSeek Harness, GEPA, SkillOpt, MWM, Harness², Continual Harness, ReHarness, Meta-Harness Adapter, ACE, AHE Adapter, RRSI Adapter, EvoSafeHarness, OPRO |
| Workspace deployment | Codex, Claude Code, Pi, mini-swe-agent, and DeepSeek Harness |
| A subscription instead of an API key | Codex (`login: chatgpt`) and Claude Code (`login: claude`, CLI 2.1.248 or later, tested on 2.1.318), acting and learning; see [Claude subscription](../guides/claude-subscription.md) |
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
- The Trainer's own process is not sandboxed; the acting agent, the judge (when sandboxed), the declared tools and the learner run in sandboxes of their own ([Sandbox design](../developer/sandbox-design.md)), except where the pipeline runs on the host by design (`sandbox.enabled: false`, Harbor, `environment.runner: host`).
- An isolated network is a network namespace: the only ways out are the forwarded ports and the allowlist proxy, so a program that ignores proxy settings has no route out.
- Budget observation can overshoot with in-flight work; missing usage remains unknown.
- DeepSeek Harness reports no cost: the cost recorded follows the price (the agent's own `price`, else the backend's). Web searches and the request that summarizes a long session's history are not counted, though the session's cost still reads as known, so `budget.usd` can undercount it.
- Crash recovery restores the last completed framework step, not arbitrary external service state.
- The evaluator's `solved_once` threshold is fixed at score 1.0.
- Per-task adapter context comes only through `exports.yaml`'s `context_tool`, consumed by `Session`, the `react` agent and the `codex`, `claude-code`, `pi`, `mini-swe-agent` and `dsh` agents; its template must declare `plugin-portable: false`, and `deploy` and `convert` refuse such adapters.
- The proposed manifest `serve` section, health checks, standalone startup, and relay lifecycle are not supported.
- A model reached through a CLI (`llm: {cli: ...}`, `agent.model: {cli: ...}`) takes text and gives text: tool calls, sampling and structured-output requests are refused ([the model contract](../guides/claude-subscription.md#the-model-contract)). A subscription has no per-token cost, so `budget.usd` bounds nothing on it.
- Harness²'s editors are told of upstream's Codex surface (the `harness2-codex` template); a Claude Code acting agent runs the same tree in its own places, and its execution's stream reaches them rendered in upstream's grammar by `harness2_upstream/render.py`.

## Earlier command interfaces

Older `evolve` / `run` instructions do not describe this interface. The current experiment commands are
`check`, `train`, and `eval`; consumption uses `deploy` and `convert`. There is no documented automatic
migration of old configurations or learner-state formats. Preserve an old experiment and create a separate
0.2.0 configuration, validating the pipeline, judge, manifest, and splits before spending on a new run.
