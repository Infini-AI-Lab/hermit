# S8-e2e review: requests for other owners

Paths are relative to `officeqa-pro133-v9-plumbing/`. Items 1–4 block the 20-question pilot (READINESS §6). Items 5–6 are recommended.

## Orchestration / data owner (S6), new pilot work

1. **Pilot subset runs.** The loop assumes all 133 questions in several places:
   - `data.verify_prepared` (`sample_count == 133`, v3–v8 seed);
   - `data.seal_predictions` / `grade` (full manifest);
   - `preflight.offline` (`question_count_133`);
   - `full_audit` (full order).

   Add a declared subset: a `.task-paths.json` key such as `"subset": {"ids": [...], "sha256": ...}`, refused unless its sha matches a PREREG addendum. Grade, audit and seal it the same way, and keep the base order and the issue-47 handling.
2. **Temperature as a declared option.** `broker.normalized` hard-codes `temperature=0`. Needed:
   - a broker option fed by `experiment.TEMPERATURE`, which in turn comes from a new `.task-paths.json` key;
   - the value recorded in `freeze.json`;
   - the audit's per-request check keeps working;
   - `campaign.broker_temperature` compares the configured value.
3. **Pilot go/no-go script** (PREREG §7), controller-side and after the final seal. It computes, over the mech-static episodes:
   - ledger coverage;
   - the finding rate on right vs wrong episodes;
   - wrong→right and right→wrong flips after a finding (grade each pre-finding candidate from `ledger.jsonl` Candidates with the pinned `reward.py`);
   - the union right-episode fire rate and the right→wrong share (the §5 gate limits);
   - submit and enforcement re-query rates.

   Output a JSON with each criterion and pass/fail.
4. **Loop-prone question list.** A label-free selection script over the v8 api records (for example, runs with ≥ 3 identical normalised commands; `harness_core.ledger` already normalises commands). Output 20 ids and their sha for a PREREG addendum. It must not read references or grades.

## Broker owner (S3)

5. **Repeat of S8 request 4.** On `Broker.__exit__`, close the active upstream response: shut down its socket so the blocked read raises. The stalled call's `transport.json` / `summary.json` would then be written before the episode summary is taken. Update the e2e adversity assertions accordingly.

## Contracts / pack owner (S2/S4), low priority

6. `OfficeQAPack.question_spec` never yields `value_type='text'`, so `answer_count` fires on any answer without a parseable number. Examples: the smoke's `TRANSPORT_<hex>`, or a word answer that the question does not quote. Watch its right-episode fire rate in the pilot. Consider `value_type='text'` for questions that ask for a name, month or word.
