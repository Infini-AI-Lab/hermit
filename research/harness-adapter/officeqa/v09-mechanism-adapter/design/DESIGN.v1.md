# OfficeQA v9 — mechanism harness adapter: design (draft 1, 2026-09-28)

Inputs: `../officeqa-divergence-analysis/FINDINGS.md` (103 failed GLM runs vs Opus reference traces),
`understand/{acting,broker,campaign,compaction,tables,replay,critic}.md` (v8 code map + offline replay + critique).
Code base: copy of `officeqa-pro133-v8-plumbing/` → `officeqa-pro133-v9-plumbing/`. No paid run without the user.

## 0. Claim to test, and the one contrast that decides it

**Claim.** With the same actor (GLM-5.3-flash in Codex), the same learner model, the same feedback and the same text
budget, an evolver that may edit *executable harness mechanisms* (checkers, control policy, tool parameters) in
addition to scoped text improves accuracy more than an evolver that may edit *text only*.

**Primary pre-registered contrast:** `mech-evolve` vs `prompt-evolve`, strict accuracy (official `reward.py`,
tolerance 0) on all 133 OfficeQA Pro questions, 3 independent campaigns per arm, paired by question and repeat;
test = sum of per-repeat paired differences (sign-flip permutation over questions), two-sided α = 0.05.
Everything else is secondary and reported as descriptive.

## 1. Arms (five; all share L0)

| arm | L0 env | L1 tools | L2–L4 runtime machinery | evolves | role |
|---|---|---|---|---|---|
| `plain` | ✓ | – | – | – | 普通 harness: Codex + fixed environment |
| `base` | ✓ | ✓ | – | – | effect of the shared tools |
| `prompt-evolve` | ✓ | ✓ | – | text notes (≤16 KB) | GEPA / SkillOpt-style text evolution |
| `mech-static` | ✓ | ✓ | ✓ (seed W0) | – | effect of the machinery without evolution |
| `mech-evolve` | ✓ | ✓ | ✓ | checkers + policy + tool params + scoped notes (≤16 KB) | harness-adapter evolution |

Why L1 goes to prompt-evolve too: L1 was tuned by us on these 133 questions (critic R1). Giving it to both evolve
arms makes that exposure common, so the primary contrast measures only *what the evolver may edit* (critic R2).
`plain` keeps the comparison against an ordinary harness honest.

Repeats: primary two arms × 3; `plain`, `base`, `mech-static` × 1 (descriptive). ≈ 9 campaigns.

## 2. Layers

### L0 — environment (all arms, identical; lives in `acting.py`, frozen in `freeze.json`)

- Kit dir `<ws>/.officeqa/` (write-protected): `bin/python`, `bin/python3` → codex runtime python 3.12 (numpy 2.3.5,
  pandas; copied into the campaign root and hashed, not symlinked to the app-managed cache); `bin/rg` (copy of the
  ChatGPT.app rg); `zdot/.zprofile` restoring PATH after `path_helper`.
- Env: `TMPPREFIX=<ws>/.actor-tmp/zsh`, `ZDOTDIR=<ws>/.officeqa/zdot`, `PATH=<kit>/bin:/usr/bin:/bin:/usr/sbin:/sbin`.
- Read roots narrowed: drop the controller venv, `harnessadapter/`, `cua_node` (unless a live smoke shows Codex needs
  them). README adds: "paths outside the corpus, index and this workspace are denied; do not search the disk".
- Codex config: unchanged from v8 for context/compaction (effective 244.8 K from fallback metadata, recorded as
  observed); `include_permissions_instructions=false` only if the offline config check + smoke show it removes just the
  false "no sandbox / network enabled" text. No `model_catalog_json` (critic N2: would change truncation for everyone).
- Sampling stays `temperature=0` (comparability with v8). A temperature pilot is a separate, declared experiment.
- Prompt learner fixes (fairness, all learners): native file tools work (runtime roots bug, campaign.md §2.3);
  learner reasoning effort identical for both learners (`high`; `full_audit` updated); ADAPTER.md capped at 16 KB with
  consolidation (same text budget as mech notes).

