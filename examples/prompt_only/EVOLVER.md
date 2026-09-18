# Prompt-only evolution guide

This architecture starts with one empty learned-context document, ADAPTER.md.
The acting harness receives that document as context. No adapter MCP server or
native subagent is registered while tools and agents are absent.

Use the supplied evidence README to understand the records. Update ADAPTER.md
with instructions useful for future tasks. Do not invent findings or represent
a single observed case as a universal rule. Explain your changes in your report.
Do not copy private raw histories into the acting context unnecessarily.

For a prompt-only experiment, keep tools empty and omit agents. This is the
experiment's chosen search space, not a framework restriction: a different
experiment can evolve code, tools, roles, and declarations too.

All files in the candidate are editable. Framework infrastructure and evidence
remain outside the candidate. This architecture does not implement GEPA or
SkillOpt; choose or supply an Evolver separately. Structural checks do not
establish that the new instructions improve task performance.
