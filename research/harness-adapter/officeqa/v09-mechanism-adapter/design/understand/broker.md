# v9 understanding: the Fireworks Responses relay (broker)

Scope: `officeqa-pro133-v8-plumbing/{broker.py, rate_control.py, client.py, tests/test_v8_broker.py}`
plus the Codex-side settings in `acting.py`/`campaign.py`, checked against the recorded
traffic of the three finished v8 roots (`officeqa-pro133-{none,prompt,cards}-v8-work`,
399 runs, 22,086 upstream calls). Every data claim below was computed from
`runs/*/NNN/api/NNN/{request,response,transport}.json`, `summary.json`, `records/events.jsonl`
and the correctness flags in `officeqa-divergence-analysis/index.json` (no answers, graders or
private files were opened). Paths are relative to the project folder; `broker.py:N` means
`officeqa-pro133-v8-plumbing/broker.py`, line N.

## 0. Verdict in one screen

| Question | Answer | Evidence |
|---|---|---|
| Does the broker hold the whole response before Codex sees any of it? | **Yes.** The full upstream SSE stream is buffered in RAM and written to Codex in one go after the terminal event. This is what makes both defect re-sends and answer replacement possible. | `read_wire` broker.py:152-189; forward broker.py:406-414 |
| (a) Can it see the full conversation on every request? | **Yes, back to the last compaction.** `previous_response_id` is rejected and `store=false` is forced, so every request carries its own history as `input` items. History is strictly append-only between compactions: 0 prefix breaks across all consecutive tool-offering request pairs in 399 runs. | broker.py:77-78, 90; §3a |
| (b) Can it add a message without breaking Codex? | **Yes.** Codex never sees the outgoing body. Fireworks already accepts user messages placed after `function_call_output`, because Codex itself does this (`<environment_context>`, none/054 call 136, prompt/035 call 236). The note has to be appended (never inserted earlier) and re-applied on every later request so the prompt cache and history stay consistent. | §3b |
| (c) Can it gate a final answer, re-query, and return the revision? | **Yes, if it forwards the revision's own provider wire unchanged.** It must not stitch or synthesize events. The draft plus the feedback must be spliced back into later requests if the revision continues the turn. | §3c |
| (d) Can it detect compaction and add a ledger? | **Yes, deterministically.** In v8, every request with `tools == []` was a compaction request (35/38/46 of 35/38/46), and its last item is the fixed compaction prompt. The next request carries Codex's fixed "Another language model started…" summary message. Insert the ledger after that summary and keep it pinned there. | §3d |
| Biggest broker-level waste in v8 | **Deterministic tool loops at temperature 0.** 36 runs repeated an identical call with identical output ≥3 times in a row. These loop requests make up 22–37% of all input tokens and 16–28% of provider time, and only 7/36 of those runs were correct. | §3a table |

## 1. Terms used below

- **Responses API.** OpenAI's `/v1/responses` HTTP interface, which Fireworks copies. A request
  holds `instructions` (the system prompt), `tools`, and `input`, a list of *items*. A response
  holds `output` items and `usage`. Codex speaks this protocol because `wire_api = 'responses'`
  (acting.py:276). This matters because each item is a separate JSON object, so a relay can read,
  append or remove items without parsing any chat template.
