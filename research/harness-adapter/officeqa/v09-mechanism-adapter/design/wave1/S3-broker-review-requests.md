# S3 review → requests for files the S3 stream does not own

These are from the adversarial review of S3. They add to the implementer's `S3-broker-requests.md` and do not
replace it. Each item names the owner, what is needed, and the local workaround in place today.

## harness_core/gate.py (S4)

R1. **`Gate._check` records a `Note` for `check_final` even when the model never sees it** (`gate.py:180`,
    `reason='enforce'`, for every `kind == 'final'` check).
    - `check_final` runs whenever the controller evaluates an unsubmitted final. When the check is clean, or
      enforcement is skipped after the check, nothing is shown to the model. The ledger still claims an
      `enforce` note was shown, but `contracts.Event` defines `n<n>` as "harness text shown to the model".
    - When a re-query does happen, `MechController` now records the feedback it actually sent, as a second
      `enforce` Note on the re-query's own call number. Reproduced in a scratch run with S4's `Ledger`, `Gate`
      and `RawPack`: notes `n3` (gate, call 8, never shown) and `n4` (controller, call 9, shown).
    - *Ask:* add the Note only for `kind == 'submit'`, where the submit CLI prints the message as tool output.
      Leave final-check notes to the controller.
    - *Workaround:* none needed for correctness of the broker. Learner evidence built from `ledger.jsonl`
      would over-count shown notes until this is fixed.

## contracts.py (wave 0 owner) / S5 learners

R2. **Policy validation is now enforced on the broker side as well.** `controller_runtime.checked_policy`
    raises `ValueError` for an unknown key or an out-of-range value, which is the same rule as
    `harness_core.rules.validate_policy`. A mech-evolve learner must only emit policies inside
    `POLICY_RANGES`. Otherwise controller construction fails loudly, by design.
    - *Ask:* S5 should validate with `rules.validate_policy` at admission, so this never fires at episode
      start.

R3. **Decide the enforcement trigger with findings versus without.**
    - The S3 brief said to re-query every unsubmitted final: `check_final`, then `Verdict('revise')`.
    - The implementation re-queries only when `check_final` reports a finding (`controller_runtime.py:431`).
      `requery_when_clean=True` restores the brief's behaviour.
    - This changes the pilot's go/no-go "re-query rate". PREREG must fix one setting before the pilot.
    - Default today: findings only.

## campaign.py (S6)

R4. **Pass `actor_deadline` on the broker's clock** (`time.monotonic()` + the actor's kill seconds), and pass
    it for every controller arm.
    - The enforcement re-query is now bounded by the broker to end `REVISION_DEADLINE_MARGIN_S = 15` s before
      that deadline. After that the draft is forwarded.
    - Without `actor_deadline` the re-query has no bound in the FULL profile. A v8-style 600–1460 s stall
      would then kill the actor with an empty prediction, although a draft answer was in RAM.

R5. **Summary field added:** `summary.json['submit_invalid']`: submits that the gate rejected as malformed
    (`status == 'findings'` with no finding). These no longer count as `gate_rounds`. `cost.py` and the
    digests should read it if they report submit rates.
