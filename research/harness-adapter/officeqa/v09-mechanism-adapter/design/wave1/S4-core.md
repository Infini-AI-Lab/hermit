# S4: harness_core (ledger, rule engine, gate, isolated runner, calc/submit, replay, toy pack)

**Status: built and tested.**
- **Tests:** 63 pass, 4 are skipped by default (the slow tier); the slow tier passes on its own, including all 399
  recorded v8 runs.
- **Neighbours:** S2's pack tests and S3's broker tests (94 tests) also pass against this code.
- **Cost:** no model calls, no network except `127.0.0.1`.
- **Data rules:** no grader or reference file was opened. Correctness labels were not needed.
- **This report:** contains no answer values.

Paths are relative to `officeqa-pro133-v9-plumbing/`.

## 1. What was built

| File | Lines | What it does |
|---|---|---|
| `harness_core/numeric.py` | 289 | Number handling shared by everything else. See the list below. |
| `harness_core/ledger.py` | ~540 | `Ledger`, which implements `contracts.LedgerAPI`, plus helpers: `normalize_output`, `command_text`, `parse_calc_frames`, `is_compaction_request`, `load_events`/`fold_lines`, `ledger_prefix_sha`. |
| `harness_core/observe.py` | 98 | `raw_line_observations`: one Observe per output line that contains numbers (`attrs['values']`). `RawPack` is the smallest valid Pack; it is used for the v8 check. |
| `harness_core/predicates.py` | ~245 | Core `when` vocabulary and shared helpers (see §2.2). |
| `harness_core/offers.py` | ~235 | Core offers `unscale`, `use_alt` and `recompute(substitutions)`. |
| `harness_core/engine.py` | ~190 | `RuleEngine` (`contracts.Engine`): trigger filter, AND of predicates, join of bindings, priority order, safe templates, offers, per-rule trace. |
| `harness_core/rules.py` | 164 | The five core seed rules, `validate_rule`, `validate_policy`/`effective_policy`, `load_rules`/`save_rule`, `rendered_text_bytes`. |
| `harness_core/gate.py` | ~220 | `Gate` (`contracts.GateAPI`): `submit`, `check_final`, `needs_enforcement`; the pure state machine `decide`; check files. |
| `harness_core/runner.py` | ~145 | `SandboxRunner` (`contracts.ComputeRunner`), `default_python`, `make_runner`. |
| `harness_core/replay.py` | ~280 | Offline replay: `load_episode`, `final_context`, `rule_outcome`, `fire_stats`, `wilson`, `precision_check`, `counterfactual`, `recheck`, `ledger_from_api_records`. |
| `harness_core/tools/calc.py`, `tools/submit.py` | 118 / 114 | Actor command-line tools. Standard library only; they run standalone under `python -I`. |
| `packs/toy/` | ~480 | Complete toy Pack: `corpus.py`, `tools.py`, `pack.py`, `items.py`, `sim.py` (episode simulator). |
| `tests/test_v9_{ledger,engine,gate,runner,replay}.py` | ~1,000 | 63 fast tests, plus a slow tier (`V9_SLOW=1`: 4 v8 runs; `V9_SLOW=all`: 399). |

What `numeric.py` provides:
- `number_tokens`: finds numbers in text, with the span of the digits, so an offer can swap one number in an answer and
  keep the rest of the text.
- `code_literals`: finds the number literals in calc code, using Python's own tokenizer. Names such as `x1` and
  `float64` never count as numbers.
- `ValueIndex`: a sorted index of every value seen, used to link literals to earlier outputs.
- `rounds_to`, `replace_number`, `short`: rounding comparison, number replacement and compact number text.

`harness_core` imports only the standard library and itself, never `packs`. A test enforces this with an `ast` scan.

## 2. Behaviour, by DESIGN layer

### 2.1 L2 ledger (`Ledger(pack, out_path)`)

