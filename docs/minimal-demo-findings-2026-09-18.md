# Minimal Adapter Demo: integration verification and design findings

Date: 2026-09-18. Tested with local Codex CLI 0.154.0, Python 3.12, and MCP Python SDK 2.2.0.

## What was built

`examples/minimal_adapter/` is an ordinary directory in the main repository, with no submodule or nested `.git`. Users can copy it and initialize an independent repository or publish it to GitHub themselves.

The example contains a Context document, three memory records, three Python MCP tools, and one native role definition. The MCP server makes no model calls. Codex's native subagent mechanism launches `ha_memory_scout`.

`tools/prepare_codex_demo.py` is an optional deterministic integration helper for this demo, writing to the ignored `.demo/codex/` directory. It is not a completed general Connector.

## Observed results

- Five tests without model calls passed, covering sources, unknown IDs, amount boundaries, and Decimal rounding.
- A standalone stdio MCP client verified tool discovery, memory reads, calculation, and error responses.
- The parent Codex session used native `spawn_agent` with `agent_type: ha_memory_scout`.
- Child metadata recorded `agent_role: ha_memory_scout` and the corresponding parent thread.
- The child trace contained ha_demo read_memory calls; MCP audit records confirmed reads of returns and shipping.
- The parent then called calculate_refund with item subtotal 200.00 and fee rate 0.05. The tool returned 190.00.
- The child returned the required `HA_MEMORY_SCOUT_V1` marker. The parent's final answer retained it and the policy sources.

These results verify this CLI integration path only. They do not establish App hot loading, compatibility with other hosts, or improved task performance. The recorded model run preceded the English translation; its original logs remain unchanged. Subsequent translation checks do not constitute another model integration run.

## Design issues exposed by the test

### 1. A repo needs discoverable exports

A repo alone does not tell a loader which Markdown to read, how to start MCP, or where roles live. This demo uses a small `adapter.json` to locate the three exports. Its filename and format remain experimental discovery metadata.

### 2. Markdown does not automatically enter model context

This helper writes ADAPTER.md into the generated workspace's AGENTS.md, using a native Codex entry point. Other hosts may use different routes. Record the actual injection path rather than assuming a model saw a file merely because it exists.

### 3. MCP needs deployment information

Tool implementations also need transport, launch command, arguments, working directory or paths, and dependencies. This demo uses stdio and an explicit Python interpreter path. Generated configuration holds machine-specific absolute paths; the original adapter declares relative entry points.

### 4. Registration and executability need separate verification

The three role fields can be converted into Codex TOML. Actual success also depends on the parent thread, role loading, and native child execution.

The first test used `codex exec --ephemeral`. Native spawning failed with `collab spawn failed: no thread with id`; no memory or calculation tools ran. Removing that option succeeded. For the tested version, syntax checks alone were insufficient: actual parent/child creation needed verification. This does not establish behavior across all Codex versions.

### 5. Exit codes and final answers are insufficient proof

The failed first test still exited with code 0 because the model reported the failure normally. Successful verification combined the parent's role request, child role metadata, child MCP calls, parent calculator event, and server audit records.

`tools/verify_codex_demo.py` extracts these facts without copying complete system prompts, credentials, or opaque payloads. It depends on the observed Codex rollout format, not a general tracing protocol.

### 6. Evidence needs provenance links

An MCP server can log its own calls, but that log cannot reconstruct all host subagent behavior. This test also read Codex parent/child thread records. A future common protocol needs links among runs, agent instances, tool calls, and adapter revisions. Markdown, MCP, and role exports do not solve all observability requirements.

### 7. Defaults are deployment choices

The source role contains only its name, purpose, and instructions. It does not override models, tools, or permissions. The launcher uses one child slot, retains the user's model and reasoning effort, and isolates unrelated global MCP servers. These are test-run settings, not additional required role fields.

### 8. Tool results and errors need a return contract

Tools explicitly return structured results and use MCP tool errors for validation failures. An initial protocol check found that a plain dict annotation did not produce the expected structured_content. An explicit structured-output declaration resolved it. The Tools interface must define results, not just input names.

## Remaining boundaries

This is a manually prepared static demo. It does not implement an Evolver, automatic updates, or candidate release. Bootstrap / Connector remains optional: users can manually configure the same three exports described in the README.

Trial settings, virtual environments, and raw logs live in ignored directories. No global Codex configuration was changed, no GitHub push was made, and no benchmark data was touched.
