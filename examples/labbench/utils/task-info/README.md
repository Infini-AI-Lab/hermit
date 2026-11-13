# LAB-Bench FigQA

181 multiple-choice questions about figures from biology papers (LAB-Bench's FigQA subset), from the Harbor registry's
`labbench@1.0`. Each question runs in its own Docker container: `/app/instruction.md` holds the question and its
lettered choices (shuffled when the container starts), the figure is an image file in `/app`, and the agent must
write the letter of its answer to `/app/answer.txt` within 300 seconds. After the agent stopped, the verifier scores
the letter: 1 for the right choice, else 0. Most questions need the figure itself: what it plots, its axes, legends,
panels and the trends or values it shows.

The agent is a coding CLI that Harbor runs inside the question's container, as the container's user with every
permission (no approval prompts) and without web search: Codex (GPT-5.6-Luna) or Claude Code (Claude Haiku 5.5), as
the run's config names; it can open the figure as an image and run Python 3.11 in the container. Its loop and its
tools are fixed; the adapter is what changes. The adapter is deployed into the working directory as `hermit deploy`
lays it out for that CLI:

- every file of the adapter but `.hermit/` is copied to `hermit/<adapter id>/`;
- `ADAPTER.md` is the agent's instructions: a block of the working directory's `AGENTS.md` (Codex) or `CLAUDE.md`
  (Claude Code), after a line that names the folder above;
- `exports.yaml` connects the rest in the CLI's own places: skills, roles (sub-agents), hooks and tools (served from
  outside the container, as an MCP server of the session).

The container has a shell and Python 3.11 with its standard library only. Nothing the agent writes in one question
reaches another.