**Input.**
- The ledger is fed only with the Responses API items the broker relays. It reads no file.
- `out_path` is opened with `O_CREAT|O_EXCL|O_NOFOLLOW`. An existing file or a symlink is refused (engineering review
  #2).

**Commands.**
- `ingest_response` records each `function_call` as a `Command`.
- For `exec_command`, the command text is the `cmd` argument with whitespace collapsed. Shell-list commands keep their
  script. Other tools become `<name> <canonical JSON arguments>`.
- The running count `n` and `command_count(cmd)` live in the ledger object, so they survive compaction.
- `command_output_count(cmd, sha)` counts one command together with one exact output.
- Commands are keyed by `call_id`. Duplicates are ignored, which makes a retried Codex request safe.
- A call that only ever appears in a later request is registered as a fallback.

**Outputs** (`ingest_request`).
- Each new `function_call_output` is matched to its Command by `call_id`.
- The `Chunk ID:` and `Wall time:` lines are removed before hashing, so identical outputs get identical
  `output_sha256`. The exit code is parsed.
- Truncation is detected from Codex's `…N chars|tokens truncated…` marker and its `Warning: truncated output` line.
- The Command line is written again with these output fields (see §4, item 1).

**Observations.**
- Taken from the output body only: Codex header lines and notice lines are dropped, and so is any line cut by a
  truncation marker.
- The gate's own texts (Notes added through `add`) are cut out first, so the harness's words are never evidence.
- `calc` frames are cut out before `pack.observations(...)` is called. They become `Compute` events instead, so the
  code's own literals can never ground themselves.
- An output identical to one already processed yields no new observations. This is the v8 loop case.
- At most 400 observations are kept per output.
- If the pack raises, the error goes to `ledger.errors` and the relay carries on.

**Computes.**
- Only frames whose sha matches are "complete". Code that `calc` truncated to 8 KB keeps a 12-hex sha and is never
  re-executed.
- Every numeric literal in the code becomes an entry in `inputs`, linked to the latest earlier Observe or Compute that
  matches it:
  - `exact`;
  - `rounded` to the literal's own precision;
  - `scaled` by 10^k, for k in ±2, ±3, ±6, ±9, and only when the literal has at least 3 significant digits.
- Small integers (≤ 12) and powers of ten are marked `trivial`. They are never linked or flagged.

**Compaction.**
- A compaction request is: `tools == []` (when the tools list is passed) and the last user message starts with the
  fixed `CONTEXT CHECKPOINT COMPACTION` prompt.
- A resume is the `Another language model started…` user message. It is keyed by item id plus text hash, so the same
  summary seen on every later request counts once.
- One `Compaction{request_call, summary_call}` event is written per resume. `compaction_requests` lists every
  tool-less request.

**Persistence.**
- Every event is appended as one JSON line and flushed.
- `snapshot()` returns (events, lines, sha256 of those lines) atomically. The gate records the last two.
- The ledger is thread-safe (one `RLock`), because `/v1/submit` arrives on another thread than the relay.

**`render(max_bytes)`.**
- Deterministic.
- Order: header, candidates, calc results with their linked inputs (newest first), the values those calcs cite, then
  the most recent values.
- Never exceeds `max_bytes` (UTF-8). S3 adds the `[harness:ledger]` prefix.

### 2.2 Core predicates and offers

Calling conventions:
- Predicate: `pred(ctx, **args) -> list[Binding]`.
- Offer: `offer(ctx, binding, **args) -> OfferResult | None`.
- Predicates only look at events *before* the candidate (`predicates.prior_events`). A counterfactual candidate
  therefore sees the same evidence.

Predicates:
- **`count_mismatch`**: `spec.n_values` differs from `len(pack.numbers(answer))`.
- **`not_from_compute(allow_observed=True)`**: an answer number that equals no calc output after rounding to the
  answer's own precision. By default a number that some tool printed also passes, so a plain look-up does not fire.
- **`scaled_100(forms=('decimal','unspecified'))`**: two directions.
  - `x100`: the answer is 100× a calc output while `percent_form` is in `forms`.
  - `div100`: the answer is 1/100 of a calc output while `percent_form == 'percent'`.
- **`ungrounded_inputs(scope='candidate')`**: non-trivial calc literals with no source. It looks at the computes that
  produced the answer and, transitively, at the computes those consume. `scope='all'` looks at every compute.
- **`competing_candidates`**: the model's `--alt`, or an earlier candidate it abandoned *on its own*. A candidate
  followed by a gate finding does not count, so accepting an offer never ping-pongs.

Offers:
- **`unscale`**: the output the answer is 100× (or 1/100) of. The percent sign is dropped. Decimals follow `spec`,
  otherwise the candidate's decimals ± 2.
- **`use_alt`**: the model's own alternative.
- **`recompute`**: re-runs the recorded calc in the runner with substituted literals.
  - The substitutions come from rule args `{'from': path, 'to': path}` or from a binding key `substitutions`. Each
    substitution is either `{from: ref, to: ref}` or `{from: ref, value, label}`.
  - The same 10^k scale as the original link is kept.
  - A sign flip is handled.
  - The answer number is read from the matching output position, formatted like the candidate.
  - The derivation cites locators, e.g. `calc <sha12> re-run with <new locator> in place of <old locator>`.

Shared helpers that S2's OfficeQA pack imports: `facts`, `from_computes`, `token_dict`, `same_answer`,
`offers.answer_decimals`. Keep these stable.

### 2.3 Engine

A rule fires in these steps:
1. **Filter.** Disabled rules are skipped. Trigger keywords (no regex) must match: empty or `'*'` means always; a
   phrase matches a contiguous word sequence of the question.
2. **Predicates (AND).** Every predicate is called. The bindings are joined; a key present on both sides must have
   equal values. At most 64 joined bindings are kept.
3. **Offer.** The offer is tried on up to 3 bindings. An offer equal to the candidate is dropped.
4. **Finding.** The template is rendered.

Name lookup order: core first, then the extra registries, then the pack. A pack cannot shadow a core name.

Trace status per rule: `fired | fired_no_offer | silent | no_trigger | disabled | n/a (unknown predicate/offer) |
error (the predicate or offer raised, or returned the wrong type) | offer_failed (the template needs {offer.*} but
there is none)`.

Templates are rendered without `str.format`:
- Only `{a.b.c}` dotted lookups into dicts and lists are replaced.
- A field value is at most 80 characters; the whole text is at most 200 bytes.
- `{x:>999}`, `{x!r}`, `{0}` and `{x.__class__}` stay literal text or become `?`.
- Substituted values are never expanded again.

### 2.4 Gate (L4)

**What `submit` does.**
1. Validates the payload (SUBMIT_FIELDS, strings, size caps).
2. Records `Candidate(via='submit')`.
3. Takes a ledger snapshot and evaluates the enabled rules. Rules disabled by the rule itself or listed in
   `policy['disabled_rules']` are skipped.
4. Applies `decide`.

**`decide`, per candidate value.** The value key is numeric, so `12.30` and `12.3` count as one value.
- At most one finding is shown per round.
- At most `gate_rounds_max` distinct findings are shown per value. After that, and when nothing new remains, the
  submission is accepted.
- The same finding is never shown twice for one value.
- `--keep "<reason>"` suppresses the findings already shown for that value.
- A value equal to an alternative the gate itself offered is accepted as is. The harness never contradicts its own
  offer.

**Message.** Two to four lines, per the effect review (P3). The Offer and Accept lines appear only when there is an
offer:
```
[harness:gate] <finding>
Offer: <alt> (<derivation>)
Accept: submit --answer "<alt>"
Keep: submit --answer "<answer>" --keep "<reason>"
```
- Otherwise: `[harness:gate] recorded "<answer>". End your turn with exactly this answer as your final message.`
- Answers are shell-quoted.
- A test asserts that no message contains "are you sure", "double-check" or similar.

**What the gate records.**
- Every message is also added to the ledger as `Note(reason='gate', sticky=False)`. For `check_final` the reason is
  `'enforce'`.
- When the shown finding has a recompute offer, its run is recorded as `Compute(source='gate:<rule>')`. An accepted
  offer is then a calc output.
- `check_final` records `Candidate(via='final')` and runs the same machine.
- `needs_enforcement` returns true when nothing was submitted, or when the final text differs canonically or
  numerically from the last submission.

**Check files.** Every check writes `<api>/checks/NNN.json`, atomically (temp file, then rename). It holds:
- question, spec, payload, candidate;
- ledger position `{lines, sha256}`;
- the full rules and policy;
- the gate state before the check;
- the engine trace, all fired findings, and the shown result.

`replay.recheck` re-derives the shown result from these fields.

### 2.5 Runner (`SandboxRunner`)

Each call runs:
```
/bin/sh -c 'ulimit -t 2 && ulimit -f … && ulimit -n 64 && ulimit -c 0 && exec /usr/bin/env -i sandbox-exec -p <profile> <python> -I -B main.py'
```
- It runs in a fresh temp directory, which is deleted afterwards.
- Stdout goes to a file, so a flood of output stops at the file-size limit (1 MB).
- A 5 s wall timeout kills the process group.
- At most 16 KB of stdout is returned.

About the pieces:
- `sandbox-exec` is macOS's command-line sandbox. It applies a rule profile to one process.
- `/bin/sh ulimit` sets the resource limits (CPU seconds, file size, open files, core files). It replaces
  `preexec_fn`, which can deadlock the child when the parent (the broker) runs several threads.
- `env -i` starts the child with an empty environment, so the shell's `PWD`/`SHLVL` do not leak.

The profile:
- Denies everything by default.
- Allows reading and mapping only the python prefix (the `python` argument), `/usr/lib`, `/System` and any
  `extra_read` roots, plus metadata of their parent directories.
- Allows listing `/`: dyld, macOS's dynamic loader, aborts without it (found empirically).
- Allows reading and writing the fresh temp directory, `/dev/null` and `/dev/urandom`.
- Allows exec of the python binary only. Fork is not allowed, so no subprocesses.
- `deny network*`.

Which python:
- `default_python()` uses `frameworks-v9/actor-python/bin/python3` when it exists, otherwise the codex runtime python.
- `HARNESS_RUNNER_PYTHON` overrides both.
- `make_runner` returns a runner that always declines when the sandbox is unavailable. Offers then return `None`;
  nothing runs unsandboxed.

### 2.6 Actor command-line tools

**`calc -c CODE | FILE | stdin`**
- Runs the code with the kit python. If the last statement is an expression, it echoes its value, like the Python
  REPL (the interactive prompt).
- Prints the exact contracts frame. Code is capped at 8 KB and stdout at 2 KB, each with `… [truncated]`.
- `⟦`/`⟧` in stdout are neutralised, so the code cannot fake a frame.
- On error, the frame is still printed; the traceback tail goes to stderr and the exit code is 1.

**`submit --answer TEXT [--alt] [--reading] [--keep] [--artifact PATH]`**
- POSTs JSON to `$HARNESS_SUBMIT_URL`.
- Refuses any URL other than `http://127.0.0.1:<port>/v1/submit`: no userinfo, query or fragment.
- Uses no proxies and follows no redirects.
- `--artifact` sends the file's sha256.
- Prints `message` from S3's JSON reply.
- Exit codes: 1 = the gate rejected the submission; 2 = a usage or environment error.

### 2.7 Replay

All functions return ids, booleans and counts only; no answer or reference values.

- **`fire_stats(rules, episodes, pack, runner)`**: per rule, on each episode's final answer:
  - fired on right and on wrong episodes;
  - number of offers, and `n/a` and error counts;
  - `precision` = P(wrong | fired), with a Wilson interval;
  - right-episode fire rate, with a Wilson interval.

  A Wilson interval is a confidence interval for a proportion that stays sensible at small counts; for example,
  0 fires in 10 right episodes still has an upper bound of 0.28.
- **`precision_check(stat, max_right_fire=0.20, min_right=10, min_fired_wrong=1, use_upper_bound=True)`** implements
  DESIGN §3 step 2 (precision over released episodes).
- **`counterfactual(rule, episode, reference, grade_fn, pack=, runner=)`** implements DESIGN §3 step 1. It admits a
  rule only when all of these hold:
  - the episode is labelled wrong;
  - the rule fires;
  - its offer grades right;
  - the rule is silent when the candidate is set to the reference.

  `grade_fn` is injected, and a float grade must be ≥ 1.0 to count as right.
- **`ledger_from_api_records`** rebuilds a ledger from the recorded `api/NNN/{request,response}.json` files. It is
  used for v8.

### 2.8 Toy pack

- **Corpus.** Three generated "Widget Bulletin" text files, each with two pipe tables. The 2020 figures exist in a
  first printing and a revised printing; revised cells carry an `r` suffix.
- **`lookup`.** A command-line tool that prints tagged cells: `⟦toy file:line|period|row|raw⟧`.
- **Observations.** A tag parser, with raw-line observations for everything else.
- **`question_spec`.** Reads quarters, decimals, percent/decimal form, "both" (two values) and "as first printed"
  (the question pins the printing).
- **Pack predicates.** `later_printing` and `period_mismatch`. Both offer the core `recompute`.
- **Items.** 10 synthetic items with synthetic references and a strict toy grader.
- **Simulator (`sim.py`).** Drives synthetic Codex items through the Ledger and the Gate in the broker's order:
  - `function_call` items carry `exec_command` arguments;
  - `function_call_output` items carry the real Codex header;
  - calc frames are real, from executing the code;
  - `submit` goes to `Gate.submit`.

  Five scripts contain the mistake one rule targets. Two right scripts produce false fires that the scripted model
  keeps.

## 3. Verification (exact commands and results)

All commands run from `officeqa-pro133-v9-plumbing/`. `PY` = `/Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd/adapter-comparison-work/.venv/bin/python` (python 3.12, pytest 9.1).

| Command | Result |
|---|---|
| `$PY -m pytest -q -p no:cacheprovider tests/test_v9_ledger.py tests/test_v9_engine.py tests/test_v9_gate.py tests/test_v9_runner.py tests/test_v9_replay.py` | **63 passed, 4 skipped in 8.9 s** (the skips are the slow tier) |
| `V9_SLOW=1 $PY -m pytest -q -p no:cacheprovider -m slow tests/test_v9_ledger.py` | **4 passed in 3.6 s** (v8 none 072 and 053, prompt 071, cards 100) |
| `V9_SLOW=all $PY -m pytest -q -p no:cacheprovider -m slow tests/test_v9_ledger.py` | **399 passed in 41.2 s** (every v8 run) |
| `$PY -m pytest -q -p no:cacheprovider tests/test_officeqa_pack.py tests/test_v9_broker.py` (S2's and S3's tests, run read-only) | 94 passed in 80 s |
| `$PY -m pytest -q -p no:cacheprovider tests/test_v8_broker.py` | 14 passed (unchanged) |

The slow tier warns `PytestUnknownMarkWarning` until S6 registers the `slow` marker (requests file, item 14). Add
`-W ignore::pytest.PytestUnknownMarkWarning` to silence it.

What the tests prove, beyond unit behaviour:

**Toy end to end** (`test_v9_replay.py`, `test_v9_gate.py`).
- Without the gate, 5 of 10 episodes end wrong. With the gate and the real sandbox runner, all 10 end right. Each
  mistake is caught by its rule, and the offer reproduces the reference.
- False fires on right episodes (`competing_candidates`, `recompute_final`) are kept by the scripted model.

**Replay** on the no-gate episodes.

| Rule | Fired on wrong | Fired on right |
|---|---|---|
| `later_printing` | 2 (2 offers) | 0 |
| `scale` | 1 | 0 |
| `answer_count` | 1 | 0 |
| `period_match` | 1 | 0 |
| `recompute_final` | 0 | 1 |

- `counterfactual` admits `later_printing`, `scale` and `period_match` on their target episodes.
- It refuses `answer_count` (no offer), `recompute_final` (the episode is not wrong), a rule that does not fire, a
  rule that also fires on the reference, and a 0.99 float grade.
- `precision_check` refuses 0/5 right fires because of the minimum count (`min_right=10`). With `min_right=5` it still
  refuses on the Wilson upper bound (0.43).

**Online equals offline.** `recheck` re-derives every stored check identically: 15 or more checks over 10 gated toy
episodes, plus 5 more in the gate test. A tampered ledger sha is detected.

**Sandbox** (`test_v9_runner.py`, with a real `sandbox-exec`).
- Denied: TCP to a live local listener (the listener sees no connection), UDP, and reads of a file in pytest's
  `tmp_path`, `~/.zshrc`, `harness_core/contracts.py` and `/etc/passwd`.
- Denied: listing `$HOME`; writes outside the temp dir; `subprocess` and `os.fork`.
- The environment inside is `{}`, and `sys.flags.isolated == 1`.
- A CPU busy loop is killed by SIGXCPU in under 4.5 s. A sleep is killed at the wall timeout. Output is capped.
- numpy works inside.
- `extra_read` grants a corpus dir.

**`submit`.**
- Refuses 7 kinds of bad URL.
- Posts exactly the SUBMIT_FIELDS.
- Ignores `HTTP_PROXY`, and does not follow a 302 redirect.
- A 400 reply exits 1; an unreachable gate exits 2.

**Offline check against real v8 traffic** (read-only; trivial `RawPack`; `V9_SLOW=all`, 399 runs):
- **Crashes:** 0 crashes and 0 ledger errors.
- **Command counts:** the ledger's commands equal the distinct `function_call` ids in the responses (21,565 in total
  over 22,086 calls). They also equal `events.jsonl` command starts + first-seen `exec_command failed: CreateProcess`
  outputs + `write_stdin` calls. The model issued these, but Codex logs no `command_execution` for them; this explains
  none/072's 356 vs 355.
