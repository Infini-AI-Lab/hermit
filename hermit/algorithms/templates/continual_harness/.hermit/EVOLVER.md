# Continual Harness adapter: structure

The adapter is the harness state H = (p, G, K, M) of Continual Harness (arXiv:2605.09998, Sec. 3) in the layout Codex
reads from its workspace root:

| path | component | reaches the agent |
|---|---|---|
| `AGENTS.md` | p, the base policy | every turn, injected whole; at most 8,000 characters |
| `sub_agents/<name>/AGENT.md` | G, a delegated role, frontmatter `description:` | when the agent spawns it (a Codex agent role); it answers in text |
| `.agents/skills/<name>/SKILL.md` (+ `scripts/`) | K, a skill, frontmatter `description:` | name and description always; the body when the agent opens it; a helper when it runs `python3 .agents/skills/<name>/scripts/<file>` |
| `memory/NOTES.md` | M, notes under `## <topic>` headings | in the workspace, read and written by the agent during a task |

- Learnable: the paths above, and `ADAPTER.md`, which must be a copy of `AGENTS.md` (`.hermit/check.py` enforces it).
- A skill needs a `description:`; a sub-agent needs a `description:`; every `.py` must compile; a helper a skill or
  `AGENTS.md` invokes must be in the tree; `AGENTS.md` is never empty; `memory/NOTES.md` is always present.
- Fixed: `.hermit/` (this guide and `check.py`), `README.md`, and the placeholder READMEs of the on-demand directories.
