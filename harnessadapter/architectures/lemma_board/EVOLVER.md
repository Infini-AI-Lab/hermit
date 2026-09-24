# Lemma board evolution guide

This architecture is a collaboration protocol for several acting agents that
prove one Lean theorem in parallel. The learned artifact is how they share
work: the acting context (ADAPTER.md) and the board tools (board.py).

What the acting agent receives: ADAPTER.md as project context, and the six
`board_*` tools as an MCP server. The board's state lives outside the adapter,
in the directory named by `LEMMA_BOARD_DIR`; the framework, not the adapter,
verifies submitted proofs (see README.md).

What may change:

- ADAPTER.md: what the agents are told about the board and about working
  together. Keep it about the protocol; it must not contain facts about any
  particular theorem or problem set.
- board.py: the semantics of the tools (claiming rules, what a post must
  contain, what pull inserts, what list shows) and the summary line printed
  after each check.
- adapter.json: tool descriptions and schemas; new tools may be added.

What must stay: the board state format (`board.json` and the `lemmas/` and
`verified/` files) is the contract with the verifier, so change it only
together with the verifier. The environment variables `LEMMA_BOARD_DIR`,
`LEMMA_BOARD_AGENT`, `LEMMA_BOARD_WORKSPACE` and `LEMMA_BOARD_TARGET` are set
by the launcher.

The evidence for evolution is the record of multi-agent runs: each agent's
event log, the board history (`board.json` snapshots) and the outcome. Judge a
change by first-proof time and success rate over repeated runs, not by a
single run; runs of the same problem vary a lot.