- **Item.** One JSON object in `input`/`output`. The types seen here are `message` (role
  `developer`/`user`/`assistant`), `function_call` (the model asks Codex to run a tool; it has
  `name`, `arguments` as a JSON string, `call_id`, and `id`), `function_call_output` (Codex's tool
  result, linked by `call_id`), and `reasoning` (the model's hidden thinking). Example: v8 run
  none/072 request 247 has 4 items: developer, AGENTS.md, question, and the compaction summary.
- **SSE (Server-Sent Events).** A streaming HTTP format. The body is a series of
  `data: {json}\n\n` blocks with `type` values such as `response.output_item.done` and
  `response.completed`. It is used so clients can show tokens as they arrive. In this setup the
  broker defeats the streaming on purpose: it collects the whole stream first (see §2).
- **`call_id` / item `id`.** `call_id` pairs a `function_call` with its `function_call_output`.
  The `id`s label items. Provider ids look like `fc_…`/`msg_…` and Codex ids look like
  `fco_…`/`msg_01a0…`. Codex re-sends them on later requests, so they can serve as stable
  anchors.
- **`previous_response_id` / `store`.** A server-side memory: the provider keeps the history and
  the client only sends the new items. v8 forbids it (broker.py:77-78) and forces `store=false`
  (broker.py:90), so the relay always sees the full history.
- **Compaction.** When the context grows too large, Codex asks the model for a hand-off summary.
  It then restarts the history as [initial context + summary] with fresh item ids.
- **Prompt cache (prefix cache).** The provider reuses computation for the longest request prefix
  it has seen before. In v8, 95.4%/95.1%/95.5% of input tokens (none/prompt/cards) were cached
  (`api/summary.json` sums). Any edit to an earlier item breaks the cache from that point onward,
  so the controller may only append.
- **Splice (term for this design).** A broker-owned item inserted after a specific Codex item. It
  is re-applied on every later request until that Codex item disappears, which happens at
  compaction.

## 2. The request path, Codex → Fireworks → Codex

| # | Step | Code |
|---|---|---|
| 1 | Codex is configured with provider `officeqa_fireworks`: `base_url=http://127.0.0.1:PORT/v1`, `wire_api='responses'`, `supports_websockets=False`, `request_max_retries=100`, `stream_max_retries=100`, `stream_idle_timeout_ms=3600000`. v8 ran the `full_*` profile (`model_context_window=1048576`, `model_auto_compact_token_limit=800000`). | acting.py:270-285, 293-295 |
| 2 | The OS sandbox lets the Codex process tree reach only the broker's TCP port. **Note:** this means the model's own shell commands can also reach the broker port. | acting.py:247 |
| 3 | Codex POSTs `/v1/responses` with `stream: true`. Observed body keys: `model, instructions, input, tools, tool_choice:'auto', parallel_tool_calls:true, reasoning:{effort:'high',summary:'auto'}, store:false, stream:true, include:['reasoning.encrypted_content'], prompt_cache_key:<session id>, client_metadata`. | none/072/api/001/request.json |
| 4 | The handler accepts only `/v1/responses` (plus `/v1/local-mwm`, used only by the MWM sandbox) and takes the per-actor lock. In the FULL profile the lock blocks, so requests are serialized; otherwise a concurrent request gets 409. | broker.py:227-248 |
| 5 | `normalized()` enforces the exact model; allows only local function/custom tools; rejects `previous_response_id`, remote images and `input_file`; sets `model`, `store=False`, `temperature=0`; in unbounded mode drops `max_output_tokens/max_tokens/max_completion_tokens/thinking`, otherwise sets `max_output_tokens`. The `reasoning` field is forwarded unchanged (v8 fix 2). | broker.py:62-99 |
| 6 | FULL only (`request_timeout is None` and `stream`): the broker immediately sends `200 text/event-stream` headers, and a heartbeat thread writes the SSE comment `: controller waiting for Fireworks` every 15 s so that Codex's idle timer never fires. | broker.py:263-275 |
| 7 | Allocate call number N; save `api/NNN/request.json` = `public(outgoing)`, which drops reasoning items, `reasoning_content`, `encrypted_content`, `api_key` and `Authorization`; build `record`; compute `request_sha256` over the exact outgoing bytes. | broker.py:276-291, 41-47 |
| 8 | `attempt()`: urllib POST to `URL` with `Authorization` and fixed `HEADERS` (a plain User-Agent, because Cloudflare blocked urllib's), no proxy, redirects forbidden. | broker.py:293-302, 219, 147-149; rate_control.py:22 |
| 9 | **SSE buffering** (`read_wire`): every byte is appended to `wire`; `data:` lines are parsed only to set `generated` (any output/reasoning/delta event) and to capture the terminal `response.completed/incomplete/failed` object as `final`; `error` events are collected. With no terminal event it raises `NoTerminalEvent`. Every exception carries `.generated`. Non-SSE bodies are read whole. | broker.py:152-189 |
| 10 | Post-checks: model mismatch → fail; explicit rate refusal before any generation → retryable `rate`; non-200 or status other than completed/incomplete → non-retryable fail; budgeted mode also checks output-token validity and cap. | broker.py:307-333; rate_control.py:80-101 |
| 11 | **Defect re-send**: `provider_defect(final, tools=bool(outgoing.tools))` flags `invalid_tool_arguments`, `incomplete_without_output`, `completed_without_usage` and `completed_without_output`. Each is raised as a retryable `transport` failure, so the **identical bytes** are re-sent and Codex never sees the defect. v8 counts: none 8+1, prompt 25, cards 16+3+3. | broker.py:116-144, 334-342 |
| 12 | HTTP errors: 429 or explicit rate text → retryable `rate` (shared cooldown); 5xx or non-JSON 403 → retryable `transport` (local backoff). Connection drops are re-sent only if nothing was generated. | broker.py:344-370; rate_control.py:25-27 |
| 13 | `rate_control.run`: a global scheduler admits each attempt (FULL campaigns use `max_in_flight=inf, min_interval=0`, campaign.py:333-335). Records are logged per phase through `on_attempt`; backoff is 15/30/60/120 s; attempts are unlimited in FULL (`max_attempts=None`). | rate_control.py:215-293, 19-20; broker.py:374-380 |
| 14 | Accounting: known usage from **every** attempt (including discarded defects) is summed into `broker.usage` and `record['usage']`; unknown usage is marked as a lower bound. | broker.py:386-400, 30-38 |
| 15 | Forward: save `api/NNN/response.json`, stop the heartbeat, send headers if not yet sent, then `self.wfile.write(wire)`, which is the upstream bytes unchanged. | broker.py:401-414 |
| 16 | On failure after headers: write `event: error` SSE; otherwise HTTP 400. Codex then re-sends the **identical input** (verified: prompt/070 call 9→10, cards/074 10→11, none/121 112→113) using its own retry budget. | broker.py:415-435 |
| 17 | `finally`: write `transport.json` (or `rejected.json` if no number was allocated), update the unknown-usage set, release the lock; `summary.json` is written at exit/stop. | broker.py:436-446, 454-488 |

`client.py` is not on this path. It is a chat-completions client used only by the cards learner
(cards_learning.py:302), with the same identical-bytes retry rules through `rate_control.run`
(client.py:38-122).

Consequences worth knowing:
- Codex receives no real streaming, only heartbeats followed by one burst, so any response can be
  inspected, discarded or replaced before Codex sees it.
- In FULL there is no per-call wall clock (`request_timeout=None` means no wire deadline; see
  experiment.py:23-24 and rate_control.py:163-191). v8 had 4 `NoTerminalEvent` calls lasting
  603–1460 s (prompt/070/009, cards/074/010, 088/019, 101/013), and each `incomplete_without_output`
  re-send took about 400–600 s. prompt/070 call 9 used 1459 s of the 3600 s actor budget.

## 3. What the broker can do

### (a) Read the full history to detect repeated identical tool calls — yes

- Each request's `input` is the whole conversation since the last compaction, because no server
  state is allowed (broker.py:77-78, 90).
- Codex history is **append-only** between compactions: comparing each tool-offering request
  with the previous one gave 0 prefix mismatches in all 399 runs. This is the public view, where
  reasoning items are stripped (broker.py:43).
- Codex re-sends provider output items **verbatim except for two fields**: it drops `status`, and
  it drops `annotations` from message content parts. Checked on 2,415 output items in 40 none-arm
  runs: all were found by `id` in the next request, and all were equal after those two removals.
- All items in tool-offering requests carry an `id`, and ids never repeat within a request
  (cards arm sample). Only the compaction prompt has no `id`.
- **Tool outputs must be normalized before comparing.** Each output starts with
  `Chunk ID: <random>\nWall time: <t>\n`. Without stripping those two lines, not a single repeat
  is detected. Outputs are already Codex-truncated (~10 KB head+tail, with an
  `Original token count:` line).
- Offline replay of the rule "the last ≥3 `function_call`s have identical `(name, arguments,
  normalized output)`" over v8:

| arm | runs hit | correct among them | requests in loop | share of input tokens | share of broker seconds |
|---|---|---|---|---|---|
| none | 14 | 3 | 1,640 / 7,041 | 36.7% | 28.1% (3.6 h) |
| prompt | 9 | 1 | 1,104 / 6,354 | 24.2% | 16.0% (2.3 h) |
| cards | 13 | 3 | 1,335 / 8,691 | 22.3% | 17.5% (2.9 h) |

  Loops start early (first firing request: median around call 25) and are **deterministic**. In
  none/072, calls 300–357 returned one identical response body 58 times; the only differences
  were `id`/`call_id`. This follows from `temperature=0` (broker.py:90) together with a prefix
  that grows by the same (call, output) pair each time. Any change to the prefix, such as an
  appended note, breaks this fixed point.
- Limitation: history before a compaction is gone from the request, so cross-compaction repeat
  detection needs a broker-side command log (§6).

### (b) Inject an extra message without breaking Codex — yes, under four rules

1. **Codex never sees the outgoing body.** It sees only the response bytes (broker.py:413), so
   adding items to `outgoing['input']` after `normalized()` (broker.py:262) is invisible to Codex.
2. **Use role `user`, not `developer`, for mid-conversation notes.** Evidence that Fireworks and
   GLM accept a user message right after a `function_call_output`: Codex's own
   `<environment_context>` message (on the date change) was the last input item at none/054
   call 136 (index 38 of 39) and at prompt/035 call 236, and both completed normally. There is no
   v8 evidence for a mid-history `developer` message; with GLM it probably becomes a non-leading
   system message, which is untested. An `id` is optional: the compaction prompt has none and was
   accepted.
3. **Append only, at the tail, after the last `function_call_output`.** Never insert between a
   `function_call` and its output. With `parallel_tool_calls: true`, several calls come first and
   their outputs follow.
4. **Make the note sticky.** Codex will not include it in the next request, so the broker must
   re-insert it at the same place every time. Otherwise the model sees a reply to a note that has
   vanished, and the next request diverges from the cached prefix at the note. A sticky note makes
   request N+1 a strict extension of request N as sent, so the cache keeps hitting.

Where it goes in code: between broker.py:262 (`normalized`) and broker.py:280 (`save
request.json`), so that `request.json` and `request_sha256` describe what the model actually saw.

### (c) Gate a final answer, re-query with checker feedback, return the revision — yes

**Detecting a final answer.** Terminal `status == 'completed'`, the request offered tools (so it
is not a compaction), `output` has no `function_call`/`custom_tool_call`, and at least one message
has non-empty text. These are the conditions under which Codex ends the turn: `codex exec` is a
single turn, and its last agent message becomes `final.md` (acting.py:325), which becomes the
prediction (campaign.py:133-135). `_visible` (broker.py:102-108) already does the message test.

**Re-query body.** `outgoing2 = outgoing` with
`input = outgoing.input + echo(draft.output) + [feedback_user_msg]`. Here `echo()` reproduces
Codex's re-serialization (drop `status`; drop `annotations` in message content, as verified in
§3a) and keeps reasoning items. Everything else stays identical (`tools`, `instructions`,
`prompt_cache_key`, `reasoning`, `store=false`, `temperature=0`), so the prefix, and therefore the
cache, is preserved. Send it through the same attempt/`rate_control.run` machinery as its own
numbered call, so defect re-sends and accounting apply unchanged.

**What Codex receives.** Forward the **revision's own buffered wire bytes, unchanged**. This is
already a complete, coherent stream with its own `resp_…` id, item ids, `call_id`s and usage.
Nothing needs stitching:
- Codex uses no response chaining (`previous_response_id` is forbidden), so a new response id is
  harmless.
- Codex builds history from `response.output_item.done` items and finishes on
  `response.completed`. The Codex 0.155.0-alpha.9.2 binary contains the strings
  `failed to parse ResponseItem from output_item.added`, `failed to parse
  response.output_item.done`, `stream closed before response.completed` (7×) and `Incomplete
  response returned, reason:` (from `codex-api/src/sse/responses.rs`). So a synthetic stream
  without per-item `output_item.done` events, or without a terminal `response.completed` carrying
  `id` and `usage`, would fail or lose items. Do not synthesize. The existing fake upstream emits
  only `response.created`, `reasoning_text.delta` and the terminal event (tests/test_v8_broker.py:34-39).
  That is fine for broker unit tests but is **not** a stream the real Codex would build history
  from.
- Never merge items from the draft and the revision into one forwarded stream. It would create
  duplicate or mismatched ids and `call_id`s.

**If the revision contains tool calls,** Codex runs them and its next request is
`I + revision items + outputs`, which lacks the draft and the feedback. The broker must splice
`echo(draft) + feedback` back in after the last item of `I`, anchored on that item's `id`. The
model then sees exactly what it saw during the re-query, and the upstream body stays a strict
prefix extension of the re-query body.

**Usage accounting.**
- Codex's own `turn.completed` usage (records/events.jsonl) sums only the forwarded responses.
- The broker's `summary.json` sums every upstream attempt, and the campaign uses the broker's
  numbers (campaign.py:150 → `transport.get('usage')`). There is precedent: v8's defect
  re-sends are already invisible to Codex but counted by the broker.
- The forwarded revision reports `input_tokens` that include the draft and the feedback. Codex
  bases auto-compaction on reported usage, so the spliced tokens count toward its compaction
  trigger. That is correct, because they really are in the upstream context.
- Never alter or zero a forwarded `usage`. A zero-usage `completed` is exactly the defect
  (`completed_without_usage`) that v8 fixed.

**Failure handling.**
- If the revision fails at the transport level, forward the draft wire (it is still in RAM) and
  record the fallback.
- If time is short, skip the gate.
- If the revision is `incomplete`, Codex treats it as an error and re-sends the identical request.
  The gate must then be idempotent: re-apply the same splice, and do not start fresh rounds beyond
  the per-task cap.

### (d) Detect a compaction request and add the ledger — yes

- **Compaction request.** `tools == []`, and the last input item is a user message (no `id`)
  starting with `You are performing a CONTEXT CHECKPOINT COMPACTION. Create a handoff summary…`
  (none/072/api/246 and 358). In v8, *all* no-tool requests were compactions (35/35, 38/38,
  46/46). They are sent with the unchanged `instructions`, and 106/118 of them got
  `cached_tokens == 0`, because removing the tools changes the prompt prefix.
- **Resume request.** Codex rebuilds `[developer, AGENTS.md user, question user, summary user]`
  with **fresh ids** (none/072/api/247). The summary text starts with `Another language model
  started to solve this problem and produced a summary of its thinking process…`. The history
  then grows append-only again.
- **Compaction timing.** Compactions fired at 241,474–490,958 input tokens (prompt arm max
  246,813). That is far below the configured `model_auto_compact_token_limit=800000`, and every
  run logs `Model metadata for 'accounts/fireworks/models/glm-5p3-flash' not found. Defaulting to
  fallback metadata` (events.jsonl item_1). So the effective trigger is Codex's own, at roughly
  240–245K tokens.
- **Recommended injection: a deterministic ledger snapshot after the summary.** On the first
  resume request, insert a user item `[harness:ledger] …` right after the summary item, anchored
  on the summary's `id`, and keep it sticky until the next compaction. Freeze the snapshot text at
  insertion time; re-rendering it each request would change the prefix and lose the cache. Later
  ledger updates reach the model through the ledger CLI's own tool outputs.
- **Optional addition:** append "include the ledger verbatim" to the compaction prompt. This is
  lossy because it depends on the model, and it is already cache-cold.
- **Rejected alternative:** rewriting the summary text inside the compaction response. It would
  mean editing provider SSE bytes (deltas, `output_item.done` and `completed.output`) and breaking
  the forward-unchanged property.
- **Ledger source.** Either read the workspace ledger file, which is actor-writable, untrusted and
  size-capped, or add a POST endpoint on the broker following the `/v1/local-mwm` precedent
  (broker.py:228-242), so that the `ledger` CLI writes into controller-held records. The actor can
  already reach the broker port (acting.py:247).

## 4. Risks

1. **Codex id bookkeeping.**
   - Forward exactly one complete provider wire per Codex HTTP request.
   - Never forward a response whose `call_id`s Codex has already seen.
   - Never put an item between a `function_call` and its output.
   - Anchor splices on item `id`s, which are stable and unique in v8, not on positions.
   - After compaction the anchors vanish (fresh ids), so the splices drop themselves. Record each
     drop.
2. **`previous_response_id` and websockets.** Splicing is valid only because every request is
   stateless and full-history. Keep broker.py:77-78 and 90, and keep `supports_websockets=False`
   (acting.py:277), because incremental websocket turns could introduce server-side state.
3. **Prompt caching (95% of input tokens).**
   - Only append at the tail and keep splices sticky.
   - Freeze ledger snapshots.
   - Never edit `instructions`/`tools` per request. `tools=[]` costs a full miss: compaction calls
     had cached_tokens 0 in 106/118 cases.
   - A non-sticky note loses only the tokens after the divergence point, but it leaves the model
     with an inconsistent history, so avoid it.
4. **Model view vs Codex view.**
   - `records/events.jsonl`, `final.md` and Codex usage will not contain injected notes, discarded
     drafts or checker feedback.
   - The analysis tools read only events.jsonl (officeqa-divergence-analysis/build_digests.py:41,
     behavior_stats.py:21), and the learners read trajectories. They must merge the broker's
     controller log, or they will misattribute behavior.
   - `request.json` is also not byte-exact today: `public()` strips reasoning items, so
     `request_sha256` cannot be re-verified from it.
5. **Retries.** After any broker error, Codex re-sends the identical input (request/stream retries
   of 100). Hooks must therefore be idempotent per Codex input: do not add a second loop note, and
   do not re-count gate rounds.
6. **Time.**
   - The actor is killed at 3600 s (acting.py:493), and a timeout yields an empty prediction
     (campaign.py:133-134).
   - Gate rounds, runaway re-sends (about 400–600 s each) and stalled streams (up to 1460 s) all
     consume that budget.
   - The controller needs the actor deadline, which the broker does not know today because it is
     created just before `run_actor` (campaign.py:126-128).
   - Wire deadlines should become deadline-aware, and the gate should be skipped near the end.
7. **Security.**
   - The broker holds the API key in RAM, runs outside the sandbox, and is reachable from the
     actor's shell.
   - Checkers must be pure functions over data. Never execute model-authored code (`calc`
     scripts) in the broker process; re-run it inside the actor sandbox if needed.
   - Treat ledger and workspace content as untrusted.
   - Apply `redact(…, key)` to all new records.
8. **Fairness and declared protocol.**
   - The broker docstring promises "an HTTP transport, not an agent loop" (broker.py:1-5), and
     PROTOCOL.md describes only defect re-sends.
   - `controller=None` must stay byte-identical to v8 for the arms without the controller, and the
     L0 environment fixes must stay outside the broker.
   - Any `temperature` change used to break loops is a sampling change and must be declared.
9. **Minor existing issues.**
   - `rejected.json` is overwritten on every pre-number rejection (broker.py:440).
   - Every `response.json` is saved twice (the attempt copy and the final copy), each about 32 KB,
     mostly echoed `instructions`/`tools`. That comes to 188 MB for none/072 and 2.8 GB for the
     none root.
   - `request.json` repeats the whole history on every call, so storage grows quadratically with
     run length.

## 5. What the broker records per call, and where

| File | Content | Code |
|---|---|---|
| `runs/<arm>/NNN/api/NNN/request.json` | `public(outgoing)`: the normalized upstream body minus reasoning items, `reasoning_content`, `encrypted_content`, key fields | broker.py:280 |
| `api/NNN/transport.json` | `number, upstream, model, native_request_settings` (Codex's `model/reasoning/temperature/max_output_tokens/stream`), `changes` (temperature 0, store false, cap, reasoning forwarded, defects re-sent), `request_sha256`, `attempts[]`, `usage_complete`, summed `usage`, `http_status`, `response_model/id/status`, `error`, `seconds` | broker.py:281-291, 386-404, 423-427, 438-439 |
| `api/NNN/attempts/KK/transport.json` | rewritten at each phase (waiting → in_flight → finished): scheduler receipt, `http_status`, `response_id/status`, `usage`, `usage_known`, `observed_generation`, `stream_errors`, `status` (`succeeded/transport_retry/rate_refused/failed/not_dispatched`), `provider_defect`, `failure_kind`, `backoff_seconds`, `wire_seconds` | broker.py:374-375; rate_control.py:230-281 |
| `api/NNN/attempts/KK/response.json` | `public(final)` of that attempt, **including discarded defects** | broker.py:312 |
| `api/NNN/attempts/KK/error.json` | HTTP error status, usage, redacted provider error (≤3000 chars) | broker.py:352-355 |
| `api/NNN/response.json` | `public(final)` of the forwarded response | broker.py:402 |
| `api/rejected.json` | the last request rejected before numbering (overwritten) | broker.py:440 |
| `api/summary.json` | calls, failures, summed usage incl. `cached_tokens`, in-flight/unknown-usage calls, wire attempts, rate refusals, scheduler wait, limits | broker.py:470-488 |

Not recorded anywhere: the raw SSE wire, which lives only in RAM (so v8 cannot tell us the real
Fireworks event sequence); the incoming Codex body, of which only 5 settings are kept; reasoning
items or content; response headers; and Codex-side retries, which show up only as consecutive
identical requests.

## 6. Proposed minimal "controller hook"

Design constraints:
- (i) `controller=None` must behave exactly like v8.
- (ii) Every upstream request is its own numbered `api/NNN` call, so v8's per-call invariants
  still hold: identical `request_sha256` across attempts, and summed attempt usage (audit.py:325-327).
- (iii) Hooks see only data that also exists in the public records, so the evolver can replay
  them offline and deterministically over past episodes.
- (iv) Forward only real provider bytes.
- (v) All inserts are appended at the tail and sticky.

### 6.1 New module `controller.py` (pure: no network, no key, no model-code execution)

```python
from dataclasses import dataclass, field
from typing import Literal, Protocol, Sequence

Kind = Literal['turn', 'compaction', 'resume']

@dataclass(frozen=True)
class View:
    codex_request: int              # ordinal of the Codex HTTP POST (a Codex retry gets a new ordinal)
    kind: Kind                      # classify(input, tools) — see helpers
    codex_input: Sequence[dict]     # items exactly as Codex sent them (never mutated)
    tools_offered: bool
    elapsed_s: float                # since actor start
    remaining_s: float | None       # to the actor deadline (None = unbounded)
    gate_rounds_used: int           # per task, across the whole run

@dataclass(frozen=True)
class Insert:
    reason: Literal['loop_guard', 'time_budget', 'ledger', 'wrap_up']
    text: str                       # rendered as a role:'user' input_text message, prefixed '[harness:<reason>] '
    anchor: Literal['tail', 'summary'] = 'tail'   # 'summary' = right after the compaction summary item
    sticky: bool = True
    drop_tools: bool = False        # wrap_up only: send tools=[] for this one call (full cache miss; declared)

@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str                     # concrete, e.g. "answer 312.20 not printed by any calc output"

@dataclass(frozen=True)
class Verdict:
    action: Literal['forward', 'revise']
    checks: tuple[Check, ...] = ()
    feedback: str | None = None     # required when action == 'revise'

class Controller(Protocol):
    def on_request(self, view: View) -> list[Insert]: ...
    def on_final(self, view: View, response: dict, round: int) -> Verdict: ...
    def on_forward(self, view: View, response: dict, *, discarded: Sequence[dict]) -> None: ...

# helpers (pure, unit-tested, replayable on v8 records)
def classify(codex_input: Sequence[dict], tools_offered: bool) -> Kind: ...
def normalized_output(text: str) -> str: ...        # strip 'Chunk ID:' and 'Wall time:' header lines
def tail_streak(codex_input: Sequence[dict]) -> tuple[int, dict | None]: ...  # identical (name, args, normalized output)
def is_final_answer(final: dict, tools_offered: bool) -> bool: ...
def echo(output: Sequence[dict]) -> list[dict]: ...  # Codex re-serialization: drop status, drop message-part annotations
def note(reason: str, text: str) -> dict: ...        # {'type':'message','role':'user','content':[{'type':'input_text','text':...}]}
```

### 6.2 Broker changes (small diff against broker.py)

```python
class Broker:
    def __init__(self, key, output, *, ..., controller: Controller | None = None,
                 actor_deadline: float | None = None,     # time.monotonic() value from campaign
                 max_gate_rounds: int = 2, gate_min_remaining_s: float = 240): ...

    # broker.py:289-400 factored out unchanged, so it can be called more than once per Codex request
    def _call(self, outgoing: dict, *, origin: Literal['codex', 'controller'],
              codex_request: int, parent: int | None) -> tuple[int, dict, bytes, str, int]:
        """Number the call, save request/transport/attempts exactly as v8, return
        (number, final, wire, content_type, http_status). Raises TransportFailure as today."""

    # splice state: survives across Codex requests, dies with its anchor
    @dataclass
    class Splice:
        after_id: str               # id of the Codex item the items follow
        items: list[dict]
        reason: str
        created_request: int

    def _materialize(self, codex_input: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
        """Return (sent_input, applied, dropped): insert every live splice after its anchor id,
        preserving creation order; drop splices whose anchor id is absent (compaction)."""
```

The `do_POST` flow, where the numbers are the v8 lines being replaced or wrapped:

1. `incoming` → `normalized` (262), unchanged.
2. If `controller`:
   - `view = View(...)`;
   - `sent, applied, dropped = _materialize(incoming.input)`;
   - `inserts = controller.on_request(view)` (wrapped in try; on exception record
     `controller_error` and continue as v8, i.e. fail-open);
   - turn each `Insert` into a note item, add it to `sent` at the tail (or after the summary item),
     and register a `Splice` if sticky;
   - `outgoing['input'] = sent`.
3. `num, final, wire, ctype, status = _call(outgoing, origin='codex', ...)`, which replaces 276-400.
4. Gate loop:

   ```python
   rounds = 0; drafts = []
   while (controller and view.tools_offered and is_final_answer(final, True)
          and view.gate_rounds_used + rounds < max_gate_rounds
          and (remaining is None or remaining > gate_min_remaining_s)):
       v = controller.on_final(view, final, rounds)
       record_verdict(num, v)
       if v.action != 'revise': break
       extra = echo(final['output']) + [note('gate', v.feedback)]
       outgoing = dict(outgoing, input=outgoing['input'] + extra)
       add_splice(after_id=last_id(incoming.input), items=extra, reason='gate')   # sticky
       drafts.append((num, final, wire, ctype, status))
       try:
           num, final, wire, ctype, status = _call(outgoing, origin='controller', parent=num, ...)
       except TransportFailure:
           num, final, wire, ctype, status = drafts.pop()    # fall back to forwarding the draft
           record_fallback(); break
       rounds += 1
   ```

5. `controller.on_forward(view, final, discarded=drafts)`, then forward `wire` exactly as
   406-414. Heartbeats keep running through the whole loop, because `heartbeat_stop` is set only
   at 406.

Wrap-up near the deadline: when `remaining_s < T_wrap`, `on_request` returns
`Insert('wrap_up', 'Time is nearly up. Reply now with only your best final answer line.',
drop_tools=True)`. Fireworks is known to return message-only responses to `tools=[]` requests,
since every compaction does, so Codex ends the turn with a provider-generated answer. Separately
(outside the broker), the campaign should read the ledger's best candidate when `run_actor` times
out and record it in a separate `fallback_answer` field, keeping the status `timeout`. A
synthesized SSE answer is **not** recommended (§3c).

Loop guard as `on_request`:

```python
k, call = tail_streak(view.codex_input)
if k >= 3 and not self.already_noted(call):
    return [Insert('loop_guard', f'You ran the identical command {k} times and got identical output: …')]
```

The controller also keeps a per-run command log fed by `on_forward`, so repeats that span a
compaction can be counted. Replaying this rule on v8 gives 14/9/13 runs and 1,640/1,104/1,335
requests as golden numbers. For example, none/072 first fires on request 22.

### 6.3 Records (transparent, reconstructable)

- **Per call:** `api/NNN/transport.json` gains `origin`, `codex_request`, `parent_call`,
  `forwarded` (bool) and `kind`.
- **New `api/NNN/controller.json`:**
  `{codex_body_sha256, codex_input_items, sent_input_sha256, inserted: [{index, reason, sticky, anchor_id, item}], dropped_splices: [...], verdict: {action, checks:[...], feedback}, round}`.
  Removing `inserted[*].index` from `request.json.input` must reproduce Codex's input exactly
  (this is a test).
- **`api/controller.jsonl`:** an append-only decision log, one line per hook decision.
- **`summary.json` gains:** `codex_requests`, `controller_calls`, `discarded_responses`,
  `inserts_by_reason`, `gate_rounds`, `gate_fallbacks`, `controller_errors`, `controller_usage`.
  `calls` stays equal to the number of `api/NNN` directories, so the audit check at
  audit.py:299 still holds.
- **Downstream:** trajectory/digest builders must merge `controller.jsonl` with `events.jsonl`.

## 7. Test cases for the fake-upstream harness

Harness upgrades:
- `Upstream(responder)` with `responder(body) -> response | raw_bytes | ('http', code, body) | 'stall'`,
  so replies can depend on whether a note or feedback is present.
- `sse_full(response)`, which emits `response.created`, `output_item.added`, deltas,
  `output_item.done` for each item, and the terminal event.
- `CodexSim`, which drives several requests the way Codex does: append `echo(output)` plus fake
  `function_call_output` items (with `Chunk ID`/`Wall time` headers), perform a compaction
  (`tools=[]` + prompt), resume with fresh ids plus the "Another language model…" summary, and
  retry the identical input after an error.
- An injectable clock for deadline tests.

1. **v8 equivalence.** With `controller=None`, all existing tests pass, and upstream bodies and
   forwarded bytes are identical to v8 for the same Codex bodies.
2. **History view.** `on_request` receives exactly the items `CodexSim` sent, and `classify`
   returns `turn/compaction/resume` correctly.
3. **Loop guard.** It fires at k=3 of (name, args, normalized output) even though Chunk ID and
   Wall time differ, and not at k=2. It does not fire on `write_stdin` polls with changing output.
   It fires only once per identical call.
4. **Placement.** The note goes after the last `function_call_output`, never between a call and its
   output, including the case of two parallel calls. The note is role `user` with an
   `input_text` part.
5. **Stickiness and cache prefix.** For every request after an insert, the upstream `input` of
   request N+1 starts with the upstream `input` of request N.
6. **Compaction drop.** After compaction plus resume, old splices are dropped (anchor absent) and
   recorded. The ledger is inserted after the summary item once, re-applied unchanged afterwards,
   and never refreshed.
7. **Codex retry idempotence.** After a broker error, `CodexSim` re-sends the identical input. The
   upstream body is byte-identical (same splices), and there is no duplicate note.
8. **Gate pass.** Exactly one upstream call; the forwarded bytes equal the upstream wire.
9. **Gate revise.** Call 2's input equals call 1's input plus `echo(draft)` (no `status`, no
   `annotations`) plus feedback. Forwarded bytes equal call 2's wire. The call 1 record has
   `forwarded: false` and `origin: codex`; the call 2 record has `origin: controller` and
   `parent: 1`.
10. **Gate cap.** Three failing drafts produce 3 upstream calls, the last is forwarded, and the
    log shows `rounds_exhausted`. The cap is per task, across turns.
11. **Revision with tool calls.** The revision is forwarded. `CodexSim`'s next request (without
    the draft) is sent upstream as call 2's input plus the revision items plus outputs.
12. **Defect during a revision** (e.g. `incomplete_without_output`). It is re-sent with identical
    bytes inside the controller call, and only one stream is forwarded.
13. **Revision transport failure.** The draft wire is forwarded, and the fallback is recorded.
14. **Deadline.** With remaining below `gate_min_remaining_s`, the gate is skipped. With remaining
    below `T_wrap`, the wrap-up note is sent with `tools=[]`, and the forwarded response is
    message-only.
15. **Incomplete revision.** Codex retries; the same splice is applied and no extra round is
    counted.
16. **Accounting.** The broker's summed usage equals the sum over all upstream calls, including
    discarded drafts. Each call directory keeps the audit invariants (identical sha across
    attempts; usage sum). `controller_usage` is reported separately.
17. **Reconstruction.** Removing `controller.json.inserted` from `request.json.input` gives
    Codex's input, and the hash matches `codex_body_sha256`.
18. **Heartbeats.** With the FULL profile and a slow fake, SSE comments keep flowing through the
    gate rounds, and the client receives exactly one terminal event.
19. **Stop.** A stop set during the gate means nothing is forwarded, an `event: error` is written,
    and the summary is written.
20. **Controller exception.** The hook raises, and the call behaves like v8 (fail-open) with a
    recorded `controller_error`.
21. **Redaction.** The key never appears in `controller.json`, `controller.jsonl` or notes.
22. **Offline replay goldens.** Running `classify`/`tail_streak` over the recorded v8 `api/`
    directories reproduces 35/38/46 compactions and 14/9/13 loop runs, and the result is
    deterministic across two runs.
23. **Codex-in-the-loop smoke test** (local, no network). Run the pinned
    `frameworks-v8/codex-0.155.0-alpha.9.2/codex exec` against broker plus fake upstream
    (`sse_full`). Check that `final.md` equals the revised text, that `events.jsonl` has only the
    revised agent message, and that a forwarded tool-call revision is executed. This is the only
    test that proves the "Codex accepts it" claims in §3c.

## 8. Open questions that cannot be settled from v8 records

- Does Codex re-send `reasoning` items, and with what fields, to Fireworks? `public()` strips them
  from every record (broker.py:43), so `echo()` for reasoning items is unverified.
  → In v9, record a reasoning skeleton for the incoming input (count and key sets, no content).
- Does GLM-5.3 on Fireworks handle a mid-history `developer` message? No evidence; use `user`.
- What is the real Fireworks SSE event sequence? The wire is never saved.
  → Record an event-type skeleton (types and byte counts only) so the fake can match it.
- Should the loop guard also allow a `temperature` bump? The v8 loops are temperature-0 fixed
  points, but a bump changes the sampling protocol and has to be declared per arm.
- Why is Codex's effective compaction trigger around 240K tokens despite `800000`? The warning
  about fallback model metadata is the likely lead. This is outside the broker, but it decides when
  the ledger splice happens.
