# Lean collaboration adapter: structure

Five proving agents work on one Lean theorem at once and share a team service: a lemma library, a plan of open parts
and a message channel. The adapter is what each agent receives.

| Part | What it is | Learnable |
|---|---|---|
| `ADAPTER.md` | The context every agent reads (placed in its AGENTS.md / CLAUDE.md) | yes: UTF-8 text, at most 60,000 bytes |
| `skills/<name>/SKILL.md` (+ files) | Host skills each agent can load on demand; frontmatter `name`, `description` | yes: UTF-8 text, at most 40,000 bytes per file, 40 files |
| `collab.py`, `assembly.py`, `reactions.py` | The team tools (`check`, `pull`, `refute`, `say`, `plan`) and the service's reactions | no (fixed) |
| `exports.yaml` | The exports: tool declarations (name, description, input schema), the reactions entrypoint, concurrent_tool_calls and the skills directory | yes: must stay valid YAML and keep the five tools and `reactions` with their entrypoints |
| `README.md`, `.hermit/` (this guide) | Documentation and metadata | no (fixed) |

A valid change edits `ADAPTER.md` or `exports.yaml`, or adds, edits or removes skills. Tool replies come from the fixed
code; the context, skills and tool descriptions may say how to use the tools but cannot change what they do.
