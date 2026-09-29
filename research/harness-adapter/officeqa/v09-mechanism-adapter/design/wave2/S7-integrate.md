# S7-integrate: wave-1 cross-stream requests applied (wave 2)

**Status: done. Every test passes.**

| tier | result |
|---|---|
| fast (default) | 243 passed, 46 skipped (the skips are the opt-in groups below) |
| slow | 28 passed |
| slow, all 399 recorded v8 runs | 399 passed |
| live_codex (the real pinned Codex binary) | 18 passed |
| everything in one run | 289 passed, 0 skipped |

Scope and rules:
- **Scope.** Items 1–8 of the stream brief are implemented in S7-owned files. `broker.py` was touched only to
  fill in `View.call`.
- **One real bug found and fixed.** The OfficeQA tables index was not usable across threads (§2).
- **No paid calls.** No network except 127.0.0.1. No credentials were read.
- **Nothing sensitive opened.** No reference answer and no grader was opened. The only OfficeQA files read
  were `questions.json` (question text), the model's own v8 outputs (`final.md`, `api/` traffic) and the corpus.

Paths are relative to `officeqa-pro133-v9-plumbing/`.
`PY=/Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd/adapter-comparison-work/.venv/bin/python`.

**Terms used below.**
- **Answer span.** The part of the model's final message that is the answer. For example, `12.3` out of
  "The total was 12.3, up from 11.1.\nFinal answer: 12.3". Only the span is checked, so the numbers in the
  surrounding prose are not.
- **Truncation marker.** Codex keeps only the head and the tail of a long tool output, and writes
  `…N chars truncated…` where it cut. The lines next to the marker are fragments.
- **Ticket.** A sequence number taken when an `ingest_request` call starts. The results are committed in
  ticket order, so the event order in the ledger never depends on which thread happens to finish first.
- **Opt-in test group.** A pytest marker whose tests are skipped unless you ask for them with `-m <name>` or
  an environment variable.

## 1. What was built, by brief item

### Item 1: the ledger (`harness_core/ledger.py`)

This covers S2 request 1, S2-review 1–3 and S3 request 5.

**Observation cap per pack.**
- `Ledger(pack, out, max_obs_per_output=None)` resolves the cap in this order:
  1. the explicit argument, if given;
  2. `pack.max_observations`, if it is a positive int;
  3. `DEFAULT_MAX_OBSERVATIONS = 400`.
- OfficeQA declares 4,000.

**Truncation markers.**
- A pack with `handles_truncation is True` gets the output body with Codex's truncation markers kept. The
  ledger still removes the calc frames and the gate's own text first.
- Every other pack gets `observable_text` (every marked line dropped), as before. This keeps the core raw
  path unchanged.

**The command as issued.**
- `Command.raw_cmd` is the command exactly as issued: `exec_command`'s `cmd`, the script of a shell list
  command, or `<name> <JSON arguments>` for any other tool. It is clipped to 8,192 UTF-8 bytes.
- `Command.tool` is the Codex tool name, or `local_shell` for a `local_shell_call`.
- `pack.observations(..., raw_command=raw_cmd)` is called only when the pack's signature accepts that
  keyword (checked once with `inspect`). `RawPack` and `ToyPack` do not accept it yet and keep working.

**Pack work outside the lock.** `ingest_request` now runs in three phases.

| phase | lock | what happens |
|---|---|---|
| 1 | held | records the calls, the output completions and the dedupe state, and cuts each new output into one work unit |
| 2 | not held | runs `pack.observations`. The pack gets provisional refs `?n`. |
| 3 | held, in ticket order | appends the observations (real `c<n>` refs, cap applied), then the calc computes, then the compaction events |

- If phase 1 raises, the ticket is still committed before the error is re-raised, so the next request does
  not deadlock. A test covers this.
- Side effect: within one request, the Command-completion lines are now written before that request's
  observation lines. Before, they alternated output by output. The folded state is identical, and the
  sweep over all 399 v8 runs still passes.

