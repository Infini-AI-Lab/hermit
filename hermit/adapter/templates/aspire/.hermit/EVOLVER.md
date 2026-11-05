# ASPIRE adapter: structure

The adapter is ASPIRE's skill library (arXiv:2607.00272, §2.2): what one robot task's validated repair hands to the
next. The acting agent reads all of it, below ADAPTER.md, before its first program (the reader `library` in
`interface.py`; files as context, no ranking).

| path | what it is |
|---|---|
| `ADAPTER.md` | the actor's instructions above the library (at most 8,000 bytes) |
| `skills/README.md` | the index: one row per skill file, linking it |
| `skills/<topic>.md` | entries `## <title>`, each with **Failure signature.**, **When to apply.**, a repair (prose, and when useful one ```python snippet), **Provenance.** |

- A snippet must run under the CaP program rules: only `import math`, no names or attributes starting with `_`, no
  class, with, global, nonlocal, del or async (`.hermit/check.py` parses every snippet). No entry names privileged
  access (simulator internals, success predicates, reward, `getattr`/`setattr`).
- `skills/proposed.md` stages proposals for a verifier, in a copy of the library; it never stays in the adapter.
- At most 12 skill files of at most 24,000 bytes each. Every file is linked from the index, and the index links only
  files that exist.
- Fixed: `.hermit/`, `exports.yaml`, `interface.py`, `README.md`.
