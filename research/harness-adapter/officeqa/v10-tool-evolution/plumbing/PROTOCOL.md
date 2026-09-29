# OfficeQA Pro 133 — v10 protocol (the adapter owns text, tools and controls; the learner evolves text and tools together)

Design: `officeqa-v10-design/DESIGN.md` (draft 2, rev1) and its review `officeqa-v10-design/review/DESIGN-REVIEW.md`.
Pre-registration: this tree's `PREREG.md` (the document DESIGN §7 calls `PREREG-v10.md`; written and hashed after
S4; its first heading must name v10 - `preflight.py` refuses a v9 copy - and `preflight.py` records the hash in
`preflight/readiness.json`, the campaign in `freeze.json`; `run.py run` refuses a root whose PREREG differs). The
v9 tree's PREREG.md is not copied into this tree.
Shared seams: `harness_core/contracts.py` (`CONTRACT_VERSION 'v10.0'`).

Each campaign root runs **one arm for one repeat** over the OfficeQA Pro questions (this round: the first-50
development subset, DESIGN §7) with the same pinned dataset, parsed TXT corpus, native Codex CLI
(`frameworks-v8/codex-0.155.0-alpha.9.2`) and the exact Fireworks model `accounts/fireworks/models/glm-5p3-flash`.
The roots of one repeat run the same question order and advance block by block (5 questions per block).

## 1. Ownership (DESIGN §1)

**Base harness, identical in every arm and free of task knowledge:** Codex + GLM through the broker relay,
temperature 0, no top_p; the L0 repair (frozen actor python with numpy/pandas, ripgrep, `TMPPREFIX`/`ZDOTDIR`,
the Seatbelt read policy, multi-agent off, permissions text off, explicit auto-compact 244,800); the task
environment (COMMON instructions, README with the corpus path, the published task conventions, the FTS5
search helper `officeqa.py`).

**Adapter (a directory; everything else):**

| export | files | prompt adapter | harness adapter |
|---|---|---|---|
| context | `SEED.md` (always shown in AGENTS.md), `notes/<id>.json` (scoped, ≤ 3 per question) | yes | yes |
| tools | `tools/manifest.json`, `tools/*.py`, `tools/USAGE.md` (the README `# Tools` section) | no | yes |
| controls | `controls.json` (loop guard, splice, submit gate); `rules/`, `policy.json` only when the gate is on | no | yes |

`adapter.json = {"architecture": "prompt" | "harness", "contract": "v10.0", "pack": "officeqa", "revision":
"w0" | "<sha12>"}`; `revision` = `arms.revision(tree)` = sha12 of the tree minus adapter.json (freeze, markers
and admission records all use it). `arms.check_adapter` validates every served adapter (W0 and every candidate)
before any actor run: architecture, pack, revision, manifest (names, scripts, `calc` pinned to the W0 copy),
tool caps, the USAGE line rule, controls (`contracts.checked_controls`), the import closure of every script
(parsed with `ast`, never imported) and a dry-run kit. The controller never imports or executes adapter python.

## 2. Arms (`.task-paths.json` "arm", parsed once by `arms.py`; DESIGN §2)

| arm | adapter | learner | controller (controls.json) | gate |
|---|---|---|---|---|
| `plain` | none | — | no | off |
| `prompt-static` | prompt W0 | — | no | off |
| `prompt-evolve` | prompt W0 | `text` (notes only; `prompt_learning.py`) | no | off |
| `harness-static` | harness W0 | — | yes | off |
| `harness-evolve` | harness W0 | `harness` (notes + tool revisions; `harness_learning.py`) | yes | off |
| `harness-evolve-notes` | harness W0 | `text` (notes only, on the harness W0) | yes | off |

- `contracts.ArmSpec{name, pack, l1_tools, controller, seed, learner, notes, gate}`: `seed ∈ {none, text,
  harness}`; a prompt adapter has no tools and no controls; a harness adapter has both; the notes-only learner
  runs on either architecture, the harness learner only on the harness one; `gate ⇒ controller`. Every submit
  site keys on `gate`, never on `controller`. The v9 names (`text-*`, `mech-*`) are refused with a message
  naming the v10 arms; profile `'v9'` is refused with a message naming the v9 tree.
