# 1. Deploy an existing adapter

You have an adapter (one of ours, or one a training run produced) and want an agent to use it. No model call, no
dataset: `deploy` writes the files a host reads.

## Pick an adapter

| Adapter | What it carries |
|---|---|
| `examples/lean_swarm/adapter` | instructions, five skills and five tools for Codex agents proving Lean theorems together |
| `harnessadapter/algorithms/templates/prompt_only` | the smallest adapter: instructions only (`ADAPTER.md`) |
| `<run>/checkout/` | the adapter a training run ended with |

Check an adapter against its template first; it must pass before it is deployed:

```bash
python -m harnessadapter.spec examples/lean_swarm/adapter --root harnessadapter/algorithms/templates/lean_collab
```

## Into a project

```bash
harnessadapter deploy --adapter examples/lean_swarm/adapter --workspace /tmp/ws --host codex       # or claude-code
cd /tmp/ws && codex                                                                                     # or claude
```

`--workspace` is the project directory you will start the agent in: the adapter's instructions go into `AGENTS.md` (Codex) or `CLAUDE.md`
(Claude Code) inside a managed block, skills and roles under `.codex/` or `.claude/`, and the tools become an MCP
server entry that starts the framework's wrapper with this machine's absolute paths. The adapter is active only
when the agent runs in that directory. Re-running `deploy` on the same directory replaces the managed block and the
config; the project's own files are kept.

```bash
cat /tmp/ws/AGENTS.md                     # the instructions the agent reads
cat /tmp/ws/.codex/config.toml            # the MCP entry for the adapter's tools
```

A context-only adapter needs nothing installed on the agent's machine; an adapter with tools needs
`harnessadapter` there, because the tools are the adapter's own Python served by the framework's wrapper.

## Into the agent

`convert` packages an adapter in a host's native plugin format (Claude Code, Codex, Pi, Hermes), installed once and
active in every project. It is in review; until it lands, use `deploy`. The two ways and what each host can carry
are compared in [What is a harness adapter?](../framework/what-is-a-harness-adapter.md#two-ways-to-use-an-adapter).

## An adapter that needs a service

Some adapters bring their own service (shared state between several agents, long-running tools). The Lean
collaboration adapter is one; how it is started today and how the manifest will declare it are in the
[Lean collaboration recipe](../recipes/lean-collaboration.md).

Next: [Reproduce a result](reproduce-a-result.md).
