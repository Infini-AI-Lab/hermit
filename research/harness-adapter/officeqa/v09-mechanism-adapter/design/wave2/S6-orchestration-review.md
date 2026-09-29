# S6-orchestration: adversarial review (wave 2)

**Verdict:** The stream is mostly sound, and the implementer's test claims hold. I found 9 defects and fixed them
in S6-owned files, with a regression test for each. The most important was arm identity in model-visible
text. Four defects were fixed partially or are handled by requests to other owners. The whole suite passes.

Rules kept:
- No paid call. No network except 127.0.0.1. No credentials read.
- No reference answer or grader output opened. The only "answers" used are the synthetic `A-<id>` ones in
  temporary test roots.
- One read-only look at v8 model traffic: `officeqa-pro133-cards-v8-work/runs/adapted/001/api/001/request.json`,
  to confirm what Codex sends to the model.
- Files written: S6-owned files, this report, and `S6-orchestration-review-requests.md`.

`PY=/Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd/adapter-comparison-work/.venv/bin/python`.
Commands run from `officeqa-pro133-v9-plumbing/`.

**Terms used below.**
- **Environment context**: the block Codex adds to the model input on every request. It includes `<cwd>`,
  the absolute path of the workspace. Anything in that path is model-visible text.
- **Leak screen**: `pack.leak_screen(text, released_answers)`, the OfficeQA check that learned text contains no
  released reference value.
- **Question-copy check**: `learner_common.shared_run`, which flags an 8-word run copied from a released
  question.
- **Readiness**: `preflight/readiness.json`. `run.py` requires it before a paid formal run.

## 1. Verification

| command | before review | after review |
|---|---|---|
| `V9_SLOW=1 V9_LIVE_CODEX=1 $PY -B -m pytest -q -p no:cacheprovider tests` | 362 passed, 209 s | **375 passed, 0 failed**, 217 s |
| `$PY -B -m pytest -q -p no:cacheprovider tests/test_v9_orchestration.py` | 31 passed, 2 skipped | **38 passed, 2 skipped**, 30 s (slowest test 2.9 s) |
| `$PY -B -m pytest -q -p no:cacheprovider -m live_codex tests/test_v9_orchestration.py` | 2 passed | **2 passed**, 4.4 s |
| `$PY -B arms.py check` | text 2,478 B, mech 2,429 B | unchanged |
| `$PY -B cost.py --self-test` | passed | passed, 0 paid calls |

- The skips are the two opt-in live-Codex tests.
- No new test is near 60 s, so none needed the `slow` marker.
- Of the 13 extra tests in the full-suite run, 7 are mine. The other 6 came from S5/S7 streams working
  in parallel.
- The implementer's claims hold:
  - no substring inference of the arm (grep over `experiment/full_audit/preflight/campaign/arms/data/cost/report/run.py`);
  - W0 generated equals W0 committed;
  - the seed-parity byte counts are correct;
  - the real S5 learners run end to end through the loop;
  - issue-47 references are never written into evidence.

## 2. Defects found and fixed (all in S6-owned files)