- `.task-paths.json` keys: `profile: "v10"`, `arm`, `repeat`, `pack`, `order_seed` (one value for the whole
  study: `v10-officeqa-pro133-order`), `barrier: {dir, roots, timeout_s[, poll_s]}`, the optional `subset:
  {name, task_ids, sha256}` (`sha256 = arms.subset_sha256(task_ids)`), `temperature` (default 0), `top_p`
  (absent = none sent), and the `paths.py` overrides. Any other key is refused. `run.py roots` always writes
  `tables_index` (default `<parent>/frameworks-v10/officeqa-tables-index/tables.sqlite`, version
  `otables-index-v10`): without it `data.py prepare` installs the frameworks-v9 index (v9.1, no narrative
  section tables), which the v10 tools open without error, so W0-12 would silently print nothing
  (integration finding 2026-09-29); `data.install_tables_index` refuses an index whose version is not
  `otables.INDEX_VERSION`, and `preflight.py` checks the installed version (`tables_index_version_current`).
- Question order: `arms.question_order` = ascending SHA-256(`order_seed` NUL `repeat=r` NUL task_id) over the
  base order; a subset runs the full order with the other questions removed, in blocks of 5.
- Root names are neutral (`v10-r1-a` … `v10-r1-e`; `run.py roots`): `arms.parse_config` refuses a name with an
  arm-revealing fragment (`arms.ARM_REVEALING`, now also `harness`, `evol`, `adapt`, `revision`). The mapping
  root → arm lives only in `.task-paths.json`, `freeze.json` and the barrier's roots list.

## 3. Serving an adapter to an episode (DESIGN §3)

1. **Kit** `<ws>/.harness/` (write-protected): `bin/python`, `bin/python3`, `bin/rg`, one POSIX wrapper per
   manifest tool, `submit` **only when the gate is on**; `zdot/.zprofile`; PATH `<ws>/.harness/bin:/usr/bin:/bin:
   /usr/sbin:/sbin`.
2. **Neutral code root.** `campaign.stage_code_root` copies the served revision's `tools/` once to
   `<root>/runs/code/<sha12>/tools/` (read-only, beside the workspace, never inside it; the path is checked
   for arm-revealing fragments); the kit wrappers embed that path; the Seatbelt profile grants exactly the
   scripts plus their sibling-import closure (`kit._import_closure`) and a listing of that directory; the
   marker records `code_root`, `code_sha12` and every granted file's sha256. Nothing of `adapters/` or
   `evolution/` is ever readable to the actor.
3. **README.md** = L0 + search helper + task conventions + (harness) `# Tools` + the served revision's
   `tools/USAGE.md` (calc lines included) + (gate on only) the submit section. **AGENTS.md** = COMMON +
   `SEED.md` + ≤ 3 notes chosen by trigger (ids in the marker, re-derived by the audit).
4. **Controls** (`controls.json`, harness adapters, `contracts.CONTROLS_DEFAULTS` / `CONTROLS_RANGES`):
   `loop_guard`, `loop_threshold` 3, `loop_notes_max` 3, `splice`, `ledger_splice_bytes` 8000, `gate` false,
   `enforce_requery_max` 0. Gate off: no `submit` wrapper, no submit README section, `/v1/submit` answers 404,
   `on_final` always forwards (a `NullGate` whose methods raise), `rules/` and `policy.json` are refused, the
   loop guard uses the gate-off wording (`LOOP_TEMPLATE_NOGATE` / `LOOP_OPTIONS_NOGATE`: no candidates line, no
   `submit`; escalation on the newest `Compute` after the last new evidence) and, after `loop_notes_max` notes,
   the R6 hard block (`HARD_BLOCK_TEMPLATE`, "blocked: identical output already shown", a sticky note recorded
   as a `hard_block` decision). **Integration change (W0-PROPOSAL §(e) items 1 and 2, applied 2026-09-29; item
   3 `empty_output_retry` deferred):** a command is noted when it reaches `loop_threshold` issues and again
   after every further `loop_threshold` issues (not once per command); gate off, once `loop_notes_max` notes
   were shown, *every* further issue of a noted command that repeats an output it already showed (same
   command, same normalised-output sha) gets the block, which names the call that first showed the output and
   the repeat count and echoes the first 20 lines (≤ 2,000 B, `HARD_BLOCK_ECHO_LINES` / `HARD_BLOCK_ECHO_BYTES`)
   of that output before the options line; a repeat whose output changed falls back to the note rule. The
   templates are counted in the model-visible budget (`arms.harness_fixed_text` =
   `learner_common.controller_template_bytes`); the echo cap is recorded beside them (`hard_block_echo_cap`,
   `arms.RECORDED_ONLY`), not counted: the echo is the model's own tool output. `controls.json` values are the
   design's defaults and fixed at W0 this round.
