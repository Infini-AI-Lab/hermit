# S5-learners review: requests for files the review does not own

Paths are relative to `officeqa-pro133-v9-plumbing/`. None of these blocks a test: the whole suite passes
(319 passed, 49 opt-in skips).

## R1 (S1 / S6): the actor sandbox can read the whole adapter directory

**Where:** `acting.py`, in the sandbox profile's `read_roots` (around lines 293-301). It contains:

    *([str(Path(adapter).resolve())] if adapter is not None else []),

**Why it matters:** the actor can therefore `cat` every file of the adapter:
- `notes/*.json`: all learned notes, not only the ≤ 3 served by trigger, plus their `stats`;
- `rules/*.json`: every rule, with its `stats`;
- `policy.json` and `SEED.md`.

This breaks the note scoping (DESIGN §2 L5, "at most 3 matching notes rendered … per episode"). It also makes
the adapter an uncounted model-visible channel next to the 16 KB `TEXT_BUDGET`.

**What S5 did:** note and rule ids are now leak-screened as a stopgap. The fix belongs to the sandbox.

**Request:** drop the adapter directory from the actor's read roots. In v9 `AGENTS.md` is composed into the
workspace, and the controller reads the rules and policy broker-side. If some part of the adapter must stay
readable, list exactly which files, and add a kit test that `cat <adapter>/notes/x.json` is denied.

## R2 (S6, full_audit.py): accept the priority and disable admission modes

**Where:** `full_audit.admitted_rules` (around lines 272-311). For every served rule that differs from W0 it
requires:

    record is not None and bool(record.get('counterfactual')) and bool(record.get('precision'))

**The mismatch:** S5 (`mechanism_learning.modify_rule`) admits two declared modes whose records have no
counterfactual:

| mode | record in `mechanical-admission.json` `rule_admission[id]` | admitted when |
|---|---|---|
| `'priority'` | `{mode: 'priority', admitted: true, reasons: []}` | the schema check passes |
| `'disable'` | `{mode: 'disable', precision: {n_right, fired_right, right_fire_rate, ...}}` | the point right-fire rate is > 0.20 on ≥ 10 right episodes |

A campaign in which the learner reprioritises or disables a rule would therefore fail the audit.

**Request:** branch on `record.get('mode')`:
- `'full'` (or absent): require `counterfactual` and `precision`;
- `'priority'`: require that the served rule differs from its predecessor only in `priority` and `stats`;
- `'disable'`: require `precision.n_right >= 10` and `precision.right_fire_rate > 0.20`, and that `enabled` is
  the only other change.

Alternatively, the user decides that these modes need the full admission, and S5 changes `modify_rule`.

**Note:** after this review, `admitted_rules`, `policy_changes`, `rule_admission` and `policy_replay` list only
**net** changes. Edits that cancel out within a round are in `edits_without_net_change` and do not reach the
served adapter. So the audit's current lookup, the last admission per id across rounds, stays valid.

## R3 (client.py owner / [user]): learner transport semantics

`learner_common.FireworksModel` calls `client.complete(..., reasoning_effort=..., max_tokens=...)` without
`unbounded=True`, so that both parameters are sent. Two side effects follow in `client.py`:
- `rate_control.MAX_ATTEMPTS = 5` instead of unlimited retries within the call timeout;
- a hard failure when the provider's `completion_tokens` exceeds the requested `max_tokens`, which reasoning
  at `'high'` may cause.

Either condition fails the round, and `campaign.learn` then raises `LearnerFailed`, which stops the campaign.
It is identical for both learners.

**Request:**
- Confirm both behaviours in the paid learner smoke.
- If they bite, add an option to `client.complete` that keeps `reasoning_effort` / `max_tokens` but uses
  `max_attempts=None`, and records rather than rejects a completion-token overrun.
- Also note that the default payload carries `reasoning_effort=4096` (an integer), which S5 overrides with the
  string `'high'`.

## R4 (PREREG / PROTOCOL owner): declare these learner behaviours

- **Net-edit accounting.**
  - A round's rule and policy edits are applied by net effect.
  - Cancelled edits count toward `MAX_EDITS=2` but change nothing.
  - A round whose only edits cancel out is `retained`.
- **The runtime limit.** `RULE_RUNTIME_MS = 2000` applies to engine time. Sandboxed offer re-execution is
  bounded separately by the runner's 5 s wall and recorded as `max_runner_ms`.
- **Mechanism-arm notes** are verified on the source adapter + the note, without the same block's rule and
  policy edits. The text arm verifies in the served context. **[user]**: keep this, or re-verify against the
  pending revision.
- **The shared proposal cap.** `PROPOSAL_ATTEMPTS = 20` is shared across notes, rules and policy in the
  mechanism learner.
