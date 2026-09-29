
### Addendum P2: pilot-1 results, a new mech W0 and the re-pilot b2, dated 2026-09-28/29

Written after pilot 1 (addendum P1) was completed, sealed, graded and evaluated with the pinned
`pilot/metrics.py`, and before any call of the re-pilot. It records the pilot-1 results (counts only), the
two changes the user approved on 2026-09-28 ("fix b and re-run b only"), the re-pilot and its
pre-registered decision rule. Pilot runs are never part of the confirmatory analysis (§7). Hashed sections
and addendum P1 are unchanged.

**Pilot-1 results (counts only; `v9-p1-metrics.json`).** 50 questions per root (pilot-first50), repeat 1.

| root | arm | temperature | top_p | strict correct | empty | loop runs |
|---|---|---|---|---|---|---|
| `v9-p1-a` | `text-static` | 0 | not sent | 31 | 1 | 10 |
| `v9-p1-b` | `mech-static` | 0 | not sent | 20 | 0 | 9 |
| `v9-p1-c` | `text-static` | 1.0 | 0.95 | 24 | 0 | 0 |

- Temperature rule (P1): loop runs of c 0 against 10 of a (condition met); strict correct c − a = −7, below
  −2 (not met). Verdict: confirmatory arms at T=0, no top_p.
- Go/no-go on `v9-p1-b` (§7): **NO-GO**. 1 ledger coverage 94.0% (47/50) PASS; 2 finding rate on wrong
  episodes 51.7% (15/29) against right episodes 57.1% (12/21) FAIL; 3 flips after a finding wrong→right 1,
  right→wrong 2 FAIL; 4a union finding rate on right episodes 57.1% (limit 20%) FAIL; 4b right→wrong 2 of 12
  fired right episodes, 16.7% (limit 5%) FAIL; 5 submit rate 100%, enforcement re-query rate 0%.
- Both right→wrong flips followed a `competing_candidates` finding whose offer was the model's own `--alt`.
- Diagnosis: the mech W0 carried only the finishing text (`SEED.md` 829 B), the text W0 the full advice
  (2,478 B). The mech arm replaced the advice by rules instead of adding rules to it, an asymmetry of the
  design; and the gate offered the model's own alternative as a one-command Accept.

**Change 1: mech W0 = text W0 + the finishing text.** `adapters/mech-w0/SEED.md` is now the text W0's
`SEED.md` verbatim (all sections), a blank line, then the finishing section: how to finish with `submit`,
what a finding and an `Offer` are, Accept and Keep, the `[harness:...]` lines. The suggestion to pass
`--alt` is removed from the finishing section, from the README's submit usage and from the gate's usage line;
the submit CLI still accepts `--alt` but no longer lists it in `--help`. Both mech arms use this W0
(mech-evolve starts from it too). The §5 seed-parity line now reads: text arms = the seed text; mech arms =
the same seed text + the finishing text + the executed rules. Model-visible bytes (`arms.model_visible`, the
learners' formula, budget 16,384 B; the rule templates of the five policy-disabled rules, 532 B, are still
counted):

p2-text-w0-model-visible-bytes: 2478
p2-mech-w0-model-visible-bytes: 4845
p2-mech-seed-bytes: 3245
p2-mech-gate-frame-bytes: 257

The mech total is 3,245 B `SEED.md` + 1,130 B rule templates + 470 B controller templates; the gate frame,
257 B, is recorded separately. Pilot 1 ran 2,429 B (829 B `SEED.md`; gate frame 275 B).

