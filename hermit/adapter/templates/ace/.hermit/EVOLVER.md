# ACE adapter: structure

The adapter is one Markdown file, `ADAPTER.md`, deployed as the acting agent's context (AGENTS.md for Codex,
CLAUDE.md for Claude Code, a skill or system prompt of a custom pipeline). It holds ACE's playbook:

- text before `<!-- ace:playbook -->`: the adapter's own text (for example a task skill), kept as it is;
- between `<!-- ace:playbook -->` and `<!-- ace:bullets -->`: the generator instructions (how to use the playbook,
  and to name the bullet ids used);
- between `<!-- ace:bullets -->` and `<!-- /ace:playbook -->`: the playbook, `## SECTION` headers and bullets
  `[slug-00001] helpful=N harmful=N :: content`, one per line, ids unique.

- Learnable: `ADAPTER.md`, UTF-8 text, at most 400,000 bytes, with the three markers once each, in that order.
- Fixed: `.hermit/` (this guide), `exports.yaml` and `README.md`.
