# S7-integrate review (adversarial)

**Verdict: S7 did what its brief asked, and the report is accurate. I found three defects and fixed them in
S7-owned files. The suite passes at every tier (290 tests in one run).**

Paths are relative to `officeqa-pro133-v9-plumbing/`.
`PY=/Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd/adapter-comparison-work/.venv/bin/python`.
There is no git in this tree. I compared each file with S7's pre-change backups (`scratchpad/s7-orig/`). Before
editing anything, I snapshotted the post-S7 tree to `scratchpad/s7r-pre/`.

Terms used below:
- **Here-document.** `cmd <<EOF … EOF`: the lines up to `EOF` are data fed to `cmd`, not commands.
- **Here-string.** `cmd <<< "text"`: one word of data on the same line. It has no body and no end delimiter.
- **Ticket.** The ledger's sequence number for each `ingest_request` call. Results are committed in ticket
  order, so if a ticket is never committed, every later request waits forever.
- **Answer span.** The part of the final message that is the answer (`pack.extract_final`).

## 1. Check of each brief item

| # | item | verdict | how I checked |
|---|---|---|---|
| 1 | Ledger: per-pack cap, markers kept for `handles_truncation` packs, `raw_command`, `raw_cmd` ≤ 8 KB, `tool`, pack work outside the lock | done, with one defect (D3) | Read the diff. Provisional `?n` refs are safe because no pack puts refs inside `attrs` (grep). `_accepts_keyword` precedence is correct. Re-ran S7's v8 coverage script: 1,322 outputs / 74,703 located / 0 errors, the same as reported |
| 2 | Gate: `check_final` records no Note; unusable payload → `'rejected'`; `needs_enforcement` kept | done | Read the diff. The gate Note is written only when `kind == 'submit'` |
| 3 | Controller: answer span to `needs_enforcement` and `check_final`; Note `ref=''`; `View.call`; `broker.py` fills `View.call` | done | The `broker.py` diff is exactly `_view(exchange, call)` plus 3 call sites. For the first call, `on_response` gets `exchange.view`, whose `call` is that same call's number, so it is correct |
| 4 | `count_mismatch` uses `allowed_words`; the spec fills them | done | Read the diff. Allowed words are optional slots: a match on numbers alone, or on numbers plus word hits, passes. I did not re-count Q103 (to keep away from the answer files) |
| 5 | OfficeQA tools moved to `packs/officeqa/tools/`; `extract_final`, `handles_truncation`, `max_observations`; imports work | done, with two defects (D1, D2) | No stale `packs.officeqa.cli` / `otables` imports remain (grep). I re-ran S7's `tools_sandbox_smoke.py` in the real Seatbelt profile. The tools work, and the only listable directory is `tools/` (`__init__.py`, `__pycache__`, `cli.py`, `otables.py`). `ls packs/officeqa` and reading `seed_rules.json` both fail with `Operation not permitted` |
| 6 | acting V9 settings: multi-agent off, permissions off, explicit 244,800 | done | `V9_SETTINGS` and `V9_DECLARED_SETTINGS` are applied after the base settings. `invocation.json` records them. The kit test pins the v8→v9 argv difference |
| 7 | Live tests on `acting.build_settings`; 244,800 accepted; a small limit compacts and splices the real ledger | done | Re-ran `-m live_codex`: 18 passed. On the 244,800 point, S7's report is right that behaviour alone cannot tell the explicit value from Codex's fallback value (the same number). Acceptance rests on the strict-config echo plus the honoured lower limit |
| 8 | Full suite plus report | done | See §3 |

The fixed-for-all-arms decisions are applied as specified:
- `requery_when_clean=False` is the default.
- Temperature is still 0 (the broker sets it).

## 2. Defects found and fixed (all in S7-owned files)

**D1. A here-string swallowed the rest of the script** (`packs/officeqa/observe.py`, `HEREDOC_RX`). This was a
regression in S7's new raw-command path.
- The pattern also matched the `<<` inside `<<<`. So `grep -n x <<< "$v"` was read as a here-document whose end
  delimiter is `$v`.
- `shell_script` then dropped every later line while it looked for that delimiter. Probe:
  `shell_script('grep -n x <<< "$v"\nsed -n 1,2p f.txt')` returned `'grep -n x <<< "$v"'`.
- Effect: the `sed` line was lost, so its output could not be located in the corpus.
- **Fix:** the pattern is now `(?<!<)<<(?!<)-?…`.
- **Tests** (in `test_raw_command_splits_script_lines_that_the_normalised_command_joins`):
  - the here-string case now yields `… ; sed -n 1,2p f.txt`;
  - a `<<-EOF` body is still removed.
- The v8 coverage is unchanged (1,322 / 74,703 / 0 errors). v8 traffic has almost no multi-line commands.

**D2. `extract_final` lost a negative sign** (`packs/officeqa/pack.py`, `_ANSWER_LABEL`).
- The label separator set included `-`, so `Answer -5` became the span `5`. The gate would then check the
  wrong sign.
- **Fix:** a hyphen counts as a separator only when no digit or `.` follows it: `(?:[:：=–—]|-(?![\d.]))`.
- The results are now:

  | message | span |
  |---|---|
  | `Answer -5` | `Answer -5` (kept whole; its number is -5) |
  | `ANSWER - 12` | `12` |
  | `Final answer: -5` | `-5` |

