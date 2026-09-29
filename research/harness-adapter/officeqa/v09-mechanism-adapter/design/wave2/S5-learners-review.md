# S5-learners: adversarial review (wave 2)

**Verdict:** the stream mostly does what its spec asks, and its report is largely accurate. But the review found
**one defect that would have stopped a campaign** (a legitimate rule-edit sequence failed the round), **three
silent or unfair paths**, and **one audit mismatch with S6**. The defects inside S5's files are fixed and
covered by new tests. The items for other owners are in `S5-learners-review-requests.md`.

Rules kept:
- No paid call and no network.
- No credential read.
- No real reference answer or grader opened. The only real data touched were the v8 none-arm `api/` records,
  read by the existing slow test.
- Only S5-owned files were edited.

Paths are relative to `officeqa-pro133-v9-plumbing/`.
`PY=/Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd/adapter-comparison-work/.venv/bin/python`.

## Terms used below

- **Round.** One learner run after a block of questions: it reads the evidence, proposes changes, and writes a
  new adapter revision (or keeps the old one).
- **Provenance check.** After a round, the controller compares the files that actually changed in the new
  adapter with the changes it admitted. Any difference fails the round, as a guard against unapproved writes.
- **Net change.** What differs between the source adapter and the new one once all of a round's edits are
  applied. For example, "set A to 4, then back to 3" has no net change.
- **Launch error.** A verification actor run that raised an error before producing an answer (for example, a
  wiring bug), as opposed to a wrong answer.
- **Sandbox runner.** The separate, locked-down process that re-executes the actor's recorded `calc` code
  when a rule's offer needs a recomputed number.

## 1. Verification

All commands were run from `officeqa-pro133-v9-plumbing/`.

| command | before the fixes | after the fixes |
|---|---|---|
| `$PY -m pytest -q -p no:cacheprovider tests/test_v9_learners.py` | 38 passed, 1 skipped | **44 passed, 1 skipped** (6.5 s) |
| `$PY -m pytest -q -p no:cacheprovider -m slow tests/test_v9_learners.py` | 1 passed | **1 passed** (8.7 s): the model view of all 133 v8 none-arm runs |
| `$PY -m pytest -q -p no:cacheprovider tests` | 313 passed, 49 skipped | **319 passed, 49 skipped** (87 s) |

The skips are the opt-in `slow` and `live_codex` groups.

Before fixing anything, both failure scenarios in §2.1 were reproduced with a scratch test. Each returned
`status: failed` with `RoundFailure: mechanical admission failed: {... "provenance": false ...}`.

## 2. Defects found and fixed (S5-owned files)

### 2.1 A legitimate edit sequence failed the round, and so the campaign (severe)

**Where:** `learner_common.write_revision` and `run_round`.

**What happened:** the new revision was written once per admitted edit, and the expected file changes were
listed the same way.
- **Propose, then modify the same rule in one block.** `rules/<id>.json` was listed as both `added` and
  `changed`, but the real diff shows it only as `added`.
- **Modify a seed rule twice.** The file appeared twice in `changed`.
- **Change a policy knob and then set it back.** `policy.json` was rewritten byte-identically, yet it was
  listed as `changed`.

In every case the provenance check set `provenance: false`, and the round failed. `campaign.learn` then raises
`LearnerFailed`, which stops the campaign. All of these are ordinary learner behaviour: the tools allow them
within `MAX_EDITS=2`.

**Fix:**
- `run_round` now computes the **net** edits:
  - each edited rule is written once, in its final form;
  - a rule or the policy that ends equal to the source is not written.
- `write_revision` compares against the source file's bytes before listing a path as added or changed. It
  refuses a rule written twice.
- `result.json` and `mechanical-admission.json` gain `edits` (every admitted edit, in order) and
  `edits_without_net_change`.
- `admitted_rules`, `policy_changes`, `rule_admission` and `policy_replay` list only net changes, so
  `full_audit` still finds a record for every served difference.
