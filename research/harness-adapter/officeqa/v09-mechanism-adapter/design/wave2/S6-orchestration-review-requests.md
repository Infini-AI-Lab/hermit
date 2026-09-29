# S6-orchestration review: requests for files the reviewer does not own

Paths are relative to `officeqa-pro133-v9-plumbing/`. None of these blocks the tests; every test passes with the
code as it is. Each item names the owner, what is needed and why.

## S5 (learner_common.py, evidence.py)

1. **Opaque verification run directories.** Codex sends the workspace path to the model on every request. A
   verification actor today works in `<root>/evolution/BB/verify/<note id>/q<NNN>-k<k>/workspace`. That path
   tells it three things: that this is not a scored run ("evolution", "verify"), which note is being tested
   (a descriptive id such as `percent_share`), and whether this is re-run 1 or 2 (`k1`/`k2`).
   - This is symmetric for text-evolve and mech-evolve, so it is not a fairness asymmetry.
   - It does make the verification episodes differ from the scored episodes they are meant to predict.
   - Ask: choose `run_dir` as `output/verify/<opaque id>` (for example the first 12 hex characters of
     sha256(note id, question, k)) and keep the mapping in `verification.json`. The campaign's runner accepts
     any directory under `output/verify/`.
2. **Learning-ledger row names read by the audit.** `full_audit.learned_text` requires that every learned note
   served (every note that is not in W0) has a row in `<root>/learning-ledger.jsonl` with
   `kind == 'note'`, `action == 'add'`, `admitted is True`, `id == note.id` and an integer `t` no later than the
   round that served it. Please keep these names, or tell S6 if they change.
3. **Compute runner.** `Campaign.learn` now passes `runner=` (the gate's
   `make_runner(ACTOR_PYTHON, extra_read=[corpus])`) to `run_learning`. S5's own default was `make_runner()`,
   which cannot read the corpus. With that default, a recorded calc that reads a corpus file succeeds in the
   live gate but fails in admission replay, so the offer is None and a correct rule is not admitted. This
   handicapped mech-evolve only. No S5 change is needed, provided `run_round` keeps accepting `runner`.

## Broker owner (broker.py)

4. **Temperature.** `broker.normalized` hard-codes `temperature=0`. Once the pilot decides a temperature, make it
   a broker option that `experiment.TEMPERATURE` feeds. Until then, the campaign refuses to start when
   `experiment.TEMPERATURE` differs from what `normalized` sends (`campaign.broker_temperature`), and the audit
   checks every `request.json`.

## Operator / user

5. **Root names must be arm-neutral** (for example `v9-r1-a`). `arms.parse_config` refuses names that contain
   an arm-revealing fragment (`arms.ARM_REVEALING`). The PROTOCOL.md draft and the arms.py example used names
   such as `plain-r2`.
6. **Barrier timeout.** Use `timeout_s = 43200`, not the drafted 21,600 (the arithmetic is in PROTOCOL.md §3).
   Longer term, a heartbeat file per root would let the timeout detect dead roots without penalising slow
   ones.
7. **PREREG pending decisions.** `run.py run` does not refuse a formal run while
   `[DECISION PENDING ...]` lines remain. This was left as the brief asked. Before the first confirmatory
   campaign, either resolve the pending lines or add a guard.
