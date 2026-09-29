# Review lens: will v9 actually move GLM-5.3-flash accuracy?

Inputs: DESIGN.md draft 1, FINDINGS.md, 54 `per-question` + 17 `reverse` analyses, replay.md, critic.md. No answers
or graders opened. "Arm-run" = one arm's pass over 133 questions; all estimates are per arm-run. FY/CY = fiscal/calendar
year; CPI = Consumer Price Index.

## Verdict

- **Expected gain of `mech-static` over `base`: about +4 to +6 questions (net).** About 60% of that comes from the L3
  loop guard and the wrap-up, which the replay supports. The six seed checkers add roughly +1 to +3, depending on two
  things the design does not yet secure: whether the ledger links the answer to cells, and whether GLM acts on the findings.
- **The evolver adds about 0 to +1.5 on top.** The noise is large: a single-run paired SD is about 6, and the critic
  estimates power of about 0.3 for a 5-question gap even with 3 repeats. So the pre-registered contrast
  (`mech-evolve` vs `prompt-evolve`) will most likely come out null. Even a significant result would mostly reflect the
  static machinery, not the evolver.
- **About a third (≈35/103) of the analysed failures get no L2–L4 mechanism:** EXTERNAL 11, ~15 of 20
  INTERPRETATION, SOURCE_TABLE 9 (L1 search only).

## Per-mechanism estimate

"Conversion" means the ledger links the answer to cells (assumed 0.5–0.7) × GLM acts correctly on the finding (assumed 0.3–0.5).

| Mechanism | Analysed failures it can plausibly flip (of 103) | Net Δ / arm-run | Risk to right answers |
|---|---|---|---|
| L0 environment (numpy, heredoc, `python3`, `rg`) | Q052, Q115 (no numpy, hand-written math), plus less wasted budget | +0.5–1 (all arms) | ~0 |
| L1 tools | ~35 addressable: label/column slides ~17, first print vs latest 7, as-of dates 6, table search 6 | +3–4 if adopted (shared; not in contrast) | low (repairs flagged) |
| L3 loop guard | 27 severe loops (26 caught) | +2–3.5 (perfect ≈+5, critic R6) | "submit now" escalation can commit an early candidate: −0.5 (12/181 right runs fire) |
| L3 wrap-up (<180 s left) | 13 empty answers (all were loops) | +0.5–1 | 0 |
| L2 ledger splice after compaction | 71/72 compacted runs are loop runs | +0–0.5 once loops are fixed | ~0 |
| `answer_count` | Q015 pr, Q019 no, Q114 no | +0.5 | 0/181 right runs fire |
| `calc_provenance` | arithmetic slips: Q058, Q080 pr | +0.3–0.7 | fires on 16% of right runs (harmless if it only re-runs `calc`) |
| `cell_exists` | mostly the 11 CPI recalls, **unfixable without data** | ≈0 or negative (nag → corpus searching) | medium |
| `flagged_cells` | Y?/identity/repair flags: Q011×3, Q017×2, Q098×3, Q123, Q088, Q043, Q064 (≈12) | +0.5–1 | **high if `r`/`p` flags trigger it**: they are on most post-2001 cells |
| `later_printing` | 7 first-print-vs-latest: Q013 pr, Q081 ca, Q089 no, Q095×2, Q120 ca, Q124 pr | +0.3–0.8 | Q086×2, Q017 pr go the other way; with no stated default it is a coin flip |
| `period_match` | ≈15: Q009, Q014×2, Q018, Q026×2, Q030, Q079, Q084, Q089×2, Q102, Q107, Q122, Q130 | +1–1.5 (largest content class) | Q124-type keys (FY rows despite "calendar years") |
| Scoped notes | — | ≈0 (text was fully followed 2/32 times) | cards text contributed to 16/39 of its failures |
| Mechanism evolver | — | 0 to +1.5 | not measured |

## Problems, ranked by threat to the goal

### P1. The measurable effect sits in static L3, is small next to the noise, and partly repairs a sampling defect

