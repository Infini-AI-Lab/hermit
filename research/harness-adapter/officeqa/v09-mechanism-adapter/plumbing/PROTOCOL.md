# OfficeQA Pro 133 — v9 protocol (mechanism harness adapter vs prompt adapter)

Design: `officeqa-v9-design/DESIGN.md` (draft 2). Pre-registered hypotheses, analysis and decision rules:
`PREREG.md` (hashed by `preflight.py` into `preflight/readiness.json` and by the campaign into
`freeze.json` before the first paid call). Shared seams: `harness_core/contracts.py` (v9.1).

Each campaign root runs **one arm for one repeat** over all 133 OfficeQA Pro questions with the same pinned
dataset, parsed TXT corpus, native Codex CLI (`frameworks-v8/codex-0.155.0-alpha.9.2`) and the exact
Fireworks model `accounts/fireworks/models/glm-5p3-flash`. The roots of one repeat run the same question
order and advance block by block (5 questions per block; 133 = 26 × 5 + 3).

## 1. Arms (`.task-paths.json` "arm", parsed once by `arms.py`)

| arm | L0 env | L1 pack tools | seed knowledge | L2–L4 controller | learner | repeats |
|---|---|---|---|---|---|---|
| `plain` | ✓ | – | – | – | – | 2 |
| `text-static` | ✓ | ✓ | text: `adapters/text-w0/SEED.md` | – | – | 1 |
| `text-evolve` | ✓ | ✓ | text + learned scoped notes | – | text | 3 |
| `mech-static` | ✓ | ✓ | executed rules `adapters/mech-w0/rules/` + minimal SEED.md | ✓ | – | 3 |
| `mech-evolve` | ✓ | ✓ | executed rules + learned rules/policy/notes | ✓ | mechanism | 3 |

- The arm is `contracts.ArmSpec{name, pack, l1_tools, controller, seed, learner, notes}` given by a standard
  name or a full spec; `arms.check_arm` adds: rules need the controller, the controller needs L1, an arm
  without seed has no adapter/notes/learner, learner arms render notes, a standard name cannot be redefined.
  No module infers an architecture from a profile or directory name (v8 turned unknown names into MWM).
- `.task-paths.json` keys: `profile: "v9"`, `arm`, `repeat`, `pack` (`officeqa`), `order_seed` (one value for
  the whole study), `barrier: {dir, roots, timeout_s[, poll_s]}` (the roots of this repeat group), and the
  `paths.py` overrides (`codex`, `frozen_runtime`, `actor_python_prefix`, `actor_rg`, `data_cache`,
  `framework`, `tables_index`). Any other key is refused.
- Wave 3 (pilot), all optional; without them a root is exactly a full-run root:
  - `subset: {name, task_ids, sha256}`: the root runs only these questions. `sha256` =
    `arms.subset_sha256(task_ids)`, the sha256 of the JSON list as declared.
    - The prepared data are still all 133 questions, with every hash and the base order unchanged. The
      manifest records the subset.
    - `data.verify_prepared` / `check_subset`, the campaign, `seal_predictions`, `grade`, `preflight.py` and
      `full_audit.py` each check it.
    - Order: the subset in this repeat's seeded order, i.e. the full order with the other questions removed.
      Blocks of 5, with learning as in a full run.
  - `temperature`: the actor temperature, a number in [0, 2]; default 0, the v8 value (§2 L0 item 8).
  - `top_p`: the actor top_p, a number in (0, 1]; absent = no top_p is sent (the v8 body) (§2 L0 item 8).
- Question order: `arms.question_order` = ascending SHA-256(`order_seed` NUL `repeat=r` NUL task_id) over the
  base order. Identical for every arm of repeat r (the barrier group checks it in `order.json`), different
  between repeats.

## 2. Declared changes against v8

### L0 environment — identical for every arm (DESIGN §2 L0; S1, S7)

1. **Frozen actor runtime** (`frameworks-v9/`): a hashed, read-only copy of the Codex runtime Python
   3.12.14 (numpy 2.3.5, pandas 2.2.3, sqlite 3.53.1 with FTS5) and of ChatGPT.app's rg 15.2.0. Verified
   deep at freeze and shallow before every episode; every `invocation.json` records it.
