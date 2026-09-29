# OfficeQA Pro 133 — no adapter, prompt-only, cards (v8)

Three independent single-arm campaigns answer all 133 OfficeQA Pro questions in
the same order, at the same time, with the same pinned dataset, 697 parsed TXT
documents, retrieval helper, native Codex CLI and the exact Fireworks
`accounts/fireworks/models/glm-5p3-flash` model:

- `officeqa-pro133-none-v8-work`: GLM + native Codex, no adapter.
- `officeqa-pro133-prompt-v8-work`: GLM + native Codex + the v6f prompt-only
  adapter (same framework files, W0, learner and reference shelf as v6f,
  verified by hash), learning every 5 questions.
- `officeqa-pro133-cards-v8-work`: GLM + native Codex + the v7 cards adapter
  (same framework revision as v7), learning every 5 questions with verification
  by actor re-runs.

The question order is the SHA-256 order under the v3-v7 seed; its first 50 are
the v4-v7 questions in the same order. 133 = 26 blocks of 5 + a final block of
3. Learning follows blocks 1-26 (always five sealed episodes); nothing is
learned after the final block.

Per-question settings are those of v6f/v7: a 3,600 s wall clock; no token,
request or cost cap; Codex retries with accounting (`request_max_retries =
100`, `stream_max_retries = 100`); `project_doc_max_bytes` 4 MiB; five adapted
submissions are sealed before their references and strict/1% grades are
released to the learner; the learner never sees unreleased answers or future
questions. Questions within an arm run one at a time, as before.

## Fixes in v8 (the same in all three campaigns)

Found by the baseline-variance audit of v6f/v7 (2026-09-26):

1. Provider defects are re-sent by the relay and never reach Codex. The relay
   buffers each response before forwarding it, so a defective one is re-sent
   with identical bytes: a `completed` response with zero usage (Codex ended
   the turn and a progress note was graded, or truncated tool-call JSON
   poisoned every later request), a `completed` response with nothing to act
   on, an `incomplete` max-output runaway with no visible output (its ~65K
   reasoning tokens stayed in Codex's history), and function-call arguments
   that are not a JSON object.
2. The configured reasoning effort (`high`) is forwarded to Fireworks. v5-v7
   dropped the `reasoning` field in the unbounded profiles, so every run used
   the provider default.
3. The MWM runtime file is no longer copied into no-adapter workspaces.
4. The prompt-only learner's sandbox resolved `codex` through PATH, where an
   older CLI (nvm's 0.134, no `sandbox -P`) could make every learning round
   fail quietly; the campaign now puts the actors' pinned binary first.
5. The cards learner excludes all 18 questions that databricks/officeqa#47
   lists as questionable from support and control (v7 listed 3 of them); its
   prequential hits survive a failed round, a failed round never activates a
   deck, and a learner response that runs away with no visible output is
   re-sent (at most 3 times) instead of kept in its history.

Frameworks and the Codex binary are private copies under `frameworks-v8/`
(`next-a48c0f9` for the no-adapter and cards roots, `prompt-c0c9b87-v6f` with
its uncommitted v6f working tree for the prompt root, and
`codex-0.155.0-alpha.9.2`), hashed at freeze and re-checked every block, so an
app update or a framework edit elsewhere cannot change a running campaign.

Because of fixes 1-2, v8 scores are not directly comparable with v4-v7.

## Outputs per root

`state.json`, `runs/<arm>/<NNN>/`, `evidence/`, `evolution/`,
`private-evaluation/paired-metrics.json` (per-arm strict and 1% accuracy),
`preflight/final-audit.json`, `cost-estimate.json`, `report.html`.
The three roots are compared by task id afterwards.
