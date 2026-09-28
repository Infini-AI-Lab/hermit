# Evolving the async collaboration adapter

The adapter is `collab.py` (tools), `adapter.json` (declarations) and `ADAPTER.md`
(the context the agents read). The launcher (fc_multi/ma_run.py, scenario
`collab`) mounts the library directory, sets COLLAB_* for each agent's MCP server,
grades every agent's Problem.lean and stops the run at the first proof.

What may change: the tool set and their replies, the `[team]` block, the ranking
of open subgoals, the channel's types and limits, the context text. What may
not change: the grading, the cap, the prompt of the launcher, the isolation.
