# Survey of common subagent settings

Checked: 2026-09-18. Scope: local Codex custom agents; native, plugin, and SDK agents in Claude Code; Pi's official subagent example extension; and native Hermes delegation.

Purpose: inform a possible union of optional Harness Adapter fields. This is not an implemented conversion specification. The survey did not run model tasks or change host settings.

Subsequent decision: v0 uses only name, description, and instructions; other runtime settings use effective host defaults. The 20-field inventory remains research and extension material. See the [v0 decisions](adapter-contract-v0.md).

## Conclusions

Roughly 20 candidate fields cover common needs without standardizing every product setting. Separate role definitions, individual invocations, and host orchestration. Omission, explicit inheritance, empty collections, zero, and false are not interchangeable.

Expressing a requirement in a common format does not mean every host can implement it unchanged. Conversion should report mapped fields, effective host defaults, required adaptations, and unsupported requirements.

## Sources and versions

- [Codex custom agents](https://learn.chatgpt.com/docs/agent-configuration/subagents#custom-agents)
- [Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)
- [Claude Code subagent fields](https://code.claude.com/docs/en/sub-agents#supported-frontmatter-fields)
- [Claude plugin agent restrictions](https://code.claude.com/docs/en/plugins-reference#agents)
- [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/subagents#agentdefinition-configuration)
- [Pi example field parser](https://github.com/earendil-works/pi/blob/46c9de402bddf46b03c3b9f46487b777aaa41861/packages/coding-agent/examples/extensions/subagent/agents.ts)
- [Pi example execution and invocation schema](https://github.com/earendil-works/pi/blob/46c9de402bddf46b03c3b9f46487b777aaa41861/packages/coding-agent/examples/extensions/subagent/index.ts)
- [Hermes delegation documentation](https://hermes-agent.nousresearch.com/docs/user-guide/features/delegation)
- [Hermes native tool schema](https://github.com/NousResearch/hermes-agent/blob/d177b119e9c56c9ddc0b7379ffce52341ec06584/tools/delegate_tool.py)
- [Hermes delegation configuration parser](https://github.com/NousResearch/hermes-agent/blob/d177b119e9c56c9ddc0b7379ffce52341ec06584/tools/delegate_tool_config.py)

Documentation reflects the access date. Pi is pinned to `46c9de402bddf46b03c3b9f46487b777aaa41861`; Hermes to `d177b119e9c56c9ddc0b7379ffce52341ec06584`. Pi findings apply only to the official example extension, not every Pi extension.

## Common fields

No dedicated field means the inspected interface lacks that setting, not that every possible extension is incapable of implementing it.

| Information | Codex | Claude Code | Pi official example | Hermes native delegation |
|---|---|---|---|---|
| Role identity | Required `name` | Required in files; SDK uses the mapping key | Required `name` | Task-based creation; no equivalent named-role catalog |
| Role purpose | Required `description` | Required `description` | Required `description` | Retain purpose in the integration layer |
| Role instructions | Required `developer_instructions` | Markdown body / SDK `prompt` | Markdown becomes `systemPrompt`, appended to Pi's system prompt | Builds child context from `goal/context`, not equivalent standalone prompt registration |
| Model | `model`; inheritance available | `model`; explicit `inherit` available | `model`; omission inherits dispatch model | Global `delegation.model` |
| Reasoning effort | `model_reasoning_effort` | `effort` | No definition field; inheriting the model also passes parent thinking level | Configuration-level `delegation.reasoning_effort`, supported in source |
| Allowed tools | MCP tool selection, skills, and session capabilities; not an equivalent generic `tools` array | `tools` | `tools` becomes subprocess `--tools` | Inherits parent tools with exclusions |
| Denied tools | MCP `disabled_tools` and permission settings | `disallowedTools` | No separate deny field | Fixed filtering; no per-task deny field in native schema |
| MCP resources | `mcp_servers` blocks | `mcpServers`; ignored in plugin agents | No dedicated AgentConfig field | Parent tool inheritance / deployment configuration, not per-task resource declarations |
| Skills | `skills.config`; not equivalent to automatic full-text preload | `skills` preloads content | No AgentConfig field | Inherited skill tools, not an equivalent preload field |
| File/network isolation | Sandbox / permission profiles, subject to runtime and parent policies | Host permissions, `permissionMode`, hooks; not equivalent to an OS sandbox | No independent sandbox field in the example | Parent environment and delegation rules, not per-role configuration |
| Approvals | `approval_policy` | `permissionMode` | `confirmProjectAgents` confirms project agents, not individual tool calls | Host/child execution policies, not a general per-role field |
| Turn limit | No corresponding `maxTurns` in the custom-agent table; do not invent a mapping | `maxTurns` | No AgentConfig field | Global `delegation.max_iterations` |
| Wall-clock timeout | No verified role-level total timeout; tool timeouts are distinct | No independent frontmatter field; inspect tool/SDK layers separately | Cancellation supported; no definition-level timeout | Global `delegation.child_timeout_seconds` |
| Background execution | Child-thread orchestration / invocation | `background` and invocation options | Single, parallel, and sequential modes with progress streaming | Top-level delegation runs in background; legacy `background` is accepted but ignored |
| Working directory | Session/workspace mechanism; not MCP server `cwd` | Parent session's current working directory by default | Invocation `cwd` | Parent workspace and child terminal mechanism |
| Worktree | Host workspace/creation mechanism; verify integration separately | `isolation: worktree` | No definition-level isolation field | Global `delegation.worktree_isolation` |
| Persistent memory | No verified equivalent role-level `memory` enum | `memory: user/project/local` | Launches with `--no-session`; does not prohibit all shared-file access | New child conversation; not a persistent profile |
| Parent conversation | Determined by child creation API; this App's `fork_turns` is not a general file field | Ordinary subagents and fork mode differ; mainly invocation-controlled | Independent process without direct parent conversation inheritance | New conversation receiving task goal/context |
| Project instructions | AGENTS.md / session mechanism; not equivalent to `omitClaudeMd` | `omitClaudeMd`, with organizational-policy exceptions | Pi project-resource loading rules | Workspace project context auto-loaded, excluding SOUL.md |
| Structured output | No verified dedicated custom-agent output schema field | Not listed in inspected AgentDefinition/frontmatter; host SDK output support is not automatically per-child support | No example field | Per-task `output_schema`, validated with one correction opportunity |

## Claude Code: native field inventory

The official frontmatter table lists 18 fields, plus the Markdown body for role instructions.

| Field | Purpose | Omission / scope |
|---|---|---|
| `name` | Identity | Required in ordinary files |
| `description` | Role selection | Required in ordinary files |
| `tools` | Allowed tools | Inherits tools available to subagents |
| `disallowedTools` | Excluded tools | No additional exclusions; not command-level rules |
| `model` | Model selection | Host precedence applies; distinguish omission from explicit inherit |
| `permissionMode` | Approval mode | Ignored in plugin definitions |
| `maxTurns` | Turn limit | Documentation gives no single fixed default |
| `skills` | Preloaded skills | Omission does not prohibit all skill use |
| `mcpServers` | MCP resources | Ignored in plugin definitions |
| `hooks` | Lifecycle handlers | Ignored in plugins; candidate for an extension block |
| `memory` | Persistent memory scope | Optional |
| `background` | Background preference/requirement | Actual behavior depends on fork mode and invocation |
| `omitClaudeMd` | Exclude ordinary project/user CLAUDE.md | Subject to organizational policy and launch mode |
| `effort` | Reasoning effort | Usually inherited; values depend on model support |
| `isolation` | Workspace isolation | Declared value is worktree |
| `color` | UI color | Can be excluded from the common core |
| `initialPrompt` | Initial message when launched as the main agent | Not primarily for ordinary child invocation |
| `experimental` | Experimental options | Currently includes cacheTtl; keep in extensions |

SDK `AgentDefinition` is not a direct copy: `name` comes from the dictionary key and the body becomes `prompt`. Python and TypeScript support can differ; the documentation notes that Python AgentDefinition lacks `omitClaudeMd`. Pin the integration surface and version.

## Codex: role and session layers

Required role fields are `name`, `description`, and `developer_instructions`. Settings allowed by the normal session configuration layer may be added. Relevant examples include:

- `model` and `model_reasoning_effort`.
- `sandbox_mode`, `approval_policy`, and permission settings.
- `mcp_servers` and tool enable/disable options.
- `skills.config`.
- Deeper session settings such as context compression and environment variables; keep these in extensions and verify that the role layer accepts them.

Global subagent settings include `agents.enabled` (default true), `agents.max_concurrent_threads_per_session` (chosen by Codex when omitted), `agents.default_subagent_model`, `agents.default_subagent_reasoning_effort`, and `agents.interrupt_message` (default true).

A role file is not a closed table of small hyperparameters. Do not treat every session configuration key as an independent subagent setting. Parent live runtime overrides can supersede role sandbox and approval defaults.

## Pi: the official example extension only

Four frontmatter fields: required `name` and `description`, optional `tools` and `model`; the body supplies `systemPrompt`.

Invocation schema: `agent`, `task`, `tasks[]`, `chain[]`, `agentScope`, `confirmProjectAgents`, and `cwd`. Each tasks/chain item accepts agent, task, and optional cwd.

Defaults are agentScope=user and confirmProjectAgents=true. Constants limit parallel batches to eight tasks and concurrency to four; these are not per-role fields. Omitted tools means no `--tools` argument, leaving the child Pi environment to decide. Do not assume exact inheritance of the parent's allowlist.

Source details: the body is added through `--append-system-prompt`, a different instruction layer from Claude's custom system prompt or Codex developer_instructions. Empty tool lists normalize to undefined, so they cannot express a strict prohibition on all tools.

## Hermes: native invocation versus global configuration

In the pinned source, the model-visible `delegate_task` schema exposes:

- `tasks[]`: each entry requires `goal`; `context`, `output_schema`, and `images` are optional.
- `group`: exposed only with `independent_completions`; groups results rather than expressing task dependencies.
- `action`: spawn/list/steer/stop, with `subagent_id` and `message` for controlling instances.

Common global delegation settings:

| Setting | Default / scope |
|---|---|
| `model`, `provider` / `base_url` | Inherit parent bindings when not overridden; no per-task model argument |
| `reasoning_effort` | Inherits parent effort; source also retains explicit disabling via false |
| `max_iterations` | 250 child iterations |
| `child_timeout_seconds` | No wall-clock timeout by default; positive values have an implementation minimum of 30 seconds |
| `max_concurrent_children` | 10 |
| `max_spawn_depth` | 1, flat delegation |
| `orchestrator_enabled` | true; delegation capability also depends on depth |
| `worktree_isolation` | false, subject to backend requirements |
| `independent_completions` | false |

API protocol, provider routing, request_overrides, and compression thresholds belong in host extensions. API keys are deployment bindings, not secrets to store in shareable role definitions.

**Documentation/source discrepancy:** the website still shows top-level goal/context and per-task role examples. The pinned public schema primarily uses tasks[], retaining compatibility with the old shape. The model-facing role parameter is hidden and, per source comments, no longer controls capability. Background is also deprecated and ignored. Do not merely copy website examples or treat every internal Python parameter as model-visible.

## Proposed common field scope (not implemented)

Role core: name, description, instructions.

Optional common fields: model, reasoning_effort, tools.allow, tools.deny, mcp_servers, skills, permissions, approval, context.mode, context.project_instructions, working_directory, isolation, memory, max_turns, timeout_seconds, background, output_schema.

These are 20 candidate fields or groups: a union, not a claim of native support by all four hosts.

Invocation-specific inputs: task, concrete context, files/images, and parent task identity. Total concurrency and nesting depth belong to orchestration, rather than being repeated as apparently independent role limits.

Exclude from the initial core: color, cache TTL, fine-grained provider requests, special hooks, main-agent initialPrompt, and low-level retry/transport parameters. Preserve them in namespaced host extensions when needed.

For each converted field, record its source, target location, scope, effective value, reliance on host defaults, and whether the requirement is satisfied. Specify units for turns, tool calls, and model calls. Report unavailable hard guarantees explicitly; prompt advice is not a runtime constraint.
