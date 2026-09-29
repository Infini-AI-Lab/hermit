# v9 pilot: how to run it (PREREG §7 + addendum P1, wave 3)

The pilot runs 50 OfficeQA Pro questions (the first 50 of the canonical order) in three cells:

- `v9-p1-a`: `text-static` at temperature 0 (no top_p sent, as v8);
- `v9-p1-b`: `mech-static` at temperature 0 (no top_p sent);
- `v9-p1-c`: `text-static` at temperature 1.0, top_p 0.95 (the GLM-5.3-Flash model card's agentic
  evaluation settings).

Its outputs are the go/no-go table of PREREG §7 and the verdict of the pre-registered confirmatory
temperature rule. Steps 0–4 are $0. Steps 5–6 are **paid** and need your explicit approval and your
Fireworks key. The key is typed at a hidden prompt or read from `FIREWORKS_API_KEY`; it is never written
anywhere.

```
PY=/Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd/adapter-comparison-work/.venv/bin/python
P=/Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd/officeqa-pro133-v9-plumbing
cd $P
```

## Files here

| file | what it is |
|---|---|
| `select_first50.py` | the pilot's label-free list rule: the first 50 task ids of the canonical base order. It reads only the v8 manifest's task ids and the task ids and ordinals of the v8 (and historic 50-question) runs |
| `first50.json` | its output: 50 ids, their base-order ordinals, the rule, the list sha256 `c95f9613…a07d` |
| `select_loop_prone.py`, `loop_prone_20.json` | the earlier loop-prone draft list (sha256 `81d2bf20…dc56`), kept for the record; not used by the pilot |
| `ADDENDUM.md` | the PREREG addendum P1, filled with your decisions and already appended to PREREG.md |
| `metrics.py` | the go/no-go computation, the temperature-rule verdict and (addendum P2) the re-pilot verdict over the sealed pilot roots. It never prints an answer value |
| `select_rules_p2.py` | addendum P2: replays the pilot-1 mech-static gate inputs and disables rules by the pre-registered procedure (counts and rule ids only) |
| `pilot2-rule-selection.json` | its output on `v9-p1-b`: disabled / enabled rules, the steps, the recheck counts; = `adapters/mech-w0/policy.json` `disabled_rules` |
| `ADDENDUM-P2.md` | the PREREG addendum P2 (pilot-1 results, the new mech W0, the re-pilot b2 and its decision rule), appended to PREREG.md |

## 0. Check the list ($0)

```
$PY -B pilot/select_first50.py --check --overlap   # must print matches_file: true; overlap = counts only
```

## 1. The pre-registration (done)

`pilot/ADDENDUM.md` holds your decisions of 2026-09-28:

- `pilot-cell-temperature: 1.0` and `pilot-cell-top-p: 0.95` (model card, agentic evaluation settings);
- `pilot-budget-cap-usd: no cap (user decision); estimate ≈ $30-45 at v8 prices`;
- the confirmatory temperature rule ("data decides"): T=1.0, top_p 0.95 for every confirmatory arm if cell
  c has at least 50% fewer loop runs than cell a and its strict accuracy is not lower by more than 2 of 50
  questions; otherwise T=0.

It is appended to PREREG.md below the "Addenda" line. Check:

```
grep -c "pilot-list-sha256: c95f9613db9e37effcdaa4de81875f3c045fabb40edfc9c299ca5e60c1b5a07d" PREREG.md   # 1
```

Every pilot root copies this PREREG.md, so all three carry the same hash. Do not edit PREREG.md or
`pilot/ADDENDUM.md` any more; a change after the pilot goes into a new dated addendum.

## 2. Test tiers ($0, ~6 min)

```
V9_SLOW=1 V9_LIVE_CODEX=1 $PY -B -m pytest -q -p no:cacheprovider tests
```

## 3. Create the pilot roots ($0, no model call)

```
$PY -B run.py pilot-roots
```

This creates `../v9-p1-a` (`text-static`, T=0), `../v9-p1-b` (`mech-static`, T=0) and `../v9-p1-c`
(`text-static`, T=1.0, top_p 0.95). Each is a copy of this plumbing with its `.task-paths.json`:

- profile v9, arm, repeat 1;
- `order_seed` `v9-officeqa-pro133-order`;
- `temperature`; `top_p` in `v9-p1-c` only (a and b have no `top_p` key, so no top_p is sent);
- the `subset` (`pilot-first50`, 50 ids, sha256);
- the barrier `../v9-p1-barrier` over the three roots, 12 h timeout;
- `framework` and `data_cache` as v8.

The command refuses in these cases:

- the addendum is missing, still has a USER field, or lacks one numeric `pilot-cell-temperature` /
  `pilot-cell-top-p`;
- the list hash differs;
- a root already exists;
- it is run inside a root.

## 4. Prepare and check each root ($0)

`data.py prepare` reads the official download controller-side. The answers stay under
`prepared/private/`, mode 0400.

```
for r in v9-p1-a v9-p1-b v9-p1-c; do
  (cd ../$r && $PY -B data.py prepare && $PY -B preflight.py --offline-only) || break
done
```

`preflight.py --offline-only` runs the full offline check list and every test tier inside the root
(≈ 6 min per root). It also checks:

- the subset hash against the prepared manifest;
- the subset order;
- declared temperature = the temperature the broker sends;
- declared top_p = the top_p the broker sends (none for a and b).

