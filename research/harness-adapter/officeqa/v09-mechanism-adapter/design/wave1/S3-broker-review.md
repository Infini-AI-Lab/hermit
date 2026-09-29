# S3-broker: adversarial review (wave 1)

Reviewer of stream S3: the broker controller hook, `/v1/submit`, splices, loop guard and enforcement. Paths are
relative to `officeqa-pro133-v9-plumbing/`. Requests to other owners are in `S3-broker-review-requests.md`.

No paid call and no network except 127.0.0.1. No answers, graders or credential files were opened, and no API key
was read.

## Terms

- **Enforcement re-query.** The model's final answer was never checked with `submit`. The broker asks the model once
  more with the draft plus the gate's feedback, and forwards the revision's own reply to Codex. The draft stays in
  RAM, so the broker can still forward it if the revision fails.
- **Splice.** A broker-owned note inserted after a Codex item (its *anchor*). It is re-sent on every later request
  until the anchor disappears, which happens at a compaction.
- **Actor deadline.** The time at which the campaign kills the Codex process (v8: 3600 s). A killed actor yields
  an empty prediction.
- **Fail open.** When the controller throws an error, the broker behaves exactly as v8 did and records the error.

## 1. Test results

Interpreter: `adapter-comparison-work/.venv/bin/python` (3.12, pytest 9). Run from `officeqa-pro133-v9-plumbing/`.

