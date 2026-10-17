# Proposed standalone service lifecycle

This page is a design record, not supported configuration or a runnable tutorial. The current `harnessadapter serve DIR` command serves tools over stdio. The manifest `serve` section, `--standalone`, `--serve-url`, health rules, relay discovery, and automatic lifecycle below are not implemented.

## Proposed manifest and lifecycle

The consumption-end assumption the framework is moving to: **the manifest states which MCP tools the adapter
provides, and the adapter can start a healthy MCP server that provides them.** A host then needs only two things
from the adapter: how to start that server and how to tell it is up. Today the first is decided by `deploy` /
`convert` when they write the host's configuration, and the second is not checked at all. The proposed section:

```yaml
serve:
  transport: stdio          # stdio: the host starts one server per session; http: a standalone service
  command: harnessadapter serve .
  health: tools/list        # up when it answers initialize and list_tools reports exactly exports.tools
  lifecycle: session        # session: the host starts and stops it; standalone: a shared service outlives sessions
  host_launch: true         # standalone only: may the host-run relay start the service when none is running?
  instructions: SERVICE.md  # standalone only: what a person needs to know to run it by hand
```

| Key | Meaning |
|---|---|
| `transport` | `stdio` for stateless tools (the per-session wrapper, today `harnessadapter serve DIR`); `http` for an adapter that needs state shared across sessions or agents, or reactions (today `harnessadapter.runtime.service`) |
| `command` | the one way to start the server; fixed, so a host configuration only points at the adapter directory |
| `health` | the readiness rule: the MCP handshake succeeds and `list_tools` matches `exports.tools` |
| `lifecycle` | `session`: the host starts and stops the server with its session. `standalone`: the process the host starts is only a stdio relay to a shared service that outlives sessions; see [How the standalone service starts](#how-the-standalone-service-starts) |
| `host_launch` | standalone only. `true`: the relay may start the service itself when none is running (a locked singleton, detached; it stops after an idle period once the last client is gone), so the user runs nothing extra. `false`: the relay only attaches; when nothing is running it exits with a message pointing at `instructions`. An adapter turns this off when the service belongs on another machine, needs heavy prerequisites (a GPU, a Lean toolchain with Mathlib), must be warm before any agent, or holds state that no single session should be the one to create |
| `instructions` | standalone only: a file in the adapter (or a section of `README.md`) for the person who runs the service by hand: prerequisites, the command, where it should live. `deploy` and `convert` print the path in their report; `check` warns when it is missing |

### How the standalone service starts

There are two ways to launch it, and both end on the same line the host runs. **The user can always launch it
by hand; the adapter decides whether the host may launch it too.**

```
host runs, at every session start:  harnessadapter serve DIR
   1. look for a running service: DIR/.serve/endpoint (this machine) or --serve-url (anywhere)
   2. found      → attach; relay this session's calls; register as client N            ← a user-launched service
   3. not found  → read the manifest:
        host_launch: true   → take DIR/.serve.lock, fork a detached daemon, write the endpoint, attach
                               (the service stops itself after an idle period once the last client is gone)
        host_launch: false  → exit with a message that names serve.instructions
```

**Launched by the user:**

```bash
harnessadapter serve DIR --standalone      # foreground or daemon; prints the endpoint and writes DIR/.serve/endpoint
```

From then on every session's relay finds it at step 1, whatever `host_launch` says. On another machine, the
printed URL is handed to `deploy` / `convert` as `--serve-url`, and the host configuration carries the URL
instead of a local command.

**Launched by the host:** only when `host_launch: true`; the host believes it started an ordinary stdio MCP
server, the relay starts the shared service behind it, and the user runs nothing extra.

**When no service is running and the host starts anyway.** The host runs the line as usual and notices
nothing at that point. The relay finds no service. With `host_launch: true` it starts one and the session only
waits a few seconds longer. With `host_launch: false` it exits at once with one line naming the fix and the
instructions file (`no service is running for <adapter>; start it with 'harnessadapter serve DIR --standalone',
see SERVICE.md`). The host then reports a failed MCP server and continues the session: context, skills and roles
are in place, only the tools are missing, and a tool call returns "server unavailable". After the user starts the
service the session has to be restarted, or reconnected where the host allows it (Claude Code: `/mcp`; Codex: a
new session), so the instructions file should say so.

So three promises hold: a stateless adapter costs the user nothing; a standalone adapter that allows host launch
costs the user nothing; any adapter can be launched by hand and is then found automatically. One thing stays
outside: reactions that interrupt or resume an agent can be decided by the service but delivered only by a host
with a control channel; today only the Lean collaboration run script plays that role.

`serve` is never typed by a user: it is the line `deploy` and `convert` write into the host's configuration, and
the host starts it. The user-facing consumption commands stay two, `deploy` (into a project) and `convert` (into
the agent). `serve` is host-agnostic by construction: it speaks MCP and nothing else, so the same process serves Codex,
Claude Code, Pi, Hermes or a custom pipeline. Everything host-specific (where the command line goes, which plugin
slot takes the context) stays in `deploy` and `convert`. With this section, `deploy` and `convert` reduce to
copying context, skills and roles and writing the `command` line into the host's own format; `harnessadapter check` can verify `health` before a run, and `serve` self-checks
at start. The stdio `serve` command exists. The proposed health check, lifecycle switch, standalone launcher, and relay do not; `exports.tools` and the current runtimes are available.

