# Harness² Codex adapter: structure

The adapter is the execution harness of Harness² (arXiv:2610.14224, Table 5) in the layout Codex reads from its
workspace root (google-research/harness2, `CodexSubstrate`):

| path | component | reaches the agent |
|---|---|---|
| `AGENTS.md` | c1-c4: role, procedure, rules, durable facts | every turn, injected whole; at most 8,000 characters |
| `.agents/skills/<name>/SKILL.md` (+ `scripts/`) | c6 a reusable workflow, frontmatter `description:` | name and description always; the body when the agent opens it |
| `scripts/<name>.py` | c7 a deterministic helper | when the agent runs it (`python3 scripts/<name>.py`) |
| `sub_agents/<name>/AGENT.md` | c5 a delegated role, frontmatter `description:` | when the agent spawns it; it answers in text |
| `.codex/hooks.json` | c8 lifecycle hooks (Claude Code hook schema) | inside the tool loop, on every matching call |

- Learnable: the paths above, and `ADAPTER.md`, which must be a copy of `AGENTS.md` (`.hermit/check.py` enforces it).
- Every skill's directory name must appear in `AGENTS.md`; a helper a skill invokes must be in its `scripts/`;
  every `.py` must compile; `AGENTS.md` is never empty. These are upstream's validators (`harness2.core.tree`).
- Fixed: `.hermit/` (this guide and `check.py`), `README.md`, and the four placeholder READMEs that name the on-demand levers.
