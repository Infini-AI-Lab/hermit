# v9 pre-registration — OfficeQA Pro 133 (development set)

Status: **draft for the user's decisions**. Lines marked `[DECISION PENDING ...]` are open user decisions;
`preflight.py` lists them in `readiness.json` (`prereg_pending_decisions`). The file's sha256 is recorded by
`preflight.py` (readiness) and by every campaign root (`freeze.json` `prereg_sha256`) before its first paid
call; `run.py` refuses a formal run whose PREREG.md differs from the readiness record. Every root of the
study must carry the same hash. Changes after hashing are made only by a dated addendum below the line at
the end, never by editing a hashed section.

Design: `officeqa-v9-design/DESIGN.md` (draft 2) and its reviews (`review/validity.md`, `effect.md`,
`engineering.md`, `generality.md`). Protocol details: `PROTOCOL.md`.

## 1. Claims and hypotheses

- **H1 (system claim).** With the same actor, learner model, tools, seed knowledge, text budget, feedback and
  learning protocol, a harness adapter that evolves executable controls beats a prompt adapter that evolves
  text: `mech-evolve > text-evolve`.
- **H2 (evolution matters).** `mech-evolve > mech-static`.
- **H3 (beats an ordinary harness).** `mech-evolve > plain`.
- **Gatekeeping.** H1, then H2, then H3, each at α = 0.05 (two-sided test; a claim also needs the positive
  sign). Testing stops at the first hypothesis that is not rejected; later ones are reported descriptively.
- **Fallback wording.** If H1 holds and H2 fails, the claim is: *"hand-designed mechanisms beat automated
  text evolution"* — not that evolving mechanisms helps. If H1 fails, no system claim is made; the
  factorial cells are reported descriptively.
- The text × mechanism × static/evolve difference-in-differences is reported descriptively only (too noisy
  to be confirmatory at this budget).

## 2. Arms, repeats, order

| arm | repeats | role |
|---|---|---|
| `plain` | 2 | H3 |
| `text-static` | 1 | factorial cell (descriptive) |
| `text-evolve` | 3 | H1 |
| `mech-static` | 3 | H2 |
| `mech-evolve` | 3 | H1–H3 |

- 12 campaign roots (one arm × one repeat each). Repeat group r = every arm with at least r repeats; the
  roots of a group run the same question order and advance block by block behind a file barrier.
- **Order randomised per repeat**, identical across the arms of a repeat: ascending SHA-256(`order_seed` NUL
  `repeat=r` NUL task_id). `order_seed` = `v9-officeqa-pro133-order` for every root.
- If the pilot (§7) shows campaign-level variance larger than item-level noise, the evolve arms and
  `mech-static` get a 4th repeat, decided **before** the first confirmatory campaign and recorded in an
  addendum.
- `[DECISION PENDING: repeats — confirm 2/1/3/3/3 (12 roots, ≈ $500–600) or 4 for the evolve arms]`

## 3. Primary outcome and analysis

- **Outcome.** `y_{item,campaign}` = strict official `reward.py` score (tolerance 0) of the sealed final
  answer (`final.md`; an empty answer scores 0). No answer fallback enters the metric.
- **Items.** All 133 questions. The same sign is **required** on the 115 questions outside
  databricks/officeqa#47 (the 18 disputed items: `data.ISSUE_47`).
- **Primary model.** Logistic GLMM `y ~ arm + (1 | item) + (1 | campaign)` over all roots of all five arms
  (campaign = one arm-repeat root; arm coded with `mech-evolve` as reference), Laplace approximation
  (`lme4::glmer`, `bobyqa`). H1/H2/H3 are the Wald z tests of the arm coefficients `text-evolve`,
  `mech-static`, `plain` (sign reversed). A singular fit (campaign variance 0) is refit without
  `(1 | campaign)` and reported as such.
- **Robustness (reported, not gating).** (a) Item-level sign-flip permutation of the per-item difference
  summed over repeats (100,000 random flips, two-sided); (b) campaign-level exact permutation (descriptive:
  with 3 vs 3 campaigns the smallest two-sided p is 0.10); (c) two-way (item × campaign) bootstrap CI of the
  difference in correct counts per repeat.
- **Minimum detectable effect.** v8 arm pairs were discordant on 34–37 of 133 items, so one paired
  difference in correct counts has SD ≈ 6 questions. With 3 vs 3 repeats the SE is ≈ 3.4 questions and the
  MDE (α = 0.05 two-sided, power 0.8) is ≈ **10 questions (7.5 pp)** before campaign variance; H3 (3 vs 2)
  ≈ 11 questions. The expected effects (effect review: `mech-static` vs text ≈ +4–6; evolution ≈ 0–1.5) are
  below the MDE, so a null result is likely and is read with TOST.