5. **Ledger.** The broker derives command / observe / compute / note / compaction events from what the
   model saw; the pack's observation parser is frozen (it parses the W0 tools' options). Parser mode per
   episode: `rerun` when every `tools/*.py` of the served revision equals W0's, else `header` (no tool re-run;
   header recovery and raw lines only). `observation_coverage` (commands parsed by invocation / recovered by
   header / raw only / none) goes into the broker summary, the episode summary and the report.
6. **Model-visible text budget:** `SEED.md` + all notes + `tools/USAGE.md` (harness) + the controller's fixed
   templates ≤ 16,384 B; `USAGE.md` ≤ 3,072 B and every line names a manifest tool or a `--option` of the tool
   sources; tool code ≤ 12 files, ≤ 196,608 B per file, ≤ 393,216 B total, python only. A prompt W0 (and every
   prompt-arm note) must not name the harness tools.

## 4. W0 adapters (DESIGN §4)

`adapters/harness-w0/` (SEED.md, notes/, tools/{cli.py, otables.py, calc.py, manifest.json, USAGE.md},
controls.json) and `adapters/prompt-w0/` (SEED.md, notes/) are generated by `arms.py build` from
`packs/officeqa` (`seed_text_harness` / `seed_text_prompt`, `w0_tools`, `usage`) and pinned by `check_w0`:
drift against the generated tree, the **parity checklist** (`packs/officeqa/SEED_KNOWLEDGE`: every item's
sentence present in its SEED; both SEEDs ≤ 4,096 B and within ±25 % in bytes; the prompt SEED names no harness
tool) and `check_adapter`. `preflight.py` records both SEEDs' bytes and hashes and the model-visible totals per
architecture (PREREG copies them). **S4 is in this tree** (build round 2): the SEED texts are W0-PROPOSAL §(a)/(b)
byte for byte (4,053 / 4,092 B), `SEED_KNOWLEDGE` is the §(c) checklist K1..K13, `tools/cli.py` + `otables.py`
carry the §(d) tool changes W0-1..W0-12 (the frozen observation parser reads their new options), USAGE.md is
the §(d) text (2,512 B with the calc lines), and `packs/officeqa/pack.py` records the **exposure record**
(`W0_CHANGES`, `EXPOSURE_ITEMS`, `exposure_record()`: per change the development items it was designed from,
issue-47 items marked). The harness learner's prompt lists the changes (`w0_changes_text()`, no item ids); the
analysis reports every contrast on all 50 and excluding `EXPOSURE_ITEMS`. The base README environment section
carries the §(d) non-tool facts (absolute corpus paths for `rg`/`sed`, macOS `find`/`cat`, `NormalDist`) and the
search helper example prints the result shape. No W0 adapter ships seed notes (notes/ is empty).

## 5. Learning (DESIGN §5; `learner_common.py`, `prompt_learning.py`, `harness_learning.py`)

- After block b (not after the last): the controller **seals** the block's predictions, **releases** the
  references of its non-disputed questions into `evidence/BB/` (issue-47 references are never released), runs
  the arm's learner with a verification actor runner (the scored episode path, so a candidate's tools are served
  from its own neutral code root), the block's `ToolRunner` and the W0, checks the returned adapter
  (`check_adapter`) and serves it from the next block on. A failed round stops the campaign (group ABORT).
