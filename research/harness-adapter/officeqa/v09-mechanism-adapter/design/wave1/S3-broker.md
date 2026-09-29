# S3: broker controller hook, `/v1/submit`, splices, loop guard, enforcement (wave 1 report)

Stream S3 of DESIGN.md draft 2, §2 L2–L4, broker side. Paths are relative to
`officeqa-pro133-v9-plumbing/`.

Requests to other owners are in `S3-broker-requests.md`.

No paid call, no network except 127.0.0.1, no answers or graders opened. Correctness labels come only from
`officeqa-divergence-analysis/index.json` (0/1), and were used for the replay statistics only.

## Terms used below

- **Splice.** Broker-owned items placed right after one Codex item (the *anchor*). They are re-sent on
  every later request (*sticky*) until the anchor disappears; at a compaction, Codex gives every item a
  fresh id. The model then keeps seeing the history it saw, and the provider's prompt cache keeps hitting.
- **SSE (Server-Sent Events).** The streaming format of Responses replies: a series of
  `data: {json}` blocks.
- **Enforcement re-query.** The final answer was never checked with `submit`. The broker asks the model
  once more with the draft plus the gate's findings, and forwards the revision's own reply.

## 1. What was built

| File | Lines | What it does |
|---|---|---|
| `broker.py` | 988 (v8: 488) | v8 relay plus controller mode (details below) |
| `controller_runtime.py` (new) | 494 | Pure helpers, the v8 replay, and `MechController` |
| `tests/fake_upstream.py` (new) | 210 | `Upstream(responder)` and `sse_full(response)` |
| `tests/codexsim.py` (new) | 208 | `CodexSim`, a model of how Codex 0.155 drives requests |
| `tests/test_v9_broker.py` (new) | 972 | broker.md §7 items 1–21, the submit route, `MechController` units, replay goldens |
| `tests/test_v9_codex_live.py` (new) | 385 | 11 tests with the real pinned Codex binary |
| `tests/test_v8_broker.py` | – | Unchanged, still passes |

### broker.py

- **Refactor.** v8's per-call path is now `_allocate()` plus `_call(number, outgoing, incoming, record)`.
  The code is the same, moved.
- **New keyword arguments:**
  - `controller` (a `contracts.Controller`, default None = v8)
  - `actor_deadline` (a monotonic time; feeds `View.remaining_s`)
  - `upstream_url` (default None = the module's Fireworks `URL`; only `https://` or `http://127.0.0.1:`
    is accepted)
  - `clock` (can be injected for tests)
- **`controller=None` is byte-identical to v8** for upstream bodies, forwarded bytes and every record,
  including the error path (test 1). The v9 fields below are written only in controller mode.
- **Controller mode, per Codex request:**
  1. Classify the request as turn, compaction or resume.
  2. Deep-copy the input for `View`.
  3. Call `on_request`, unless this is an identical Codex retry. The retry is detected by the canonical
     sha of the input, so it keeps its splices and gets no new decision.
  4. Register inserts as splices.
  5. Build the upstream input from Codex's items plus every live splice after its anchor. A splice is
     deferred past any call whose output has not arrived yet. Splices whose anchor is gone are dropped
     and recorded.
  6. `_call`, then `on_response`.
- **Anchors.**
  - `tail` = the last item of the current input.
  - `summary` = the item that begins "Another language model started…".
  - The anchor key is the item's `id`, or a content hash when it has none.
- **Enforcement loop** (on a final answer, and not a compaction):
  1. `on_final` returns `revise`.
  2. The broker sends `input + echo(draft) + [role:user '[harness:enforce] ' + feedback]` as a new
     numbered call (`origin: controller`, `parent: N`).
  3. It forwards the revision's own wire unchanged.
  4. It registers `echo(draft) + feedback` as a sticky splice after the last Codex item. The next request
     is then a strict extension of the re-query body.
  5. If the revision fails in transport, it forwards the draft wire and registers no splice.
- **Caps.** At most 3 revisions per Codex request (`MAX_REVISIONS_PER_REQUEST`, a backstop). The real cap
  is the controller's: 1 per episode.
- **Note items.** `{'type':'message','role':'user','content':[{'type':'input_text','text':'[harness:<reason>] …'}]}`,
  with no id. The API key is redacted from note and feedback text before sending.
