# v9 understand: offline replay of candidate commit-gate checkers over the v8 runs

Date: 2026-09-28. Zero-cost, deterministic replay of four candidate checkers over all 399 v8 arm-runs (3 roots x 133 questions). Labels: `officeqa-divergence-analysis/index.json` only. This report contains no answer values (question ordinals, flags, counts and relative distances only).

## TL;DR

- **loop_detect is the only checker that clearly separates wrong from right runs**: precision 84% (95% CI 74–91) vs a base rate of 55%, recall 29% (64 of 218 wrong runs; 12 right runs also fire). What it detects is a temperature-0 repetition collapse (same reasoning + same command + same output, dozens to hundreds of times) that typically ends only when Codex auto-compacts the context (71/76 firing runs compact after the firing point). It is, however, mostly a proxy for "this run is long": within the top command-count quartile, looping runs are wrong 85% vs 76% for non-looping ones. Its value for v9 is as an early *control* trigger (median first firing at command 26, i.e. 20% into the run), not as an answer verdict.
- **format_check has almost nothing to catch.** GLM followed the asked rounding in every run where the decimals check applied. Stray units/words/`$` fire as often on right runs as on wrong ones (the grader evidently extracts the number), so they are cosmetic here. Without them the only flags that fire on wrong runs are EMPTY (timeouts, 13 runs) and COUNT (2 multi-number answers): precision 94% but recall 7% over all runs. As specified (with the cosmetic flags) it is at the base rate on submitted answers: precision 49%, recall 12%.
- **provenance_check does not separate**: precision 54% vs base 53% on submitted answers (recall 17%). Right answers are often typed from 2–3-number mental arithmetic or a hand unit conversion; wrong answers mostly use numbers that *did* appear in outputs (wrong cell / wrong vintage), which a provenance check cannot see. No variant (model-visible outputs only, rescaling-tolerant, echo-aware, calc-output-only) moves precision off the base rate.
- **doubt_unresolved is noise**: almost every run (right or wrong) has doubt words in its last 10 reasoning notes, and the "no command after the last doubting note" rule has precision 57% vs base 55%. A keyword-addressed variant is more precise but catches only a handful of runs.
- **Union (a)+(b)+(c)**: precision 66%, recall 49% (106 wrong / 54 right fire); with cosmetic format flags dropped: precision 71%, recall 42%. Almost all of the union's lift comes from loop_detect and empty answers.
- **Implication for v9**: a commit gate built from these three checks would bounce about as many right answers as wrong ones (except for loops and empties), so L4 needs *content* checkers (period/label match, vintage conflict, parts-sum-to-total) that these trajectories cannot score without the ledger. The replay argues for spending L3 on the loop guard (inject a perturbing nudge at the 3rd identical command+output; 11,308 of 21,508 v8 commands (53%) were the 3rd-or-later issue of an identical command with an identical output) and submit-best-candidate at timeout, and against bouncing answers for units/`%`/commas.

## 1. Data and method

- Runs: 399 = 3 roots (`officeqa-pro133-{none,prompt,cards}-v8-work/runs/{baseline,adapted,adapted}/NNN`) x 133. Question from each run's `task.json`; submitted answer from `summary.json` `answer` (= stripped `final.md` for completed runs, empty otherwise: `officeqa-pro133-v8-plumbing/campaign.py:134`).
- Labels per arm (index.json `correct`): none 62 right / 71 wrong, prompt 64 right / 69 wrong, cards 55 right / 78 wrong. Empty answers (all timeouts, all wrong): none 4, prompt 5, cards 4.
- Two populations: **all runs** (399) and **submitted answers** (non-empty, 386), the population a commit gate would actually see.
- Trajectory: `records/events.jsonl`. Commands are taken in issue order from `item.started` and joined to `item.completed` for `aggregated_output` (`replay/load.py:57`). `aggregated_output` is the *full* output; the model saw a Codex-truncated copy (~10 KB, head+tail with a "…N chars truncated…" marker). The model-visible copy is reconstructed from the `function_call_output` items of the compaction requests plus the final request (`replay/load.py:127`).
- Compaction marker: Codex writes an `error` item "Heads up: Long threads and multiple compactions …" after each auto-compaction (auto-compact limit 160,000 tokens, `officeqa-pro133-v8-plumbing/acting.py:284`). Verified against the API log: in 71 of 72 runs with markers the marker count equals the number of tool-less (`"tools": []`) compaction requests; mismatches: prompt Q071 (7 markers vs 8 tool-less requests) (a compaction still in flight at the wall-clock kill).
- The broker forces `temperature=0` on every request (`officeqa-pro133-v8-plumbing/broker.py:90`); this matters for (c).

Checker definitions (code in `officeqa-v9-design/understand/replay/`):