**Measured effect.** Scratch script `v8_ledger_coverage.py`, run read-only through `replay.ledger_from_api_records`
with `OfficeQAPack` over all 133 v8 none-arm runs. Counts only.

| ledger path | outputs with a located observation | located observations | all observations | errors |
|---|---|---|---|---|
| wave-1 (marked lines dropped, cap 400) | 1,163 | 60,599 | 95,601 | 0 |
| v9.1 (markers kept for the pack, cap 4,000) | **1,322** | **74,703** | 109,624 | 0 |
| v9.1 without `raw_command` | 1,322 | 74,703 | 109,624 | 0 |

- A located observation is one whose locator is a `file.txt:line` corpus position.
- `raw_command` changes nothing on v8 traffic: v8 had no pack tools and almost no multi-line commands. Its
  effect is covered by unit tests (for example `cd …⏎sed -n …`).
- Run time is the same once the caches are warm. Swapping the variant order on 25 runs moved the slowdown
  to whichever variant ran first.

### Item 2: the gate (`harness_core/gate.py`)

This covers S3-review R1 and S4 request 5.
- `check_final` no longer records a `Note`. Only `submit` messages are Notes (`reason='gate'`), because only
  those are printed as tool output. The controller records the enforcement feedback it actually sends.
- An unusable payload returns `status='rejected'`, with no candidate, no check file and no Note.
- `needs_enforcement` is unchanged.

### Item 3: the controller and the broker

This covers S4-review 1, S4 request 7 and contracts `View.call`.

`controller_runtime.MechController` changes:
- **Answer span.** It passes only the answer span to `gate.needs_enforcement` and `gate.check_final`.
  - The span comes from `pack.extract_final(final message)`.
  - The pack is the `pack=` keyword (new), else `gate.pack`, else `ledger.pack`.
  - With no pack or no `extract_final`, the span is `last_answer_line` (the last non-empty line).
  - An empty span skips enforcement (`no_answer_span`).
- **Note refs.** Notes are created with `ref=''`, so the ledger assigns the ref and it never collides with a
  gate Note.
- **Call numbers.**
  - `View.call` is used in `on_request` and `on_final`.
  - `on_response` uses its own `call` argument, which is the same number.
  - `bind_call` is kept as a fallback.
- **Loop guard.** It reads `Command.tool` to skip `write_stdin` polls, then falls back to its own
  call_id→tool map and the command-prefix test.

`broker.py` change: `_view(exchange, call)` fills in `View.call` at its three call sites. The diff is the `_view`
signature, its docstring and those three call sites (§6 lists them); nothing else in `broker.py` changed.

`harness_core/predicates.py` change: `_gate_finding_after` now looks for an "accepted" notice anywhere in the
note text, not only at its start. The controller's enforcement note wraps the gate message in its own text,
so with `requery_when_clean=True` a clean check would otherwise have counted as a finding.

### Item 4: allowed words (`predicates.count_mismatch`, `packs/officeqa/spec.py`)

- **Spec.** `QuestionSpec.allowed_words` holds the single words the question puts in `'…'` or `"…"`. This is
  the replay parser's rule, extended to double quotes. An apostrophe inside a word, as in "Treasury's",
  never opens a quote.
- **Coverage.** Over the 133 questions (question text only), only Q103 has allowed words:
  `('surplus', 'deficit')`, with `n_values=2`.
- **Count rule.** `count_mismatch` passes when the answer's numbers alone equal the expected count, or when
  its numbers plus its whole-word hits of allowed words do. The allowed words are optional slots, so an
  answer such as `Total: 12.5` does not fire when the question happens to quote `'Total'`.

### Item 5: the OfficeQA pack (`packs/officeqa/`)

**Tools moved.**
- `cli.py` and `otables.py` moved to `packs/officeqa/tools/`, with an empty `__init__.py`.
- Controller-side imports are now `from .tools import cli, otables`.
- Verified inside the real v9 sandbox (scratch `tools_sandbox_smoke.py`, mech-static kit built from
  `tool_wrappers()`):

  | check | result |
  |---|---|
  | `find_tables`, `show_table --help`, `calc`, `submit --help` | all work |
  | files the kit grants | `tools/cli.py`, `tools/otables.py`, `calc.py`, `submit.py` |
  | the one listable directory | `tools/` (names: `__init__.py`, `__pycache__`, `cli.py`, `otables.py`) |
  | `ls packs/officeqa` | `Operation not permitted` |
  | reading `seed_rules.json` | `Operation not permitted` |