- **Compaction:** detected in 72 runs; 119 compaction requests; 118 resumed. This equals `understand/compaction.md`:
  72 runs, 119 compactions, 118 completed. The one unresumed request is prompt Q71's 8th compaction, in flight at
  timeout.
- **Unmatched outputs:** exactly one command in all 399 runs has no output (in flight at a timeout).
- **Render:** `render(8000)` stayed ≤ 8,000 bytes in every run.

**Timings:**

| Measurement | Result |
|---|---|
| Ledger rebuild, 4 samples: none/072 (359 calls; 356 commands; 2 compactions; max repeat 227) | 0.39 s |
| none/053 (182 calls) | 0.21 s |
| prompt/071 (644 calls; 8 compaction requests, 7 resumed; 6,582 raw observations) | 0.96 s |
| cards/100 (541 calls) | 0.68 s |
| Full sweep: 399 runs including the independent recounts | 40.7 s |
| Ledger time per run | median 0.024 s, max 0.96 s |
| All 5 core rules on the largest v8 ledger (7,225 events) | 16 ms |
| `render(8000)` | < 1 ms |
| Sandbox run, `print` | median 25 ms (max 49 ms) |
| Sandbox run, numpy import | about 90 ms |

## 4. Deviations from contracts.py (all additive; requests in `S4-core-requests.md`)

