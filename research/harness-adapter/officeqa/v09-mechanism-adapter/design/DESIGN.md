# v9 — mechanism harness adapter: design (draft 2, 2026-09-28)

Draft 1 = `DESIGN.v1.md`. This draft applies the four reviews in `review/` (validity, effect, engineering,
generality) on top of the evidence in `../officeqa-divergence-analysis/FINDINGS.md` and `understand/*.md`.
No paid call without the user. Items marked **[user]** are decisions left to the user; defaults are stated.

## 0. Claims and tests (pre-registered before any paid call; hashed in `PREREG.md`)

- **H1 (system claim):** with the same actor, learner, tools, seed knowledge, text budget, feedback and learning
  protocol, a *harness adapter* that evolves executable controls beats a *prompt adapter* that evolves text:
  `mech-evolve > text-evolve`.
- **H2 (evolution matters):** `mech-evolve > mech-static`.
- **H3 (beats an ordinary harness):** `mech-evolve > plain`.
- Gatekeeping H1 → H2 → H3 at α = 0.05. Primary metric: strict `reward.py` (tolerance 0) on all items; the same sign is
  required on the undisputed subset (OfficeQA: 115 without issue #47). Primary model: GLMM
  `y ~ arm + (1|item) + (1|campaign)`; sign-flip permutation as robustness. MDE and a TOST (±5 items) reading of a
  null are declared. Pre-declared fallback wording if H2 fails: "hand-designed mechanisms beat automated text
  evolution".
- **Development vs confirmation.** OfficeQA Pro-133 has been analysed with labels (71/133) → it is the *development*
  set; results there are secondary. Confirmation uses (a) SpreadsheetBench Verified (on disk, SkillOpt split, pack
  built from the train split only) and optionally (b) non-Pro OfficeQA prepared by script without viewing answers
  **[user: download from HF]**. Frozen adapters from the dev campaigns are additionally evaluated on the held-out set.

## 1. Arms — 2×2 factorial + plain

| arm | L0 env | L1 pack tools | seed knowledge | L2–L4 runtime | learner edits | repeats |
|---|---|---|---|---|---|---|
| `plain` | ✓ | – | – | – | – | 2 |
| `text-static` | ✓ | ✓ | as text (W0 notes) | – | – | 1–2 |
| `text-evolve` | ✓ | ✓ | as text | – | scoped notes | 3 |
| `mech-static` | ✓ | ✓ | as executed rules (+ minimal text) | ✓ | – | 3 |
| `mech-evolve` | ✓ | ✓ | as executed rules | ✓ | rules + policy + scoped notes | 3 |

- **Seed parity:** the six seed rules, the loop advice and the provenance advice are written once as W0 *text* for the
  text arms and once as executed *rules* for the mech arms, with equal model-visible text length budget.
- **Order:** question order randomised per repeat, identical across arms within a repeat (block barrier pairs them).
- **Equal learner inputs:** one evidence builder for both learners (all released episodes of this campaign, the same
  merged model-view trajectory, the same truncation), same learner model, reasoning effort and token budget, same
  domain text (no OfficeQA ladder/shelf for either). issue-47 references withheld from both learners.
- **Equal text budget:** 16 KB of model-visible adapter text per arm, counting notes *and* rule message templates.
- **Equal verification for text:** notes in both evolve arms are admitted only after paired re-runs (cards machinery:
  support ×2 + 2 controls) and retired when their prequential hits fall below matched controls.

## 2. Layers (core vs pack)

Code layout: `harness_core/` (benchmark-neutral) + `packs/officeqa/`, `packs/spreadsheet/`, `packs/toy/`.
A test asserts `harness_core` imports nothing from `packs`. The Codex/macOS host integration stays in the plumbing
(`acting.py`, `broker.py`, `campaign.py`). The adapter contract gains a 4th export, **Controls** (policy + rules),
consumed by the framework-owned controller in the broker.

### L0 — environment (all arms identical)

- Kit `<ws>/.harness/` (write-protected): `bin/python`, `bin/python3` → a frozen copy of the codex runtime python 3.12
  (numpy, pandas; hashed by manifest, outside the roots), `bin/rg` (copy of ChatGPT.app rg), `zdot/.zprofile` restoring
  PATH after `path_helper`. Env adds `TMPPREFIX`, `ZDOTDIR`, narrowed PATH. Read roots drop the controller venv,
  `harnessadapter/` and `cua_node` (if the $0 real-Codex test shows Codex starts without them). README: denied paths.
- Codex config: as v8 (effective compaction 244.8 K from fallback metadata, recorded as observed). No
  `model_catalog_json`. `include_permissions_instructions=false` only if the $0 real-Codex test shows it removes just
  the false permissions text.
- **Temperature [user, pilot]:** v8 loops are temperature-0 fixed points and temperature 0 buys no reproducibility
  (27% flips). A pilot ({0, model-card value} × `text-static` × 20 loop-prone questions, ≈$5–10) sets one all-arm value.
- **External reference data [user]:** default *none*. Option: BLS CPI-U monthly table for all arms (neutral for the
  contrasts; ≈+3 items per arm-run on OfficeQA).
- Task conventions in README for all arms: the published OfficeQA paper rules ("maximum precision unless specified";
  "most up-to-date revision unless the question pins a date") — task specification, not key-derived knowledge.
- No wrap-up and no answer fallback in the official metric (0/386 completed v8 runs ended in their last 300 s; all 13
  timeouts were loops). `fallback_answer` recorded as a diagnostic only.

### L1 — pack tools (all arms except `plain`)

OfficeQA pack: `find_tables`, `show_table` (≤8,000 B pages), `printings`, `calc` from `understand/tables_proto/`
(17/21 structural cases pass), hardened. Every cell the tools print carries a machine-readable tag
`⟦c<id> <table> <file>:<line>|<period>|<column>|<raw> <flags>⟧`; `calc` prints `⟦k<id> …⟧` with code sha and output.

### L2 — ledger = event log derived by the broker (mech arms)

- The broker builds the ledger from the `function_call` / `function_call_output` items it relays (what the model
  actually saw), plus a command log that survives compaction. No actor-writable ledger file is read by the controller
  (removes the symlink leak, engineering #2).
- Neutral schema (`harness_core/events.py`): `observe{ref, locator, text, value?, attrs}`,
  `compute{ref, code_sha, inputs[], output}`, `candidate{answer|artifact_sha, note, alt?}`, `command{cmd, n}`.
  Pack attributes are namespaced (`oq.period`, `oq.table`, `oq.flags`, …). Untagged outputs (rg/sed/cat) become
  `observe` events with raw text so grounding checks still see them.
- After a compaction the broker splices a frozen `[harness:ledger]` user item right after the summary item
  (≤8 KB: candidates, computes with their inputs, last observations).

### L3 — control (mech arms, broker controller hook)

- Loop guard: an identical normalised command issued a 3rd time (command log across compactions). Sticky tail note in
  the fixed form: the command; "output identical"; current candidates with compute ids; options `submit "<A>"` or a
  command not yet run. Escalate to "submit now" only if a candidate exists that was computed after the last new
  evidence.

### L4 — commit gate (mech arms)

- `submit --answer "<a>" [--alt "<b>"] [--reading "…"]` (pack may add `--artifact PATH`) POSTs to the broker route
  `/v1/submit`. The broker evaluates the enabled rules over the ledger and returns findings, printed as tool output
  (faithful Codex records). Checker inputs are saved to `api/checks/NNN.json`, so offline replay uses identical inputs.
- **The gate does the work, GLM only chooses.** A finding with an *offer* re-executes the recorded compute with the
  substituted input (later printing, correctly labelled period, unscaled value) in a sandboxed runner and shows the
  alternative number: `Accept: submit --answer "<alt>"` · `Keep: submit --answer "<ans>" --keep "<reason>"`. One
  finding per round, highest-precision rule first, ≤4 lines, never "are you sure". A kept finding is never repeated.
- Enforcement: if the final message was never submitted (or its numeric tokens differ from the last submission) the
  broker re-queries once with the draft's findings; skipped with <60 s left. Submit and re-query rates are pilot
  go/no-go numbers.
- **Rules are declarative, not free Python** (`harness_core/rules.py`): `when` (predicates from a fixed vocabulary over
  question features, events and candidate relations) → `finding` (≤200 B template citing ledger values) →
  optional `offer` (a substitution operator). Core predicates: count mismatch, candidate ≠ any compute output after
  rounding, candidate = 100× a compute output, inputs not grounded in any observation, competing candidates. Pack
  predicates (OfficeQA): cell flag in {repair, `Y?`, identity-fail}, later printing differs, period ≠ question period
  (FY/CY aware), better-matching table exists, unused cells of the cited row. Seed rules: `answer_count`,
  `recompute_final`, `grounded`, `scale`, `competing_candidates` (core); `later_printing`, `period_match`,
  `flagged_cells` (no `r`/`p`), `table_alternatives`, `row_coverage` (pack).
- Runner: the trusted engine evaluates rules in-process; any re-execution of model-authored calc code runs in a
  separate `sandbox-exec` subprocess (no network, reads only python + corpus, empty env, CPU 2 s, wall 5 s).
- Pre-registered gate limits on right episodes: union fire rate ≤ 20 %; right→wrong flips after feedback ≤ 5 % of
  fired right episodes (else the lowest-precision rule is disabled before the confirmatory run).

### L5 — scoped notes (text-evolve, mech-evolve; same format)

`{id, triggers: keywords (no regex), text ≤ 600 B, admitted_block, stats}`; at most 3 matching notes rendered into
AGENTS.md per episode (chosen before the marker hash; ids recorded; audit re-derives them).

## 3. Learners

- Same model, reasoning effort, token budget, evidence builder and domain text for both.
- **Text learner** proposes notes (≤2 per block) → paired re-run verification → admission; retirement by prequential
  hits. Prompt-learner v8 bugs fixed (file tools; cap).
- **Mechanism learner** proposes rule edits (new rule from the vocabulary, trigger/threshold/template changes,
  enable/disable), policy knob changes and notes. Admission for rules, zero model cost:
  1. *counterfactual*: on a wrong released episode it targets, the rule fires and its offer reproduces the released
     reference (grading by the controller); with the candidate set to the reference, it stays silent;
  2. *precision*: over all released episodes, right-episode fire rate within the gate limits (rates with minimum
     counts; Wilson bound);
  3. leak screen (pack-specific: numbers for OfficeQA; normalised substrings for text answers), runtime limit.
  Optional (declared, ≈$1–3 per rule): *decision-point replay* — re-send the episode's final request + gate text,
  2 samples, count flips. Notes use the same paired re-run verification as the text learner.
- Both learners fail loudly: a failed round is an audit failure, not a silent static adapter.

## 4. Orchestration, records, audit, cost

- `ArmSpec{kit, l1, controller, learner, notes, rules}` in `.task-paths.json`, parsed once in `arms.py`; no substring
  architecture inference.
- One root per arm-repeat; `barrier.py` file barrier per block with timeout → ABORT file; block-level resume.
- Records: `transport.json` gains `origin/kind/parent/codex_input_sha256`; `api/NNN/controller.json`,
  `api/controller.jsonl`, `api/checks/NNN.json`; summary fields (compactions, loop notes, submits, re-queries,
  findings, fallback). Analysis/digest tools merge controller logs.
- `full_audit` architecture branches; `preflight` learner smoke + $0 real-Codex tier; `cost.py` groups by `origin`.
- `PROTOCOL.md` v9 + `PREREG.md` hashed before the first paid call.

## 5. Build plan

- **Wave 0 (me):** `harness_core/contracts.py` (events, ArmSpec, rule/predicate/offer schema, findings, submit API,
  pack protocol, cell-tag grammar), this document.
- **Wave 1 (parallel, disjoint files):** S1 L0 kit + sandbox (`acting.py`, `kit.py`) + tier-0 probes; S2 OfficeQA pack
  tools + tags + structural tests; S3 broker controller hook + `/v1/submit` + splices + loop guard + fake upstream
  (`sse_full`, CodexSim) + tests; S4 ledger builder + rule engine + seed rules + sandboxed recompute + offline replay.
- **Wave 2:** S5 learners (text + mechanism, admission, evidence builder); S6 orchestration (arms, campaign, barrier,
  notes render, data, preflight, audit, cost, report, PROTOCOL/PREREG); toy pack end-to-end.
- **Wave 3:** integration review; $0 tier-1 tests with the real Codex binary against the broker + fake upstream.
- Then **[user]**: paid smoke (≈$1), pilot (20 questions × {text-static, mech-static} + temperature cell, ≈$10–20),
  go/no-go (ledger coverage ≥60 %, wrong-fire ≥2× right-fire, flips wrong→right ≥3× right→wrong), then campaigns
  (OfficeQA dev ≈12 campaigns ≈$500–600; SpreadsheetBench confirmation separately budgeted).