- Both learners: same model, reasoning effort high, budget, evidence (model view with COMMAND exit codes,
  `read_tool_output`, friction digest) and note machinery; `propose_note` admitted by paired actor re-runs
  (support × K = 2 + KEEP = 2 controls × 2; cards `judge`), retired by prequential hits. `SUPPORT_MAX = 3`.
  **On a harness adapter both learners see the same tool evidence** (`learner_common.tools_evidence_section`:
  the (a)-(d) taxonomy text, manifest, USAGE.md, diff vs W0, W0 change list, friction digest) and read the same
  files (`read_adapter_file` / `search_adapter_file` over tools/*.py, USAGE.md, manifest.json, controls.json;
  `search_adapter_file` is a plain-text search, several alternatives with `|`, never a regular expression), so
  `harness-evolve` vs `harness-evolve-notes` differs only in the proposal tools (review fix).
- Harness learner only: `stage_tool_patch` (exact, unique replacement in a `tools/*.py`; ≤ 20 per proposal),
  `stage_tool_file` (new module ≤ 16 KB, USAGE.md, manifest.json), `try_tool` (≤ 30 calls),
  `propose_revision(id, support, rationale, expect, note)`; `expect` steps may be the COMMAND or the OUTPUT
  line (normalised to the COMMAND step); a brand-new tool must be bundled with a change that alters an
  existing command's output (stated in the prompt).
  Screens (zero model cost): path / caps / stdlib-shadowing / calc pinned; the functional python screen
  (`py_compile`, imports resolvable by the frozen python, no `exec`/`eval`/`__import__`/`compile` calls, no
  `os.fork/exec*/system/popen/posix_spawn*/spawn*` calls, network and process modules - `subprocess`, `pty`,
  `multiprocessing` - refused); the code-aware diff leak screen (delta rule vs the source file, also for the
  arm-revealing words: only words new to the file count; numeric-literal rule against released references over
  literals and constant-foldable expressions incl. `int('2' + '52')`, `ord(...)`, `len(...)`, string
  concatenation); the proposal id screened for both notes and revisions; manifest; text budget; the **replay**
  of the recorded manifest-tool commands (rebuilt from `raw_cmd` incl. `if tool ...; then`, `$(tool ...)` and
  backticks; every invocation of the support episodes first, then the most recent of the other episodes up to
  80 in all; `calc` skipped; 4 workers, 20 s each, 300 s wall; ≥ 5 source jobs must exit 0; regressions and
  first-line contract breaks refuse; `expect` must change an output, and a USAGE.md-only proposal consumes no
  replay); the **tier** (`additive` / `value-changing`) chooses the replay-based controls (KEEP 2 / 4, a changed
  control must stay right). Slots per block: ≤ 2 notes + ≤ 1 revision (a bundle uses the revision slot). Cost
  bound: ≤ 34 verification actor runs per block. Grounding is recorded, not gating (scope decision 1);
  `propose_revert` and prequential revision monitoring are not built (scope decision 2).
- **ToolRunner** (replay and `try_tool`; `campaign.make_tool_runner` is its one constructor, used by the
  campaign and by preflight): runs a *neutral copy* of the tools under `runs/code/<tools sha12>/tools`
  (`campaign.stage_tools_copy`), with the actor kit's environment keys pointing into the per-call work dir, a
  cwd named `workspace`, and exactly the script's import closure readable (never the whole tools/ directory), so
  a tool cannot tell the replay from the actor by `__file__`, `os.environ` or a read of USAGE.md. It denies
  process creation and network (the actor profile allows fork/exec and TCP to its broker port), so the screen
  refuses process calls, and it caps resident memory (`memory_mb`, 3 GB, a process-group watchdog: macOS has no
  enforceable RLIMIT_AS). The replay is a functional screen in a non-actor profile; admission rests on the
  paired actor re-runs.
- **Verification episodes** run in `<root>/runs/verify/BB/<token>/<NN>/workspace` (`Layout.verify_runs`): an
  opaque token per proposal (`r01`, `r02`, …; the mapping is in `evolution/BB/verification.json`) and a sequence
  number per run, so the model-visible cwd names neither the proposal id, the question, the role nor the word
  evolution; the verification runner refuses any other location and any revealing path.
- Records: `evolution/BB/sessions/learner/control/mechanical-admission.json` (`admitted_notes`,
  `admitted_revision`, the `revision` record with tier, files, patches, controls, replay counts and rows,
  `candidate_revision`, `screens`, `screen_version`, `replay_seconds`, `try_tool_calls`) and
  `learning-ledger.jsonl` rows (kind note | revision | bundle, action add | screen_rejected | retire, admitted,
  t = the round).

## 6. Orchestration, records, audit (`campaign.py`, `full_audit.py`, `cost.py`, `report.py`)

- Freeze (code, PROTOCOL/PREREG, `harness_core/`, `packs/`, `adapters/`, Codex, frozen runtime, manifest,
  tables index, order, W0 with `arms.revision`, settings incl. `controls_defaults` / `controls_ranges`) → per
  block: barrier, episodes, (learner arms) seal → release → learn → next adapter → barrier arrival → final seal
  and grade (strict `reward.py` on the subset and on the undisputed part).
- Root layout: `state.json`, `freeze.json`, `runs/episodes/NNN/` (task, workspace, records, api, prediction,
  summary), `runs/verify/BB/<token>/<NN>/` (verification episodes, opaque names), `runs/code/<sha12>/tools/`
  (neutral code roots: served revisions and the learners' replay copies), `private-learning-seals/`,
  `evidence/`, `evolution/BB/{candidate, tmp, verify/<id>/adapter, sessions, adapter, verification.json, ...}`,
  `learner-backups/`, `runs-aborted/`, `<arm>-predictions.jsonl`, `private-evaluation/`, `preflight/`,
  `cost-estimate.json`, `report.html`. Model-visible names carry no arm word: the kit directory is `.kit/`,
  controller inserts are prefixed `[runtime:<reason>]`.
- **Block-level resume** (`run.py resume [--clear-abort]`) as in v9: the first unfinished block's outputs move
  to `runs-aborted/<stamp>/`, the learners' durable files are restored from `learner-backups/BB/`.
- **`preflight.py`** (readiness, $0 then the paid smoke): spec, data, tables index, frozen runtime (deep),
  barrier order, both W0s (drift, parity checklist, byte counts, `check_adapter`), W0 idempotence through the
  learner screens (`preflight.W0_SCREENS`; TODO until registered), the **ToolRunner smoke** (every harness W0
  manifest tool once through `harness_core.runner.ToolRunner` over this root's corpus and tables index; read
  roots checked against the private trees), an offline workspace of the first question (kit built, submit iff
  gate, neutral code root), the test suites and the $0 live-Codex tier, PREREG hashed. Separate and optional
  (never part of readiness): `preflight.py --replay-v9a <root>` replays the recorded tool commands of the
  read-only v9 evidence root against the unchanged W0 tools (each twice: source = candidate = W0) and requires
  ≥ 200 rebuilt invocations and 0 changed outputs. Result on the merged W0 tools (integration, 2026-09-29, on
  `v9-p1-a`, `preflight/replay-v9a.json`): 3,235 recorded commands, **724 rebuilt invocations** (285 `find_tables`,
  274 `show_table`, 165 `printings`), 721 exit 0 (the 3 others exit 2 in both runs, as recorded in the pilot),
  **0 changed outputs**, 0 timeouts, 141 s wall. The first integration run found 1 changed `printings` output
  (a no-match line whose period range broke ties between labels of one year in set-iteration order, i.e. by the
  per-process hash seed); `otables._period_range` now orders ties by the label text and the replay was re-run.
- **`calibrate.py`** (the calibration round of DESIGN §5): `python calibrate.py --source-root ../v9-p1-a
  [--actor-runs 20] [--source-tools w0|v9] [--dry-run] [--tables-index FILE|none]` builds a scratch root over
  the finished root's records (its 50 released episodes as one evidence block; issue-47 references never
  written; `prepared/` linked entry by entry with the study's v10 tables index in place of the root's own v9.1
  one - default when `../frameworks-v10/officeqa-tables-index/tables.sqlite` exists, `none` keeps the root's
  own; recorded in the record's `block`), runs one harness learner
  session on it with the campaign's ToolRunner and a capped verification runner (real episodes under
  `runs/verify/01/`), and writes `preflight/calibration.json` (proposals by kind, screen outcomes, replays,
  tiers, admission, actor runs, cost). `preflight.offline` requires a non-dry record for a harness-learner
  root before `prereg_pending` clears.
- **`run.py`**: `roots` ($0: the five DESIGN §7 roots on the first-50 subset), `smoke` (paid, cents; the
  question asks for `submit` and a submission is required iff the gate is on; a gate-off controller arm must
  have no submit route), `run`, `resume`, `audit`, `cost`, `report`.
- **`full_audit.py`** (v9 checks plus): the code root of every scored and verification episode
  (`<root>/runs/code/<revision>/tools`, arm-neutral path, every kit script under it, every granted file's sha
  equal to the served revision's `tools/`, the granted files and listing dirs re-derived from the manifest's
  import closure, the marker's `code_root` / `code_sha12` present, the revision segment =
  `arms.revision(adapter)`); every workspace path (scored and verification) arm-neutral, verification episodes
  under `runs/verify/`; the adapter of block b tied to the round b-1 candidate (or the block b-1 adapter), block
  1 = W0, every activated round with a `mechanical-admission.json` naming its candidate revision; submit wrapper /
  README submit section / gate counters / check files iff gate, `# Tools` iff `l1_tools`; `controls.json`,
  `SEED.md` (and `rules/`, `policy.json`) equal to W0; `observation_coverage` recorded on controller arms; every
  served note admitted by a ledger row, every served tool revision admitted (ledger row kind revision | bundle)
  with an admission record that staged exactly those files and names the served candidate revision, and passing
  `check_adapter`; every served note's ledger row carries its text and triggers; learned notes and USAGE.md
  lines added since W0 pass the leak screen and the question-copy check; static arms have no learning outputs;
  barrier order; subset / temperature / top_p on every record.
- **`cost.py`**: scored actors by transport origin, learner calls, verification actors (`verification`
  totals), `runs-aborted/`, synthetic preflight, and the $0 `replay_seconds` group (replay wall time and job
  counts, try_tool calls, the preflight v9-a replay). Unknown usage is never priced as zero.
- **`report.py`**: answer-free; the per-block prequential table, the gate-off controller line with the
  observation-coverage block average, the learning rounds and the admitted tool revisions (tier, files,
  patches, replay numbers).

## 7. This round's run (DESIGN §7)

Roots `v10-r1-a` plain, `-b` prompt-evolve, `-c` harness-static, `-d` harness-evolve, `-e`
harness-evolve-notes (whether `-e` runs is a launch-time budget decision); T = 0, no top_p; subset
`pilot-first50` (sha `c95f9613…a07d`, 5 issue-47 ids scored and withheld); blocks of 5; barrier
`v10-r1-barrier` (12 h). Named contrasts: `d` vs `e` (one factor: the learner may also change tools), `d`/`e`
vs `c` (evolution helps), `b` vs `a` (notes-only evolution on the prompt architecture), `c` vs `a` and `b` vs
`d` (system contrasts, optimistic for the harness side by construction). **Status: descriptive** (one repeat at
T = 0; a paired difference within ±4 questions is noise, ≥ 8 more than noise). Steps: `pilot/README.md`.

## 8. Statements PREREG-v10.md carries (build round 2)

- **Tool processes.** A tool process started by the actor runs under the actor's Seatbelt profile: no reads
  outside the granted roots (the corpus, the tables index, the script's import closure), writes only in the
  workspace, TCP only to the actor's own broker port (which Codex's own shell can reach as well). The nested
  per-tool network denial is deferred (scope decision 4). The learners' replay and `try_tool` run tools in the
  ToolRunner profile (no network, no processes, memory-capped) from a neutral copy with an actor-like
  environment; the replay is a functional screen, admission rests on the paired actor re-runs.
- **Model-visible names.** No name the model can see carries an arm word: root names, `runs/episodes/NNN`,
  `runs/verify/BB/rNN/NN`, `runs/code/<sha12>/tools`, `.kit/`, `[runtime:<reason>]` inserts, tool file and
  manifest names (`arms.ARM_REVEALING`, checked at creation, in the E2E request bodies and by the audit).
- **Leak screen exposure.** The code-aware diff leak screen is best-effort: it folds literals, arithmetic,
  string concatenation and the builtins int/float/str/ord/len/abs/round/chr; a released reference encoded through
  data the tool reads at run time, or through a comparison with a corpus value, is not caught. Support questions
  are released wrong episodes, so a tool that special-cases them can pass the paired re-runs; the harness-evolve
  vs harness-evolve-notes contrast is read with that residual in mind (the post-admission diagnostic - re-running
  each support question's commands and flagging output lines that print the released reference verbatim - is
  not built this round).
- **d vs e** is a one-factor contrast: both learners see the same evidence, the same tool evidence section and
  read the same adapter files; only `stage_tool_patch`, `stage_tool_file`, `try_tool` and `propose_revision`
  differ.
- **Exposure record.** `packs/officeqa/pack.py: exposure_record()` (W0-1..W0-12 with their development items;
  issue-47 items marked); every contrast is reported on all 50 and excluding `EXPOSURE_ITEMS`.
- **Calibration.** The outcome of `calibrate.py` on the v9-a evidence (proposals, screens, replay, admission,
  actor runs ≤ 20) and the expected proposals / admissions per block.
- **Replay constants.** 80 jobs (support episodes first), 4 workers, 20 s per job, 300 s wall, ≥ 5 source jobs
  exit 0, ≤ 4 replays per session (USAGE.md-only proposals excluded), ≤ 30 `try_tool` calls, 3 GB memory cap.

## 9. Metrics

Strict `reward.py` (tolerance 0) on the 50 questions, the same sign required on the 45 undisputed; 1 %
tolerance secondary; per block prequential; paired discordant counts per named contrast; learner rounds
(proposals by kind, screen rejections by reason, replay stats, verification runs by role, admitted revisions
with tier and files); manifest-tool invocation and friction counts per arm; cost. Pro-133 is the
**development** set; confirmation on unseen questions follows this round.
