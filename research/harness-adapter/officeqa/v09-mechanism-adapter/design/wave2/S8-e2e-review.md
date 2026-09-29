# S8-e2e review (final $0 gate)

**Verdict: S8's report and rehearsal are accurate. I found one real readiness blocker and fixed it: preflight could never pass inside a campaign root. I also made four smaller fixes. Every test passes at every tier.**

The readiness verdict, the blockers and the user summary are in `READINESS.md` (same directory). This file records what I reviewed and verified.

Paths are relative to `officeqa-pro133-v9-plumbing/`.
`PY=/Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd/adapter-comparison-work/.venv/bin/python`.

## What I reviewed

- **`S8-e2e.md`.** Its claims match the code and the records:
  - the live test list;
  - the six integration fixes (the toy CLI import, the toy pack wiring, `W0_PACKS`, pack-aware `campaign`, per-round audit modes, `submit_invalid` for `'rejected'`);
  - the declared deviations.
- **`tests/test_v9_e2e.py` and `tests/e2e/`.** The assertions are specific: exact answers per arm, exact counters, exact learner verdicts re-derived with `judge`. The scenarios pin real failure modes: crash/resume, 5xx, a dropped stream, a stall with an actor kill, and a learned rule that is served and fires. No test hides a failure behind a broad `try`.
  - Weak spot 1: the main group uses one order seed and one scripted actor, so it covers wiring, not behaviour. S8 says so itself.
  - Weak spot 2: the paid-smoke code path was covered at $0 only for `plain` + `mech-static`. I ran it for all five arms (below).

## Verification (exact commands and results)

| command | result |
|---|---|
| `V9_SLOW=1 V9_LIVE_CODEX=1 $PY -B -m pytest -q -p no:cacheprovider tests -rs --durations=25` (before fixes) | 390 passed, 0 skipped, 308 s |
| `$PY -B -m pytest -q -p no:cacheprovider tests` (after fixes) | 329 passed, 61 skipped (opt-in), 95 s |
| `V9_SLOW=1 V9_LIVE_CODEX=1 $PY -B -m pytest -q -p no:cacheprovider tests` (after fixes) | **390 passed**, 0 skipped, 308 s |
| `$PY -B arms.py check`; `$PY -B cost.py --self-test` | W0 2,478 / 2,429 B, unchanged; self-test passed, 0 paid calls |
| `$PY -B <scratchpad>/s8r_smoke5.py <dir>`: `run.smoke` for all 5 arms, fake upstream, real Codex | passed 5/5. Each mech arm: 1 submit, 1 gate round (`answer_count`: "1 value(s); your answer has 0" on the non-numeric marker); cost accounting 12 calls, 0 unknown |
| default + live suite inside a simulated root (`scratchpad/s8r/rootcopy/v9-r1-a`, arm `mech-static`) | reproduced R1 (below). The other failures there come from my scratch layout (symlinked siblings; `adapter-comparison-work` not a sibling), not from the code |
| `-m live_codex tests/test_v9_e2e.py` inside that root | 12 passed, 87 s |

## Fixes (details in `READINESS.md` §3)

- **R1.** `tests/test_v9_orchestration.py::test_experiment_constants_without_a_campaign` failed in every campaign root. So preflight's `suite_orchestration` / `suite_live_codex` failed there, readiness could never pass, and `run.py run` was blocked. Fixed.
- **R2.** `preflight.LIVE` now includes `tests/test_v9_e2e.py`.
- **R3.** `full_audit._admission_matches` now rejects a `full` record whose counterfactual never admitted or whose precision failed. Four new asserts.
- **R4.** `PROTOCOL.md` is aligned with the code:
  - the false permission line in Codex's environment context;
  - the admission modes and constants;
  - the per-round audit;
  - cost after transport faults;
  - the stalled-stream record gap.
- **R5.** `arms.py` docstring: the `order_seed` example now equals PREREG's value.

## Deviations

- I did not change the smoke's synthetic marker. It triggers one `answer_count` finding on the mech arms. That is declared in READINESS §5 rather than hidden. A numeric marker (`TRANSPORT-<digits>`) would make the gate silent, but it would change the smoke semantics and the S6 test.
- I did not fix the stalled-stream record gap in `broker.py`, because it changes transport semantics, which the adversity scenario pins. It is recommended before the pilot and requested again.

## Gaps

- A real prepared root has never been through `preflight.py --offline-only`. This would be the first $0 operator step.
- The pilot tooling does not exist (READINESS §6).
- The learner transport against real Fireworks is untested.
