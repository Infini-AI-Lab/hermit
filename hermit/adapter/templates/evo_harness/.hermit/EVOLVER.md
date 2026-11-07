# Evo-Harness adapter: structure

- `ADAPTER.md`, the acting agent's context: any text of the adapter's own (for example a task skill), then the block
  between `<!-- evo-harness:general -->` and `<!-- /evo-harness:general -->`, rendered from the skills: each general
  skill in full, and a line per topic skill with its description.
- `general/<name>/SKILL.md`: a cross-task skill, frontmatter `name` and `description`; not deployed as a file.
- `skills/<topic>--<name>/SKILL.md`: a task-type skill, deployed to the host's skills; the directory's name is at most
  64 characters, the longest Codex loads.

- Learnable: the three above. Every skill is named in the block, and `name` equals its directory's.
- Fixed: `.hermit/` (this guide), `exports.yaml` and `README.md`.
