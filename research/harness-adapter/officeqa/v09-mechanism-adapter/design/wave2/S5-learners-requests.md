# S5-learners: requests for files S5 does not own

S5 wrote only its own files (`evidence.py`, `learner_common.py`, `text_learning.py`, `mechanism_learning.py`,
`tests/test_v9_learners.py`, `tests/fake_learner_model.py`). None of the items below blocks a test: the whole
suite passes as it is (312 passed, 48 opt-in skips). Paths are relative to `officeqa-pro133-v9-plumbing/`.

## S6 (orchestration: campaign.py, arms.py, notes_render.py, full_audit.py, preflight.py, PREREG/PROTOCOL)

S6 was building in parallel and already calls the learners; S5 read its current files to check the seams.
Everything below either matches already (marked "matches") or needs a small change or a decision.

1. **Entry point (matches).**
   `run_learning(key, source, evidence, output, block, stop=None, *, actor_runner, **options)` in both
   `text_learning` and `mechanism_learning`. `options` are the `learner_common.run_round` keywords:
   `pack, grader, model, corpus, root, w0, budget, reasoning_effort, exclude, runner, concurrency, profile`.
   `campaign.call_learner` passes every offered keyword because `run_learning` takes `**options`.
   - `corpus` defaults to `<root>/prepared/agent/corpus` + `corpus.sqlite` (`officeqa_tools.OfficeQACorpus`),
     or `TextDirCorpus` when there is a corpus but no FTS index. This matches S6's `Layout`.
   - `runner` (the sandboxed compute runner for counterfactual offers) defaults to
     `harness_core.runner.make_runner()`.
   - `model` defaults to `learner_common.FireworksModel(key, reasoning_effort='high')`.

2. **Note serving order must stay equal on both sides (matches; please keep it pinned).**
   `learner_common.select_notes` now uses the same order as `notes_render.choose`: matching notes sorted by
   `(admitted_block, id)`, at most `NOTES_PER_EPISODE`. The learner's screen ("would this note be shown on its
   support question?") and the retirement fallback depend on it.
   `tests/test_v9_learners.py::test_selection_and_rendering_equal_the_served_ones` pins the equality of the
   selection and of `render_note`. If you change the order, change `select_notes` in the same edit.

3. **Result statuses.** `result.json['status']` is one of:
   - `admitted`: something was admitted (a note, a rule edit or a policy edit);
   - `revised`: no admission, but a note was retired (new in v9). `activated` is true and `candidate` is the
     new revision;
   - `retained`: nothing changed (`candidate` = source);
   - `failed`: loud failure with `error`; `activated` false, `candidate` = source.
   `campaign.learn` already raises `LearnerFailed` on `failed` and checks `activated` against `candidate`.

4. **Durable learner files (matches).** `<root>/learner-state.json` and `<root>/learning-ledger.jsonl`
   (`learner_common.STATE_FILE` / `LEDGER_FILE`). S6 already backs them up per block. They are written only
   after a round's outcome is final. A failed round appends its in-memory ledger rows as `action: void`
   and does not write the state.

5. **Served-note ids.** The evidence builder reads them from the run's
   `workspace/.acting-prepared.json` `extra.notes` (write-protected for the actor through
   `acting.PROTECTED`), else from `summary.json['notes_served']`. S6 writes both, so this matches.
   Retirement needs these ids; without them it falls back to re-deriving them.

6. **Evidence layout (matches).** S6's `release_block` writes the format `evidence.validate_block` checks:
   - a relative `run_dir` in the manifest;
   - withheld (issue-47) episodes only under `manifest.withheld` / `seal.withheld_episodes`.
   A block whose episodes are all withheld is accepted (tested). Please keep:
   - `api/` in every released run dir;
   - the private field names out of `summary.json` / `failure.json`. The builder refuses
     `gold_answer`, `official_answer`, etc. outside `reference_answer.json`.