- **`POST /v1/submit`** (`contracts.SUBMIT_PATH`):
  - Takes no response lock. Hook calls are serialised by a separate `controller_lock`.
  - Returns 404 when `controller is None`.
  - Size limits: 64 KB body, 8 KB per field. Fields must be strings from `SUBMIT_FIELDS`, and `answer` (or
    `artifact_sha256`) must be non-empty.
  - Reply: `{status, message, candidate_ref, findings:[{rule, text, offer:{answer, derivation}}]}`. No
    bindings or records are returned, and the key is redacted.
  - Errors return `{status:'error', message, findings:[]}` with HTTP 400, 413, 500 or 503.
  - `relay.submit_url` gives the URL for `HARNESS_SUBMIT_URL`.
- **Fail-open.** Every hook runs in try/except. An exception is written to `controller.jsonl`, counted in
  `controller_errors`, and the request continues as in v8. `_prepare` never raises; on an internal error
  the request is relayed exactly as Codex sent it.
- **Records** (controller mode only):
  - `transport.json` gains `origin`, `kind`, `parent`, `codex_request`, `codex_input_sha256` and
    `forwarded`.
  - `api/NNN/controller.json`:
    `{number, codex_request, origin, parent, round, kind, codex_body_sha256, codex_input_sha256,
    codex_public_input_sha256, codex_input_items, sent_input_items, sent_input_sha256, replayed_retry,
    inserted:[{index, public_index, reason, sticky, splice, anchor_id, reapplied, item_sha256, item}],
    dropped_splices, verdict{action, feedback}, forwarded, fallback, errors}`.
    - `item` holds the full item only where it is new; re-applied copies carry just the hash, which bounds
      record growth.
    - `public_index` is the item's position in `request.json`. It is None for reasoning items, because
      `public()` drops them.
  - `api/controller.jsonl` logs these events: `request`, `decision` (the controller's own details),
    `verdict`, `revision`, `revision_failed`, `rounds_exhausted`, `forward`, `submit`,
    `controller_error`.
  - `summary.json` gains these top-level fields: `codex_requests`, `controller_calls`,
    `discarded_responses`, `inserts_by_reason`, `gate_rounds` (submits answered with findings),
    `enforce_requeries`, `enforce_fallbacks`, `submits`, `submit_rejected`, `controller_errors`,
    `compactions`, `resumes`, `dropped_splices`, `retries_replayed` and `controller_usage`.

### controller_runtime.py

- **Helpers.** Each is pure, deterministic and stdlib-only; the only harness import is
  `harness_core.contracts`.
  - `classify`:
    - `compaction` = no tools, and the last item is the fixed compaction prompt.
    - `resume` = a summary item followed only by user or developer messages.
    - Otherwise `turn`.
  - `normalize_output`: drops only the header lines `Chunk ID:` and `Wall time:`.
  - `command_of`: exec_command → its `cmd` with whitespace collapsed, which is the same as S4's
    `command_text`.
  - `echo`: re-serialises items the way Codex does; verified byte for byte against the binary.
  - Also: `note_item`, `is_final_answer`, `final_text`, `numeric_tokens`, `same_answer`, `anchor_key` and
    `canonical_sha`.
  - `replay_run` and `replay_root` rebuild kinds and the command log from the v8 `api/NNN/request.json`
    and `response.json` files (read-only).
- **`MechController(ledger, gate, policy, clock)`** implements `contracts.Controller`. The policy is merged
  over `POLICY_DEFAULTS`.
  - **`on_request`:**
    1. `ledger.ingest_request`.
    2. On `resume`, one frozen `Insert('ledger', ledger.render(ledger_splice_bytes), anchor='summary')` per
       summary item.
    3. Then the loop guard: any non-poll command whose `ledger.command_count` is at least `loop_threshold`
       gets one note per distinct command. The note uses fixed wording:
       - the command
       - "output identical each time" or "output changed between runs", from `Command.output_sha256`
       - the current candidates (the last 3), with the ids of compute events whose values match
       - `Options: submit --answer "<A>" … or run a command you have not run yet`
    4. After `loop_notes_max` notes, the wording escalates to `Submit now: submit --answer "<A>"`, but only
       if a candidate exists that is newer than the last `observe` event.
    - Nothing is inserted into compaction requests.
  - **`on_response`:** `ledger.ingest_response`, and it records call_id → tool name so `write_stdin` polls
    are never counted as loops.
  - **`on_final` (enforcement).** It skips, in this order:
    - a compaction or a non-final response
    - `enforced ≥ enforce_requery_max`
    - `remaining_s < enforce_min_remaining_s`
    - an answer that was already submitted. This uses `gate.needs_enforcement` when the gate has it (S4 does:
      the DESIGN rule, compared with the last submission, pack-aware). Otherwise it uses "no submitted
      candidate with equal numeric tokens" (the task wording).

    Otherwise it calls `gate.check_final` and returns `Verdict('revise', ENFORCE_TEMPLATE(message))`, but
    only when the check reports findings. `requery_when_clean=True` also re-queries when the check is clean.
  - **`on_submit`:** calls `gate.submit`.
  - Every insert is also added to the ledger as a `Note` event.

## 2. Verification (exact commands and results)

Interpreter: `adapter-comparison-work/.venv/bin/python` (Python 3.12.14, pytest 9.1.1). Run from
`officeqa-pro133-v9-plumbing/`:

| Command | Result |
|---|---|
| `…/python -m pytest tests/test_v8_broker.py tests/test_v9_broker.py tests/test_v9_codex_live.py -q -p no:cacheprovider` | **53 passed, 13 skipped** (the opt-in groups) in 38 s |
| `…/python -m pytest tests/test_v9_broker.py -q -p no:cacheprovider -m slow` | **2 passed** in 55 s (replay goldens 37 s, heartbeat 16 s) |
| `…/python -m pytest tests/test_v9_codex_live.py -q -p no:cacheprovider -m live_codex` | **11 passed** in 9 s |
| `…/python -m pytest tests/test_v8_broker.py -q -p no:cacheprovider` (as in `preflight.py`) | 14 passed |

Coverage of broker.md §7:

| Items | Test |
|---|---|
| 1 | Byte-identity with v8. The read-only v8 `broker.py` is loaded via importlib and compared on the same bodies in the full-unbounded and bounded profiles, including a defect re-send and an HTTP 400 |
| 2 | View and classify |
| 3 | k=3 fires once; the A,B,A,B,A pattern fires; `write_stdin` polls with changing output do not; 2 repeats do not |
| 4 | Placement after parallel outputs; never between a call and its output (also a unit test of `_materialize`) |
| 5 | Request N+1 extends request N |
| 6 | Compaction includes the old notes; resume drops them and records the drop; the ledger splice sits at index 4 after the summary; `render` is called once |
| 7 | An HTTP 400 on the request carrying a new note; the retry sends identical `input` bytes and one note; `retries_replayed=1`; the ledger is not re-fed |
| 8 | Gate pass |
| 9 | Revise, with exact echo (no `status` or `annotations`; reasoning has `content: null`) |
| 10 | Per-episode cap, plus the broker backstop (4 calls, `rounds_exhausted`) |
| 11 | A tool-call revision is executed and spliced |
| 12 | A defect inside the revision is re-sent with identical bytes |
| 13 | Revision transport failure → the draft is forwarded, the fallback is recorded, no splice |
| 14 | Deadline skip with an injected clock; also a skip when the final equals a submission |
| 15 | Incomplete revision (Codex keeps the partial item) |
| 16 | Accounting |
| 17 | Reconstruction via `index` and `public_index` against both hashes |
| 18 | Heartbeats (slow) |
| 19 | Stop during the gate |
| 20 | Fail-open |
| 21 | Redaction: the key appears in no `api/` file and no upstream body |
| submit route | Findings reply without internals; 7 bad-payload cases; 404 with no controller; submit answers in < 2 s while a `/v1/responses` call is stalled |

### Offline replay goldens (read-only over all three v8 roots; `test_22_replay_goldens_over_v8_api_records`)

| arm | compaction requests (`classify`) | resumes | loop runs, any 3rd identical cmd (critic C2) | right among them | streak ≥3 runs | median first loop call | compacted runs with a loop |
|---|---|---|---|---|---|---|---|
| none | **35** | 35 | **22** | 4 | **14** | 25.5 | 21/21 |
| prompt | **38** | 37 (Q71 ended in flight) | **25** | 3 | **9** | 24 | 21/22 |
| cards | **46** | 46 | **29** | 5 | **13** | 29 | 29/29 |

- All goldens reproduce, and a second run gives identical results.
- A fast test also checks, on every request of none/010 and none/072, that `classify == 'compaction'`
  exactly when Codex's own `client_metadata.x-codex-turn-metadata.request_kind` says `compaction`.

### Scratch integration with S4's real `Ledger`, `Gate` and `OfficeQAPack` (informational, not a test)

This was done in the scratchpad, as the brief asks: tests must not import S4's code.

- One CodexSim episode with 3 identical commands, a compaction, a resume, a submit and a final went through
  the broker with `MechController` over S4's classes.
- Result: 0 controller errors, a loop note and the ledger splice inserted, the submit accepted via the real
  gate, and `Note`, `Candidate` and `Compaction` events in the real ledger.

## 3. Live Codex findings ($0; pinned `codex-0.155.0-alpha.9.2`; sandbox-exec allowing only the broker port)

Setup: v8 argv (`command_for`) and v8 FULL settings, `--ephemeral --json`. `(deny network*)` plus outbound
TCP to `localhost:<broker port>`, and no file writes outside the scratch dir. About 0.1–1.5 s per scripted
episode.

1. **`sse_full` streams are accepted.** Codex builds its history from `output_item.done` and ends on
   `response.completed`. `final.md` is the last agent message.
2. **Codex's re-serialisation**, now `controller_runtime.echo`, is verified byte for byte including key
   order:

   | Item | As Codex re-sends it |
   |---|---|
   | reasoning | `{type,id,summary,content,encrypted_content}`; missing fields become **`null`** (not dropped) |
   | message | `{type,id,role,content:[{type,text}]}`; `status`, `annotations` and `logprobs` dropped |
   | function_call | `{type,id,name,arguments,call_id}`; `status` and unknown keys dropped |

3. **Parallel calls run concurrently**, and the completion order varies. The outputs, however, are appended
   **in call order**, each with a fresh `fco_` id and the header
   `Chunk ID: <6 hex>\nWall time: <t> seconds\nProcess exited with code N\nOriginal token count: N\nOutput:\n`.
4. **Gate revise works.** `final.md` equals the revision, and `events.jsonl` holds only the revised agent
   message. Codex never sees the draft.
5. **A tool-call revision is executed.** Codex's next request plus the broker splice is a strict extension of
   the re-query body.
6. **A loop-note splice is accepted.** All consecutive upstream bodies are prefix-consistent, and
   `instructions` and `tools` do not change.
7. **After an injected upstream error, Codex re-sends the same history.** It prints
   `Reconnecting... 1/100 (stream disconnected before completion: stream closed before response.completed)`.
   - The parsed bodies are equal, and the `input` bytes are identical.
   - The raw body differs only in the **key order of `client_metadata`**, which is a Rust HashMap. This is
     why idempotence is keyed on the canonical input sha.
8. **New: after `response.incomplete` with visible output, Codex keeps the partial items.** It shows them as
   an `agent_message` and retries the turn **with them appended**, so this is not an identical re-send.
   - This contradicts broker.md §3c's assumption.
   - It is handled: the enforce splice sits before the partial item (test 15, plus a live test).
   - It is also true of v8: an `incomplete` response with visible output leaves partial text in the
     history.
9. **`include_permissions_instructions=false` removes only the permissions block.** Request 001 loses only
   the second `input_text` of the developer message: the `<permissions instructions>` block with the false
   "No filesystem sandboxing … Network access is enabled".
   - `instructions`, `tools` (exec_command, write_stdin, request_user_input, view_image, multi_agent_v1),
     AGENTS.md, `environment_context` and the task are unchanged.
   - Also present in every arm (and in v8): a `<skills_instructions>` block listing 5 system skills.
10. **Truncation: 10,000 characters of tool output are kept (head + tail).** For 102,000 chars printed:
    - the header says `Warning: truncated output (original token count: 25500)`, then
      `Total output lines: …`, and the body contains `…92000 chars truncated…`;
    - the whole `function_call_output` is about 10.2 K chars;
    - `max_output_tokens: 100000` in the call arguments does not raise the limit.
11. **Forcing a compaction works in two ways.**
    - (a) Reported usage of 300,000 input tokens under the FULL profile. The configured limit is 800,000 but
      the effective one is about 244.8 K, so Codex compacts.
    - (b) `model_auto_compact_token_limit=2000`: a reported 5,000 compacts and 1,500 does not. A limit lower
      than the fallback-derived one is honoured.
    - The compaction request has `tools=[]`, the whole history and the fixed prompt (no id), and metadata
      `request_kind:"compaction"`.
    - The resume request has 4 items with fresh ids and `window_number` + 1.
    - `events.jsonl` gets a `Heads up: Long threads…` error item.
    - The `[harness:ledger]` splice lands at index 4, right after the summary, and is byte-identical on
      later requests.
12. **Sandbox check.** A model command `curl http://127.0.0.1:<another local port>` fails with rc=7, while
    Codex works normally through the broker port.
    - The broker port itself is reachable from model commands. `/v1/submit` depends on this. It is
      bookkeeping, not a security boundary (critic C4).

## 4. Interface deviations from contracts.py

- `MechController` has two optional adapter hooks, `bind_call(n)` and `drain_log()`, which the broker calls
  if they exist. It also takes `t0` and `requery_when_clean` keywords. Requests 1, 2, 4 and 5 propose
  contract fixes.
- **Enforcement rule.** `MechController` delegates to `gate.needs_enforcement` when present (the DESIGN
  rule, compared with the last submission). Otherwise it uses the task wording (any submitted candidate).
  Request 3 asks for one rule.
- `on_final` is also asked on `resume` requests: any request that offered tools and got a final answer.
  `round` is the number of revisions already made within this Codex request; the episode cap lives in the
  controller.
- Enforcement splices come from `Verdict` feedback, not from an `Insert`. An `Insert` must have reason
  `loop_guard`, `ledger` or `enforce` and non-empty text; otherwise it is rejected and counted as a
  controller error.
- `broker.SUBMIT_PATH` duplicates `contracts.SUBMIT_PATH`, and a test asserts they are equal. The broker
  imports `harness_core` lazily, and only in controller mode.
- The v9 transport and summary fields exist only in controller mode. This is required for byte-identity
  with v8, so `cost.py` must default a missing `origin` to `codex` (request 8).

## 5. Known gaps

- **Not implemented (intentionally):** broker.md §7 item 14's wrap-up (`tools=[]` near the deadline). DESIGN
  draft 2 drops wrap-up.
- **Not implemented:** a per-call wire deadline tied to `actor_deadline`. `actor_deadline` only feeds
  `remaining_s`.
- **Heartbeat test is slow.** The interval is fixed at 15 s (v8 code), so the test takes 16 s and is opt-in.
- **Not verified live against Fireworks:** its real SSE event sequence and its reasoning-item fields
  (echoed reasoning, a mid-history user note). The fake follows the OpenAI Responses shape, which Codex
  accepts. This is paid tier 2 (engineering #4 item 13).
- **Placement near compaction.** Loop notes that fall due on a compaction request are not inserted there.
  They appear on the resume request, after the ledger splice.
- **Submit can delay hooks.** `controller_lock` serialises hooks, so a slow `gate.submit` (sandboxed
  recompute, ≤ 5 s) delays the next request's hooks by that much.
- **Short keys corrupt records.** `redact()` (from v8) replaces every occurrence of the key string, so a
  very short dummy key corrupts records. Real keys are long, and the tests use a long dummy.
- **Model-visible text budget.** The model-visible templates (`LOOP_TEMPLATE`, `LOOP_ESCALATE`,
  `ENFORCE_TEMPLATE`, about 0.5 KB in total) must be counted in TEXT_BUDGET by S5/S6.
- **Live tests use v8 settings.** They use the v8 FULL settings, not yet S1's v9 kit settings (request 16).
- **`CodexSim` is a model, not the real client.** The live module is what checks its assumptions: echo,
  output headers, output order, compaction and resume shape, retry, and incomplete handling.
