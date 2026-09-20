# Harness Adapter — design snapshot

Date: 2026-09-18. This describes the proposed architecture, not a released SDK, CLI, or Reef integration.

The [design discussion](design-discussion-2026-09-18.md) records decisions, rationale, and open questions.

## Current core / framework boundary

An adapter contains only evolvable core: knowledge, prompts, business code, roles, and callable declarations. Every core file can change. The framework owns the MCP wrapper, dependency environment, host integration, evidence snapshots, and candidate lifecycle outside the adapter.

The Python prototype now loads declared callables automatically. MWM is a runnable reference core and Evolver, with candidate-only updates through bounded file tools. See [the implemented boundary and limitations](core-and-evolution.md). The broader SDK and Reef integration remain proposed.

## Goal

Provide independently runnable, versioned, continually maintained auxiliary capabilities for existing agents. Preserve the host model and core execution flow while adding knowledge, skills, tools, or native subagent definitions through explicit interfaces.

## Agreed v0 consumption interfaces

An adapter is a repo exporting three kinds of content:

1. **Context:** a Markdown introduction, instructions, and optional briefing, injected through a host-supported entry point.
2. **Tools:** MCP integration. One server can provide multiple tools; MWM's single wm dispatch tool is optional.
3. **Agent Definitions:** optional roles requiring only name, description, and instructions. Other settings use effective host defaults.

The Acting Harness launches and manages subagents. An adapter-run 4B search helper is a tool from the host's perspective.

Integration must be ready before formal tasks. Definitions stay fixed during a run; instances launch on demand. Bootstrap / Connector is optional: native configuration, manual setup, or external frameworks can integrate directly. Helpers assist with conversion, preparation, and verification when needed. New definitions take effect in the next run. Host defaults and capabilities are not guaranteed to match.

See [adapter-contract-v0.md](adapter-contract-v0.md). Filenames and serialization remain open.

## Core boundaries

- **Harness Adapter:** an independent capability workspace containing code, prompts, knowledge state, interfaces, and resource declarations. Methods retain their own internal structures.
- **Adapter Runtime:** runs adapter tools and binds models, tools, and state. Native subagents run in the Acting Harness.
- **Evolver:** reads LearningMaterial and produces candidates, potentially through multi-round search, testing, and selection. Its search history is separate from adapter knowledge.
- **Host Connector (optional):** assists with Context/MCP integration, role conversion, and readiness checks. External integrations can handle these themselves. LearningMaterial collection requires a separate integration based on available host capabilities.
- **Evolution Manager:** default orchestration for evidence selection, triggers, checks, evaluation, and activation. External frameworks may replace it.

## Two usage modes

1. Standalone CLI: create from a template, import history, generate and inspect updates, and activate revisions.
2. Embedded SDK: external frameworks use adapter, runtime, and evolver capabilities separately while retaining orchestration and final release authority.

Reef is a target integration. Its tree mutations require explicit conversion and delivery to work with adapter codebases; compatibility is not automatic. Framework integration and the Host Connector have different responsibilities.

## Templates, instances, and revisions

- A template is an initialization recipe; updates do not automatically overwrite learned knowledge.
- A package contains shareable code, prompts, declarations, and optional initial knowledge.
- An instance holds project settings, private LearningMaterial, persistent state, and resource bindings.
- A revision identifies compatible code and knowledge state. External state must declare snapshot, migration, or reconstruction behavior.

## Update conventions

- Evolvers declare required input, editable scope, resources, and feedback conditions.
- Candidates start from a specific revision in an isolated workspace, without directly editing the running instance.
- Passing checks and improving performance are different claims. Without task evaluation, do not claim improvement.
- Each update chain has one final release authority.
- v0 role definitions remain fixed during a formal run; new ones load at the next integration boundary. Helpers are optional. Other state-update boundaries remain open.
- Restoring code does not restore all state. Git rollback cannot undo irreversible external actions.

## Initial goals

Validate a simple skill and a stateful executable adapter, one default evolver, and an external method with different internals. Then test reuse across hosts and Reef-driven evolution. A 4B search helper exercises independent model-resource bindings.

MWM can supply a default adapter and evolver. Its memory directories, claims, briefing, journal, and learning agenda are not universal requirements. Build a marketplace after real third-party use and contributions emerge.

## Acceptance questions

Can another author run, collect LearningMaterial, update, and deliver their method without understanding or adopting MWM internals? When changing hosts or orchestration, are required code changes confined to connection and integration boundaries?

## References

- [Reef harness method](https://www.reefinfra.ai/docs/developer-guide/write-a-harness-method/)
- [Reef adapters](https://www.reefinfra.ai/docs/developer-guide/harness-adapters/)

## MWM fidelity correction

The earlier simplified MWM example is superseded. The current core preserves tools/<name>/{TOOL.md,run.py,test.py}, agents/<name>/SPEC.md, the docket, journal rolling, and the original five-section package plus single wm interface. The Evolver uses the original pinned method materials. See [the correction and verified scope](mwm-fidelity-correction.md).

## Evolver policy: three areas and independent networking

The candidate core is always readable/writable. The evidence store is always readable and learner-read-only, preserving its record layout. Additional environment directories are user-defined read grants or all reads, never extra writable roots. Network is enabled by default and can be disabled for tools and commands while retaining model inference transport.

The framework freezes policy outside the core and probes actual OS enforcement before starting Codex or Claude Code. Both use the same constrained tool broker. Scratch and required runtime files are explicit infrastructure exceptions. The current sandbox executor uses Codex CLI for both learner clients. See [the execution policy](execution-policy.md).