1. **`ledger.jsonl` writes a Command twice** under one `ref`: issue, then completion. Readers fold by `ref`
   (`load_events`). Why: the first N lines reproduce the exact ledger state at any moment, which the check files
   depend on.
2. **`Ledger.ingest_request(..., *, tools=None)`** takes an optional keyword.
3. **Extra Ledger methods and attributes:** `snapshot()`, `lines`, `command_output_count()`, `next_ref()`,
   `compaction_requests`, `errors`, `close()`.
   - `add()` assigns a missing ref and raises on a duplicate ref.
   - `add()` indexes added Observes and Computes, so values the gate offered are linkable later.
4. **Extra Gate method and attributes:** `needs_enforcement(final_text, ledger)`, `summary` (per check: kind, status,
   rules shown and fired; no values), `checks`.
5. **Unusable payload.** An unusable `submit` payload returns `status='findings'` with `findings=[]` and the message
   `[harness:gate] rejected: …`. It never raises, and records no candidate.
6. **`Compute.inputs` extra keys:** `trivial`, `in_string`, `scale`. For a truncated frame, `Compute.code_sha256` holds
   only the 12-hex prefix. That is how offers know the code cannot be re-executed.
7. **Interpretations of the brief:**
   - `scaled_100` fires in the 1/100 direction only when the question asks for a percent. Firing when it does not
     would flag correct percent-to-fraction conversions.
   - `not_from_compute` lets values some tool printed pass (`allow_observed=True`), so pure look-ups do not fire. A
     rule can pass `allow_observed: false`.
   - The gate accepts a value equal to an offer it showed.
   - The engine drops an offer equal to the candidate.
   - The shown recompute run is recorded as `Compute(source='gate:<rule>')`.
   - Gate messages are Notes with `sticky=False`.

