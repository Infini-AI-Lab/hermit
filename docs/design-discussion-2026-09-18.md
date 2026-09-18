# Harness Adapter design discussion · 2026-09-18

This archive records decisions, rationale, and open questions. It does not claim that the framework, SDK, CLI, or cross-host integrations have been implemented.

See [adapter-contract-v0.md](adapter-contract-v0.md) for the current contract and [subagent-fields-2026-09-18.md](subagent-fields-2026-09-18.md) for the native field survey.

**Later clarification on the same day: Bootstrap / Connector is optional.** The bootstrap flow below describes a reference path using our helpers. Native host configuration, manual integration, or an external framework can satisfy the consumption interfaces directly. Roles must still be ready before formal tasks, with definitions fixed for the run. The website and v0 contract reflect this clarification.

## 1. Project direction: an open-source platform

The focus is now an open-source project that lets users build, integrate, and continually maintain Harness Adapters, and lets other method authors contribute implementations.

Existing methods should retain their knowledge structures and evolution processes. MWM is a default or reference implementation; its research settings are not universal protocol requirements.

Two usage modes are planned:

- **Public SDK:** callable by external frameworks such as Reef, which can retain control over experience selection, evaluation, and release.
- **Standalone application / CLI:** default orchestration for template initialization, experience import, evolution, checks, and activation.

Both share the underlying facilities. Package layout, function signatures, and command names remain open.

## 2. Three core objects

| Object | Responsibility |
|---|---|
| Acting Harness | Runs the parent agent's tasks; loads, creates, and manages native subagents |
| Harness Adapter | Provides additional context, tools, and optional role definitions; the object being evolved |
| Meta Harness | Gives the meta-agent an execution environment for reading experience, editing the repo, invoking tools, and checking changes |

The Evolver is the algorithm, distinct from the Meta Harness execution environment. The five engineering components—Adapter, Runtime, Evolver, Connector, and Manager—describe responsibilities, not five mandatory services.

The goal is independent implementations and replacement through explicit interfaces, not compatibility between arbitrary combinations without adaptation.

## 3. An adapter is fundamentally a repo

An adapter is an independent, versioned codebase. It can contain text, knowledge, executable code, and method-specific structures.

MWM may use evidence, knowledge, executable, briefing, and journal structures. Other implementations may store only a skill or use different memory and tool architectures.

The repo holds definitions and code. Databases, indexes, complete raw trajectories, model services, and credentials need not all live in Git. A runtime instance binds the required resources and state.

Distinguish templates, packages, instances, and revisions. A template provides a starting point; an instance holds project-specific experience and bindings; a revision identifies compatible code and knowledge state. Detailed migration and restoration semantics remain open.

## 4. Decision: three consumption interfaces

| Export | Meaning |
|---|---|
| **Context** | Markdown describing the adapter and its usage, optionally including a current briefing or knowledge summary |
| **Tools** | Adapter tools exposed through MCP; the integration arranges server launch or connection |
| **Agent Definitions** | An optional catalog converted into native definitions for the Acting Harness to load and run |

MCP provides a common tool entry point without requiring all capabilities to share one tool name. MWM's `wm(name, args)` is one implementation.

The adapter's agent registry is a role catalog. The host's registry and executor make those roles available as native subagent types. Successful registration does not guarantee invocation.

Simple adapters without tools or subagents remain valid. Methods should not create them merely to satisfy a format.

## 5. Decision: distinguish tools and subagents by execution ownership

This project uses these definitions:

- **Tool:** the host invokes an adapter capability and receives a result; execution belongs to the adapter. It may include models and multiple steps.
- **Subagent:** a separate agent execution instance created and managed by the Acting Harness.

A 4B search helper is therefore a tool. Search preprocessing or repeated model calls inside it do not make it a native host subagent.

The host owns native subagent model calls, tool execution, and lifecycle. The Adapter Runtime runs adapter tools rather than replacing the native subagent executor.

## 6. Decision: minimal role definitions in v0

Only three fields are required:

```yaml
name: researcher
description: Investigate project behavior and gather supporting evidence
instructions: agents/researcher.md
```

This illustrates the information, not a finalized YAML, Markdown frontmatter, or index-file format. Instructions may also be inline text.

The invocation supplies the task, input files, and relevant context. The role definition describes ongoing responsibilities and behavior.

Model, tools, permissions, and budgets use effective host defaults, managed by deployment and integration. The meta-agent need not fill in many runtime settings. Defaults are not guaranteed to match across hosts.

Rationale: reduce configuration burden and start with simple, reusable roles. Participants observed infrequent subagent creation in the discussed use cases, but did not conclude that subagents lack value in general.

An earlier survey identified about 20 common fields and additional host-specific settings. Keep this inventory for future extensions, not as a mandatory v0 schema.

## 7. Decision: prepare before startup

The reference deployment flow is:

1. Select the adapter revision and role definitions.
2. Prepare native role files or launch settings for the target host's format, version, and capabilities.
3. Prepare Markdown context, MCP connections, and resource bindings.
4. Start the Acting Harness and let its loader discover the definitions.
5. Confirm readiness before formal tasks.

Role definitions stay fixed during this run; the parent agent creates actual instances on demand. New definitions load at the next integration boundary.

Static here constrains role definitions, not all knowledge, caches, and tool state forever.

Rationale: hosts differ in hot loading, watched directories, and refresh mechanisms. Preparation before startup avoids depending on a uniform online registration mechanism.

A common integration sequence does not erase capability differences. A host without the required native mechanism still needs specific adaptation or an explicit unsupported result.

## 8. Model-assisted conversion still requires loading verification

A coding agent can read integration documentation and convert role descriptions into the target format. A deterministic converter or a combination is also possible. No implementation route is frozen.

Conversion and loading are separate. A model can generate files; the host program loads the native roles. Generated configuration alone does not prove readiness.

Keep source definitions separate from host-specific outputs and preserve role-name and version mappings. Check that the target host discovers each role and, when necessary, verify invocation with a small task.

Translation cannot create unsupported model, tool, or permission capabilities. Do not silently drop requirements; report the source of effective defaults.

## 9. How the discussion converged

| Option discussed | Current decision | Rationale |
|---|---|---|
| Text plus MCP only | Add optional Agent Definitions | Tool calls alone do not register native roles |
| Adapter runs its own subagent | Classify it as a tool here | Reserve subagent for host-owned instances |
| One large schema for all native settings | Keep the survey; start with three fields | Reduce definition and conversion complexity; add special capabilities as needed |
| Require online role registration | No v0 dependency | Hot-loading behavior varies across hosts |
| Static loading means starting instances early | Separate definitions from instances | Definitions are fixed; instances and tasks are runtime decisions |
| Model reads docs and declares integration complete | Verify actual loading | Understanding text and generating files do not prove execution works |

## 10. Next design questions

- Discovery, filenames, and serialization for Context, MCP configuration, and Agent Definitions.
- Helper boundaries and the combination of deterministic and model-assisted conversion.
- Role-name conflicts, generated-file locations, cleanup, and upgrades.
- Supported hosts and versions, loading verification, and incompatibility reports.
- Resource, permission, and budget bindings in the first deployment scenario.
- Returning host experience as evidence: consumption interfaces do not automatically expose all host or child trajectories.
- Evolution contracts: editable scope, candidates, algorithm state, feedback, and final activation authority.
- Joint versioning, migration, and restoration of code and persistent state.

The framework provides shared facilities while method authors retain their internal designs. An embeddable SDK and standalone CLI are target products. Reef integration and a marketplace remain future work.

## Reading order

1. This archive explains the rationale.
2. The [v0 contract](adapter-contract-v0.md) records current interface decisions.
3. The [subagent field survey](subagent-fields-2026-09-18.md) records native capabilities, version differences, and candidate extensions.

At the time of this design discussion, only documentation and the website were updated; no role registration, agent launch, model call, or benchmark run was performed. The later demo is documented separately in the repository's demo findings.