| Command | Before review | After review |
|---|---|---|
| `python -m pytest tests/test_v8_broker.py tests/test_v9_broker.py tests/test_v9_codex_live.py -q -p no:cacheprovider` | 53 passed, 13 skipped (33.8 s) | **59 passed, 13 skipped** (42.7 s) |
| `python -m pytest tests/test_v9_broker.py -q -p no:cacheprovider -m slow` | 2 passed (52.9 s) | **2 passed** (54.6 s) |
| `python -m pytest tests/test_v9_codex_live.py -q -p no:cacheprovider -m live_codex` (real pinned Codex, sandbox-exec) | 11 passed (10.2 s) | **11 passed** (9.9 s) |
| `python -m pytest tests/test_v8_broker.py -q -p no:cacheprovider` | 14 passed | **14 passed** |
| `python -m pytest tests -q -p no:cacheprovider` (every stream's tests) | – | **245 passed, 17 skipped** (91 s) |

- The implementer's numbers reproduce.
- The review added 6 tests: the two `test_14` deadline tests, `test_upstream_url_guard`,
  `test_oversize_insert_fails_open`, `test_policy_is_validated_against_ranges` and
  `test_submit_invalid_payload_is_not_a_gate_round`. It also extended 3 existing tests: 9/16/17, 13, and
  `normalize_output`/`command_of`.
- The only warning is `PytestUnknownMarkWarning` for `slow` and `live_codex`. Registering the markers is S6's job
  (implementer request 6).

Scratch integration with S4's real `Ledger`, `Gate` and `RawPack` (not a test, because tests must not import S4). One
CodexSim episode ran 3 identical commands, a compaction, a resume, and a final answer that was re-queried
(`requery_when_clean=True`, since `RawPack` produces no findings). Result:
- 0 controller errors;
- the inserts were `{ledger: 1, loop_guard: 1, enforce: 1}`;
- `final.md` would equal the revision;
- the enforce Note was recorded on the re-query's own call.

## 2. Defects found

| # | Sev. | Where | Defect | Status |
|---|---|---|---|---|
| 1 | **High** | `broker.py:_after`, `_call` | The enforcement re-query had **no deadline tied to the actor deadline**. `enforce_min_remaining_s` (≥ 30 s) is checked before the re-query starts. In the FULL profile the revision call itself is unbounded, and v8 recorded stalls of 603–1460 s. The stalled revision holds the draft, so the actor is killed and the run records an empty prediction, although a complete draft answer sat in RAM. **Probe:** 35 s left, revision stalls; 10 s *after* the deadline Codex still had no response. | **Fixed.** The revision's total deadline is now `actor_deadline − now − REVISION_DEADLINE_MARGIN_S` (15 s), or `request_timeout` if that is smaller (`broker.py:48, 659-668, 700`; `_call(timeout=…)` at `:340`). On expiry the existing fallback forwards the draft. If less than the margin is left, the re-query is skipped (logged `revision_skipped`). Tests: `test_14_revision_is_bounded_by_the_actor_deadline` (draft forwarded after about 3 s), `test_14_revision_skipped_inside_the_deadline_margin`. |
| 2 | Medium | `broker.py:_controller_record` | **Private reasoning was written to `api/NNN/controller.json`.** `inserted[].item` stored `public(item)` for the echoed draft. `public()` removes reasoning items from lists but not a reasoning dict passed on its own, so the draft's reasoning `summary` and `content` text were recorded. v8's rule, repeated in `summary.json`, is "omitted from public request/response records". **Probe:** `PRIVATE-CHAIN` was found in `api/002/controller.json`. | **Fixed** (`:739`). Reasoning items are recorded as a skeleton `{type, id, keys, private: true}`, and `item_sha256` still covers the full item. `test_9_16_17…` now asserts that the skeleton is used and that the reasoning text appears in no `api/` file. |
| 3 | Medium (defensive) | `broker.py:Broker.__init__` | **The upstream URL guard could be bypassed.** `startswith('http://127.0.0.1:')` accepted `http://127.0.0.1:80@evil.example/…`, whose host is `evil.example`. The Bearer key would then go over plain http to that host. Only trusted campaign config sets this value, but the docstring promised "only https or 127.0.0.1". | **Fixed** with `_upstream_ok` (`:52`). It parses the URL and allows `https` to any host, or plain `http` only to host `127.0.0.1` with an explicit port and no userinfo. Test: `test_upstream_url_guard`. |
| 4 | Medium | `controller_runtime.py:MechController.__init__` | **The policy was not validated.** It was merged over `POLICY_DEFAULTS` without checking `POLICY_RANGES`, so a learned `policy.json` could silently run, for example, `loop_threshold=1` or `enforce_requery_max=5`, which is an undeclared regime. S4's gate already refuses such a policy (`rules.effective_policy`). | **Fixed** with `checked_policy` (`:311`). It uses the same rule as S4's `validate_policy`, copied locally so there is no S4 import. It raises `ValueError` on an unknown key or an out-of-range or non-int value. Test: `test_policy_is_validated_against_ranges`. |
| 5 | Low | `controller_runtime.py:on_final` | **The enforce `Note` was recorded before the re-query was sent.** On a transport fallback the ledger claimed the model saw feedback it never saw. The contract defines `n<n>` as "harness text shown to the model". | **Fixed** (`_settle_note` `:381`, `:437`). The feedback is held as pending. It is recorded when the re-query's own response arrives, using that call number (bound through `bind_call`). Otherwise it is dropped and logged `enforce_not_shown`. Tests: test 9 (the Note is on call 2) and test 13 (no Note after a fallback). |
| 6 | Low | `controller_runtime.py:command_of` | **`command_of` did not match S4's `command_text` for `{"command": "<string>"}` arguments.** `command_of` produced `shell {json}`; S4 produces the command. The offline replay and the runtime loop counts could then disagree. | **Fixed** (`:148`). Test added in `test_normalize_output_and_command_of`. |
| 7 | Low | `broker.py:_register`, `_after` | **Insert and feedback text had no size cap.** A faulty controller could push unbounded text upstream. | **Fixed.** The cap is `MAX_NOTE_BYTES = 16,384` (`:47, 582, 675`). Oversized text becomes a recorded controller error and fails open. Test: `test_oversize_insert_fails_open`. |
| 8 | Low | `broker.py:_submit` | **`gate_rounds` also counted rejected submits.** S4 returns a malformed-payload rejection as `status='findings'` with an empty findings list, and these were counted as gate rounds. | **Fixed** (`:832`). The new summary field is `submit_invalid`. Test: `test_submit_invalid_payload_is_not_a_gate_round`. |
| 9 | Low (perf) | `controller_runtime.py:_note_event` | **Note refs were found by an O(events) scan per note.** | **Fixed** (`:376`). The controller now uses `ledger.next_ref('note')` when the ledger has it (S4 does), and keeps the scan as a fallback. |

`controller=None` byte-identity still holds. `test_1` compares with the read-only v8 `broker.py` in both profiles. The
new `_call(timeout=)` argument defaults to `request_timeout`, so the v8 path is unchanged.

## 3. Checked and found sound

- **v8 equivalence** (`test_1`): the v8 per-call code was moved, not changed. The review's `diff` confirms this.
- **Splice placement:**
  - Splices are placed after their anchor in creation order.
  - They are deferred past open calls, so a note never sits between a call and its output.
  - A compaction request still contains the old notes, so the summariser sees them. The resume request drops
    them and records the drop.
  - The ledger splice comes first after the summary and stays frozen: `render` is called once.
- **Enforcement splice ordering:**
  - Same-request loop notes precede the draft and feedback, both in the re-query body and in later requests.
  - A Codex retry of the request after a forwarded revision gets the re-query body.
  - The fallback reorders `entries` correctly when several rounds were made.
- **Security:**
  - The controller reads no files. Hooks get deep copies, and records are redacted.
  - `/v1/submit` returns no bindings or records, rejects unknown fields and non-string fields, caps sizes,
    returns 404 without a controller, and never takes the response lock.
  - `controller_runtime` imports only `harness_core.contracts`, and `harness_core` stays stdlib-only.
- **Live sandbox:**
  - `(deny network*)` plus outbound TCP only to `localhost:<broker port>`, and file writes only inside the scratch
    root. A curl from a model command to another local port fails.
  - The scripted fake model issues only fixed commands.
- **Determinism:** loop-note and enforcement text depend only on ledger state and policy. The replay goldens are
  stable across two runs.

## 4. Interface deviations from `contracts.py` (after review)

The implementer's list in `S3-broker.md` §4 still applies: `bind_call`/`drain_log` duck hooks, `needs_enforcement`
delegation, `on_final` also on resume, and v9 record fields only in controller mode. The review adds four:
- `MechController(…, policy)` now **raises `ValueError`** on an invalid policy. The contract does not specify this.
- An enforce `Note` enters the ledger only after the re-query is sent. The broker must call `bind_call` before
  the re-query, and it does. Without that hook the Note is recorded at the next `on_response`.
- `Broker._call(…, timeout=None)` is an internal keyword; `None` means `request_timeout`.
- `summary.json` gains `submit_invalid` in controller mode.

## 5. Remaining risks (not fixed)

1. **The broker port is reachable from model commands** (critic C4; true for `/v1/responses` already in v8).
   - In controller mode a model-issued `curl …/v1/responses` is handled as a Codex request:
     - it feeds `ledger.ingest_request` with arbitrary items;
     - it permanently drops every live splice, because `_materialize` drops any splice whose anchor is absent
       (`broker.py:553`).
   - v8 had 0 such commands.
   - A possible mitigation is to drop splices only on `resume`-classified requests and keep them dormant
     otherwise. It was not applied because it changes recorded drop semantics and C4 accepted a bookkeeping
     trust model.
2. **Enforcement policy is undecided.**
   - The implementation re-queries only when there is a finding. The brief said to re-query every unsubmitted
     final (request R3).
   - The rule itself is also open: "any submitted candidate" or S4's "last submission" (implementer request 3).
   - Both settings change the pilot's re-query rate and must be fixed in PREREG.
3. **S4's gate records an `enforce` Note on every `check_final`**, even when nothing is shown (request R1).
   Learner evidence over-counts shown notes until S4 changes this.
4. **One loop note per distinct command, per episode** (`controller_runtime.py:476`). This follows the brief. But
   after a compaction drops the note, the same loop continuing on the resumed history gets no new note, and the
   ledger splice does not mention loops. A loop note that falls due on a compaction request appears only on the
   resume request.
5. **Retry edge cases.**
   - Codex can fail to read a forwarded response and retry. The broker then counts the commands of the unread
     response (`on_response` at `broker.py:648`), which inflates a command count by 1.
   - A replayed retry gets no `on_request`. If the first attempt's `on_request` failed open, the retry gets no
     decision either.
   - A non-sticky splice would not be re-applied on a retry. `MechController` emits none.
6. **Deadline scope.**
   - Only the re-query is now bounded. A Codex-origin call still has no wire deadline, as in v8.
   - The deadline bound relies on S6 passing `actor_deadline` on the `time.monotonic` clock (request R4).
   - A deadline fallback increments `call_failures` and sets `failure_kind`. This is harmless: `campaign.py:146`
     uses `failure_kind` only when `failed` is set.
7. **Per-request cost.**
   - Each request makes one deep copy and two canonical hashes of the full Codex input, which can be about 1 MB
     late in an episode. The total cost grows quadratically with run length.
   - The loop-guard scan costs about 4 ms per request at 180 K ledger events.
   - A burst of many simultaneous loop notes costs O(notes × events). 150 notes at once took 2.4 s with a stub
     ledger that has no `next_ref`, which is not a realistic case.
8. **Not verified:**
   - Fireworks' real SSE event sequence and its handling of echoed reasoning items: paid tier.
   - The live tests use v8 Codex settings until S1's `build_settings` lands.
   - The live sandbox profile confines network and writes but not reads. It uses `(allow default)`, so it is
     not evidence of read confinement.
   - `redact` still corrupts records if a test uses a very short key (implementer's note).
