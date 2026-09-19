# Minimal Shop Adapter

A minimal adapter example that can be copied into an independent repository. This directory contains ordinary files, with no nested `.git` or submodule.

## Three exports

| Export | File | Contents |
|---|---|---|
| Context | `ADAPTER.md` | Adapter introduction and usage instructions |
| Tools | `adapter.json` + `adapter_tools.py` | Plain Python callables and schemas; the framework supplies MCP |
| Agent Definitions | `agents/index.json` | One minimal role, with instructions in `agents/memory_scout.md` |

`adapter.json` is demo-specific discovery metadata pointing to these three entry points, not a finalized universal protocol.

`memory/policies.json` contains three fictional shop records: returns, shipping, and rounding.
`adapter_tools.py` provides reads and Decimal calculations, without model calls or actual refund execution.

## Quick start

From the `harnessadapter` repository root, choose one host:

```sh
# Prepare and launch Codex
harnessadapter minimal-shop --codex

# Prepare and launch Claude Code
harnessadapter minimal-shop --claude
```

In the resulting session, send: `Run the adapter integration task in PROMPT.md.`

Requires Python 3.11+ and an installed, signed-in host CLI. The launcher creates an isolated environment and installs pinned dependencies on first use, then reuses it. It prepares the three exports; the host starts and owns the stdio MCP process. Preparation alone makes no model call and does not run development tests.

Accept the host's normal workspace trust and MCP approval prompts when shown. Global model, permission, approval, and login settings are preserved. Preparation alone does not prove that native roles and tools ran; use the corresponding verifier below.

To prepare without launching, pass `--host codex` or `--host claude-code`. The default host is Codex. The command prints how to start the chosen host.

Generated files are saved in Git-ignored workspaces:

| Export | Codex: `.demo/codex/` | Claude Code: `.demo/claude/` |
|---|---|---|
| Context | A managed block in `AGENTS.md` | A managed block in `CLAUDE.md` |
| MCP | `.codex/config.toml` | `.mcp.json` |
| Native role | `.codex/agents/ha_memory_scout.toml` | `.claude/agents/ha-memory-scout.md` |
| Test task | `PROMPT.md` | `PROMPT.md` |

Both instruction files visibly distinguish the adapter from project instructions:

```md
# Project instructions

Existing project guidance stays outside the adapter block.

<!-- harnessadapter:minimal-shop:start -->

---

## Adapter: `minimal-shop`

> Managed by Harness Adapter. Source: `ADAPTER.md` · Version: `demo-v1`.

### Minimal Shop Adapter

...adapter context...

**End of adapter: `minimal-shop`**

---

<!-- harnessadapter:minimal-shop:end -->
```

The comments identify the update boundary in source; the heading, metadata, and end label remain visible in Markdown previews. Original adapter headings are nested under the wrapper. Ambiguous or broken markers cause an error rather than a whole-file replacement.

The same adapter source is reused. Claude role conversion maps `ha_memory_scout` to `ha-memory-scout` and records that mapping in generated metadata. Role model, tool, and permission fields remain unspecified so effective host defaults apply.

Use `--workspace /path/to/empty-directory` for another dedicated workspace. Helpers refuse to overwrite unrelated nonempty directories or workspaces generated for the other host. Repeated preparation for either host replaces only its marked context block, preserving surrounding text. Generated MCP and role files remain owned by the demo helper. This is not a general attach/detach implementation for existing projects.

The launcher supports minimal-shop, mwm, and local repos using the current Python-callable contract. It is not a published adapter registry.

## Developer checks

The following checks are for development and verification, not required user setup. Run the quick start first to prepare the environment, or use the explicit setup below:

```sh
python3 -m venv .venv
.venv/bin/pip install -e ".[test]"

# No model calls: unit checks and a real MCP protocol check
.venv/bin/python -m pytest -q tests
.venv/bin/python tools/smoke_adapter_mcp.py

# Low-level alternative to the quick-start preparation command
.venv/bin/python -m harnessadapter.hosts.codex
```

### Automated Codex integration trial


An existing local Codex login is required. The following runs a real model task using the model and reasoning effort in the current global configuration:

```sh
.venv/bin/python tools/run_codex_demo.py
```

The runner does not change global Codex settings. It supplies MCP and role settings as command arguments and omits unrelated global MCP servers. It retains the parent session for native subagent creation and does not use `--ephemeral`.

Logs are saved in `.demo/codex/runs/codex-*/`: `events.jsonl`, `answer.md`, `tool-calls.jsonl`, and `stderr.log`. A zero exit code only means the Codex process completed normally; it does not prove that the role and tool checks passed.

Verify that run with the framework's helper:

```sh
.venv/bin/python tools/verify_codex_demo.py .demo/codex/runs/codex-<run-id>
```

It checks the native role request, parent/child thread linkage, child memory calls, and parent calculation call, then writes `verification.json`. It depends on the local Codex rollout format.

You can also open the generated `.demo/codex` folder in the Codex App and send `PROMPT.md` in a new conversation. The host must trust and load the project settings. This App route requires separate verification; it does not imply that the current session has hot-loaded the role.

### Automated Claude Code integration trial

After preparing the Claude workspace:

```sh
.venv/bin/python tools/run_claude_demo.py
.venv/bin/python tools/verify_claude_demo.py .demo/claude/runs/claude-<run-id>
```

This calls the existing configured Claude model using the existing login. The trial has a default 180-second timeout and a reported API-cost budget of USD 2; the reported cost is not necessarily an incremental subscription charge. Logs stay in the ignored run directory.

The runner loads CLAUDE.md and native role files from the project. It explicitly selects a run-specific copy of the same MCP configuration with `--mcp-config` and `--strict-mcp-config`, preapproves only the demo role and three tools, disables unrelated hooks and auto-memory, and exposes only the Agent built-in tool. It does not bypass all permissions, force another model, or inject the role via `--agents`.

The verifier checks native role discovery, the successful parent Agent call, child memory calls linked by `parent_tool_use_id`, the parent calculation, server audit records, and a context-only marker. See [the findings](../../docs/claude-demo-findings-2026-09-18.md). The headless test does not verify interactive MCP approval dialogs or an already running App session.

## Copy into your own adapter repository

Copy this directory elsewhere and install its dependencies. Users decide whether to run `git init`, commit, or publish to GitHub. The framework repository does not maintain a nested Git repository.

The copied repo contains core only. Run it using the separately installed framework, for example from a framework checkout:

```sh
harnessadapter /absolute/path/to/copied-adapter --codex
```

The framework installs its own dependencies and launches `harnessadapter/runtime/mcp_server.py --adapter <repo>`. No server.py, MCP decorators, host paths, or MCP SDK dependencies belong in the copied adapter. All core files, including adapter.json and adapter_tools.py, may evolve; new revisions must satisfy the public contract.

## Verifiable behavior

Test case: an unopened ordinary item returned for a non-defect reason, delivered seven days ago, with an item subtotal of CNY 200 and original shipping of CNY 10.

Expected: native `ha_memory_scout` (Codex) or `ha-memory-scout` (Claude Code) reads memory through MCP and returns the role marker. The parent agent calls the calculator, obtaining an item refund of CNY 190.00; original shipping is not refundable. Check both host delegation records and MCP audit logs, not just the final answer.

The role's call limit and no-file-edit instruction are work instructions, not new runtime permissions or hard budgets. The MCP tools themselves only provide reads and calculations. The host manages actual subagent execution.