7. **full_audit: fields to check in `sessions/learner/control/mechanical-admission.json`.** All values are
   computed, none is hard-coded:
   - `operation_provenance_matches`: the file-level diff of the new revision equals the admitted changes;
   - `protected_unchanged.source` / `.evidence`;
   - `text_budget.ok`;
   - `expected_diff` / `diff`;
   - `admitted_notes`, `admitted_rules`, `policy_changes`, `retired_notes`;
   - `verification` (judge reason per note);
   - `rule_admission` (counterfactual booleans + precision counts per admitted rule);
   - `policy_replay` (loop-guard, gate, enforcement and splice replay counts per policy key).

   A round whose checks fail is itself recorded as `failed`.

8. **Preflight (matches).** `LEARNER_TESTS` already globs `tests/test_v9_learn*.py`. Fast tier: 38 tests in
   about 5 s. Slow tier: `-m slow tests/test_v9_learners.py`, 1 test in about 10 s (the model view of all 133
   v8 none-arm runs).

9. **PREREG / PROTOCOL: declare these learner constants.** They are identical for both learners and are
   **[user]** decisions:
   - `Budget`: input 2.0 M tokens, output 120 k tokens, 400 calls, checkpoint at 600 k prompt tokens. For
     comparison, the v8 cards learner used a median of 2.04 M prompt / 87 k completion tokens per block
     (max 13.5 M / 590 k) with the provider-default reasoning, so about half of the v8 sessions would hit the
     input cap.
   - `reasoning_effort='high'` for both learners. v8 learners sent none (provider default).
   - The note constants: `K=2`, `KEEP=2`, `SUPPORT_MAX=5` (new cost bound), `B_MAX_NOTES=2`.
   - The retirement rule (>= 4 served later episodes, >= 20 points below the same blocks' others).
   - The mechanism admission constants: `MAX_RIGHT_FIRE=0.20`, `MIN_RIGHT=10`, `MAX_EDITS=2`,
     `RULE_RUNTIME_MS=2000`.
   - The consequence of the Wilson bound: with 0 right-episode fires, no rule can be admitted before
     16 right episodes are released.

10. **Cost accounting.** Verification actors run under
    `evolution/BB/verify/<note_id>/q<NNN>-k<j>[-r1]` (the `run_dir` passed to `actor_runner`). The verdict of
    each run is `evolution/BB/verify/<note_id>/q<NNN>-k<j>.verdict.json`. Learner calls are
    `evolution/BB/sessions/learner/control/fireworks-calls/NNN.json` (`meta.usage`, `reasoning_effort`,
    `request_max_tokens`).

## S2 (packs/officeqa)

11. **Optional: `disputed_items` pack attribute.** `learner_common.default_exclude(pack)` uses
    `pack.disputed_items` when the pack declares it. Otherwise it uses the issue-47 list copied from
    `cards_learning.EXCLUDED_TASKS` (a test pins the copy). S6 passes `exclude=data.withheld_ids(root)`
    explicitly, so nothing is needed for correctness.
12. **Optional: binding keys in pack predicate docstrings.** The mechanism learner's prompt shows each
    predicate's keyword arguments and first doc paragraph, plus the module docstrings of the rule modules
    (`packs/officeqa/rules.py` already lists the binding keys). A predicate whose bindings are not documented
    there is hard for the learner to template. Please keep that list complete when predicates change.

## S4 (harness_core)

13. **Optional: timing in `replay.fire_stats`.** `mechanism_learning.rule_stats` re-implements
    `replay.fire_stats` for one rule, only to add per-episode timing (the runtime limit). A test pins that
    the counts are equal. If `fire_stats` returned `max_ms`, the copy could go.

## Contracts owner

14. **Documentation only.**
    - Learned notes are stricter than `NoteSpec` allows: no `'*'` trigger, 1-8 triggers, each with a content
      word of 3 or more letters; `admitted_block` is set by the controller.
    - `result.json` gains the status `revised` (retirement only).
