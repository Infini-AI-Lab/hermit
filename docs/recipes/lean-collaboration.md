# Lean collaboration

The `lean-collab@1.0` template equips a team of acting agents to prove one Lean theorem together. It is the
framework's example of an adapter that **needs a standalone service**: its tools keep state that several agents
share, its calls run for minutes and must not block one another, and it can decide to interrupt or resume an
agent. This page shows what the adapter contains, how it is used today, and how that use maps onto the
consumption-end design in [What is a harness adapter?](../framework/what-is-a-harness-adapter.md) and the
`serve` section of the [adapter.yaml reference](../framework/adapter-manifest.md#serve-design-not-implemented).

A finished adapter of this template, v12.5, is in `examples/lean_swarm/`; its `CARD.md` records how it
was obtained and how it performs. The template and the host deployment are in this branch; the multi-agent
launcher and the grader are not yet a pipeline of this framework, so there is no end-to-end training recipe here.
Use [OfficeQA](officeqa.md) for one.

## What the adapter contains

```text
harnessadapter/algorithms/templates/lean_collab/
  adapter.yaml     template, exports (context, five tools, reactions, concurrent_tool_calls, skills), structure
  ADAPTER.md       learnable instructions every agent reads
  skills/          learnable skills (good-map, good-leaf, kernel-check, lean-workflow, solving-together)
  collab.py        fixed: the five tools — check, pull, refute, say, plan
  assembly.py      fixed: proof assembly
  reactions.py     fixed: the reaction handler
  EVOLVER.md, README.md
```

The exports are the plugin-shaped part: context, skills and tools, usable by any host that reads an instruction
file and starts an MCP server. Two declarations go beyond what a plugin can carry and are why this adapter has a
service:

| Declaration | What it means |
|---|---|
| `concurrent_tool_calls: true` | one agent's calls run one at a time, different agents' calls run in parallel: a `check` compiles against Mathlib for minutes while others keep working |
| `reactions: reactions.py:react` | every few seconds the service hands the agents' status to this function; it returns commands (interrupt or resume an agent with a text) that the launcher carries out |

Shared state lives in `COLLAB_DIR`, outside the adapter: `state.json` (lemmas, subgoals, agents, events,
messages), `verified/*.lean`, `channel.jsonl`, and `index.md` as the rendered view. Every tool reply carries the
team's delta since the caller's last call.

## Why it needs a standalone service

A per-session MCP server, the one `deploy` configures for ordinary adapters, is started by the host and dies with
the session, and holds nothing in common with any other session. Here five agents must see one plan, one lemma
library and one channel; a lemma verified by one must be pullable by the others a second later; and the adapter
must be able to act on an agent that is stuck. So the service is one process per run, started before the agents,
serving each of them over its own socket, alive until the run ends. The socket is the agent's identity: a jail
binds only its own agent's socket, so no token is needed.

## How it is used today

Everything below is done by the Lean run script (`lean-results/fc_multi/run_collab.py`, outside this
repository), not by `deploy`:

1. **Start the service**, one per run, from a JSON config:

   ```bash
   python -m harnessadapter.runtime.service --config run.json
   ```

   ```json
   {"adapter": "examples/lean_swarm/adapter", "control_socket": "/run/collab/control.sock", "tick": 5,
    "env": {"COLLAB_DIR": "...", "COLLAB_TARGET": "...", "COLLAB_REPO": "...", "COLLAB_LAKE": "..."},
    "agents": {"A1": {"socket": "/run/collab/A1.sock", "audit_log": "...", "env": {"COLLAB_AGENT": "A1", "COLLAB_WORKSPACE": "..."}},
               "A2": {"socket": "/run/collab/A2.sock", "...": "..."}}}
   ```

   The service exposes one MCP endpoint (streamable HTTP over a unix socket) per agent and a control socket.

2. **Deploy a workspace per agent** with the adapter's context, skills and roles:

   ```bash
   harnessadapter deploy --adapter examples/lean_swarm/adapter --workspace /work/A1 --host codex
   ```

   and point the agent at its own endpoint instead of the per-session wrapper `deploy` wrote: the run script
   passes it to Codex on the command line (`-c mcp_servers.team.url=…`). Supported host today: Codex.

3. **Start the agents**, each in its workspace and jail, with the Lean toolchain on its path (`ELAN_HOME`, `PATH`,
   `HOME`) and the `COLLAB_*` variables above.

4. **Carry the reactions**: the run script polls the control socket for the commands the adapter's `react` decided
   and interrupts or resumes the agents accordingly. The run script decides nothing itself.

The tools need a working Lean/Mathlib checkout (`COLLAB_REPO`, `COLLAB_LAKE`); nothing in the framework installs
it.

## How this maps onto the consumption design

The current integration uses the explicit service and run-script steps above. Generic manifest-driven startup, relay discovery, health checking, and host reactions remain a [design proposal](../developer/service-design.md). Do not use proposed `--standalone` or `--serve-url` flags with the current CLI. The implemented `serve DIR` command provides a per-session stdio tool server.

## What is needed to train it

Provide a pipeline that launches the agents with the adapter and collects a proof, a judge that checks the result,
and task data and splits. The framework calls those through the same [task interface](../framework/task.md) used
by OfficeQA. Select `lean-collab@1.0` as the template and a compatible Evolver. Its permitted edits cover
instructions and skills; the collaboration implementation (`collab.py`, `assembly.py`, `reactions.py`) is fixed.
