# Deployment

```sh
./harnessadapter deploy prompt-only --host codex --workdir .demo/my-codex
./harnessadapter deploy mwm --host claude-code --workdir .demo/my-claude
```

Deploy consumes the selected adapter. --workdir names a generated acting workspace, not an environment-read grant. This prototype accepts an empty directory or a workspace previously generated for that host. It refuses an unrelated populated project rather than rewriting its configuration.

## What gets delivered

| Export | Codex | Claude Code |
| --- | --- | --- |
| Context | Marked adapter block in AGENTS.md | Marked adapter block in CLAUDE.md |
| Nonempty tool list | MCP entry in .codex/config.toml | MCP entry in .mcp.json |
| Optional native roles | Generated .codex/agents files | Generated .claude/agents files |

Global host configuration is not modified. Project text around the managed context block is preserved. Host trust and approval settings remain in effect.

A context-only adapter generates no adapter MCP registration. When switching a managed workspace from a tool-bearing adapter to Prompt-only, the framework removes the old generated server registration and archives retired role files outside discovery directories.

## Loading and runtime

Preparation runs before host startup. The host reads context, starts registered MCP servers if present, and loads native role definitions. The host controls whether to invoke tools or launch a subagent. The adapter MCP server does not launch native subagents or select their model.

The first role contract contains name, description, and instructions. Other runtime settings use effective host defaults. Hot reloading and arbitrary cross-host role settings are not promised.

## Current delivery boundary

The generated MCP configuration points to the local adapter path. It is not a bundled remote artifact or a pinned release. Deploy a reviewed candidate directory and keep it unchanged during a run. Automatic version activation, process replacement, and external-state rollback remain future work.

Only one adapter is supported per generated workspace today. Multi-adapter deployment and merging into arbitrary existing projects remain planned.
