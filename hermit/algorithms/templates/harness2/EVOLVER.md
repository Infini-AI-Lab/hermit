# Harness² adapter: structure

The adapter is a tree of harness components (Harness², arXiv:2610.14224, Table 5), rendered into `ADAPTER.md`, the
text the acting agent receives:

| path | component | reaches the agent |
|---|---|---|
| `harness/systemprompt.md` | c1 role and procedure | always (first in ADAPTER.md) |
| `harness/guardrails.md` | c2 standing rules | always |
| `harness/LongTermMEMORY.md` | c3 durable domain facts | always |
| `harness/tool_guidance.md` | c4 how to use the tools | always |
| `skills/<name>/SKILL.md` | c6 a reusable workflow (frontmatter `description:`) | rendered under `## Skills` |

- Learnable: the five paths above. `ADAPTER.md` is learnable only as their rendering: `check.py` rejects any other
  content, so edit the component files and render (`harness2.core.surface.render_context`).
- Every skill must be named in one of the always-on files; the four always-on files together are at most 24,000
  characters and ADAPTER.md at most 40,000 bytes (`harness2.yaml`).
- Fixed: `harness2.yaml`, `check.py`, this guide, `README.md`, `skills/README.md`.
