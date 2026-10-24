# Continual Harness template

The harness state a Refiner maintains, as separate learnable files, after Continual Harness (Karten et al. 2026):

- `policy.md`    — base policy `p`
- `subagents.md` — sub-agents `G` (named playbooks for repeated multi-step patterns)
- `skills.md`    — skills `K` (reusable procedures / step sequences that worked)
- `memory.md`    — memory `M` (facts and notes discovered while acting)

`ADAPTER.md` is the agent's context — the four parts rendered into one document. The `continual_harness` Evolver
CRUD-edits the four source files from the rollouts' failure signatures (label-free) and re-renders `ADAPTER.md`.

The harness is not only text the agent reads — it is also things the agent **invokes**, through hermit's own export
hooks in `interface.py` (self-contained: it reads only these files, imports no other code):

- **push** — `ADAPTER.md`, the whole harness baked into the context.
- **pull** — the `harness` tool: `harness("skills"|"memory"|"subagents"|"list", {"query": ...})` fetches one part on
  demand (handy when the harness grows past what belongs in every prompt).
- **sub-agents** — each `## <name>` section of `subagents.md` is exported as a delegable sub-agent `G`.

So evolved skills `K`, memory `M` and sub-agents `G` are reachable at act time, not just prose in the prompt. Write
each skill / sub-agent / memory note under a `## <short-name>` heading so it parses into a callable entry.