### L1 — shared tools (`base`, `prompt-evolve`, `mech-*`)

Shipped in the kit, documented in README (tools get adopted: `officeqa.py` was used in 130/133 v8 runs):

- `find_tables <phrase> [--years a-b] [--rows]` — table-level search (ids with OCR canon, titles, headers,
  syllable-split fallback), grouped by table identity with issue coverage.
- `show_table <file>:<line> [--stitch] [--long] [--cols …] [--from-row N]` — clean grid: header realignment with VERIFY
  notes, period fill-down (`Y?` when unknown, never guessed), identity checks, repair flags; ≤8,000-byte pages.
- `printings "<row>" --table <id|title> [--period P] [--column C]` — the same row across issues, distinct values,
  first/latest printing, ISOLATED outliers.
- `calc <script.py | -c code>` — runs python with numpy; prints result.
- All four append a JSON line to `<ws>/.officeqa-ledger/ledger.jsonl` automatically (tool, args, file:line refs, cells
  shown with period/column/flags, calc code + output). **The model never has to write the ledger** (critic N4/G3).
- Source: `understand/tables_proto/otables.py` (17/21 structural cases pass) hardened; index built at prepare time.

### L2 — ledger (mech arms)

- Automatic records from L1 (above) + `submit "<answer>" [--note "<reading/assumption>"]` which records a candidate.
- Trust: honest bookkeeping inside the actor sandbox, not a security boundary (critic C4; 0 v8 commands touched the
  broker port). The broker reads it as untrusted data, size-capped, rendered as data.
- After a compaction the broker splices a frozen `[harness:ledger]` user item right after the summary item
  (critic C1: broker.md splice, not SSE rewrite). Rendered ≤ 8 KB: candidates, cells used by calc, last cells shown.

### L3 — control (mech arms; broker controller hook)

- **Loop guard:** any identical (normalised) command issued a 3rd time over a broker-side command log that survives
  compaction (critic C2; replay golden 22/25/29 runs). Inject a sticky tail note: the command, "its output will not
  change", current candidates from the ledger, "take a different action or submit". Once per distinct command; after
  3 notes in a run escalate to "submit your best answer now".
- **Wrap-up:** when remaining time < 180 s, one `tools=[]` wrap-up request asking for the final answer line.
- **Timeout fallback:** controller writes `fallback_answer` (last submitted candidate, else empty) in a separate field;
  the official score uses `answer` (empty on timeout, as v8); fallback reported as a secondary metric (critic R5).

### L4 — commit gate (mech arms)

- `submit` runs the enabled checkers inside the actor sandbox over (question, candidate, ledger) and prints findings
  immediately — feedback reaches the model through a normal tool output, so Codex records stay faithful (resolves
  critic C3 without resume and without broker re-queries in the common path).
