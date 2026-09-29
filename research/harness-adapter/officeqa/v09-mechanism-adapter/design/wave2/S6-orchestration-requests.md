# S6 (orchestration) — requests for files S6 does not own

None of these blocks the S6 tests: the suite passes with the code as it is. Paths are relative to
`officeqa-pro133-v9-plumbing/`. Each item names the owner, what is needed and why.

## S5 (learners: evidence.py, learner_common.py, text_learning.py, mechanism_learning.py)

1. **Withheld (issue-47) episodes are never written into evidence.** The campaign seals them (seal key
   `withheld_episodes`) but lists them only under the evidence manifest key `withheld: [{id, ordinal}]`; they
   have no directory and no `reference_answer.json`, and they are not in the manifest's `episodes`. So
   `build_evidence(..., exclude=...)` never meets them and `Evidence.excluded` / the listing line "N further
   episodes are withheld" says 0.
   - Ask: count `manifest['withheld']` into `Evidence.excluded` (ordinals), so the learner sees the right number.
   - Ask: `validate_block` refuses a manifest with no `episodes`. A 5-question block whose questions are all
     issue-47 ids is possible in principle (18 of 133). Please accept `episodes == []` when `withheld` is
     non-empty (the campaign still calls the learner for that block).
2. **Note order: one rule for "which ≤ 3 notes are served".** The S6 brief fixes the order as
   (admitted_block, id) among the notes whose triggers match (`notes_render.choose`; the ids go into the
   marker and the audit re-derives them). `learner_common.select_notes` / `note_rank` rank specific triggers
   before `'*'` and longer matching triggers first. The served ids are always read from the marker, so
   retirement statistics are right, but `screen_note`'s check "3 other notes rank before this one" uses
   `select_notes`: a note can pass screening and then not be served by the campaign when ≥ 3 older notes
   match its support question (or be refused although it would be served).
   - Ask: make `select_notes` delegate to `notes_render.choose` (or tell the orchestrator which rule should
     win; changing `notes_render.choose` is one line plus its test).
3. **Text budget formula.** `arms.model_visible` now uses exactly `learner_common.text_budget` (SEED.md +
   rendered notes + enabled rule templates + controller loop/enforce templates). The campaign refuses a
   returned adapter over budget (`arms.check_adapter` -> `LearnerFailed`), so the two must never diverge;
   `tests/test_v9_orchestration.py::test_model_visible_equals_the_learners_text_budget` pins the equality.
   Please keep it (or import `arms.model_visible`).
4. **Durable root files.** The campaign backs up `learner-state.json` and `learning-ledger.jsonl`
   (`learner_common.STATE_FILE`, `LEDGER_FILE`; a test asserts the names) before each round and restores
   them on a block-level resume. If a learner adds another durable file at the root, tell S6 (it goes into
   `campaign.Campaign.LEARNER_FILES`).
5. **What the campaign passes to `run_learning`:** `key, source, evidence (= <root>/evidence/BB), output
   (= <root>/evolution/BB), block, stop, actor_runner, pack (= OfficeQAPack(corpus, tables index) of the
   root), grader (= evidence.Grader(score = the sha-checked prepared/private/reward.py score_answer)), root,
   w0 (= adapters/text-w0 or mech-w0 of the root), exclude (= the manifest's withheld ids)`. It does not pass
   `model`, `budget`, `reasoning_effort`, `corpus` or `concurrency` (your defaults apply).
6. **`actor_runner(adapter, task, run_dir)`** returns the campaign's episode summary (`status`, `answer`,
   `out_tokens`, `transport_failed`, `elapsed_seconds`, `requests`, `notes_served`, ...). It raises
   `ValueError` for a question not released in this campaign, a task text that differs from the frozen
   question, a `run_dir` outside `output/verify/`, an existing `run_dir`, or an adapter that is neither the
   source nor inside `output`. When the campaign stops (signal or group ABORT) it raises and **sets the
   `stop` event it passed you**; `verify_notes` records `launch_error` rows for those, then fails the round
   on `stop.is_set()` — that is the intended behaviour.
7. **Admission record names read by the audit** (`full_audit.admitted_rules`): `admitted_rules`,
   `rule_admission[<id>]` with non-empty `counterfactual` and `precision`, `policy_changes` and
   `policy_replay[<key>]`, in any `evolution/*/**/mechanical-admission.json`. Every served rule that differs
   from its W0 version (new, modified, disabled) must appear there. Please keep these names.
8. **Preflight runs your tests as the learner smoke:** `tests/test_v9_learn*.py`, `tests/test_v9_evidence*.py`,
   `tests/test_v9_*learner*.py` (today: `tests/test_v9_learners.py`). Keep the fake-model learner smoke in a
   file matching one of these.

## S3 / broker owner (broker.py)

9. Repeat of S7-integrate request 1: `summary.json['submit_invalid']` should also count gate status
   `'rejected'`. `report.py` and the episode summary copy the broker's controller counters as they are.

## Contracts owner (harness_core/contracts.py; documentation only)

10. The `TEXT_BUDGET` comment says "notes shown + SEED.md + every rule finding template". The implemented and
    tested formula (S5 `text_budget` = S6 `arms.model_visible`) counts **every** note of the adapter (not only
    the ≤ 3 shown), SEED.md, the enabled rule templates and, for controller arms, the controller's loop and
    enforcement templates (the gate frame is recorded but not counted). Please update the comment.
11. Please document the served-note order next to `NOTES_PER_EPISODE` once item 2 is decided.

## Orchestrator / user decisions (also listed in PREREG.md as `[DECISION PENDING ...]`)

12. Temperature (pilot), external CPI data, repeats (12 roots as drafted: plain 2, text-static 1, others 3),
    the sha256 of the 71-question analysed-id list (exposure check), the confirmation set(s).
13. `text-static` has 1 repeat in the draft (DESIGN: 1–2); 2 would give the factorial cell a replicate.
14. The v8 legacy modules still in the tree (`learning.py`, `full_learning.py`, `postmortem.py`,
    `cards_learning.py`, `audit.py`, `full_runtime.py`, `full_sandbox.py`, `cards_domain.md`) are frozen but
    unused by v9 arms; `cards_learning.py` cannot import in v9 (it needs the v8 framework's
    `harnessadapter/evolvers/cards`). `evidence.vendor_grader` still imports `postmortem`. Consider removing
    the unused ones before the first freeze (owner: whoever owns them; S6 did not touch them).
