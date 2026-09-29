# S6-orchestration: arms, campaign loop, W0 adapters, notes, barrier, data, preflight, audit, cost, report (wave 2)

**Status: built and tested. Every test passes.**

| tier | result |
|---|---|
| `tests/test_v9_orchestration.py` (default) | 31 passed, 2 skipped (the 2 opt-in live tests), 23 s |
| `tests/test_v9_orchestration.py -m live_codex` | 2 passed, 4.7 s (real pinned Codex, fake upstream, $0) |
| whole suite, default | 313 passed, 49 skipped (opt-in groups), 87 s |
| whole suite, everything (`V9_SLOW=1 V9_LIVE_CODEX=1`) | **362 passed, 0 skipped**, 209 s |

Rules kept: no paid call, no network except 127.0.0.1, no credentials read, no reference answer or grader
output opened. The only "answers" used are synthetic (`A-<task id>`) in temporary test roots; the pinned
`reward.py` is copied once into a synthetic root for its sha check and never run. Files written: only the
S6-owned files listed in §6, this report and `S6-orchestration-requests.md`.

Paths are relative to `officeqa-pro133-v9-plumbing/`.
`PY=/Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd/adapter-comparison-work/.venv/bin/python`.

**Terms used below.**
- **Root**: one campaign directory = one arm × one repeat (a copy of this plumbing plus `prepared/` data).
- **Repeat group**: the roots of one repeat r (every arm with at least r repeats). They share one question
  order and one barrier directory.
- **Barrier**: a rendezvous. A root may start block b+1 only after every root of its group finished block b.
  Here it is a set of files (`block-NN/<root>.done`) because the roots are separate processes.
- **Seal**: a JSON file of SHA-256 hashes of predictions, written *before* any reference is read, so later
  tampering is detectable. **Release**: copying the references of a sealed block to the learner's evidence.
- **W0**: the unlearned starting adapter of an arm. **Seed parity**: the same seed knowledge is given as text
  (text arms) and as executed rules (mech arms) under the same 16 KB text budget.
- **Block-level resume**: after a crash or ABORT, the unfinished block is moved aside and run again; finished
  blocks and the freeze are reused.

## 1. What was built (by brief item)

### 1. `arms.py` (new): the only place `.task-paths.json` becomes an arm
- `parse_config(config, root_name)` → `CampaignSpec{arm: ArmSpec, repeat, pack, order_seed, root, barrier}`.
  `arm` is a standard name (`STANDARD_ARMS`) or a full spec object with exactly the ArmSpec fields.
  Refused: unknown keys, a non-`v9` profile, an unknown arm name (so `"cards"` or `"mech-evolve-x"` never map
  to anything), a standard name with non-standard fields, a bad repeat/seed/pack, a barrier whose root list
  lacks this root, a relative barrier dir, a non-positive timeout.
- `check_arm` = `ArmSpec.validate()` + orchestration invariants (rules need the controller; the controller
  needs L1; no seed ⇒ no notes/learner; learner arms render notes; standard names cannot be redefined).
- `question_order(ids, order_seed, repeat)` = ascending SHA-256(`seed` NUL `repeat=r` NUL id): same for every arm
  of a repeat, different between repeats. `blocks_of(133, 5)` → 26 × 5 + 3.
- W0: `CORE_ADVICE`, `MECH_SEED`, `w0_files/build_w0/check_w0` (the committed `adapters/*-w0` must equal the
  generated ones), `check_adapter` (architecture, pack, note schema, rule vocabulary, policy ranges, budget),
  `model_visible` (budget accounting, §3), `load_pack` (explicit table, no import by string).
- `python arms.py build|check` regenerates / checks the W0 adapters.
- **No substring inference anywhere**: `experiment.py`, `full_audit.py` and `preflight.py` read the arm only
  through `arms.py`; a test greps the old `'cards' in` / `in PROFILE` patterns out of them.

### 2. `experiment.py` (v9)
`PROFILE='v9'`; `CAMPAIGN`, `ARM` from `.task-paths.json` (None without one); `ARCHITECTURE` =
`arms.architecture(ARM)` (`'none'` without a campaign); `ACTOR_SECONDS=3600`; `BROKER_OPTIONS` = v8 FULL (all
None); `LEARN_EVERY=5`; `LEARN_LAST_BLOCK=False`; `REQUERY_WHEN_CLEAN=False`; `TEMPERATURE=0` (placeholder, the
broker sends 0); kept for acting.py and legacy modules: `FULL=True`, `MWM=False`, `PROJECT_DOC_MAX_BYTES`,
`SHELF=None`, `EXECUTION_IMAGE`.