- Three cases were added to `test_extract_final_returns_the_answer_span`.

**D3. A ticket could leak and deadlock the ledger** (`harness_core/ledger.py`, `ingest_request`).
- Phase 1 caught only `Exception`, and phase 2 (`_derive`) ran with no guard at all.
- A `KeyboardInterrupt`/`SystemExit` in either phase therefore skipped the commit. Every later `ingest_request`
  then blocked forever in `_turn.wait()`.
- **Fix:**
  - both phases now catch `BaseException` into `error`;
  - the ticket is always committed, with nothing appended when there is an error;
  - the error is then re-raised.
- **New test:** `test_an_interrupted_derivation_still_releases_its_turn`. A pack raises `KeyboardInterrupt` once,
  and the next request must still commit within 10 s.

## 3. Verification (run from `officeqa-pro133-v9-plumbing/`, after the fixes)

| command | result |
|---|---|
| `V9_SLOW=1 V9_LIVE_CODEX=1 $PY -m pytest -q -p no:cacheprovider tests -x` (before my fixes) | 289 passed, 167 s. This matches S7's report |
| `$PY -m pytest -q -p no:cacheprovider tests` | **244 passed, 46 skipped**, 58 s. The skips are the opt-in slow and live_codex groups |
| `$PY -m pytest -q -p no:cacheprovider -m slow tests` | **28 passed**, 86 s |
| `$PY -m pytest -q -p no:cacheprovider -m live_codex tests` | **18 passed**, 23 s. This uses the real pinned Codex against a local fake upstream at $0 |
| `V9_SLOW=all $PY -m pytest -q -p no:cacheprovider -m slow tests/test_v9_ledger.py` | **399 passed**, 41 s: every recorded v8 run |
| `V9_SLOW=1 V9_LIVE_CODEX=1 $PY -m pytest -q -p no:cacheprovider tests` | **290 passed**, 0 skipped, 166 s |
| `scratchpad/tools_sandbox_smoke.py` (S7's) | The tools work in the sandbox. Only `tools/` is listable. Seed and rule files: `Operation not permitted` |
| `scratchpad/s7r_coverage.py` (S7's v8 coverage script, v9.1 variant only) | 133 runs: 1,322 outputs located, 74,703 located, 109,624 observations, 0 errors (unchanged) |

Safety:
- No network except 127.0.0.1, and no paid model calls.
- No credentials were read.
- No reference answers or graders were opened.
- The coverage script reads only the model's own v8 api traffic plus the corpus, as S7's did.

## 4. Accepted as declared (not changed), with notes

1. **`test_validate_config_offline` is not marked `live_codex`,** though it starts the real Codex binary. It does
   only an offline `config/read`: no model endpoint, about 1 s, and S1's preflight tier 0 relies on it. This is
   a defensible reading of "live-Codex test", but a strict reading would mark it. The user or S6 should decide.
2. **A one-line final message is kept whole by `extract_final`.** So the S4-review example "In FY2004 the total
   was 12.3, up from 11.1" still triggers `count_mismatch` when it was never submitted. S7 chose this on
   purpose (it is the graded line) and pinned it in a test. The pilot's "enforce fire rate on right episodes"
   is the check.
3. **A `'rejected'` submit message records no Note.** The submit CLI still prints it, so the ledger does not
   strip it from that tool output.
   - Its only numbers are the field limits (1000, 500, 64), which could become raw observations.
   - This behaviour predates S7. Adding a `gate` Note would be wrong, because `_gate_finding_after` would
     count it as a finding on the previous candidate.
   - Low impact; left as is.
4. **The ledger's event order changed:** within one request, Command completions now come before
   observations. The folded state is unchanged (the 399-run sweep passes). Readers that rely on the order of
   raw lines rather than refs should know this. S7 already told S5/S6 in its requests file (item 7).
5. **The `shell_script` quote scanner treats an apostrophe in a `#` comment as an opening quote**, and shlex then
   fails the whole command. This is not a regression: the old normalised-command path fails the same way.

## 5. Remaining gaps (for owners outside S7; already in `S7-integrate-requests.md`)

- **Broker (S3).** `summary.json['submit_invalid']` should count `status == 'rejected'` (requests item 1).
- **Broker (S3).** `controller_lock` still makes `/v1/submit` wait for `on_request` (item 2).
- **`RawPack` / `ToyPack`.** They lack `raw_command` / `extract_final`. The defaults cover them (item 3).
- **Contract comment.** It should say what `View.call` means per hook (item 11).

I have no new requests, so I did not create `S7-integrate-review-requests.md`.

## 6. Files changed by this review

- `packs/officeqa/observe.py`: `HEREDOC_RX` (D1).
- `packs/officeqa/pack.py`: `_ANSWER_LABEL` (D2).
- `harness_core/ledger.py`: `ingest_request`, the `BaseException` handling (D3).
- `tests/test_officeqa_pack.py`: 2 here-string/here-doc asserts and 3 `extract_final` cases.
- `tests/test_v9_ledger.py`: 1 new test.
