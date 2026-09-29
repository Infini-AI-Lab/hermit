# S7-integrate: requests for files S7 does not own

S7 applied the wave-1 cross-stream requests inside its own files (see `S7-integrate.md`). The items below need
a change in someone else's file, or a decision. None of them blocks the tests: the suite passes as it is.

Paths are relative to `officeqa-pro133-v9-plumbing/`.

## Broker owner (S3): `broker.py` beyond `View.call`

S7 was allowed to touch `broker.py` only to fill in `View.call`. Two follow-ups are needed.

1. **Count `status == 'rejected'` as `submit_invalid`.**
   - The gate now returns `GateResult.status = 'rejected'` for a payload it cannot use (contracts v9.1;
     S4 request 5). Before, it returned `'findings'` with no findings.
   - `Broker._submit` counts `submit_invalid` only for `status == 'findings' and not result.findings`
     (`broker.py`, the lines after `result = self._hook('on_submit', None, payload)`). A `'rejected'` result
     is now counted in `submits`, but in neither `gate_rounds` nor `submit_invalid`.
   - This happens only for payloads that pass the broker's own checks but fail the gate's. Examples: an
     answer between 1,001 and 8,000 characters, or a `keep` reason over 500 characters.
   - Suggested change: `elif result.status in ('findings', 'rejected'):` → `submit_invalid`.
   - The HTTP status can stay 200. The `submit` CLI prints the gate's "rejected: … Usage: …" message
     either way.

2. **The controller lock still makes `/v1/submit` wait for `on_request`.**
   - The ledger now runs the pack re-derivation outside its own lock (S2-review 3).
   - But `Broker._hook` holds `controller_lock` around the whole `on_request` call, and `on_request` runs
     `ledger.ingest_request`, which includes that re-derivation. `on_submit` takes the same lock, so a
     submit that arrives during an `on_request` still waits for it. That wait is at most about 6 pack
     re-runs, about 3 s each in the worst case.
   - In practice this rarely happens. Codex sends its next request only after every tool call has
     finished, so a submit coincides with an `on_request` only when a background `write_stdin` session
     calls `submit`.
   - If you want to remove it anyway:
     - `MechController.on_submit` touches only the gate (it has its own lock), the ledger (its own lock)
       and `decisions.append`;
     - so `on_submit` could run under a separate, narrower lock.
   - This is your call, and it is not needed for correctness.

## S4 (harness_core outside S7's files; packs/toy)

3. **`RawPack.observations` (`harness_core/observe.py`) and `ToyPack.observations` (`packs/toy/pack.py`)
   could accept `raw_command=''`**, as `contracts.Pack` now documents.
   - The ledger inspects the signature and passes `raw_command` only when the pack accepts it, so
     nothing breaks today.
   - Both packs could also gain `extract_final`. The controller falls back to the last non-empty line,
     which is what they would do anyway.

## S5 (learners) and S6 (orchestration, audit, cost, preflight)

4. **OfficeQA tool paths changed** (S1-review request 2).
   - `packs/officeqa/cli.py` and `otables.py` are now `packs/officeqa/tools/cli.py` and
     `packs/officeqa/tools/otables.py`. The old paths no longer exist.
   - Get the kit wrappers from the pack:
     ```python
     from packs.officeqa.pack import tool_wrappers
     tools = {**tool_wrappers(), 'calc': str(HARNESS_CORE / 'tools/calc.py')} if arm.l1_tools else {}
     ```
   - The actor then reads `tools/cli.py` and `tools/otables.py`, and can list only `tools/`. The files in
     that listing are `__init__.py`, `cli.py`, `otables.py` and `__pycache__`.
   - Building the index is now `python -m packs.officeqa.tools.cli build --corpus … --out …`.
5. **Settings defaults changed in `acting.py`.** These are the decisions made for all v9 arms.
   - `build_settings` and `run_actor` default to `permissions_instructions=False`. Do not pass it.
   - `V9_SETTINGS` is now: multi-agent off, and `model_auto_compact_token_limit=244800` set explicitly.
   - `invocation.json` gains two fields:
     - `auto_compact_token_limit`;
     - `v9_declared_settings`: the declared v8→v9 settings differences, each with its reason.
   - Please copy `acting.V9_DECLARED_SETTINGS` into PREREG.md / PROTOCOL.md.
   - Temperature is unchanged: the broker still sends 0.
6. **Controller construction.** S3 request 7 still applies. Two notes:
   - `MechController(ledger, gate, policy, clock)` finds the pack through `gate.pack`, so it needs no
     extra argument.
   - Leave `requery_when_clean` at its default (`False`). That is the decision: re-query only when the
     gate has findings.
   - Construct `OfficeQAPack(corpus, index)` once per episode and pass the same object to the Ledger and
     the Gate. Its tables connection is now safe to share across the broker's threads.
7. **What ledger.jsonl records now.** Learner evidence, audit and digests should expect the following.
   - `Note(reason='enforce')` is written only by the controller. It is written once per enforcement
     re-query whose response arrived, on that re-query's call number, and its text is the feedback the
     model saw.
   - The gate no longer writes a Note for `check_final`.
   - `Note(reason='gate')` still marks every submit message.
   - `Command` events carry `raw_cmd` (at most 8 KB, the exact command) and `tool` (the Codex tool
     name). Every Command is written twice, as before, so ledger.jsonl grows by about 2 × `raw_cmd`
     per command.
   - `Candidate(via='final').answer` is the answer span (`pack.extract_final`), not the whole final
     message.
8. **Rejected submits.**
   - `api/controller.jsonl` rows `event: submit` can now carry `status: 'rejected'`. `summary.json`
     counts them only after item 1 is done.
   - A rejected submit writes no `checks/NNN.json` and no Candidate.
9. **pytest groups** (`tests/conftest.py`, new).
   - `slow` and `live_codex` are registered and opt-in. Opt in with `-m slow` / `V9_SLOW=1` or
     `-m live_codex` / `V9_LIVE_CODEX=1`.
   - The S2 structural cases (`tests/test_officeqa_tables.py`, 22 slow tests) are now opt-in too. Before,
     they ran by default.
   - The two `test_real_codex_*` tests in `tests/test_v9_kit.py` are now `live_codex`.
   - For preflight:
     - tier 0: `pytest tests/test_v9_kit.py tests/test_v9_{ledger,engine,gate,runner,replay}.py`;
     - the $0 real-Codex tier: add `V9_LIVE_CODEX=1`, or run `-m live_codex tests/test_v9_codex_live.py`
       (16 tests, about 20 s).
10. **Text arms' W0** (decision already made, for S5/S6): the full seed text is the core advice plus
    `OfficeQAPack().seed_text()`. Scoped learned notes: at most 3 per episode, chosen by trigger.
    `seed_notes()` stays available for the note format.

## Contracts owner (documentation only; no shape change)

11. **Say what `View.call` means for each hook.** As S7 implemented it in the broker:
    - `on_request`: the call about to be sent;
    - `on_response`: the call whose response is shown, which also equals the hook's `call` argument;
    - `on_final`: the call that produced the final message being judged (the draft's call). The re-query's
      number is not allocated until the verdict is `revise`.

    The contract comment says "the api/NNN number the (next) upstream call is sent as". That is exact only
    for `on_request`.
12. **`handles_truncation` counts only when it is exactly `True`.** The ledger uses
    `getattr(pack, 'handles_truncation', False) is True`. `max_observations` must be a positive int;
    anything else means the default of 400.
