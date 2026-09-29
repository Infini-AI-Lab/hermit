# v10 pre-registration — OfficeQA Pro, first-50 development subset (DRAFT; hashed after S4/S5)

Status: draft written before the W0 content was merged. The final `PREREG-v10.md` lives in
`officeqa-pro133-v10-plumbing/`, is hashed by `preflight.py` before the first paid call, and every root's
`freeze.json` records that hash; `run.py` refuses a root whose hash differs. Changes after hashing go in a dated
addendum at the end. Fields marked `[FILL]` are completed at protocol freeze (S5).

Design: `officeqa-v10-design/DESIGN.md` (draft 2a) and `review/DESIGN-REVIEW.md`. W0 content:
`officeqa-v10-analysis/synthesis/W0-PROPOSAL.md` (exposure record in §(d)).

## 1. What this run is

A **descriptive, exposure-matched development run** of five arms on the same 50 OfficeQA Pro questions used by the
v9 pilots (subset `pilot-first50`, sha `c95f9613…a07d` [FILL full]), one repeat, temperature 0, no top_p, blocks of 5,
prequential (every question is answered before its reference is released; references of the 5 issue-47 items in the
subset — UID0007, UID0154, UID0155, UID0213, UID0237 — are scored but never released to any learner).

| root | arm | adapter | learner | controller |
|---|---|---|---|---|
| `v10-r1-a` | `plain` | none | — | no |
| `v10-r1-b` | `prompt-evolve` | prompt W0 (text only) | notes | no |
| `v10-r1-c` | `harness-static` | harness W0 (text + tools + controls) | — | yes, gate off |
| `v10-r1-d` | `harness-evolve` | harness W0 | notes + tool revisions | yes, gate off |
| `v10-r1-e` | `harness-evolve-notes` | harness W0 | notes only | yes, gate off |

Root `e` runs if the launch-time cost estimate allows (recorded in the addendum either way).

## 2. Named contrasts and their exposure

| contrast | question it answers | exposure |
|---|---|---|
| `d` vs `e` | does letting the learner change tools add anything over notes alone? | matched (same W0, tools, controls, evidence) |
| `d` vs `c`, `e` vs `c` | does evolution help on top of the hand-built W0? | matched |
| `b` vs `a` | does notes-only evolution help a text-only adapter? | matched on the text side |
| `c` vs `a` | hand-built harness adapter vs the clean base harness | **optimistic**: W0 tools and text were designed from these 50 questions' failures (development set; exposure record §(d) of the proposal) |
| `b` vs `d` | prompt-adapter evolution vs harness-adapter evolution (system contrast) | **optimistic** for `d`: every tool-side development fix lands only on the harness side |

Every contrast is reported on all 50 and on the questions not named in the exposure record of any W0 change.

## 3. Outcome, metric, noise band

- Primary: strict `reward.py` (pinned sha [FILL]) score of the sealed final answer (`final.md`; empty = 0), per arm,
  on all 50 and on the 45 undisputed questions. No fallback answer enters the metric.
- Descriptive status: one repeat cannot separate the expected effects (0–3 questions) from same-configuration
  flips (v8/v9: ~27 % of items flip between identical configurations; SD of a difference of counts ≈ 3.6). A paired
  difference within ±4 questions is reported as noise; ≥ 8 is described as more than noise; nothing in between is
  called an effect. No hypothesis test is run on this data.
- Secondary (reported): per-block prequential curves; paired discordant counts per contrast; learner rounds
  (proposals by kind, screen rejections by reason, replay statistics, verification runs by role with per-run strict
  and out_tokens, admitted revisions with tier, files, diff size); tool invocation and friction counts per arm;
  `observation_coverage`; loop notes and hard blocks; compactions; cost by transport origin.

## 4. Go / no-go for the next step (declared now)

- If `d` − `e` ≥ 4 and `d` − `c` ≥ 4 with at least one admitted tool revision that replay marks as affecting ≥ 3 later
  episodes: proceed to a confirmatory design on unseen questions (non-Pro OfficeQA or SpreadsheetBench), 3 repeats,
  GLMM as in the v9 PREREG.
- If `d` admits no tool revision in 10 blocks: the calibration record decides whether the pipeline (screens, replay,
  judge) or the learner blocked it; fix the pipeline before any further spend.
- If `c` − `a` < 4: the hand-built W0 did not transfer even on the development set; revisit the tools before evolving them.
- Repeats of `c`/`d`/`e` are the first spend of any later budget.

## 5. Pinned at freeze [FILL]

Roots and neutral names; subset ids and sha; order_seed `v10-officeqa-pro133-order`, repeat 1, T = 0, no top_p,
`ACTOR_SECONDS`; both W0 revision hashes, per-file tool hashes, SEED hashes and byte counts, model-visible byte totals
per arm, parity checklist (13 items); `controls.json` values and `CONTROLS_RANGES`; learner constants (model,
reasoning effort, budget, K = 2, KEEP 2/4 by tier, SUPPORT_MAX = 3, slots 2 notes + 1 revision, PROPOSAL_ATTEMPTS,
try_tool ≤ 30, replay ≤ 4 per session, 80 jobs, 300 s cap); screen version hashes (leak, diff screen, import list,
reserved names) and replay thresholds; grader and Codex binary hashes; calibration outcome (one harness learner session
on the v9-a evidence: proposals, screen outcomes, replay outcome, admission) and the expected proposals/admissions per
block; exposure record; analysis script hashes; the b2 = 26-question caveat for cross-day comparisons.
