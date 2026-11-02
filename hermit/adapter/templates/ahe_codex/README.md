# ahe-codex template

The harness of Agentic Harness Engineering (arXiv:2604.25850, Sec. 3.1) as an adapter in the layout Codex reads
from its workspace root: `AGENTS.md` (the system prompt, seeded with the paper's three non-deletable rules, plus
tool-usage guidance), `scripts/` (tool implementations the agent runs as `python3 scripts/<name>.py` from the
adapter's folder), `middleware.py` (the hook pipeline: veto a tool call, append an execution note, stop, queue a
reminder), `.agents/skills/<name>/SKILL.md` with `scripts/` (skills with executable helpers), `agents/<name>.md`
(sub-agents, installed as Codex agent roles) and `MEMORY.md` (long-term memory, handed to the model at session start
by the seed middleware). The exports are `context`, `skills`, `agents` and `hooks`: a deployment (`hermit deploy`,
or the framework's Codex agent for every task) copies the adapter to `hermit/ahe-codex/` in the workspace and
connects them as `AGENTS.md`, `.codex/skills/`, `.codex/agents/*.toml` and `.codex/hooks.json`, whose hooks run the
adapter's own `hook_runner.py` (with `hermit_hooks.py`, the middleware's interface) from that copy on every Codex
lifecycle event. The runner logs every call to `.hermit-hooks/hook-calls.jsonl` and the sessions' states to
`.hermit-hooks/hook-state/` under the session's workspace. `ADAPTER.md` is the framework's context export and is
kept equal to `AGENTS.md`. Trained by the `ahe_adapter` Evolver: per step the paper's Agent Debugger over each
rollout's trajectory and verdict, then the Evolve Agent editing the tree with a change manifest, scored at the next
step.
