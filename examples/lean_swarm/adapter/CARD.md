# Adapter card: lean-collab v12.5

- Template: `lean-collab@1.0` (this adapter's learnable content is the template's initial content)
- Evolved by: a supervised research loop before this framework existed (a Claude Code session edited the skills after
  reading each round's trajectories, with the user's direction; lean-results `fc_collab_auto`, rounds r55–r69,
  2026-10-02 to 10-04); final version `final_v12_5` = v12.5 + a fix to `check`'s scratch-file output
- Acting agents: five Codex agents with DeepSeek V4.1 Flash (Fireworks), reasoning effort high, one shared team service
  (lemma library, plan, channel), first proof wins
- What was learned: five skills (`good-map`, `good-leaf`, `kernel-check`, `lean-workflow`, `solving-together`) and
  `library/lemmas.md`, a list of every proved statement with full definitions, read before re-proving anything

## Evaluation

Development set (8 problems the skills were tuned on; 2-hour cap):

| Run | Solved | Output tokens | Cost (USD) | API speed |
|---|---|---|---|---|
| r58 (v12.5) | 8/8 | 10.65 M | 20.93 | 75 tok/s |
| r61 (v12.5 repeat) | 6/8 | 10.55 M | 20.60 | 59 tok/s (two problems hit the cap) |

Held-out problems (final54b, 3-hour cap, times normalised to 75 tok/s; in progress, first 24 problems):

| Group | Problems | v12.5 solved | Shared-notes team (s2) | Single agent best of 5 (s1) | v12.5 / s2 total time (min) | v12.5 / s2 cost (USD) |
|---|---|---|---|---|---|---|
| Easy (single agents 5/5) | 11 | 11 | 11 | 11 | 290 / 290 | 10.01 / 10.14 |
| Hard (single agents 0/5, s2 unsolved) | 13 | 1 | 0 | 0 | — | 54.87 / 55.84 |

Where it has not been tested: hosts other than Codex, models other than DeepSeek V4.1 Flash, non-Lean tasks.
