# Cards Architecture

A deck of verified rules for a fixed acting agent on tasks graded by code:
short-answer question answering over documents, spreadsheet manipulation,
document visual question answering, and similar families where every attempt
gets a deterministic 0/1 grade.

```text
adapter.json      Context renderer entrypoint; empty tools list; no roles
interface.py      context() -> cards_core.render.compile(ROOT)
cards_core/       Protected: card format (schema.py) and deck compiler (render.py)
PREAMBLE.md       Protected: what cards are and how to use them (fixed text)
cards/            Card files C001.md ...; only admitted ones are served
EVOLVER.md        The contract with the verifier and the learner
```

The deck starts empty. `harnessadapter deploy cards --host codex` writes the
preamble into `AGENTS.md`; no MCP server is registered.

Verification is the launcher's job. A host that evolves this architecture
grades each block with the benchmark's own grader, runs one learner session
that proposes at most two cards, re-runs the actor with each proposal on the
questions it targets and on control questions, and assembles the next deck
from the admitted cards. The block algorithm lives in
`harnessadapter/evolvers/cards/block.py`; the reference host is
`tasks/OfficeQA`-style campaign code that wraps its actor and grader.

Why cards and not a free-form document: a card is the unit of attribution. Each
one is verified alone, recorded alone in the ledger with the runs that admitted
it, and measured alone afterwards on the questions its scope matches. A
document edited as a whole can be neither verified nor blamed in pieces.
