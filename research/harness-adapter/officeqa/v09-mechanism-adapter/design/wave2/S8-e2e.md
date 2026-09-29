# S8-e2e: a $0 end-to-end rehearsal of the v9 pipeline, and its integration fixes (wave 2)

**Status: done. The whole v9 pipeline runs end to end on the toy pack with the real pinned Codex binary. Six
integration defects were found, fixed and pinned by tests. Every test passes.**

| tier | result |
|---|---|
| `tests/test_v9_e2e.py -m live_codex` (the rehearsal) | **12 passed**, 88 s; three consecutive repeats of the module: 15/15 each |
| `tests/test_v9_e2e.py` default tier (fast regression tests of the fixes) | 3 passed, 12 skipped (opt-in), 0.1 s |
| whole suite, default | **329 passed, 61 skipped** (the opt-in groups), 94 s (before S8: 326 passed, 49 skipped) |
| whole suite, everything (`V9_SLOW=1 V9_LIVE_CODEX=1`) | **390 passed, 0 skipped**, 307 s |
| `arms.py check` / `cost.py --self-test` | OfficeQA W0s unchanged (text 2,478 B, mech 2,429 B) / passed, 0 paid calls |

Rules kept:
- No paid call. No network except 127.0.0.1. No API key: the broker ran with a dummy string, and a test checks
  that it appears in no record.
- No reference answer or grader of the real benchmark was opened. The only references are the toy's synthetic
  ones, and the toy grader stands in for `reward.py`.
- v8 directories and other projects were not touched.

Paths are relative to `officeqa-pro133-v9-plumbing/`.
`PY=/Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd/adapter-comparison-work/.venv/bin/python`.

## Terms used below

- **Rehearsal**: running the real pipeline with fake model endpoints, to find wiring defects before money is spent.
- **Fake upstream**: `tests/fake_upstream.py`, a local HTTP server on 127.0.0.1 that speaks the Responses
  streaming format (SSE, Server-Sent Events: a stream of `data: {json}` lines). The broker sends it what it
  would send Fireworks.
- **Weak actor**: the scripted model behind the fake upstream. It makes typical mistakes on purpose (see §1.2),
  so every harness mechanism has something to act on.
- **Repeat group**: the roots (one per arm) that share one question order and one file barrier.
- **Seal / release**: the block's predictions are hashed (sealed) before any reference is copied (released) into
  the learner's evidence.
- **Block-level resume**: after a crash, the unfinished block is moved aside and run again. Finished blocks are
  kept.
- **Admission mode**: how the mechanism learner's controller accepts a rule edit. It is `full` (counterfactual +
  precision replay), `priority` (a priority-only change, schema check) or `disable` (measured noise).

## 1. What was built

### 1.1 `tests/e2e/` (fixtures; 472 lines)

- **`toy_campaign.py`**
  - `code_copy`: a frozen code copy whose `adapters/text-w0` and `adapters/mech-w0` are built for the toy pack,
    i.e. `arms.py build --pack toy`.
  - `make_root`: a root in the exact `data.py` layout.
    - `prepared/agent/corpus`: the 3 toy bulletins.
    - `agent/corpus.sqlite`: the same FTS5 (full-text search) index builder as OfficeQA.
    - `tasks.jsonl`, `private/answers.jsonl` (the toy references), `corpus_manifest.json`.
    - `manifest.json`: built by `data.manifest_v9`.
    - One toy question (toy06) is declared disputed. Its reference is withheld from learners and it is still
      scored, like OfficeQA issue #47.
  - `toy_score`: the toy grader in the `reward.py` `score_answer(reference, predicted, tolerance)` shape.
- **`weak_actor.py`**: `respond(body)` is a pure function of the request body, i.e. of what the model sees. One
  fake upstream therefore serves all roots and all episodes at once. For each toy question it:
  - reads README.md first (as the prompt asks) and learns from it which tools exist and where the corpus is.
    The plain arm uses `rg` / `python3 -c` instead of the pack tools;
  - makes that question's mistake: first printing (toy01, toy07), percent for a decimal (toy03), one value for
    two (toy05), the wrong quarter's price (toy09);
  - loops on one lookup (toy02, toy09): 5 times, or until a `[harness:loop_guard]` note appears;
  - answers two questions without `submit`: toy04 (the enforcement check is clean) and toy10 (the check finds
    something, so the broker re-queries);
  - on toy06, first probes the sandbox: `ls ..`, `cat ../records/invocation.json`, the root's `state.json`, the
    answer key, the hidden marker;
  - on toy08, reports 250,000 input tokens once. That is above the explicit compaction limit of 244,800, so
    Codex compacts the context. The compaction reply is a summary with a `STATE {...}` line, from which the
    actor resumes;
  - after a gate finding, accepts the offer, fixes the answer or keeps it (per question). On toy07 the first
    `--keep` reason is over the gate's 500-character limit, so the gate rejects that payload;
  - reacts to a scoped note only through the note's text ("revised printing" → reads the `_b` file).
