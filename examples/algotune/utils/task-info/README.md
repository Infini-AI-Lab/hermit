# AlgoTune

154 numerical programming tasks (linear algebra, optimization, signal processing, graphs, cryptography, statistics),
from the Harbor registry's `algotune@1.0`. Each task describes a problem and its reference solver, and asks for a class
`Solver` in `/app/solver.py` whose `solve` returns the reference's outputs as fast as possible; Python with numpy,
scipy, numba, cython, pythran, jax, torch, ortools, cvxpy and the other packages the instruction lists is installed.
After the agent stopped, the verifier checks the outputs on 100 instances and times the solver against the reference,
interleaved: the trial's speedup is the reference's time over the solver's when every output is valid and the solver
is faster, else 1. The score of a trial is 1 - 1/speedup (0 for no speedup, 0.5 for twice as fast, 0.9 for ten times),
so that the mean of the scores m gives AlgoTune's own score, the harmonic mean of the speedups, as 1 / (1 - m): a task
the solver does not speed up counts as 1x, not as 0, and a large speedup on one task cannot hide slow ones.

The agent is a coding CLI that Harbor runs inside the task's container, as the container's user with every permission
(no approval prompts) and without web search: Codex (GPT-5.6-Luna) or Claude Code (Claude Haiku 5.5), as the run's
config names, for up to an hour. Its loop and its tools are fixed; the adapter is what changes. The adapter is
deployed into the task's working directory as `hermit deploy` lays it out for that CLI:

- every file of the adapter but `.hermit/` is copied to `hermit/<adapter id>/`;
- `ADAPTER.md` is the agent's instructions: a block of the working directory's `AGENTS.md` (Codex) or `CLAUDE.md`
  (Claude Code), after a line that names the folder above;
- `exports.yaml` connects the rest in the CLI's own places: skills, roles (sub-agents), hooks and tools (served from
  outside the container, as an MCP server of the session).

Scripts run inside the task's container with what it has: a shell and the listed Python packages. A task's container
gets 8 CPUs and 64 GB of memory, and the machine running the trials is shared, so timings carry noise. Nothing the
agent writes in one task reaches another.
