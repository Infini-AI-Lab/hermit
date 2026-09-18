# Minimal Shop Adapter

A minimal Harness Adapter using fictional shop data to verify three kinds of interface.

## Context

For return questions, read the current memory first, then use the Python tool for monetary calculations. These examples are not real shop policies. Do not execute actual refunds.

Policies live in memory and are not duplicated here, avoiding two sources of truth across the prompt and memory. Each memory response includes a source path; cite the records actually read in the final answer.

## MCP tools

- `list_memories()`: list available memory IDs and titles.
- `read_memory(memory_id)`: read a complete entry and its source.
- `calculate_refund(item_subtotal, fee_rate)`: calculate the fee and refundable item amount using Decimal. Pass decimal strings such as `"200.00"` and `"0.05"`. This tool performs arithmetic; it does not determine policy eligibility.

All business tools only read files or calculate results. They do not change memory or access external services. Optional call logs provide evidence that the interface was actually used.

## Optional native subagent

`ha_memory_scout` is a small native subagent role. It investigates relevant memory and returns a short, sourced summary. Its definition contains only a name, description, and instructions. The Acting Harness creates and manages the actual subagent. The MCP server does not launch subagents or call models.

When the user explicitly requests a subagent test, delegate memory investigation to this role. The parent agent handles calculation and the final answer. If the host has not loaded the role, report that fact instead of claiming a successful native role invocation.
