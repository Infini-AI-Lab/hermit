# Prompt-only adapter: structure

The adapter is one Markdown file, `ADAPTER.md`. When deployed, its text is placed in the acting agent's project
instructions (AGENTS.md for Codex, CLAUDE.md for Claude Code, or the system prompt of a custom pipeline).

- Learnable: `ADAPTER.md`, UTF-8 text, at most 40,000 bytes; it must stay (a deleted one is restored).
- Fixed: `.hermit/` (this guide), `exports.yaml` and `README.md`.

A valid change is any rewrite of `ADAPTER.md` within that size.
