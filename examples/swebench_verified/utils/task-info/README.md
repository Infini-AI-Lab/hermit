# SWE-bench Verified

500 GitHub issues of 12 Python repositories (django, sympy, sphinx, matplotlib, scikit-learn, xarray, astropy,
pytest, pylint, requests, seaborn, flask), the human-validated subset of SWE-bench, from the Harbor registry's
`swebench-verified@1.0`. Each task runs in its own Docker container holding the repository at the issue's commit in
`/testbed`, with its dependencies installed: the agent gets the issue's text, edits the repository until it stops or
3,000 seconds run out, and the issue's tests (those the fix must make pass, and those it must keep passing), copied in
only after the agent stopped, decide the score: 1 if the issue is resolved, else 0.

The agent is a coding CLI that Harbor runs inside the task's container, as the container's user with every permission
(no approval prompts) and without web search: Codex (GPT-5.6-Luna) or Claude Code (Claude Haiku 5.5), as the run's
config names. Its loop and its tools are fixed; the adapter is what changes. The adapter is deployed into the task's
working directory as `hermit deploy` lays it out for that CLI:

- every file of the adapter but `.hermit/` is copied to `hermit/<adapter id>/`;
- `ADAPTER.md` is the agent's instructions: a block of the working directory's `AGENTS.md` (Codex) or `CLAUDE.md`
  (Claude Code), after a line that names the folder above;
- `exports.yaml` connects the rest in the CLI's own places: skills, roles (sub-agents), hooks (run on the CLI's
  lifecycle events, from the adapter's folder) and tools (served from outside the container, as an MCP server of the
  session).

A task's container gets 1 CPU and 4 GB of memory. Nothing the agent writes in one task reaches another.