- **Null reading (TOST).** Equivalence margin ±5 questions per 133-question campaign (±3.76 pp): the mean
  per-repeat difference in correct counts is declared equivalent to 0 when its 90% two-way bootstrap CI lies
  inside (−5, +5). Otherwise a non-significant result is "inconclusive", never "no effect".
- **Exposure check.** 71 of the 133 questions were analysed with labels before v9 (development exposure).
  A secondary GLMM adds `arm × analysed` to test whether the effect differs between the 71 analysed and 62
  unanalysed questions. `[DECISION PENDING: record the sha256 of the analysed-id list before hashing]`

## 4. Secondary outcomes (descriptive)

1% tolerance accuracy; `fallback_answer` accuracy (last submitted candidate, controller arms); per arm and
block: submit rate, gate rounds, findings shown / accepted / kept, enforcement re-query rate, loop notes,
compactions, controller errors, right→wrong and wrong→right flips after a finding; learner rounds (notes /
rules / policy admitted and retired, verification actor runs); cost per question by transport origin
(`cost.py`); the text-vs-mech factorial DiD.

## 5. Fixed protocol choices (declared; see PROTOCOL.md §2)

- L0 identical for all arms: frozen runtime + kit, narrowed read roots, multi-agent off, explicit
  compaction limit 244,800, `include_permissions_instructions=false`, 3,600 s wall clock, unbounded
  transport, reasoning effort `high`, no wrap-up, no answer fallback, v8 COMMON/prompt, published task
  conventions in every README.
- Enforcement re-query only when the gate has findings (`requery_when_clean=False`), at most once per
  episode, skipped with < 60 s left.
- Seed parity: the same seed knowledge as W0 text (text arms, 2,478 B) and as executed rules + minimal text
  (mech arms, 2,429 B counted), one 16 KB model-visible budget per arm (notes + SEED.md + rule templates +
  controller templates).
- Notes: ≤ 3 per episode by keyword trigger, order (admitted_block, id).
- Learners: same model (GLM-5.3-flash), reasoning effort, token budget, evidence builder and domain text;
  released blocks only; issue-47 references withheld from both learners (still scored); a failed learner
  round stops the campaign (fixed, then resumed at block level).
- Pre-registered gate limits on right episodes (from the pilot, enforced before confirmation): union
  finding rate ≤ 20%; right→wrong flips after a finding ≤ 5% of fired right episodes; otherwise the
  lowest-precision rule is disabled in `adapters/mech-w0` (a new W0, recorded in an addendum) before any
  confirmatory campaign.
- **Actor temperature.** `[DECISION PENDING: temperature — pilot cell {0, model-card value} on text-static
  × 20 loop-prone questions; default 0 (broker)]`
- **External reference data.** `[DECISION PENDING: BLS CPI-U monthly table for all arms (neutral for the
  contrasts, ≈ +3 items per arm-run) — default none]`

## 6. Development versus confirmation

OfficeQA Pro-133 is the **development** set: 71 questions were analysed with labels and the seed rules
were chosen on it. Results here are secondary evidence. Confirmation: the frozen, hashed final adapters
(3 per evolve arm) and the static arms are evaluated on a held-out set, each adapter twice per question —
(a) SpreadsheetBench Verified (SkillOpt split; pack built from the train split only), and optionally (b)
non-Pro OfficeQA prepared by script without viewing answers. Each confirmation set gets its own addendum
(same H1–H3, same analysis) hashed before its first paid call. `[DECISION PENDING: confirmation set(s) and
the non-Pro OfficeQA download]`

## 7. Pilot and go/no-go (before any confirmatory campaign)

Pilot: 20 questions × {`text-static`, `mech-static`} + the temperature cell (≈ $10–20), questions chosen
from Pro-133 loop-prone items; pilot runs are never part of the confirmatory analysis. **Go** only if all
hold on the pilot's mech-static episodes:
1. ledger coverage ≥ 60% (the submitted or final answer links to at least one ledger observation or calc);
2. wrong-episode finding rate ≥ 2 × right-episode finding rate;
3. wrong→right flips after a finding ≥ 3 × right→wrong flips;
4. the gate limits of §5 hold (else disable the lowest-precision rule and re-check);
5. submit rate and enforcement re-query rate are reported (no threshold; a submit rate below 50% is
   investigated before going on).
