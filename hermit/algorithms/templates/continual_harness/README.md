# Continual Harness template

The harness state a Refiner maintains, as separate learnable files, after Continual Harness (Karten et al. 2026):

- `policy.md`    — base policy `p`
- `subagents.md` — sub-agents `G` (named playbooks for repeated multi-step patterns)
- `skills.md`    — skills `K` (reusable procedures / step sequences that worked)
- `memory.md`    — memory `M` (facts and notes discovered while acting)

`ADAPTER.md` is the agent's context — the four parts rendered into one document. The `continual_harness` Evolver
CRUD-edits the four source files from the rollouts' failure signatures (label-free) and re-renders `ADAPTER.md`.