- Broker enforcement (rare path): if the model's final message is not equal to the last `submit`ted candidate, the
  broker re-queries once with "call `submit` with your final answer first" (broker.md §3c mechanics: forward the
  revision's own wire; splice draft+note; records in `controller.json`).
- Seed checkers (domain-general for document QA; all advisory, never blocking):
  1. `answer_count` — number of values vs what the question asks (replay: 2/2 wrong, 0 right).
  2. `calc_provenance` — the candidate should equal a recorded calc output after the requested rounding; if not, ask to
     recompute in `calc` (advisory only; replay shows typed answers are often right).
  3. `cell_exists` — every number fed to calc that matches no shown cell and no prior calc output is listed
     ("typed from memory?").
  4. `flagged_cells` — calc inputs matched to cells carrying `r`/`p`/repair/`Y?`/identity-failure flags → ask to confirm
     with `printings`/`show_table --raw`.
  5. `later_printing` — for calc inputs matched to table cells, if a `printings` record shows a different later value
     → ask to state which printing and why (surfaces vintage forks; does **not** encode a vintage rule — critic G7).
  6. `period_match` — periods of matched cells vs years/months/FY/CY named in the question.
- Checker runner: pure functions, 2 s CPU limit each, no network, reading only the episode ledger; the same runner is
  used online and in offline replay.

### L5 — scoped notes (prompt-evolve and mech-evolve; same 16 KB budget)

- Note = `{id, triggers (keywords/regex over the question), text ≤ 600 B, status}`. At episode start the host renders
  AGENTS.md with only the notes whose triggers match (max 3) for mech-evolve. prompt-evolve keeps its whole-document
  form (that is what text evolution is), capped at 16 KB.

## 3. The mechanism evolver (`mechanism_learning.py`)

- **Model/settings:** same learner model, reasoning effort and call budget as the prompt learner.
- **Sees:** this block's sealed answers, released references, public trajectories **merged with** ledger + controller
  logs (what the model saw), checker findings; plus all earlier released episodes of this campaign for replay.
- **May edit:** `checkers/*.py` (new or modified, ≤ 8 per adapter, each ≤ 4 KB, pure API), `policy.json` (loop
  threshold 3–6, nudge wording, which checkers are on, gate rounds ≤ 2, tool defaults such as `printings` window),
  `notes/*.md` (≤ 16 KB total). Not: the runner, the tools' code, the broker, AGENTS.md base text.
- **Admission (deterministic, zero model cost):**
  - checker: re-run over every released episode of this campaign (ledger available because all ran under the mech
    runtime): fires on ≥ 1 wrong episode it names; fires on ≤ 1 right episode and ≤ 10 % of right episodes; leak screen
    (no released reference value, no ordinals/uids, no ≥ 3-significant-digit constants > 400); runtime ≤ 2 s.
  - policy: replay suites (loop replay on this campaign's controller logs; structural table cases) unchanged or better.
  - notes: schema + leak screen + budget (text is unverified in both evolve arms — equal treatment).
  - issue-47 questions are excluded as support/targets for both learners.
- Limitation, declared: offline admission measures detection, not the effect of the feedback on the actor.

## 4. Scheduling, records, audit, cost

- One root per arm-repeat; a shared **block barrier** (all roots finish block b before any starts b+1) keeps provider
  conditions aligned (critic G9/R7). Learning arms learn inside the barrier interval.
- Records: `api/NNN/controller.json`, `api/controller.jsonl`, `summary.json` new fields (compactions, loop notes, gate
  rounds, fallback), `.officeqa-ledger/` copied into evidence for learners; digest tools merge controller logs.
- `full_audit`: new architecture branch (checker admission reports present; every served checker admitted; notes ≤
  16 KB; controller=None arms byte-identical to v8 broker behaviour).
- `cost.py`: groups for controller calls and mech learner.
- PROTOCOL.md v9: declares L0 changes vs v8, all arms, the controller, fallback metric, evolver admission, limits.

## 5. Build order (no paid calls until step 6)

1. v9 plumbing copy + experiment profiles + L0 kit + L1 tools (+ tests: sandbox probe, structural table cases).
2. Broker controller hook + controller (loop guard, splices, wrap-up, submit enforcement) + fake-upstream tests incl.
   `sse_full` and a CodexSim; offline replay goldens on v8 `api/` dirs.
3. `submit` + checker runner + seed checkers + replay harness (on synthetic ledgers; v8 has none).
4. Learners: prompt learner fixes + cap; mechanism learner + admission.
5. Campaign/data/preflight/full_audit/cost/report + PROTOCOL.md + block barrier.
6. Paid smoke (needs user OK): 1 synthetic task per arm; then the live Codex-in-the-loop checks the critic lists (G5).
7. Pilot (needs user OK): 20 questions × {plain, mech-static} to measure loop-guard and gate behaviour before the full run.

## 6. Open items for the user

- Budget sign-off for smoke / pilot / ≈ 9 campaigns (≈ $350–450 at v8 prices).
- CPI/external data: default **none for all arms** (critic G6).
- A second benchmark for generalisation (critic G11) after OfficeQA.
