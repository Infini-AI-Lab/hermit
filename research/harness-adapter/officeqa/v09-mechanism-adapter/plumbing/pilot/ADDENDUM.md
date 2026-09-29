
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