Changes made after the pilot are listed in an addendum before the first confirmatory call.

## 8. Data handling

Predictions are sealed before any reference is released (per block for learner arms, all 133 at the end);
grades exist only after the final seal; the learners never see unreleased references, grades of unreleased
blocks, future questions, other arms' trajectories or the issue-47 references. Reported numbers never sit
next to SkillOpt's published OfficeQA numbers (different scorer).

---
Addenda (dated; appended after hashing):

### Addendum P1: pilot (PREREG §7), drafted 2026-09-28, filled with the user's decisions 2026-09-28

This addendum fixes the pilot before any pilot call and before `run.py pilot-roots`. It replaces the pilot
details of §5 (the temperature cell "on text-static × 20 loop-prone questions") and §7 ("20 questions",
"≈ $10–20") with the user's decisions below. The go/no-go criteria of §7 and the gate limits of §5 are
unchanged. The pilot is never part of the confirmatory analysis (§7).

**Questions (label-free).** 50 OfficeQA Pro questions: the first 50 task ids of the canonical base order.
That is the prepared-manifest order of v3–v8 (ascending sha256("metaworld-officeqa-pro-codex70-online-v1"
NUL uid), uid tie-break), and its first 50 are exactly the historic 50-question set of the v4–v7 runs.
`pilot/select_first50.py` derives the list from the v8 manifest's task ids and the v8 runs' task ids and
ordinals. It checks that the manifest order equals the recomputed seeded order and the order of all three
v8 arms, and that the historic 50-question roots on disk (v4 gold, v5, v6f mwm, v6f prompt, v7 cards) ran
exactly these 50. It read no answer, grade, correctness label or trajectory.

- 5 of the 50 are databricks/officeqa#47 items; they are scored as usual.
- Loop-prone overlap (counts only; `pilot/select_first50.py --overlap`, with the rule of
  `pilot/select_loop_prone.py`): 17 of the 50 are in the loop-prone eligible set (46 of 133 questions).
  4 of them looped in 3 v8 arms, 9 in 2 and 4 in 1; the other 33 looped in no v8 arm.
- The earlier loop-prone draft list (`pilot/loop_prone_20.json`, list sha256
  81d2bf20c9f12033965abd728f71960d31a20e291ae3921bacba0f4da844dc56) is kept for the record and is not used.
  12 of its 20 questions are among the 50.

pilot-list-name: pilot-first50
pilot-list-sha256: c95f9613db9e37effcdaa4de81875f3c045fabb40edfc9c299ca5e60c1b5a07d
pilot-list-file-sha256: 00ff0832c67ff09ae648283b563781c07984deb2bf8c1bf402dbe4a548838a3d
pilot-selection-script-sha256: 318ab73cdf8afdfd03aa007c87e23e68cee494bdc64a8e57660dafba4fe43d81
pilot-metrics-script-sha256: dc302abc268c063c3f916b51f315a88a5c6d48eda82a1c03efc2f0fcf6fd03ba

The list sha256 is `arms.subset_sha256(task_ids)`, the sha256 of the JSON list of the 50 ids in ascending
order. The same value is the `subset.sha256` in every pilot root's `.task-paths.json`. The data preparation,
the campaign, the seal, the grade, the preflight and the audit all check it.

**Cells.** Three roots, created by `run.py pilot-roots` as neutral-named copies of the plumbing:

| root | arm | actor temperature | actor top_p |
|---|---|---|---|
| `v9-p1-a` | `text-static` | 0 | not sent (as v8) |
| `v9-p1-b` | `mech-static` | 0 | not sent (as v8) |
| `v9-p1-c` | `text-static` | 1.0 | 0.95 |

- All three roots use repeat 1 and `order_seed` = `v9-officeqa-pro133-order` (§2).
- Each root runs the 50 questions in the repeat-1 seeded order: the full order with the other 83 questions
  removed. That gives 10 blocks of 5.
- The three roots form one barrier group (`v9-p1-barrier`, `timeout_s` 43,200).
- Everything else is as in §5 and PROTOCOL.md: 3,600 s wall clock, no answer fallback, no learner.
- Temperature and top_p are broker options. They are sent in every upstream body, controller re-queries
  included, and recorded in `freeze.json`, `invocation.json` and `transport.json` (top_p only where it is
  sent). A root that declares no top_p sends none. The audit checks both on every request, and the campaign
  refuses to start when the broker would send anything else. The learners' own model calls keep their own
  settings (the pilot has no learner).

pilot-cell-temperature: 1.0
pilot-cell-top-p: 0.95

