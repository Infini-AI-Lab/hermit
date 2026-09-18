# Claude Code demo: verified native integration

Date: 2026-09-18. Host: Claude Code 2.1.274. Observed model: `claude-fable-5-1`, selected by the existing host configuration, not overridden by the runner.

## What was reused

The original minimal-shop adapter, Python MCP server, policy records, and role instructions were reused unchanged. The Claude-specific helper translates only the host-facing exports:

- ADAPTER.md becomes a marked block in the generated CLAUDE.md. Repeated preparation updates that block while preserving surrounding project text.
- The same stdio server becomes an entry in `.mcp.json`.
- The source role becomes `.claude/agents/ha-memory-scout.md` with name and description in YAML frontmatter and instructions in the body.

The helper maps `ha_memory_scout` to `ha-memory-scout`, following the native naming convention, and records the mapping. Model, tools, and permissions remain host defaults in the role definition. See the [official subagent format](https://code.claude.com/docs/en/sub-agents#supported-frontmatter-fields).

## Actual test evidence

The recorded session was `fd7f92e5-6075-4066-bc46-509b6ff03d2c`. Its local run directory is `.demo/claude/runs/claude-hzs5bz8v/` (ignored by Git).

1. The initialization event listed `ha-memory-scout` as an available native agent and `ha_demo` as a connected MCP server.
2. The parent called Agent with `subagent_type: ha-memory-scout`.
3. Three successful child MCP calls read returns, shipping, and rounding. Their `parent_tool_use_id` matched the parent's Agent call ID.
4. The successful Agent result contained the role instruction marker `HA_MEMORY_SCOUT_V1`.
5. The parent called calculate_refund with subtotal 200.00 and fee rate 0.05. The server returned a fee of 10.00 and refund of 190.00.
6. The final answer contained `HA_ADAPTER_CONTEXT_V1`, supplied only through generated CLAUDE.md, not through the test task or MCP data.
7. The process and session succeeded, with no permission denials. The CLI reported a cost of USD 0.370647; this is its usage estimate, not a claim about incremental subscription billing.

All 11 verifier checks passed. Raw execution evidence remains in the original run files. The proof records selected facts rather than full prompts or thinking.

## Scope of the test

This was a real, bounded, non-interactive Claude Code trial. Context and native roles were discovered from project files; neither `--agents` nor `--append-system-prompt` replaced those loading paths. Claude's [streaming events](https://code.claude.com/docs/en/headless#follow-subagent-messages) provided parent/child tool-call links.

The runner passed a run-specific MCP configuration explicitly to isolate the demo and route audit logs to the run directory. It allowed only the demo role and tools, disabled unrelated hooks and auto-memory, and limited built-in tools to Agent. It retained the existing authentication and model. No blanket permission bypass was used.

The interactive shortcut starts ordinary Claude Code with normal settings and project `.mcp.json`; its first-use trust and MCP approval dialogs were not exercised by this headless trial. See [project server approvals](https://code.claude.com/docs/en/mcp#project-server-approvals-and-workspace-trust). No claim is made about hot loading an existing session, arbitrary host compatibility, or performance improvements.

## User entry point

From the framework repository:

```sh
./harnessadapter minimal-shop --claude
```

Then send: `Run the adapter integration task in PROMPT.md.`

This is still a dedicated demo workspace. General installation into an existing project, merging arbitrary MCP configurations, and detach are separate work. No global host settings, adapter source data, or benchmark data were changed by the trial.