**Pack surface.**
- **Tool map and paths.**
  - `OfficeQAPack.tools` maps to `tools.cli:<fn>`.
  - New `tool_wrappers()` returns `{name: [tools/cli.py, name]}`.
  - New constants `TOOLS_DIR` and `CLI`.
- **Declared attributes.** `handles_truncation = True` and `max_observations = 4000`.
- **`extract_final(message)`.** The rules, in order:
  1. an `<answer>` or `<final_answer>` tag, or else the last line labelled `Final answer:` / `Answer:`, gives
     its value;
  2. a one-line message is kept whole, because that is what gets graded;
  3. otherwise, the last non-empty line that contains a digit, else the last line.

  Markdown `**`/`` ` `` wrappers are removed, and the span is capped at 1,000 characters.

  On the 386 v8 final messages (all three arms; the model's own outputs, counts only), all are single-line
  and `extract_final` changes none of them. It only matters when the model breaks the one-line format.
- **Raw command.** `observations(..., raw_command='')`. When the raw command is given, `observe.shell_script`
  turns it into one shell line:
  - here-document bodies are removed;
  - backslash-newline continuations are joined;
  - every other newline outside quotes becomes ` ; `.

  So `cd /corpus⏎sed -n 'a,bp' f.txt` now yields corpus-located lines instead of raw lines (S2-review 2).
  Corpus file names are still read from the full raw text.
- **Module-path forms.** `python -m packs.officeqa.tools.cli <tool>` is recognised as a tool call. The old
  `packs.officeqa.cli` form is still parsed as well.
- `usage()` and `seed_notes()` were unchanged. The index-build hint is now
  `python -m packs.officeqa.tools.cli build`.

### Item 6: acting settings (`acting.py`)

The decisions made for all v9 arms:

| setting | v8 | v9 |
|---|---|---|
| `features.multi_agent` | true | false |
| `agents.enabled` | true | false |
| `model_auto_compact_token_limit` | 800,000 (inert: the effective value was 244,800) | 244,800, set explicitly |
| `model_context_window` | 1,048,576 | 1,048,576 (unchanged) |
| `include_permissions_instructions` | absent (Codex's text kept) | false: the default of `build_settings` and `run_actor` (`permissions_instructions=False`) |

- These values live in `V9_SETTINGS`.
- `V9_DECLARED_SETTINGS` records each change with its v8 value, its v9 value and the reason.
- Every `invocation.json` now carries `v9_declared_settings` and `auto_compact_token_limit`.
- Temperature is unchanged: the broker sends 0.

What the tests now check:
- **Offline strict config.** In `tests/test_v9_kit.py::test_validate_config_offline`, Codex's
  `--strict-config` plus `config/read` accepts all four values and echoes them. This runs both outside and
  inside the v9 profile.
- **v8→v9 difference.** In `test_v8_v9_differences_are_the_documented_ones`, the settings that differ from
  the recorded v8 invocation are exactly the three changed values plus `include_permissions_instructions`.
  The values are checked too (true→false, true→false, 800000→244800).
- **Real Codex.** It no longer offers the `multi_agent_v1` tool. The tool list is
  exec_command, write_stdin, request_user_input, view_image.

### Item 7: live tests with the real Codex (`tests/test_v9_codex_live.py`)

**How every episode runs now.**
- `acting.prepare_actor_workspace` builds the kit, then `acting.run_actor` runs the episode.
- That means the v9 argv and settings from `build_settings`, the real v9 Seatbelt profile, and the kit
  environment. Controller arms have `submit` and a `HARNESS_SUBMIT_URL` derived from the endpoint.
- The broker sits in front of the fake upstream.
- This replaces the old `settings_for`, which copied the v8 FULL settings (S3 request 16).

The existing 11 tests pass under v9. What changed in them:
- The tool list has no `multi_agent_v1`.
- The permissions toggle now compares `permissions=True` with the v9 default.
- **Is the 244,800 limit accepted and effective?**
  (`test_explicit_244800_limit_is_the_compaction_threshold`, v9 settings unchanged)

  | reported usage | Codex compacts? | upstream requests |
  |---|---|---|
  | 240,000 | no | 3 |
  | 250,000 | yes | 5 |

  `invocation.json` and the argv carry `model_auto_compact_token_limit=244800`. Codex's fallback metadata
  gives the same 244,800, so the behaviour alone cannot tell the explicit value from the fallback. The
  offline echo and the low-limit test below together show that Codex reads and honours the configured value.
- **Does a small limit still compact?** Yes. With the limit lowered to 2,000 for one episode, a reported
  5,000 compacts.
  - With the stub ledger (existing test, both modes).
  - With the **real Ledger over OfficeQAPack** (new, `test_small_limit_compaction_splices_the_real_ledger`):
    - the resume request carries `[harness:ledger] ` plus the real `Ledger.render()` text right after the
      summary, and it stays byte-identical on the next request;
    - `Compaction(request_call=3, summary_call=4)` is recorded;
    - the `ledger.jsonl` fold equals the state in memory;
    - `Command.tool` and `raw_cmd` are recorded.

New real-component tests (real OfficeQAPack with the frameworks-v9 index, Ledger, Gate with the core and pack
seed rules, and MechController):

**`test_submit_round_trip_through_the_kit_wrapper`**
- The model runs the kit's `submit`: the frozen python in the sandbox → the broker's `/v1/submit` → the gate.
- The tool output is `[harness:gate] recorded "42". …`.
- The ledger has one `Candidate(via='submit')` and one `gate` Note. No observation contains gate text.
- No enforcement re-query happens, and `checks/001.json` is written.

**`test_real_gate_enforcement_checks_the_answer_span_and_records_one_note`**
- The prose draft ends in `Final answer: 41`, and the question asks for both values.
- The controller checks only the span `41`.
- The real gate reports "The question asks for 2 value(s); your answer has 1.", and the broker re-queries once.
- `final.md` is the revision, and Codex never saw the draft.
- The ledger holds:
  - `Candidate(via='final', answer='41', call=1)`;
  - exactly one Note: `enforce`, ref `n1`, on call 2. The gate adds none.
- The Note's text equals the feedback the model saw.

### Test configuration (`tests/conftest.py`, new)

- Registers the `slow` and `live_codex` markers, so the `PytestUnknownMarkWarning`s are gone.
- Makes both groups opt-in:
  - `slow`: opt in with a `-m` expression that names it, or `V9_SLOW=1`. `V9_SLOW=all` still widens the
    ledger's v8 sweep to all 399 runs.
  - `live_codex`: opt in with `-m live_codex`, or `V9_LIVE_CODEX=1`.
- The per-module opt-in code in `test_v9_broker.py`, `test_v9_ledger.py` and `test_v9_codex_live.py` was
  removed, so the rules are the same everywhere.
- The two `test_real_codex_*` tests in `test_v9_kit.py` now carry `live_codex`, because they run `codex exec`
  episodes.

## 2. Bug found and fixed: the OfficeQA tables index could not be used across threads

**What went wrong.**
- `otables.connect_ro` opened SQLite with Python's default `check_same_thread=True`.
- The broker's HTTP server handles each Codex request on a new thread, and serves `/v1/submit` on yet another.
- So after the first thread had opened the pack's shared connection, every later use from another thread
  raised `ProgrammingError`. `observe.observations` and the rules catch every exception, so the failure was
  silent.

**Effect.** Reproduced before the fix, with the same page text in the main thread and in a second thread:
- `find_tables` re-derivation: 8 observations in the main thread, 13 in the other;
- `printings`: 60 observations in the main thread, 64 in the other.

The pack's re-run failed, so the table cells fell back to raw lines without their `oq.*` attributes.
Index-backed rules (`later_printing`, `table_alternatives`) would have errored in the submit thread. Wave-1
tests could not see this, because they run in a single thread.

**Fix.**
- The connection now uses `check_same_thread=False` when `sqlite3.threadsafety == 3`. That is the case for
  both the venv python and the frozen python (SQLite 3.53.1).
- In that mode the SQLite library serialises access to the connection itself, and the index is read-only
  and immutable.

**Regression test.** `test_tables_are_usable_from_any_thread` fails with the old line and passes with the new
one; this was checked by reverting the fix temporarily.

## 3. Verification (exact commands and results)

All commands run from `officeqa-pro133-v9-plumbing/`.

| Command | Result |
|---|---|
| `$PY -m pytest -q -p no:cacheprovider tests` (before any change, for reference) | 251 passed, 17 skipped, 86.6 s. S2's 22 slow tests were not yet opt-in, so they were among the passes. |
| `$PY -m pytest -q -p no:cacheprovider tests` | **243 passed, 46 skipped**, 56.2 s. The 46 skips are exactly the opt-in groups: 28 slow + 18 live_codex. |
| `$PY -m pytest -q -p no:cacheprovider -m slow tests` | **28 passed**, 87.2 s (S2 structural 22, broker 2, ledger v8 samples 4) |
| `V9_SLOW=all $PY -m pytest -q -p no:cacheprovider -m slow tests/test_v9_ledger.py` | **399 passed**, 39.8 s: every v8 run through the new three-phase ledger |
| `$PY -m pytest -q -p no:cacheprovider -m live_codex tests` | **18 passed**, 23.3 s (16 in `test_v9_codex_live.py`, 2 in `test_v9_kit.py`) |
| `V9_SLOW=1 V9_LIVE_CODEX=1 $PY -m pytest -q -p no:cacheprovider tests` (everything in one process) | **289 passed**, 0 skipped, 164.1 s |
| `$PY -m pytest -q -p no:cacheprovider tests/test_v9_kit.py` / `V9_LIVE_CODEX=1 …` | 28 passed + 2 skipped / 30 passed |

**Tests added: 21 (268 → 289 collected).**

| file | new | what they cover |
|---|---|---|
| `test_v9_ledger.py` | 5 | raw command and tool; per-pack cap; truncation-aware packs; work outside the lock with ticket order; a failed request still releases its turn |
| `test_v9_engine.py` | 2 | allowed words; enforcement notes that wrap an "accepted" notice |
| `test_v9_broker.py` | 4 | `View.call` per hook; answer-span enforcement; ledger-assigned note refs; loop guard by `Command.tool` |
| `test_officeqa_pack.py` | 5 | raw-command shell lines; ledger truncation path; `extract_final`; allowed words; tables across threads |
| `test_v9_codex_live.py` | 5 | 244,800 threshold (2 cases); real-ledger splice; submit round trip; real-gate enforcement |

**Tests changed.**
- `test_v9_gate.py`: `rejected` status; no Note after a final check.
- `test_v9_kit.py`: the declared v9 settings; the `tools/` closure; the strict-config echo; no sub-agent
  tool; `live_codex` markers.
- `test_officeqa_*.py`: the new import paths; the subprocess CLI test now runs `tools/cli.py` with `-I -B`,
  as the kit does.

**Scratch checks (not tests).** All in the scratchpad `3c1bdbf8-…/scratchpad/`:

| script | what it gave |
|---|---|
| `v8_ledger_coverage.py` | the §1 coverage table |
| `v8_ledger_coverage_rev.py` | the timing check with the variant order swapped |
| `tools_sandbox_smoke.py` | the §1 item 5 sandbox checks |
| `cfgread.py` | the offline config echo |

## 4. Deviations from the brief and from contracts v9.1

1. **Ledger event order within one request.** Command-completion lines now come before that request's
   observation lines. The folded state, the refs and the 399-run check are unchanged.
2. **`Ledger(max_obs_per_output=None)`** is the new default. Before it was `400`; now `None` means "the
   pack's cap, else 400".
3. **`MechController`** gained a `pack=` keyword, an `answer_span()` method and the `no_answer_span` skip
   reason. `controller_runtime` gained `last_answer_line`.
4. **`predicates._gate_finding_after`** looks for "accepted" anywhere in a note. The brief did not ask for
   this; it follows from the controller now owning the enforcement notes (§1 item 3).
5. **`otables.connect_ro` thread fix.** Not requested: this is the bug in §2.
6. **Opt-in behaviour changed for some tests.**
   - S2's 22 slow tests used to run by default; now they are opt-in.
   - `-m slow` alone now runs the ledger's v8 sample test (before it also needed `V9_SLOW`).
   - The broker's slow tests now also accept `V9_SLOW=all`.
7. **`test_validate_config_offline` is not marked `live_codex`.** It starts `codex app-server` only for an
   offline config read: no episode, no model endpoint, about 1 s. It stays in tier 0, where S1's preflight
   request expects it.
8. **The live tests use empty stand-ins for corpus and index.** `build_settings` only checks that they exist.
   The three real-component tests use the real corpus and the frameworks-v9 index, and skip if those are
   missing.
9. **Old module path still parsed.** The `python -m packs.officeqa.cli` form is still recognised by
   `observe.tool_invocation`, even though that module no longer exists. A model cannot run it, but v8-style
   replays stay parseable.

## 5. Gaps and remaining risks

1. **Broker accounting of `rejected`.** `summary.json['submit_invalid']` does not count gate `rejected`
   results until the broker owner changes one condition (requests item 1). It is rare: only payloads the
   broker accepts but the gate refuses.
2. **The controller lock.** `/v1/submit` still waits for a running `on_request`, because the broker holds
   `controller_lock` around the whole hook, including the pack re-derivation. In practice this needs a
   background `write_stdin` session that calls `submit` (requests item 2).
3. **The explicit 244,800 limit** cannot be told apart from Codex's fallback-derived 244,800 by behaviour
   alone. Its acceptance rests on two facts: the strict-config echo, and a configured lower limit being
   honoured.
4. **`extract_final` heuristics** were written by hand. They are a no-op on all 386 v8 finals, and their
   behaviour on non-compliant v9 finals is unmeasured. The pilot's enforce fire rate on right episodes is the
   check (S4-review 1).
5. **`RawPack` and `ToyPack`** have neither `raw_command` nor `extract_final`. The defaults cover them
   (requests item 3).
6. **The actor can list `tools/__pycache__`** (names only; the contents are unreadable). It exists because
   the controller imports the tools. Unchanged in kind from wave 1.
7. **Scratchpad housekeeping.** In this shared scratchpad, S1-review's pre-review copies `orig/acting.py`
   and `orig/kit.py` were overwritten by S7's own backup step, which copied the then-current files there.
   Those copies were scratch only. S7's backups are in `scratchpad/s7-orig/`.

## 6. Files changed (all S7-owned)

`broker.py` change (`_view` signature and docstring, plus three call sites): `_view(exchange, call)`. The three
call sites are:
- `_prepare`: the call number;
- `on_final`: `entries[-1]['number']`;
- the re-query `on_response`: that call's number.

| area | files |
|---|---|
| harness_core | `harness_core/ledger.py`, `gate.py`, `predicates.py` (`engine.py` unchanged) |
| controller and broker | `controller_runtime.py`, `broker.py` (the `_view` change above) |
| acting and kit | `acting.py`, `kit.py` (one docstring path) |
| OfficeQA pack | `packs/officeqa/`: `tools/__init__.py` (new), `tools/cli.py` and `tools/otables.py` (moved; docstrings, thread-safe connection), `pack.py`, `observe.py`, `rules.py` (import), `spec.py` |
| tests | `tests/conftest.py` (new), `test_v9_ledger.py`, `test_v9_gate.py`, `test_v9_engine.py`, `test_v9_broker.py`, `test_v9_kit.py`, `test_v9_codex_live.py`, `test_officeqa_pack.py`, `test_officeqa_tables.py` |

Requests to other owners: `officeqa-v9-design/wave2/S7-integrate-requests.md`.