Source: the GLM-5.3-Flash model card, its settings for agentic / tool-use evaluation (temperature 1.0,
top_p 0.95).

**Budget.**

pilot-budget-cap-usd: no cap (user decision); estimate ≈ $30-45 at v8 prices

Cost is read after each block (`run.py cost`, `known_cost_lower_bound_usd`, added over the three roots) and
reported. The pilot is not stopped for cost.

**Confirmatory temperature rule** (PREREG §5), pre-registered now, before any pilot call:

If cell c (T=1.0, top_p 0.95) has at least 50% fewer loop runs than cell a (T=0) — a loop run = some
identical normalised command issued ≥3 times — AND its strict accuracy is not lower than cell a's by more
than 2 of 50 questions, all confirmatory arms run at T=1.0, top_p 0.95; otherwise at T=0.

Operational definitions (`pilot/metrics.py`, pinned above, computes both quantities and prints the verdict):

- A *loop run* is a scored episode whose `records/events.jsonl` holds at least 3 completed
  `command_execution` items with the same whitespace-normalised command
  (`harness_core.ledger.normalize_command`, the rule of `pilot/select_loop_prone.py`). Loop runs are
  counted per cell over its 50 episodes.
- "At least 50% fewer": loop runs of c ≤ 0.5 × loop runs of a. When cell a has no loop run, nothing can be
  reduced, so the condition is not met and the rule gives T=0.
- Strict accuracy is the official `reward.py` strict grade (tolerance 0) of the sealed final answer (§3). The
  difference is the strict-correct questions of c minus those of a, on the same 50 questions. The
  condition is: difference ≥ −2.
- "T=0" means temperature 0 with no top_p sent (the v8 body).
- The rule is evaluated only on completed, sealed and graded roots. The resulting temperature is recorded
  in a further dated addendum before the first confirmatory call.

**Go / no-go (copied from §7, unchanged).** The criteria are evaluated on the mech-static episodes
(`v9-p1-b`) by `pilot/metrics.py` (its sha256 is pinned above). That runs after all three roots are sealed
and graded. It grades the pre-finding candidates controller-side with the official `reward.py` (strict,
tolerance 0). **Go** only if all of the following hold:

1. ledger coverage ≥ 60% (the submitted or final answer links to at least one ledger observation or calc);
2. wrong-episode finding rate ≥ 2 × right-episode finding rate;
3. wrong→right flips after a finding ≥ 3 × right→wrong flips;
4. the gate limits of §5 hold: union finding rate on right episodes ≤ 20%; right→wrong flips after a
   finding ≤ 5% of fired right episodes (else disable the lowest-precision rule and re-check);
5. submit rate and enforcement re-query rate are reported (no threshold; a submit rate below 50% is
   investigated before going on).

Operational definitions (fixed here because §7 does not spell them out; `pilot/metrics.py` implements them):

- A *shown finding* is one of two things:
  - a `submit` check with status `findings`;
  - an enforcement (`final`) check with findings whose re-query was actually sent, so the ledger holds its
    `enforce` Note.

  A *fired* episode has at least one shown finding.
- The *pre-finding candidate* is the candidate of the first shown finding.
- An episode's *judged candidate* is its pre-finding candidate when it fired. Otherwise it is the last
  candidate the gate checked, or the final answer when the gate checked none. Right/wrong for criteria 2
  and 4 is the strict grade of the judged candidate. The rates by the final answer's grade are reported
  next to them.
- Coverage (1) counts an answered episode as linked when a number of its answer equals a calc output
  after rounding, or a value a tool printed (the gate's own relations). The answer is the answer span of the
  final message (the gate's own extraction, `packs.officeqa.pack.extract_final`, so prose numbers such
  as years do not count), else the last submitted one. An answer without numbers is linked when its text appears in an observed
  span. The denominator is the answered episodes. The rate over all episodes is reported too.
- Flips (3) compare, per fired episode, the pre-finding candidate with the sealed final answer. When both
  counts are 0, criterion 3 is "no data" and does not count as a pass.
- "Fired right episodes" (4) are fired episodes whose pre-finding candidate was right.
- Criterion 2 with no right episodes is "no data". With a right-episode rate of 0 it passes only if the
  wrong-episode rate is above 0.
- Loop runs (reported for every root) are defined as in the temperature rule above. A loop run
  *recovered* when it still completed with a non-empty answer.

Changes after the pilot (a disabled rule, a new W0, the confirmatory temperature, a 4th repeat) go into a
further dated addendum before the first confirmatory call.
