# Demo: classifying a pasted germination-log excerpt

`python3 test.py` — six lines are pasted verbatim: two clean accepts,
one row over its bench-zone cap (REJECT), one wilted row without a
pest-report tag (HOLD), one wilted row with a tag (ACCEPT), and one row a
tired nursery hand retyped in prose — the strict grammar refuses it with
a CATCH line rather than guessing. A second run against a corrupted
rules file refuses to classify anything.

Form (keep when re-domaining): the strict-grammar parse before any
judgment, CATCH refusal of rows that do not parse, one reasoned
`VERDICT|id|label|reason` line per record, and whole-run refusal when
the parameters themselves fail to parse.

Content (replace): the row grammar and its fields, the verdict labels,
and every rule parameter — your thresholds and codes come from
evidence, not from this demo.
