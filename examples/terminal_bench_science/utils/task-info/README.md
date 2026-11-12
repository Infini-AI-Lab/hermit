# Terminal-Bench-Science

70 research workflows across the life, physical, earth, mathematical and engineering sciences (fitting and inference
on real data, simulation, data recovery, image and signal analysis), from the Harbor hub's
`terminal-bench-science/terminal-bench-science@0.1.0`. Each task runs in its own Docker container built from the
task's Dockerfile, with its data and pinned packages: the agent gets the task's instruction, works in the container
until it stops or 8 hours run out, and the task's hidden tests, copied in only after the agent stopped and run
without network, decide the score: 1 if they all pass, else 0. A training step runs every task twice.

The agent is a coding CLI that Harbor runs inside the task's container, as the container's user with every permission
(no approval prompts) and without web search (the container itself has network): Codex (GPT-5.6-Luna) or Claude Code
(Claude Haiku 5.5), as the run's config names. Its loop and its tools are fixed; the adapter is what changes. The
adapter is deployed into the task's working directory as `hermit deploy` lays it out for that CLI:

- every file of the adapter but `.hermit/` is copied to `hermit/<adapter id>/`;
- `ADAPTER.md` is the agent's instructions: a block of the working directory's `AGENTS.md` (Codex) or `CLAUDE.md`
  (Claude Code), after a line that names the folder above; an `AGENTS.md` or `CLAUDE.md` the image already had stays;
- `exports.yaml` connects the rest in the CLI's own places: skills, roles (sub-agents), hooks (run on the CLI's
  lifecycle events, from the adapter's folder; python3 is installed for them where the image lacks it) and tools
  (served from outside the container, as an MCP server of the session).

Hooks and scripts run inside the task's container with what its image has: a shell, the task's own Python or R
packages, and the image's tools. A task's container gets 1 to 4 CPUs and 2 to 16 GB of memory, and the machine running
the trials is shared, so timings carry noise. Nothing the agent writes in one task reaches another.