- **`learners.py`**: scripted learner models (`ScriptedModel`). Both first read `list_episodes` and base every
  proposal on the wrong episodes it lists, so their decisions are a function of the evidence.
  - Text learner: one scoped note (`revised_figures`, trigger "widgets shipped").
  - Mechanism learner: one new rule (`revised_cells`, a copy of the printing check), then one policy knob
    (`loop_threshold` 3 → 4) or one priority-only `modify_rule`, then the same note (optional).

### 1.2 `tests/test_v9_e2e.py` (789 lines; 15 tests: 12 live, 3 fast)

**Main rehearsal** (one module fixture; 5 roots `v9-r1-a..e` = plain, text-static, text-evolve, mech-static,
mech-evolve).
- The 5 roots run concurrently, one thread each, behind one `FileBarrier`.
- The toy pack has 10 questions in 2 blocks of 5; order seed `toy-e2e-73`.
- Every episode, scored or verification, takes the production path:
  1. `campaign.prepare_episode`: kit, README, AGENTS.md with ≤ 3 notes;
  2. `campaign.execute_episode`, i.e. the real `Broker`. Controller arms also get the real
     `Ledger` + `Gate` + `SandboxRunner` + `MechController`;
  3. `acting.run_actor`: the pinned Codex under the v9 Seatbelt profile (the macOS OS sandbox), with the v9
     settings.
- Learning uses the real S5 rounds (`text_learning` / `mechanism_learning`), called through
  `campaign.call_learner`. Paired re-run verification goes through `Campaign.verification_runner`, so it too
  takes the real episode path.
- Then, for every root: final seal + grade, `full_audit.build`, `cost.estimate`, `report.build`.

What each live test asserts:

| test | asserts |
|---|---|
| `test_every_root_completes_and_the_audit_passes` | 5/5 roots `completed`; `full_audit` passes on each (391 checks for plain, 413 text-static, 718 text-evolve, 473 mech-static, 720 mech-evolve) |
| `test_order_barrier_seal_release_and_grades` | one order per group; every root arrived at blocks 0-2, no ABORT; exact answers per arm; grades on all 10 and on the 9 undisputed; learner arms seal block 1 with toy06 under `withheld_episodes`, release exactly the 4 other references, nothing of block 2; static arms have no seals, evidence or evolution |
| `test_controller_arms_record_loop_notes_submits_findings_and_splices` | per mech arm and question: loop notes (after the 3rd identical lookup; the 4th in mech-evolve's block 2 under its learned `loop_threshold=4`); one compaction with a frozen `[harness:ledger]` splice (`turn, turn, compaction, resume, turn`); one enforcement re-query (toy10, origin `controller`) and a clean unsubmitted final (toy04, no re-query); gate findings, then accept/fix/keep, with check files and candidates; the gate-rejected over-long `--keep` counted in `submit_invalid`; totals per mech arm: loop 2, ledger 1, enforce 1, submits 15, gate rounds 6, invalid 1 |
| `test_text_arms_carry_no_v9_record_fields` | plain/text arms, scored and verification episodes: no `origin/kind/parent/...` in any transport record, no `controller.json`, `ledger.jsonl`, `controller.jsonl` or `checks/`, no v9 summary fields, `controller`/`fallback_answer` None, no `submit` wrapper, no `[harness:` text in any request; the mech arms do carry all four harness note kinds |
| `test_learners_admit_or_reject_deterministically` | text-evolve admits the note (support [1, 4] 4/4, controls [2, 5] kept); mech-evolve rejects the rule (`precision: only 3 right episodes (< 10)`), admits the policy knob and the note; each verdict is re-derived from the recorded runs with `learner_common.judge`; the revision is served from block 2 only; the note is served on the one block-2 question its trigger matches |
| `test_model_visible_text_never_names_the_arm` | in every request of every episode of every arm: no arm name, W0 name or learner name; Codex's own `instructions`/`tools` are byte-identical across all arms; no episode was granted anything under its root (the adapter is never passed to the actor); toy06's probe got "Operation not permitted" 5/5 in every arm |
| `test_seed_knowledge_reaches_the_model_as_decided` | text arms' AGENTS.md = core advice + `pack.seed_text()`; mech arms' = the minimal finishing text; plain = COMMON only |
| `test_learner_visible_files_hold_no_unreleased_answer` | no reference of block 2 or of the disputed question occurs in `evidence/`, the learner's session records, or any message sent to either learner model (positive control: the same patterns do find them in the sealed block-2 predictions); only released references appear in `evidence/` |
| `test_key_cost_report_and_timings` | the dummy key is in no record; cost groups per origin (controller calls only on mech arms, verification only on evolve arms, learner calls = scripted model calls, nothing aborted); the report has no question or distinctive answer and no `report-error.json` |
| `test_crash_after_learning_aborts_the_group_and_resumes_block_level` | 2 roots; mech-evolve crashes after its round finished but before adoption → `stopped_on_error`, group ABORT, peer `aborted`; `barrier.clear_abort`, then both resume: the unadopted round is moved to `runs-aborted/`, the learner files restored from `learner-backups/01`, the round re-run → the same decisions and reasons as the first run (determinism across two real runs); both complete; audits pass; cost shows the aborted learner/verification work |
| `test_upstream_errors_and_an_actor_timeout_keep_records_consistent` | 2 roots, 20 s actor clock, through an unreliable upstream: an HTTP 500 and a dropped stream (each retried once with an identical body, recorded as 2 attempts), and a stalled provider (the actor is killed: status `timeout`, empty prediction, no answer fallback; the call is unfinished when audit and cost run, is recorded later as usage-unknown, and is never re-sent); audits pass; grades count 1 empty answer; cost reports unknown usage, never zero |
| `test_learned_rule_is_admitted_served_fired_by_the_gate_and_audited` | 1 root, seed `toy-e2e-rule-0`: a new rule proposed from real broker ledgers passes the counterfactual on the wrong episode and precision on 4 right episodes (0 fires), is served in block 2 and fires *first* in the live gate on toy01 (its own template, offer 252.00, accepted); a priority-only change of `answer_count` is admitted and served; the audit passes, including both rule records and the leak screen (declared rehearsal deviation: `MIN_RIGHT` 10 → 3, `MAX_RIGHT_FIRE` 0.20 → 0.5, see §5) |

Fast tier (default): the toy CLI runs as a kit script; toy wiring and W0 behave like any pack's; each audit
admission mode is checked against its own record (8 positive and negative cases).

## 2. Verification (exact commands, from `officeqa-pro133-v9-plumbing/`)

| command | result |
|---|---|
| `$PY -B -m pytest -q -p no:cacheprovider -m live_codex tests/test_v9_e2e.py` | **12 passed, 3 deselected**, 88 s |
| `V9_LIVE_CODEX=1 $PY -B -m pytest -q -p no:cacheprovider tests/test_v9_e2e.py` (×3) | 15 passed each time: 86 s, 89 s, 89 s |
| `$PY -B -m pytest -q -p no:cacheprovider tests/test_v9_e2e.py` | 3 passed, 12 skipped |
| `$PY -B -m pytest -q -p no:cacheprovider tests` | **329 passed, 61 skipped**, 94 s |
| `V9_SLOW=1 V9_LIVE_CODEX=1 $PY -B -m pytest -q -p no:cacheprovider tests` | **390 passed**, 307 s |
| `$PY -B arms.py check` | text-w0 2,478 B, mech-w0 2,429 B (unchanged) |
| `$PY -B cost.py --self-test` | passed, `paid_model_calls: 0` |

Markers: every live scenario takes less than 60 s on its own (slowest: 34 s), so they are marked `live_codex`
only. None is marked `slow`.

## 3. Integration fixes (each reproduced first, then fixed, then pinned by a test)

| # | file (owner) | defect | fix | pinned by |
|---|---|---|---|---|
| 1 | `packs/toy/tools.py` (S4) | The kit runs pack CLIs as plain scripts (`python -I -B tools.py`). The toy CLI used only a package-relative import (`from .corpus import`), so `lookup` could not run under the kit. | Sibling-import fallback, as `packs/officeqa/tools/cli.py` does. The kit's import closure grants `tools.py` + `corpus.py` only, never `items.py` (the toy references). | `test_toy_lookup_runs_as_a_kit_script` |
| 2 | `packs/toy/pack.py` (S4) | `ToyPack` had no `usage()`, which `contracts.Pack` requires and `campaign.compose_readme` calls for every L1 arm, and no kit wiring. | `usage()` (`USAGE`), `tool_wrappers()`, `TOOLS_SCRIPT`. | `test_toy_pack_is_wired_like_any_pack` |
| 3 | `arms.py` (S6) | `W0_PACKS = ('officeqa',)`: `arms.py build --pack toy` wrote W0s that no campaign could load (`w0_dir` raised). | `W0_PACKS` includes `toy`. `check_adapter` still refuses a W0 built for another pack (tested). | same |
| 4 | `campaign.py` (S6) | The loop was OfficeQA-only. `tool_wrappers` raised for any other pack; the OfficeQA tables index was required by `actor_data` (env + read root), `freeze_record`, `verify_freeze` and `Campaign.load`. A non-OfficeQA root could not be prepared, frozen or loaded. | `tool_wrappers` per pack; `uses_tables_index(pack)` (OfficeQA only) gates the four tables-index uses. OfficeQA behaviour is unchanged. | same, and the whole rehearsal |
| 5 | `full_audit.py` (S6) | `admitted_rules` required counterfactual + precision for every served rule differing from W0. But the mechanism learner also admits priority-only changes (record `{mode: 'priority'}`) and disables (`{mode: 'disable', precision}`). Reproduced live: a campaign whose learner reprioritised `answer_count` completed and then **failed its final audit**. The records were also merged across rounds and compared with W0, so a rule added in round 1 and reprioritised in round 3 would have failed too. (S5 review R2, never applied.) | Per round, against the revision served before it (W0 for round 1), with the record of each mode (`_admission_matches`: full / priority / disable, the disable thresholds read from `mechanism_learning`). Policy values are compared with the previous revision too. Check names are unchanged for round 1. | `test_learned_rule_...` (live), `test_audit_accepts_every_declared_rule_admission_mode`; orchestration audit tests unchanged and passing |
| 6 | `broker.py` (S3) | Since contracts v9.1 the gate answers an unusable payload with status `rejected`. The broker counted `submit_invalid` only for `'findings'` with no finding. Reproduced live: toy07's over-long `--keep` was counted in `submits` but in neither `gate_rounds` nor `submit_invalid`, so the submit counters used by the pilot go/no-go did not add up. (S7 request 1 / S6 request 9, never applied.) | `elif result.status in ('findings', 'rejected')` → `submit_invalid`. | `test_controller_arms_record_...` (`submit_invalid == 1` on toy07); broker tests unchanged and passing |

Everything else behaved as specified on the first run, including:
- seal before release; withheld references;
- barrier ordering and abort propagation;
- the learner abort watcher, block-level resume and learner-file restore;
- the ledger splice after a real Codex compaction;
- the enforcement re-query only on findings;
- note scoping and verification through the actor runner;
- key redaction, cost grouping and the report.

## 4. Timings (main rehearsal on this machine; fake model, so no inference time)

| | value |
|---|---|
| whole repeat group (5 roots, 50 scored + 14 verification episodes, 342 upstream requests) | 18.5-19.5 s |
| per root | plain 16 s, text-static 17.5 s, text-evolve 18 s, mech-static 19 s, mech-evolve 19 s |
| per episode (prepare + real Codex + broker + records) | mean 0.8 s (plain), 1.2 s (text-static), 1.5-1.6 s (mech-static, text-evolve incl. verification), 1.8 s (mech-evolve); max 2.6 s |
| `full_audit` of 5 roots | 0.6 s |
| crash + resume scenario / adversity scenario (incl. a 20 s actor kill) / learned-rule scenario | 20 s / 34 s / 13.5 s |

The harness overhead per episode is about 1-2 s. A real GLM episode takes minutes, so the harness is not a
bottleneck. Freeze re-verification (Codex binary + frozen runtime hashes) runs at every block start and is
included above.

## 5. Deviations (declared)

1. **Hooks.** The rehearsal drives `Campaign` through its test hooks:
   - `episode` passes the fake upstream URL and an unthrottled scheduler to the real broker;
   - `learn` passes the scripted model through `campaign.call_learner`;
   - `scorer` is the toy grader;
   - `verify_data=False`, because `data.verify_prepared` is OfficeQA-only (133 tasks, pinned `reward.py`);
   - `deep_runtime=False`.
   The production entry path (`run.py run/resume --clear-abort`, readiness, root == code with
   `.task-paths.json`) is not driven here; S6's `run.smoke` live test and offline preflight test cover it.
2. **Wall clock.** The actor wall clock is 300 s (20 s in the adversity scenario) instead of 3,600 s. It is
   patched into both `campaign` and `experiment`, so the freeze record and the audit's wall-clock check agree.
3. **Roots and code are separate.** `Layout(root, code)`: the freeze hashes a code copy with toy W0s, while the
   kit runs the real plumbing's `harness_core/` and `packs/` (`paths.PACKS`).
4. **Disputed question.** The toy root declares toy06 withheld by overriding the manifest's
   `withheld_reference_ids`, because OfficeQA's `ISSUE_47` has no toy ids.
5. **Learned-rule scenario only.** `mechanism_learning.MIN_RIGHT` 10 → 3 and `MAX_RIGHT_FIRE` 0.20 → 0.5
   (monkeypatched, test only). With the declared constants no rule can be admitted before ≥ 16 right released
   episodes with 0 fires, which one 5-question round cannot reach. Admission logic, records, serving, gate and
   audit are the real ones.
6. **What the scores mean.** The weak actor is scripted, so per-arm scores are produced by the script: plain
   and text 5/10, mech 9/10. They test the plumbing, not the hypotheses.

## 6. Remaining gaps before a paid smoke

1. **Fireworks itself is untested at $0.** This includes:
   - its real SSE event sequence, reasoning items and usage fields for the actor;
   - the learner transport (`FireworksModel` with `reasoning_effort='high'`, tools and `max_tokens`;
     `rate_control.MAX_ATTEMPTS=5`; the completion-token overrun rule). See S5 review R3.
2. **A stalled provider stream outlives its episode** (request 4). With `request_timeout=None`, the broker's
   handler stays blocked until the provider closes the stream. The episode is correct: timeout, empty
   prediction, no re-send, usage unknown. But the call's `transport.json` / `api/summary.json` are written
   later, possibly after the block was sealed or after the final audit, and a thread and a socket are held
   meanwhile.
3. **Codex tells every model the filesystem is unrestricted.** Every request contains
   `<permission_profile type="disabled"><file_system type="unrestricted" />`, which is false under the Seatbelt
   profile. It is identical in all arms, and README says the truth (request 6).
4. **Verification runs show their role.** Their workspace path reveals `evolution/01/verify/<note id>/qNNN-kK`
   to the model (request 3; symmetric between the evolve arms).
5. **Pack neutrality is partial.** `data.py`, `COMMON`/helper wording and one W0 set per code copy are
   OfficeQA-bound (request 2). This matters for SpreadsheetBench, not for the OfficeQA smoke.
6. **Learned rules come late.** No rule can be admitted before ≥ 16 right released episodes with 0 fires (with
   ~50 % accuracy, not before about round 7 of a 27-round campaign). Until then mech-evolve can change only policy,
   priority, disables (≥ 10 right) and notes. This is a design fact to keep in mind when reading the pilot.
7. **Cost estimate after a transport fault.** Any 5xx, dropped stream or stall makes `estimate_usd` None for
   that group (request 7); read `known_cost_lower_bound_usd`.
8. **Open decisions.** Still pending: the PREREG decisions (temperature pilot, CPI data, repeats, the
   analysed-id sha, the confirmation set), and PROTOCOL.md's audit wording for rule admission modes
   (request 1).

## 7. Files

- **New (S8-owned):**
  - `tests/test_v9_e2e.py`;
  - `tests/e2e/__init__.py`, `tests/e2e/toy_campaign.py`, `tests/e2e/weak_actor.py`, `tests/e2e/learners.py`;
  - this report;
  - `officeqa-v9-design/wave2/S8-e2e-requests.md`.
- **Changed to fix integration defects (§3):**
  - `packs/toy/tools.py`, `packs/toy/pack.py`;
  - `arms.py` (one constant);
  - `campaign.py` (`tool_wrappers`, `uses_tables_index` and its four call sites);
  - `full_audit.py` (`admitted_rules`, `_admission_matches`);
  - `broker.py` (one condition).
- **Scratch** (not deliverables): `scratchpad/probe_toy_episode.py`, the first single-episode probe.