- A round whose edits all cancel out is `retained`.

**Tests:**
- `test_a_rule_edited_twice_in_one_block_is_written_once`
- `test_edits_that_cancel_out_change_nothing`
- `test_write_revision_lists_an_identical_rewrite_as_no_change`

### 2.2 The text learner never saw its own seed text (fairness)

**What happened:** the mechanism learner's prompt lists every rule in full (the seed knowledge in executed
form), plus the vocabulary docs. The text learner's W0 seed text (`SEED.md` = core advice +
`pack.seed_text()`) appeared nowhere in its prompt. It could see it only inside the actor's `AGENTS.md` in the
trajectories, and that item is cut to 1,500 + 500 characters, so the middle, where the seed sits, was lost.
The spec asks for the same domain text for both learners and a text learner that refines its seed. It could
not see what it was refining.

**Fix:** `status_text`, which is shared by both learners, now prints the current adapter's `SEED.md` in full
under "Seed text of the current adapter". For the mechanism arm this is its minimal SEED.md.

**Test:** `test_both_learners_see_identical_evidence` now also asserts that:
- the text prompt holds the pack seed text;
- the mechanism prompt holds its own minimal seed and not the text seed.

### 2.3 A broken verification runner ended as a quiet `retained` (silent failure)

**What happened:** when `actor_runner` raised on every job, every row became `launch_error`, was retried once,
and then counted as inconclusive. The round then ended `retained` with no `error`. Examples: S6's refusal "a
verification adapter is the source or a candidate…", or a signature mismatch. This is the "never silently
return the source" case the spec forbids.

**Fix:** `verify_notes` raises `RoundFailure('verification actor runner failed on every job (N): <first
error>')` when every run is a launch error and no stop was requested. A partial failure is still judged as
inconclusive, as in cards. `result.json` gains `verification_launch_errors`.

**Test:** `test_a_verification_runner_that_always_fails_is_loud`.

### 2.4 Smaller fixes

