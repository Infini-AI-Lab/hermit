# S4-core adversarial review (harness_core + toy pack)

**Verdict.** The implementer's report is accurate on what it claims. Tests, v8 counts, compaction numbers and timings
all reproduce.

The review found one **critical sandbox escape** in the compute runner: the trusted parent could be made to read any
file, including answer keys, and the result would reach model-visible gate text. It also found one high-severity
correctness bug in literal spans, which made `recompute` edit the wrong characters, plus six smaller defects. All eight
are fixed in S4-owned files, each with a regression test, and all tests pass.

The review made no paid calls and used no network except `127.0.0.1`. It opened no grader, reference or credential
file. This report contains no answer values.

Paths are relative to `officeqa-pro133-v9-plumbing/`.
`PY=/Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd/adapter-comparison-work/.venv/bin/python`.

## 1. Test results

| Command (run from `officeqa-pro133-v9-plumbing/`) | Before review | After fixes |
|---|---|---|
| `$PY -m pytest -q -p no:cacheprovider tests/test_v9_{ledger,engine,gate,runner,replay}.py` | 63 passed, 4 skipped, 9.0 s | **71 passed, 4 skipped, 9.0 s** (8 new regression tests) |
| `V9_SLOW=1 $PY -m pytest -q -p no:cacheprovider -m slow tests/test_v9_ledger.py` | 4 passed, 3.6 s | (included in the next row) |
| `V9_SLOW=all $PY -m pytest -q -p no:cacheprovider -m slow tests/test_v9_ledger.py` (all 399 v8 runs) | 399 passed, 42.3 s | **399 passed, 40.9 s** |
| `$PY -m pytest -q -p no:cacheprovider tests/test_officeqa_pack.py tests/test_v9_broker.py tests/test_v8_broker.py` (S2, S3 and v8, read-only) | — | **108 passed, 2 skipped, 36.9 s** |

- Add `-W ignore::pytest.PytestUnknownMarkWarning` to silence the unregistered `slow` marker, which S6 still has to
  register.
- The two skips in the neighbours' suites are their own skips.

## 2. Defects found

Severity scale: **Critical** means a security boundary is broken; **High** means wrong results in a common path;
**Medium** or **Low** means an edge case or robustness problem.

| # | Severity | Where (after fix) | Defect | Status |
|---|---|---|---|---|
| 1 | **Critical** | `harness_core/runner.py:108-129` | **Sandbox escape by symlink.** See below. | **Fixed** + test `test_planted_symlinks_cannot_redirect_the_runner_reads` |
| 2 | **High** | `harness_core/numeric.py:183-187` | **Literal spans shift after unusual line breaks.** See below. | **Fixed** + test `test_literal_spans_survive_unusual_line_breaks` |
| 3 | Medium | `harness_core/ledger.py:488-512` | **Truncated calc frames produced false values and inputs.** See below. | **Fixed** + test `test_truncated_calc_output_and_code_lines_give_no_values` |
| 4 | Medium | `harness_core/predicates.py:166-211` | **`ungrounded_inputs` fired on constants the question states.** See below. | **Fixed** + test `test_numbers_stated_in_the_question_ground_calc_literals` |
| 5 | Medium | `harness_core/ledger.py:251, 409-418`; `harness_core/gate.py:215-220` | **Lone surrogates desynced the ledger.** See below. | **Fixed** + test `test_lone_surrogates_are_persisted_not_crashing` |
| 6 | Low | `harness_core/gate.py:97` | **The Offer line could break the ≤ 4-line form.** See below. | **Fixed** + test `test_offer_line_is_one_line_whatever_the_alternative_contains` |
| 7 | Low | `harness_core/engine.py:111, 174-179` | **One malformed rule priority broke every check.** See below. | **Fixed** + test `test_malformed_priority_sorts_last_instead_of_crashing` |
| 8 | Low | `harness_core/rules.py:132-144` | **`save_rule` wrote through planted symlinks.** See below. | **Fixed** + test `test_save_rule_replaces_a_planted_symlink_instead_of_writing_through` |
| 9 | Low | `harness_core/runner.py:120-123` | **`killpg` race at timeout.** See below. | **Fixed** (no test; the race window is too small to test reliably) |
| 10 | Low | `harness_core/tools/submit.py:50-57` | **`submit --artifact` could hang on a FIFO.** See below. | **Fixed** |

**1. Sandbox escape by symlink (runner.py).**
- *What went wrong.* stdout and stderr went to files named `stdout` and `stderr` inside the child's writable work
  directory. After the child exited, the unsandboxed parent re-opened them by name.