### 3. W0 adapters (new): `adapters/text-w0/`, `adapters/mech-w0/`
- `text-w0`: `adapter.json {architecture: text, contract: v9.1, pack: officeqa, revision: w0}`; `SEED.md` = core
  advice (loops: "a command repeated with the same output will not change — do something different or
  answer"; compute final numbers with `calc`; provenance; answer count/format; percent vs fraction; weigh
  alternatives) + `OfficeQAPack().seed_text()`; `notes/` empty.
- `mech-w0`: `adapter.json {architecture: mechanism, ...}`; `rules/` = the 5 core seed rules
  (`harness_core.rules.core_seed_rules`) + the 5 pack seed rules (`packs/officeqa/seed_rules.json`);
  `policy.json` = `POLICY_DEFAULTS`; minimal `SEED.md` (finish with `submit`, what findings and offers mean,
  Accept/Keep, `[harness:...]` lines).
- **Seed parity (model-visible bytes, recorded in `freeze.json` and `preflight/offline-checks.json`)**: text
  W0 **2,478 B**; mech W0 **2,429 B** = 829 SEED.md + 1,130 rule templates + 470 controller templates (the gate
  frame, 275 B, recorded, not counted). Budget 16,384 B each.

### 4. `notes_render.py` (new)
`load_notes` (schema, id = file name, triggers without regex, text ≤ 600 B, no control characters),
`choose` (triggers matched with `harness_core.engine.trigger_match` — the rule engine's function; ≤ 3;
order **(admitted_block, id)** as the brief says), `choose_ids` (audit re-derivation), `render_note` (=
S5's), `render_agents`, `compose_agents(common, adapter, question) -> (AGENTS.md, ids, byte counts)`.
AGENTS.md = COMMON + adapter SEED.md + "## Notes for this question" (only when a note is chosen).

### 5. `campaign.py` (v9 loop)
- `Layout(root, code)`: production root == code; tests point `root` at a temp dir.
- `compose_readme(arm, pack, corpus, index)`: `kit.readme_l0` + the v8 search helper (`officeqa.py`, all
  arms) + `pack.task_conventions()` (all arms) + `pack.usage()` and `calc` usage (L1) + `submit` usage
  (controller).
- `tool_wrappers(arm)`: `packs.officeqa.pack.tool_wrappers()` + `harness_core/tools/calc.py` for L1 arms
  (the kit adds `submit` itself). `actor_data(arm)`: `OFFICEQA_CORPUS`, `TABLES_INDEX` and the tables-index
  data root for L1 arms only.
- `prepare_episode`: notes chosen from (adapter, question) **before** the marker; `acting.prepare_actor_workspace
  (ws, None, common_instructions=AGENTS, arm=, tool_wrappers=, marker_extra={role, ordinal, task_id, notes,
  adapter, adapter_revision, readme sha/bytes, agents bytes})`; `officeqa.py`; README; `task.json`.
- `build_controller` (controller arms, S3/S4/S7 wiring): one `OfficeQAPack(corpus, tables_index)` for
  `Ledger(pack, api/ledger.jsonl)` and `Gate(pack, rules, policy, make_runner(frozen python, corpus),
  question, api_dir=api)`; `MechController(ledger, gate, policy, time.monotonic,
  requery_when_clean=False)`.
- `execute_episode`: `Broker(key, api, **BROKER_OPTIONS, controller=, actor_deadline=monotonic()+3600)` →
  `acting.run_actor(..., arm=, env_extra=, data=, permissions_instructions=False)`; prediction = `final.md` of
  a completed run; `fallback_answer` = last `Candidate(via='submit')` in the ledger (diagnostic only,
  `fallback_differs`); summary gains arm, role, notes served, adapter revision, kit intact, README bytes,
  `out_tokens`, the broker's controller counters.
- `freeze_record` / `verify_freeze` (every block and at the end): top-level `*.py`, PROTOCOL.md, PREREG.md,
  `.task-paths.json`; trees `harness_core/`, `packs/`, `adapters/`; `officeqa_tools.py`; vendor `reward.py` sha;
  Codex sha; frozen runtime (deep at freeze, shallow after); prepared manifest; tables index; this repeat's
  order; W0 snapshot + model-visible bytes; PREREG sha; all v9 settings (acting `V9_SETTINGS`,
  `V9_DECLARED_SETTINGS`, permissions off, temperature, re-query rule, wall clock, broker options, policy
  defaults, budget).
- `Campaign.run()`: load (tasks, order, freeze or verified freeze, `order.json` registration), per block:
  barrier `pass_block(b-1)` (block 0 = ready), freeze + seal checks, 5 episodes one at a time (checking stop
  and ABORT before each), then for learner arms (not after the last block) `release_block` → backup of the
  learner's durable files → `learn` → adopt the checked candidate; block `done`; `arrive(b)`. Final: seal all
  predictions, `data.grade` (strict + 1%, all + undisputed), `completed`.
- `release_block`: seal first (`private-learning-seals/BB.json`: episodes + `withheld_episodes`), then
  `data.release_references` for the **non-withheld** ids only, `evidence/BB/<NNN>/{task, prediction, summary,
  failure, trajectory, reference_answer}.json`, manifest in the format S5's `evidence.validate_block` checks
  (`includes_gold: true`, the four `includes_*: false`, `batch_seal`, per-episode `run_dir`) plus
  `withheld: [{id, ordinal}]`. Issue-47 references are never read into evidence.
- `verification_runner` (S5's `ActorRunner(adapter, task, run_dir)`): the scored episode path, in
  `evolution/BB/verify/...`; refuses unreleased questions, a changed question text, a run dir outside
  `verify/`, a reused run dir, an adapter that is neither the source nor inside the round.
- `learn`: calls `text_learning`/`mechanism_learning.run_learning` with the keywords of S5's signature
  (`call_learner` filters by `inspect.signature`; a required parameter we do not offer is an error).
  `status == 'failed'`, a candidate outside the round, `activated` disagreeing with the candidate, or a
  candidate failing `arms.check_adapter` raises `LearnerFailed` → the campaign stops and writes ABORT.
- Errors: a local stop (signal) or an error writes ABORT for the group; an ABORT seen from another root sets
  the local stop event (so a running learner/verification pool stops) and ends the root as `aborted`.
- **Block-level resume** (`run(resume=True)`): reuses and verifies the freeze; the first block not `done` is
  moved to `runs-aborted/<stamp>/` — only `evidence/BB`, `evolution/BB`, the seal when all its episodes are
  complete and unchanged, otherwise also its run dirs; the learner files are restored from
  `learner-backups/BB/`; the served adapter is reset to the one recorded at the block's start.

### 6. `data.py` (v9)
Manifest v9 (`manifest_v9`): protocol `officeqa-pro133-v9-<arm>-r<repeat>`, `arm_spec`, architecture, repeat,
pack, order seed, learning type, `learner_policy.withheld_reference_ids` (issue-47 ids present), grading
subsets, tables-index facts, actor read list. Tables index: `install_tables_index` (copy +
`corpus_manifest_sha256` must equal this corpus's hash computed like `otables.build_index`),
`verify_tables_index`. `release_references(root, ids)` (None for withheld ids), `official_scorer` (sha-checked
`reward.py`), `seal_predictions(root, arm, path)` and `grade(root, ..., scorer=None, order=)` for single-arm
roots: `<arm>-grades.json` and `paired-metrics.json` with all-133 and undisputed-115 summaries and per-item
scores with their position in this repeat's order. `load_tasks` takes the count from the manifest (synthetic
roots can be small); `verify_prepared` also checks the arm/repeat/seed and the withheld list.

### 7. `preflight.py`, `full_audit.py`, `cost.py`, `report.py`, `run.py`
- **preflight** (`--offline-only` = $0; `--no-suites` for debugging): arm valid; data + tables index vs corpus
  manifest; 133 tasks; base order vs the v8 manifest (read-only, when present); this repeat's order registered
  in the group's `order.json`; `kit.verify_frozen(deep=True)`; seed parity of both W0s; W0 check; a real
  workspace for this root's first question + `acting.build_settings` + `validate_config_offline` (strict
  config read, no model call) + v9 settings; suites kit / pack / core / broker / learners (S5's
  `tests/test_v9_learn*.py` etc. = the fake-model learner smoke) / orchestration / `-m live_codex` ($0);
  PREREG present, hash and pending decisions recorded; frameworks are the private copies. Then the paid smoke
  and `readiness.json` (source hashes incl. PREREG, order sha, seed parity).
- **full_audit** (checks listed in its docstring and PROTOCOL.md §3): freeze; completion in order; per
  episode (scored and every verification run at any depth) `kit_after.intact`, `invocation.arm == root arm`,
  frozen runtime equality, v9 settings, wall clock, retries, AGENTS.md size, prediction hash, notes ≤ 3 and
  re-derived, adapter revision of the block; request records (model, reasoning forwarded, no caps, upstream);
  controller=None arms without any v9 record field or controller file; controller arms with
  origin/kind/`codex_input_sha256`, parent + copied `native_request_settings` for controller calls, ledger,
  v9 summary fields; learner rounds present and not failed, seal before release, exact scope, withheld
  references never released, served adapters within budget, every served rule/policy that differs from W0
  admitted with replay numbers (S5's `mechanical-admission.json` names); static arms without seals/evidence/
  evolution; barrier arrivals and start(b+1) ≥ max end(b) across the group.
- **cost**: groups `actors_codex`, `actors_controller` (by `transport.json` origin; a missing origin =
  codex), `learners` (any `fireworks-calls/` under `evolution/`), `verification_codex/controller`,
  `aborted_actors/learners` (`runs-aborted/`), `synthetic_preflight`; `actor_calls_by_origin`.
  `python cost.py --self-test` passes (13 checks).
- **report**: answer-free HTML for one arm root (completion, statuses, grades when sealed, controller
  counters, learning rounds, warnings; light/dark tokens).
- **run**: `smoke` (paid; per arm of this root, or `--arms`; controller arms must submit the marker; takes
  `base`/`broker_extra` so tests run it at $0), `run` (needs readiness + unchanged sources + PREREG hash),
  `resume [--clear-abort]`, `audit`, `cost`, `report`; SIGINT/SIGTERM set the stop event.

### 8. `PROTOCOL.md` (rewritten for v9) and `PREREG.md` (new)
- PROTOCOL: arms table, `.task-paths.json` keys, order, every declared change vs v8 (L0 list, L1, controller,
  submit/rules, enforcement decision, W0/seed parity numbers, notes, learners, withheld references),
  orchestration/records/audit/cost, metrics, outputs, root layout, recommended barrier timeout.
- PREREG: H1 → H2 → H3 gatekeeping at α = 0.05; GLMM `y ~ arm + (1|item) + (1|campaign)`; sign-flip,
  campaign-level and two-way bootstrap robustness; MDE ≈ 10 questions (3 vs 3); TOST ±5 questions; issue-47
  handling (primary all 133, same sign on 115, references withheld); repeats and repeat groups; randomised
  order per repeat; pilot go/no-go (DESIGN §5 + gate limits); fallback wording if H2 fails; development vs
  confirmation; placeholders `[DECISION PENDING ...]` for temperature, CPI, repeats, the analysed-id list
  sha, the confirmation set.

### 9. `tests/test_v9_orchestration.py` (new, 33 tests)
Arms parsing and refusals (19 bad configs); experiment constants; order per repeat and blocks; barrier
(3 threads × 3 blocks ordering, timeout → ABORT → others stop → `clear_abort`, stop event, order
registration); notes (≤ 3, order, contiguous triggers, rendering, refusals); committed W0 = generated, seed
content, budgets, **`arms.model_visible` == S5 `text_budget`**; `check_adapter` refusals; README/AGENTS/kit per
standard arm; an episode through the **real broker + real Ledger/Gate/MechController** driven by `CodexSim`
through the fake upstream (plain and mech-static) and the audit's record checks on it; campaign loops with a
fake broker/actor: text-evolve (seal → release → learn → serve → grade → audit), two roots concurrently behind
one barrier, crash → ABORT → resume, failed learner round → resume (learner files restored), stop event;
**the real S5 `text_learning` and `mechanism_learning` rounds (S5's scripted model) through the campaign
loop** (release passes S5's validator, S5's verification re-runs go through the campaign's runner, the note is
admitted, served and audited); verification-runner refusals; `call_learner` signature filtering; audit
failure detection (kit, arm, v9 field, notes, prediction, unadmitted rule, barrier order, static arm with
evidence); tables index bound to the corpus; freeze detects changes; cost grouping; report without answers;
`preflight.py --offline-only --no-suites` on a synthetic 133-task root (subprocess, real Codex config read);
smoke fixture per arm. Opt-in `live_codex`: a composed mech episode under the real Codex (README visible,
`find_tables` and `submit` work from the kit, the gate answers), and `run.smoke` at $0 for plain + mech-static.

## 2. Verification (exact commands, from `officeqa-pro133-v9-plumbing/`)

| command | result |
|---|---|
| `$PY -B -m pytest -q -p no:cacheprovider tests/test_v9_orchestration.py` | 31 passed, 2 skipped, 23.1 s |
| `$PY -B -m pytest -q -p no:cacheprovider -m live_codex tests/test_v9_orchestration.py` | 2 passed, 4.7 s |
| `$PY -B -m pytest -q -p no:cacheprovider tests` | 313 passed, 49 skipped, 87 s |
| `V9_SLOW=1 V9_LIVE_CODEX=1 $PY -B -m pytest -q -p no:cacheprovider tests` | **362 passed**, 209 s |
| `$PY -B arms.py check` | both W0 equal to the generated ones; text 2,478 B, mech 2,429 B, within 16,384 |
| `$PY -B cost.py --self-test` | passed, 13 checks, 0 paid calls |
| `$PY -B -m pytest ... tests/test_v9_orchestration.py --durations=8` | slowest test 2.7 s (none near 60 s, so none is marked `slow`) |

The paid paths (`run.py smoke` against Fireworks, `run.py run/resume`, `preflight.py` without
`--offline-only`) were **not** run. Their code paths ran at $0: `run.smoke` under the real Codex against a fake
upstream, `preflight.offline` on a synthetic 133-task root, and the campaign loop with fake and real
(scripted-model) learners.

## 3. Deviations from the brief and from contracts v9.1

1. **Withheld references are not written at all** (not written-then-excluded): issue-47 episodes are sealed
   under `withheld_episodes` and listed under the evidence manifest's `withheld`, without a directory. S5's
   builder works unchanged (its `exclude` becomes a no-op); requests 1 ask S5 to count them in `excluded`.
2. **Grades are not in evidence** (`includes_grades: false`, as S5's validator demands): S5 grades released
   episodes itself with the `evidence.Grader` the campaign passes (the sha-checked `reward.py`).
3. **Budget formula = S5's** (all notes rendered + SEED.md + rule templates + controller loop/enforce
   templates); the gate frame is recorded, not counted. Contracts' comment differs (request 10).
4. **Served-note order is (admitted_block, id)** as the brief says; S5 ranks differently (request 2).
5. `data.load_tasks` takes the task count from the manifest instead of the constant 133 (synthetic roots);
   `verify_prepared` still requires 133 and the v3-v8 selection seed.
6. `ARCHITECTURE` is `'none'` (not `None`) without a campaign, and `campaign.W0` exists (v8 names), so the
   legacy v8 modules still import; `cards_learning.py` cannot (it needs the v8 framework tree).
7. A local stop (Ctrl-C) also writes the group ABORT: the group cannot advance without this root.
8. The preflight offline test starts `codex app-server` for a config read (no model endpoint) without the
   `live_codex` marker, following S7's precedent for `test_validate_config_offline`.

## 4. Gaps and risks

1. **Not run for real**: the paid smoke, preflight's paid part, a real campaign, `data.prepare` (reads the
   official download incl. answers; controller-side, for the operator), the final grade with the real
   `reward.py` (tests inject a scorer).
2. **PREREG decisions are open** (5 `[DECISION PENDING ...]` lines). `run.py` requires the PREREG hash in
   readiness, not that the list is empty; the operator must resolve them before confirmatory campaigns.
3. **Note order disagreement with S5** (request 2) can make S5's screening mispredict which notes are served.
4. **Freeze cost**: every block re-hashes the Codex binary (234 MB) and the tables index (147 MB), ≈ 1 s.
5. **Barrier is advisory between processes on one machine** (files + wall clock). A root that is killed with
   `SIGKILL` writes no ABORT; the others wait `timeout_s` (recommended 6 h) and then abort.
6. **Resume granularity**: a crashed block's partial episodes are re-run (paid again; counted under
   `aborted_actors`); a crashed learning round is re-run from its backed-up state.
7. **Roots must sit next to `frameworks-v8/`/`frameworks-v9/`** or set `codex`/`frozen_runtime` in
   `.task-paths.json` (found by the preflight test).
8. The audit trusts `state.json` start times and the barrier files' `end_unix` (same host clock).

## 5. Requests

`officeqa-v9-design/wave2/S6-orchestration-requests.md`: S5 items 1–8 (withheld count and empty blocks, note
order, budget formula, durable files, the exact keywords passed, runner behaviour, admission field names,
learner test names), broker item 9, contracts items 10–11, user decisions 12–14.

## 6. Files (all S6-owned)

New: `arms.py`, `notes_render.py`, `barrier.py`, `PREREG.md`, `adapters/text-w0/{adapter.json, SEED.md,
notes/.keep}`, `adapters/mech-w0/{adapter.json, SEED.md, policy.json, notes/.keep, rules/*.json (10)}`,
`tests/test_v9_orchestration.py`. Rewritten: `experiment.py`, `campaign.py`, `data.py`, `preflight.py`,
`full_audit.py`, `cost.py`, `report.py`, `run.py`, `PROTOCOL.md`.
