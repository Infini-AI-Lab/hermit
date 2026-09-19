# Evolver execution policy

The framework enforces permissions; the user chooses the additional read scope and network flag. The supported learner clients are Codex and Claude Code. This policy is outside the evolvable core and is frozen for a session.

## Three data areas

| Area | Read | Write |
|---|---|---|
| Candidate adapter | Always | Always, through framework capabilities |
| Evidence store | Always | Never by the learner |
| Other environment directories | User-defined paths, or all reads | Never |

Evidence is a separate storage area, not adapter content or an optional environment read grant. This version snapshots the supplied evidence directory without changing its layout or record format. Its README defines the interpretation. The learner can follow original records regardless of whether additional environment access is disabled.

Method instructions and selected history are also readable and immutable. Temporary work is allowed in an isolated scratch directory; scratch is not published as part of the adapter. Necessary OS/interpreter/framework runtime files have read-only infrastructure access for command execution. That does not grant writes to those installations.

## Configuration

Use TOML or JSON. Network is **enabled by default**. No additional user directories are readable by default.

```toml
backend = "codex"  # or "claude-code"
network = false

[read_access]
paths = ["/absolute/path/to/project", "/absolute/path/to/environment"]
```

For full reading:

```toml
backend = "claude-code"
network = true
read_access = "all"
```

Full reading is not full access: only the candidate and scratch remain writable. There is no configurable write_all switch. Unknown policy fields and invalid values are rejected.

```sh
harnessadapter evolve mwm \
  --evidence examples/mwm_evidence \
  --config config/evolution.toml

# Equivalent CLI overrides
harnessadapter evolve mwm --backend codex \
  --evidence examples/mwm_evidence \
  --read-dir /path/to/project --no-network

harnessadapter evolve mwm --backend claude-code \
  --evidence examples/mwm_evidence --read-all --network
```

Repeat --read-dir for multiple directories. CLI overrides take precedence over the file. Relative paths inside a policy file resolve against that file's directory. --prepare-only constructs and tests the policy without making a model call.

## Enforcement, not prompt advice

Both clients use the same framework tool broker:

- get_policy describes the effective grants.
- read_file/list_files address intrinsic areas such as evidence and adapter.
- read_path/list_path access authorized environment paths.
- write_file/delete_path/move_path operate on the candidate or scratch only.
- run_command executes arbitrary shell commands inside the OS sandbox with the same read/write/network policy.
- fetch_url accesses HTTP(S) only when network is enabled.
- validate checks syntax and declarations without importing candidate code.
- test_python remains MWM's stricter production-tool admission path, requiring Linux bubblewrap. Passing a general run_command check does not bypass that admission gate.

File operations also run inside the OS sandbox. The framework does not rely solely on a check-then-open pathname guard that could be raced by a symlink change. The broker itself is trusted infrastructure and writes its audit records outside the core.

Before a learner starts, real probes must demonstrate candidate and scratch writes, evidence reads, rejection of evidence/other writes, the requested read boundary, and the requested network connectivity. Missing or ineffective enforcement blocks startup. There is no unsandboxed fallback. The model cannot edit control/policy.json, and the broker stops if that file changes during a session.

The present shared executor is the installed Codex CLI's standalone `codex sandbox` command on macOS/Linux. It makes no model call. It is used even when Claude Code is the learner client, so **both modes currently require this Codex CLI infrastructure dependency**. This integration was tested with Codex CLI 0.154.0 on macOS; other platforms must pass their own preflight. The sandbox executor can later be replaced without changing the policy or Evolver method.

Claude Code's native tools and unrelated MCP/skill routes are disabled for these sessions. Codex's native execution, app/plugin, search, image, and delegation routes are disabled; its remaining native filesystem access uses a read-only permission profile. Writes and exploration go through the constrained broker. Only that framework MCP server is preapproved; no blanket host permission bypass is used.

## Network semantics

network=false blocks agent-initiated tool and command networking, including a direct socket connection attempted inside a program. Model inference/authentication transport remains available to the trusted Codex or Claude Code client. Native web-search routes do not provide an alternate path around the broker.

network=true allows connectivity; it does not enlarge local filesystem permissions. Network permissions do not make remote services read-only: remote write authorization must be controlled by the services and credentials supplied for the task. The framework does not automatically provide production service credentials to sandboxed programs, and it strips unrelated environment variables from command execution.

## Scope and evidence

The policy covers the agent-visible operations and command descendants, not an OS sandbox around the entire authenticated model client. Vendor authentication, model transport, and infrastructure bookkeeping are trusted control-plane behavior. Persistent user-file edits initiated by the learner are confined to the candidate; scratch and framework-generated logs are explicit runtime exceptions.

Sessions record requested policy, effective paths, preflight observations, tool calls, and post-run fingerprints under control/. Those records are not editable through learner capabilities. A process exiting normally without any successful broker call is not counted as a successful evolution session.

The prototype supports a wall-clock limit for either backend. The dollar-budget flag is supported only by Claude Code; explicitly requesting it for Codex is rejected rather than silently ignored. Candidate validity and permission compliance do not establish learning quality or automatic deployment readiness.
