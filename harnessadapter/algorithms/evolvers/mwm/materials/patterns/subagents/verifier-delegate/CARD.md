name: verifier-delegate
when: decisions made late in long interactions are wrong more often than early ones, and re-checking within the same conversation keeps agreeing with them
uses: delegation, tool-output-surface

## Applicable when

Evidence shows wrong decisions late in long tasks that a fresh reader of
the same raw material gets right, and same-context checks that merely
restate it. Worth its cost only before steps that are expensive to undo.

## Skeleton

Before the step, the host delegates: a sub-agent that has not seen the
host's reasoning re-derives the decision from the raw material in the
task text (requester's words, tool outputs verbatim) and from what it
can check in the environment itself, and answers in ONE
machine-parseable line:

```
VERDICT: agree
VERDICT: differ <derived-value>
VERDICT: cannot-tell <why>
```

Anything unparseable IS cannot-tell — a defined fallback: it never
confirms; the host treats the step as "unverified". A second delegation
is the host's choice; two verdicts agreeing on a different value outrank
the host's own — a CATCH.

## Demo

See `demo/`. A seedling-bench assignment is verified against the
attached seedling record and zone rules: a mis-zoned proposal draws two
agreeing `differ` verdicts (CATCH); a sound one draws `agree`; a garbled
record makes the stub ramble; the fallback marks the step unverified.
Verdict grammar, parser fallback, and two-vote rule are form (keep);
records, rules, values are content (replace). The stub delegate is
deterministic — the form is the protocol, not the model.

## Integration

A spec whose prompt is the verdict grammar and the rule "raw material
and the environment only, never the host's reasoning"; its first line
names the step it verifies. The host decides whether to call it and what
to do with the verdict — nothing sits between the host and its action.

## Instrumentation

Count verifications (adoption), CATCHes, cannot-tell rate. A CATCH that
reversed a bad step is the value. High cannot-tell: the task text lacks
the deciding material. Zero CATCHes over many runs: retire it here.
