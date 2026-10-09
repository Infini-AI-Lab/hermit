name: preflight-checklist
when: irreversible actions launch on remembered prerequisites, and sometimes a prerequisite was never actually met
uses: stateful-tools, tool-output-surface

## Applicable when

Evidence shows a hard-to-undo action taken while a precondition was
false or never checked — the actor walked the list from memory, and
the miss surfaced only after the action landed. The fix: a gate
consulted immediately before the action, holding per-action
prerequisite lists in code.

## Skeleton

```
preflight(action_type, facts) -> GO line | CATCH line
  facts unparseable -> "CATCH: facts input unparseable; treating as
    NO-GO"           (never resolves toward GO)
  checklist = per_action_lists.get(action_type)
  checklist absent -> "CATCH: unknown action type; no checklist on
    file, refusing"   (no list means no launch, not a free pass)
  missing = [item for item in checklist if not facts.get(item)]
  missing -> "CATCH: <action> blocked - missing: <items>"
  else    -> "GO|<action>|satisfied=<items>"
```

Three commitments: every failure mode resolves to NO-GO; the GO line
names what was checked, so a stale checklist is visible; the gate reads
supplied facts, never gathers them — cheap enough to run every time.

## Demo

See `demo/`. A seedling transplant is gated on bed prepared,
hardening-off done, and pest check passed: one facts file passes GO,
one missing two items gets a CATCH listing both, one names an action
with no checklist (refused), one is broken JSON (NO-GO). The
gate-before-action shape, refuse-on-unknown, and fail-toward-NO-GO are
form; action names and checklist items are content.

## Integration

A tool file with test.py, plus one briefing line per gated action:
"before <action>, run preflight and proceed only on GO."

## Instrumentation

Count GO and CATCH lines per gated action. CATCHes hosts then work
around indicate a wrong checklist item; a long streak of pure GO
means the gate may be checking what no longer fails — trim it.
