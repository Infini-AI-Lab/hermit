# Lemma Board Architecture

A collaboration protocol for N acting agents proving the same Lean theorem in
parallel, asynchronously: nobody waits for anybody. The shared channel is a
board of lemmas. An agent posts a lemma statement, claims it, submits a proof;
a verifier outside the adapter compiles the proof on its own and marks it
done; any agent can then pull the verified source into its own file.

```text
adapter.json   six board_* tools
ADAPTER.md     what the acting agent is told about the board
board.py       the tools: state in $LEMMA_BOARD_DIR, no Lean inside
EVOLVER.md     what may evolve
```

Deploy into each agent's workspace (`harnessadapter deploy lemma-board --host
codex --workdir <agent workspace>`), then start the host with these variables
in the MCP server's environment:

| variable | meaning |
| --- | --- |
| `LEMMA_BOARD_DIR` | the shared board directory (read-write for every agent) |
| `LEMMA_BOARD_AGENT` | this agent's id, the owner name on the board |
| `LEMMA_BOARD_WORKSPACE` | the directory holding this agent's Problem.lean |
| `LEMMA_BOARD_TARGET` | the target theorem's name (where `board_pull` inserts) |

Board state: `board.json` (one entry per lemma), `lemmas/<name>.lean`
(submitted proofs), `verified/<name>.lean` (accepted proofs), `Shared.lean`
(all accepted proofs in acceptance order, for reading). Writers take
`board.lock`. The verifier is the launcher's job: it watches for `submitted`
entries, compiles each against the original file plus the verified lemmas,
and sets `done` or `rejected` (with the compiler message). The reference
launcher is `fc_multi/ma_run.py` in the Lean results repository, scenario
`board`.
