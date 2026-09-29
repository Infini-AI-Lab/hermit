# Review of DESIGN.md draft 1: generality beyond OfficeQA

Reviewer lens: can the same evolver and runtime (L2–L4, admission by replay) be reused on SearchQA (EM = exact match on the extracted answer), SpreadsheetBench (run the code, compare cells), OfficeQA and DocVQA (ANLS = Average Normalized Levenshtein Similarity, a 0–1 string-closeness score), with only a thin benchmark-specific layer? Read-only review. No answers or graders were opened.

## Verdict

The mechanisms carry over as ideas, not yet as code. The design builds OfficeQA into the three interfaces the evolver writes against: the ledger schema, the checker signature and the answer channel. A checker or policy learned on OfficeQA therefore could not even be loaded on another benchmark. DESIGN §6 leaves the second benchmark until "after OfficeQA". Too late: the boundary costs one module now and a rewrite after build step 3.

Evidence that the ideas carry over (my tally of `per-question/*.json`, first-choice remedy for each failed run): 76 remedies are document-QA-general, 22 are domain-general and 5 are OfficeQA-specific. By type: CHECK 36/5/0, CONTROL 10/13/0, TOOL 26/2/3.

## 1. Which parts carry over and which are OfficeQA-specific

| Part | Status | Where it belongs |
|---|---|---|
| L0 fixes, read-root narrowing, truncation/compaction config | Portable. It is tied to Codex + macOS Seatbelt, not to OfficeQA | core. Rename `.officeqa/` to `.harness/`; the pack declares extra dependencies (openpyxl + LibreOffice, OCR), which are hashed |
| L1 `find_tables`, `show_table`, `printings`, table index | OfficeQA-specific | pack |
| L1 `calc` with automatic records; L2 ledger, `submit`, compaction splice; L3 loop guard, wrap-up, fallback; L4 runner; L5 triggered notes | The mechanisms are portable. The ledger schema (period, printing, r/p/`Y?` flags) and the L3 constants (180 s of 3,600 s) are OfficeQA-specific | core, with a neutral schema (P1) and thresholds as fractions of the pack budget |
| `answer_count`, `calc_provenance`, `cell_exists` | Portable as count and grounding checks. The question parser is regex tuned on the 133 questions (replay.md §12) | core, using the pack's `question_spec` and normaliser |
| `flagged_cells`, `later_printing`, `period_match` | OfficeQA / time-series specific | pack |
| Admission leak screen (numbers only), binary labels, issue-47, `printings` window, `reward.py` | The skeleton is portable; these specifics are not | pack hooks (P4) |

## 2. Problems, ranked by threat to the generality goal

**P1 (critical): the checker API is bound to an OfficeQA ledger.** Four of the six seed checkers read table-cell fields. Evolved checkers will copy that pattern, then crash or silently do nothing elsewhere, so "same evolver" means nothing.
*Fix:* use a neutral event schema with four kinds:
- `observe{ref, locator, text, value?, attrs}`
- `compute{ref, code_sha, inputs:[ref], output}`
- `candidate{answer | artifact_sha, note}`
- `artifact{path, sha, summary}`

Pack-specific attributes get a namespace (`oq.period`, `oq.printing`, `xl.range`). Each checker declares `REQUIRES = {attrs, answer_kinds}`. The runner skips a checker whose requirements are unmet and logs `n/a`, not `pass`. Split the seeds into two folders:
- `core/checkers/`: `answer_count`, `grounded`, `recompute_final`, `competing_candidates`
- `packs/officeqa/checkers/`: `flagged_cells`, `later_printing`, `period_match`

Admission tags every evolved checker as core or pack.

**P2 (critical): the answer channel only handles text.** Three parts assume the answer is a string: `submit "<answer>"`, the rule that the final message must equal the last submitted answer, and `fallback_answer`. SpreadsheetBench's answer is a file (`output.xlsx`). SearchQA and DocVQA answers come from `<answer>` tags (SkillOpt `envs/*/rollout.py`).
*Fix:*
- `submit` takes either `--answer TEXT` or `--artifact PATH`. For an artifact it stores a copy plus its hash.
- The pack provides `extract_final(final_msg, ws)` and `canonical(x)`.
- The submit check compares canonical forms.
- The timeout fallback restores the last stored copy.

**P3 (high): the code layout has no pack boundary.** The plan is to copy v8 plumbing into v9 with `.officeqa/`, `.officeqa-ledger`, provider `officeqa_fireworks`, issue-47 inside admission, and OfficeQA tool defaults inside `policy.json`.
*Fix:* lay the code out as `core/` + `packs/officeqa/` + `packs/toy/`. Add a test that `core` imports nothing from `packs`. Keep the learner prompt in core and generic, with a pack task description of at most 1 KB, so the OfficeQA failure taxonomy (vintage, period) does not leak into it. The toy pack (10 synthetic arithmetic-QA items) runs build steps 2–5 end to end. The pack protocol:
```
items(split); prepare(root); workspace(item, ws); tools {name: cli}; runtime_deps
answer_kind: text|artifact; extract_final; canonical
question_spec(q) -> {n_values, value_type, format}
score(item, answer) -> float; is_right(score) -> bool | None
leak_screen(text, released_refs); policy_schema (tool params + allowed ranges); exclusions
```
Match this to the user's `MetaHarnessAdapter-next/docs/adapter-contract-v0.md`. That contract has three exports: Context, Tools and Agent Definitions. Add a fourth, **Controls** (`policy.json` + checkers), consumed by the framework-owned broker. Without it, evolving mechanisms sits outside the adapter framework.