| checker | fires when | code |
|---|---|---|
| (a) format_check | question-spec parser (count of values, brackets, per-slot decimals incl. "first value … second value", significant digits, decimal-vs-percent form, no-commas, "how many" ⇒ integer, "which year", "absolute") vs. a tokenised answer; fires on any of: EMPTY, COUNT, NO_BRACKETS, DEC_LONG, DEC_SHORT, SIGFIG, PCT_SIGN_DECIMAL_ASKED, PCT_SIGN_FORBIDDEN, DECIMAL_FORM_MAGNITUDE, STRAY_TEXT, STRAY_UNIT, COMMAS_FORBIDDEN, YEAR_EXPECTED, NEGATIVE_ABSOLUTE, NON_NUMERIC. (a\*) drops the cosmetic STRAY_TEXT/STRAY_UNIT. THOUSANDS_SEP, EXTRA_BRACKETS, STRAY_PERCENT are reported but never fire the checker. | `checkers.py:174` (spec), `:67` (answer), `:277` (check) |
| (b) provenance_check | some number in the answer has no output number within half a unit of the answer's last typed decimal (sign-insensitive; thousands separators and CSV pieces parsed) in the full `aggregated_output` of any executed command | `checkers.py:404` |
| (c) loop_detect | some whitespace-normalised command string is issued for the 3rd time; reports first index, repeats, longest identical streak, and whether a compaction happened at/after the firing point | `checkers.py:463` |
| (d) doubt_unresolved | the last 10 reasoning notes contain maybe/unclear/not sure/ambiguous/alternatively/should we, and no command was executed after the last such note | `checkers.py:517` |

## 2. Headline: all 399 runs

| checker | arm | fired on wrong (= recall) | fired on right | precision P(wrong\|fired) [95% CI] | recall P(fired\|wrong) | base P(wrong) | lift |
|---|---|---|---|---|---|---|---|
| (a) format_check | none | 16/71 (23%) | 12/62 (19%) | **57%** [39–73] | 23% | 53% | 1.07 |
|  | prompt | 7/69 (10%) | 3/64 (5%) | **70%** [40–89] | 10% | 52% | 1.35 |
|  | cards | 15/78 (19%) | 11/55 (20%) | **58%** [39–74] | 19% | 59% | 0.98 |
|  | ALL | 38/218 (17%) | 26/181 (14%) | **59%** [47–71] | 17% | 55% | 1.09 |
| (a\*) format_check, cosmetic flags dropped | none | 5/71 (7%) | 0/62 (0%) | **100%** [57–100] | 7% | 53% | 1.87 |
|  | prompt | 6/69 (9%) | 0/64 (0%) | **100%** [61–100] | 9% | 52% | 1.93 |
|  | cards | 4/78 (5%) | 1/55 (2%) | **80%** [38–96] | 5% | 59% | 1.36 |
|  | ALL | 15/218 (7%) | 1/181 (1%) | **94%** [72–99] | 7% | 55% | 1.72 |
| (b) provenance_check | none | 15/71 (21%) | 11/62 (18%) | **58%** [39–74] | 21% | 53% | 1.08 |
|  | prompt | 10/69 (14%) | 11/64 (17%) | **48%** [28–68] | 14% | 52% | 0.92 |
|  | cards | 9/78 (12%) | 7/55 (13%) | **56%** [33–77] | 12% | 59% | 0.96 |
|  | ALL | 34/218 (16%) | 29/181 (16%) | **54%** [42–66] | 16% | 55% | 0.99 |
| (c) loop_detect | none | 18/71 (25%) | 4/62 (6%) | **82%** [61–93] | 25% | 53% | 1.53 |
|  | prompt | 22/69 (32%) | 3/64 (5%) | **88%** [70–96] | 32% | 52% | 1.70 |
|  | cards | 24/78 (31%) | 5/55 (9%) | **83%** [65–92] | 31% | 59% | 1.41 |
|  | ALL | 64/218 (29%) | 12/181 (7%) | **84%** [74–91] | 29% | 55% | 1.54 |
| (d) doubt_unresolved | none | 45/71 (63%) | 33/62 (53%) | **58%** [47–68] | 63% | 53% | 1.08 |
|  | prompt | 56/69 (81%) | 41/64 (64%) | **58%** [48–67] | 81% | 52% | 1.11 |
|  | cards | 40/78 (51%) | 33/55 (60%) | **55%** [43–66] | 51% | 59% | 0.93 |
|  | ALL | 141/218 (65%) | 107/181 (59%) | **57%** [51–63] | 65% | 55% | 1.04 |
| (a)+(b)+(c) union | none | 37/71 (52%) | 22/62 (35%) | **63%** [50–74] | 52% | 53% | 1.17 |
|  | prompt | 30/69 (43%) | 15/64 (23%) | **67%** [52–79] | 43% | 52% | 1.29 |
|  | cards | 39/78 (50%) | 17/55 (31%) | **70%** [57–80] | 50% | 59% | 1.19 |
|  | ALL | 106/218 (49%) | 54/181 (30%) | **66%** [59–73] | 49% | 55% | 1.21 |
| (a\*)+(b)+(c) union | none | 31/71 (44%) | 13/62 (21%) | **70%** [56–82] | 44% | 53% | 1.32 |
|  | prompt | 30/69 (43%) | 14/64 (22%) | **68%** [53–80] | 43% | 52% | 1.31 |
|  | cards | 30/78 (38%) | 11/55 (20%) | **73%** [58–84] | 38% | 59% | 1.25 |
|  | ALL | 91/218 (42%) | 38/181 (21%) | **71%** [62–78] | 42% | 55% | 1.29 |