- *The attack.* Model-authored calc code can run `os.remove('stdout'); os.symlink('<any path>', 'stdout')`. The trusted
  parent then reads the symlink's target, which could be an answer key.
- *Where the stolen text goes.* It is returned as the run's output. `recompute` turns output numbers into an offer,
  and the offer is printed in the gate message. The text also landed in `runner.last['stderr_tail']`.
- *Reproduced before the fix:* `ok=True, out='TOPSECRET 4242.42\n'`.
- *Fix.* stdout and stderr are now unlinked `tempfile.TemporaryFile`s outside the work directory, read back through
  the file descriptors the parent holds. The child cannot name them.

**2. Literal spans shift after unusual line breaks (numeric.py).**
- *What went wrong.* `code_literals` mapped tokenize's (row, col) positions to character offsets with
  `str.splitlines`. That function also splits on `\r`, `\x0c`, ` ` and a few others. `tokenize` splits rows on
  `\n` only.
- *Effect.* After a form feed, a lone CR or a U+2028 in a comment or string, every later span pointed at the wrong
  characters. Example: the span of `1234.5` covered `'\na = 1'`.
- *Consequence.* `recompute` substitutes by span, so it would edit the wrong text and offer a wrong number.
- *Fix.* Row starts now come from `io.StringIO(code).readlines()`, which splits rows the same way `tokenize` does.

**3. Truncated calc frames produced false values and inputs (ledger.py).**
- *What went wrong.* When Codex's head/tail truncation cut a calc frame, the numbers on both sides of the
  `…N chars truncated…` marker became `Compute.values`. The marker's own count (`N`) and any half-numbers in the code
  became `inputs` literals. That produced false `ungrounded_inputs` findings and false "calc output" matches.
  Observations already had this protection; computes did not.
- *Fix.* Values are now taken from the output with marked lines removed, and literals on marked code lines are skipped.

**4. `ungrounded_inputs` fired on constants the question states (predicates.py).**
- *What went wrong.* A constant typed from the question counted as "a value no tool output showed". Examples: the 3.5
  in "grow at 3.5 %", written as `0.035` or `1.035`, and a year named in the question.
- *Why it matters.* This is a guaranteed false fire on right episodes. It counts against the pre-registered 20 %
  right-episode fire limit.
- *Fix.* Numbers stated in the question now count as grounded. So do their percent-as-fraction form and 1 ± p/100
  (the fraction and growth-factor forms apply only for |p| < 100).

**5. Lone surrogates desynced the ledger (ledger.py, gate.py).**
- *What went wrong.* A JSON escape such as `"\udc00"` in model text can reach the gate, for example by POSTing to
  `/v1/submit` with curl. `json.dumps(..., ensure_ascii=False).encode('utf-8')` then raised inside `Ledger._write`
  *after* the event was already appended in memory, so the file and memory stopped matching and replay broke. The
  check file write raised the same way.
- *Fix.* Such lines fall back to ASCII-escaped JSON, which loads back identically. The ledger file is now written in
  binary, so the file bytes are exactly the hashed bytes.

**6. The Offer line could break the ≤ 4-line form (gate.py).** The Offer line printed `offer.answer` and `derivation`
raw. A `--alt` containing newlines, or a multi-line pack locator, broke the ≤ 4-line message form (effect review P3).
The line is now whitespace-collapsed.

**7. One malformed rule priority broke every check (engine.py).**
- *What went wrong.* One rule with a non-int `priority`, for example an unvalidated learned rule, made
  `sorted(rules, key=(priority, id))` raise TypeError. That took down every gate check of the episode.
- *Fix.* A malformed priority now sorts last.

**8. `save_rule` wrote through planted symlinks (rules.py).**
- *What went wrong.* `save_rule` used `path.write_text`. A symlink planted at `rules/<id>.json` would be written
  through, overwriting its target. The rule id was also not checked, so an id like `../x` could escape the directory.
- *Fix.* The rule is written to a temp file with `O_EXCL|O_NOFOLLOW` and then `os.replace`d into place, which replaces
  a symlink instead of following it. The id is validated first.

**9. `killpg` race at timeout (runner.py).** `os.killpg` raised ProcessLookupError when the child exited between the
wall timeout and the kill. The engine then reported the offer as an error. The error is now caught.

**10. `submit --artifact` could hang on a FIFO (tools/submit.py).** `open()` on a FIFO (a named pipe) blocks forever,
so `submit --artifact <fifo>` hung the actor's tool call. The file type is now checked before opening.

## 3. Checked and found sound