**P4 (high): admission assumes numeric answers.** The leak screen (a check that learned code or notes do not contain released reference answers) catches numbers only. It misses entity strings (SearchQA), document strings (DocVQA) and golden cell values (SpreadsheetBench). Right/wrong labels are binary, which does not fit ANLS.
*Fix:*
- The pack provides `leak_screen` for its answer type. For text, flag a normalised reference substring of 4 or more characters. For artifacts, flag any golden-range value or task id that appears in checker source.
- `is_right` for ANLS: 1.0 counts as right, below 0.5 counts as wrong, and the band in between is left out of admission.
- Write the thresholds as rates with minimum counts.

**P5 (medium-high): replay only reads the stored ledger.** For benchmarks whose answer is a file, a stronger replay exists: re-run `solution.py` on the input and re-derive the observations exactly, offline.
*Fix:* add an optional pack hook `replay(episode) -> ledger`. The default is the stored ledger.

**P6 (medium): how much the mechanisms can do depends on episode length, so the claim must be pre-registered per benchmark.** SkillOpt runs SearchQA and DocVQA with `max_turns: 1` (`configs/{searchqa,docvqa}/default.yaml`), and SpreadsheetBench with 30 turns. Under Codex, SearchQA and DocVQA become episodes of 1–3 commands: no loops, no compaction, so L2 and L3 sit idle.
*Fix:* pre-register the predictions:
- mech-evolve beats prompt-evolve on OfficeQA and SpreadsheetBench.
- It is no worse than prompt-evolve on SearchQA and DocVQA, within a margin of −2 points (a non-inferiority test).

Report per-layer activity (loop notes, gate rounds, checker `n/a` rate) so a null result can be read.

**P7 (medium): developers could again tune on the test set.** In OfficeQA, the L1 tools and the question parser were tuned on the 133 test questions (critic R1).
*Fix:* for any new pack:
- Build it from the train split only, then freeze and hash it before any test episode.
- Record the pack's lines of code and developer hours.

SpreadsheetBench already has this problem: `baseline-work/results/spreadsheet-analysis.md` analysed 50 items after scoring. Of those, 36 fall in SkillOpt's test split (8 in train, 6 in val). Exclude those 36 from scoring.

## 3. Which second benchmark, and the smallest experiment that supports the claim

| | SpreadsheetBench | SearchQA | DocVQA |
|---|---|---|---|
| Data on disk | **Yes.** `baseline-work/data-cache/spreadsheetbench_verified_400.tar.gz`; LibreOffice formula recalculation and a pinned scorer already work | No: Hugging Face download (network, needs user OK) | No: download plus images |
| Episode shape | Multi-turn, writes code, answer is a file | One turn, context given | One image; GLM image input through Codex is unverified |
| GLM-5.3-flash evidence | 43/50, repeat 44/49, 3/49 flips (`baseline-work`, custom non-Codex runner, about $0.39 per 50 tasks) | None | None |
| Room for mechanisms | High: 6/50 runs used up the call budget; failures look mechanical (17-35 header written one row down, 45944 names written in the wrong order) | Low | Medium; needs an OCR engine dependency |

**Recommendation: SpreadsheetBench next.** It is the cheapest option (no download; scorer and recalculation already built). It is also the most convincing: a different domain, execution-verified answers, and failure types a mechanism can catch. SearchQA comes third, as a control where text should do well. DocVQA comes last.

Minimal experiment:
- **Items.** Use SkillOpt's split (80 train / 40 val / 280 test). Build the pack on the 80 train items only. The scored stream is the 280 test items minus the 36 already analysed, so 244. Report cell-level and sheet-level tasks separately (the baseline scored 34/37 cell-level and 9/13 sheet-level).
- **Pack** (target ≤ 600 lines of code):
  - `sheet_view`: a grid with coordinates, formulas, merged cells and detected header rows.
  - `run_solution`: run the solution on a copy, recalculate in LibreOffice, dump the answer cells and diff against the input.
  - Plus core `calc`.
- **Seed pack checkers** (written from the train split only):
  - `target_empty`: answer cells are empty after recalculation.
  - `uncached_formula`: a formula has no computed value.
  - `write_outside_target`: cells outside the answer range changed.
  - `header_offset`: the first written row is an existing header row.
  - `hardcoded_literals`: values copied from the input appear as constants in `solution.py`.
- **Arms.** prompt-evolve vs mech-evolve, 3 campaigns each, plus `base` once. Same block protocol, learner, statistics and core as OfficeQA.
- **Acceptance rules.** The core changes by 0 lines between benchmarks, and the learner prompt is byte-identical.
- **Power.** 3/49 flips is about 6%, so roughly 15 items would come out different between two identical runs on 244. That gives an SD of about 3.9 items per repeat and about 2.2 over 3 repeats, so about +6 items is detectable (a lower bound on the noise: learned adapters add campaign-to-campaign variance). That is about 1/5 of the roughly 30 expected failures.
- **Ceiling guard.** If `base` under Codex scores 90% or more on the train split, switch to the original 912-task SpreadsheetBench. There, each solution must also work on 2 workbooks the model never sees. That needs a download and a user decision.
- **Free cross-benchmark test.** Replay the core checkers evolved on OfficeQA over the SpreadsheetBench episodes and report which pass admission unchanged.

The generalisation claim would then rest on: one core hash across both benchmarks, the pack's size, the effect with its CI on each benchmark, and the core-vs-pack tags of the evolved checkers.

## Build-order changes

Step 1 adds the core/pack split and the toy pack. Step 3's runner supports `REQUIRES`/`n/a`, and `submit` supports artifacts. Step 4's admission calls the pack hooks (`is_right`, `leak_screen`, `replay`). Step 5's `freeze.json` hashes core and pack separately.
