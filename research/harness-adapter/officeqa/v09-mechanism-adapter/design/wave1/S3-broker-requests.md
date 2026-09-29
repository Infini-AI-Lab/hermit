# S3 (broker) → requests for files S3 does not own

Each item says who owns the file, what S3 needs, and the local workaround in place today.

## contracts.py (wave 0 owner)

1. **`View` should carry the upstream call number.** Add `call: int` (the `api/NNN` number the request is
   sent as). `LedgerAPI.ingest_request(call, …)`, `GateAPI.submit(…, call)` and `Event.call` all need it,
   but `View` carries only `codex_request` (the Codex POST ordinal). A Codex request can produce more than one
   upstream call (enforcement re-query), and a failed call still uses up a number.
   *Workaround:* the broker calls an optional duck-typed `controller.bind_call(n)` right before each
   upstream call. `MechController` implements it (`controller_runtime.py`).
2. **Decision log.** Add an optional `Controller.drain_log() -> list[dict]` (or state that controller
   decisions are logged only as ledger `Note` events).
   *Workaround:* the broker calls it if present and writes the rows to `api/controller.jsonl` as
   `event: decision`.
3. **Enforcement rule: pick one wording.**
   - DESIGN §2 L4 says: re-query when the final was never submitted, *or its numeric tokens differ from the
     last submission*.
   - The S3 task says: re-query when *no submitted candidate* has the same numeric tokens.
   - S4's `harness_core/gate.py` already has `Gate.needs_enforcement(final_text, ledger)`, which follows the
     DESIGN wording and is pack-aware.
   *Workaround:* `MechController` uses `gate.needs_enforcement` when the gate has it. Otherwise it falls back
   to the task wording. Please add `needs_enforcement` to `GateAPI`, or drop one of the two rules.
4. **`LedgerAPI.next_ref(kind)`.** The controller adds `Note` events and must invent refs.
   *Workaround:* `'n%d' % (1 + number of existing notes)`.
5. **`Command.tool` (tool name).** The loop guard must skip `write_stdin` polls, but `Command` has no tool
   name.
   *Workaround:* the controller keeps its own `call_id → tool name` map (from `on_response`), and also treats
   `cmd` starting with `'write_stdin '` (S4's `command_text` form) as a poll.

## tests/conftest.py or pytest.ini (S6 / integration owner)

6. **Register the markers.** Please add `slow` and `live_codex`. Today pytest prints
   `PytestUnknownMarkWarning`. Both groups are opt-in:
   - `-m slow` or `V9_SLOW=1`
   - `-m live_codex` or `V9_LIVE_CODEX=1`

## campaign.py / cost.py / audit / digests (S6)

7. **Construct the controller for mech arms only:**
   ```python
   ledger = Ledger(pack, api / 'ledger.jsonl')
   gate = Gate(pack, rules, policy, runner, question, api_dir=api)          # writes api/checks/NNN.json
   controller = controller_runtime.MechController(ledger, gate, policy, time.monotonic)
   with broker.Broker(key, api, controller=controller,
                      actor_deadline=time.monotonic() + ACTOR_SECONDS, **BROKER_OPTIONS) as relay:
       env[contracts.SUBMIT_ENV] = relay.submit_url                        # http://127.0.0.1:PORT/v1/submit
   ```
   - For `plain` and `text-*` arms, keep `controller=None`. The records are then byte-identical to v8
     (tested), and have none of the v9 fields.
8. **`cost.py`: group by `transport.json['origin']`, treating a missing value as `'codex'`.**
   - Values: `codex` or `controller`.
   - `summary.json['controller_usage']` sums the controller re-queries.
   - `summary.json['usage']` still sums every attempt.
9. **Digests, learner evidence and audit must merge `api/controller.jsonl` and `api/NNN/controller.json`.**
   - Verified live: Codex's `events.jsonl` and `final.md` never contain loop notes, the ledger splice, the
     discarded draft or the enforcement feedback.
   - `summary.json['calls']` still equals the number of `api/NNN` directories. Controller calls are numbered
     calls with `origin: controller` and `parent: N`.
   - Controller calls copy the parent's `native_request_settings`.
10. **Update `PROTOCOL.md` and the audit wording** (critic R9). The broker docstring now describes the
    controller mode. The old text said "an HTTP transport, not an agent loop", and that is true only for
    `controller=None`.

## acting.py (S1) — facts from the $0 live test (`tests/test_v9_codex_live.py`)

11. **`include_permissions_instructions=false` removes exactly one part:** the second `input_text` of the
    developer message.
    - That part is the `<permissions instructions>` block, which wrongly says "No filesystem sandboxing - all
      commands are permitted. Network access is enabled."
    - `instructions`, `tools` and every other item stay identical.
12. **Tool output is truncated to 10,000 chars (head + tail)** under the fallback model metadata.
    - The kept output carries a `Warning: truncated output (original token count: N)` header and a
      `…K chars truncated…` marker.
    - The call argument `max_output_tokens: 100000` does not raise the limit.
13. **A lower `model_auto_compact_token_limit` is honoured.**
    - With 2000, a reported usage of 5,000 compacts and 1,500 does not.
    - So S1 can set the limit explicitly, for example 244800, instead of leaving it inert.
14. **The developer message also carries a `<skills_instructions>` block** listing 5 system skills from
    `$HOME/.codex/skills/.system`. v8 had it too. It affects all arms equally; drop it only if the kit can.
15. **The broker port is reachable from model commands.** That reachability is what `/v1/submit` needs.
    - Any other localhost port is blocked by the one-port profile (`curl` rc=7).
    - Do not advertise the port in README beyond the `submit` CLI (critic C4).
16. **Please re-run `pytest -m live_codex tests/test_v9_codex_live.py` once the v9 `build_settings` lands.**
    Today the module copies the v8 FULL settings. To switch, replace `settings_for` with a call to
    `acting.build_settings`.

## harness_core/ledger.py (S4)

17. **`ingest_request` has an extra keyword `tools`.** The broker calls it with the contract's three
    positional arguments, so the ledger detects compaction from the prompt text alone. That matches v8 in
    119 of 119 cases.
18. **`Ledger.render()` is called once per compaction summary**, and the result is frozen by the broker. It
    must stay deterministic and at most `ledger_splice_bytes`.