**Contract.**
- `LedgerAPI`, `GateAPI`, `ComputeRunner`, `Engine` and the `Pack` usage match `contracts.py`.
- The deviations are additive and all documented in S4-core.md §4: the double Command line, `tools=`, extra methods,
  the rejected-payload result and the extra input keys.
- The review added no new interface deviations.

**Ledger.**
- It reads no file.
- `out_path` is opened with `O_EXCL|O_NOFOLLOW`.
- Refs are unique, and duplicates are refused.
- Folding the first N lines reproduces the snapshot, and its sha is verified.
- Counts survive compaction.
- `render` is deterministic and bounded.

**Engine templates.** No `str.format`; only dotted dict/list lookups; ≤ 80 characters per field and ≤ 200 bytes in
total. Format specs and attribute paths stay literal.

**Gate.**
- One finding per round.
- `--keep` works, and no finding is repeated for the same value.
- No nag wording.
- Check files are written atomically, and `recheck` reproduces them.
- The lock ordering (gate lock, then the ledger's RLock; the ledger never calls the gate) cannot deadlock.
- The relay is not blocked while offers run in the sandbox.

**Sandbox profile.**
- Deny by default.
- Exec is allowed only for the interpreter. No fork.
- Network is denied.
- Reads are limited to the prefix, `/usr/lib`, `/System` and extra roots. Writes are limited to the work directory.
- The environment is empty and `-I` is set.
- The original tests plus the new symlink test pass on the real `sandbox-exec`.

**`submit` CLI.** Accepts only `http://127.0.0.1:<port>/v1/submit`, uses no proxies and follows no redirects.

**Replay and counterfactual.** They return only ids, booleans and counts. `grade_fn` is injected, and nothing imports
a grader.

**Stdlib-only and pack-neutral.** A test enforces that `harness_core` uses only the standard library and imports
nothing from `packs`.

## 4. Remaining risks (not fixed; judgement calls or other owners)

1. **Forged calc frames (Low).**
   - Any command output is parsed for calc frames, so `echo` of a hand-made frame with a matching sha becomes a
     `Compute`. That silences `not_from_compute` for an invented number.
   - It needs a deliberately adversarial model. Restricting frames to commands that invoke `calc` is a weak defence
     (`echo …; calc -c 1`).
   - Pilot: count frames whose command has no `calc` word.
2. **`scale` precision on `percent_form == 'unspecified'` (design risk).**
   - `print(r, r * 100)` followed by answering `r*100` fires `scale` and offers `r`, even though the answer is itself a
     calc output.
   - This follows the effect review ("no rescaling unless asked"). It cannot be measured on v8 because v8 had no calc.
   - Measure in the pilot before trusting it on right episodes.
3. **`competing_candidates` pulls back to an earlier accepted answer.** If the model submits A, gets it recorded,
   investigates further and submits B, the rule offers A. This is by design ("differing earlier candidates"), but it
   is a right→wrong flip risk. Watch it in the pilot flip counts.
4. **`grounded` false fires on unit constants.** Constants such as 24, 52, 60 and 365 are non-trivial literals, so they
   fire when no tool printed them. The rule is advisory and has no offer.
5. **Runner resource limits.**
   - Memory: RLIMIT_AS is not enforced on macOS (already listed by the implementer).
   - Disk: many 1 MB files in the work directory can be written within 2 s of CPU, because RLIMIT_FSIZE is per file.
     Consider a quota check or a size-capped tmpfs if the pilot shows abuse.
6. **`check_final` gets prose.** The whole final message is the candidate (S3 request 1).
7. **Rejected submit payloads are not saved** under `checks/`. They are in the S3 controller log. `recheck` would need
   a guard if they were added.
8. **Implementer's known gaps are unchanged:**
   - single-calc `recompute` (chained calcs are not followed);
   - the latest-match literal linking;
   - gate state lost on a broker restart (and the ledger's `O_EXCL` refuses to reopen);
   - the provenance rules cannot be measured before the pilot.

## 5. Files changed by this review (all S4-owned)

- `harness_core/runner.py`
- `harness_core/numeric.py`
- `harness_core/ledger.py`
- `harness_core/predicates.py`
- `harness_core/gate.py`
- `harness_core/engine.py`
- `harness_core/rules.py`
- `harness_core/tools/submit.py`
- Tests: `tests/test_v9_runner.py` (+1), `tests/test_v9_ledger.py` (+3), `tests/test_v9_engine.py` (+3),
  `tests/test_v9_gate.py` (+1)

Requests to other owners: `officeqa-v9-design/wave1/S4-core-review-requests.md`.
