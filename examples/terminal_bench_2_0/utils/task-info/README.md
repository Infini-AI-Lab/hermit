# Terminal-Bench 2.0

89 terminal tasks (software engineering, system administration, scientific computing, security, data science,
debugging, model training and more; 4 easy, 55 medium, 30 hard), from the Harbor registry's `terminal-bench@2.0`.
Each task runs in its own Docker container, started from the task's image: the agent gets the task's instruction,
works in the container until it stops or an hour runs out, and the task's hidden tests, copied in only after the agent
stopped, decide the score: 1 if they all pass, else 0. A training step runs every task twice.

The agent is a coding CLI that Harbor runs inside the task's container, as the container's user with every permission
(no approval prompts) and without web search: Codex (GPT-5.6-Luna) or Claude Code (Claude Haiku 5.5), as the run's
config names. Its loop and its tools are fixed; the adapter is what changes. The adapter is deployed into the task's
working directory as `hermit deploy` lays it out for that CLI:

- every file of the adapter but `.hermit/` is copied to `hermit/<adapter id>/`;
- `ADAPTER.md` is the agent's instructions: a block of the working directory's `AGENTS.md` (Codex) or `CLAUDE.md`
  (Claude Code), after a line that names the folder above; an `AGENTS.md` or `CLAUDE.md` the image already had stays;
- `exports.yaml` connects the rest in the CLI's own places: skills, roles (sub-agents), hooks (run on the CLI's
  lifecycle events, from the adapter's folder; python3 is installed for them where the image lacks it) and tools
  (served from outside the container, as an MCP server of the session).

Hooks and scripts run inside the task's container with what its image has: a shell, python3, and the image's own
tools; assume no other packages. A task's container gets 1 to 4 CPUs and 2 to 8 GB of memory, and the machine running
the trials is shared, so timings carry noise. Nothing the agent writes in one task reaches another.
