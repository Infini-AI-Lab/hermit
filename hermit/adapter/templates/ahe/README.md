# ahe template

The harness of Agentic Harness Engineering (arXiv:2604.25850, Sec. 3.1) as an adapter, the same for every host that
runs its hooks: `AGENTS.md` (the system prompt, seeded with the paper's three non-deletable rules, plus tool-usage
guidance), `scripts/` (tool implementations the agent runs as `python3 scripts/<name>.py` from the adapter's folder),
`middleware.py` (the hook pipeline: veto a tool call, append an execution note, stop, queue a reminder),
`.agents/skills/<name>/SKILL.md` with `scripts/` (skills with executable helpers), `agents/<name>.md` (sub-agents,
installed as the host's roles) and `MEMORY.md` (long-term memory, handed to the model at session start by the seed
middleware). The exports are `context`, `skills`, `agents` and `hooks`; a deployment (`hermit deploy`, or the
framework's agent for every task) copies the adapter to `hermit/ahe/` in the workspace and connects them where the
host reads them:

| | Codex | Claude Code |
|---|---|---|
| `context` (`ADAPTER.md`, kept equal to `AGENTS.md`) | `AGENTS.md` | `CLAUDE.md` |
| `skills` | `.codex/skills/` | `.claude/skills/` |
| `agents` | `.codex/agents/*.toml` | `.claude/agents/*.md` |
| `hooks` (`hooks.json`) | `.codex/hooks.json` | `hooks` in `.claude/settings.json` |

The hooks run the adapter's own `hook_runner.py` from that copy on every lifecycle event; the runner reads the host
from the deployment's record (`.hermit-deploy.json`) and hands each call to `hook_hosts.py`, the host's wire, so that
`middleware.py` gets the same `HookInput` and returns the same `HookResult` (`hermit_hooks.py`) on either host. What
each host can and cannot do is the table of `.hermit/EVOLVER.md` (`hook_hosts.CAPABILITIES`). Pi reads no hook file:
its deployment lists the hook commands for the agent to run by hand, and without an event's payload only
`session_start` has something to do (the memory). The runner logs every call to `.hermit-hooks/hook-calls.jsonl` and
the sessions' states to `.hermit-hooks/hook-state/` in the workspace. The template check deploys a copy to each host
and runs one session of that host's events through the hooks it installed, as the host runs them. Trained by the
`ahe_adapter` Evolver: per step the paper's Agent Debugger over each rollout's trajectory and verdict, then the
Evolve Agent editing the tree with a change manifest, scored at the next step.
