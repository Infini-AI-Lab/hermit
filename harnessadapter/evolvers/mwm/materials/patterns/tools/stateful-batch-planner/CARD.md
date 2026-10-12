name: stateful-batch-planner
when: a sequence of decisions each changes a running quantity, the decisions are made one at a time from memory, and the running total drifts
uses: stateful-tools, tool-output-surface

## Applicable when

Evidence shows step-by-step decisions that were each locally right but
wrong in total — step 4 decided as if steps 1-3 had not already changed
the count. Order matters: the same steps in another order give different
answers, so the running arithmetic must live in code, not recall.

## Skeleton

```
plan(batch, limits, already_used) -> per-item verdicts, in order
  any item fails to parse -> CATCH, refuse the WHOLE batch before
    any verdict (counts past the gap would be wrong)
  state = copy(already_used)
  for i, item in enumerate(batch, 1):
    key = item's capacity key
    if key has no limit on file      -> CATCH: item i refused
    elif state[key] + 1 > limits[key] -> CATCH: item i refused,
         limit already consumed by earlier items (used=n/limit)
    else state[key] += 1; emit ACCEPT with used=n/limit AND the
         exact write-call arguments to copy
```

Accepted lines carry copy-ready write arguments — the actor
transcribes, never re-derives; `used=n/limit` makes each verdict
auditable alone.

## Demo

See `demo/`. Six propagation requests are planned against per-seed-bank
monthly bench allowances, some capacity pre-consumed: accepts with
running counts and write arguments, one refusal on an exhausted
allowance, one for a seed bank with none on file, and a second batch
refused whole for one broken row. The ordered fold, whole-batch
refusal, and copy-ready write args are form; the keys, limits, and
write-call shape are content.

## Integration

A tool file with test.py. The briefing routes multi-item requests here
BEFORE any write: "plan the batch first; execute only lines the
planner accepted, arguments verbatim."

## Instrumentation

Count planned batches (adoption) and CATCH refusals (overshoots
prevented). Writes appearing without a matching ACCEPT line mean
bypassed routing — fix integration before touching the form.