| # | severity | defect | fix | test |
|---|---|---|---|---|
| 1 | **high (validity)** | **Arm identity reached the model.** Scored episodes ran in `<root>/runs/<arm name>/NNN/workspace`, e.g. `runs/mech-evolve/001`. Codex sends that path as `<cwd>` on every request; v8 traffic confirms it. README.md also prints the corpus path under the root, so the root name is model-visible too. v8 roots were named after their arm (`officeqa-pro133-cards-v8-work`), and the draft PROTOCOL/arms examples used `plain-r2` and similar. | `Layout.runs()` is now arm-neutral: `runs/episodes/NNN` for every arm (`campaign.RUNS_DIR`). `arms.parse_config` refuses a root name containing an arm-revealing fragment (`arms.ARM_REVEALING`, the token `none`, or the arm name). The audit checks that the recorded scored workspace path (below the root's parent) names no arm. Preflight adds `root_name_reveals_no_arm`. PROTOCOL §3/§5 updated. | `test_root_names_and_run_paths_do_not_reveal_the_arm` |
| 2 | medium (fairness) | **Mechanism admission replay could not do what the live gate does.** The live gate runs offers in `make_runner(ACTOR_PYTHON, extra_read=[corpus])`. S5's admission replay defaulted to `make_runner()`, which has no corpus read. A calc that reads a corpus file works live but fails in replay, so a correct rule is not admitted. This handicapped mech-evolve only. | `Campaign.learn` passes `runner=` (the gate's runner, via `Campaign.compute_runner`) to both learners. | `test_learners_get_the_gate_compute_runner` |
| 3 | medium (leakage hygiene) | **Verification could run on withheld questions.** The verification runner allowed every question of blocks 1..b, including the issue-47 questions. Their references are never released, so a learner could spend verification actors on them. | Withheld ids are removed from the allowed set. | `test_verification_runner_refuses_withheld_questions` |
| 4 | medium (audit hole) | **No check that notes were verified.** The audit checked that served *rules* were admitted, but not that served *notes* were. DESIGN §1 requires paired re-run verification of notes in both evolve arms. | `full_audit.learned_text`: every learned note served needs an admitting row in `learning-ledger.jsonl` (`kind=note, action=add, admitted=true, t<=round`). Both evolve arms are held to the same check. | `test_audit_requires_note_admission_and_a_clean_leak_screen` |
| 5 | medium (audit hole) | **No independent leak check.** Nothing re-checked learned text for leaked answers. This matters because verification re-runs *released* questions: a note carrying a released answer passes verification trivially. | The audit re-runs `pack.leak_screen` and the question-copy check on every learned note and every rule template changed from W0. It checks against the references and questions released up to that round. | same test |
| 6 | medium (audit hole) | **Workspace text was not re-derived.** Per-episode README/AGENTS.md were not re-derived, and verification episodes were not re-derived at all. | For scored and verification episodes, the audit now checks four things. AGENTS.md must equal exactly `compose_agents(COMMON, served adapter, question)` and match the marker sha. README must have the tools section iff `l1_tools` and the submit section iff `controller`. The README sha must equal the marker's. Verification notes are re-derived from the marker's adapter and `task.json`. | `test_root_names_and_run_paths_...` (tampered AGENTS/README) |
| 7 | medium (silent failure) | **The recorded temperature could be wrong.** `experiment.TEMPERATURE` was only a label, while the broker hard-codes `temperature=0`. Once the user decides the pilot, editing `TEMPERATURE` alone would make `freeze.json` claim a value that is never sent. | `campaign.broker_temperature()` reads what `broker.normalized` sends. It is recorded in `settings_record`, and `Campaign.load` refuses a mismatch. Preflight adds `temperature_sent_equals_declared`, and the audit checks every `request.json`. | `test_declared_temperature_must_be_the_one_the_broker_sends`, audit part of #1's test |
| 8 | medium | **Readiness missed the code trees.** Readiness hashed only top-level files. A change to `harness_core/`, `packs/` or `adapters/` between preflight and `run.py run` went unnoticed, because the freeze is taken at run start. | `preflight.source_trees()` goes into readiness, and `run.require_readiness` refuses on a difference. | `test_readiness_covers_the_code_trees` |
| 9 | low-medium | **A group ABORT did not stop a running learner round.** The round noticed the ABORT only when its next verification actor started. Learner model calls (and a whole session without verification) kept running and paying. | `Campaign.learn` runs a watcher thread. It polls the barrier at `poll_s` and sets the learner's stop event, and the round then ends as `BarrierAborted` (state `aborted`), not as `LearnerFailed`. | `test_group_abort_stops_a_running_learner_round` |

Also fixed:
- The PROTOCOL.md barrier timeout recommendation is now 43,200 s (it was 21,600). A fast root waits through
  the slowest root's whole block: up to 5 × 3,600 s of actors plus a learning round. That round can run up to
  28 verification actors, 8 at a time, which is about 4 h in the worst case.
- The arms.py docstring example now uses neutral root names.

Test-file edits:
- `runs/<arm>` paths → `runs/episodes`.
- Root names `plain-r1` / `mech-static-r1` → `g1-a` / `g1-b`.
- The fake broker records `temperature: 0`.
- The fake learner accepts `runner=`, can write the S5 note-admission ledger row, and can take a custom note
  text.

## 3. Checked and found sound (no change)