## 3. Submitted answers only (the commit-gate population)

| checker | arm | fired on wrong (= recall) | fired on right | precision P(wrong\|fired) [95% CI] | recall P(fired\|wrong) | base P(wrong) | lift |
|---|---|---|---|---|---|---|---|
| (a) format_check | none | 12/67 (18%) | 12/62 (19%) | **50%** [31–69] | 18% | 52% | 0.96 |
|  | prompt | 2/64 (3%) | 3/64 (5%) | **40%** [12–77] | 3% | 50% | 0.80 |
|  | cards | 11/74 (15%) | 11/55 (20%) | **50%** [31–69] | 15% | 57% | 0.87 |
|  | ALL | 25/205 (12%) | 26/181 (14%) | **49%** [36–62] | 12% | 53% | 0.92 |
| (a\*) format_check, cosmetic flags dropped | none | 1/67 (1%) | 0/62 (0%) | **100%** [21–100] | 1% | 52% | 1.93 |
|  | prompt | 1/64 (2%) | 0/64 (0%) | **100%** [21–100] | 2% | 50% | 2.00 |
|  | cards | 0/74 (0%) | 1/55 (2%) | **0%** [0–79] | 0% | 57% | 0.00 |
|  | ALL | 2/205 (1%) | 1/181 (1%) | **67%** [21–94] | 1% | 53% | 1.26 |
| (b) provenance_check | none | 15/67 (22%) | 11/62 (18%) | **58%** [39–74] | 22% | 52% | 1.11 |
|  | prompt | 10/64 (16%) | 11/64 (17%) | **48%** [28–68] | 16% | 50% | 0.95 |
|  | cards | 9/74 (12%) | 7/55 (13%) | **56%** [33–77] | 12% | 57% | 0.98 |
|  | ALL | 34/205 (17%) | 29/181 (16%) | **54%** [42–66] | 17% | 53% | 1.02 |
| (c) loop_detect | none | 14/67 (21%) | 4/62 (6%) | **78%** [55–91] | 21% | 52% | 1.50 |
|  | prompt | 17/64 (27%) | 3/64 (5%) | **85%** [64–95] | 27% | 50% | 1.70 |
|  | cards | 20/74 (27%) | 5/55 (9%) | **80%** [61–91] | 27% | 57% | 1.39 |
|  | ALL | 51/205 (25%) | 12/181 (7%) | **81%** [70–89] | 25% | 53% | 1.52 |
| (d) doubt_unresolved | none | 45/67 (67%) | 33/62 (53%) | **58%** [47–68] | 67% | 52% | 1.11 |
|  | prompt | 56/64 (88%) | 41/64 (64%) | **58%** [48–67] | 88% | 50% | 1.15 |
|  | cards | 40/74 (54%) | 33/55 (60%) | **55%** [43–66] | 54% | 57% | 0.96 |
|  | ALL | 141/205 (69%) | 107/181 (59%) | **57%** [51–63] | 69% | 53% | 1.07 |
| (a)+(b)+(c) union | none | 33/67 (49%) | 22/62 (35%) | **60%** [47–72] | 49% | 52% | 1.16 |
|  | prompt | 25/64 (39%) | 15/64 (23%) | **62%** [47–76] | 39% | 50% | 1.25 |
|  | cards | 35/74 (47%) | 17/55 (31%) | **67%** [54–78] | 47% | 57% | 1.17 |
|  | ALL | 93/205 (45%) | 54/181 (30%) | **63%** [55–71] | 45% | 53% | 1.19 |
| (a\*)+(b)+(c) union | none | 27/67 (40%) | 13/62 (21%) | **68%** [52–80] | 40% | 52% | 1.30 |
|  | prompt | 25/64 (39%) | 14/64 (22%) | **64%** [48–77] | 39% | 50% | 1.28 |
|  | cards | 26/74 (35%) | 11/55 (20%) | **70%** [54–83] | 35% | 57% | 1.22 |
|  | ALL | 78/205 (38%) | 38/181 (21%) | **67%** [58–75] | 38% | 53% | 1.27 |

## 4. (a) format_check in detail

