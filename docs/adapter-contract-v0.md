# Harness Adapter public contract: v0 decisions

Date: 2026-09-18. Status: initial contract with an executable Python prototype. The later [core and evolution contract](core-and-evolution.md) supersedes adapter-owned MCP packaging: the repo contains core only; the framework owns the wrapper. A general SDK and distribution have not been released.

For context, rationale, and open questions, see the [design discussion](design-discussion-2026-09-18.md).

## 1. The basic unit

A Harness Adapter is an independent, versioned core repository. Every core file may evolve, including code, knowledge, context rendering, roles, and declarations. Each method chooses its organization; the framework owns transport and deployment outside this repo.

MWM is a reference implementation. It may include evidence, knowledge, briefing, and executable structures. The common framework does not require other methods to adopt these directories, representations, or update algorithms.

The framework delivers three kinds of content from the core to an Acting Harness: **Context documents, MCP tools, and optional Agent Definitions**.

## 2. Three public exports

| Export | Contents | Integration responsibility |
|---|---|---|
| Context | A Markdown introduction, usage instructions, and an optional current briefing or knowledge summary | The host, user, or external framework uses a native context entry point; an optional Connector can help |
| Tools | Ordinary callable implementations and capability declarations; the framework exposes them through MCP and binds resources | The host's MCP mechanism connects to tools implemented by the Adapter Runtime; integration helpers are optional |
| Agent Definitions | An optional list of subagent roles and instructions; may be empty | Convert to native definitions before startup; the host loads them and manages actual subagent instances |

One MCP entry point can expose multiple tools. MWM's `wm(name, args)` is one implementation choice; v0 does not require every adapter to use a single dispatch tool.

On the adapter side, a registry means an exportable catalog of role definitions. The Acting Harness manages the actual native registry. Providing definitions does not prove that the host has loaded them or that the model will invoke them.

These are consumption boundaries, not three mandatory processes or fixed directories. A text-only adapter can omit tools and subagents.

## 3. Subagents and the minimal role format

In this project, a **subagent is an agent instance created, run, and managed by the Acting Harness**. A 4B search helper that calls a model inside the adapter and returns a result is a tool. It may contain multiple model and tool calls without becoming a native host subagent.

A v0 role definition requires only:

1. `name`: a stable role name that can be mapped to the host.
2. `description`: when to use this role.
3. `instructions`: the role's work instructions, as inline text or a file reference.

Model, tools, permissions, and budgets use the host's effective subagent defaults, managed by the deployer and integration layer. The meta-agent need not specify every native setting. v0 does not promise identical defaults across hosts.

The current task and materials are supplied when invoking an instance, rather than embedded in the role definition. An adapter without subagents is valid; the meta-agent need not create roles on every evolution round.

Illustrative structure; filenames and serialization are not finalized:

```text
adapter-repo/
  ADAPTER.md                 Introduction, usage, optional briefing
  adapter.json               Editable callable declarations; no MCP server configuration
  agents/index               Names, descriptions, instruction locations
  agents/researcher.md       Researcher instructions
  ...                        Method-specific knowledge, tools, code, state definitions
```

The [subagent field survey](subagent-fields-2026-09-18.md) documents a broader union of possible fields. It is an extension inventory, not the required v0 schema.

## 4. Integration timing and optional Bootstrap / Connector

The initial design requires roles to be ready before formal tasks and does not depend on online native role registration. **Bootstrap / Connector is an optional integration helper, not a mandatory platform layer.**

Hosts can consume standard entry points directly, users can configure them manually, and external frameworks can handle conversion, preparation, and readiness checks. Existing integrations need not invoke our helpers.

When using the provided helpers, the reference flow is:

1. **Select a revision:** choose the adapter repo revision and role definitions for this run.
2. **Transform:** use the target host's format, version, and capabilities to generate native role files or launch settings. A deterministic program or a coding agent can assist.
3. **Prepare integration:** arrange Context injection, MCP connections, resource bindings, and role-name mappings. Keep host-specific outputs separate from source definitions.
4. **Start and load:** start the Acting Harness with the prepared configuration; its native loader loads the roles.
5. **Verify readiness:** confirm that declared tools and roles actually work before formal tasks. Generated files do not prove successful loading. Report unsupported capabilities. An external integration can own this verification.

Static means that role definitions stay fixed during a formal run. The host still creates instances dynamically as tasks require. Evolved definitions belong to a new revision and are adopted at the next integration boundary, using native configuration or optional helpers.

This decision does not make all knowledge and tool state immutable. Other state-update boundaries belong to the future runtime and maintenance protocol. Hot loading may later become an optional Connector capability; v0 does not depend on it.

## 5. Responsibilities

- **Acting Harness:** runs user tasks and loads native role definitions. The parent agent chooses when to delegate; the host creates and manages instances.
- **Harness Adapter:** exports Context, Tools, and optional Agent Definitions while retaining method-specific internals.
- **Meta Harness:** supplies the learner's execution environment for reading LearningMaterial, editing the adapter repo, and checking changes.
- **Evolver:** the update algorithm, distinct from the Meta Harness execution environment.
- **Connector / bootstrap helper (optional):** assists with conversion, configuration, and readiness checks. Native, manual, or external integrations can bypass it.
- **Adapter Runtime:** runs adapter tools and their model calls, resources, and state. Its own model services are not presented as native host subagents.

## 6. Implementation details still open

- Discovery conventions and filenames for Markdown, MCP configuration, and role catalogs.
- Namespaces and mappings for native role-name conflicts.
- Initial supported host versions and readiness checks.
- How host trajectories and feedback return as evidence. The three consumption interfaces do not guarantee access to complete host history.
- Candidate checks, revision activation, and state restoration protocols.

These remain future design questions and do not expand the v0 subagent configuration scope.
