# S4 (harness_core) — requests to other streams

S4 edited no file outside its own (harness_core/* except contracts.py, packs/toy/, tests/test_v9_{ledger,engine,gate,runner,replay}.py).
Each item names the owner of the change and why. Nothing here blocks S4's tests; they pass with the code as it is.

## contracts.py (Wave-0 owner)

1. **`LedgerAPI.ingest_request` gains an optional keyword `tools: Sequence | None = None`.**
   - What it is: the request's `tools` list, which Codex leaves empty (`[]`) only on its context-compaction request.
   - Why: detection is then exact. Without it, the ledger falls back to the fixed prompt text
     ("You are performing a CONTEXT CHECKPOINT COMPACTION"), which matched 119/119 v8 compaction requests.
   - Status: already implemented in `Ledger` as an additive keyword. Existing callers (S3's `controller_runtime.py`)
     keep working.
2. **Document the `ledger.jsonl` line semantics in the `LedgerAPI` docstring.**
   - A `Command` is written twice under the same `ref`: once when its `function_call` is relayed, and again when its
     `function_call_output` arrives (exit code, output hash, byte size, truncation flag).
   - Readers must fold lines by `ref`, keeping the last line and the first-seen order. `harness_core.ledger.load_events`
     does this.
   - Why: the first N lines then reproduce the exact state when line N was written. Every gate check records N, so
     offline replay sees the same inputs as the live gate did.
3. **Write the calling conventions into `Predicate` / `Offer`.**
   - Predicate: `pred(ctx: RuleContext, **args) -> list[Binding]`.
   - Offer: `offer(ctx: RuleContext, binding: Binding, **args) -> OfferResult | None`.
   - S2 already follows both. Today the types say only `Callable[..., ...]`.
4. **`Compute.inputs` entries carry three more keys. Please list them in the comment.**
   - `trivial: True`: small integers ≤ 12 and powers of ten. These are never linked or flagged.
   - `in_string: True`: a quoted number, e.g. `float("1,234.5")`.
   - `scale: k`: the 10^k factor of a `'scaled'` match.
5. **`GateResult.status` for an unusable payload (optional).**
   - Now: `status='findings'` with `findings=[]` and the message `[harness:gate] rejected: …`.
   - Proposal: add `'rejected'` to the Literal. S3's route already rejects most bad payloads with HTTP 400 before they
     reach the gate.
6. **`OfferResult` could carry the executed code and its stdout (optional).**
   - Now: the core `recompute` offer stores its run on the `RuleContext`. The gate then records the run of the offer it
     shows as `Compute(source='gate:<rule>')`.
   - Why record it: an accepted offer is then itself a calc output, so `recompute_final` does not flag it.

## S3 (broker.py, controller_runtime.py)

7. **`MechController._note_event` builds note refs as `n<count+1>`. Please pass `ref=''` instead.**
   - `Ledger.add` then assigns the ref itself.
   - Why: `Ledger.add` raises `ValueError` on a duplicate ref. Today's scheme only works because the ledger's note refs
     happen to be contiguous.
8. **Enforcement notes are recorded twice.** This is harmless.
   - `Gate.check_final` adds `Note(reason='enforce', text=<gate message>)`.
   - `_note_event('enforce', feedback)` adds the wrapped feedback.
   - Keep both (the gate's copy is what the ledger strips from outputs), or drop yours. The choice is yours.
9. **Loop guard, "identical output".**
   - `Ledger.command_output_count(cmd, output_sha256)` counts one command together with one exact normalised output.
   - The `Command` events you already read carry `output_sha256`.
10. **How to build the gate and the runner per episode:**
    ```python
    ledger = Ledger(pack, run / 'api' / 'ledger.jsonl')          # refuses an existing file or a symlink
    runner = SandboxRunner(python=paths.ACTOR_PYTHON, extra_read=[...])  # extra_read only if pack offers read data
    gate = Gate(pack, rules, policy, runner, question, api_dir=run / 'api')   # writes api/checks/NNN.json
    ```
    - `Gate.needs_enforcement(final_text, ledger)` implements the "never submitted, or numerically different" test.
      Using your own `same_answer` is also fine.
11. **Restart caveat.** Gate state lives only in RAM: the findings shown per value, the keeps, and the offers made. If
    the broker restarts mid-episode, that state is lost. The check files hold it, but nothing reloads it.

## S1 (kit)

12. **No change needed. For the record:**
    - `harness_core/tools/calc.py` and `submit.py` run standalone under `python -I -B <file>`. They fall back to their
      own copies of the contract constants. `test_cli_scripts_run_standalone_like_the_kit_wrapper` checks that those
      copies match.
    - `calc` executes code with `sys.executable`, i.e. the frozen python, under a 60 s wall timeout (`--timeout`).
    - `submit` accepts only `http://127.0.0.1:<port>/v1/submit`. It uses no proxies and follows no redirects.

## S5 (learners)

13. **Use `harness_core.replay` and `harness_core.rules` for admission.** Results hold only ids, booleans and counts,
    never answer values.
    - Load episodes: `load_episode(api_dir, episode_id=, question=, final_answer=, label=)`.
    - Step 2, precision: `fire_stats(rules, episodes, pack, runner)`, then `precision_check(stat, max_right_fire=0.2,
      min_right=10)`. The check uses the Wilson upper bound by default.
    - Step 1, counterfactual: `counterfactual(rule, episode, reference, grade_fn, pack=, runner=)`. You inject
      `grade_fn`; S4 never imports a grader.
    - Online equals offline: `recheck(check, ledger_path, pack, runner)`.
    - Schema gate for learned rules: `validate_rule(rule, predicates=..., offers=...)`, `validate_policy`,
      `load_rules`, `save_rule`, `rendered_text_bytes` (the rules' share of the 16 KB budget).

## S6 (orchestration / preflight)

14. **Register the `slow` pytest marker** in a `conftest.py` or `pytest.ini`. S4 owns neither. S4's slow tests also
    need `V9_SLOW=1`: 4 v8 runs, about 4 s. With `V9_SLOW=all` they cover all 399 v8 runs in about 41 s.
15. **Add the S4 tier-0 tests to preflight:**
    `adapter-comparison-work/.venv/bin/python -m pytest -q tests/test_v9_{ledger,engine,gate,runner,replay}.py`
    (63 tests, about 9 s, $0).