2. **Kit** `<ws>/.harness/` (write-protected): `bin/python`, `bin/python3`, `bin/rg`; `zdot/.zprofile`
   restoring PATH after `path_helper`; env adds `TMPPREFIX` (zsh here-documents) and `ZDOTDIR`; PATH is
   `<ws>/.harness/bin:/usr/bin:/bin:/usr/sbin:/sbin` (no controller venv, no cua_node).
3. **Narrowed read roots**: frozen runtime, Codex binary, frozen rg, corpus, FTS index, the tables index
   (L1 arms) and exactly the kit's code files (literal grants); the venv, `harnessadapter` and `cua_node`
   are gone. The workspace marker (arm, note ids) is unreadable to the actor. Network: the broker port only.
4. **Codex settings** (`acting.V9_SETTINGS`, `V9_DECLARED_SETTINGS`): `features.multi_agent=false` and
   `agents.enabled=false` (v8: true; sub-agent traffic would bypass the ledger), explicit
   `model_auto_compact_token_limit=244800` (v8 configured 800,000, which Codex ignored; 244,800 was the
   effective value), `include_permissions_instructions=false` (removes only the false "no sandbox, network
   enabled" developer text; verified at $0 with the real binary). All other settings as v8.
   **Known model-visible inaccuracy (declared, S8):** Codex's environment context in every request still
   says `<permission_profile type="disabled"><file_system type="unrestricted" />`, although the Seatbelt
   profile denies everything outside the workspace and the granted roots (the rehearsal's probe gets
   "Operation not permitted" 5/5). `include_permissions_instructions=false` does not remove this line. It is
   byte-identical in every arm, and README.md states the real rule.
5. **README** for every arm: the L0 environment text (`kit.readme_l0`), the v8 search helper `officeqa.py`
   (unchanged tool), and the published OfficeQA task conventions ("maximum precision unless specified";
   "most up-to-date revision unless the question pins a date") — task specification, not key-derived.
6. **AGENTS.md** starts with the v8 COMMON text in every arm; the prompt is the v8 prompt.
7. **No wrap-up and no answer fallback** in the official metric; the last submitted candidate is recorded
   as `fallback_answer` (controller arms, diagnostic only).
8. **Temperature and top_p**: the broker option `temperature` (default 0, as v8) is sent in every upstream
   body, controller re-queries included, until the pilot decision in PREREG.md. The broker option `top_p`
   (default: absent, so the body carries no top_p, as v8) is sent in every upstream body when declared.
   Both apply to actor episodes (scored and verification); the learners' own model calls (`client.py`)
   keep their own settings.
   - The campaign passes the spec's `.task-paths.json` "temperature" and "top_p" to every episode's broker
     (scored and verification).
   - It refuses to start when the broker would send anything else.
   - `freeze.json` (spec + settings `temperature` / `broker_temperature`, `top_p` / `broker_top_p`, null =
     not sent), `invocation.json` `temperature` / `top_p` and `transport.json` `changes.temperature` /
     `changes.top_p` (only when sent) record them.
   - The audit checks the freeze, every invocation and every request (an undeclared top_p must be absent
     from every body).
9. Unchanged from v8: 3,600 s wall clock per question, unbounded transport with accounted retries
   (`BROKER_OPTIONS`), reasoning effort `high` forwarded, 4 MiB project doc limit, one actor at a time.

### L1 pack tools — every arm except `plain`

`find_tables`, `show_table` (pages ≤ 8,000 B), `printings` (`packs/officeqa/tools/cli.py`) and `calc`
(`harness_core/tools/calc.py`, prints its code with its result) in `.harness/bin`, their README usage, the
tables index as a read-only data root, `OFFICEQA_CORPUS` / `TABLES_INDEX` in the environment.

### L2–L4 controller — `mech-static`, `mech-evolve` (DESIGN §2; S3, S4, S7)

- The broker builds the **ledger** from the items it relays (what the model saw), never from actor files;
  a frozen `[harness:ledger]` splice follows each compaction summary (≤ 8 KB).
- **Loop guard**: an identical normalised command issued a 3rd time gets one fixed-form note (command,
  "output identical", candidates, options); escalation to "submit now" only when a candidate was computed
  after the last new evidence.
- **Commit gate**: `submit --answer "<a>" [--alt] [--reading] [--keep]` POSTs to `/v1/submit` on the actor's
  broker port; the gate evaluates the adapter's rules (declarative, fixed vocabulary) and prints one finding
  per round with an offer computed by re-running the model's own calc in the sandboxed runner
  (`Accept:` / `Keep:`). Check inputs are saved to `api/checks/NNN.json`.
- **Enforcement re-query** (decided): only when the final answer was never submitted (or differs
  numerically from the last submission) **and the gate has findings** (`requery_when_clean=False`); at most
  once per episode; skipped with < 60 s left; bounded by the actor deadline − 15 s.
- Seed rules: core `answer_count`, `scale`, `competing_candidates`, `grounded`, `recompute_final`; pack
  `period_match`, `later_printing`, `flagged_cells`, `table_alternatives`, `row_coverage`; policy =
  `contracts.POLICY_DEFAULTS`. Controller=None arms keep the v8 relay byte for byte.

### L5 notes and W0 adapters (seed parity; DESIGN §1)

- `adapters/text-w0`: `SEED.md` = the core advice (loops, calc for final numbers, provenance, answer count
  and format, percent vs fraction, weighing alternatives) + `OfficeQAPack.seed_text()`; `notes/` empty.
- `adapters/mech-w0`: the same knowledge as executed rules + a minimal `SEED.md` (how to finish with
  `submit`, what findings and offers mean, Accept/Keep); `policy.json` = the defaults.
- Model-visible adapter text (`arms.model_visible`, the learners' `text_budget` formula): SEED.md + every
  note as rendered + every enabled rule template + (controller arms) the controller's loop/enforcement
  templates ≤ 16,384 B. W0: text 2,478 B, mech 2,429 B (829 SEED + 1,130 rule templates + 470 controller;
  the gate frame, 275 B, is recorded separately). Both numbers are in `freeze.json` and `readiness.json`.
- Scoped notes (text-evolve, mech-evolve): at most 3 per episode whose keyword triggers match the question
  (`harness_core.engine.trigger_match`), in (admitted_block, id) order, chosen before the marker hash; the
  ids are in the marker and re-derived by the audit.

### Learners — `text-evolve`, `mech-evolve` (DESIGN §3; S5)

- One evidence builder (`evidence.py`) for both: every released episode of this campaign (blocks 1..b),
  the model-view trajectory from the broker records, controller and ledger views, identical truncation;
  the same model, reasoning effort, token budget and domain text (no OfficeQA ladder or shelf).
- After block b (not after the last): the controller **seals** the block's predictions, then **releases**
  the references of its non-disputed questions into `evidence/BB/` (manifest, seal hash). The 18
  databricks/officeqa#47 questions are scored but their references are **never released** (listed under
  `withheld`, sealed under `withheld_episodes`).
- Text learner: notes (≤ 2 per block) admitted after paired actor re-runs (support × 2 + 2 controls × 2,
  cards `judge`), retired by prequential hits. Mechanism learner: rule edits from the fixed vocabulary
  (counterfactual + precision replay + leak screen + runtime; zero model cost), policy knobs (range +
  replay), notes (as text). Verification actors run through the scored episode path (`actor_runner`) in
  `evolution/BB/verify/`.
- A failed round stops the campaign (fail loudly; ABORT for the group); the operator fixes and resumes.
  A group ABORT written while a round runs sets the learner's stop event within one barrier poll.
- Both learners receive the gate's compute runner (frozen python, corpus readable), so rule admission
  replays offers exactly as the live gate executes them.
- **Rule admission, declared constants** (`mechanism_learning.py`; S5 review R4, S8):
  - *new rule, or any modification other than the two below* (`mode: full`): schema + vocabulary, leak
    screen + 8-word question-copy check, 16 KB budget, counterfactual (on ≥ 1 wrong released target the
    rule fires, its offer grades right with the controller-side grader, and it is silent when the candidate
    is the reference), precision (Wilson 95 % upper bound of the right-episode fire rate ≤
    `MAX_RIGHT_FIRE` = 0.20 on ≥ `MIN_RIGHT` = 10 right released episodes, and ≥ 1 wrong-episode fire),
    engine runtime ≤ `RULE_RUNTIME_MS` = 2,000 ms per episode (sandboxed offer re-execution is bounded
    separately by the runner's 5 s wall and recorded as `max_runner_ms`). Consequence: with 0 right fires the
    bound first drops under 0.20 at **16** right released episodes, so no new rule can be admitted before
    then (≈ round 7 at ~50 % accuracy); until then mech-evolve can change policy, priorities, disables and
    notes.
  - *priority-only change* (`mode: priority`): schema check only.
  - *disable* (`mode: disable`): the point right-episode fire rate > 0.20 on ≥ 10 right episodes.
  - Policy knobs: `contracts.POLICY_RANGES` + replay of the released controller logs.
  - At most `MAX_EDITS` = 2 rule/policy edits per block, applied by **net effect**: edits that cancel out
    within a round count toward the cap but change nothing (a round whose only edits cancel is `retained`).
    `PROPOSAL_ATTEMPTS` = 20 screened proposals per session, shared by notes, rules and policy.
  - Mechanism-arm notes are verified on the source adapter + the note, without the same block's rule and
    policy edits; text-arm notes are verified in their served context. **[user]** keep, or re-verify
    against the pending revision.

## 3. Orchestration, records, audit

- `campaign.py`: freeze (code, PROTOCOL/PREREG, `harness_core/`, `packs/`, `adapters/`, Codex, frozen
  runtime, manifest, tables index, order, W0, settings) → per block: barrier, 5 episodes, (learner arms)
  seal → release → learn → next adapter → barrier arrival → final seal and grade.
- Root layout: each root is a copy of this plumbing next to `frameworks-v8/` and `frameworks-v9/` (the
  `paths.py` defaults); a root elsewhere sets `codex` and `frozen_runtime` in `.task-paths.json`.
- **Arm-neutral model-visible paths.** Codex sends the workspace path in every request and README.md prints
  the corpus path under the root, so the actor sees the root name and the run path. v8 roots were named
  after their arm (`officeqa-pro133-cards-v8-work`). In v9 every scored episode lives in
  `runs/episodes/NNN/` (all arms), and `arms.parse_config` refuses a root name containing an arm-revealing
  fragment (`arms.ARM_REVEALING`: plain, text, mech, static, evolv, rule, control, prompt, card, learn, seed,
  note, adapt, mwm, baseline, the token `none`, the arm name). Use neutral names such as `v9-r1-a`; the
  mapping root → arm lives only in `.task-paths.json`, `freeze.json` and the barrier's `roots` list.
  Verification episodes still run under `evolution/BB/verify/<learner-chosen dirs>/` (role-revealing,
  symmetric for both evolve arms; see the review report). The barrier
  directory is shared by the repeat group and lives outside every root; recommended `timeout_s` = 43,200
  (12 h). The wait of a fast root spans the slowest root's whole block: up to 5 scored episodes × 3,600 s
  plus a learning round whose verification actors (≤ 2 notes × (support ≤ 5 + 2 controls) × 2 re-runs,
  8 at a time, ≤ 3,600 s each) can take ≈ 4 h in the worst case. v8 rounds averaged 17–23 min, but a
  6 h timeout (the first draft) can abort a healthy group when timeouts cluster in one block. The cost
  of a long timeout is idle wall time after a root dies without writing ABORT (SIGKILL).
- `barrier.py`: `<dir>/block-NN/<root>.done` files; a root starts block b+1 only after every root of its
  repeat group finished block b; a wait longer than `timeout_s`, an error or a stop writes `<dir>/ABORT` and
  every root stops at its next episode. **Block-level resume** (`run.py resume [--clear-abort]`): the freeze
  is reused; the first unfinished block's outputs (and, if its episodes are incomplete, its runs) move to
  `runs-aborted/<stamp>/`, the learners' durable files are restored from `learner-backups/BB/`, the block
  reruns.
- Records: v8 records plus, in controller arms only, `transport.json` `origin/kind/parent/codex_request/
  codex_input_sha256/forwarded`, `api/NNN/controller.json`, `api/controller.jsonl`, `api/ledger.jsonl`,
  `api/checks/NNN.json` and the v9 summary counters; the episode `summary.json` adds arm, role, notes
  served, adapter revision, kit intact, controller counters, `fallback_answer`.
- `full_audit.py`: frozen inputs; completion in order; per episode kit intact, invocation arm, frozen
  runtime, v9 settings, notes (≤ 3, re-derived), adapter revision; request records (model, reasoning,
  caps, upstream); controller=None arms without v9 fields, controller arms with origin/parent and copied
  request settings; seals before release, exact scope, no withheld reference released, adapters within
  budget; every served rule/policy change admitted — judged **per round against the revision served before
  that round** (W0 for round 1), with the record of its admission mode (`full`: a counterfactual target that
  admitted and a precision record that did not fail; `priority`: differs from its predecessor only in
  `priority`/`stats`; `disable`: the predecessor with `enabled: false` and a recorded right-fire rate >
  0.20 on ≥ 10 right episodes); policy values compared with the previous revision; static arms without
  learning outputs;
  barrier order. Added by the S6 review: AGENTS.md re-derived exactly (COMMON + served adapter + chosen
  notes) and README sections per arm, for scored and verification episodes; the scored workspace path
  names no arm; every request's temperature equals the frozen one; every learned note served has an
  admitting paired re-run row in `learning-ledger.jsonl`; every learned note and changed rule template
  passes the pack leak screen and the question-copy check against the references and questions released
  up to that round (both evolve arms under the same check).
- `cost.py`: scored actor calls by transport origin (codex / controller), learner calls, verification
  actors (by origin), work moved to `runs-aborted/`, synthetic preflight. Unknown usage is never priced as
  zero: after any HTTP 5xx, dropped stream or stalled call a group's `estimate_usd` is `null`; read
  `known_cost_lower_bound_usd` together with `unknown_usage_logical_calls`.
- **Stalled provider calls (S8 request 4, closed in wave 3).** With `request_timeout=None` a stalled provider
  stream used to outlive its episode: its records were written only when the provider closed the stream,
  possibly after a seal or an audit. Now, on exit (the episode ended or its actor was stopped),
  `Broker.abandon` does the following:
  - it sets stop, so nothing is re-sent;
  - it shuts down the socket of the call in flight. This works before the response headers (the tracked
    connection) and after them (the response being read);
  - it waits up to `EXIT_WAIT_S` (10 s) for the handler.

  The call is then recorded before the episode summary is taken: a dispatched attempt with usage unknown,
  then `not_dispatched`, and `api/summary.json` `abandoned_calls`. Nothing is written after the episode.
  Remaining limits: a stall inside the TCP connect, or in reading an HTTP error body, is not reachable this
  way. A handler still busy after 10 s is marked `transport_in_flight` as before.

## 4. Metrics

Strict `reward.py` (tolerance 0) on all 133 questions is primary; the same sign is required on the 115
undisputed questions; 1% tolerance and `fallback_answer` metrics are secondary. A subset root (pilot) seals
and grades exactly its subset (denominator = its size); its go/no-go numbers come from `pilot/metrics.py`
(`pilot/README.md`, PREREG addendum text in `pilot/ADDENDUM.md`). `private-evaluation/
paired-metrics.json` holds per-question strict and 1% scores with their position in this repeat's order;
roots are paired by task id across arms within a repeat. Pro-133 is the **development** set (analysed with
labels before v9); confirmation is on a held-out benchmark (PREREG.md §6).

## 5. Outputs per root

`state.json`, `freeze.json`, `runs/episodes/<NNN>/` (task, workspace, records, api, prediction, summary),
`private-learning-seals/`, `evidence/`, `evolution/` (learner arms), `learner-backups/`, `runs-aborted/`
(after a resume), `<arm>-predictions.jsonl`, `private-evaluation/{predictions-seal,<arm>-grades,
paired-metrics}.json`, `preflight/{readiness,offline-checks,final-audit}.json`, `cost-estimate.json`,
`report.html`.