- **Arms and order.**
  - `parse_config` refuses unknown keys, names, fields and inconsistent specs.
  - `question_order` is identical across the arms of a repeat and different across repeats.
  - The barrier's `order.json` enforces the order within a group.
- **Barrier.**
  - Done files are written atomically.
  - ABORT is exclusive: the first writer wins.
  - `arrive` is idempotent, so a root that crashed between "block done" and "arrive" re-arrives through
    `pass_block`.
  - The final `arrive` is a no-op.
- **Seal and release.**
  - The seal is written before any reference is read.
  - Withheld ids are sealed under `withheld_episodes` and never read from the key: `release_references` is
    called only for non-withheld ids, and a `None` value raises.
  - Predictions are re-verified at every block start and before grading.
- **Composition.** Every arm's README has the same L0 text, helper and conventions. Tools appear iff L1,
  submit iff controller. No arm name appears in README or AGENTS (checked by rendering all 5 arms).
  MECH_SEED matches the gate's actual wording (`recorded`, `Offer:`, `Accept:`, `Keep:`).
- **Seed parity.** Text 2,478 B vs mech 2,429 B, both computed with S5's `text_budget` formula (a test pins
  it). The text seed maps one-to-one to the rules and the loop guard.
- **Learner inputs.** Both learners get the same keywords, the same evidence builder and the same withheld
  list, and now the same compute runner. `call_learner` fails loudly on a required parameter it does not
  offer.
- **Grading.** Strict and 1% scores, on all items and on the undisputed subset. Per-item order position is
  recorded for pairing. The injected scorer is used only in tests: production (`verify_data=True`) always
  uses the sha-checked `reward.py` after a full data re-verification.

## 4. Remaining gaps (not fixed; owners in the requests file)

1. **Verification run paths leak the episode's role and the note being tested** (S5 request 1). Example:
   `evolution/01/verify/percent_share/q003-k1/workspace`. This is symmetric for both evolve arms, but it makes
   verification episodes differ from the scored episodes they are meant to predict. The audit's arm-name path
   check covers scored episodes only for this reason.
2. **PREREG pending decisions do not block `run.py run`.** There are 5 `[DECISION PENDING …]` lines. This was
   left as the brief asked, and they are recorded in readiness.
3. **No barrier heartbeat.** A root killed with SIGKILL writes no ABORT, so the others idle until `timeout_s`.
   That costs wall time only, no paid calls.
4. **Grading crash window.** A crash between writing `<arm>-grades.json` and `paired-metrics.json` leaves a
   root that `grade` refuses to regrade ("Grade outputs already exist"). Recovery is a manual file move.
   This is rare, since both files are written in one call.
5. **Order seed across repeat groups.** Each repeat group checks its own order in its own barrier directory.
   Nothing checks that every group uses the PREREG `order_seed` value. A per-group typo would still give a
   valid permutation, but not the pre-registered one. Preflight could compare `spec.order_seed` with the
   value in PREREG §2.
6. **Minor.** The `OfficeQAPack` built for each learning round (it holds a sqlite handle on the tables index)
   is never closed.
7. **Records differ by arm, by design.** Mech-arm evidence summaries carry controller counters and
   `fallback_answer`; text arms have none. This follows from the arms' records, not from the evidence builder.

## 5. Files changed (all S6-owned)

- `arms.py`: `ARM_REVEALING`, `revealing_fragments`, the root-name refusal in `parse_config`, the docstring
  example.
- `campaign.py`:
  - `RUNS_DIR` and the arm-neutral `Layout.runs`;
  - withheld exclusion in `verification_runner`;
  - `compute_runner` passed as `runner=`;
  - `broker_temperature` recorded in `settings_record` and checked in `load`;
  - the abort watcher and abort mapping in `learn`.
- `full_audit.py`: the composition, path, temperature, note-admission and leak-screen checks; the
  `runs/episodes` path.
- `preflight.py`: `source_trees` in readiness; the `temperature_sent_equals_declared` and
  `root_name_reveals_no_arm` checks.
- `run.py`: `require_readiness` checks the code trees.
- `PROTOCOL.md`: the arm-neutral paths and root naming, the temperature guard, the learner runner and abort
  watcher, the audit additions, the barrier timeout.
- `tests/test_v9_orchestration.py`: 7 new tests and the fixture updates listed above.
