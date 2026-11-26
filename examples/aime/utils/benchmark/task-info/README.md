# AIME

Problems of the American Invitational Mathematics Examination, from MathArena's datasets: two papers of 15 a year,
each answer an integer from 0 to 999. The agent is given MathArena's prompt, "Put your final answer within \boxed{}.
The answer is an integer between 0 and 999 inclusive.", followed by the problem, and has an hour.

The agent is a coding CLI (Codex or Claude Code, as the run's config names) in a sandbox of its own, with a shell and
Python (the packages of the run's Python environment) and no network but its model's; web search is off. Its loop and its
tools are fixed; the adapter is what changes. The adapter is deployed into the working directory as `hermit deploy`
lays it out for that CLI: `ADAPTER.md` is the agent's instructions (a block of `AGENTS.md` for Codex, `CLAUDE.md` for
Claude Code), and the rest of the adapter is copied to `hermit/<adapter id>/`.

The score is 1 when the content of the last `\boxed{}` in the agent's final message equals the reference (compared as
values, so `070` and `70` are the same answer), else 0; a reply without a `\boxed{}` scores 0. Each problem runs in a
fresh workspace: nothing the agent writes in one problem reaches another.

Training and validation problems are AIME 2024's; the adapter is tested on AIME 2025 and AIME 2026.
