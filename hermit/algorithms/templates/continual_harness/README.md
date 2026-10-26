# continual-harness template

The harness state of Continual Harness (Karten et al. 2026, arXiv:2605.09998), H = (p, G, K, M), as an adapter in
the layout Codex reads from its workspace root: `AGENTS.md` (p, the base policy), `sub_agents/<name>/AGENT.md`
(G, delegated roles), `.agents/skills/<name>/SKILL.md` with `scripts/` (K, skills with executable helpers) and
`memory/NOTES.md` (M, notes the acting agent writes during a task and reads in the next). An acting agent that runs
`codex exec` deploys these files into its workspace (`examples/officeqa/utils/agent/codex_agent.py`) and hands the
memory it wrote back with its trajectory; `ADAPTER.md` is the framework's context export and is kept equal to
`AGENTS.md`. Refined by the `continual_harness` Evolver: four passes per step, one per component, from the step's
trajectories, applied in place without a validation gate.