- The loops are a temperature-0 fixed point. In 4,117 of 4,172 back-to-back repeats the reasoning was byte-identical,
  and 29 of the 36 streaks ended only at a compaction.
- Temperature 0 buys no reproducibility: the rerun flip rate is already ~27%, and three temperature-0 baselines on the
  same 50 questions scored 26, 19, 22.
- So the mechanism arms' main advantage may be repairing a defect that one config line could fix for everyone.

**Fix.**
1. Make actor temperature an all-arm L0 choice, set by a pilot: {0, 0.6 or the model card's value} × `base` × 20
   loop-prone questions (~$5–10). Keep the loop guard for alternating cycles and the 39 "mild thrash" runs.
2. Decide which claim you are paying for:
   - **"Machinery helps"**: `mech-static` vs `base`, about +4–6. Power is ≈0.3 at 3 repeats and ≈0.5–0.7 at 6.
     Neither arm has a learner (~$30 per run at v8 prices), so 6 × 2 runs ≈ $360, inside the planned $350–450.
     This one is testable.
   - **"Mechanism evolution beats text evolution"**: give `prompt-evolve` the frozen W0 L2–L4 too, so that only the
     editable surface differs. The expected gain is then about 0–2, which is not testable at 3 repeats.
   - Do not report the current contrast as evidence for the second claim.

### P2. The content checkers only see what the model chose to look at

- `later_printing` needs a `printings` record the model produced itself; Q081 none never looked past 2003, so it
  stays silent.
- The other checks key on "calc inputs matched to shown cells". In v8, 28% of submitted answers never appeared in any
  calc output (mental arithmetic), and `officeqa.py read` (used in 130/133 v8 runs) and `sed` write nothing to the ledger.

**Fix: a checker SDK with read-only index access.**
- `cells_for(value, files_touched)` uses `analyze()`, so period, column, flags and identity come from the index.
- `printings_of(cell)` is precomputed or cached, with a gate budget of about 10 s instead of 2 s per checker.
- `question_periods()`.
- Log every shell command via `$ZSH_EXECUTION_STRING` in the L0 `zdot/.zprofile`, so that `files_touched` covers `sed`
  and `grep`. Make `officeqa.py read` log too.
- Match the **candidate itself** and the `--note` numbers, not only calc inputs, so checkers fire on what the answer
  *is*, not on how the model got there.

### P3. The feedback asks a weak model to "confirm / state why"

- Text was fully followed 2/32 times; 85/103 failures already showed doubt at the fork (right runs too). "Confirm with
  printings" adds one more doubt, and advice like cards' overreach can flip right answers.

**Fix: the gate does the work and GLM only chooses.**
- Re-execute the recorded `calc` code with the substituted cell (later printing, correctly labelled period, unscaled
  value), and show the resulting alternative number.
- Wording template, ≤4 lines, one finding per round, highest-precision checker first:
  > `Your answer uses FFO-3 2003_06:712 [2003-01 | GSA]. The latest printing (2004_03:745) differs. Re-running your calc
  > with it gives <alt>. The question names no publication date; task convention: latest printing.`
  > `Accept: submit "<alt>"` · `Keep: submit "<ans>" --keep "<reason>"`
- Never "are you sure" / "double-check". Accept a keep with a reason; never raise the same finding twice.
- The convention "use the most up-to-date revision unless the question specifies an exact date" is the **published
  OfficeQA paper rule**, not a rule derived from the keys.
  - Put it in the all-arm README as task specification (user decision; answers critic G7), and let mech arms
    *enforce* it via `later_printing`: exactly the text-vs-mechanism difference under study.
  - Analysed runs: 7 fixes vs ~3 harmed; 4 reverse right answers (Q004, Q020, Q078, Q111) came from this rule.
- Loop notes should use the same form: the command, "output identical", the ledger candidates with the calc id, and
  (1) `submit "<A>"` or (2) a command not yet run. Escalate to "submit now" only if a candidate exists that was computed
  after the last new evidence.

### P4. The evolver cannot learn anything useful with this admission test

- Admission requires "≤1 and ≤10% of right episodes", which allows 0 right fires until there are about 10 right
  episodes (around block 5).
  - 0 of R right episodes only bounds the false-fire rate at about 3/R: 30% at R=10.
  - A 10% bound needs R≈30, around block 13.
  - A checker admitted then acts on at most about 70 questions: ≈70 × 0.53 (wrong) × 0.1 (fires) × 0.3 (fixed) ≈
    **+1 question**.
- Early on, the only checkers that pass are narrow keyword matches on the named question. The leak screen blocks
  constants, not question phrases.
- The learner is GLM-5.3-flash (v8: file tools failed 100%, a 231 KB document). Free-form 4 KB Python checkers will
  mostly be broken or overfit.

**Fix.**
1. **Counterfactual admission (zero model calls).**
   - On a wrong episode, the checker's proposed substitution, re-run through the recorded calc, must reproduce the
     released reference (the evidence was on screen in 97/103 failures). With the candidate set to the reference, the
     checker must stay silent. This tests whether it *points the right way*, not only whether it detects.
2. **Decision-point replay.** Re-send the episode's final request plus the gate text, 2 samples, to measure
   wrong→right and right→wrong flips. That is 1–2 GLM calls per episode (roughly $1–3 per candidate checker). It
   directly answers the "detection is not effect" limitation.
3. **Restrict edits to a template language over the SDK**, for example
   `when cell.flag in {...} and question matches /.../ then offer alt(...)`, plus knobs and message templates. A GLM
   learner can write these, and overfitting is visible in the trigger.

### P5. Failure classes the design leaves untouched

- **EXTERNAL (11, CPI).** "None for all arms" freezes about 3 or 4 failures per arm-run. Six of them (Q077×3, Q106×3)
  had the right formula and wrong recalled index values.
  - Fix: ship the public BLS CPI-U monthly table (U.S. Bureau of Labor Statistics urban CPI, ~30 KB) to all arms in
    L0: ~+3 per arm-run everywhere, neutral for the contrast. If declined, mute `cell_exists` on CPI/inflation questions.
- **Percent vs fraction scale (Q044×2, Q049×3).** Add a `scale` checker: the candidate is 100× a calc output and the
  question does not say percent, so offer the unscaled value.
  First count right v8 runs of this shape (zero-cost replay over `aggregated_output`).
- **Interpretation forks** (Q039, Q044 pr, Q049 pr, Q064 pr and Q114 ca computed or stated the right reading, then
  dropped it).
  - Add `submit "<a>" --alt "<b>" --reading "..."`. The gate shows both values next to the quoted question words
    that decide between them, and applies a fixed tie-break order: literal label match, then published conventions,
    then no rescaling.
- **SOURCE_TABLE (Q101×2, Q109×2, Q133×2, Q080 ca).** Add a `table_alternatives` checker: other tables in the same
  issue whose title or headers match the question's nouns better than the table used (FTS rank; FTS = SQLite full-text
  search).
- **Omissions from hand-typing (Q081 none, Q082 cards).** Add a `row_coverage` checker that lists unused cells of the
  cited row, with their headers.

### P6. Right-answer breakage is not budgeted

- Drop the `r`/`p` flags as triggers for `flagged_cells`; revisions are `later_printing`'s domain. Keep only
  repair, `Y?` and identity failures.
- Submit enforcement: compare answers numerically after normalisation. Re-query only when `submit` was never called,
  because a weak model told "call submit first" can reopen the investigation.
- Pre-register gate limits on right episodes: the union fire rate should stay ≤20%, and right→wrong flips after
  feedback ≤5% of fired right episodes. Disable the lowest-precision checker until both hold.

## Pilot (step 7): change what it measures

Run 20 questions × {`base`, `mech-static`}, not `plain`. `plain` mixes L1 into the comparison.

Add P1's temperature cell and decision-point replays over every pilot submit. Go only if:
- ledger coverage (the answer traces to indexed cells) is ≥60%;
- the fire rate on wrong episodes is ≥2× the rate on right episodes;
- wrong→right flips exceed right→wrong flips by ≥3:1.

Otherwise fix P2 and P3 before spending money on about 9 campaigns.