## 5. Readiness smoke (paid, cents per root)

```
for r in v9-p1-a v9-p1-b v9-p1-c; do (cd ../$r && $PY -B preflight.py) || break; done
```

Each root runs one synthetic marker question through its own arm at its own temperature and top_p (the
`v9-p1-c` smoke is the first paid call that sends top_p 0.95). It then writes `preflight/readiness.json`,
which `run.py run` requires.

## 6. The pilot (paid, no cap; estimate ≈ $30–45 at v8 prices; start the three together, the barrier pairs their blocks)

```
for r in v9-p1-a v9-p1-b v9-p1-c; do
  (cd ../$r && nohup $PY -B run.py run > campaign-stdout.log 2>&1 &)
done
```

`run.py run` asks for the key at a hidden prompt when `FIREWORKS_API_KEY` is not set. When starting in the
background, export the variable in that shell first.

- **Cost.** There is no cap (your decision). Read it after each block and report it:

  ```
  for r in v9-p1-a v9-p1-b v9-p1-c; do (cd ../$r && $PY -B run.py cost | head -c 600; echo); done
  ```

  Add up `known_cost_lower_bound_usd`. To stop anyway, send SIGINT to each root:
  `kill -INT $(python3 -c "import json;print(json.load(open('../v9-p1-a/process.json'))['pid'])")` (and b, c).
  A stop writes the group ABORT. `run.py resume --clear-abort` continues at the block level.
- **Progress.** Open `../v9-p1-*/report.html`. It shows no questions or answers.

## 7. Go / no-go and the temperature rule ($0, after all three roots completed, sealed and graded)

```
$PY -B pilot/metrics.py ../v9-p1-a ../v9-p1-b ../v9-p1-c --json ../v9-p1-metrics.json
echo $?          # 0 = GO, 1 = NO-GO, 2 = no mech-static root given
```

The script prints:

- a per-root table: T, top_p, n, right, empty, loop runs, recovered, loop runs right, statuses;
- the go/no-go table (criteria 1–5, values, thresholds, verdicts; thresholds as PREREG §7);
- loop-note numbers;
- the rules ordered by engine precision. This is the list to use if criterion 4 fails and the
  lowest-precision rule must be disabled;
- the confirmatory temperature rule: loop runs of a and c, the reduction, strict-correct counts of a and c
  and their difference, and the line `RULE VERDICT: confirmatory arms at ...` (`T=1.0, top_p 0.95` or
  `T=0`). The exit code is the go/no-go decision only.

The script grades controller-side, with each root's pinned `reward.py`, but only after the seal. It prints
counts, rates and rule ids, never an answer. Also run `$PY -B run.py audit` in each root. It checks the
subset, the temperature and the top_p of every request.

Any change after the pilot goes into a new dated PREREG addendum before the first confirmatory call. Such
changes are a disabled rule, a new W0, the confirmatory temperature (from the rule verdict), or a 4th repeat.

## Re-pilot b2 (PREREG addendum P2): mech-static with the new W0, one root

Pilot 1 was NO-GO (`../v9-p1-metrics.json`). With your approval of 2026-09-28 ("fix b and re-run b only"):

- the mech W0's `SEED.md` = the text W0's `SEED.md` + the finishing text (rules added to the advice, not
  substituted for it); `--alt` is no longer advertised;
- `policy.json` disables `competing_candidates`, `flagged_cells`, `grounded`, `row_coverage`,
  `table_alternatives` (the pre-registered procedure, `select_rules_p2.py` -> `pilot2-rule-selection.json`).

The re-pilot is one root, `v9-p2-a`: `mech-static`, the same 50 questions, repeat 1, order seed as pilot 1,
temperature 0, no top_p, a barrier over this root only. Steps 1–3 are $0; steps 4–5 are **paid** (pilot-1 b
cost ≈ $5 known lower bound).

```
$PY -B pilot/select_rules_p2.py ../v9-p1-b --out /tmp/check.json   # optional: re-derives pilot2-rule-selection.json
V9_SLOW=1 V9_LIVE_CODEX=1 $PY -B -m pytest -q -p no:cacheprovider tests     # 1. test tiers
$PY -B run.py pilot2-root                                          # 2. creates ../v9-p2-a (refuses without addendum P2)
(cd ../v9-p2-a && $PY -B data.py prepare && $PY -B preflight.py --offline-only)          # 3. $0
(cd ../v9-p2-a && $PY -B preflight.py)                                                  # 4. paid smoke (cents)
(cd ../v9-p2-a && nohup $PY -B run.py run > campaign-stdout.log 2>&1 &)                 # 5. paid run
```

After b2 is completed, sealed and graded ($0):

```
$PY -B pilot/metrics.py ../v9-p1-a ../v9-p2-a --json ../v9-p2-metrics.json
```

It prints the §7 go/no-go table for b2 and the line `P2 VERDICT: PROCEED | INCONCLUSIVE | STOP`: b2 ≥ 31
strict correct → plan the confirmatory study with mech = text + rules; b2 ≤ 27 → stop the OfficeQA mechanism
line and move to the second benchmark; 28–30 → re-run text-static and mech-static concurrently before
deciding. `v9-p1-a` and b2 run at different times (provider drift; ~27% of questions flip between runs).