## 5. Known gaps

- **One calc per recompute.** `recompute` re-runs only the calc that read the substituted cell. A chain k1 → k2 (k2
  typed k1's printed value) is not propagated. The offer then returns `None`, or S2's `depth` guard refuses it.
- **Value-based linking.** A literal is linked to the *latest* earlier value that matches. When the same number appears
  in several cells, the link can name a different cell than the one the model read. Scaled links need at least 3
  significant digits and k in {±2, ±3, ±6, ±9}.
- **Observation caps.** Raw-line observations stop at 400 per output and 64 values per line. An output identical to one
  already seen adds nothing, which is intended for loops.
- **Memory.** The sandbox does not bound memory: `RLIMIT_AS` is not enforced on macOS. A process can allocate a lot
  within its 2 s of CPU. The profile also allows listing `/` and metadata of the read roots' parent directories: names
  only, no file contents.
- **Gate state is in RAM** (shown, kept, offered per value). It is lost if the broker restarts mid-episode.
- **`calc` code without `-I`.** `calc` runs the model's code with the kit python *without* `-I`, so workspace-local
  imports work. It runs inside the actor sandbox, not the runner sandbox.
- **Toy scope.** The toy corpus is small and its predicates are shallow. It exercises the plumbing, not the precision
  of real rules.
- **Pilot numbers.** `not_from_compute` and `ungrounded_inputs` over v8 cannot be measured: v8 has no calc frames
  (compute count 0). Their real fire rates first appear in the pilot.