**Change 2: rules disabled by the pre-registered procedure (§5 gate limits, §7 item 4).**
`pilot/select_rules_p2.py` replays the pilot-1 mech-static gate inputs (`v9-p1-b`, 50 episodes, 86 checks,
controller-side after the seal; labels = strict official `reward.py` grades, which reproduce the root's
official grades file) with the P1 operational definitions and the per-rule table of `pilot/metrics.py`.
`competing_candidates` is disabled first whatever its numbers (it offers the model's own `--alt`). Then the
enabled rule with the lowest engine precision is disabled (ties: more right-episode fires first, then the rule
id) until (a) the union of engine fires on right episodes is at most 20% of the right episodes and (b) no
right→wrong flip is attributable to an enabled rule. Rule code and thresholds are unchanged; the rules are
switched off by `disabled_rules` in `adapters/mech-w0/policy.json`.

| rule | engine fires right / wrong | engine precision |
|---|---|---|
| `table_alternatives` | 2 / 2 | 0.50 |
| `grounded` | 4 / 6 | 0.60 |
| `row_coverage` | 1 / 2 | 0.67 |
| `competing_candidates` | 2 / 5 | 0.71 |
| `flagged_cells` | 2 / 5 | 0.71 |
| `recompute_final` | 3 / 8 | 0.73 |
| `later_printing` | 0 / 1 | 1.00 |
| `scale` | 0 / 2 | 1.00 |
| `answer_count`, `period_match` | 0 / 0 | never fired |

Steps (union of engine fires on the 21 right episodes; attributable right→wrong flips): all enabled 12/21
(57.1%), 2; without `competing_candidates` 10/21 (47.6%), 0; without `table_alternatives` 9/21 (42.9%), 0;
without `grounded` 6/21 (28.6%), 0; without `row_coverage` 5/21 (23.8%), 0; without `flagged_cells` 3/21
(14.3%), 0: both limits hold, stop.

- Disabled: `competing_candidates`, `flagged_cells`, `grounded`, `row_coverage`, `table_alternatives`.
- Enabled: `answer_count`, `later_printing`, `period_match`, `recompute_final`, `scale`.
- Online = offline: all 86 stored checks re-derive identically (`harness_core.replay.recheck`), and the
  re-evaluation with only the enabled rules equals the stored fires restricted to them on 86 of 86.
- Projection on the pilot-1 trajectories (not a result; the model's replies to findings are not replayable):
  engine fires of the enabled rules on right episodes 3/21 (14.3%) and on wrong episodes 9/29 (31.0%); on the
  first checked candidate only 3/21 and 8/29 (27.6%).
- Sensitivity, reported and not used: the same procedure with the final answers' official grades as labels
  disables `competing_candidates`, `grounded`, `recompute_final`, `scale`, `table_alternatives`; on the first
  checked candidate only it disables `competing_candidates`, `grounded`, `recompute_final`,
  `table_alternatives`. All three disable `competing_candidates`, `grounded` and `table_alternatives`; they
  differ on the rules with 1 to 3 right-episode fires. The selection above uses the definition pre-designated
  in P1 (the per-rule table of `pilot/metrics.py`).

**Re-pilot b2.** One root, `v9-p2-a` (neutral name), created by `run.py pilot2-root`: arm `mech-static` with
the new W0, the same 50 questions (pilot-first50, the list pinned in P1), repeat 1 and order seed
`v9-officeqa-pro133-order` (the pilot-1 order), temperature 0 and no top_p (as `v9-p1-a` and `v9-p1-b`), a
barrier over this root only (`v9-p2-barrier`, 43,200 s), no learner, 3,600 s wall clock, no answer
fallback; everything else as pilot 1. The command refuses without this addendum or when a pin below differs
from the plumbing. The root copies this PREREG.md, so its hash differs from the pilot-1 roots' (P1 + P2).

p2-root: v9-p2-a
p2-arm: mech-static
p2-temperature: 0
p2-top-p: none
p2-repeat: 1
p2-order-seed: v9-officeqa-pro133-order

**Pre-registered decision rule (single run, descriptive).** Compare the strict correct count of b2 (official
`reward.py`, tolerance 0, sealed final answers, 50 questions) with pilot-1 `text-static` `v9-p1-a` (31 of 50):

- b2 ≥ 31: proceed to plan the confirmatory study with mech = text + rules;
- b2 ≤ 27: rules add nothing on OfficeQA; stop the OfficeQA mechanism line and move to the second benchmark;
- b2 = 28–30: inconclusive; re-run text-static and mech-static concurrently (paired, at the same time) before
  deciding.

p2-reference-root: v9-p1-a
p2-reference-strict-correct: 31
p2-proceed-min: 31
p2-stop-max: 27

The §7 go/no-go criteria 1–5 (unchanged thresholds) are reported again for b2 by the same run of
`pilot/metrics.py` (`pilot/metrics.py ../v9-p1-a ../v9-p2-a`), which prints the verdict line
`P2 VERDICT: PROCEED | INCONCLUSIVE | STOP`. The go/no-go result is reported next to the verdict; the
decision rule above is the one that decides the next step.

**Caveat.** `v9-p1-a` ran on 2026-09-28 and b2 runs later: provider drift, and single runs of the same
configuration disagree on about 27% of questions (v8 arm pairs: 34–37 of 133 discordant). On 50 questions
that is about 13 discordant questions and a standard deviation of about 3.6 questions for a difference of
correct counts, so a b2 − a difference within ±4 questions is within run-to-run noise. The decision rule is a
planning rule for the next step, not a test.

Pins (sha256; the mech W0 revision is `campaign.revision(adapters/mech-w0)`, as in `freeze.json`):

p2-mech-w0-revision: d4e9c3a1a574fca83ab8c25a71babe5fd0393f68346f2292d845bdc5a9102398
p2-mech-seed-sha256: 8a331deac25fb552cc3c26b0dc17774b87504abcf4d8d15c2c4b455061fc623c
p2-mech-policy-sha256: 0349180f10227f971d701542c92f355b9f21eeb1aaaa26f67f9bb4fd15fb658a
p2-rule-selection-file-sha256: bddd065ab52af7416e9a7dc89cbb36eddf301ca67d197318ba0b0753efba19e8
p2-rule-selection-script-sha256: c26046e611c743891b050e454242d4a158bf6f3411fe24ac53e5cf750da9badf
p2-metrics-script-sha256: 156c4d533bb4d163f0450f73b7f6dc4d1e52e66923c60f3c6b178e3ebf63dd7e