| # | file | problem | fix | test |
|---|---|---|---|---|
| a | `evidence.py` `model_view` | A missing or unreadable `api/NNN/request.json` became `{}`, so the trajectory was silently thinner, or segmented wrongly if the missing call was a compaction. | Refuse it (`ValueError`); the round fails loudly. All 7,041 v8 call directories have `request.json`, and the S3 broker writes it before every upstream call. | `test_model_view_refuses_a_missing_request_record` |
| b | `evidence.py` `build_evidence` | In S6's release format, withheld (issue-47) episodes appear only under `manifest.withheld`. The builder did not count them, so the learner prompt said "no episode is withheld" and `evidence-index.json` `excluded_ordinals` was empty (an audit hole). | Withheld ordinals are counted and must be distinct integers. An episode that is both released and withheld is refused. | all-withheld test extended |
| c | `mechanism_learning.rule_stats` | The 2 s runtime limit included time spent in the sandbox runner (process start-up for offer re-execution). Admission could therefore depend on how busy the host is. | A pass-through runner measures sandbox time: `max_ms` = engine time (the contract's ≤ 2 s) and `max_runner_ms` = sandbox time (which has its own 5 s wall). | `test_rule_stats_equals_replay_fire_stats` (extended; a 300 ms slow runner stays out of `max_ms`) |
| d | `mechanism_learning.modify_rule` (disable) | Fire counts were taken with `NullRunner`. A rule whose finding cites `{offer.*}` fires only when its offer runs (`offer_failed` otherwise), so the right-episode fire rate that justifies a disable was understated. | Use the real runner. The status section still uses `NullRunner` (it is cheap and shown every round), and now says that its counts can understate such rules. | existing tests |
| e | `learner_common.propose_note`, `mechanism_learning.admit_rule` | Note and rule ids were not leak-screened. Ids name `notes/<id>.json` and `rules/<id>.json` in the adapter directory, which the actor sandbox can read (see request R1). | The id (with `_` turned into spaces) is screened with the text. | `test_note_and_rule_ids_are_leak_screened` |
| f | `learner_common.calc` | The v8 whitelist accepted runaway work that would hang the controller-side session with no timeout. Examples: `[0] * 10**9` (an 8 GB list), `(10**1000)**10`, `round(1, 10**9)`. | Operators take numbers only. Integers are capped at 4,096 bits (the power is checked before it is computed). `round` digits must be within ±30. Lists are capped at 10,000 items. | `test_calc_is_arithmetic_only` (extended) |

## 3. Spec check (what holds)

| spec item | status |
|---|---|
| **1. One evidence builder** | Holds. It covers blocks 1..b, drops issue-47 entirely, and computes grades controller-side through an injected grader (production: VENDOR `reward.py` via `postmortem.grader`, or S6's pinned scorer). The model view is rebuilt from `request.json` segments (every compaction request + the final request and response), with Codex truncation kept. Controller and ledger views are included. Truncation is 600 + 1,400 and 1,500 + 500 for both learners. Private field names are refused. The equality test compares byte-identical tool results and digests. |
| **2. Session and infrastructure** | Holds. The tools are controller-owned only. The budget is the same (2.0 M input / 120 k output / 400 calls; checkpoint at 600 k; ≤ 3 runaway re-sends). Call records use the `client.complete` shape. `result.json` has the v8 keys. `mechanical-admission.json` values are computed. `reasoning_effort` is one parameter for both learners. |
| **3. Text learner** | Holds. `propose_note`: ≤ 2 per block, ≤ `NOTE_MAX`, keyword triggers, leak screen, budget with the W0 seed counted. The paired re-run judge is byte-equal to cards `block.judge` (the test reads the v8 file). Retirement follows the declared rule (on *served* episodes, a stricter reading of "triggered"). Ledger rows go to `learning-ledger.jsonl`. |
| **4. Mechanism learner** | Holds. `propose_rule`, `modify_rule` and `set_policy` work. Admission for new rules: `validate_rule`, leak screen, budget, counterfactual (≥ 1 wrong target: the rule fires, its offer grades right, and it is silent on the reference), `precision_check` (Wilson ≤ 0.2, `min_right=10`), runtime. Policy: `validate_policy` + replay. ≤ 2 edits per block. |
| **5. Tests** | Holds. Toy pack, scripted fake model, fake `actor_runner`, real Ledger / Gate / sandbox. |

## 4. Remaining findings (not fixed here: other owners, or decisions)

1. **full_audit rejects two admission modes S5 allows (audit mismatch). Request R2 to S6.**
   - `full_audit.admitted_rules` requires `rule_admission[id]` to carry both `counterfactual` and `precision`
     for every served rule that differs from W0.
   - S5 admits a **priority-only** change after a schema check only (the record is `{mode: 'priority'}`), and
     a **disable** on measured precision without a counterfactual.
   - Both would be flagged by the audit although they were admitted by declared rules. Either S6 accepts
     `mode in ('priority', 'disable')` with the numbers each mode has, or the user decides that these modes
     need the full admission.
2. **The actor can read the whole adapter directory. Request R1 to S1/S6.**
   - `acting.py` puts `str(Path(adapter).resolve())` in the sandbox read roots, so every note (not only the
     ≤ 3 scoped ones), every rule and the policy is readable with `cat`.
   - This undermines note scoping ("≤ 3 per episode by trigger") and the 16 KB model-visible budget. The rest
     of the adapter is an uncounted channel.
   - In v9 `AGENTS.md` is composed into the workspace, so the actor should not need the directory. Please drop
     it from `read_roots`, or declare why it is needed.
3. **Transport robustness of the learner call. [user] decision.**
   - `FireworksModel` calls `client.complete` without `unbounded=True`, unlike v8. So, as S5 intends, it sends
     `reasoning_effort` and `max_tokens`. But it also gets:
     - `rate_control.MAX_ATTEMPTS = 5` instead of unlimited retries within the timeout;
     - a hard failure when the provider's `completion_tokens` exceeds `max_tokens`. Fireworks may count
       reasoning tokens that way at `'high'`.
   - Either condition fails the learner round, and the campaign stops. It is the same for both learners, so
     this is not a fairness issue. Confirm in the paid smoke; if needed, add a bounded-but-patient mode to
     `client.py` (not S5's file).
   - `client.complete`'s own default payload carries `reasoning_effort=4096` (an integer). S5 overrides it
     with `'high'`.
4. **The mechanism learner's notes are verified in a different context than the text learner's.**
   - The trial adapter for a mech-arm note is the source + the note, without the rule and policy edits
     admitted in the same block. The note is then served with them.
   - The text arm verifies in exactly the served context. This is declared (deviation 7).
   - It is a small validity asymmetry for H1, the primary claim that `mech-evolve` beats `text-evolve`.
     Consider re-verifying against the pending revision.
5. **Admission-only rules have no effect gate.**
   - Priority changes and all policy changes are admitted without any measured benefit; `set_policy` reports
     a replay but only needs the range.
   - Rules without an offer (`answer_count`, `grounded`, `recompute_final` and similar) can never be
     *modified* in wording, triggers or `when`, because modification uses the new-rule admission, which needs
     an offer that grades right.
   - A narrowing change (for example, adding triggers to a noisy offer-less rule) is therefore impossible;
     only a disable above 20 % is. **[user]**: consider admitting "narrowing" modifications on precision
     alone.
6. **Shared proposal cap.** `PROPOSAL_ATTEMPTS = 20` covers notes, rules and policy together. The mechanism
   learner's note attempts therefore compete with its rule attempts, while the text learner gets all 20 for
   notes. The effect is minor and against the mechanism arm; declare it or split the counters.
7. **Policy replay is approximate.** `loop_guard_replay` re-implements `controller_runtime._loop_guard` per
   event rather than per request, and it is not pinned against the real guard by a test. Its numbers are
   reported, not used for admission. The only live cross-check is `recorded_loop_notes` vs `notes_old`.
8. **Warnings rather than failures.** A session that ends `provider_incomplete` or `checkpoint_failed` still
   proceeds, and ends `admitted` or `retained` with `warnings` and `learner_status` recorded. This matches v8.
   It is recorded, not silent, but a strict reading of "fail loudly" would fail these rounds.
9. **Gaps the report already declares, confirmed:**
   - no rule retirement;
   - no union fire-rate check at admission;
   - no decision-point replay;
   - no rule admission before 16 right released episodes with 0 fires (Wilson bound);
   - toy-only end-to-end runs;
   - `reasoning_effort='high'` unverified against Fireworks.

## 5. Accuracy of the implementer report

- The test counts are reproduced. The whole suite now has 313 / 319 tests because S6 and this review added
  tests.
- The claims about truncation, digest equality, the judge equality with v8, the `result.json` keys and the
  loud failure cases are all confirmed.
- **Overstated:**
  - "mechanical-admission.json whose values are all computed" was true, but the provenance check it relies on
    failed on legitimate edit sequences (§2.1).
  - "Failures are loud" missed the all-launch-error verification path (§2.3) and missing broker records
    (§2.4a).
  - The report's list of what `full_audit` checks does not match `full_audit` for priority and disable
    edits (§4.1).

## 6. Files changed by this review

- `evidence.py`: loud missing request records; withheld ordinals counted.
- `learner_common.py`:
  - net edits and byte-compared expected diff;
  - seed text in the shared status;
  - loud all-launch-error verification;
  - note id leak screen;
  - `calc` hardening;
  - result and admission fields `edits`, `edits_without_net_change`, `verification_launch_errors`.
- `mechanism_learning.py`: sandbox time split out of the runtime limit; real runner for disable stats;
  rule id leak screen; status-section label.
- `tests/test_v9_learners.py`: 6 new tests and 4 extended ones.