| flag | counted in (a)? | fired wrong | fired right | precision | per arm (wrong/right) |
|---|---|---|---|---|---|
| COUNT | yes | 2 | 0 | 100% | none 1/0, prompt 1/0, cards 0/0 |
| EMPTY | yes | 13 | 0 | 100% | none 4/0, prompt 5/0, cards 4/0 |
| STRAY_TEXT | yes, dropped in (a\*) | 22 | 26 | 46% | none 11/12, prompt 2/3, cards 9/11 |
| STRAY_UNIT | yes, dropped in (a\*) | 10 | 9 | 53% | none 6/5, prompt 1/0, cards 3/4 |
| YEAR_EXPECTED | yes | 0 | 1 | 0% | none 0/0, prompt 0/0, cards 0/1 |
| STRAY_PERCENT | no (soft) | 5 | 4 | 56% | none 1/2, prompt 1/0, cards 3/2 |
| THOUSANDS_SEP | no (soft) | 11 | 10 | 52% | none 6/5, prompt 1/2, cards 4/3 |

- The rounding parser extracted a decimals requirement for 115/133 questions. On submitted answers the decimals check applied to 330 runs and **passed in 330**; 51 runs had no rounding instruction to check. GLM essentially never mis-rounds: v8 format failures are not decimals failures.
- Wrong runs by distance bin (index.json `error_bin`, descriptive) and how many (a\*) catches: >25pct 0/94, within_1pct 0/35, 5-25pct 0/34, 1-5pct 0/19, within_0.1pct 0/16, empty 13/13, scale_10^2 0/5, count_mismatch(7 vs 1) 1/1, count_mismatch(2 vs 1) 1/1.
- The five `scale_10^2` wrong runs (Q044 x2, Q049 x3: percent-vs-fraction slips) are **not catchable from the question text**: neither question states a percent/fraction form, so no format rule fires. A scale check needs the evidence ledger (e.g. "answer = 100 x the last calc output" or a unit field on the candidate).
- The two COUNT hits are real: one answer was a full sentence with 7 numbers (prompt Q015), one gave a value plus its percent in parentheses (none Q019); both were graded wrong. This is the one format rule worth keeping as a hard gate, together with EMPTY (which is L3's job: submit the best ledger candidate at timeout).
- STRAY_TEXT / STRAY_UNIT / STRAY_PERCENT / THOUSANDS_SEP fire on right runs as often as on wrong ones (e.g. `$` + "million" suffixes, "percentage points", `%` on a rate): the grader tolerates them. Bouncing such answers would cost a feedback round and risk the model changing a correct number.

## 5. (b) provenance_check in detail (submitted answers)

| variant | none: fired w/r · P · R | prompt: fired w/r · P · R | cards: fired w/r · P · R | ALL: fired w/r · P [CI] · R |
|---|---|---|---|---|
| full outputs (primary) | 15/11 · 58% · 22% | 10/11 · 48% · 16% | 9/7 · 56% · 12% | 34/29 · 54% [42–66] · 17% |
| model-visible outputs only (Codex-truncated) | 16/12 · 57% · 24% | 10/11 · 48% · 16% | 9/7 · 56% · 12% | 35/30 · 54% [42–65] · 17% |
| rescaling-tolerant (x10^±2,3,6,9 allowed) | 9/6 · 60% · 13% | 5/8 · 38% · 8% | 8/4 · 67% · 11% | 22/18 · 55% [40–69] · 11% |
| echo-aware (also fires if only `print(<literal>)` produced it) | 15/12 · 56% · 22% | 10/11 · 48% · 16% | 9/7 · 56% · 12% | 34/30 · 53% [41–65] · 17% |
| calc-output only (python/awk/bc output ≤3 KB, not a corpus dump) | 25/20 · 56% · 37% | 17/17 · 50% · 27% | 15/13 · 54% · 20% | 57/50 · 53% [44–62] · 28% |

Why it does not separate:

- Of the right runs that fire, most typed the answer after doing small arithmetic in the reasoning (sum/difference/mean of 2–3 table cells, e.g. Q002, Q022, Q050, Q094, Q123) or after a hand unit conversion (thousands→dollars Q040, millions→billions/trillions Q027, Q048, Q066): GLM's mental arithmetic on a few numbers is usually right.
- The dominant wrong-run mechanisms in FINDINGS.md (CELL_SELECTION, SOURCE_ISSUE_VINTAGE, INTERPRETATION) produce answers whose numbers *were* printed by some command, so provenance passes. It only sees arithmetic/transcription slips (34 wrong submitted runs fire, several of them in the `within_1pct`/`within_0.1pct` bins, e.g. Q045, Q046, Q050).
- Wrong submitted runs by distance bin, provenance fired / all: >25pct 15/94, within_1pct 1/35, 5-25pct 10/34, 1-5pct 3/19, within_0.1pct 4/16, scale_10^2 0/5, count_mismatch(7 vs 1) 1/1, count_mismatch(2 vs 1) 0/1. Firing is spread across bins rather than concentrated in near misses.
- Visible-vs-full makes almost no difference (the model rarely answers with a number that only existed in a truncated middle). Requiring the number to come from a *calc* output roughly doubles firing on both sides.
- Implication: as a gate it should not block; at most, when it fires, ask the model to re-run the final arithmetic in `calc` (cheap, and harmless when the typed value was right).

## 6. (c) loop_detect in detail

| variant | none: fired w/r · P · R | prompt: fired w/r · P · R | cards: fired w/r · P · R | ALL: fired w/r · P [CI] · R |
|---|---|---|---|---|
| same command 3x (primary) | 18/4 · 82% · 25% | 22/3 · 88% · 32% | 24/5 · 83% · 31% | 64/12 · 84% [74–91] · 29% |
| same command + same output 3x | 18/4 · 82% · 25% | 22/3 · 88% · 32% | 24/5 · 83% · 31% | 64/12 · 84% [74–91] · 29% |
| same command 3x in a row (streak) | 11/3 · 79% · 15% | 8/1 · 89% · 12% | 10/3 · 77% · 13% | 29/7 · 81% [65–90] · 13% |
| same command ≥10x | 18/3 · 86% · 25% | 19/3 · 86% · 28% | 24/5 · 83% · 31% | 61/11 · 85% [75–91] · 28% |
| baseline: any compaction happened | 18/3 · 86% · 25% | 18/4 · 82% · 26% | 24/5 · 83% · 31% | 60/12 · 83% [73–90] · 28% |
| baseline: commands > 31 (top quartile) | 26/5 · 84% · 37% | 25/5 · 83% · 36% | 29/7 · 81% · 37% | 80/17 · 82% [74–89] · 37% |

Loop runs vs run length (all runs, quartiles of command count):

| command-count quartile | loop fired: wrong/right | no loop: wrong/right |
|---|---|---|
| Q1 | 0/0 | 36/64 |
| Q2 | 1/0 | 48/56 |
| Q3 | 2/1 | 51/43 |
| Q4 | 61/11 | 19/6 |

- Fired in 76 runs; first 3rd-issue at median command 26 (20% into the run). The repeated command is issued a median 47 times (max 256); requiring an identical output as well fires on exactly the same runs.
- Compaction: 71/76 fired runs compacted at or after the firing point (60/64 wrong, 11/12 right). All 13/13 empty (timeout) runs are loop runs.
- Mechanism (measured on the 36 runs with an identical-command streak of ≥3): in 4,117 of 4,172 back-to-back repeats the reasoning note before the command was byte-identical too, and the longest streak ended at an auto-compaction in 29 of 36 runs (6 ran until the run ended, 1 broke on its own). With `temperature=0` the context after each repeat differs only by one more identical call/output pair, so the model re-emits the same step until compaction rewrites the context. Alternating cycles (A,B,A,B…) account for the loop runs without a streak.
- It is largely a run-length proxy: a plain "more than 31 commands" rule and "any compaction happened" select almost the same runs at similar precision. The looping itself is what makes these runs long, so the useful question for v9 is whether breaking the loop early recovers accuracy, which only an online run can answer. What the replay does show: 11,308 of 21,508 commands (53%) were the 3rd-or-later issue of an identical command with an identical output (by definition all inside the 76 loop runs, which issued 15,635 commands in total) — pure wasted budget that also pushed the context into compaction (FINDINGS.md: compaction dropped verified intermediates in 26 failed runs).
- Right runs that fire: 11 of 12 recovered after a compaction and answered correctly; the other repeated one command a few times near the end and then answered (see FP examples). A loop guard that injects a nudge rather than aborting should not hurt them.

## 7. (d) doubt_unresolved in detail

| variant | none: fired w/r · P · R | prompt: fired w/r · P · R | cards: fired w/r · P · R | ALL: fired w/r · P [CI] · R |
|---|---|---|---|---|
| any doubt word in last 10 notes (no "unresolved" test) | 71/58 · 55% · 100% | 69/64 · 52% · 100% | 78/55 · 59% · 100% | 218/177 · 55% [50–60] · 100% |
| last 5 notes, no command after (window 5) | 45/33 · 58% · 63% | 56/41 · 58% · 81% | 40/33 · 55% · 51% | 141/107 · 57% [51–63] · 65% |
| last 10 notes, no command after (primary) | 45/33 · 58% · 63% | 56/41 · 58% · 81% | 40/33 · 55% · 51% | 141/107 · 57% [51–63] · 65% |
| last 20 notes, no command after (window 20) | 45/33 · 58% · 63% | 56/41 · 58% · 81% | 40/33 · 55% · 51% | 141/107 · 57% [51–63] · 65% |
| keyword: no later command mentions a year/number from the doubting notes | 8/2 · 80% · 11% | 4/1 · 80% · 6% | 2/1 · 67% · 3% | 14/4 · 78% [55–91] · 6% |

- The window size does not matter: the run's last reasoning note (normally the one written just before the final answer) contains a doubt word in 268 of 399 runs, so the last doubting note nearly always falls inside even the 5-note window; windows of 5, 10 and 20 fire on exactly the same runs.
- Doubt words are near-universal in GLM's reasoning (`maybe` dominates), so the presence of doubt carries no signal; the "answered straight after doubting" pattern is just as common in right runs. The keyword-addressed variant has higher precision on a tiny number of firings; the interval is too wide to rely on.
- Consistent with FINDINGS.md (85/103 failures expressed doubt at the divergence point): doubt is present but so is it in right runs. A doubt-based trigger would need the *content* of the doubt (e.g. two candidate readings) recorded in the ledger, which L2 would make checkable.

## 8. Union of (a)+(b)+(c)

All runs:

| variant | none: fired w/r · P · R | prompt: fired w/r · P · R | cards: fired w/r · P · R | ALL: fired w/r · P [CI] · R |
|---|---|---|---|---|
| (a)+(b)+(c) | 37/22 · 63% · 52% | 30/15 · 67% · 43% | 39/17 · 70% · 50% | 106/54 · 66% [59–73] · 49% |
| (a\*)+(b)+(c) | 31/13 · 70% · 44% | 30/14 · 68% · 43% | 30/11 · 73% · 38% | 91/38 · 71% [62–78] · 42% |
| (a\*)+(c) (no provenance) | 19/4 · 83% · 27% | 23/3 · 88% · 33% | 24/6 · 80% · 31% | 66/13 · 84% [74–90] · 30% |
| (c) alone | 18/4 · 82% · 25% | 22/3 · 88% · 32% | 24/5 · 83% · 31% | 64/12 · 84% [74–91] · 29% |

Submitted answers only:

| variant | none: fired w/r · P · R | prompt: fired w/r · P · R | cards: fired w/r · P · R | ALL: fired w/r · P [CI] · R |
|---|---|---|---|---|
| (a)+(b)+(c) | 33/22 · 60% · 49% | 25/15 · 62% · 39% | 35/17 · 67% · 47% | 93/54 · 63% [55–71] · 45% |
| (a\*)+(b)+(c) | 27/13 · 68% · 40% | 25/14 · 64% · 39% | 26/11 · 70% · 35% | 78/38 · 67% [58–75] · 38% |
| (a\*)+(c) (no provenance) | 15/4 · 79% · 22% | 18/3 · 86% · 28% | 20/6 · 77% · 27% | 53/13 · 80% [69–88] · 26% |
| (c) alone | 14/4 · 78% · 21% | 17/3 · 85% · 27% | 20/5 · 80% · 27% | 51/12 · 81% [70–89] · 25% |

Wrong runs by which checkers fire (all runs): none 112, c 41, b 20, a 16, a+c 15, b+c 7, a+b 6, a+b+c 1.

## 9. False-positive examples (right runs that fire), 5 per checker per arm

### (a) format_check

- **none** (12 false positives)
  - Q002: STRAY_TEXT — 4 stray word(s) (units/labels/prose) around the number(s); STRAY_UNIT — value 1/1 carries a currency symbol
  - Q003: STRAY_TEXT — 2 stray word(s) (units/labels/prose) around the number(s)
  - Q018: STRAY_TEXT — 2 stray word(s) (units/labels/prose) around the number(s)
  - Q060: STRAY_TEXT — 1 stray word(s) (units/labels/prose) around the number(s); STRAY_UNIT — value 1/1 carries a currency symbol
  - Q068: STRAY_TEXT — 2 stray word(s) (units/labels/prose) around the number(s)
- **prompt** (3 false positives)
  - Q002: STRAY_TEXT — 4 stray word(s) (units/labels/prose) around the number(s)
  - Q003: STRAY_TEXT — 2 stray word(s) (units/labels/prose) around the number(s)
  - Q014: STRAY_TEXT — 2 stray word(s) (units/labels/prose) around the number(s)
- **cards** (11 false positives)
  - Q002: STRAY_TEXT — 4 stray word(s) (units/labels/prose) around the number(s); STRAY_UNIT — value 1/1 carries a currency symbol
  - Q003: STRAY_TEXT — 2 stray word(s) (units/labels/prose) around the number(s)
  - Q027: STRAY_TEXT — 1 stray word(s) (units/labels/prose) around the number(s); STRAY_UNIT — value 1/1 carries a currency symbol
  - Q051: STRAY_TEXT — 1 stray word(s) (units/labels/prose) around the number(s); YEAR_EXPECTED — question asks which year; answer is not a bare 4-digit year
  - Q060: STRAY_TEXT — 1 stray word(s) (units/labels/prose) around the number(s); STRAY_UNIT — value 1/1 carries a currency symbol

### (b) provenance_check

- **none** (11 false positives)
  - Q003: value 1/1 (2 dp) not in any command output; nearest output number off by 0.80%
  - Q015: value 1/1 (2 dp) not in any command output; nearest output number off by 0.08%; matches only after x10^k rescaling (unit/percent conversion typed by hand)
  - Q022: value 1/1 (2 dp) not in any command output; nearest output number off by 0.28%
  - Q040: value 1/1 (0 dp) not in any command output; nearest output number off by 81.56%; matches only after x10^k rescaling (unit/percent conversion typed by hand)
  - Q048: value 1/2 (3 dp) not in any command output; nearest output number off by 1.53%; matches only after x10^k rescaling (unit/percent conversion typed by hand) | value 2/2 (3 dp) not in any command output; nearest output number off by 6.63%
- **prompt** (11 false positives)
  - Q002: value 1/1 (0 dp) not in any command output; nearest output number off by 0.14%
  - Q003: value 1/1 (2 dp) not in any command output; nearest output number off by 0.38%
  - Q022: value 1/1 (2 dp) not in any command output; nearest output number off by 0.28%
  - Q027: value 1/1 (2 dp) not in any command output; nearest output number off by 17.65%; matches only after x10^k rescaling (unit/percent conversion typed by hand)
  - Q040: value 1/1 (0 dp) not in any command output; nearest output number off by 99.46%; matches only after x10^k rescaling (unit/percent conversion typed by hand)
- **cards** (7 false positives)
  - Q002: value 1/1 (0 dp) not in any command output; nearest output number off by 0.07%
  - Q003: value 1/1 (2 dp) not in any command output; nearest output number off by 3.91%
  - Q022: value 1/1 (2 dp) not in any command output; nearest output number off by 0.28%
  - Q027: value 1/1 (2 dp) not in any command output; nearest output number off by 17.65%; matches only after x10^k rescaling (unit/percent conversion typed by hand)
  - Q040: value 1/1 (0 dp) not in any command output; nearest output number off by 83.11%; matches only after x10^k rescaling (unit/percent conversion typed by hand)

### (c) loop_detect

- **none** (4 false positives)
  - Q040: one command issued 41x; 3rd issue at command 47 of 157; longest identical streak 1; 1 compaction(s), at least one after the loop fired; answered correctly
  - Q053: one command issued 50x; 3rd issue at command 25 of 178; longest identical streak 50; 2 compaction(s), at least one after the loop fired; answered correctly
  - Q066: one command issued 60x; 3rd issue at command 33 of 195; longest identical streak 59; 2 compaction(s), at least one after the loop fired; answered correctly
  - Q120: one command issued 5x; 3rd issue at command 28 of 30; longest identical streak 5; 0 compaction(s), none after the loop; answered correctly
- **prompt** (3 false positives)
  - Q069: one command issued 35x; 3rd issue at command 33 of 200; longest identical streak 1; 1 compaction(s), at least one after the loop fired; answered correctly
  - Q099: one command issued 19x; 3rd issue at command 24 of 57; longest identical streak 1; 1 compaction(s), at least one after the loop fired; answered correctly
  - Q113: one command issued 58x; 3rd issue at command 10 of 75; longest identical streak 58; 1 compaction(s), at least one after the loop fired; answered correctly
- **cards** (5 false positives)
  - Q040: one command issued 32x; 3rd issue at command 28 of 128; longest identical streak 1; 1 compaction(s), at least one after the loop fired; answered correctly
  - Q053: one command issued 47x; 3rd issue at command 62 of 106; longest identical streak 46; 1 compaction(s), at least one after the loop fired; answered correctly
  - Q056: one command issued 65x; 3rd issue at command 25 of 87; longest identical streak 64; 1 compaction(s), at least one after the loop fired; answered correctly
  - Q066: one command issued 126x; 3rd issue at command 59 of 248; longest identical streak 126; 1 compaction(s), at least one after the loop fired; answered correctly
  - Q107: one command issued 118x; 3rd issue at command 20 of 268; longest identical streak 1; 1 compaction(s), at least one after the loop fired; answered correctly

### (d) doubt_unresolved

- **none** (33 false positives)
  - Q002: 6 of the last 10 reasoning notes contain doubt words (maybe); the last one was followed by 0 commands — the model answered straight after, and was right
  - Q003: 5 of the last 10 reasoning notes contain doubt words (maybe); the last one was followed by 0 commands — the model answered straight after, and was right
  - Q009: 4 of the last 10 reasoning notes contain doubt words (maybe); the last one was followed by 0 commands — the model answered straight after, and was right
  - Q013: 7 of the last 10 reasoning notes contain doubt words (maybe); the last one was followed by 0 commands — the model answered straight after, and was right
  - Q018: 8 of the last 10 reasoning notes contain doubt words (maybe); the last one was followed by 0 commands — the model answered straight after, and was right
- **prompt** (41 false positives)
  - Q002: 6 of the last 10 reasoning notes contain doubt words (maybe); the last one was followed by 0 commands — the model answered straight after, and was right
  - Q003: 9 of the last 10 reasoning notes contain doubt words (maybe); the last one was followed by 0 commands — the model answered straight after, and was right
  - Q005: 5 of the last 10 reasoning notes contain doubt words (maybe); the last one was followed by 0 commands — the model answered straight after, and was right
  - Q012: 8 of the last 10 reasoning notes contain doubt words (maybe); the last one was followed by 0 commands — the model answered straight after, and was right
  - Q014: 6 of the last 10 reasoning notes contain doubt words (maybe); the last one was followed by 0 commands — the model answered straight after, and was right
- **cards** (33 false positives)
  - Q002: 3 of the last 10 reasoning notes contain doubt words (maybe); the last one was followed by 0 commands — the model answered straight after, and was right
  - Q003: 8 of the last 10 reasoning notes contain doubt words (maybe); the last one was followed by 0 commands — the model answered straight after, and was right
  - Q005: 4 of the last 10 reasoning notes contain doubt words (maybe); the last one was followed by 0 commands — the model answered straight after, and was right
  - Q008: 4 of the last 10 reasoning notes contain doubt words (maybe); the last one was followed by 0 commands — the model answered straight after, and was right
  - Q009: 6 of the last 10 reasoning notes contain doubt words (maybe); the last one was followed by 0 commands — the model answered straight after, and was right

## 10. Agreement with the divergence analysts (descriptive; 103 analysed failed runs)

- Analyst `loop_or_thrash` vs loop_detect: severe: 26 fire / 1 not, mild: 2 fire / 37 not, none: 0 fire / 37 not. loop_detect captures the severe collapses; "mild" thrash (re-searching with varied queries) is not identical-command repetition and needs a similarity-based detector if it is to be caught.
- Analyst `glm_expressed_doubt` = yes: 85 have doubt words in the last 10 notes, 56 fire (d).
- First-divergence stage vs (a\*) or (b) firing: CELL_SELECTION 3/21, INTERPRETATION 7/20, SOURCE_ISSUE_VINTAGE 1/19, EXTERNAL_KNOWLEDGE 2/11, EXTRACTION 1/11, SOURCE_TABLE 0/9, ROUNDING_FORMAT 1/5, FORMULA 1/3, OPERATIONAL 2/3, ARITHMETIC 0/1.

## 11. What this means for the v9 plan

1. **L3 loop guard: build it; the replay supports it.** Trigger on the 3rd identical command (adding "with identical output" fires on exactly the same v8 runs). Because the collapse is a temperature-0 fixed point, the guard must change the context (inject a message with the current best candidate and "that command already returned X; do something different"), not just count. Also consider a small non-zero temperature for the actor; that is an experimental variable to declare, not a silent fix.
2. **L3 submit-at-timeout: build it.** All empty answers are loop runs that hit the wall clock; a ledger candidate would at least be gradable.
3. **L4 gate: keep only COUNT/EMPTY-type format rules as hard failures.** Do not bounce units, `$`, `%`, commas or "percentage points" text: they cost right answers nothing under this grader. Decimals rules are cheap to keep but caught nothing in v8.
4. **Provenance: advisory only.** It fires on right and wrong runs alike; use it to ask for a `calc` re-run of the final arithmetic, never to block. Its real use is after L1/L2 exist: "answer = a recorded calc output whose inputs are cited cells" is checkable against the ledger, and that is where the selection errors live.
5. **Doubt: drop as a gate.** Revisit only when doubts are recorded as explicit alternative candidates in the ledger.
6. **Evolver admission test**: this replay harness is the template for the evolver's admission test — a proposed checker must show precision well above the arm base rate on past episodes (these tables), not merely fire on the failure it was written for.

## 12. Limitations

- One run per question per arm; the paired-rerun flip rate is ~27% (FINDINGS.md), so per-arm precision estimates carry the wide Wilson intervals shown; pooled ALL rows are the ones to read.
- The question-spec parser is heuristic regex written after reading the 133 question texts (not the answers); it is tuned to this set and will need generalising for another benchmark.
- Provenance uses Codex `aggregated_output` (full) and a reconstruction of what the model saw; outputs of commands still running at a wall-clock kill are missing. Tolerance is half a unit in the answer's last typed decimal, sign-insensitive.
- loop_detect uses exact (whitespace-normalised) command strings; near-duplicate thrash (same intent, different query) is not caught.
- The effect of *acting* on a checker (nudging, re-running) cannot be measured offline; these numbers bound detection only.

## 13. Reproduce

```
cd officeqa-v9-design/understand/replay
/opt/homebrew/bin/python3.11 run_replay.py   # ~15 s, writes results.jsonl (flags only, no answer values)
/opt/homebrew/bin/python3.11 report.py       # writes ../replay.md
```

Files: `load.py` (run/trajectory loader, model-visible output reconstruction), `checkers.py` (the four checkers), `run_replay.py`, `report.py`, `results.jsonl` (per-run flags, counts and value-free reasons).
