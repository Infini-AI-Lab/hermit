# harness2-codex template

The execution harness of Harness² as an adapter in Codex's own layout: `AGENTS.md`, `.agents/skills/`, `scripts/`,
`sub_agents/`, `.codex/hooks.json`. An acting agent that runs `codex exec` deploys these files into its workspace
root (`examples/officeqa/utils/agent/codex_agent.py`); `ADAPTER.md` is the framework's context export and is
kept equal to `AGENTS.md`. Trained by the `harness2_upstream` Evolver, which edits the tree with upstream Harness²'s
own Codex surface and validators.
